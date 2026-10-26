"""The native Windows input backend (emunium._native.win32) without real input.

INPUT records and the key table are checked through the pure builders; the
backend runs against ``FakeUser32``. Real user32 is only called for read-only
APIs, and the injecting ones are shown to hit the conftest safety net.
"""

from __future__ import annotations

import ctypes
import os
import random
import subprocess
import sys
from collections.abc import Sequence
from typing import NamedTuple

import pytest
from conftest import REAL_INPUT_MESSAGE, REPO_ROOT, SafetyNet

from emunium import _native, _screen
from emunium._browser.context import DragInteractor
from emunium._element_interactor import (
    ElementInteractor,
    WindowsKeyboardSender,
    keyboard_events,
)
from emunium._native import (
    FAILSAFE_ENV,
    check_failsafe,
    failsafe_enabled,
    in_failsafe_corner,
    win32,
    x11,
)
from emunium._native.win32 import (
    INPUT,
    KEYBDINPUT,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    MOUSEEVENTF_HWHEEL,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MIDDLEDOWN,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    MOUSEEVENTF_WHEEL,
    MOUSEINPUT,
    Win32Backend,
    WindowInfo,
)
from emunium.errors import NativeInputError

UP = KEYEVENTF_KEYUP
EXT = KEYEVENTF_EXTENDEDKEY
U = KEYEVENTF_UNICODE

SCAN = win32.KEYEVENTF_SCANCODE
MEDIUM = 0x2000
HIGH = 0x3000

Record = tuple[str, int, int, int]


def describe(record: INPUT) -> Record:
    """("key", vk, scan, flags) or ("mouse", flags, mouseData, 0)."""
    if record.type == win32.INPUT_KEYBOARD:
        return ("key", record.ki.wVk, record.ki.wScan, record.ki.dwFlags)
    return ("mouse", record.mi.dwFlags, record.mi.mouseData, 0)


class FakeUser32:
    """Stands in for ``win32.User32``; records every call."""

    def __init__(
        self,
        cursor: tuple[int, int] = (500, 400),
        screen: tuple[int, int] = (1920, 1080),
        virtual: tuple[int, int, int, int] = (-1280, 0, 3200, 1080),
    ) -> None:
        self.cursor = cursor
        self.screen = screen
        self.virtual = virtual
        self.batches: list[list[Record]] = []
        self.moves: list[tuple[int, int]] = []
        self.mapped: list[tuple[int, int]] = []
        self.deliver: int | None = None
        self.move_ok = True
        self.cursor_ok = True
        self.own: int | None = MEDIUM
        self.foreground: int | None = MEDIUM
        self.integrity_queries: list[str] = []
        # VkKeyScanW results of the active layout (US-like); others: -1.
        self.layout: dict[str, int] = {"-": 0x00BD, "/": 0x00BF, "?": 0x01BF}
        self.scanned: list[str] = []

    def cursor_pos(self) -> tuple[int, int]:
        if not self.cursor_ok:
            raise NativeInputError("GetCursorPos failed (error 5)")
        return self.cursor

    def vk_key_scan(self, character: str) -> int:
        self.scanned.append(character)
        return self.layout.get(character, -1)

    def own_integrity(self) -> int | None:
        self.integrity_queries.append("own")
        return self.own

    def foreground_integrity(self) -> int | None:
        self.integrity_queries.append("foreground")
        return self.foreground

    def set_cursor_pos(self, x: int, y: int) -> bool:
        self.moves.append((x, y))
        if self.move_ok:
            self.cursor = (x, y)
        return self.move_ok

    def system_metric(self, index: int) -> int:
        metrics = {
            win32.SM_CXSCREEN: self.screen[0],
            win32.SM_CYSCREEN: self.screen[1],
            win32.SM_XVIRTUALSCREEN: self.virtual[0],
            win32.SM_YVIRTUALSCREEN: self.virtual[1],
            win32.SM_CXVIRTUALSCREEN: self.virtual[2],
            win32.SM_CYVIRTUALSCREEN: self.virtual[3],
        }
        return metrics[index]

    def map_virtual_key(self, code: int, map_type: int) -> int:
        self.mapped.append((code, map_type))
        return code + 0x100  # distinguishable from the virtual key

    def send_input(self, records: Sequence[INPUT]) -> int:
        self.batches.append([describe(record) for record in records])
        return len(records) if self.deliver is None else self.deliver

    def last_error(self) -> int:
        return 5


@pytest.fixture(autouse=True)
def failsafe_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FAILSAFE_ENV, raising=False)


def make(cursor: tuple[int, int] = (500, 400)) -> tuple[Win32Backend, FakeUser32]:
    user32 = FakeUser32(cursor=cursor)
    return Win32Backend(user32=user32), user32


# -- records and layout ----------------------------------------------------------


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="x64 layout")
def test_struct_sizes_x64() -> None:
    assert ctypes.sizeof(INPUT) == 40
    assert ctypes.sizeof(KEYBDINPUT) == 24
    assert ctypes.sizeof(MOUSEINPUT) == 32


def test_keyboard_input_record() -> None:
    record = win32.keyboard_input((0x25, 0x4B, EXT | UP))
    assert describe(record) == ("key", 0x25, 0x4B, EXT | UP)
    assert record.ki.time == 0 and record.ki.dwExtraInfo == 0


def test_mouse_input_negative_wheel_is_twos_complement() -> None:
    assert describe(win32.mouse_input(MOUSEEVENTF_WHEEL, -120)) == (
        "mouse",
        MOUSEEVENTF_WHEEL,
        0xFFFFFF88,
        0,
    )


def test_wheel_inputs_one_record_per_notch() -> None:
    assert [describe(r) for r in win32.wheel_inputs(3)] == [
        ("mouse", MOUSEEVENTF_WHEEL, 120, 0)
    ] * 3
    assert [describe(r) for r in win32.wheel_inputs(-2, horizontal=True)] == [
        ("mouse", MOUSEEVENTF_HWHEEL, 0xFFFFFF88, 0)
    ] * 2
    assert win32.wheel_inputs(0) == []
    with pytest.raises(TypeError):
        win32.wheel_inputs(True)


def test_input_array_copies_records() -> None:
    records = [win32.keyboard_input((0x41, 0x1E, 0)), win32.mouse_input(0x2)]
    array = win32.input_array(records)
    assert len(array) == 2
    assert [describe(array[i]) for i in range(2)] == [describe(r) for r in records]


@pytest.mark.parametrize(
    ("button", "down", "up"),
    [
        ("left", MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        ("right", MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        ("middle", MOUSEEVENTF_MIDDLEDOWN, win32.MOUSEEVENTF_MIDDLEUP),
    ],
)
def test_button_flags(button: str, down: int, up: int) -> None:
    assert win32.button_flags(button, up=False) == down
    assert win32.button_flags(button, up=True) == up


def test_unknown_button_rejected() -> None:
    with pytest.raises(ValueError, match="button must be one of"):
        win32.button_flags("back", up=False)


# -- key table -------------------------------------------------------------------

KEY_TABLE = {
    "a": 0x41,
    "A": 0x41,
    "z": 0x5A,
    "0": 0x30,
    "9": 0x39,
    "f1": 0x70,
    "F12": 0x7B,
    "f13": 0x7C,
    "f24": 0x87,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "insert": 0x2D,
    "delete": 0x2E,
    "enter": 0x0D,
    "return": 0x0D,
    "\n": 0x0D,
    "tab": 0x09,
    "\t": 0x09,
    "space": 0x20,
    " ": 0x20,
    "backspace": 0x08,
    "escape": 0x1B,
    "esc": 0x1B,
    "shift": 0x10,
    "shiftleft": 0xA0,
    "shiftright": 0xA1,
    "ctrl": 0x11,
    "ctrlleft": 0xA2,
    "ctrlright": 0xA3,
    "alt": 0x12,
    "altleft": 0xA4,
    "altright": 0xA5,
    "win": 0x5B,
    "winleft": 0x5B,
    "winright": 0x5C,
    "lwin": 0x5B,
    "rwin": 0x5C,
    "command": 0x5B,
    "Ctrl": 0x11,
    "capslock": 0x14,
    "numlock": 0x90,
    "scrolllock": 0x91,
    "printscreen": 0x2C,
    "prtsc": 0x2C,
    "pause": 0x13,
    "apps": 0x5D,
    "menu": 0x5D,
    "num0": 0x60,
    "num5": 0x65,
    "num9": 0x69,
    "add": 0x6B,
    "subtract": 0x6D,
    "multiply": 0x6A,
    "divide": 0x6F,
    "decimal": 0x6E,
    "volumeup": 0xAF,
    "volumedown": 0xAE,
    "volumemute": 0xAD,
    "playpause": 0xB3,
    "nexttrack": 0xB0,
    "prevtrack": 0xB1,
}
EXTENDED = {
    "divide",
    "numlock",
    "printscreen",
    "prtsc",
    "apps",
    "menu",
    "left",
    "up",
    "right",
    "down",
    "home",
    "end",
    "pageup",
    "pagedown",
    "insert",
    "delete",
    "ctrlright",
    "altright",
    "win",
    "winleft",
    "winright",
    "lwin",
    "rwin",
    "command",
}


@pytest.mark.parametrize(("key", "code"), sorted(KEY_TABLE.items()))
def test_key_table(key: str, code: int) -> None:
    assert win32.virtual_key(key) == code
    extended = EXT if key in EXTENDED else 0
    scans = {code: 0x5A}
    assert win32.key_events(key, up=False, scan_codes=scans) == [(code, 0x5A, extended)]
    assert win32.key_events(key, up=True, scan_codes=scans) == [
        (code, 0x5A, extended | UP)
    ]


def test_untabled_character_is_sent_as_unicode() -> None:
    assert win32.virtual_key("é") is None
    assert win32.key_events("é", up=False, scan_codes={}) == [(0, 0xE9, U)]
    assert win32.key_events("\U0001f600", up=True, scan_codes={}) == [
        (0, 0xD83D, U | UP),
        (0, 0xDE00, U | UP),
    ]


@pytest.mark.parametrize("key", ["bogus", "f25", "num10", ""])
def test_unknown_key_name_rejected(key: str) -> None:
    with pytest.raises(ValueError, match="key"):
        win32.virtual_key(key)


def test_numpad_enter_is_an_extended_scan_code() -> None:
    assert win32.virtual_key("numpadenter") is None
    assert win32.key_events("NumpadEnter", up=False, scan_codes={}) == [
        (0, 0x1C, SCAN | EXT)
    ]
    assert win32.key_events("numpadenter", up=True, scan_codes={}) == [
        (0, 0x1C, SCAN | EXT | UP)
    ]


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (-1, None),
        (0xFFFF, None),
        (0x00BD, (0xBD, 0)),
        (0x01BF, (0xBF, 1)),
        (0x0632, (0x32, 6)),
    ],
)
def test_layout_key_decodes_vk_key_scan(
    result: int, expected: tuple[int, int] | None
) -> None:
    assert win32.layout_key(result) == expected


def test_layout_key_events_use_the_vk_and_ignore_shift() -> None:
    scans = {0xBF: 0x35}
    assert win32.key_events("?", up=False, scan_codes=scans, layout=(0xBF, 1)) == [
        (0xBF, 0x35, 0)
    ]
    # Letters and digits keep their fixed codes whatever the layout says.
    assert win32.key_events("q", up=False, scan_codes={}, layout=(0x41, 0)) == [
        (0x51, 0, 0)
    ]


def test_press_events_honour_the_shift_state() -> None:
    scans = {0xBF: 0x35, win32.VK_SHIFT: 0x2A}
    assert win32.press_events("/", scan_codes=scans, layout=(0xBF, 0)) == [
        (0xBF, 0x35, 0),
        (0xBF, 0x35, UP),
    ]
    assert win32.press_events("?", scan_codes=scans, layout=(0xBF, 1)) == [
        (win32.VK_SHIFT, 0x2A, 0),
        (0xBF, 0x35, 0),
        (0xBF, 0x35, UP),
        (win32.VK_SHIFT, 0x2A, UP),
    ]
    # Ctrl/Alt (AltGr) characters and unmapped ones arrive as Unicode.
    assert win32.press_events("@", scan_codes=scans, layout=(0x32, 6)) == [
        (0, ord("@"), U),
        (0, ord("@"), U | UP),
    ]
    assert win32.press_events("€", scan_codes=scans) == [
        (0, 0x20AC, U),
        (0, 0x20AC, U | UP),
    ]


# -- fail-safe -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "enabled"),
    [(None, True), ("1", True), ("0", False), ("false", False), (" OFF ", False)],
)
def test_failsafe_env(value: str | None, enabled: bool) -> None:
    environ = {} if value is None else {FAILSAFE_ENV: value}
    assert failsafe_enabled(environ) is enabled


@pytest.mark.parametrize(
    ("point", "corner"),
    [
        ((0, 0), True),
        ((1919, 0), True),
        ((0, 1079), True),
        ((1919, 1079), True),
        ((1, 0), False),
        ((960, 540), False),
        ((-5, 0), False),
    ],
)
def test_failsafe_corners(point: tuple[int, int], corner: bool) -> None:
    assert in_failsafe_corner(point, (1920, 1080)) is corner


def test_check_failsafe_raises_in_corner() -> None:
    with pytest.raises(NativeInputError, match=FAILSAFE_ENV):
        check_failsafe(lambda: (0, 0), lambda: (1920, 1080), {})
    check_failsafe(lambda: (0, 0), lambda: (1920, 1080), {FAILSAFE_ENV: "0"})


@pytest.mark.parametrize(
    "action",
    [
        lambda b: b.moveTo(10, 10),
        lambda b: b.mouseDown(),
        lambda b: b.mouseUp(),
        lambda b: b.scroll(1),
        lambda b: b.hscroll(1),
        lambda b: b.keyDown("a"),
        lambda b: b.keyUp("a"),
        lambda b: b.press("enter"),
        lambda b: b.write("x"),
    ],
)
def test_every_action_checks_failsafe(action, monkeypatch: pytest.MonkeyPatch) -> None:
    backend, user32 = make(cursor=(1919, 1079))
    with pytest.raises(NativeInputError, match="fail-safe"):
        action(backend)
    assert user32.batches == [] and user32.moves == []
    monkeypatch.setenv(FAILSAFE_ENV, "0")
    action(backend)
    assert user32.batches or user32.moves


def test_raw_releases_skip_failsafe() -> None:
    backend, user32 = make(cursor=(0, 0))
    layer = backend.platformModule
    assert layer._position() == (0, 0)
    layer._mouseUp(0, 0, "right")
    layer._keyUp("ctrl")
    assert user32.batches == [
        [("mouse", MOUSEEVENTF_RIGHTUP, 0, 0)],
        [("key", 0x11, 0x111, UP)],
    ]
    assert user32.moves == []


def test_raw_key_down_skips_failsafe_and_preflight() -> None:
    # Escape that cancels an aborted drag: cursor in a corner, elevated window
    # in front. Neither may stop the send; the cleanup goes on to the release.
    backend, user32 = make(cursor=(0, 0))
    user32.foreground = HIGH
    backend.platformModule._keyDown("escape")
    assert user32.batches == [[("key", 0x1B, 0x11B, 0)]]
    assert user32.integrity_queries == [] and user32.moves == []


def test_raw_key_down_still_reports_a_short_send() -> None:
    backend, user32 = make(cursor=(0, 0))
    user32.deliver = 0
    with pytest.raises(NativeInputError, match="delivered 0 of 1 keyboard"):
        backend.platformModule._keyDown("escape")


def test_raw_key_down_rejects_unknown_keys_before_sending() -> None:
    backend, user32 = make(cursor=(0, 0))
    with pytest.raises(ValueError, match="unknown key name"):
        backend.platformModule._keyDown("bogus")
    assert user32.batches == []


def test_drag_escape_is_sent_in_a_failsafe_corner() -> None:
    backend, user32 = make(cursor=(1919, 1079))
    user32.foreground = HIGH
    DragInteractor(backend=backend, platform="win32").press_escape()
    assert user32.batches == [
        [("key", 0x1B, 0x11B, 0)],
        [("key", 0x1B, 0x11B, UP)],
    ]


# -- backend against FakeUser32 ----------------------------------------------------


def test_position_size_and_virtual_screen() -> None:
    backend, _ = make(cursor=(-300, 25))
    assert backend.position() == (-300, 25)
    assert backend.position().x == -300
    assert backend.size() == (1920, 1080) and backend.size().width == 1920
    assert backend.virtual_screen() == (-1280, 0, 3200, 1080)


def test_move_to_allows_negative_coordinates_and_rounds() -> None:
    backend, user32 = make()
    backend.moveTo(-1500, 20, _pause=False)
    backend.moveTo(10.6, 20.4)
    assert user32.moves == [(-1500, 20), (11, 20)]
    assert user32.batches == []


def test_move_to_failure_raises() -> None:
    backend, user32 = make()
    user32.move_ok = False
    with pytest.raises(
        NativeInputError, match=r"SetCursorPos\(5, 6\) failed \(error 5\)"
    ):
        backend.moveTo(5, 6)


def test_buttons_press_at_current_position() -> None:
    backend, user32 = make()
    backend.mouseDown(button="right", _pause=False)
    backend.mouseUp(button="right", _pause=False)
    assert user32.moves == []
    assert user32.batches == [
        [("mouse", MOUSEEVENTF_RIGHTDOWN, 0, 0)],
        [("mouse", MOUSEEVENTF_RIGHTUP, 0, 0)],
    ]


def test_buttons_move_first_when_given_a_point() -> None:
    backend, user32 = make(cursor=(500, 400))
    backend.mouseDown(100, 200)
    backend.mouseUp(y=300)
    assert user32.moves == [(100, 200), (100, 300)]
    assert [batch[0][1] for batch in user32.batches] == [
        MOUSEEVENTF_LEFTDOWN,
        MOUSEEVENTF_LEFTUP,
    ]


def test_bad_button_sends_nothing() -> None:
    backend, user32 = make()
    with pytest.raises(ValueError):
        backend.mouseDown(10, 10, button="back")
    assert user32.batches == [] and user32.moves == []


def test_scroll_sends_wheel_notches() -> None:
    backend, user32 = make()
    backend.scroll(2)
    backend.scroll(-1)
    backend.hscroll(1)
    backend.scroll(0)
    assert user32.batches == [
        [("mouse", MOUSEEVENTF_WHEEL, 120, 0)] * 2,
        [("mouse", MOUSEEVENTF_WHEEL, 0xFFFFFF88, 0)],
        [("mouse", MOUSEEVENTF_HWHEEL, 120, 0)],
    ]


def test_key_down_up_with_scan_codes_and_extended_flag() -> None:
    backend, user32 = make()
    backend.keyDown("ctrl")
    backend.keyDown("left")
    backend.keyUp("left")
    backend.keyUp("CTRL")
    assert user32.batches == [
        [("key", 0x11, 0x111, 0)],
        [("key", 0x25, 0x125, EXT)],
        [("key", 0x25, 0x125, EXT | UP)],
        [("key", 0x11, 0x111, UP)],
    ]
    # MapVirtualKeyW(MAPVK_VK_TO_VSC) once per key, then cached.
    assert user32.mapped == [(0x11, win32.MAPVK_VK_TO_VSC), (0x25, 0)]


def test_unknown_key_sends_nothing() -> None:
    backend, user32 = make()
    with pytest.raises(ValueError, match="unknown key name"):
        backend.keyDown("bogus")
    assert user32.batches == []


def test_press_sends_down_up_per_key() -> None:
    backend, user32 = make()
    backend.press("enter")
    backend.press(["shift", "tab"], presses=2)
    backend.press("a", presses=0)
    shift_tab = [
        ("key", 0x10, 0x110, 0),
        ("key", 0x10, 0x110, UP),
        ("key", 0x09, 0x109, 0),
        ("key", 0x09, 0x109, UP),
    ]
    assert user32.batches == [
        [("key", 0x0D, 0x10D, 0), ("key", 0x0D, 0x10D, UP)],
        shift_tab,
        shift_tab,
    ]
    with pytest.raises(ValueError):
        backend.press("a", presses=-1)


def test_write_is_unicode_and_layout_independent() -> None:
    backend, user32 = make()
    backend.write("hé\r\n")
    assert user32.batches == [
        [
            ("key", 0, ord("h"), U),
            ("key", 0, ord("h"), U | UP),
            ("key", 0, 0xE9, U),
            ("key", 0, 0xE9, U | UP),
            ("key", 0x0D, 0, 0),
            ("key", 0x0D, 0, UP),
        ]
    ]
    assert user32.mapped == []


def test_write_chunks_without_splitting_surrogates() -> None:
    backend, user32 = make()
    backend.write("x" * 31 + "\U0001f600")
    assert [len(batch) for batch in user32.batches] == [62, 4]


def test_partial_send_input_raises() -> None:
    backend, user32 = make()
    user32.deliver = 0
    with pytest.raises(NativeInputError, match="SendInput delivered 0 of 1 mouse"):
        backend.mouseDown()


# -- layout characters, extra keys ---------------------------------------------------


def test_key_down_maps_printable_characters_through_the_layout() -> None:
    backend, user32 = make()
    backend.keyDown("-")
    backend.keyDown("?")  # hotkey semantics: the VK only, Shift is not added
    backend.keyUp("?")
    backend.keyDown("€")  # no key on this layout: Unicode
    backend.keyDown("a")  # letters keep their fixed code, no layout lookup
    assert user32.batches == [
        [("key", 0xBD, 0x1BD, 0)],
        [("key", 0xBF, 0x1BF, 0)],
        [("key", 0xBF, 0x1BF, UP)],
        [("key", 0, 0x20AC, U)],
        [("key", 0x41, 0x141, 0)],
    ]
    assert user32.scanned == ["-", "?", "?", "€"]


def test_press_wraps_shifted_characters_in_shift() -> None:
    backend, user32 = make()
    backend.press("?")
    backend.press("/")
    assert user32.batches == [
        [
            ("key", 0x10, 0x110, 0),
            ("key", 0xBF, 0x1BF, 0),
            ("key", 0xBF, 0x1BF, UP),
            ("key", 0x10, 0x110, UP),
        ],
        [("key", 0xBF, 0x1BF, 0), ("key", 0xBF, 0x1BF, UP)],
    ]


def test_hotkey_with_a_punctuation_key() -> None:
    backend, user32 = make()
    interactor = ElementInteractor(
        backend=backend, platform="win32", sleep=lambda _seconds: None
    )
    interactor.hotkey("ctrl", "-")
    assert user32.batches == [
        [("key", 0x11, 0x111, 0)],
        [("key", 0xBD, 0x1BD, 0)],
        [("key", 0xBD, 0x1BD, UP)],
        [("key", 0x11, 0x111, UP)],
    ]


def test_extra_keys_reach_send_input() -> None:
    backend, user32 = make()
    backend.press(["numpadenter", "divide", "volumeup", "num7"])
    assert user32.batches == [
        [
            ("key", 0, 0x1C, SCAN | EXT),
            ("key", 0, 0x1C, SCAN | EXT | UP),
            ("key", 0x6F, 0x16F, EXT),
            ("key", 0x6F, 0x16F, EXT | UP),
            ("key", 0xAF, 0x1AF, 0),
            ("key", 0xAF, 0x1AF, UP),
            ("key", 0x67, 0x167, 0),
            ("key", 0x67, 0x167, UP),
        ]
    ]


# -- UIPI preflight ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("own", "foreground", "blocked"),
    [
        (MEDIUM, HIGH, True),
        (MEDIUM, MEDIUM, False),
        (HIGH, MEDIUM, False),
        (None, HIGH, False),
        (MEDIUM, None, False),
    ],
)
def test_uipi_blocked(own: int | None, foreground: int | None, blocked: bool) -> None:
    assert win32.uipi_blocked(own, foreground) is blocked


@pytest.mark.parametrize(
    "action",
    [
        lambda b: b.mouseDown(),
        lambda b: b.mouseUp(),
        lambda b: b.scroll(1),
        lambda b: b.hscroll(-1),
        lambda b: b.keyDown("a"),
        lambda b: b.keyUp("a"),
        lambda b: b.press("enter"),
        lambda b: b.write("x"),
    ],
)
def test_elevated_foreground_blocks_input(action) -> None:
    backend, user32 = make()
    user32.foreground = HIGH
    with pytest.raises(NativeInputError) as raised:
        action(backend)
    assert str(raised.value) == win32.UIPI_MESSAGE
    assert user32.batches == []


def test_preflight_lets_moves_and_releases_through() -> None:
    backend, user32 = make()
    user32.foreground = HIGH
    backend.moveTo(10, 10)
    backend.platformModule._mouseUp(0, 0, "left")
    backend.platformModule._keyUp("shift")
    assert user32.moves == [(10, 10)]
    assert user32.batches == [
        [("mouse", MOUSEEVENTF_LEFTUP, 0, 0)],
        [("key", 0x10, 0x110, UP)],
    ]


@pytest.mark.parametrize(("own", "foreground"), [(None, HIGH), (MEDIUM, None)])
def test_failed_integrity_query_does_not_block(
    own: int | None, foreground: int | None
) -> None:
    backend, user32 = make()
    user32.own, user32.foreground = own, foreground
    backend.mouseDown()
    assert len(user32.batches) == 1


def test_own_integrity_is_cached_once_known() -> None:
    backend, user32 = make()
    user32.own = None
    backend.keyDown("a")  # unknown: not cached, asked again next time
    user32.own = MEDIUM
    backend.keyDown("a")
    backend.keyDown("a")
    backend.write("x" * 40)  # one preflight for every chunk
    assert user32.integrity_queries == [
        "foreground",
        "own",
        "foreground",
        "own",
        "foreground",
        "foreground",
    ]
    assert [len(batch) for batch in user32.batches] == [1, 1, 1, 64, 16]


class RawFake:
    """A raw user32-like object (attributes named like the exports)."""

    def __init__(self, deliver: int | None = None) -> None:
        self.calls: list[tuple[int, int]] = []
        self.errors: list[int] = []
        self.deliver = deliver

    def SendInput(self, count: int, array: ctypes.Array[INPUT], size: int) -> int:
        self.calls.append((count, size))
        if sys.platform == "win32":
            self.errors.append(ctypes.get_last_error())
        return count if self.deliver is None else self.deliver

    def GetAsyncKeyState(self, vk: int) -> int:
        return -32768 if vk == 0x12 else 0


def test_keyboard_sender_uses_the_single_send_path() -> None:
    raw = RawFake(deliver=1)
    sender = WindowsKeyboardSender(user32=raw)
    assert sender.held_modifiers() == ["alt"]
    with pytest.raises(
        NativeInputError,
        match="SendInput delivered 1 of 2 keyboard events .* or the secure desktop",
    ):
        sender.send(keyboard_events("a"))
    assert raw.calls == [(2, ctypes.sizeof(INPUT))]


@pytest.mark.skipif(sys.platform != "win32", reason="ctypes last error is Windows")
def test_last_error_cleared_before_send_input() -> None:
    assert sys.platform == "win32"  # narrows ctypes for the type checker
    raw = RawFake()
    ctypes.set_last_error(1234)
    win32.RawUser32(raw).send_input([win32.keyboard_input((0x41, 0, 0))])
    assert raw.errors == [0]


def test_release_never_reads_the_cursor() -> None:
    backend, user32 = make()

    def lose_cursor(_seconds: float) -> None:
        user32.cursor_ok = False  # GetCursorPos starts failing mid-click

    interactor = ElementInteractor(backend=backend, platform="win32", sleep=lose_cursor)
    interactor.press("right")
    assert user32.batches == [
        [("mouse", MOUSEEVENTF_RIGHTDOWN, 0, 0)],
        [("mouse", MOUSEEVENTF_RIGHTUP, 0, 0)],
    ]


def test_interactor_drives_the_backend() -> None:
    backend, user32 = make(cursor=(500, 400))
    interactor = ElementInteractor(
        backend=backend,
        platform="win32",
        sleep=lambda _seconds: None,
        rng=random.Random(3),
        double_click_time=0.5,
    )
    interactor.click(120, 80, human=False)
    interactor.hotkey("ctrl", "v")
    assert user32.moves == [(120, 80)]
    assert user32.batches == [
        [("mouse", MOUSEEVENTF_LEFTDOWN, 0, 0)],
        [("mouse", MOUSEEVENTF_LEFTUP, 0, 0)],
        [("key", 0x11, 0x111, 0)],
        [("key", 0x56, 0x156, 0)],
        [("key", 0x56, 0x156, UP)],
        [("key", 0x11, 0x111, UP)],
    ]


def test_interactor_releases_in_failsafe_corner() -> None:
    backend, user32 = make(cursor=(0, 0))
    interactor = ElementInteractor(
        backend=backend, platform="win32", sleep=lambda _seconds: None
    )
    with pytest.raises(NativeInputError, match="fail-safe"):
        interactor.press("left")
    with pytest.raises(NativeInputError, match="fail-safe"):
        interactor.hotkey("shift", "a")
    # Nothing went down, but every release was still attempted.
    assert user32.batches == [
        [("mouse", MOUSEEVENTF_LEFTUP, 0, 0)],
        [("key", 0x10, 0x110, UP)],
    ]


# -- selection -------------------------------------------------------------------


def test_native_backend_none_off_windows_without_a_display() -> None:
    # An explicit empty environment: the runner's DISPLAY would pick X11.
    assert _native.native_backend("linux", {}) is None
    assert _native.native_backend("darwin", {}) is None


def test_native_backend_selects_x11_when_display_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[str] = []

    class StubX11Backend:
        def __init__(self) -> None:
            built.append("x11")

    # No libX11 or X server needed: only the selection is under test.
    monkeypatch.setattr(x11, "X11Backend", StubX11Backend)
    assert isinstance(
        _native.native_backend("linux", {"DISPLAY": ":0"}), StubX11Backend
    )
    assert built == ["x11"]


@pytest.mark.skipif(sys.platform != "win32", reason="user32 is Windows-only")
def test_native_backend_on_windows() -> None:
    assert isinstance(_native.native_backend("win32"), Win32Backend)


def test_input_backend_chosen_once_after_dpi(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    backend, _ = make()

    def native(platform: str | None = None) -> Win32Backend:
        order.append(f"native:{platform}")
        return backend

    monkeypatch.setattr(_screen, "_BACKEND", None)
    monkeypatch.setattr(_screen, "_platform", lambda: "win32")
    monkeypatch.setattr(_screen, "ensure_dpi_awareness", lambda: order.append("dpi"))
    monkeypatch.setattr(_native, "native_backend", native)
    assert _screen.input_backend() is backend
    assert _screen.input_backend() is backend
    assert order == ["dpi", "native:win32"]


def test_input_backend_falls_back_to_pyautogui(monkeypatch: pytest.MonkeyPatch) -> None:
    gui = object()
    monkeypatch.setattr(_screen, "_BACKEND", None)
    monkeypatch.setattr(_screen, "_PYAUTOGUI", gui)
    monkeypatch.setattr(_screen, "_platform", lambda: "darwin")
    monkeypatch.setattr(_screen, "ensure_dpi_awareness", lambda: None)
    assert _screen.input_backend() is gui


def test_interactor_uses_input_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    backend, _ = make()
    monkeypatch.setattr(_screen, "_BACKEND", backend)
    assert ElementInteractor()._gui() is backend


@pytest.mark.skipif(sys.platform != "win32", reason="pyautogui-free path is Windows")
def test_windows_input_path_never_imports_pyautogui() -> None:
    # Builds the backend only; nothing is sent (no safety net in a subprocess).
    code = (
        "import sys, emunium._element_interactor as ei; "
        "print('pyautogui' in sys.modules); "
        "import emunium; from emunium import _screen; "
        "gui = ei.ElementInteractor()._gui(); "
        "print(type(gui).__name__, 'pyautogui' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["False", "Win32Backend", "False"]


# -- real user32 (read-only, or trapped) -----------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="user32 is Windows-only")
def test_real_user32_read_only_calls() -> None:
    user32 = win32.User32()
    x, y = user32.cursor_pos()
    assert isinstance(x, int) and isinstance(y, int)
    width = user32.system_metric(win32.SM_CXSCREEN)
    assert width > 0 and user32.system_metric(win32.SM_CYSCREEN) > 0
    assert user32.system_metric(win32.SM_CXVIRTUALSCREEN) >= width
    assert 0 < user32.map_virtual_key(0x41, win32.MAPVK_VK_TO_VSC) < 0x100
    assert isinstance(user32.vk_key_scan("-"), int)
    assert user32.vk_key_scan("\U0001f600") == -1
    assert isinstance(user32.key_held(0x10), bool)


@pytest.mark.skipif(sys.platform != "win32", reason="tokens are Windows-only")
def test_real_integrity_levels() -> None:
    user32 = win32.User32()
    own = user32.own_integrity()
    # Untrusted 0x0 ... protected process 0x5000; medium is 0x2000.
    assert own is not None and 0 <= own <= 0x5000
    foreground = user32.foreground_integrity()
    assert foreground is None or 0 <= foreground <= 0x5000


@pytest.mark.skipif(sys.platform != "win32", reason="user32 is Windows-only")
def test_real_user32_injection_hits_safety_net(
    input_safety_net: SafetyNet | None,
) -> None:
    assert input_safety_net is not None, "safety net must be armed in unit tests"
    assert sys.platform == "win32"  # narrows ctypes for the type checker
    user32 = win32.User32()
    for name in ("SendInput", "SetCursorPos"):
        assert input_safety_net.is_trap(user32._library[name]), name
    ctypes.set_last_error(1234)
    with pytest.raises(RuntimeError, match=REAL_INPUT_MESSAGE):
        user32.send_input([win32.keyboard_input((0x41, 0, 0))])
    assert ctypes.get_last_error() == 0  # cleared right before SendInput
    with pytest.raises(RuntimeError, match=REAL_INPUT_MESSAGE):
        user32.set_cursor_pos(10, 10)


# -- top-level windows and the foreground --------------------------------------------

CHROME = win32.CHROME_WINDOW_CLASS
OUR_THREAD = 7


class FakeWindow(NamedTuple):
    pid: int
    thread_id: int
    rect: tuple[int, int, int, int] | None = (0, 0, 800, 600)
    class_name: str = CHROME
    visible: bool = True
    ex_style: int = 0
    iconic: bool = False


class FakeWindows:
    """Stands in for ``win32.Win32Windows``; records every call by name."""

    def __init__(
        self,
        windows: dict[int, FakeWindow] | None = None,
        processes: list[tuple[int, int]] | None = None,
        foreground: int = 0,
    ) -> None:
        self.windows = windows or {}
        # EnumWindows order; defaults to the window table's order.
        self.order: list[int] | None = None
        self.processes = processes or []
        self.foreground = foreground
        self.calls: list[tuple[object, ...]] = []
        self.attached: set[int] = set()
        self.attach_ok = True
        # Foreground lock: SetForegroundWindow only works while attached.
        self.lock = True
        self.fail_on: str | None = None

    def _call(self, *call: object) -> None:
        self.calls.append(call)
        if call[0] == self.fail_on:
            raise OSError(f"{call[0]} failed")

    def enum_windows(self) -> list[int]:
        return list(self.windows) if self.order is None else list(self.order)

    def window_thread_process_id(self, hwnd: int) -> tuple[int, int]:
        window = self.windows.get(hwnd)
        return (0, 0) if window is None else (window.thread_id, window.pid)

    def is_window_visible(self, hwnd: int) -> bool:
        return self.windows[hwnd].visible

    def class_name(self, hwnd: int) -> str:
        return self.windows[hwnd].class_name

    def window_rect(self, hwnd: int) -> tuple[int, int, int, int] | None:
        return self.windows[hwnd].rect

    def is_iconic(self, hwnd: int) -> bool:
        return self.windows[hwnd].iconic

    def ex_style(self, hwnd: int) -> int:
        return self.windows[hwnd].ex_style

    def process_parents(self) -> list[tuple[int, int]]:
        return list(self.processes)

    def foreground_window(self) -> int:
        return self.foreground

    def current_thread_id(self) -> int:
        return OUR_THREAD

    def attach_thread_input(self, attach: int, attach_to: int, on: bool) -> bool:
        self._call("attach", attach, attach_to, on)
        if not self.attach_ok:
            return False
        if on:
            self.attached.add(attach_to)
        else:
            self.attached.discard(attach_to)
        return True

    def show_window(self, hwnd: int, command: int) -> bool:
        self._call("show", hwnd, command)
        self.windows[hwnd] = self.windows[hwnd]._replace(iconic=False)
        return True

    def set_foreground_window(self, hwnd: int) -> bool:
        self._call("set_foreground", hwnd)
        owner = self.windows[self.foreground].thread_id if self.foreground else 0
        if self.lock and owner not in self.attached:
            return False
        self.foreground = hwnd
        return True

    def bring_window_to_top(self, hwnd: int) -> bool:
        self._call("bring_to_top", hwnd)
        return True

    def set_focus(self, hwnd: int) -> int:
        self._call("set_focus", hwnd)
        return 0


def info(
    hwnd: int, rect: tuple[int, int, int, int], *, minimized: bool = False
) -> WindowInfo:
    return WindowInfo(hwnd, 10, 11, CHROME, *rect, minimized)


def test_top_level_windows_keeps_visible_browser_windows_in_z_order() -> None:
    api = FakeWindows(
        {
            0x10: FakeWindow(pid=10, thread_id=11, rect=(-8, -8, 1928, 1048)),
            0x20: FakeWindow(pid=99, thread_id=91),  # another process
            0x30: FakeWindow(pid=10, thread_id=11, visible=False),
            0x40: FakeWindow(pid=10, thread_id=11, rect=(5, 5, 5, 300)),  # zero width
            0x50: FakeWindow(pid=12, thread_id=13, ex_style=win32.WS_EX_TOOLWINDOW),
            0x60: FakeWindow(pid=12, thread_id=13, class_name="Chrome_WidgetWin_0"),
            0x70: FakeWindow(pid=12, thread_id=13, rect=None),  # vanished
            0x80: FakeWindow(
                pid=12, thread_id=13, rect=(-32000, -32000, -31840, -31972), iconic=True
            ),
            0x90: FakeWindow(pid=12, thread_id=14, rect=(100, 50, 900, 650)),
        }
    )
    found = win32.top_level_windows({10, 12}, api)
    assert [window.hwnd for window in found] == [0x10, 0x80, 0x90]
    assert found[0] == WindowInfo(0x10, 10, 11, CHROME, -8, -8, 1928, 1048, False)
    assert (found[0].width, found[0].height) == (1936, 1056)
    assert found[1].minimized and not found[2].minimized
    assert win32.top_level_windows(set(), api) == []


def test_top_level_windows_skips_a_window_gone_mid_enumeration() -> None:
    api = FakeWindows({0x10: FakeWindow(pid=10, thread_id=11)})
    api.order = [0x99, 0x10]  # 0x99 closed before it was read
    assert [window.hwnd for window in win32.top_level_windows([10], api)] == [0x10]


def test_descendant_pids_walks_the_process_tree() -> None:
    api = FakeWindows(
        processes=[
            (0, 0),  # System Idle Process is its own parent
            (4, 0),
            (100, 4),  # launcher
            (200, 100),  # chrome browser
            (201, 200),  # renderer
            (202, 200),  # gpu
            (300, 201),  # grandchild
            (400, 4),  # unrelated
            (500, 300),  # pid reuse closes a loop back to the root
            (100, 500),
        ]
    )
    assert win32.descendant_pids(100, api) == {100, 200, 201, 202, 300, 500}
    assert win32.descendant_pids(200, api) == {200, 201, 202, 300, 500, 100}
    assert win32.descendant_pids(400, api) == {400}
    assert win32.descendant_pids(777, api) == {777}


def test_pick_browser_window_matches_the_scaled_geometry() -> None:
    windows = [
        info(1, (0, 0, 1000, 800)),
        info(2, (150, 75, 1350, 975)),  # 100,50 800x600 DIP at 1.5
        info(3, (-12000, -12000, -11000, -11000)),
    ]
    assert win32.pick_browser_window(windows, (100, 50, 800, 600), 1.5) == windows[1]
    # Invisible resize borders of a maximized frame stay within the tolerance.
    maximized = [info(4, (-8, -8, 1928, 1048)), windows[0]]
    assert (
        win32.pick_browser_window(maximized, (0, 0, 1920, 1040), 1.0) == (maximized[0])
    )


def test_pick_browser_window_prefers_the_closest_then_the_topmost() -> None:
    near = info(1, (10, 10, 810, 610))
    exact = info(2, (0, 0, 800, 600))
    twin = info(3, (0, 0, 800, 600))
    assert win32.pick_browser_window([near, exact, twin], (0, 0, 800, 600), 1) == exact
    assert win32.pick_browser_window([twin, exact], (0, 0, 800, 600), 1) == twin


def test_pick_browser_window_without_a_match() -> None:
    first = info(1, (0, 0, 800, 600))
    second = info(2, (900, 0, 1700, 600))
    parked = info(3, (-32000, -32000, -31840, -31972), minimized=True)
    geometry = (300, 300, 400, 400)
    assert win32.pick_browser_window([first, second], geometry, 1.0) is None
    assert win32.pick_browser_window([first, second], None, 1.0) is None
    assert win32.pick_browser_window([first], geometry, 1.0) == first
    assert win32.pick_browser_window([parked], None, 2.0) == parked
    # A minimized window never matches by geometry, even at the parking spot.
    assert (
        win32.pick_browser_window(
            [parked, first], (-32000, -32000, 160, 28), 1.0, tolerance=1
        )
        is None
    )
    assert win32.pick_browser_window([], geometry, 1.0) is None
    with pytest.raises(ValueError, match="scale"):
        win32.pick_browser_window([first], geometry, 0)


def test_is_foreground() -> None:
    api = FakeWindows({0x10: FakeWindow(pid=1, thread_id=2)}, foreground=0x10)
    assert win32.is_foreground(0x10, api)
    assert not win32.is_foreground(0x20, api)
    api.foreground = 0
    assert not win32.is_foreground(0, api)


def background_browser(*, iconic: bool = False) -> FakeWindows:
    return FakeWindows(
        {
            0xA: FakeWindow(pid=50, thread_id=51),  # IDE in front
            0xB: FakeWindow(pid=60, thread_id=61, iconic=iconic),  # the browser
        },
        foreground=0xA,
    )


MANAGEMENT_CALLS = {"attach", "show", "set_foreground", "bring_to_top", "set_focus"}


def test_bring_to_foreground_lifts_the_lock_and_detaches() -> None:
    api = background_browser()
    assert win32.bring_to_foreground(0xB, api) is True
    assert api.foreground == 0xB
    assert api.calls == [
        ("attach", OUR_THREAD, 51, True),
        ("attach", OUR_THREAD, 61, True),
        ("set_foreground", 0xB),
        ("bring_to_top", 0xB),
        ("set_focus", 0xB),
        ("attach", OUR_THREAD, 61, False),
        ("attach", OUR_THREAD, 51, False),
    ]
    assert api.attached == set()
    # Window management only: no input of any kind.
    assert {call[0] for call in api.calls} <= MANAGEMENT_CALLS
    assert not hasattr(api, "send_input")


def test_bring_to_foreground_restores_a_minimized_window_first() -> None:
    api = background_browser(iconic=True)
    assert win32.bring_to_foreground(0xB, api) is True
    assert api.calls[0] == ("show", 0xB, win32.SW_RESTORE)
    assert win32.SW_RESTORE == 9
    assert not api.windows[0xB].iconic


def test_bring_to_foreground_already_in_front() -> None:
    api = background_browser()
    api.foreground = 0xB
    assert win32.bring_to_foreground(0xB, api) is True
    # Only the window's own thread is attached; nothing else to lift.
    assert api.calls[0] == ("attach", OUR_THREAD, 61, True)
    assert api.calls[-1] == ("attach", OUR_THREAD, 61, False)
    assert api.attached == set()


def test_bring_to_foreground_reports_a_refused_lock() -> None:
    api = background_browser()
    api.attach_ok = False
    assert win32.bring_to_foreground(0xB, api) is False
    assert api.foreground == 0xA
    # Without an attached queue SetFocus is not allowed and is not tried.
    assert [call[0] for call in api.calls] == [
        "attach",
        "attach",
        "set_foreground",
        "bring_to_top",
    ]


@pytest.mark.parametrize("failing", ["set_foreground", "bring_to_top", "set_focus"])
def test_bring_to_foreground_detaches_when_a_call_fails(failing: str) -> None:
    api = background_browser()
    api.fail_on = failing
    with pytest.raises(OSError, match=failing):
        win32.bring_to_foreground(0xB, api)
    assert api.attached == set()
    assert api.calls[-2:] == [
        ("attach", OUR_THREAD, 61, False),
        ("attach", OUR_THREAD, 51, False),
    ]


def test_bring_to_foreground_skips_our_own_thread_and_null() -> None:
    api = FakeWindows(
        {
            0xA: FakeWindow(pid=50, thread_id=OUR_THREAD),
            0xB: FakeWindow(pid=60, thread_id=61),
        },
        foreground=0xA,
    )
    api.lock = False
    assert win32.bring_to_foreground(0xB, api) is True
    assert ("attach", OUR_THREAD, OUR_THREAD, True) not in api.calls
    assert win32.bring_to_foreground(0, api) is False


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="x64 layout")
def test_window_struct_sizes_x64() -> None:
    assert ctypes.sizeof(win32.PROCESSENTRY32W) == 568
    assert ctypes.sizeof(win32.RECT) == 16


@pytest.mark.skipif(sys.platform != "win32", reason="user32 is Windows-only")
def test_real_window_queries_are_read_only() -> None:
    # Only enumeration and queries: nothing is shown, raised or focused.
    windows = win32.Win32Windows()
    own = os.getpid()
    family = win32.descendant_pids(os.getppid(), windows)
    assert own in family
    assert own in win32.descendant_pids(own, windows)
    assert windows.current_thread_id() > 0
    assert isinstance(windows.foreground_window(), int)
    handles = windows.enum_windows()
    assert handles and all(isinstance(hwnd, int) for hwnd in handles)
    for hwnd in handles[:20]:
        thread_id, _pid = windows.window_thread_process_id(hwnd)
        assert thread_id >= 0
        assert isinstance(windows.class_name(hwnd), str)
        assert isinstance(windows.ex_style(hwnd), int)
        rect = windows.window_rect(hwnd)
        assert rect is None or len(rect) == 4
    assert win32.top_level_windows({own}, windows) == []
    every_pid = {pid for pid, _parent in windows.process_parents()}
    assert own in every_pid
    assert all(
        window.class_name == CHROME and window.width > 0 and window.height > 0
        for window in win32.top_level_windows(every_pid, windows)
    )
