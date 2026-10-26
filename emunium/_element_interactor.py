"""OS-level mouse and keyboard input.

Every call passes ``_pause=False`` to pyautogui; no pyautogui global
(``PAUSE``, ``FAILSAFE`` ...) is changed. Every public method holds
``NATIVE_INPUT_LOCK`` so concurrent callers cannot interleave OS events.
"""

from __future__ import annotations

import functools
import math
import os
import random
import sys
import threading
import time
from typing import Any, Callable, List, Optional, Sequence, Tuple

from emunium import _screen
from emunium._screen import CLIPBOARD_LOCK, NATIVE_INPUT_LOCK
from emunium._standalone.config import ClickType
from emunium.errors import NativeInputError

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_RETURN = 0x0D
VK_TAB = 0x09
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

KeyEvent = Tuple[int, int, int]  # (virtual key, scan / UTF-16 unit, flags)


def _strokes(text: str) -> List[str]:
    """Split text into keystrokes; ``\\r\\n`` is one Enter."""
    strokes: List[str] = []
    index = 0
    while index < len(text):
        if text.startswith("\r\n", index):
            strokes.append("\n")
            index += 2
            continue
        character = text[index]
        strokes.append("\n" if character == "\r" else character)
        index += 1
    return strokes


def stroke_events(stroke: str) -> List[KeyEvent]:
    """SendInput keyboard events for one keystroke (layout independent)."""
    if stroke == "\n":
        return [(VK_RETURN, 0, 0), (VK_RETURN, 0, KEYEVENTF_KEYUP)]
    if stroke == "\t":
        return [(VK_TAB, 0, 0), (VK_TAB, 0, KEYEVENTF_KEYUP)]
    encoded = stroke.encode("utf-16-le")
    events: List[KeyEvent] = []
    for offset in range(0, len(encoded), 2):
        unit = int.from_bytes(encoded[offset : offset + 2], "little")
        events.append((0, unit, KEYEVENTF_UNICODE))
        events.append((0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    return events


def _stroke_groups(events: Sequence[KeyEvent]) -> List[List[KeyEvent]]:
    """Group flat events into keystrokes; a surrogate pair stays one group."""
    groups: List[List[KeyEvent]] = []
    current: List[KeyEvent] = []
    for event in events:
        current.append(event)
        vk, scan, flags = event
        released = bool(flags & KEYEVENTF_KEYUP)
        high_surrogate = bool(flags & KEYEVENTF_UNICODE) and 0xD800 <= scan <= 0xDBFF
        if released and not high_surrogate:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def keyboard_events(text: str) -> List[KeyEvent]:
    events: List[KeyEvent] = []
    for stroke in _strokes(text):
        events.extend(stroke_events(stroke))
    return events


@functools.lru_cache(maxsize=None)
def _win_input_types() -> Tuple[Any, Any]:
    """ctypes INPUT layout (40 bytes on x64, 28 on x86)."""
    import ctypes

    ulong_ptr = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ulong_ptr),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.c_ushort),
            ("wScan", ctypes.c_ushort),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ulong_ptr),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", ctypes.c_ulong),
            ("wParamL", ctypes.c_ushort),
            ("wParamH", ctypes.c_ushort),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]

    return INPUT, KEYBDINPUT


class WindowsKeyboardSender:
    """Sends keyboard events through ``user32.SendInput``."""

    _CHUNK = 64

    def __init__(self, user32: Any = None) -> None:
        import ctypes

        self._ctypes = ctypes
        self._input_type, _ = _win_input_types()
        if user32 is None:
            # A private WinDLL keeps argtypes off the shared windll.user32
            # that pyautogui uses.
            user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
            user32.SendInput.argtypes = [
                ctypes.c_uint,
                ctypes.POINTER(self._input_type),
                ctypes.c_int,
            ]
            user32.SendInput.restype = ctypes.c_uint
            user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
            user32.GetAsyncKeyState.restype = ctypes.c_short
        self._user32 = user32

    def held_modifiers(self) -> List[str]:
        """Names of user-held modifiers that would turn text into shortcuts."""
        held = []
        for vk, name in _BLOCKING_MODIFIERS:
            if self._user32.GetAsyncKeyState(vk) & 0x8000:
                held.append(name)
        return held

    def build(self, events: Sequence[KeyEvent]) -> Any:
        array = (self._input_type * len(events))()
        for index, (vk, scan, flags) in enumerate(events):
            array[index].type = INPUT_KEYBOARD
            array[index].ki.wVk = vk
            array[index].ki.wScan = scan
            array[index].ki.dwFlags = flags
            array[index].ki.time = 0
            array[index].ki.dwExtraInfo = 0
        return array

    def _chunks(self, events: Sequence[KeyEvent]) -> List[List[KeyEvent]]:
        """Chunks of at most _CHUNK events, cut only between keystrokes."""
        chunks: List[List[KeyEvent]] = []
        current: List[KeyEvent] = []
        for stroke in _stroke_groups(events):
            if current and len(current) + len(stroke) > self._CHUNK:
                chunks.append(current)
                current = []
            current.extend(stroke)
        if current:
            chunks.append(current)
        return chunks

    def send(self, events: Sequence[KeyEvent]) -> None:
        size = self._ctypes.sizeof(self._input_type)
        for chunk in self._chunks(events):
            array = self.build(chunk)
            sent = self._user32.SendInput(len(chunk), array, size)
            if sent != len(chunk):
                error = self._ctypes.get_last_error()
                raise NativeInputError(
                    "SendInput delivered %d of %d keyboard events (error %d); "
                    "input may be blocked by a higher-integrity window"
                    % (sent, len(chunk), error)
                )


def _windows_double_click_time() -> float:
    import ctypes

    milliseconds = ctypes.windll.user32.GetDoubleClickTime()  # type: ignore[attr-defined]
    return milliseconds / 1000.0 if milliseconds else DEFAULT_DOUBLE_CLICK_TIME


def _usable_keyboard_module(platform: str) -> Any:
    """The ``keyboard`` package when it can inject keys here, else None."""
    if platform.startswith("linux") or platform == "darwin":
        geteuid = getattr(os, "geteuid", None)
        # keyboard injects through /dev/uinput or a root event tap.
        if geteuid is None or geteuid() != 0:
            return None
    try:
        import keyboard
    except Exception:
        return None
    return keyboard


def _off_corner(
    point: Tuple[int, int], box: Optional[Tuple[int, int]]
) -> Tuple[int, int]:
    """Move an intermediate path point off a primary-screen fail-safe corner."""
    if box is None:
        return point
    width, height = box
    x, y = point
    if x in (0, width - 1) and y in (0, height - 1):
        return (1 if x == 0 else width - 2), y
    return point


def _ease(t: float) -> float:
    """Minimum-jerk profile: slow start, fast middle, slow landing."""
    return t * t * t * (10.0 + t * (-15.0 + 6.0 * t))


def _bezier(p0: float, p1: float, p2: float, p3: float, t: float) -> float:
    u = 1.0 - t
    return u * u * u * p0 + 3 * u * u * t * p1 + 3 * u * t * t * p2 + t * t * t * p3


class ElementInteractor:
    def __init__(
        self,
        *,
        backend: Any = None,
        platform: Optional[str] = None,
        unicode_sender: Any = None,
        clipboard: Any = None,
        keyboard: Any = _UNSET,
        double_click_time: Optional[float] = None,
        sleep: Optional[Callable[[float], None]] = None,
        rng: Optional[random.Random] = None,
    ) -> None:
        self._backend = backend
        self._platform = platform or sys.platform
        self._unicode_sender = unicode_sender
        self._clipboard = clipboard
        self._keyboard = keyboard
        self._double_click_time = double_click_time
        self._sleep = sleep or time.sleep
        self._rng = rng or random.Random()
        self._init_lock = threading.Lock()

    # -- lazy collaborators -------------------------------------------------

    def _gui(self) -> Any:
        if self._backend is None:
            self._backend = _screen.pyautogui()
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

    def position(self) -> Tuple[int, int]:
        point = self._gui().position()
        return int(point[0]), int(point[1])

    def move(self, x: float, y: float, *, human: bool = True) -> None:
        target = (int(round(x)), int(round(y)))
        with NATIVE_INPUT_LOCK:
            if human:
                self._human_move(target)
            self._land(target)

    def press(self, button: str = "left", *, clicks: int = 1) -> None:
        if button not in _BUTTONS:
            raise ValueError("button must be one of %s, got %r" % (_BUTTONS, button))
        if isinstance(clicks, bool) or not isinstance(clicks, int) or clicks < 1:
            raise ValueError("clicks must be a positive int, got %r" % (clicks,))
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
            raise ValueError("unsupported click_type %r" % (click_type,))
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
                    self._human_move(target)
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
        offset_x: Optional[float] = None,
        offset_y: Optional[float] = None,
    ) -> Tuple[int, int]:
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
    ) -> Optional[BaseException]:
        """Run every release even if one is interrupted; return the first error."""
        first: Optional[BaseException] = None
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
        release_all: Callable[[], Optional[BaseException]],
    ) -> None:
        """Run ``action`` then ``release_all`` no matter what; the action's error wins."""
        error: Optional[BaseException] = None
        try:
            action()
        except BaseException as exc:
            error = exc
        release_error = release_all()
        if error is not None:
            raise error
        if release_error is not None:
            raise release_error

    def _land(self, target: Tuple[int, int]) -> None:
        gui = self._gui()
        for _ in range(2):
            gui.moveTo(target[0], target[1], _pause=False)
            if self.position() == target:
                return
        raise NativeInputError(
            "cursor did not land on %r (at %r); the point may be off-screen"
            % (target, self.position())
        )

    def _move_duration(self, distance: float) -> float:
        base = 0.08 + 0.085 * math.log2(1.0 + distance / 12.0)
        return min(1.2, base * self._rng.uniform(0.85, 1.15))

    def _screen_box(self) -> Optional[Tuple[int, int]]:
        try:
            size = self._gui().size()
            return int(size[0]), int(size[1])
        except Exception:
            return None

    def _human_path(
        self, start: Tuple[int, int], end: Tuple[int, int], steps: int
    ) -> List[Tuple[int, int]]:
        sx, sy = start
        ex, ey = end
        dx, dy = ex - sx, ey - sy
        distance = math.hypot(dx, dy)
        nx, ny = -dy / distance, dx / distance
        side = self._rng.choice((-1.0, 1.0))
        bend = min(160.0, distance * self._rng.uniform(0.05, 0.18))
        bend2 = bend * self._rng.uniform(0.3, 1.0)
        if self._rng.random() < 0.2:
            bend2 = -bend2  # occasional S-curve
        c1 = (sx + dx * 0.3 + nx * bend * side, sy + dy * 0.3 + ny * bend * side)
        c2 = (sx + dx * 0.7 + nx * bend2 * side, sy + dy * 0.7 + ny * bend2 * side)

        box = self._screen_box()
        inside = box is not None and all(
            0 <= px < box[0] and 0 <= py < box[1] for px, py in (start, end)
        )
        points: List[Tuple[int, int]] = []
        for index in range(1, steps + 1):
            t = _ease(index / float(steps))
            px = int(round(_bezier(sx, c1[0], c2[0], ex, t)))
            py = int(round(_bezier(sy, c1[1], c2[1], ey, t)))
            if inside and box is not None:
                # Stay on the primary screen and off the pyautogui fail-safe corners.
                px = min(max(px, 1), box[0] - 2)
                py = min(max(py, 1), box[1] - 2)
            points.append(_off_corner((px, py), box))
        points[-1] = end
        return points

    def _human_move(self, target: Tuple[int, int]) -> None:
        gui = self._gui()
        start = self.position()
        distance = math.hypot(target[0] - start[0], target[1] - start[1])
        if distance < 2:
            return
        duration = self._move_duration(distance)
        steps = max(8, min(100, int(round(duration * 90))))
        step_sleep = duration / steps
        last = start
        for point in self._human_path(start, target, steps)[:-1]:
            if point != last:
                gui.moveTo(point[0], point[1], _pause=False)
                last = point
            self._sleep(step_sleep)

    def _linear_move(self, target: Tuple[int, int], *, steps: int, duration: float) -> None:
        gui = self._gui()
        sx, sy = self.position()
        box = self._screen_box()
        for index in range(1, steps):
            t = index / float(steps)
            point = _off_corner(
                (int(round(sx + (target[0] - sx) * t)), int(round(sy + (target[1] - sy) * t))),
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
            raise TypeError("text must be str, got %r" % (type(text).__name__,))
        if not text:
            return
        delay = 60.0 / characters_per_minute if human and characters_per_minute > 0 else 0.0
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
                raise NativeInputError("clipboard is unavailable: %s" % exc) from exc
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
            pressed: List[str] = []

            def press_all() -> None:
                for key in keys:
                    # Recorded first: a key interrupted mid-press is still released.
                    pressed.append(key)
                    gui.keyDown(key, _pause=False)

            self._guarded(
                press_all,
                lambda: self._run_releases(
                    [functools.partial(self._key_release, gui, key) for key in reversed(pressed)]
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
                "modifier key held by the user (%s); typing would trigger "
                "shortcuts, release it and retry" % ", ".join(held)
            )

    def _type_windows(self, text: str, delay: float, offset: int) -> None:
        sender = self._sender()
        if delay <= 0:
            self._check_modifiers(sender)
            sender.send(keyboard_events(text))
            return
        for stroke in _strokes(text):
            self._check_modifiers(sender)
            sender.send(stroke_events(stroke))
            self._pace(delay, offset)

    @staticmethod
    def _is_pyautogui_stroke(stroke: str) -> bool:
        return stroke in ("\n", "\t") or (stroke.isascii() and stroke.isprintable())

    def _type_posix(self, text: str, delay: float, offset: int) -> None:
        gui = self._gui()
        strokes = _strokes(text)
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
            while run_end < len(strokes) and not self._is_pyautogui_stroke(strokes[run_end]):
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
