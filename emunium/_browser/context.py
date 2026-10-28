"""Per-browser state shared by every element handle.

Contract: ``docs/architecture/python-internals.md`` (Browser context). Holds the
bridge, the headless / verify-input switches, the pointer calibration, the OS
interactor and the browser's process id; ``Element`` transactions
(``native-input.md`` section 4) read them.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from typing import Protocol

from emunium._bridge.facade import Bridge
from emunium._element_interactor import ElementInteractor
from emunium._motion import DEFAULT_TARGET_SIZE, MotionPlanner, TypingRhythm
from emunium._native.win32 import (
    CHROME_WINDOW_CLASS,
    GECKO_WINDOW_CLASS,
    Win32Windows,
    WindowApi,
    bring_to_foreground,
    descendant_pids,
    is_foreground,
    pick_browser_window,
    top_level_windows,
)
from emunium._screen import NATIVE_INPUT_LOCK, Calibration, Geometry, input_per_dip
from emunium.errors import InvalidArgumentError, NativeInputError

__all__ = ["BrowserContext", "DragInteractor", "Interactor"]

# Attribute on a bare Bridge holding its default context. Living on the bridge
# (not in a registry keyed by it) lets both be collected together.
_DEFAULT_ATTR = "_emunium_default_context"
_DEFAULTS_LOCK = threading.Lock()
# Window API failures that mean "the window could not be raised".
_WINDOW_ERRORS = (NativeInputError, OSError, ctypes.ArgumentError)
HEADLESS_HINT = (
    "launch with headless=False to use native input; DOM-only work does not need it"
)


def _on_windows() -> bool:
    return sys.platform == "win32"


class Interactor(Protocol):
    """The OS input surface an element transaction drives."""

    def position(self) -> tuple[int, int]: ...

    def move(
        self,
        x: float,
        y: float,
        *,
        human: bool = True,
        target_size: float | None = None,
    ) -> None:
        """Move to ``(x, y)`` (input px).

        ``target_size`` is Fitts' ``W`` in input px: element transactions pass
        the target's smaller visible side on every human move; instant moves
        and callers without a target leave it to the interactor's default.
        """
        ...

    def press(self, button: str = "left", *, clicks: int = 1) -> None: ...

    def button_down(self, button: str = "left") -> None: ...

    def button_up(self, button: str = "left") -> None: ...

    def press_escape(self) -> None: ...

    def can_land(self, x: int, y: int) -> bool: ...

    def type_text(
        self,
        text: str,
        *,
        characters_per_minute: int | None = None,
        offset: int | None = None,
        human: bool = True,
    ) -> None:
        """Type ``text``; None keeps the interactor's typing rhythm speed and jitter."""
        ...


def _corner_of(point: tuple[int, int], box: tuple[int, int, int, int]) -> bool:
    """True when ``point`` is a corner pixel of ``(left, top, width, height)``."""
    left, top, width, height = box
    return point[0] in (left, left + width - 1) and point[1] in (top, top + height - 1)


class DragInteractor(ElementInteractor):
    """``ElementInteractor`` plus the primitives a verified drag needs.

    A drag verifies the drop point while the button is held, so the press and
    the release cannot be one ``drag()`` call; an aborted drag is cancelled
    with Escape before the button is released.
    """

    def move(
        self,
        x: float,
        y: float,
        *,
        human: bool = True,
        target_size: float | None = None,
    ) -> None:
        """``ElementInteractor.move``; ``target_size=None`` is its default ``W``."""
        size = DEFAULT_TARGET_SIZE if target_size is None else target_size
        super().move(x, y, human=human, target_size=size)

    def button_down(self, button: str = "left") -> None:
        with NATIVE_INPUT_LOCK:
            self._gui().mouseDown(button=button, _pause=False)

    def button_up(self, button: str = "left") -> None:
        with NATIVE_INPUT_LOCK:
            # The fail-safe-free release path: a cursor parked in a screen
            # corner must not leave the button held.
            self._mouse_release(self._gui(), button)

    def press_escape(self) -> None:
        """Escape down/up; the release always runs and skips the fail-safe.

        The key-down uses the platform layer's raw ``_keyDown`` whenever the
        backend has one (pyautogui, the Windows backend), so a cursor in a
        fail-safe corner cannot refuse the Escape that cancels a drag; other
        backends fall back to ``keyDown``, which passes the fail-safe check.
        """
        gui = self._gui()
        with NATIVE_INPUT_LOCK:
            raw_down = getattr(getattr(gui, "platformModule", None), "_keyDown", None)

            def key_down() -> None:
                if raw_down is not None:
                    raw_down("escape")
                else:
                    gui.keyDown("escape", _pause=False)

            self._guarded(
                key_down,
                lambda: self._run_releases([lambda: self._key_release(gui, "escape")]),
            )

    def can_land(self, x: int, y: int) -> bool:
        """``(x, y)`` is on the virtual screen and not on a screen corner."""
        gui = self._gui()
        point = (x, y)
        width, height = gui.size()
        boxes = [(0, 0, int(width), int(height))]
        virtual_screen = getattr(gui, "virtual_screen", None)
        if callable(virtual_screen):
            left, top, v_width, v_height = virtual_screen()
            virtual = (int(left), int(top), int(v_width), int(v_height))
            if not (
                virtual[0] <= x < virtual[0] + virtual[2]
                and virtual[1] <= y < virtual[1] + virtual[3]
            ):
                return False
            boxes.append(virtual)
        return not any(_corner_of(point, box) for box in boxes)


class BrowserContext:
    def __init__(
        self,
        bridge: Bridge,
        *,
        headless: bool = False,
        verify_input: bool = True,
        calibration: Calibration | None = None,
        interactor: Interactor | None = None,
        pid: int | None = None,
        windows: WindowApi | None = None,
        motion: MotionPlanner | str | None = None,
        typing: TypingRhythm | str | None = None,
    ) -> None:
        """``motion`` picks the human pointer planner of the default interactor
        (a preset name, a ``MotionPlanner`` or None for the ``natural``
        ``SigmaDriftPlanner``); ``typing`` picks its typing rhythm (a preset
        name, a ``TypingRhythm`` or None for the ``natural`` preset). Neither
        can be combined with ``interactor``.
        """
        if motion is not None and interactor is not None:
            raise InvalidArgumentError(
                "motion configures the default interactor; pass motion or "
                "interactor, not both"
            )
        if typing is not None and interactor is not None:
            raise InvalidArgumentError(
                "typing configures the default interactor; pass typing or "
                "interactor, not both"
            )
        self.bridge = bridge
        self.headless = bool(headless)
        self.verify_input = bool(verify_input)
        self.calibration = calibration if calibration is not None else Calibration()
        # The interactor resolves its OS backend lazily, on the first event.
        self.interactor: Interactor = (
            interactor
            if interactor is not None
            else DragInteractor(motion=motion, typing=typing)
        )
        self.last_action_ms: float | None = None
        # The browser process launched for this context (set by Browser.launch);
        # None when the context only wraps a bridge.
        self.pid = pid
        # Window API, created on first use (Win32Windows needs Windows).
        self._windows = windows
        # Browser.launch/connect populate these before foreground selection.
        self.browser_family: str | None = None
        self.install_method: str | None = None

    @classmethod
    def of(cls, ctx_or_bridge: BrowserContext | Bridge) -> BrowserContext:
        """``ctx_or_bridge`` itself, or the shared default context of a bare bridge.

        Handles built from the same bridge share one context, so calibration is
        learned once per bridge rather than once per element.
        """
        if isinstance(ctx_or_bridge, BrowserContext):
            return ctx_or_bridge
        with _DEFAULTS_LOCK:
            context = getattr(ctx_or_bridge, _DEFAULT_ATTR, None)
            if not isinstance(context, BrowserContext):
                context = cls(ctx_or_bridge)
                setattr(ctx_or_bridge, _DEFAULT_ATTR, context)
            return context

    def mark_action(self) -> float:
        """Wall-clock epoch ms taken right before an action starts."""
        stamp = time.time() * 1000.0
        self.last_action_ms = stamp
        return stamp

    def require_native_input(self) -> None:
        if self.headless:
            raise NativeInputError(
                "native input is unavailable for a headless browser: there is no "
                "window to send OS events to",
                hint=HEADLESS_HINT,
            )

    @property
    def manages_foreground(self) -> bool:
        """True when ``ensure_foreground`` acts: Windows, headed, pid known."""
        return self.pid is not None and not self.headless and _on_windows()

    def ensure_foreground(self, geometry: Geometry | None) -> None:
        """Make the session's browser window the foreground window (Windows).

        The window is a visible top-level window of the browser process or a
        descendant whose rectangle matches ``geometry`` (``screenX``,
        ``screenY``, ``outerWidth``, ``outerHeight`` in DIP, scaled by
        ``input_per_dip``); without geometry only a single candidate counts.
        It is raised only when it is not already the foreground window. Sends
        no keyboard or mouse input.

        A no-op on other platforms (the extension's ``chrome.windows`` focus is
        all there is there), for a headless browser and when no pid is known
        (a context built around a bare bridge instead of ``Browser.launch``).

        Raises ``NativeInputError`` when the window cannot be found or raised.
        """
        pid = self.pid
        if pid is None or not self.manages_foreground:
            return
        try:
            api = self._window_api()
            class_name = (
                GECKO_WINDOW_CLASS
                if self.browser_family == "firefox" or self.install_method == "bidi"
                else CHROME_WINDOW_CLASS
            )
            candidates = top_level_windows(
                descendant_pids(pid, api), api, class_name=class_name
            )
            if geometry is None:
                window = pick_browser_window(candidates, None, 1.0)
            else:
                window = pick_browser_window(
                    candidates,
                    (
                        geometry.screen_x,
                        geometry.screen_y,
                        geometry.outer_width,
                        geometry.outer_height,
                    ),
                    input_per_dip(geometry),
                )
            if window is None:
                reason = (
                    f"none of the {len(candidates)} visible top-level window(s) of "
                    f"browser pid {pid} matches the page's window geometry"
                )
            elif is_foreground(window.hwnd, api) or bring_to_foreground(
                window.hwnd, api
            ):
                return
            else:
                reason = (
                    f"Windows kept another window in front of window {window.hwnd:#x} "
                    "(foreground lock)"
                )
        except _WINDOW_ERRORS as exc:
            raise NativeInputError(
                f"the browser window could not be brought to the foreground: {exc}"
            ) from exc
        raise NativeInputError(
            f"the browser window could not be brought to the foreground: {reason}; "
            "an occluded window receives no pointer events"
        )

    def _window_api(self) -> WindowApi:
        if self._windows is None:
            self._windows = Win32Windows()
        return self._windows
