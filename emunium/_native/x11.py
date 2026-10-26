"""Linux X11 input through libX11 and the XTest extension, ctypes only.

``XTestFakeMotionEvent`` / ``XTestFakeButtonEvent`` / ``XTestFakeKeyEvent``
carry the pointer, buttons, the wheel (buttons 4-7) and keys; ``XQueryPointer``
and the root window size answer ``position`` and ``size``. Coordinates are
root-window pixels, the unit Chrome's screen geometry maps to on X11.

Keys are looked up in the server's keyboard mapping, fetched fresh for every
action (the user may switch layouts at any time): a keysym on a key's first
level is pressed plainly, one on its second level with Shift. Any other
keysym, which covers every character the layout lacks (Cyrillic on a US map,
CJK, emoji as ``0x01000000 + codepoint``), is typed on a spare keycode, one
with no symbols at all, temporarily mapped to it with
``XChangeKeyboardMapping`` (``RemapPool``). X clients read a changed mapping
lazily, when they handle the ``MappingNotify``, so a mapped keycode keeps its
symbol while a client may still be reading it: consecutive characters take
different keycodes (least recently used first), a character already mapped
reuses its keycode, and every keycode gets its empty row back only after
``REMAP_QUIET`` without remapped input, on ``X11Backend.release_remaps`` or at
interpreter exit. A pooled keycode whose row someone else changed (setxkbmap,
xmodmap, another client) is neither typed on nor restored.

Run one emunium process per display: pools of different processes do not
see each other. A mapping is read back after every change, so a keycode
another process took meanwhile is left to it, but a pooled keycode it remaps
between two of our reads can still carry its keysym for one key event.

One display connection serves the process (``shared_xlib``); ``XInitThreads``
runs before any other Xlib call and every multi-step sequence holds the
connection's lock. Works under XWayland too; pure Wayland has no X display.
"""

from __future__ import annotations

import atexit
import ctypes
import logging
import os
import threading
import time
import unicodedata
import weakref
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any, NamedTuple, Protocol

from emunium._native import check_failsafe
from emunium._native.win32 import Point, Size, VirtualScreen, split_strokes
from emunium.errors import NativeInputError

LIBX11 = "libX11.so.6"
LIBXTST = "libXtst.so.6"

NO_SYMBOL = 0
# Event delay for the XTest calls: CurrentTime, i.e. immediately.
CURRENT_TIME = 0
# XTestFakeMotionEvent screen argument: the screen the pointer is on.
CURRENT_SCREEN = -1

BUTTON_LEFT = 1
BUTTON_MIDDLE = 2
BUTTON_RIGHT = 3
WHEEL_UP = 4
WHEEL_DOWN = 5
WHEEL_LEFT = 6
WHEEL_RIGHT = 7
_BUTTONS = {"left": BUTTON_LEFT, "middle": BUTTON_MIDDLE, "right": BUTTON_RIGHT}

UNICODE_KEYSYM_OFFSET = 0x01000000
SHIFT_KEYSYM_NAME = "Shift_L"

logger = logging.getLogger(__name__)

# Head start for clients between mapping a keycode and its first key event:
# they fetch the new mapping when they handle the MappingNotify.
REMAP_SETTLE = 0.025
# At most this many spare keycodes carry temporary symbols at once.
REMAP_POOL_SIZE = 16
# A keycode gets a new symbol only once its last key event is this old (s).
REMAP_REUSE_AGE = 0.5
# Remapped keycodes get their empty rows back after this long without
# remapped input (s).
REMAP_QUIET = 0.75

# pyautogui key names -> X keysym names (XStringToKeysym).
_KEY_NAMES: dict[str, str] = {
    **{f"f{number}": f"F{number}" for number in range(1, 25)},
    **{f"num{digit}": f"KP_{digit}" for digit in range(10)},
    "enter": "Return",
    "return": "Return",
    "\n": "Return",
    "\r": "Return",
    "tab": "Tab",
    "\t": "Tab",
    "space": "space",
    "backspace": "BackSpace",
    "escape": "Escape",
    "esc": "Escape",
    "left": "Left",
    "up": "Up",
    "right": "Right",
    "down": "Down",
    "home": "Home",
    "end": "End",
    "pageup": "Prior",
    "pgup": "Prior",
    "pagedown": "Next",
    "pgdn": "Next",
    "insert": "Insert",
    "delete": "Delete",
    "del": "Delete",
    "capslock": "Caps_Lock",
    "numlock": "Num_Lock",
    "scrolllock": "Scroll_Lock",
    "printscreen": "Print",
    "prtsc": "Print",
    "pause": "Pause",
    "apps": "Menu",
    "menu": "Menu",
    "multiply": "KP_Multiply",
    "add": "KP_Add",
    "subtract": "KP_Subtract",
    "decimal": "KP_Decimal",
    "divide": "KP_Divide",
    "numpadenter": "KP_Enter",
    "volumemute": "XF86AudioMute",
    "volumedown": "XF86AudioLowerVolume",
    "volumeup": "XF86AudioRaiseVolume",
    "nexttrack": "XF86AudioNext",
    "prevtrack": "XF86AudioPrev",
    "playpause": "XF86AudioPlay",
    "shift": "Shift_L",
    "shiftleft": "Shift_L",
    "lshift": "Shift_L",
    "shiftright": "Shift_R",
    "rshift": "Shift_R",
    "ctrl": "Control_L",
    "control": "Control_L",
    "ctrlleft": "Control_L",
    "lctrl": "Control_L",
    "ctrlright": "Control_R",
    "rctrl": "Control_R",
    "alt": "Alt_L",
    "option": "Alt_L",
    "altleft": "Alt_L",
    "lalt": "Alt_L",
    "altright": "Alt_R",
    "ralt": "Alt_R",
    "win": "Super_L",
    "winleft": "Super_L",
    "lwin": "Super_L",
    "super": "Super_L",
    "command": "Super_L",
    "cmd": "Super_L",
    "winright": "Super_R",
    "rwin": "Super_R",
}


# -- pure helpers --------------------------------------------------------------


def char_keysym(character: str) -> int:
    """The keysym that types ``character``.

    Latin-1 characters are their own keysyms; everything else is the Unicode
    keysym ``0x01000000 + codepoint``. Control characters and lone surrogates
    have no keysym and raise ``ValueError``.
    """
    if not isinstance(character, str) or len(character) != 1:
        raise ValueError(f"expected one character, got {character!r}")
    if unicodedata.category(character) in ("Cc", "Cs"):
        raise ValueError(f"no keysym types the character {character!r}")
    codepoint = ord(character)
    if 0x20 <= codepoint <= 0x7E or 0xA0 <= codepoint <= 0xFF:
        return codepoint
    return UNICODE_KEYSYM_OFFSET + codepoint


def key_name(key: str) -> str | None:
    """X keysym name for a pyautogui key name; ``None`` for a single character.

    Names are case-insensitive; an unknown multi-character name raises.
    """
    if not isinstance(key, str) or not key:
        raise ValueError(f"key must be a non-empty str, got {key!r}")
    name = _KEY_NAMES.get(key.lower())
    if name is None and len(key) > 1:
        raise ValueError(f"unknown key name {key!r}")
    return name


def button_number(button: str) -> int:
    number = _BUTTONS.get(button)
    if number is None:
        raise ValueError(f"button must be one of {sorted(_BUTTONS)}, got {button!r}")
    return number


def wheel_buttons(clicks: int, *, horizontal: bool = False) -> list[int]:
    """One wheel button per notch; positive is up (or right)."""
    if isinstance(clicks, bool) or not isinstance(clicks, int):
        raise TypeError(f"clicks must be an int, got {clicks!r}")
    if horizontal:
        button = WHEEL_RIGHT if clicks > 0 else WHEEL_LEFT
    else:
        button = WHEEL_UP if clicks > 0 else WHEEL_DOWN
    return [button] * abs(clicks)


class KeyboardMapping:
    """A snapshot of the server's core keyboard mapping.

    ``rows[keycode]`` lists the keysyms of one key; index 0 is the plain level
    and index 1 the Shift level of the first group.
    """

    def __init__(self, first: int, per_keycode: int, keysyms: Sequence[int]) -> None:
        if per_keycode < 1:
            raise ValueError(f"keysyms per keycode must be positive, got {per_keycode}")
        self.first = first
        self.per_keycode = per_keycode
        count = len(keysyms) // per_keycode
        self.rows: dict[int, list[int]] = {
            first + index: list(
                keysyms[index * per_keycode : (index + 1) * per_keycode]
            )
            for index in range(count)
        }

    def find(
        self, keysym: int, exclude: Collection[int] = ()
    ) -> tuple[int, int] | None:
        """``(keycode, level)`` of the lowest key with ``keysym`` on level 0 or 1.

        A plain-level match on any key wins over a Shift-level one. Keycodes
        in ``exclude`` (temporarily remapped ones) are skipped.
        """
        for level in (0, 1):
            for keycode, row in sorted(self.rows.items()):
                if keycode in exclude:
                    continue
                if len(row) > level and row[level] == keysym:
                    return keycode, level
        return None

    def spare_keycodes(self) -> list[int]:
        """Keycodes without any symbol, highest first (away from real keys)."""
        return sorted(
            (code for code, row in self.rows.items() if not any(row)), reverse=True
        )


class Stroke(NamedTuple):
    """How one keysym is typed: a key (with Shift or not), or a remap."""

    keysym: int
    keycode: int
    shift: int | None
    remap: bool


def plan_stroke(
    mapping: KeyboardMapping,
    keysym: int,
    shift: int | None,
    exclude: Collection[int] = (),
) -> Stroke:
    """Plain key for a level-0 keysym, Shift + key for level 1, else a remap.

    ``shift`` is the Shift key's keycode (``None`` when the mapping has none,
    so a level-1 keysym is remapped instead). Keycodes in ``exclude`` are
    not keys of the layout (see ``KeyboardMapping.find``).
    """
    found = mapping.find(keysym, exclude)
    if found is not None and found[1] == 0:
        return Stroke(keysym, found[0], None, False)
    if found is not None and shift is not None:
        return Stroke(keysym, found[0], shift, False)
    return Stroke(keysym, 0, None, True)


# -- libX11 / libXtst ------------------------------------------------------------


class XFunction(Protocol):
    """A foreign function (``ctypes`` function pointer or a test double)."""

    argtypes: Any
    restype: Any

    def __call__(self, *args: Any) -> Any: ...


class XLibrary(Protocol):
    """A loaded shared library: exports by name (``ctypes.CDLL`` or a fake)."""

    def __getitem__(self, name: str, /) -> XFunction: ...


KeySym = ctypes.c_ulong
XWindow = ctypes.c_ulong
KeyCode = ctypes.c_ubyte
_DISPLAY = ctypes.c_void_p
_INT_P = ctypes.POINTER(ctypes.c_int)
_UINT_P = ctypes.POINTER(ctypes.c_uint)
_WINDOW_P = ctypes.POINTER(XWindow)
_KEYSYM_P = ctypes.POINTER(KeySym)

# name: (library, restype, argtypes)
_PROTOTYPES: dict[str, tuple[str, Any, list[Any]]] = {
    "XInitThreads": ("x11", ctypes.c_int, []),
    "XOpenDisplay": ("x11", _DISPLAY, [ctypes.c_char_p]),
    "XCloseDisplay": ("x11", ctypes.c_int, [_DISPLAY]),
    "XDefaultScreen": ("x11", ctypes.c_int, [_DISPLAY]),
    "XRootWindow": ("x11", XWindow, [_DISPLAY, ctypes.c_int]),
    "XDisplayWidth": ("x11", ctypes.c_int, [_DISPLAY, ctypes.c_int]),
    "XDisplayHeight": ("x11", ctypes.c_int, [_DISPLAY, ctypes.c_int]),
    "XQueryPointer": (
        "x11",
        ctypes.c_int,
        [_DISPLAY, XWindow, _WINDOW_P, _WINDOW_P]
        + [_INT_P, _INT_P, _INT_P, _INT_P, _UINT_P],
    ),
    "XStringToKeysym": ("x11", KeySym, [ctypes.c_char_p]),
    "XDisplayKeycodes": ("x11", ctypes.c_int, [_DISPLAY, _INT_P, _INT_P]),
    "XGetKeyboardMapping": (
        "x11",
        _KEYSYM_P,
        [_DISPLAY, KeyCode, ctypes.c_int, _INT_P],
    ),
    "XChangeKeyboardMapping": (
        "x11",
        ctypes.c_int,
        [_DISPLAY, ctypes.c_int, ctypes.c_int, _KEYSYM_P, ctypes.c_int],
    ),
    "XFree": ("x11", ctypes.c_int, [ctypes.c_void_p]),
    "XFlush": ("x11", ctypes.c_int, [_DISPLAY]),
    "XSync": ("x11", ctypes.c_int, [_DISPLAY, ctypes.c_int]),
    "XTestQueryExtension": (
        "xtst",
        ctypes.c_int,
        [_DISPLAY, _INT_P, _INT_P, _INT_P, _INT_P],
    ),
    "XTestFakeMotionEvent": (
        "xtst",
        ctypes.c_int,
        [_DISPLAY, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong],
    ),
    "XTestFakeButtonEvent": (
        "xtst",
        ctypes.c_int,
        [_DISPLAY, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong],
    ),
    "XTestFakeKeyEvent": (
        "xtst",
        ctypes.c_int,
        [_DISPLAY, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong],
    ),
}


def _load(name: str, package: str) -> XLibrary:
    try:
        return ctypes.CDLL(name)
    except OSError as exc:
        raise NativeInputError(
            f"{name} could not be loaded ({exc}); X11 input needs the {package} package"
        ) from exc


class Xlib:
    """One X display connection through private libX11/libXtst handles.

    Prototypes are set on this object's own function pointers, never on a
    shared library. ``lock`` serialises every call; hold it across a
    sequence that must not interleave with another thread's input.
    """

    def __init__(
        self, x11: XLibrary | None = None, xtst: XLibrary | None = None
    ) -> None:
        self._libraries: dict[str, XLibrary] = {
            "x11": x11 if x11 is not None else _load(LIBX11, "libx11-6"),
            "xtst": xtst if xtst is not None else _load(LIBXTST, "libxtst6"),
        }
        self._functions: dict[str, XFunction] = {}
        self.lock = threading.RLock()
        # The connection's temporary keycodes (``remap_pool``), shared by
        # every backend on it: the mapping is server-wide.
        self.remaps: RemapPool | None = None
        # Must precede every other Xlib call in the process.
        self._call("XInitThreads")
        display = self._call("XOpenDisplay", None)
        if not display:
            raise NativeInputError(
                f"cannot open the X display {os.environ.get('DISPLAY')!r}; "
                "X11 input needs a running X server (or XWayland) that this "
                "user may connect to"
            )
        self._display: int = display
        codes = [ctypes.c_int() for _ in range(4)]
        if not self._call(
            "XTestQueryExtension", display, *(ctypes.pointer(code) for code in codes)
        ):
            self._call("XCloseDisplay", display)
            raise NativeInputError(
                "the X server has no XTEST extension; X11 input cannot be sent"
            )
        self._screen = int(self._call("XDefaultScreen", display))
        self._root = int(self._call("XRootWindow", display, self._screen))

    def _function(self, name: str) -> XFunction:
        function = self._functions.get(name)
        if function is None:
            library, restype, argtypes = _PROTOTYPES[name]
            function = self._libraries[library][name]
            function.restype = restype
            function.argtypes = argtypes
            self._functions[name] = function
        return function

    def _call(self, name: str, *args: Any) -> Any:
        with self.lock:
            return self._function(name)(*args)

    # -- reading ------------------------------------------------------------

    def pointer(self) -> tuple[int, int]:
        """Pointer position in root-window pixels."""
        root, child = XWindow(), XWindow()
        root_x, root_y, win_x, win_y = (ctypes.c_int() for _ in range(4))
        mask = ctypes.c_uint()
        # False only means the pointer is on another screen; the root
        # coordinates are still those of the screen it is on.
        self._call(
            "XQueryPointer",
            self._display,
            self._root,
            ctypes.pointer(root),
            ctypes.pointer(child),
            ctypes.pointer(root_x),
            ctypes.pointer(root_y),
            ctypes.pointer(win_x),
            ctypes.pointer(win_y),
            ctypes.pointer(mask),
        )
        return int(root_x.value), int(root_y.value)

    def screen_size(self) -> tuple[int, int]:
        return (
            int(self._call("XDisplayWidth", self._display, self._screen)),
            int(self._call("XDisplayHeight", self._display, self._screen)),
        )

    def keysym(self, name: str) -> int:
        """``XStringToKeysym``; ``NO_SYMBOL`` (0) for an unknown name."""
        return int(self._call("XStringToKeysym", name.encode("ascii")) or 0)

    def keyboard_mapping(self) -> KeyboardMapping:
        """The server's current mapping for every keycode (a round trip)."""
        with self.lock:
            low, high = ctypes.c_int(), ctypes.c_int()
            self._call(
                "XDisplayKeycodes",
                self._display,
                ctypes.pointer(low),
                ctypes.pointer(high),
            )
            first, count = int(low.value), int(high.value) - int(low.value) + 1
            per = ctypes.c_int()
            table = self._call(
                "XGetKeyboardMapping",
                self._display,
                first,
                count,
                ctypes.pointer(per),
            )
            if not table:
                raise NativeInputError("XGetKeyboardMapping returned no mapping")
            try:
                keysyms = [int(table[index]) for index in range(count * per.value)]
            finally:
                self._call("XFree", table)
            return KeyboardMapping(first, int(per.value), keysyms)

    # -- writing ------------------------------------------------------------

    def change_mapping(self, keycode: int, keysyms: Sequence[int]) -> None:
        """Map one keycode to ``keysyms`` (all its levels)."""
        array = (KeySym * len(keysyms))(*keysyms)
        self._call(
            "XChangeKeyboardMapping", self._display, keycode, len(keysyms), array, 1
        )

    def motion(self, x: int, y: int) -> None:
        self._call(
            "XTestFakeMotionEvent", self._display, CURRENT_SCREEN, x, y, CURRENT_TIME
        )

    def button(self, number: int, *, press: bool) -> None:
        self._call(
            "XTestFakeButtonEvent", self._display, number, int(press), CURRENT_TIME
        )

    def key(self, keycode: int, *, press: bool) -> None:
        self._call(
            "XTestFakeKeyEvent", self._display, keycode, int(press), CURRENT_TIME
        )

    def flush(self) -> None:
        self._call("XFlush", self._display)

    def sync(self) -> None:
        """Flush and wait until the server has processed every request."""
        self._call("XSync", self._display, 0)

    @property
    def is_open(self) -> bool:
        return bool(self._display)

    def close(self) -> None:
        with self.lock:
            if self._display:
                self._call("XCloseDisplay", self._display)
                self._display = 0


_SHARED_LOCK = threading.Lock()
_SHARED: Xlib | None = None


def shared_xlib() -> Xlib:
    """The process-wide display connection, opened on first use."""
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = Xlib()
        return _SHARED


# -- temporary keycodes ----------------------------------------------------------


class Cancellable(Protocol):
    def cancel(self) -> None: ...


TimerFactory = Callable[[float, Callable[[], None]], Cancellable]


def start_timer(delay: float, callback: Callable[[], None]) -> Cancellable:
    """``callback`` on a daemon thread after ``delay`` s (``threading.Timer``)."""
    timer = threading.Timer(delay, callback)
    timer.daemon = True
    timer.start()
    return timer


# Exit-time restore: every pool that ever remapped a keycode, hook added once.
_EXIT_LOCK = threading.Lock()
_EXIT_HOOK_REGISTERED = False
_EXIT_POOLS: weakref.WeakSet[RemapPool] = weakref.WeakSet()
# How long the exit hook waits for a connection another thread holds (s).
_EXIT_LOCK_TIMEOUT = 1.0


def release_all_remaps() -> None:
    """Restore every pool's keycodes; the atexit hook. Never raises."""
    with _EXIT_LOCK:
        pools = list(_EXIT_POOLS)
    for pool in pools:
        pool.release_at_exit()


def _track_for_exit(pool: RemapPool) -> None:
    global _EXIT_HOOK_REGISTERED
    with _EXIT_LOCK:
        _EXIT_POOLS.add(pool)
        if not _EXIT_HOOK_REGISTERED:
            atexit.register(release_all_remaps)
            _EXIT_HOOK_REGISTERED = True


def _holds(mapping: KeyboardMapping, keycode: int, keysym: int) -> bool:
    """True when ``keycode``'s plain level is ``keysym`` in ``mapping``."""
    row = mapping.rows.get(keycode)
    return bool(row) and row[0] == keysym


class RemapPool:
    """Spare keycodes temporarily carrying keysyms the layout lacks.

    Every method expects the caller to hold ``xlib.lock`` or takes it. A
    keycode keeps its symbol until it is the least recently used one and its
    last use is ``REMAP_REUSE_AGE`` old, or until the pool is released: after
    ``REMAP_QUIET`` without a remapped key, by ``release``, or at exit. A
    keycode whose row someone else changed leaves the pool unwritten.
    """

    def __init__(
        self,
        xlib: Xlib,
        *,
        sleep: Callable[[float], None],
        clock: Callable[[], float],
        timer: TimerFactory,
    ) -> None:
        self._xlib = xlib
        self._sleep = sleep
        self._clock = clock
        self._timer_factory = timer
        # keycode -> keysym, least recently used first.
        self._mapped: dict[int, int] = {}
        self._last_use: dict[int, float] = {}
        self._original: dict[int, list[int]] = {}
        self._timer: Cancellable | None = None
        self._generation = 0

    def keycodes(self) -> frozenset[int]:
        """The keycodes carrying a temporary symbol now."""
        return frozenset(self._mapped)

    def prune(self, mapping: KeyboardMapping) -> None:
        """Forget, without writing, every keycode whose row in ``mapping``
        no longer holds the pooled keysym (setxkbmap, xmodmap or another
        client changed it: the row is theirs now)."""
        for keycode, keysym in list(self._mapped.items()):
            if not _holds(mapping, keycode, keysym):
                self._forget(keycode)

    def can_map(self, mapping: KeyboardMapping) -> bool:
        """True when a keysym can get a keycode (a pooled or a spare one)."""
        self.prune(mapping)
        return bool(self._mapped) or bool(self._free(mapping, ()))

    def reuse_wait(self, keysym: int, mapping: KeyboardMapping) -> float:
        """Seconds to wait before ``keycode_for`` may recycle a keycode.

        Zero when ``keysym`` is pooled, a spare keycode is free, or the least
        recently used keycode is ``REMAP_REUSE_AGE`` old.
        """
        self.prune(mapping)
        if keysym in self._mapped.values() or not self._mapped:
            return 0.0
        if self._free(mapping, ()) and len(self._mapped) < REMAP_POOL_SIZE:
            return 0.0
        oldest = next(iter(self._mapped))
        return max(0.0, REMAP_REUSE_AGE - (self._clock() - self._last_use[oldest]))

    def wait(self, seconds: float) -> None:
        """Sleep with ``xlib.lock`` still held (see ``X11Backend.write``)."""
        self._xlib.flush()
        self._sleep(seconds)

    def hold_release(self) -> None:
        """Cancel the pending restore for a write in progress.

        A timer that already fired and waits for the lock finds its
        generation stale and restores nothing; ``schedule_release`` re-arms.
        """
        with self._xlib.lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._generation += 1

    def keycode_for(
        self, keysym: int, mapping: KeyboardMapping
    ) -> tuple[int, KeyboardMapping]:
        """The keycode carrying ``keysym`` and the newest mapping snapshot.

        ``mapping`` is a snapshot no older than the caller's last remap. A
        pooled keycode counts only while ``mapping`` shows it still holds
        ``keysym``. A newly mapped keycode is read back after the request
        (another client may have changed it meanwhile, then the next spare
        is tried) and given ``REMAP_SETTLE`` before it is returned.
        """
        self.prune(mapping)
        for keycode, mapped in self._mapped.items():
            if mapped == keysym:
                self._touch(keycode)
                return keycode, mapping
        tried: set[int] = set()
        while True:
            # A recycled keycode is only ever the first candidate: its age
            # was awaited (reuse_wait), a further one's was not.
            keycode = self._take(mapping, tried, recycle=not tried)
            tried.add(keycode)
            # Pooled before the request, so a failed one is still restored;
            # NO_SYMBOL matches no keysym a caller asks for.
            self._mapped[keycode] = NO_SYMBOL
            self._touch(keycode)
            _track_for_exit(self)
            # Both levels: Shift or Caps Lock cannot change it.
            self._xlib.change_mapping(keycode, [keysym, keysym])
            self._xlib.sync()
            mapping = self._xlib.keyboard_mapping()
            if _holds(mapping, keycode, keysym):
                self._mapped[keycode] = keysym
                self._sleep(REMAP_SETTLE)
                return keycode, mapping
            # Another client wrote this row after us: leave it to them.
            self._forget(keycode)

    def schedule_release(self) -> None:
        """(Re)start the quiet period after which every keycode is restored.

        Nothing to restore (an empty pool): no timer.
        """
        with self._xlib.lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._generation += 1
            if not self._mapped:
                return
            generation = self._generation
            self._timer = self._timer_factory(
                REMAP_QUIET, lambda: self._expire(generation)
            )

    def release(self) -> None:
        """Give every pooled keycode its original row back now."""
        with self._xlib.lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._generation += 1
            if not self._mapped:
                return
            if not self._xlib.is_open:
                self._forget_all()
                return
            # Only rows that still hold the pooled keysym are ours to restore.
            self.prune(self._xlib.keyboard_mapping())
            for keycode in sorted(self._mapped, reverse=True):
                self._xlib.change_mapping(keycode, self._original[keycode])
                self._forget(keycode)
            self._xlib.sync()

    def release_at_exit(self) -> None:
        """``release`` for the atexit hook: bounded wait, never raises."""
        lock = self._xlib.lock
        if not lock.acquire(timeout=_EXIT_LOCK_TIMEOUT):
            logger.warning("X11 keycodes left remapped: the display stayed busy")
            return
        try:
            self.release()
        except Exception:
            # Interpreter exit: report and go on with the remaining pools.
            logger.warning("restoring remapped X11 keycodes failed", exc_info=True)
        finally:
            lock.release()

    # -- internals ----------------------------------------------------------

    def _free(self, mapping: KeyboardMapping, exclude: Collection[int]) -> list[int]:
        return [
            code
            for code in mapping.spare_keycodes()
            if code not in self._mapped and code not in exclude
        ]

    def _take(
        self, mapping: KeyboardMapping, exclude: Collection[int], *, recycle: bool
    ) -> int:
        """A new spare keycode while the pool has room, else (``recycle``)
        the least recently used one."""
        free = self._free(mapping, exclude)
        if free and len(self._mapped) < REMAP_POOL_SIZE:
            keycode = free[0]
            self._original[keycode] = list(mapping.rows[keycode])
            return keycode
        if recycle and self._mapped:
            return next(iter(self._mapped))
        raise NativeInputError(
            "the keyboard mapping has no spare keycode to type a "
            "character the layout lacks"
        )

    def _touch(self, keycode: int) -> None:
        self._mapped[keycode] = self._mapped.pop(keycode)
        self._last_use[keycode] = self._clock()

    def _forget(self, keycode: int) -> None:
        del self._mapped[keycode]
        self._last_use.pop(keycode, None)
        self._original.pop(keycode, None)

    def _forget_all(self) -> None:
        self._mapped.clear()
        self._last_use.clear()
        self._original.clear()

    def _expire(self, generation: int) -> None:
        with self._xlib.lock:
            if generation != self._generation:
                return
            self._timer = None
            self.release()


def remap_pool(
    xlib: Xlib,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timer: TimerFactory = start_timer,
) -> RemapPool:
    """The connection's pool, created with these callables on first use."""
    with xlib.lock:
        if xlib.remaps is None:
            xlib.remaps = RemapPool(xlib, sleep=sleep, clock=clock, timer=timer)
        return xlib.remaps


# -- backend -------------------------------------------------------------------


class RawReleases:
    """``platformModule`` of the backend: input that skips the fail-safe.

    A release must happen even with the cursor in a fail-safe corner, or the
    button / key would stay down. Coordinates are ignored.
    """

    def __init__(self, backend: X11Backend) -> None:
        self._backend = backend

    def _position(self) -> tuple[int, int]:
        return self._backend.position()

    def _mouseUp(self, x: int, y: int, button: str) -> None:
        self._backend.release_button(button)

    def _keyDown(self, key: str) -> None:
        self._backend.raw_key_down(key)

    def _keyUp(self, key: str) -> None:
        self._backend.release_key(key)


class X11Backend:
    """pyautogui-compatible input backend (``emunium._native.InputBackend``).

    Every public action checks the fail-safe first (``EMUNIUM_FAILSAFE``);
    the releases and the raw key-down of ``platformModule`` skip it.
    ``_pause`` is accepted for compatibility and never sleeps.
    """

    # write types a character the layout lacks on a temporarily mapped keycode.
    supports_unicode_write: bool = True

    def __init__(
        self,
        xlib: Xlib | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        timer: TimerFactory = start_timer,
    ) -> None:
        """``sleep``, ``clock`` and ``timer`` drive the connection's
        ``RemapPool`` when this backend is the first on ``xlib``."""
        self._xlib = xlib if xlib is not None else shared_xlib()
        self._environ = environ
        self._remaps = remap_pool(self._xlib, sleep=sleep, clock=clock, timer=timer)
        self.platformModule = RawReleases(self)

    def release_remaps(self) -> None:
        """Give temporarily remapped keycodes their empty rows back now.

        Waits for a ``write``/``press`` in progress on this connection to
        finish (the connection lock), then restores every row that still
        holds its pooled keysym; a row someone else changed is left alone.
        Otherwise this happens ``REMAP_QUIET`` after the last remapped key,
        or at interpreter exit. The backend stays usable: the next character
        the layout lacks maps a keycode again.
        """
        self._remaps.release()

    def close(self) -> None:
        """``release_remaps``; the shared display connection stays open."""
        self.release_remaps()

    # -- reading ------------------------------------------------------------

    def position(self) -> Point:
        x, y = self._xlib.pointer()
        return Point(x, y)

    def size(self) -> Size:
        """The root window (every monitor of the X screen) in px."""
        width, height = self._xlib.screen_size()
        return Size(width, height)

    def virtual_screen(self) -> VirtualScreen:
        width, height = self._xlib.screen_size()
        return VirtualScreen(0, 0, width, height)

    # -- mouse --------------------------------------------------------------

    def moveTo(self, x: float, y: float, _pause: bool = False) -> None:
        with self._xlib.lock:
            self._failsafe()
            self._xlib.motion(round(x), round(y))
            self._xlib.flush()

    def mouseDown(
        self,
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
        _pause: bool = False,
    ) -> None:
        self._button(button_number(button), x, y, press=True)

    def mouseUp(
        self,
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
        _pause: bool = False,
    ) -> None:
        self._button(button_number(button), x, y, press=False)

    def release_button(self, button: str) -> None:
        """Button up at the current position; no fail-safe."""
        number = button_number(button)
        with self._xlib.lock:
            self._xlib.button(number, press=False)
            self._xlib.flush()

    def scroll(
        self,
        clicks: int,
        x: int | None = None,
        y: int | None = None,
        _pause: bool = False,
    ) -> None:
        """Vertical wheel, ``clicks`` notches; positive scrolls up."""
        self._wheel(wheel_buttons(clicks), x, y)

    def hscroll(
        self,
        clicks: int,
        x: int | None = None,
        y: int | None = None,
        _pause: bool = False,
    ) -> None:
        """Horizontal wheel, ``clicks`` notches; positive scrolls right."""
        self._wheel(wheel_buttons(clicks, horizontal=True), x, y)

    # -- keyboard -----------------------------------------------------------

    def keyDown(self, key: str, _pause: bool = False) -> None:
        keysym = self._key_keysym(key)
        with self._xlib.lock:
            keycode = self._held_keycode(key, keysym)
            self._failsafe()
            self._xlib.key(keycode, press=True)
            self._xlib.flush()

    def keyUp(self, key: str, _pause: bool = False) -> None:
        keysym = self._key_keysym(key)
        with self._xlib.lock:
            keycode = self._held_keycode(key, keysym)
            self._failsafe()
            self._xlib.key(keycode, press=False)
            self._xlib.flush()

    def raw_key_down(self, key: str) -> None:
        """Key down for a cancel path; no fail-safe."""
        keysym = self._key_keysym(key)
        with self._xlib.lock:
            self._xlib.key(self._held_keycode(key, keysym), press=True)
            self._xlib.flush()

    def release_key(self, key: str) -> None:
        """Key up; no fail-safe."""
        keysym = self._key_keysym(key)
        with self._xlib.lock:
            self._xlib.key(self._held_keycode(key, keysym), press=False)
            self._xlib.flush()

    def press(
        self, keys: str | Sequence[str], presses: int = 1, _pause: bool = False
    ) -> None:
        """Press and release each key in order, ``presses`` times.

        A character on a key's Shift level is wrapped in Shift; one the
        layout lacks is typed on a temporarily mapped spare keycode.
        """
        names = [keys] if isinstance(keys, str) else list(keys)
        if isinstance(presses, bool) or not isinstance(presses, int):
            raise TypeError(f"presses must be an int, got {presses!r}")
        if presses < 0:
            raise ValueError(f"presses must not be negative, got {presses}")
        keysyms = [self._key_keysym(name) for name in names]
        self._tap(keysyms * presses)

    def write(self, message: str, _pause: bool = False) -> None:
        """Type ``message``; any character, whatever the keyboard layout.

        One call is atomic with respect to other emunium input on this
        connection: it holds the connection lock throughout, including any
        wait for a pooled keycode to age (at most ``REMAP_REUSE_AGE`` per
        character, with the fail-safe checked before and after it).
        """
        if not isinstance(message, str):
            raise TypeError(f"message must be str, got {type(message).__name__}")
        keysyms = [
            self._named_keysym(_KEY_NAMES[stroke])
            if stroke in ("\n", "\t")
            else char_keysym(stroke)
            for stroke in split_strokes(message)
        ]
        self._tap(keysyms)

    # -- internals ----------------------------------------------------------

    def _failsafe(self) -> None:
        check_failsafe(self.position, self.size, self._environ)

    def _move_if_given(self, x: int | None, y: int | None) -> None:
        if x is None and y is None:
            return
        current = self.position()
        self._xlib.motion(
            current.x if x is None else round(x), current.y if y is None else round(y)
        )

    def _button(
        self, number: int, x: int | None, y: int | None, *, press: bool
    ) -> None:
        with self._xlib.lock:
            self._failsafe()
            self._move_if_given(x, y)
            self._xlib.button(number, press=press)
            self._xlib.flush()

    def _wheel(self, buttons: list[int], x: int | None, y: int | None) -> None:
        with self._xlib.lock:
            self._failsafe()
            self._move_if_given(x, y)
            for number in buttons:
                self._xlib.button(number, press=True)
                self._xlib.button(number, press=False)
            self._xlib.flush()

    def _named_keysym(self, name: str) -> int:
        keysym = self._xlib.keysym(name)
        if keysym == NO_SYMBOL:
            raise NativeInputError(f"the X server knows no keysym {name!r}")
        return keysym

    def _key_keysym(self, key: str) -> int:
        name = key_name(key)
        return char_keysym(key) if name is None else self._named_keysym(name)

    def _held_keycode(self, key: str, keysym: int) -> int:
        """The key that carries ``keysym`` (either level), for down/up.

        A held key is never remapped, nor held on a pooled keycode: the
        mapping would have to outlive the hold. ``"V"`` is the V key, without
        Shift.
        """
        mapping = self._xlib.keyboard_mapping()
        self._remaps.prune(mapping)
        found = mapping.find(keysym, self._remaps.keycodes())
        if found is None:
            raise NativeInputError(
                f"no key carries {key!r} in the current keyboard mapping"
            )
        return found[0]

    def _tap(self, keysyms: Sequence[int]) -> None:
        """Press and release each keysym in order (see ``press``).

        The lock is held throughout (see ``write``) and a pending restore is
        held off until the last key, so no pooled keycode changes mid-call.
        The fail-safe is checked again before every remapped keysym and after
        each wait for a keycode to age (``RemapPool.reuse_wait``, at most
        ``REMAP_REUSE_AGE``), after which the mapping is read again.
        """
        if not keysyms:
            return
        xlib = self._xlib
        remaps = self._remaps
        with xlib.lock:
            mapping = xlib.keyboard_mapping()
            remaps.prune(mapping)
            pooled = remaps.keycodes()
            shift_found = mapping.find(self._named_keysym(SHIFT_KEYSYM_NAME), pooled)
            shift = None if shift_found is None else shift_found[0]
            plan = [plan_stroke(mapping, keysym, shift, pooled) for keysym in keysyms]
            if any(stroke.remap for stroke in plan) and not remaps.can_map(mapping):
                raise NativeInputError(
                    "the keyboard mapping has no spare keycode to type a "
                    "character the layout lacks"
                )
            self._failsafe()
            remapping = any(stroke.remap for stroke in plan)
            if remapping:
                remaps.hold_release()
            try:
                for stroke in plan:
                    if not stroke.remap:
                        self._stroke(stroke.keycode, stroke.shift)
                        continue
                    self._failsafe()
                    wait = remaps.reuse_wait(stroke.keysym, mapping)
                    while wait > 0:
                        remaps.wait(wait)
                        self._failsafe()
                        mapping = xlib.keyboard_mapping()
                        wait = remaps.reuse_wait(stroke.keysym, mapping)
                    keycode, mapping = remaps.keycode_for(stroke.keysym, mapping)
                    self._stroke(keycode, None)
                xlib.flush()
            finally:
                if remapping:
                    remaps.schedule_release()

    def _stroke(self, keycode: int, shift: int | None) -> None:
        if shift is not None:
            self._xlib.key(shift, press=True)
        self._xlib.key(keycode, press=True)
        self._xlib.key(keycode, press=False)
        if shift is not None:
            self._xlib.key(shift, press=False)
