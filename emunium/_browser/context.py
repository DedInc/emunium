"""Per-browser state shared by every element handle.

Contract: ``docs/architecture/python-internals.md`` (Browser context). Holds the
bridge, the headless / verify-input switches, the pointer calibration and the OS
interactor; ``Element`` transactions (``native-input.md`` section 4) read them.
"""

from __future__ import annotations

import threading
import time
from typing import Protocol

from emunium._bridge.facade import Bridge
from emunium._element_interactor import ElementInteractor
from emunium._screen import NATIVE_INPUT_LOCK, Calibration
from emunium.errors import NativeInputError

__all__ = ["BrowserContext", "DragInteractor", "Interactor"]

# Attribute on a bare Bridge holding its default context. Living on the bridge
# (not in a registry keyed by it) lets both be collected together.
_DEFAULT_ATTR = "_emunium_default_context"
_DEFAULTS_LOCK = threading.Lock()


class Interactor(Protocol):
    """The OS input surface an element transaction drives."""

    def position(self) -> tuple[int, int]: ...

    def move(self, x: float, y: float, *, human: bool = True) -> None: ...

    def press(self, button: str = "left", *, clicks: int = 1) -> None: ...

    def button_down(self, button: str = "left") -> None: ...

    def button_up(self, button: str = "left") -> None: ...

    def press_escape(self) -> None: ...

    def can_land(self, x: int, y: int) -> bool: ...

    def type_text(
        self,
        text: str,
        *,
        characters_per_minute: int = 280,
        offset: int = 20,
        human: bool = True,
    ) -> None: ...


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

        The key-down uses the platform layer's raw ``_keyDown`` when the
        backend has one (pyautogui); the native backends only expose raw
        releases, so there the key-down passes the fail-safe check.
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
    ) -> None:
        self.bridge = bridge
        self.headless = bool(headless)
        self.verify_input = bool(verify_input)
        self.calibration = calibration if calibration is not None else Calibration()
        # The interactor resolves its OS backend lazily, on the first event.
        self.interactor: Interactor = (
            interactor if interactor is not None else DragInteractor()
        )
        self.last_action_ms: float | None = None

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
                "window to send OS events to"
            )
