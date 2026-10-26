"""The native X11 input backend (emunium._native.x11) without a real X server.

``Xlib`` runs against ``FakeX``, a stand-in for libX11 and libXtst that keeps
a pointer, a screen and a keyboard mapping and records every event, so the
ctypes marshalling (out parameters, the mapping array) is exercised too.

The ``e2e`` tests at the end use the real libraries and need an X display;
they run inside the Linux container (``tests/docker/run_linux.py``) only.
"""

from __future__ import annotations

import ctypes
import http.server
import os
import sys
import threading
import time
import types
import weakref
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import pytest

from emunium import _native, _screen
from emunium._element_interactor import ElementInteractor
from emunium._native import FAILSAFE_ENV, x11
from emunium._native.x11 import (
    REMAP_QUIET,
    REMAP_REUSE_AGE,
    REMAP_SETTLE,
    KeyboardMapping,
    X11Backend,
    Xlib,
    char_keysym,
    key_name,
    wheel_buttons,
)
from emunium.errors import NativeInputError

ESCAPE = 0xFF1B
BACKSPACE = 0xFF08
TAB = 0xFF09
RETURN = 0xFF0D
LEFT = 0xFF51
PRIOR = 0xFF55
F1 = 0xFFBE
SHIFT_L = 0xFFE1
CONTROL_L = 0xFFE3
ALT_L = 0xFFE9
SUPER_L = 0xFFEB
ISO_LEFT_TAB = 0xFE20

KEYSYM_NAMES = {
    "Escape": ESCAPE,
    "BackSpace": BACKSPACE,
    "Tab": TAB,
    "Return": RETURN,
    "Left": LEFT,
    "Prior": PRIOR,
    "F1": F1,
    "Shift_L": SHIFT_L,
    "Control_L": CONTROL_L,
    "Alt_L": ALT_L,
    "Super_L": SUPER_L,
    "space": 0x20,
    "Alt_R": 0xFFEA,
}

# A US-like core mapping, two keysyms per keycode.
US_ROWS: dict[int, list[int]] = {
    9: [ESCAPE, 0],
    10: [0x31, 0x21],  # 1 !
    22: [BACKSPACE, 0],
    23: [TAB, ISO_LEFT_TAB],
    36: [RETURN, 0],
    37: [CONTROL_L, 0],
    38: [0x61, 0x41],  # a A
    50: [SHIFT_L, 0],
    55: [0x76, 0x56],  # v V
    59: [0x2C, 0x3C],  # , <
    64: [ALT_L, 0],
    65: [0x20, 0],
    67: [F1, 0],
    112: [PRIOR, 0],
    113: [LEFT, 0],
    133: [SUPER_L, 0],
}
MIN_KEYCODE = 8
MAX_KEYCODE = 140
SPARES = (137, 138, 139)

Event = tuple[Any, ...]


class FakeFunction:
    """A foreign function: prototypes can be set, calls go to ``FakeX``."""

    def __init__(self, implementation: Callable[..., Any]) -> None:
        self.implementation = implementation
        self.argtypes: Any = None
        self.restype: Any = None

    def __call__(self, *args: Any) -> Any:
        return self.implementation(*args)


class FakeLibrary:
    """libX11 or libXtst: exports by name, each a ``FakeFunction``."""

    def __init__(self, server: FakeX, names: frozenset[str]) -> None:
        self._server = server
        self._names = names
        self.functions: dict[str, FakeFunction] = {}

    def __getitem__(self, name: str, /) -> FakeFunction:
        if name not in self._names:
            raise AttributeError(f"fake library has no export {name}")
        # Fresh per lookup, like CDLL.__getitem__; Xlib caches its own.
        function = FakeFunction(getattr(self._server, name))
        self.functions[name] = function
        return function


DISPLAY = 0xD15


class FakeTimer:
    """A ``threading.Timer`` stand-in the test fires by hand."""

    def __init__(self, delay: float, callback: Callable[[], None]) -> None:
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if not self.cancelled:
            self.callback()


class FakeX:
    """X server state behind the fake libraries; ``events`` records input."""

    X11_NAMES = frozenset(
        {
            "XInitThreads",
            "XOpenDisplay",
            "XCloseDisplay",
            "XDefaultScreen",
            "XRootWindow",
            "XDisplayWidth",
            "XDisplayHeight",
            "XQueryPointer",
            "XStringToKeysym",
            "XDisplayKeycodes",
            "XGetKeyboardMapping",
            "XChangeKeyboardMapping",
            "XFree",
            "XFlush",
            "XSync",
        }
    )
    XTST_NAMES = frozenset(
        {
            "XTestQueryExtension",
            "XTestFakeMotionEvent",
            "XTestFakeButtonEvent",
            "XTestFakeKeyEvent",
        }
    )

    def __init__(
        self,
        pointer: tuple[int, int] = (500, 400),
        screen: tuple[int, int] = (1920, 1080),
        spares: tuple[int, ...] = SPARES,
        rows: dict[int, list[int]] | None = None,
    ) -> None:
        self.pointer = pointer
        self.screen = screen
        self.rows: dict[int, list[int]] = {}
        for code in range(MIN_KEYCODE, MAX_KEYCODE + 1):
            filler = [0, 0] if code in spares else [0x1008FE00 + code, 0]
            self.rows[code] = list((rows or US_ROWS).get(code, filler))
        self.calls: list[str] = []
        self.events: list[Event] = []
        self.open_ok = True
        self.xtest = True
        self.freed = 0
        self.fail_key: int | None = None
        self.steal: dict[int, list[int]] = {}
        self.x11 = FakeLibrary(self, self.X11_NAMES)
        self.xtst = FakeLibrary(self, self.XTST_NAMES)
        self._tables: list[Any] = []
        # Monotonic clock and timers of the backend's remap pool.
        self.now = 0.0
        self.timers: list[FakeTimer] = []

    def xlib(self) -> Xlib:
        return Xlib(x11=self.x11, xtst=self.xtst)

    def clock(self) -> float:
        return self.now

    def start_timer(self, delay: float, callback: Callable[[], None]) -> FakeTimer:
        timer = FakeTimer(delay, callback)
        self.timers.append(timer)
        return timer

    def input_events(self) -> list[Event]:
        return [e for e in self.events if e[0] in ("motion", "button", "key", "map")]

    # -- libX11 -------------------------------------------------------------

    def XInitThreads(self) -> int:
        self.calls.append("XInitThreads")
        return 1

    def XOpenDisplay(self, name: bytes | None) -> int | None:
        self.calls.append("XOpenDisplay")
        assert name is None
        return DISPLAY if self.open_ok else None

    def XCloseDisplay(self, display: int) -> int:
        self.calls.append("XCloseDisplay")
        return 0

    def XDefaultScreen(self, display: int) -> int:
        assert display == DISPLAY
        return 0

    def XRootWindow(self, display: int, screen: int) -> int:
        return 0x2A0

    def XDisplayWidth(self, display: int, screen: int) -> int:
        return self.screen[0]

    def XDisplayHeight(self, display: int, screen: int) -> int:
        return self.screen[1]

    def XQueryPointer(self, display: int, window: int, *outs: Any) -> int:
        assert window == 0x2A0
        root, child, root_x, root_y, win_x, win_y, mask = outs
        root.contents.value = window
        child.contents.value = 0
        root_x.contents.value, root_y.contents.value = self.pointer
        win_x.contents.value, win_y.contents.value = self.pointer
        mask.contents.value = 0
        return 1

    def XStringToKeysym(self, name: bytes) -> int:
        return KEYSYM_NAMES.get(name.decode("ascii"), 0)

    def XDisplayKeycodes(self, display: int, low: Any, high: Any) -> int:
        low.contents.value, high.contents.value = MIN_KEYCODE, MAX_KEYCODE
        return 1

    def XGetKeyboardMapping(
        self, display: int, first: int, count: int, per: Any
    ) -> Any:
        self.calls.append("XGetKeyboardMapping")
        per.contents.value = 2
        flat = [
            keysym for code in range(first, first + count) for keysym in self.rows[code]
        ]
        table = (ctypes.c_ulong * len(flat))(*flat)
        self._tables.append(table)
        return ctypes.cast(table, ctypes.POINTER(ctypes.c_ulong))

    def XChangeKeyboardMapping(
        self, display: int, first: int, per: int, keysyms: Any, count: int
    ) -> int:
        values = [int(keysyms[index]) for index in range(per * count)]
        for offset in range(count):
            row = values[offset * per : (offset + 1) * per]
            self.events.append(("map", first + offset, row))
            # Another client rewrites the row right after this request.
            self.rows[first + offset] = self.steal.pop(first + offset, row)
        return 0

    def XFree(self, data: Any) -> int:
        self.freed += 1
        return 1

    def XFlush(self, display: int) -> int:
        self.events.append(("flush",))
        return 1

    def XSync(self, display: int, discard: int) -> int:
        self.events.append(("sync",))
        return 1

    # -- libXtst ------------------------------------------------------------

    def XTestQueryExtension(self, display: int, *outs: Any) -> int:
        for out in outs:
            out.contents.value = 2
        return 1 if self.xtest else 0

    def XTestFakeMotionEvent(
        self, display: int, screen: int, x: int, y: int, delay: int
    ) -> int:
        assert (screen, delay) == (-1, 0)
        self.pointer = (x, y)
        self.events.append(("motion", x, y))
        return 1

    def XTestFakeButtonEvent(
        self, display: int, button: int, press: int, delay: int
    ) -> int:
        assert delay == 0
        self.events.append(("button", button, bool(press)))
        return 1

    def XTestFakeKeyEvent(
        self, display: int, keycode: int, press: int, delay: int
    ) -> int:
        assert delay == 0
        if keycode == self.fail_key:
            raise OSError("key event failed")
        self.events.append(("key", keycode, bool(press)))
        return 1


@pytest.fixture(autouse=True)
def failsafe_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FAILSAFE_ENV, raising=False)


def make(
    pointer: tuple[int, int] = (500, 400), **kwargs: Any
) -> tuple[X11Backend, FakeX, list[float]]:
    server = FakeX(pointer=pointer, **kwargs)
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        server.now += seconds

    backend = X11Backend(
        server.xlib(), sleep=sleep, clock=server.clock, timer=server.start_timer
    )
    return backend, server, sleeps


def tap(code: int) -> list[Event]:
    return [("key", code, True), ("key", code, False)]


def shifted(code: int) -> list[Event]:
    return [("key", 50, True), *tap(code), ("key", 50, False)]


# -- pure helpers ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("character", "keysym"),
    [
        ("a", 0x61),
        ("A", 0x41),
        (" ", 0x20),
        ("~", 0x7E),
        ("é", 0xE9),
        ("ÿ", 0xFF),
        ("Ж", 0x01000416),
        ("€", 0x010020AC),
        ("✓", 0x01002713),
        ("👋", 0x0101F44B),
        ("‍", 0x0100200D),  # ZWJ, joins emoji sequences
    ],
)
def test_char_keysym(character: str, keysym: int) -> None:
    assert char_keysym(character) == keysym


@pytest.mark.parametrize("bad", ["", "ab", "\x07", "\x85", "\ud800"])
def test_char_keysym_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        char_keysym(bad)


def test_key_names_are_case_insensitive_and_unknown_names_raise() -> None:
    assert key_name("Enter") == "Return"
    assert key_name("ctrl") == "Control_L"
    assert key_name("pagedown") == "Next"
    assert key_name("f24") == "F24"
    assert key_name("num7") == "KP_7"
    assert key_name("\n") == "Return"
    assert key_name("a") is None and key_name("Ж") is None
    with pytest.raises(ValueError):
        key_name("notakey")
    with pytest.raises(ValueError):
        key_name("")


def test_wheel_buttons() -> None:
    assert wheel_buttons(3) == [4, 4, 4]
    assert wheel_buttons(-2) == [5, 5]
    assert wheel_buttons(2, horizontal=True) == [7, 7]
    assert wheel_buttons(-1, horizontal=True) == [6]
    assert wheel_buttons(0) == []
    with pytest.raises(TypeError):
        wheel_buttons(True)


def test_mapping_prefers_the_plain_level_and_lists_spares_highest_first() -> None:
    mapping = KeyboardMapping(8, 2, [0x41, 0x61, 0, 0, 0x61, 0x41, 0, 0])
    assert mapping.rows == {8: [0x41, 0x61], 9: [0, 0], 10: [0x61, 0x41], 11: [0, 0]}
    assert mapping.find(0x61) == (10, 0)
    assert mapping.find(0x41) == (8, 0)
    assert mapping.find(0x42) is None
    assert mapping.spare_keycodes() == [11, 9]


# -- the ctypes layer ----------------------------------------------------------------


def test_xinitthreads_comes_first_and_prototypes_are_set() -> None:
    server = FakeX()
    xlib = server.xlib()
    assert server.calls[:2] == ["XInitThreads", "XOpenDisplay"]
    xlib.pointer()
    xlib.motion(1, 1)
    query = server.x11.functions["XQueryPointer"]
    assert query.restype is ctypes.c_int
    assert len(query.argtypes) == 9
    motion = server.xtst.functions["XTestFakeMotionEvent"]
    assert motion.argtypes[-1] is ctypes.c_ulong


def test_no_display_raises_with_the_display_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISPLAY", ":42")
    server = FakeX()
    server.open_ok = False
    with pytest.raises(NativeInputError, match="':42'"):
        server.xlib()


def test_missing_xtest_closes_the_display() -> None:
    server = FakeX()
    server.xtest = False
    with pytest.raises(NativeInputError, match="XTEST"):
        server.xlib()
    assert server.calls[-1] == "XCloseDisplay"


def test_missing_library_is_a_native_input_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(name: str) -> Any:
        raise OSError(f"{name}: cannot open shared object file")

    monkeypatch.setattr(x11.ctypes, "CDLL", missing)
    with pytest.raises(NativeInputError, match="libx11-6"):
        Xlib()


def test_keyboard_mapping_reads_and_frees_the_table() -> None:
    server = FakeX()
    mapping = server.xlib().keyboard_mapping()
    assert server.freed == 1
    assert mapping.first == MIN_KEYCODE and mapping.per_keycode == 2
    assert mapping.rows[38] == [0x61, 0x41]
    assert mapping.spare_keycodes() == [139, 138, 137]


def test_shared_xlib_is_opened_once(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeX()
    monkeypatch.setattr(x11, "_SHARED", None)
    monkeypatch.setattr(x11, "Xlib", server.xlib)
    results: list[Xlib] = []
    threads = [
        threading.Thread(target=lambda: results.append(x11.shared_xlib()))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 8 and all(xlib is results[0] for xlib in results)
    assert server.calls.count("XOpenDisplay") == 1


# -- pointer and buttons -----------------------------------------------------------


def test_position_size_and_virtual_screen() -> None:
    backend, _, _ = make(pointer=(12, 34))
    assert backend.position() == (12, 34)
    assert backend.position().x == 12
    assert backend.size() == (1920, 1080)
    assert backend.size().width == 1920
    assert backend.virtual_screen() == (0, 0, 1920, 1080)


def test_move_to_rounds_and_flushes() -> None:
    backend, server, _ = make()
    backend.moveTo(100.6, 200.4, _pause=False)
    assert server.events == [("motion", 101, 200), ("flush",)]
    assert backend.position() == (101, 200)


@pytest.mark.parametrize(
    ("button", "number"), [("left", 1), ("middle", 2), ("right", 3)]
)
def test_buttons_press_at_current_position(button: str, number: int) -> None:
    backend, server, _ = make()
    backend.mouseDown(button=button)
    backend.mouseUp(button=button)
    assert server.input_events() == [
        ("button", number, True),
        ("button", number, False),
    ]


def test_buttons_move_first_when_given_a_point() -> None:
    backend, server, _ = make(pointer=(5, 6))
    backend.mouseDown(x=50, button="left")
    assert server.input_events() == [("motion", 50, 6), ("button", 1, True)]


def test_bad_button_sends_nothing() -> None:
    backend, server, _ = make()
    with pytest.raises(ValueError):
        backend.mouseDown(button="side")
    assert server.events == []


def test_scroll_and_hscroll_click_the_wheel_buttons() -> None:
    backend, server, _ = make()
    backend.scroll(2)
    backend.scroll(-1, x=10, y=20)
    backend.hscroll(1)
    backend.hscroll(-1)
    press_release = [(True,), (False,)]
    assert server.input_events() == [
        *[("button", 4, *state) for state in press_release] * 2,
        ("motion", 10, 20),
        *[("button", 5, *state) for state in press_release],
        *[("button", 7, *state) for state in press_release],
        *[("button", 6, *state) for state in press_release],
    ]


# -- keys --------------------------------------------------------------------------


def test_key_down_up_named_keys() -> None:
    backend, server, _ = make()
    backend.keyDown("ctrl")
    backend.keyDown("Enter")
    backend.keyUp("enter")
    backend.keyUp("ctrl")
    assert server.input_events() == [
        ("key", 37, True),
        ("key", 36, True),
        ("key", 36, False),
        ("key", 37, False),
    ]


def test_key_down_of_a_shifted_character_is_its_key_without_shift() -> None:
    backend, server, _ = make()
    backend.keyDown("V")
    backend.keyUp("V")
    assert server.input_events() == tap(55)


def test_unknown_key_sends_nothing() -> None:
    backend, server, _ = make()
    with pytest.raises(ValueError):
        backend.keyDown("notakey")
    with pytest.raises(NativeInputError, match="no key carries 'Ж'"):
        backend.keyDown("Ж")
    with pytest.raises(NativeInputError, match="F24"):
        backend.press("f24")
    assert server.input_events() == []


def test_press_taps_keys_and_wraps_shift_level_characters() -> None:
    backend, server, _ = make()
    backend.press("enter")
    backend.press(["a", "A", "!"], presses=2)
    assert server.input_events() == tap(36) + 2 * (tap(38) + shifted(38) + shifted(10))
    with pytest.raises(ValueError):
        backend.press("a", presses=-1)
    with pytest.raises(TypeError):
        backend.press("a", presses=True)


def test_write_ascii_through_the_layout() -> None:
    backend, server, sleeps = make()
    backend.write("aA, 1!\r\n\t")
    assert server.input_events() == (
        tap(38)
        + shifted(38)
        + tap(59)
        + tap(65)
        + tap(10)
        + shifted(10)
        + tap(36)
        + tap(23)
    )
    assert server.events[-1] == ("flush",)
    assert sleeps == []


ZHE, SMALL_ZHE, YO, WAVE = 0x01000416, 0x01000436, 0x01000401, 0x0101F44B


def remap_events(server: FakeX) -> list[Event]:
    return [e for e in server.events if e[0] in ("map", "key")]


def test_write_unicode_on_spare_keycodes_restored_after_the_quiet_period() -> None:
    backend, server, sleeps = make()
    backend.write("ЖaЖ👋")
    assert [e for e in server.events if e[0] != "flush"] == [
        ("map", 139, [ZHE, ZHE]),
        ("sync",),
        *tap(139),
        *tap(38),
        *tap(139),
        ("map", 138, [WAVE, WAVE]),
        ("sync",),
        *tap(138),
    ]
    # REMAP_SETTLE before the first key event of each newly mapped keycode.
    assert sleeps == [REMAP_SETTLE, REMAP_SETTLE]
    assert server.rows[139] == [ZHE, ZHE] and server.rows[138] == [WAVE, WAVE]
    [timer] = server.timers
    assert timer.delay == REMAP_QUIET
    server.events.clear()
    timer.fire()
    assert server.events == [("map", 139, [0, 0]), ("map", 138, [0, 0]), ("sync",)]
    assert server.rows[138] == server.rows[139] == [0, 0]


def test_consecutive_characters_take_distinct_keycodes() -> None:
    backend, server, _ = make()
    for character in "ЖжЁ":
        backend.write(character)
    assert remap_events(server) == [
        ("map", 139, [ZHE, ZHE]),
        *tap(139),
        ("map", 138, [SMALL_ZHE, SMALL_ZHE]),
        *tap(138),
        ("map", 137, [YO, YO]),
        *tap(137),
    ]


def test_a_mapped_character_reuses_its_keycode_without_remapping() -> None:
    backend, server, sleeps = make()
    backend.write("Ж")
    backend.write("Жa")
    assert remap_events(server) == [
        ("map", 139, [ZHE, ZHE]),
        *tap(139),
        *tap(139),
        *tap(38),
    ]
    assert sleeps == [REMAP_SETTLE]


def test_each_remapped_write_restarts_the_quiet_period() -> None:
    backend, server, _ = make()
    backend.write("Ж")
    backend.write("a")
    assert len(server.timers) == 1
    backend.write("Ж")
    first, second = server.timers
    assert first.cancelled and not second.cancelled
    # A stale expiry that raced the reschedule restores nothing.
    first.callback()
    assert server.rows[139] == [ZHE, ZHE]
    second.fire()
    assert server.rows[139] == [0, 0]


@pytest.mark.parametrize("release", [X11Backend.close, X11Backend.release_remaps])
def test_close_and_release_remaps_restore_at_once(
    release: Callable[[X11Backend], None],
) -> None:
    backend, server, _ = make()
    backend.write("Ж👋")
    server.events.clear()
    release(backend)
    assert server.events == [("map", 139, [0, 0]), ("map", 138, [0, 0]), ("sync",)]
    assert server.timers[-1].cancelled
    # Released keycodes are spare again: the next character maps the highest.
    backend.write("ж")
    assert ("map", 139, [SMALL_ZHE, SMALL_ZHE]) in server.events


def test_a_full_pool_recycles_the_least_recently_used_keycode() -> None:
    backend, server, sleeps = make(spares=(139, 138))
    backend.write("Ж")
    backend.write("ж")
    server.now += 1.0
    backend.write("Ж")  # 138 is now the least recently used
    sleeps.clear()
    backend.write("Ё")
    assert remap_events(server)[-3:] == [("map", 138, [YO, YO]), *tap(138)]
    assert server.rows[139] == [ZHE, ZHE]
    assert sleeps == [REMAP_SETTLE]


def test_the_pool_holds_at_most_remap_pool_size_keycodes() -> None:
    spares = tuple(range(114, 133))  # 19 spare keycodes
    backend, server, _ = make(spares=spares)
    letters = "абвгдежзийклмнопр"
    assert len(letters) == x11.REMAP_POOL_SIZE + 1
    for letter in letters:
        server.now += 1.0
        backend.write(letter)
    mapped = [e[1] for e in server.events if e[0] == "map"]
    # 16 keycodes, highest first; the 17th letter recycles the oldest one.
    assert mapped == [*range(132, 116, -1), 132]
    assert server.rows[132] == [char_keysym("р")] * 2


def test_a_recently_used_keycode_is_recycled_only_once_old_enough() -> None:
    backend, server, sleeps = make(spares=(139,))
    backend.write("Жж")
    assert remap_events(server) == [
        ("map", 139, [ZHE, ZHE]),
        *tap(139),
        ("map", 139, [SMALL_ZHE, SMALL_ZHE]),
        *tap(139),
    ]
    # Ж was tapped REMAP_SETTLE ago: the remap waits for the rest of the age.
    assert sleeps == pytest.approx(
        [REMAP_SETTLE, REMAP_REUSE_AGE - REMAP_SETTLE, REMAP_SETTLE]
    )


def test_pooled_keycodes_are_not_keys_to_hold() -> None:
    backend, server, _ = make()
    backend.write("Ж")
    with pytest.raises(NativeInputError, match="no key carries"):
        backend.keyDown("Ж")
    assert remap_events(server) == [("map", 139, [ZHE, ZHE]), *tap(139)]


def test_no_spare_keycode_raises_before_any_input() -> None:
    backend, server, _ = make(spares=())
    with pytest.raises(NativeInputError, match="no spare keycode"):
        backend.write("aЖ")
    assert server.input_events() == []


def test_mapping_is_restored_when_a_key_event_fails() -> None:
    backend, server, _ = make()
    server.fail_key = 139
    with pytest.raises(OSError):
        backend.write("Ж")
    assert server.rows[139] == [ZHE, ZHE]
    server.timers[-1].fire()
    assert server.rows[139] == [0, 0]
    assert server.events[-2:] == [("map", 139, [0, 0]), ("sync",)]


def test_exit_hook_is_registered_once_and_restores_every_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered: list[Callable[[], None]] = []
    monkeypatch.setattr(x11, "_EXIT_HOOK_REGISTERED", False)
    monkeypatch.setattr(x11, "_EXIT_POOLS", weakref.WeakSet())
    monkeypatch.setattr(x11.atexit, "register", registered.append)
    first, first_server, _ = make()
    second, second_server, _ = make()
    first.write("a")
    assert registered == []
    first.write("Ж")
    second.write("Ж")
    first.write("ж")
    assert registered == [x11.release_all_remaps]
    x11.release_all_remaps()
    assert first_server.rows[139] == first_server.rows[138] == [0, 0]
    assert second_server.rows[139] == [0, 0]


def test_exit_hook_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(x11, "_EXIT_HOOK_REGISTERED", True)
    monkeypatch.setattr(x11, "_EXIT_POOLS", weakref.WeakSet())
    server = FakeX()
    xlib = server.xlib()
    backend = X11Backend(
        xlib, sleep=lambda s: None, clock=server.clock, timer=server.start_timer
    )
    backend.write("Ж")

    def broken(keycode: int, keysyms: Sequence[int]) -> None:
        raise OSError("the display went away")

    monkeypatch.setattr(xlib, "change_mapping", broken)
    x11.release_all_remaps()
    assert server.rows[139] == [ZHE, ZHE]


def test_exit_hook_gives_up_on_a_busy_display(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(x11, "_EXIT_HOOK_REGISTERED", True)
    monkeypatch.setattr(x11, "_EXIT_POOLS", weakref.WeakSet())
    monkeypatch.setattr(x11, "_EXIT_LOCK_TIMEOUT", 0.05)
    server = FakeX()
    xlib = server.xlib()
    backend = X11Backend(
        xlib, sleep=lambda s: None, clock=server.clock, timer=server.start_timer
    )
    backend.write("Ж")
    held, done = threading.Event(), threading.Event()

    def hold_the_display() -> None:
        with xlib.lock:
            held.set()
            done.wait(5.0)

    holder = threading.Thread(target=hold_the_display)
    holder.start()
    try:
        assert held.wait(5.0)
        with caplog.at_level("WARNING", logger=x11.__name__):
            x11.release_all_remaps()
    finally:
        done.set()
        holder.join()
    assert "display stayed busy" in caplog.text
    assert server.rows[139] == [ZHE, ZHE]


FOREIGN = 0x1008FF01


def test_a_pooled_keycode_changed_by_someone_else_is_left_to_them() -> None:
    backend, server, _ = make()
    backend.write("Ж")
    server.rows[139] = [FOREIGN, 0]  # xmodmap / setxkbmap / another client
    backend.write("Ж")
    assert remap_events(server) == [
        ("map", 139, [ZHE, ZHE]),
        *tap(139),
        ("map", 138, [ZHE, ZHE]),
        *tap(138),
    ]
    backend.release_remaps()
    assert server.rows[139] == [FOREIGN, 0]
    assert server.rows[138] == [0, 0]


def test_release_restores_only_rows_still_holding_the_pooled_keysym() -> None:
    backend, server, _ = make()
    backend.write("Ж👋")
    server.rows[139] = [FOREIGN, 0]
    server.events.clear()
    server.timers[-1].fire()
    assert server.events == [("map", 138, [0, 0]), ("sync",)]
    assert server.rows[139] == [FOREIGN, 0]


def test_a_keycode_taken_during_the_remap_moves_on_to_the_next_spare() -> None:
    backend, server, _ = make()
    server.steal = {139: [FOREIGN, FOREIGN]}
    backend.write("Ж")
    assert remap_events(server) == [
        ("map", 139, [ZHE, ZHE]),
        ("map", 138, [ZHE, ZHE]),
        *tap(138),
    ]
    backend.release_remaps()
    assert server.rows[139] == [FOREIGN, FOREIGN]
    assert server.rows[138] == [0, 0]


def test_failsafe_is_checked_before_every_remapped_character() -> None:
    server = FakeX()

    def park_in_corner(seconds: float) -> None:
        server.pointer = CORNER

    backend = X11Backend(
        server.xlib(),
        sleep=park_in_corner,
        clock=server.clock,
        timer=server.start_timer,
    )
    with pytest.raises(NativeInputError, match="fail-safe"):
        backend.write("ЖжЁ")
    # Ж went out before the cursor reached the corner; nothing after it.
    assert remap_events(server) == [("map", 139, [ZHE, ZHE]), *tap(139)]
    assert not server.timers[-1].cancelled


def test_a_due_restore_does_nothing_while_a_write_runs() -> None:
    server = FakeX(spares=(139,))
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        server.now += seconds
        if seconds > REMAP_SETTLE:  # a wait for 139 to age
            waits.append(seconds)
            # The restore armed by the previous write comes due right now.
            server.timers[0].callback()

    backend = X11Backend(
        server.xlib(), sleep=sleep, clock=server.clock, timer=server.start_timer
    )
    backend.write("Ё")
    backend.write("Жж")
    assert len(waits) == 2
    assert remap_events(server) == [
        ("map", 139, [YO, YO]),
        *tap(139),
        ("map", 139, [ZHE, ZHE]),
        *tap(139),
        ("map", 139, [SMALL_ZHE, SMALL_ZHE]),
        *tap(139),
    ]
    server.timers[-1].fire()
    assert server.events[-2:] == [("map", 139, [0, 0]), ("sync",)]


def test_the_reuse_wait_reads_the_mapping_again() -> None:
    server = FakeX(spares=(139,))

    def sleep(seconds: float) -> None:
        server.now += seconds
        if seconds > REMAP_SETTLE:  # xmodmap ran meanwhile
            server.rows[139] = [FOREIGN, 0]

    backend = X11Backend(
        server.xlib(), sleep=sleep, clock=server.clock, timer=server.start_timer
    )
    # ж needs Ж's keycode, the only spare; meanwhile the row was taken.
    with pytest.raises(NativeInputError, match="no spare keycode"):
        backend.write("Жж")
    assert remap_events(server) == [("map", 139, [ZHE, ZHE]), *tap(139)]
    assert server.rows[139] == [FOREIGN, 0]


def test_release_remaps_from_another_thread_waits_for_the_write() -> None:
    server = FakeX()
    releasers: list[threading.Thread] = []

    def sleep(seconds: float) -> None:
        server.now += seconds
        if not releasers:  # Ж's settle: the write is half done
            releaser = threading.Thread(target=backend.release_remaps)
            releasers.append(releaser)
            releaser.start()
            releaser.join(0.2)
            assert releaser.is_alive(), "release_remaps did not wait"

    backend = X11Backend(
        server.xlib(), sleep=sleep, clock=server.clock, timer=server.start_timer
    )
    backend.write("Жж")
    [releaser] = releasers
    releaser.join(5.0)
    assert not releaser.is_alive()
    assert remap_events(server) == [
        ("map", 139, [ZHE, ZHE]),
        *tap(139),
        ("map", 138, [SMALL_ZHE, SMALL_ZHE]),
        *tap(138),
        ("map", 139, [0, 0]),
        ("map", 138, [0, 0]),
    ]


def test_write_rejects_bad_input_before_sending() -> None:
    backend, server, _ = make()
    not_text: Any = b"abc"
    with pytest.raises(TypeError):
        backend.write(not_text)
    with pytest.raises(ValueError):
        backend.write("a\x07")
    assert server.events == []


# -- fail-safe ---------------------------------------------------------------------


CORNER = (1919, 0)


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
        lambda b: b.press("a"),
        lambda b: b.write("aЖ"),
    ],
)
def test_every_action_checks_failsafe(action: Callable[[X11Backend], None]) -> None:
    backend, server, _ = make(pointer=CORNER)
    with pytest.raises(NativeInputError, match="fail-safe"):
        action(backend)
    assert server.input_events() == []


def test_failsafe_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAILSAFE_ENV, "0")
    backend, server, _ = make(pointer=CORNER)
    backend.mouseDown()
    assert server.input_events() == [("button", 1, True)]


def test_raw_releases_skip_failsafe() -> None:
    backend, server, _ = make(pointer=CORNER)
    raw = backend.platformModule
    assert raw._position() == CORNER
    raw._mouseUp(0, 0, "left")
    raw._keyDown("escape")
    raw._keyUp("ctrl")
    assert server.input_events() == [
        ("button", 1, False),
        ("key", 9, True),
        ("key", 37, False),
    ]


def test_interactor_hotkey_and_release_in_a_corner() -> None:
    backend, server, _ = make()
    interactor = ElementInteractor(
        backend=backend, platform="linux", sleep=lambda s: None
    )
    interactor.hotkey("ctrl", "v")
    assert server.input_events() == [
        ("key", 37, True),
        ("key", 55, True),
        ("key", 55, False),
        ("key", 37, False),
    ]
    server.events.clear()

    def park_in_corner(seconds: float) -> None:
        server.pointer = CORNER

    interactor = ElementInteractor(
        backend=backend, platform="linux", sleep=park_in_corner
    )
    interactor.press("left")
    # The button went down before the corner; the release still happens.
    assert server.input_events() == [("button", 1, True), ("button", 1, False)]


class NoClipboard(types.ModuleType):
    """A ``pyperclip`` stand-in that records any use of the clipboard."""

    def __init__(self) -> None:
        super().__init__("pyperclip")
        self.calls: list[str] = []

    def copy(self, text: str) -> None:
        self.calls.append(f"copy {text!r}")
        raise AssertionError("the clipboard was used")

    def paste(self) -> str:
        self.calls.append("paste")
        raise AssertionError("the clipboard was used")


def test_backend_declares_native_unicode_write() -> None:
    backend, _, _ = make()
    assert X11Backend.supports_unicode_write is True
    assert ElementInteractor._writes_unicode(backend)


def test_interactor_types_unicode_through_write_without_the_clipboard() -> None:
    backend, server, _ = make()
    clipboard = NoClipboard()
    interactor = ElementInteractor(
        backend=backend,
        platform="linux",
        clipboard=clipboard,
        keyboard=None,
        sleep=lambda s: None,
    )
    interactor.type_text("aЖ👋", human=False)
    # One write per character; each new character gets its own keycode.
    assert remap_events(server) == [
        *tap(38),
        ("map", 139, [ZHE, ZHE]),
        *tap(139),
        ("map", 138, [WAVE, WAVE]),
        *tap(138),
    ]
    assert clipboard.calls == []


# -- selection ---------------------------------------------------------------------


def test_native_backend_selects_x11_with_a_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeX()
    monkeypatch.setattr(x11, "shared_xlib", server.xlib)
    backend = _native.native_backend("linux", {"DISPLAY": ":0"})
    assert isinstance(backend, X11Backend)
    # XWayland sets both; X11 wins.
    both = {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}
    assert isinstance(_native.native_backend("linux", both), X11Backend)


def test_native_backend_refuses_pure_wayland() -> None:
    with pytest.raises(NativeInputError, match="Wayland") as raised:
        _native.native_backend("linux", {"WAYLAND_DISPLAY": "wayland-0"})
    assert "XWayland" in str(raised.value)


def test_native_backend_without_any_display_or_off_linux() -> None:
    assert _native.native_backend("linux", {}) is None
    assert _native.native_backend("darwin", {"DISPLAY": ":0"}) is None


def test_input_backend_on_x11_never_imports_pyautogui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeX()

    def no_pyautogui() -> Any:
        raise AssertionError("pyautogui imported on X11")

    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(x11, "shared_xlib", server.xlib)
    monkeypatch.setattr(_screen, "_BACKEND", None)
    monkeypatch.setattr(_screen, "_platform", lambda: "linux")
    monkeypatch.setattr(_screen, "pyautogui", no_pyautogui)
    assert isinstance(_screen.input_backend(), X11Backend)


# -- real X server (Linux container only) ------------------------------------------

real_x11 = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not os.environ.get("DISPLAY"),
    reason="needs Linux with an X display (tests/docker/run_linux.py)",
)


@pytest.fixture
def real_backend() -> X11Backend:
    return X11Backend(x11.shared_xlib())


@pytest.mark.e2e
@real_x11
def test_real_pointer_round_trip(real_backend: X11Backend) -> None:
    width, height = real_backend.size()
    assert width > 100 and height > 100
    real_backend.moveTo(width // 3, height // 4)
    assert real_backend.position() == (width // 3, height // 4)
    real_backend.moveTo(width // 2, height // 2)
    assert real_backend.position() == (width // 2, height // 2)


@pytest.mark.e2e
@real_x11
def test_real_write_restores_the_keyboard_mapping(real_backend: X11Backend) -> None:
    xlib = x11.shared_xlib()
    # The pool is the connection's: start from no keycode remapped.
    real_backend.release_remaps()
    before = xlib.keyboard_mapping().rows
    assert xlib.keyboard_mapping().spare_keycodes(), "no spare keycode on this server"
    real_backend.write("Ж")
    # Ж stays mapped through the quiet period, then every row comes back.
    assert xlib.keyboard_mapping().find(char_keysym("Ж")) is not None
    deadline = time.monotonic() + REMAP_QUIET + 5.0
    while xlib.keyboard_mapping().rows != before and time.monotonic() < deadline:
        time.sleep(0.1)
    assert xlib.keyboard_mapping().rows == before


_PAGE = (
    b"<!doctype html><html><head><meta charset='utf-8'><title>x11</title></head>"
    b"<body><input id='field' style='width:600px;font-size:20px'></body></html>"
)


class _PageHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_PAGE)))
        self.end_headers()
        self.wfile.write(_PAGE)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def page_url() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.e2e
@real_x11
def test_real_write_types_any_text_into_chromium(page_url: str) -> None:
    from emunium import Browser, ensure_chrome

    text = "Hello, Привет Ёж 你好 ✓ 👋🚀 ok!"
    browser = Browser(
        chrome_path=ensure_chrome(),
        extra_args=["--window-position=60,40", "--window-size=900,600"],
        bridge_timeout=30.0,
    )
    try:
        browser.launch()
        browser.goto(page_url)
        browser.click("#field", human=False)
        backend = _screen.input_backend()
        assert isinstance(backend, X11Backend)
        backend.write(text)
        deadline = time.monotonic() + 5.0
        value = None
        while time.monotonic() < deadline:
            value = browser.execute_script(
                "return document.getElementById('field').value"
            )
            if value == text:
                break
            time.sleep(0.1)
        assert value == text
    finally:
        browser.close()
