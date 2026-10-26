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
import os
import random
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from emunium import _screen
from emunium._motion import (
    DEFAULT_TARGET_SIZE,
    MotionPlanner,
    MotionStep,
    ScreenBounds,
    resolve_motion,
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
# winmm's success code for timeBeginPeriod.
_TIMERR_NOERROR = 0


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
    except Exception:
        return None


def _off_corner(point: tuple[int, int], box: tuple[int, int] | None) -> tuple[int, int]:
    """Move an intermediate path point off a primary-screen fail-safe corner."""
    if box is None:
        return point
    width, height = box
    x, y = point
    if x in (0, width - 1) and y in (0, height - 1):
        return (1 if x == 0 else width - 2), y
    return point


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
    ``emunium._motion``. ``rng`` feeds every random choice, the planner's too.
    ``clock`` (default ``time.perf_counter``, also when only ``sleep`` is
    injected) times the playback of a planned move.
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
    ) -> None:
        self._motion = resolve_motion(motion)
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
            except Exception:
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

    def move(
        self,
        x: float,
        y: float,
        *,
        human: bool = True,
        target_size: float = DEFAULT_TARGET_SIZE,
    ) -> None:
        """Move to ``(x, y)``; ``target_size`` (input px) is Fitts' ``W``."""
        target = (int(round(x)), int(round(y)))
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
            target = (int(round(end[0])), int(round(end[1])))

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
        return int(round(screen_x + x_offset)), int(round(screen_y + y_offset))

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
    def _run_releases(
        releases: Sequence[Callable[[], None]],
    ) -> BaseException | None:
        """Run every release even if one is interrupted; return the first error."""
        first: BaseException | None = None
        for release in releases:
            try:
                release()
            except BaseException as exc:
                if first is None:
                    first = exc
        return first

    @staticmethod
    def _guarded(
        action: Callable[[], None],
        release_all: Callable[[], BaseException | None],
    ) -> None:
        """Run ``action`` then ``release_all`` no matter what; the action's error wins."""
        error: BaseException | None = None
        try:
            action()
        except BaseException as exc:
            error = exc
        release_error = release_all()
        if error is not None:
            raise error
        if release_error is not None:
            raise release_error

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
        except Exception:
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

    def _human_move(self, target: tuple[int, int], target_size: float) -> None:
        """Walk the planner's steps; the caller lands on ``target``."""
        gui = self._gui()
        start = self.position()
        steps = self._motion.plan(
            start,
            target,
            target_size=target_size,
            rng=self._rng,
            bounds=self._motion_bounds(start, target),
        )
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
                    int(round(sx + (target[0] - sx) * t)),
                    int(round(sy + (target[1] - sy) * t)),
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
        characters_per_minute: int = 280,
        offset: int = 20,
        human: bool = True,
    ) -> None:
        if not isinstance(text, str):
            raise TypeError(f"text must be str, got {type(text).__name__!r}")
        if not text:
            return
        delay = (
            60.0 / characters_per_minute if human and characters_per_minute > 0 else 0.0
        )
        with NATIVE_INPUT_LOCK:
            if self._platform == "win32":
                self._type_windows(text, delay, offset)
            else:
                self._type_posix(text, delay, offset)

    def paste_text(self, text: str) -> None:
        if not text:
            return
        clipboard = self._clip()
        modifier = "command" if self._platform == "darwin" else "ctrl"
        with NATIVE_INPUT_LOCK, CLIPBOARD_LOCK:
            try:
                previous = clipboard.paste()
                restore = isinstance(previous, str)
            except Exception:
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
                    except Exception:
                        pass

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

    def _pace(self, delay: float, offset: int) -> None:
        if delay <= 0:
            return
        self._sleep(max(0.0, delay + self._rng.uniform(-offset, offset) / 1000.0))

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

    def _type_windows(self, text: str, delay: float, offset: int) -> None:
        sender = self._sender()
        if delay <= 0:
            self._check_modifiers(sender)
            sender.send(keyboard_events(text))
            return
        for stroke in split_strokes(text):
            self._check_modifiers(sender)
            sender.send(stroke_events(stroke))
            self._pace(delay, offset)

    @staticmethod
    def _is_pyautogui_stroke(stroke: str) -> bool:
        return stroke in ("\n", "\t") or (stroke.isascii() and stroke.isprintable())

    def _type_posix(self, text: str, delay: float, offset: int) -> None:
        gui = self._gui()
        strokes = split_strokes(text)
        index = 0
        while index < len(strokes):
            stroke = strokes[index]
            if self._is_pyautogui_stroke(stroke):
                if stroke == "\n":
                    gui.press("enter", _pause=False)
                elif stroke == "\t":
                    gui.press("tab", _pause=False)
                else:
                    gui.write(stroke, _pause=False)
                self._pace(delay, offset)
                index += 1
                continue
            run_end = index
            while run_end < len(strokes) and not self._is_pyautogui_stroke(
                strokes[run_end]
            ):
                run_end += 1
            self._type_other("".join(strokes[index:run_end]), delay, offset)
            index = run_end

    def _type_other(self, chunk: str, delay: float, offset: int) -> None:
        """Characters pyautogui cannot type: ``keyboard`` package, else paste."""
        keyboard = self._keyboard_module()
        if keyboard is not None:
            for position, character in enumerate(chunk):
                try:
                    keyboard.write(character)
                except Exception:
                    self.paste_text(chunk[position:])
                    self._pace(delay, offset)
                    return
                self._pace(delay, offset)
            return
        # One paste per run: a paste per character would cost a settle each.
        self.paste_text(chunk)
        self._pace(delay, offset)
