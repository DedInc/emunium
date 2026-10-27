"""OS-level mouse and keyboard input.

Mouse and keys go through ``emunium._screen.input_backend()`` (native ctypes
on Windows, pyautogui elsewhere). Every call passes ``_pause=False``; no
pyautogui global (``PAUSE``, ``FAILSAFE`` ...) is changed. Every public method
holds ``NATIVE_INPUT_LOCK`` so concurrent callers cannot interleave OS events.
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import importlib
import logging
import math
import os
import random
import sys
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from emunium import _screen
from emunium._motion import (
    DEFAULT_TARGET_SIZE,
    MotionPlanner,
    MotionStep,
    ScreenBounds,
    TypingRhythm,
    resolve_motion,
    resolve_typing,
)
from emunium._native.win32 import (
    INPUT,
    KEYBDINPUT,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    VK_RETURN,
    VK_TAB,
    InputSender,
    KeyEvent,
    RawReleases,
    RawUser32,
    User32,
    keyboard_events,
    split_strokes,
    stroke_events,
)
from emunium._screen import CLIPBOARD_LOCK, NATIVE_INPUT_LOCK
from emunium._standalone.config import ClickType
from emunium.errors import NativeInputError

# The Windows keystroke helpers live in emunium._native.win32 and stay
# importable from here.
__all__ = [
    "DEFAULT_DOUBLE_CLICK_TIME",
    "KEYEVENTF_KEYUP",
    "KEYEVENTF_UNICODE",
    "MAX_DOUBLE_CLICK_INTERVAL",
    "PASTE_SETTLE_SECONDS",
    "VK_RETURN",
    "VK_TAB",
    "ElementInteractor",
    "KeyEvent",
    "WindowsKeyboardSender",
    "keyboard_events",
    "stroke_events",
]

# Held by the user, these turn injected text into shortcuts (Ctrl+A, Win+L ...).
_BLOCKING_MODIFIERS = ((0x11, "ctrl"), (0x12, "alt"), (0x5B, "lwin"), (0x5C, "rwin"))

# Fallback OS double-click time (s) when the platform does not expose one.
DEFAULT_DOUBLE_CLICK_TIME = 0.25
MAX_DOUBLE_CLICK_INTERVAL = 0.12
# Time the target application gets to read the clipboard before restore.
PASTE_SETTLE_SECONDS = 0.3
_BUTTONS = ("left", "right", "middle")
_CLICK_TYPES = {
    ClickType.LEFT: ("left", 1),
    ClickType.RIGHT: ("right", 1),
    ClickType.MIDDLE: ("middle", 1),
    ClickType.DOUBLE: ("left", 2),
}
_UNSET: Any = object()
# Control characters and lone surrogates: no backend types them as keystrokes.
_UNTYPEABLE_CATEGORIES = ("Cc", "Cs")
# winmm's success code for timeBeginPeriod.
_TIMERR_NOERROR = 0
# pyperclip raises PyperclipException (a RuntimeError) when no clipboard
# mechanism works, OSError when its helper (xclip, wl-copy, pbcopy) cannot run
# and UnicodeDecodeError (a ValueError) on undecodable clipboard content.
_CLIPBOARD_ERRORS = (RuntimeError, OSError, ValueError)
# keyboard.write: OSError from the injection call (uinput, SendInput, event
# tap), ImportError from its lazy root check on Linux, ValueError for a name it
# cannot map.
_KEYBOARD_WRITE_ERRORS = (OSError, ImportError, ValueError)
# Screen size lookup: the backend's NativeInputError, a failed or missing
# ctypes export, or an argument the ctypes prototype rejects.
_SCREEN_SIZE_ERRORS = (NativeInputError, OSError, AttributeError, ctypes.ArgumentError)

logger = logging.getLogger(__name__)


def _win_input_types() -> tuple[type[INPUT], type[KEYBDINPUT]]:
    """ctypes INPUT layout (40 bytes on x64, 28 on x86), shared with the backend."""
    return INPUT, KEYBDINPUT


class WindowsKeyboardSender:
    """Sends keyboard events through the win32 backend's ``InputSender``.

    ``user32`` is a raw user32-like object (``SendInput``/``GetAsyncKeyState``
    attributes); by default the backend's private ``WinDLL`` wrapper is used.
    """

    def __init__(self, user32: Any = None) -> None:
        api: User32 | RawUser32
        if user32 is None:
            api = User32()
            user32 = api.library
        else:
            api = RawUser32(user32)
        self._user32 = user32
        self._api = api
        self._sender = InputSender(api)

    def held_modifiers(self) -> list[str]:
        """Names of user-held modifiers that would turn text into shortcuts."""
        return [name for vk, name in _BLOCKING_MODIFIERS if self._api.key_held(vk)]

    def send(self, events: Sequence[KeyEvent]) -> None:
        self._sender.send_keys(events)


def _windows_double_click_time() -> float:
    import ctypes

    if sys.platform != "win32":
        return DEFAULT_DOUBLE_CLICK_TIME
    milliseconds = ctypes.windll.user32.GetDoubleClickTime()
    return milliseconds / 1000.0 if milliseconds else DEFAULT_DOUBLE_CLICK_TIME


def _usable_keyboard_module(platform: str) -> Any:
    """The ``keyboard`` package when it can inject keys here, else None."""
    if platform.startswith("linux") or platform == "darwin":
        geteuid = getattr(os, "geteuid", None)
        # keyboard injects through /dev/uinput or a root event tap.
        if geteuid is None or geteuid() != 0:
            return None
    try:
        # Optional extra (``emunium[keyboard]``), resolved only at runtime.
        return importlib.import_module("keyboard")
    except (ImportError, OSError):
        # Not installed or not root (ImportError); an unsupported platform or
        # an OS library it cannot load (OSError).
        return None


def _run_after_failure(releases: Sequence[Callable[[], None]], cause: str) -> None:
    """Run ``releases`` while an earlier error propagates.

    That earlier error wins, so a release's own error (an interrupt included)
    is recorded at debug level and the remaining releases still run.
    """
    for release in releases:
        try:
            release()
        except BaseException:
            logger.debug("Input release failed after %s", cause, exc_info=True)


def _off_corner(point: tuple[int, int], box: tuple[int, int] | None) -> tuple[int, int]:
    """Move an intermediate path point off a primary-screen fail-safe corner."""
    if box is None:
        return point
    width, height = box
    x, y = point
    if x in (0, width - 1) and y in (0, height - 1):
        return (1 if x == 0 else width - 2), y
    return point


def _onto_monitor(
    point: tuple[int, int], monitors: Sequence[tuple[int, int, int, int]]
) -> tuple[int, int]:
    """Clamp ``point`` into the nearest ``(left, top, width, height)`` monitor.

    A point already on a monitor is returned as is; otherwise the monitor at
    the smallest Euclidean distance wins (the first one on a tie).
    """
    x, y = point

    def distance(monitor: tuple[int, int, int, int]) -> float:
        left, top, width, height = monitor
        dx = max(left - x, 0, x - (left + width - 1))
        dy = max(top - y, 0, y - (top + height - 1))
        return math.hypot(dx, dy)

    left, top, width, height = min(monitors, key=distance)
    return (
        min(max(x, left), left + width - 1),
        min(max(y, top), top + height - 1),
    )


def _load_winmm() -> Any:
    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        raise OSError("ctypes.WinDLL is unavailable")
    return win_dll("winmm")


@contextlib.contextmanager
def _fine_timer(
    platform: str, load_winmm: Callable[[], Any] | None = None
) -> Iterator[None]:
    """1 ms Windows timer resolution while a human move plays.

    The default 15.6 ms tick would stretch every short sleep of the
    playback. ``timeBeginPeriod(1)`` is undone by ``timeEndPeriod(1)`` on any
    exit; nothing happens off Windows or when winmm cannot be used.
    """
    end_period: Any = None
    if platform == "win32":
        try:
            winmm = (load_winmm or _load_winmm)()
            begin, end = winmm.timeBeginPeriod, winmm.timeEndPeriod
            for function in (begin, end):
                function.argtypes = [ctypes.c_uint]
                function.restype = ctypes.c_uint
            if begin(1) == _TIMERR_NOERROR:
                end_period = end
        except (OSError, AttributeError):
            end_period = None
    try:
        yield
    finally:
        if end_period is not None:
            end_period(1)


class ElementInteractor:
    """OS input; ``move(human=True)`` follows the ``motion`` planner.

    ``motion`` is a preset name (``natural``, ``careful``, ``brisk``), a
    ``MotionPlanner`` or None (``SigmaDriftPlanner.preset("natural")``); see
    ``emunium._motion``. ``typing`` is a preset name, a ``TypingRhythm`` or
    None (``TypingRhythm.preset("natural")``); it paces
    ``type_text(human=True)``. ``rng`` feeds every random choice, the
    planner's and the rhythm's too. ``clock`` (default ``time.perf_counter``,
    also when only ``sleep`` is injected) times the playback of a planned move.
    """

    def __init__(
        self,
        *,
        backend: Any = None,
        platform: str | None = None,
        unicode_sender: Any = None,
        clipboard: Any = None,
        keyboard: Any = _UNSET,
        double_click_time: float | None = None,
        sleep: Callable[[float], None] | None = None,
        rng: random.Random | None = None,
        motion: MotionPlanner | str | None = None,
        clock: Callable[[], float] | None = None,
        typing: TypingRhythm | str | None = None,
    ) -> None:
        self._motion = resolve_motion(motion)
        self._typing = resolve_typing(typing)
        self._backend = backend
        self._platform = platform or sys.platform
        self._unicode_sender = unicode_sender
        self._clipboard = clipboard
        self._keyboard = keyboard
        self._double_click_time = double_click_time
        self._sleep: Callable[[float], None] = sleep or time.sleep
        self._clock: Callable[[], float] = clock or time.perf_counter
        self._rng = rng or random.Random()
        self._init_lock = threading.Lock()

    # -- lazy collaborators -------------------------------------------------

    def _gui(self) -> Any:
        if self._backend is None:
            self._backend = _screen.input_backend()
        return self._backend

    def _sender(self) -> Any:
        with self._init_lock:
            if self._unicode_sender is None:
                self._unicode_sender = WindowsKeyboardSender()
            return self._unicode_sender

    def _clip(self) -> Any:
        with self._init_lock:
            if self._clipboard is None:
                import pyperclip

                self._clipboard = pyperclip
            return self._clipboard

    def _keyboard_module(self) -> Any:
        with self._init_lock:
            if self._keyboard is _UNSET:
                self._keyboard = _usable_keyboard_module(self._platform)
            return self._keyboard

    def double_click_interval(self) -> float:
        if self._double_click_time is None:
            try:
                if self._platform == "win32":
                    self._double_click_time = _windows_double_click_time()
                else:
                    self._double_click_time = DEFAULT_DOUBLE_CLICK_TIME
            except (OSError, AttributeError):
                # user32 failed to load, or has no GetDoubleClickTime export.
                self._double_click_time = DEFAULT_DOUBLE_CLICK_TIME
        return min(0.5 * self._double_click_time, MAX_DOUBLE_CLICK_INTERVAL)

    # -- mouse ---------------------------------------------------------------

    def position(self) -> tuple[int, int]:
        point = self._gui().position()
        return int(point[0]), int(point[1])

    @property
    def motion(self) -> MotionPlanner:
        """The planner ``move(human=True)`` follows."""
        return self._motion

    @property
    def typing(self) -> TypingRhythm:
        """The rhythm ``type_text(human=True)`` follows."""
        return self._typing

    def move(
        self,
        x: float,
        y: float,
        *,
        human: bool = True,
        target_size: float = DEFAULT_TARGET_SIZE,
    ) -> None:
        """Move to ``(x, y)``; ``target_size`` (input px) is Fitts' ``W``."""
        target = (round(float(x)), round(float(y)))
        with NATIVE_INPUT_LOCK:
            if human:
                self._human_move(target, target_size)
            self._land(target)

    def press(self, button: str = "left", *, clicks: int = 1) -> None:
        if button not in _BUTTONS:
            raise ValueError(f"button must be one of {_BUTTONS}, got {button!r}")
        if isinstance(clicks, bool) or not isinstance(clicks, int) or clicks < 1:
            raise ValueError(f"clicks must be a positive int, got {clicks!r}")
        gui = self._gui()
        with NATIVE_INPUT_LOCK:
            interval = self.double_click_interval() if clicks > 1 else 0.0
            for index in range(clicks):
                hold = self._rng.uniform(0.03, 0.06)
                if clicks > 1:
                    hold = min(hold, interval / 2.0)
                self._button_down_up(gui, button, hold)
                if index < clicks - 1:
                    self._sleep(interval)

    def click(
        self,
        x: float,
        y: float,
        *,
        click_type: ClickType = ClickType.LEFT,
        human: bool = True,
    ) -> None:
        if click_type not in _CLICK_TYPES:
            raise ValueError(f"unsupported click_type {click_type!r}")
        button, clicks = _CLICK_TYPES[click_type]
        with NATIVE_INPUT_LOCK:
            self.move(x, y, human=human)
            self.press(button, clicks=clicks)

    def drag(
        self,
        start: Sequence[float],
        end: Sequence[float],
        *,
        human: bool = True,
    ) -> None:
        gui = self._gui()
        with NATIVE_INPUT_LOCK:
            self.move(start[0], start[1], human=human)
            target = (round(float(end[0])), round(float(end[1])))

            def drag_with_button_down() -> None:
                gui.mouseDown(button="left", _pause=False)
                self._sleep(self._rng.uniform(0.05, 0.1) if human else 0.05)
                if human:
                    self._human_move(target, DEFAULT_TARGET_SIZE)
                else:
                    self._linear_move(target, steps=10, duration=0.2)
                self._land(target)
                self._sleep(self._rng.uniform(0.05, 0.1) if human else 0.05)

            self._guarded(
                drag_with_button_down,
                lambda: self._run_releases([lambda: self._mouse_release(gui, "left")]),
            )

    # Pre-3.1 names still used by emunium.element.
    def screen_point(
        self,
        screen_x: float,
        screen_y: float,
        *,
        offset_x: float | None = None,
        offset_y: float | None = None,
    ) -> tuple[int, int]:
        x_offset = offset_x if offset_x is not None else self._rng.uniform(0.0, 1.5)
        y_offset = offset_y if offset_y is not None else self._rng.uniform(0.0, 1.5)
        return round(float(screen_x + x_offset)), round(float(screen_y + y_offset))

    def move_cursor(self, x: float, y: float, *, human: bool = True) -> None:
        self.move(x, y, human=human)

    def _button_down_up(self, gui: Any, button: str, hold: float) -> None:
        def hold_button() -> None:
            gui.mouseDown(button=button, _pause=False)
            self._sleep(hold)

        self._guarded(
            hold_button,
            lambda: self._run_releases([lambda: self._mouse_release(gui, button)]),
        )

    # Releases bypass pyautogui's public API: its fail-safe check raises when
    # the cursor sits in a screen corner, which would leave the button down.
    @staticmethod
    def _mouse_release(gui: Any, button: str) -> None:
        platform_module = getattr(gui, "platformModule", None)
        release = getattr(platform_module, "_mouseUp", None)
        if release is None:
            gui.mouseUp(button=button, _pause=False)
            return
        if isinstance(platform_module, RawReleases):
            # The native layer releases where the cursor is and ignores the
            # coordinates; reading them could fail and leave the button down.
            release(0, 0, button)
            return
        raw_position = getattr(platform_module, "_position", None)
        point = raw_position() if raw_position is not None else gui.position()
        release(int(point[0]), int(point[1]), button)

    @staticmethod
    def _key_release(gui: Any, key: str) -> None:
        platform_module = getattr(gui, "platformModule", None)
        release = getattr(platform_module, "_keyUp", None)
        if release is None:
            gui.keyUp(key, _pause=False)
            return
        # pyautogui.keyUp lower-cases key names before the platform call.
        release(key.lower() if len(key) > 1 else key)

    @staticmethod
    def _run_releases(releases: Sequence[Callable[[], None]]) -> None:
        """Run every release even if one is interrupted; the first error propagates."""
        for index, release in enumerate(releases):
            try:
                release()
            except BaseException:
                _run_after_failure(releases[index + 1 :], "an earlier release failed")
                raise

    @staticmethod
    def _guarded(action: Callable[[], None], release_all: Callable[[], None]) -> None:
        """Run ``action`` then ``release_all`` no matter what; the action's error wins."""
        try:
            action()
        except BaseException:
            _run_after_failure([release_all], "the guarded action failed")
            raise
        release_all()

    def _land(self, target: tuple[int, int]) -> None:
        gui = self._gui()
        for _ in range(2):
            gui.moveTo(target[0], target[1], _pause=False)
            if self.position() == target:
                return
        raise NativeInputError(
            f"cursor did not land on {target!r} (at {self.position()!r}); the point may be off-screen"
        )

    def _screen_box(self) -> tuple[int, int] | None:
        try:
            size = self._gui().size()
            return int(size[0]), int(size[1])
        except _SCREEN_SIZE_ERRORS:
            return None

    def _motion_bounds(
        self, start: tuple[int, int], end: tuple[int, int]
    ) -> ScreenBounds | None:
        """Virtual screen (else the primary monitor), widened to hold both ends.

        A backend without ``virtual_screen`` only reports the primary
        monitor; an end outside it proves the screen extends there.
        """
        primary = self._screen_box()
        virtual: tuple[int, int, int, int] | None = None
        virtual_screen = getattr(self._gui(), "virtual_screen", None)
        if callable(virtual_screen):
            try:
                left, top, width, height = virtual_screen()
                virtual = (int(left), int(top), int(width), int(height))
            except (OSError, RuntimeError, TypeError, ValueError):
                virtual = None
        if virtual is None and primary is not None:
            virtual = (0, 0, primary[0], primary[1])
        if virtual is None or virtual[2] < 1 or virtual[3] < 1:
            return None
        if primary is not None and (primary[0] < 1 or primary[1] < 1):
            primary = None
        return ScreenBounds(*virtual, primary=primary).covering(start, end)

    def _monitor_rects(self) -> list[tuple[int, int, int, int]]:
        """``(left, top, width, height)`` of every real monitor; [] when unknown."""
        monitors = getattr(self._gui(), "monitors", None)
        if not callable(monitors):
            return []
        try:
            rects = [
                (int(left), int(top), int(width), int(height))
                for left, top, width, height in monitors()
            ]
        except (OSError, RuntimeError, TypeError, ValueError, ctypes.ArgumentError):
            return []
        return [rect for rect in rects if rect[2] >= 1 and rect[3] >= 1]

    def _onto_monitors(
        self, steps: Sequence[MotionStep], bounds: ScreenBounds | None
    ) -> list[MotionStep]:
        """Every intermediate step onto its nearest monitor, then off the corners.

        The virtual screen is the monitors' bounding box; on a mixed layout a
        planned point can fall in a dead zone that belongs to no monitor, and
        ``SetCursorPos`` would clip it somewhere unplanned (possibly a
        fail-safe corner). The corner rule (``ScreenBounds.settle``) runs
        after the clamp, so a clamped point never rests on a corner. The last
        step, the target itself, is never altered.
        """
        monitors = self._monitor_rects()
        if not monitors:
            return list(steps)
        fitted: list[MotionStep] = []
        for step in steps[:-1]:
            point = _onto_monitor((step.x, step.y), monitors)
            if bounds is not None:
                point = bounds.settle(point)
            moved = point != (step.x, step.y)
            fitted.append(MotionStep(point[0], point[1], step.dt) if moved else step)
        fitted.extend(steps[-1:])
        return fitted

    def _human_move(self, target: tuple[int, int], target_size: float) -> None:
        """Walk the planner's steps; the caller lands on ``target``."""
        gui = self._gui()
        start = self.position()
        bounds = self._motion_bounds(start, target)
        steps = self._motion.plan(
            start, target, target_size=target_size, rng=self._rng, bounds=bounds
        )
        steps = self._onto_monitors(steps, bounds)
        with _fine_timer(self._platform):
            self._play(gui, steps)

    def _play(self, gui: Any, steps: Sequence[MotionStep]) -> None:
        """Play ``steps`` against absolute deadlines from the clock.

        Step ``i`` is due at ``t0 + dt_1 + ... + dt_i``; each wait sleeps only
        the time left to its deadline, so coarse sleeps cannot add up. A wait
        never exceeds the step's own ``dt``: after a sleep that returned at
        or past the previous deadline the time left is at most ``dt`` anyway,
        and a sleep that returned early (or a stub that does not sleep)
        cannot turn into one long stall. An
        intermediate step is dropped rather than delayed once it is late by
        one step, i.e. the next step is already due: a sleep that overran a
        15.6 ms tick yields one move per tick, never a burst. The last step's
        deadline is always waited for (the caller then lands on it).
        """
        deadline = self._clock()
        last = len(steps) - 1
        for index, step in enumerate(steps):
            deadline += step.dt
            now = self._clock()
            if now < deadline:
                self._sleep(min(deadline - now, step.dt))
                now = self._clock()
            if index == last:
                break
            if now >= deadline + steps[index + 1].dt:
                continue
            gui.moveTo(step.x, step.y, _pause=False)

    def _linear_move(
        self, target: tuple[int, int], *, steps: int, duration: float
    ) -> None:
        gui = self._gui()
        sx, sy = self.position()
        box = self._screen_box()
        for index in range(1, steps):
            t = index / float(steps)
            point = _off_corner(
                (
                    round(float(sx + (target[0] - sx) * t)),
                    round(float(sy + (target[1] - sy) * t)),
                ),
                box,
            )
            gui.moveTo(point[0], point[1], _pause=False)
            self._sleep(duration / steps)

    # -- keyboard ------------------------------------------------------------

    def type_text(
        self,
        text: str,
        *,
        characters_per_minute: int | None = None,
        offset: int | None = None,
        human: bool = True,
    ) -> None:
        """Type ``text``; with ``human`` each keystroke waits ``typing.interval``.

        ``characters_per_minute`` (60-900) sets the rhythm's median speed and
        ``offset`` (ms, below the median interval) its ``jitter_ms``; None
        keeps the rhythm's own value. Out-of-range values raise
        ``InvalidArgumentError``. A ``characters_per_minute`` of 0 or less
        still types without delay, as ``human=False`` does.
        """
        if not isinstance(text, str):
            raise TypeError(f"text must be str, got {type(text).__name__!r}")
        rhythm: TypingRhythm | None = None
        instant = (
            isinstance(characters_per_minute, (int, float))
            and not isinstance(characters_per_minute, bool)
            and characters_per_minute <= 0
        )
        if human and not instant:
            rhythm = self._typing.adjusted(characters_per_minute, offset)
        if not text:
            return
        with NATIVE_INPUT_LOCK:
            if self._platform == "win32":
                self._type_windows(text, rhythm)
            else:
                self._type_posix(text, rhythm)

    def paste_text(self, text: str) -> None:
        if not text:
            return
        clipboard = self._clip()
        modifier = "command" if self._platform == "darwin" else "ctrl"
        with NATIVE_INPUT_LOCK, CLIPBOARD_LOCK:
            try:
                previous = clipboard.paste()
                restore = isinstance(previous, str)
            except _CLIPBOARD_ERRORS:
                previous, restore = None, False
            try:
                clipboard.copy(text)
            except Exception as exc:
                raise NativeInputError(f"clipboard is unavailable: {exc}") from exc
            try:
                self.hotkey(modifier, "v")
                # Restoring before the target has read the clipboard pastes the old text.
                self._sleep(PASTE_SETTLE_SECONDS)
            finally:
                if restore:
                    try:
                        if clipboard.paste() == text:
                            clipboard.copy(previous)
                    except _CLIPBOARD_ERRORS:
                        logger.debug(
                            "Clipboard restore after paste failed", exc_info=True
                        )

    def hotkey(self, *keys: str) -> None:
        if not keys:
            raise ValueError("hotkey needs at least one key")
        gui = self._gui()
        with NATIVE_INPUT_LOCK:
            pressed: list[str] = []

            def press_all() -> None:
                for key in keys:
                    # Recorded first: a key interrupted mid-press is still released.
                    pressed.append(key)
                    gui.keyDown(key, _pause=False)

            self._guarded(
                press_all,
                lambda: self._run_releases(
                    [
                        functools.partial(self._key_release, gui, key)
                        for key in reversed(pressed)
                    ]
                ),
            )

    def _pace(
        self, rhythm: TypingRhythm | None, typed: str, upcoming: str | None
    ) -> None:
        """Wait the rhythm's interval from ``typed`` to ``upcoming``.

        After the last keystroke (``upcoming`` None) the wait still runs: a
        long text reaches here in chunks, and the next chunk must not follow
        without a pause.
        """
        if rhythm is None:
            return
        self._sleep(rhythm.interval(typed, upcoming, self._rng))

    @staticmethod
    def _check_modifiers(sender: Any) -> None:
        held_modifiers = getattr(sender, "held_modifiers", None)
        if held_modifiers is None:
            return
        held = held_modifiers()
        if held:
            raise NativeInputError(
                "modifier key held by the user ({}); typing would trigger "
                "shortcuts, release it and retry".format(", ".join(held))
            )

    def _type_windows(self, text: str, rhythm: TypingRhythm | None) -> None:
        sender = self._sender()
        if rhythm is None:
            self._check_modifiers(sender)
            sender.send(keyboard_events(text))
            return
        strokes = split_strokes(text)
        for index, stroke in enumerate(strokes):
            self._check_modifiers(sender)
            # Down and up go out in one SendInput batch: no dwell between them.
            sender.send(stroke_events(stroke))
            upcoming = strokes[index + 1] if index + 1 < len(strokes) else None
            self._pace(rhythm, stroke, upcoming)

    @staticmethod
    def _writes_unicode(gui: Any) -> bool:
        """True when the backend declares it types any Unicode character itself.

        Only an explicit ``supports_unicode_write = True`` counts: pyautogui
        lacks the attribute, and a truthy stand-in must not enable it.
        """
        return getattr(gui, "supports_unicode_write", False) is True

    @staticmethod
    def _is_backend_stroke(stroke: str, native_unicode: bool) -> bool:
        """True when ``gui.write``/``gui.press`` can type ``stroke``.

        pyautogui types printable ASCII only; a native Unicode backend types
        every character except control characters and lone surrogates.
        """
        if stroke in ("\n", "\t"):
            return True
        if native_unicode:
            return unicodedata.category(stroke) not in _UNTYPEABLE_CATEGORIES
        return stroke.isascii() and stroke.isprintable()

    def _type_posix(self, text: str, rhythm: TypingRhythm | None) -> None:
        gui = self._gui()
        native_unicode = self._writes_unicode(gui)
        strokes = split_strokes(text)
        index = 0
        while index < len(strokes):
            stroke = strokes[index]
            if self._is_backend_stroke(stroke, native_unicode):
                # One write per keystroke, press and release in one call (no
                # dwell between), so every character gets its own wait.
                if stroke == "\n":
                    gui.press("enter", _pause=False)
                elif stroke == "\t":
                    gui.press("tab", _pause=False)
                else:
                    gui.write(stroke, _pause=False)
                upcoming = strokes[index + 1] if index + 1 < len(strokes) else None
                self._pace(rhythm, stroke, upcoming)
                index += 1
                continue
            run_end = index
            while run_end < len(strokes) and not self._is_backend_stroke(
                strokes[run_end], native_unicode
            ):
                run_end += 1
            following = strokes[run_end] if run_end < len(strokes) else None
            self._type_other("".join(strokes[index:run_end]), rhythm, following)
            index = run_end

    def _type_other(
        self, chunk: str, rhythm: TypingRhythm | None, following: str | None
    ) -> None:
        """Characters the backend cannot type: ``keyboard`` package, else paste.

        ``following`` is the keystroke after ``chunk`` (None at the end).
        """
        keyboard = self._keyboard_module()
        if keyboard is not None:
            for position, character in enumerate(chunk):
                try:
                    keyboard.write(character)
                except _KEYBOARD_WRITE_ERRORS:
                    self.paste_text(chunk[position:])
                    self._pace(rhythm, chunk, following)
                    return
                upcoming = chunk[position + 1 : position + 2] or following
                self._pace(rhythm, character, upcoming)
            return
        # One paste per run: a paste per character would cost a settle each.
        self.paste_text(chunk)
        self._pace(rhythm, chunk, following)
