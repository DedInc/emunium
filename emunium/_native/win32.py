"""Windows input through user32, ctypes only.

``SetCursorPos`` / ``GetCursorPos`` move and read the pointer; ``SendInput``
carries buttons, the wheel and keys. ``emunium._screen.input_backend`` makes the
process per-monitor-v2 DPI aware before building this backend, so coordinates
are physical px and negative on monitors left of or above the primary one.

The INPUT records and the key table are built by pure functions, separate from
the user32 calls, so they can be checked without sending input. Every
``SendInput`` goes through ``InputSender``: UIPI preflight, keystroke-safe
chunking and the delivery check live in one place.
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, NamedTuple, Protocol, TypeAlias

from emunium._native import check_failsafe
from emunium.errors import NativeInputError

# Fixed-width Win32 types: the record layout is the same on every host, so the
# pure builders also run (and are tested) off Windows.
DWORD = ctypes.c_uint32
LONG = ctypes.c_int32
WORD = ctypes.c_uint16
ULONG_PTR = ctypes.c_size_t
BOOL = ctypes.c_int
UINT = ctypes.c_uint
HANDLE = ctypes.c_void_p

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
WHEEL_DELTA = 120

SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

MAPVK_VK_TO_VSC = 0

VK_RETURN = 0x0D
VK_TAB = 0x09
VK_SHIFT = 0x10

# VkKeyScanW shift-state bits (high byte of the result).
SHIFT_STATE_SHIFT = 0x01

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_INTEGRITY_LEVEL = 25  # TOKEN_INFORMATION_CLASS.TokenIntegrityLevel

UIPI_MESSAGE = (
    "the foreground window runs elevated; Windows blocks input from "
    "non-elevated processes (UIPI) — run Python as administrator or "
    "un-elevate the target"
)

# SendInput batch size for text; batches are cut only between keystrokes.
KEY_CHUNK = 64

KeyEvent = tuple[int, int, int]  # (virtual key, scan code / UTF-16 unit, flags)


class POINT(ctypes.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", LONG),
        ("dy", LONG),
        ("mouseData", DWORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", WORD),
        ("wScan", WORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", DWORD), ("wParamL", WORD), ("wParamH", WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    """40 bytes on x64, 28 on x86."""

    _anonymous_ = ("u",)
    _fields_ = [("type", DWORD), ("u", _INPUTUNION)]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", DWORD)]


class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]


# -- text ----------------------------------------------------------------------


def split_strokes(text: str) -> list[str]:
    """Split text into keystrokes; ``\\r\\n`` and ``\\r`` are one Enter."""
    strokes: list[str] = []
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


def _utf16_units(text: str) -> list[int]:
    encoded = text.encode("utf-16-le")
    return [
        int.from_bytes(encoded[offset : offset + 2], "little")
        for offset in range(0, len(encoded), 2)
    ]


def stroke_events(stroke: str) -> list[KeyEvent]:
    """SendInput keyboard events for one keystroke (layout independent)."""
    if stroke == "\n":
        return [(VK_RETURN, 0, 0), (VK_RETURN, 0, KEYEVENTF_KEYUP)]
    if stroke == "\t":
        return [(VK_TAB, 0, 0), (VK_TAB, 0, KEYEVENTF_KEYUP)]
    events: list[KeyEvent] = []
    for unit in _utf16_units(stroke):
        events.append((0, unit, KEYEVENTF_UNICODE))
        events.append((0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    return events


def keyboard_events(text: str) -> list[KeyEvent]:
    events: list[KeyEvent] = []
    for stroke in split_strokes(text):
        events.extend(stroke_events(stroke))
    return events


def stroke_groups(events: Sequence[KeyEvent]) -> list[list[KeyEvent]]:
    """Group flat events into keystrokes; a surrogate pair stays one group."""
    groups: list[list[KeyEvent]] = []
    current: list[KeyEvent] = []
    for event in events:
        current.append(event)
        _, scan, flags = event
        released = bool(flags & KEYEVENTF_KEYUP)
        high_surrogate = bool(flags & KEYEVENTF_UNICODE) and 0xD800 <= scan <= 0xDBFF
        if released and not high_surrogate:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def chunk_events(
    events: Sequence[KeyEvent], limit: int = KEY_CHUNK
) -> list[list[KeyEvent]]:
    """Chunks of at most ``limit`` events, cut only between keystrokes."""
    chunks: list[list[KeyEvent]] = []
    current: list[KeyEvent] = []
    for stroke in stroke_groups(events):
        if current and len(current) + len(stroke) > limit:
            chunks.append(current)
            current = []
        current.extend(stroke)
    if current:
        chunks.append(current)
    return chunks


# -- key names -----------------------------------------------------------------

_KEY_CODES: dict[str, int] = {
    **{chr(code).lower(): code for code in range(0x41, 0x5B)},  # a-z
    **{chr(code): code for code in range(0x30, 0x3A)},  # 0-9
    **{f"f{number}": 0x6F + number for number in range(1, 25)},  # F1-F24
    **{f"num{digit}": 0x60 + digit for digit in range(10)},  # keypad 0-9
    "enter": 0x0D,
    "return": 0x0D,
    "\n": 0x0D,
    "\r": 0x0D,
    "tab": 0x09,
    "\t": 0x09,
    "space": 0x20,
    " ": 0x20,
    "backspace": 0x08,
    "escape": 0x1B,
    "esc": 0x1B,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "insert": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "capslock": 0x14,
    "numlock": 0x90,
    "scrolllock": 0x91,
    "printscreen": 0x2C,
    "prtsc": 0x2C,
    "pause": 0x13,
    "apps": 0x5D,
    "menu": 0x5D,
    "multiply": 0x6A,
    "add": 0x6B,
    "subtract": 0x6D,
    "decimal": 0x6E,
    "divide": 0x6F,
    "volumemute": 0xAD,
    "volumedown": 0xAE,
    "volumeup": 0xAF,
    "nexttrack": 0xB0,
    "prevtrack": 0xB1,
    "playpause": 0xB3,
    "shift": 0x10,
    "shiftleft": 0xA0,
    "lshift": 0xA0,
    "shiftright": 0xA1,
    "rshift": 0xA1,
    "ctrl": 0x11,
    "control": 0x11,
    "ctrlleft": 0xA2,
    "lctrl": 0xA2,
    "ctrlright": 0xA3,
    "rctrl": 0xA3,
    "alt": 0x12,
    "option": 0x12,
    "altleft": 0xA4,
    "lalt": 0xA4,
    "altright": 0xA5,
    "ralt": 0xA5,
    # There is no generic Windows-key code; the left one is what keyboards send.
    "win": 0x5B,
    "winleft": 0x5B,
    "lwin": 0x5B,
    "winright": 0x5C,
    "rwin": 0x5C,
    "command": 0x5B,
    "cmd": 0x5B,
}
# Keys on the extended (E0-prefixed) scan-code set. Without the flag an arrow
# arrives as its numeric-keypad twin when NumLock is off.
EXTENDED_KEYS = frozenset(
    {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}
    | {0xA3, 0xA5, 0x5B, 0x5C}
    | {0x6F, 0x90, 0x2C, 0x5D}  # divide, numlock, printscreen, apps
)
# Keys only a scan code tells apart: keypad Enter shares VK_RETURN with the
# main Enter and differs by the extended scan code E0 1C.
SCANCODE_KEYS: dict[str, int] = {"numpadenter": 0x1C}


def virtual_key(key: str) -> int | None:
    """Table virtual-key code for a key name; ``None`` when it has none.

    ``None`` means a single character outside the table (resolved through the
    keyboard layout, else sent as Unicode) or a scan-code key. Names are
    case-insensitive (``"V"`` is the V key, without Shift).
    """
    if not isinstance(key, str) or not key:
        raise ValueError(f"key must be a non-empty str, got {key!r}")
    name = key.lower()
    code = _KEY_CODES.get(name)
    if code is None and len(key) > 1 and name not in SCANCODE_KEYS:
        raise ValueError(f"unknown key name {key!r}")
    return code


def layout_key(scan_result: int) -> tuple[int, int] | None:
    """``(virtual key, shift state)`` from a ``VkKeyScanW`` result.

    ``None`` when the active layout has no key for the character (-1).
    """
    value = scan_result & 0xFFFF
    vk, shift_state = value & 0xFF, value >> 8
    if vk == 0xFF or shift_state == 0xFF:
        return None
    return vk, shift_state


def _vk_event(code: int, scan_codes: Mapping[int, int], release: int) -> KeyEvent:
    flags = release | (KEYEVENTF_EXTENDEDKEY if code in EXTENDED_KEYS else 0)
    return (code, scan_codes.get(code, 0) & 0xFFFF, flags)


def key_events(
    key: str,
    *,
    up: bool,
    scan_codes: Mapping[int, int],
    layout: tuple[int, int] | None = None,
) -> list[KeyEvent]:
    """Events for pressing (``up=False``) or releasing one key, ignoring Shift.

    ``scan_codes`` maps virtual keys to ``MapVirtualKeyW`` scan codes;
    ``layout`` is the ``VkKeyScanW`` key of a character outside the table.
    Without one, the character is sent as Unicode, like ``write``.
    """
    release = KEYEVENTF_KEYUP if up else 0
    scan = SCANCODE_KEYS.get(key.lower()) if isinstance(key, str) else None
    if scan is not None:
        return [(0, scan, KEYEVENTF_SCANCODE | KEYEVENTF_EXTENDEDKEY | release)]
    code = virtual_key(key)
    if code is None and layout is not None:
        code = layout[0]
    if code is None:
        return [(0, unit, KEYEVENTF_UNICODE | release) for unit in _utf16_units(key)]
    return [_vk_event(code, scan_codes, release)]


def press_events(
    key: str,
    *,
    scan_codes: Mapping[int, int],
    layout: tuple[int, int] | None = None,
) -> list[KeyEvent]:
    """Press and release one key, honouring the layout's Shift state.

    A character that needs Shift is wrapped in Shift; one that needs Ctrl/Alt
    (AltGr) is sent as Unicode instead, so the right character arrives.
    """
    if layout is not None and virtual_key(key) is None:
        code, shift_state = layout
        if shift_state & ~SHIFT_STATE_SHIFT:
            layout = None
        elif shift_state & SHIFT_STATE_SHIFT:
            return [
                _vk_event(VK_SHIFT, scan_codes, 0),
                _vk_event(code, scan_codes, 0),
                _vk_event(code, scan_codes, KEYEVENTF_KEYUP),
                _vk_event(VK_SHIFT, scan_codes, KEYEVENTF_KEYUP),
            ]
    return key_events(key, up=False, scan_codes=scan_codes, layout=layout) + (
        key_events(key, up=True, scan_codes=scan_codes, layout=layout)
    )


# -- INPUT records ---------------------------------------------------------------

_BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def button_flags(button: str, *, up: bool) -> int:
    flags = _BUTTON_FLAGS.get(button)
    if flags is None:
        raise ValueError(
            f"button must be one of {sorted(_BUTTON_FLAGS)}, got {button!r}"
        )
    return flags[1] if up else flags[0]


def keyboard_input(event: KeyEvent) -> INPUT:
    vk, scan, flags = event
    record = INPUT(type=INPUT_KEYBOARD)
    record.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return record


def mouse_input(flags: int, data: int = 0) -> INPUT:
    """A button or wheel record at the current cursor position (no move)."""
    record = INPUT(type=INPUT_MOUSE)
    # mouseData is a DWORD; a negative wheel delta travels as its two's complement.
    record.mi = MOUSEINPUT(
        dx=0, dy=0, mouseData=data & 0xFFFFFFFF, dwFlags=flags, time=0, dwExtraInfo=0
    )
    return record


def wheel_inputs(clicks: int, *, horizontal: bool = False) -> list[INPUT]:
    """One record per notch of ``WHEEL_DELTA``; positive is up (or right)."""
    if isinstance(clicks, bool) or not isinstance(clicks, int):
        raise TypeError(f"clicks must be an int, got {clicks!r}")
    flags = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    delta = WHEEL_DELTA if clicks > 0 else -WHEEL_DELTA
    return [mouse_input(flags, delta) for _ in range(abs(clicks))]


def input_array(records: Sequence[INPUT]) -> ctypes.Array[INPUT]:
    array = (INPUT * len(records))()
    for index, record in enumerate(records):
        array[index] = record
    return array


# -- sending -------------------------------------------------------------------


def last_error() -> int:
    """The calling thread's ctypes-saved Win32 error (``use_last_error`` DLLs)."""
    if sys.platform != "win32":
        return 0
    return ctypes.get_last_error()


def clear_last_error() -> None:
    """Zero the saved error so a short ``SendInput`` reports its own code."""
    if sys.platform == "win32":
        ctypes.set_last_error(0)


def uipi_blocked(own: int | None, foreground: int | None) -> bool:
    """True when the foreground process has a higher integrity level than ours.

    Unknown levels (a failed query) never block.
    """
    return own is not None and foreground is not None and foreground > own


class SendApi(Protocol):
    """What ``InputSender`` needs: delivery plus the UIPI integrity queries."""

    def send_input(self, records: Sequence[INPUT]) -> int: ...

    def last_error(self) -> int: ...

    def own_integrity(self) -> int | None: ...

    def foreground_integrity(self) -> int | None: ...


class InputSender:
    """The single ``SendInput`` path.

    Before input: the UIPI preflight (``SendInput`` drops input aimed at a
    higher-integrity window without reporting it). Text is cut into batches
    only between keystrokes; a short delivery raises ``NativeInputError``.
    """

    def __init__(self, api: SendApi) -> None:
        self._api = api
        self._own: int | None = None

    def _own_integrity(self) -> int | None:
        # Our token's level does not change; only a successful query is cached.
        if self._own is None:
            self._own = self._api.own_integrity()
        return self._own

    def preflight(self) -> None:
        foreground = self._api.foreground_integrity()
        if foreground is not None and uipi_blocked(self._own_integrity(), foreground):
            raise NativeInputError(UIPI_MESSAGE)

    def send(
        self, records: Sequence[INPUT], kind: str, *, preflight: bool = True
    ) -> None:
        if preflight:
            self.preflight()
        sent = self._api.send_input(records)
        if sent != len(records):
            raise NativeInputError(
                f"SendInput delivered {sent} of {len(records)} {kind} events "
                f"(error {self._api.last_error()}); input may be blocked by a "
                "higher-integrity window or the secure desktop"
            )

    def send_keys(self, events: Sequence[KeyEvent], *, preflight: bool = True) -> None:
        if preflight:
            self.preflight()
        for chunk in chunk_events(events):
            records = [keyboard_input(event) for event in chunk]
            self.send(records, "keyboard", preflight=False)


# -- user32 --------------------------------------------------------------------


if TYPE_CHECKING:
    # ctypes types of the prototypes below (pointer types are not subscriptable
    # at runtime).
    _CType: TypeAlias = (
        ctypes.c_int
        | ctypes.c_uint
        | ctypes.c_short
        | ctypes.c_uint32
        | ctypes.c_void_p
        | ctypes.c_wchar
        | ctypes._Pointer[POINT]
        | ctypes._Pointer[INPUT]
        | ctypes._Pointer[ctypes.c_uint32]
        | ctypes._Pointer[ctypes.c_void_p]
        | ctypes._Pointer[ctypes.c_ubyte]
    )


class User32Api(SendApi, Protocol):
    """The OS calls the backend makes (a fake stands in for it in tests)."""

    def cursor_pos(self) -> tuple[int, int]: ...

    def set_cursor_pos(self, x: int, y: int) -> bool: ...

    def system_metric(self, index: int) -> int: ...

    def map_virtual_key(self, code: int, map_type: int) -> int: ...

    def vk_key_scan(self, character: str) -> int: ...


# A failed integrity query (access denied, missing export, NULL pointer) means
# "unknown": the preflight then lets the input through.
_QUERY_ERRORS = (OSError, AttributeError, ValueError, ctypes.ArgumentError)


class User32:
    """user32/kernel32/advapi32 through private ``WinDLL`` objects.

    Every call resolves its export with ``library[name]``, a fresh uncached
    function pointer with explicit argtypes and restype: prototypes never leak
    onto the shared ``windll`` libraries, and an input trap installed on a
    library is never bypassed.
    """

    _library: ctypes.CDLL
    _kernel32: ctypes.CDLL
    _advapi32: ctypes.CDLL

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise NativeInputError("the win32 input backend needs Windows")
        self._library = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    @property
    def library(self) -> ctypes.CDLL:
        """The private user32 ``WinDLL``."""
        return self._library

    @staticmethod
    def _function(
        library: ctypes.CDLL,
        name: str,
        restype: type[_CType],
        argtypes: list[type[_CType]],
    ) -> ctypes._NamedFuncPointer:
        function = library[name]
        function.restype = restype
        function.argtypes = argtypes
        return function

    def _user32(
        self, name: str, restype: type[_CType], argtypes: list[type[_CType]]
    ) -> ctypes._NamedFuncPointer:
        return self._function(self._library, name, restype, argtypes)

    # -- pointer, screen, keys ---------------------------------------------

    def cursor_pos(self) -> tuple[int, int]:
        point = POINT()
        get_cursor_pos = self._user32("GetCursorPos", BOOL, [ctypes.POINTER(POINT)])
        if not get_cursor_pos(ctypes.byref(point)):
            raise NativeInputError(
                f"GetCursorPos failed (error {self.last_error()}); "
                "the input desktop may be locked or secure"
            )
        return int(point.x), int(point.y)

    def set_cursor_pos(self, x: int, y: int) -> bool:
        set_cursor_pos = self._user32(
            "SetCursorPos", BOOL, [ctypes.c_int, ctypes.c_int]
        )
        return bool(set_cursor_pos(x, y))

    def system_metric(self, index: int) -> int:
        get_system_metrics = self._user32(
            "GetSystemMetrics", ctypes.c_int, [ctypes.c_int]
        )
        return int(get_system_metrics(index))

    def map_virtual_key(self, code: int, map_type: int) -> int:
        map_virtual_key = self._user32("MapVirtualKeyW", UINT, [UINT, UINT])
        return int(map_virtual_key(code, map_type))

    def vk_key_scan(self, character: str) -> int:
        """``VkKeyScanW``; -1 when the layout has no key (or outside the BMP)."""
        if len(character) != 1 or ord(character) > 0xFFFF:
            return -1
        vk_key_scan = self._user32("VkKeyScanW", ctypes.c_short, [ctypes.c_wchar])
        return int(vk_key_scan(character))

    def key_held(self, vk: int) -> bool:
        get_async_key_state = self._user32(
            "GetAsyncKeyState", ctypes.c_short, [ctypes.c_int]
        )
        return bool(get_async_key_state(vk) & 0x8000)

    def send_input(self, records: Sequence[INPUT]) -> int:
        send_input = self._user32(
            "SendInput", UINT, [UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        )
        clear_last_error()
        return int(send_input(len(records), input_array(records), ctypes.sizeof(INPUT)))

    def last_error(self) -> int:
        return last_error()

    # -- integrity levels (UIPI) ---------------------------------------------

    def own_integrity(self) -> int | None:
        try:
            get_current_process = self._function(
                self._kernel32, "GetCurrentProcess", HANDLE, []
            )
            return self._process_integrity(get_current_process())
        except _QUERY_ERRORS:
            return None

    def foreground_integrity(self) -> int | None:
        try:
            window = self._user32("GetForegroundWindow", HANDLE, [])()
            if not window:
                return None
            pid = DWORD(0)
            get_window_thread_process_id = self._user32(
                "GetWindowThreadProcessId", DWORD, [HANDLE, ctypes.POINTER(DWORD)]
            )
            get_window_thread_process_id(window, ctypes.byref(pid))
            if not pid.value:
                return None
            open_process = self._function(
                self._kernel32, "OpenProcess", HANDLE, [DWORD, BOOL, DWORD]
            )
            process = open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if not process:
                return None
            try:
                return self._process_integrity(process)
            finally:
                self._close(process)
        except _QUERY_ERRORS:
            return None

    def _close(self, handle: int) -> None:
        close_handle = self._function(self._kernel32, "CloseHandle", BOOL, [HANDLE])
        close_handle(handle)

    def _process_integrity(self, process: int) -> int | None:
        open_process_token = self._function(
            self._advapi32,
            "OpenProcessToken",
            BOOL,
            [HANDLE, DWORD, ctypes.POINTER(HANDLE)],
        )
        token = HANDLE()
        if not open_process_token(process, TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            return self._token_integrity(token)
        finally:
            self._close(token.value or 0)

    def _token_integrity(self, token: ctypes.c_void_p) -> int | None:
        get_token_information = self._function(
            self._advapi32,
            "GetTokenInformation",
            BOOL,
            [HANDLE, ctypes.c_int, HANDLE, DWORD, ctypes.POINTER(DWORD)],
        )
        needed = DWORD(0)
        # The size probe fails with ERROR_INSUFFICIENT_BUFFER by design.
        get_token_information(
            token, TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(needed)
        )
        if not needed.value:
            return None
        # 8-byte units keep the SID pointer inside the label aligned.
        buffer = (ctypes.c_uint64 * ((needed.value + 7) // 8))()
        if not get_token_information(
            token,
            TOKEN_INTEGRITY_LEVEL,
            buffer,
            ctypes.sizeof(buffer),
            ctypes.byref(needed),
        ):
            return None
        sid = TOKEN_MANDATORY_LABEL.from_buffer(buffer).Label.Sid
        if not sid:
            return None
        sub_authority_count = self._function(
            self._advapi32,
            "GetSidSubAuthorityCount",
            ctypes.POINTER(ctypes.c_ubyte),
            [HANDLE],
        )(sid)
        count = int(sub_authority_count[0])
        if not count:
            return None
        sub_authority = self._function(
            self._advapi32,
            "GetSidSubAuthority",
            ctypes.POINTER(DWORD),
            [HANDLE, DWORD],
        )(sid, count - 1)
        # The last sub-authority is the mandatory level RID (0x2000 medium ...).
        return int(sub_authority[0])


class RawUser32Library(Protocol):
    """An object exposing user32 exports as attributes (a test double)."""

    def SendInput(self, count: int, array: ctypes.Array[INPUT], size: int) -> int: ...

    def GetAsyncKeyState(self, vk: int) -> int: ...


class RawUser32:
    """``SendApi`` over a raw user32-like object (``WindowsKeyboardSender(user32=)``).

    Such an object cannot answer integrity queries, so the UIPI preflight sees
    "unknown" and lets the input through.
    """

    def __init__(self, library: RawUser32Library) -> None:
        self._library = library

    def send_input(self, records: Sequence[INPUT]) -> int:
        clear_last_error()
        array = input_array(records)
        return int(self._library.SendInput(len(records), array, ctypes.sizeof(INPUT)))

    def last_error(self) -> int:
        return last_error()

    def own_integrity(self) -> int | None:
        return None

    def foreground_integrity(self) -> int | None:
        return None

    def key_held(self, vk: int) -> bool:
        return bool(self._library.GetAsyncKeyState(vk) & 0x8000)


# -- backend -------------------------------------------------------------------


class Point(NamedTuple):
    x: int
    y: int


class Size(NamedTuple):
    width: int
    height: int


class VirtualScreen(NamedTuple):
    """Bounding box of all monitors; ``left``/``top`` are negative when a
    monitor sits left of or above the primary one."""

    left: int
    top: int
    width: int
    height: int


class RawReleases:
    """``platformModule`` of the backend: releases that skip the fail-safe.

    A release must happen even with the cursor in a fail-safe corner, or the
    button / key would stay down. Coordinates are ignored and never read.
    """

    def __init__(self, backend: Win32Backend) -> None:
        self._backend = backend

    def _position(self) -> tuple[int, int]:
        return self._backend.position()

    def _mouseUp(self, x: int, y: int, button: str) -> None:
        # Like pyautogui's platform layer, the release lands where the cursor is.
        self._backend.release_button(button)

    def _keyUp(self, key: str) -> None:
        self._backend.release_key(key)


class Win32Backend:
    """pyautogui-compatible input backend (``emunium._native.InputBackend``).

    Every action checks the fail-safe first (``EMUNIUM_FAILSAFE``), then the
    UIPI preflight before any button, wheel or key input. Releases skip both.
    ``_pause`` is accepted for compatibility and never sleeps.
    """

    def __init__(self, user32: User32Api | None = None) -> None:
        self._user32: User32Api = user32 if user32 is not None else User32()
        self._sender = InputSender(self._user32)
        self._scan_codes: dict[int, int] = {}
        self.platformModule = RawReleases(self)

    # -- reading ------------------------------------------------------------

    def position(self) -> Point:
        x, y = self._user32.cursor_pos()
        return Point(x, y)

    def size(self) -> Size:
        """Primary monitor size in physical px."""
        return Size(
            self._user32.system_metric(SM_CXSCREEN),
            self._user32.system_metric(SM_CYSCREEN),
        )

    def virtual_screen(self) -> VirtualScreen:
        metric = self._user32.system_metric
        return VirtualScreen(
            metric(SM_XVIRTUALSCREEN),
            metric(SM_YVIRTUALSCREEN),
            metric(SM_CXVIRTUALSCREEN),
            metric(SM_CYVIRTUALSCREEN),
        )

    # -- mouse --------------------------------------------------------------

    def moveTo(self, x: float, y: float, _pause: bool = False) -> None:
        self._failsafe()
        self._set_cursor(round(x), round(y))

    def mouseDown(
        self,
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
        _pause: bool = False,
    ) -> None:
        flags = button_flags(button, up=False)
        self._failsafe()
        self._move_if_given(x, y)
        self._sender.send([mouse_input(flags)], "mouse")

    def mouseUp(
        self,
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
        _pause: bool = False,
    ) -> None:
        flags = button_flags(button, up=True)
        self._failsafe()
        self._move_if_given(x, y)
        self._sender.send([mouse_input(flags)], "mouse")

    def release_button(self, button: str) -> None:
        """Button up at the current position; no fail-safe, no preflight."""
        flags = button_flags(button, up=True)
        self._sender.send([mouse_input(flags)], "mouse", preflight=False)

    def scroll(
        self,
        clicks: int,
        x: int | None = None,
        y: int | None = None,
        _pause: bool = False,
    ) -> None:
        """Vertical wheel, ``clicks`` notches; positive scrolls up."""
        self._wheel(wheel_inputs(clicks), x, y)

    def hscroll(
        self,
        clicks: int,
        x: int | None = None,
        y: int | None = None,
        _pause: bool = False,
    ) -> None:
        """Horizontal wheel, ``clicks`` notches; positive scrolls right."""
        self._wheel(wheel_inputs(clicks, horizontal=True), x, y)

    # -- keyboard -----------------------------------------------------------

    def keyDown(self, key: str, _pause: bool = False) -> None:
        events = self._key_events(key, up=False)
        self._failsafe()
        self._sender.send_keys(events)

    def keyUp(self, key: str, _pause: bool = False) -> None:
        events = self._key_events(key, up=True)
        self._failsafe()
        self._sender.send_keys(events)

    def release_key(self, key: str) -> None:
        """Key up; no fail-safe, no preflight."""
        self._sender.send_keys(self._key_events(key, up=True), preflight=False)

    def press(
        self, keys: str | Sequence[str], presses: int = 1, _pause: bool = False
    ) -> None:
        """Press and release each key in order, ``presses`` times."""
        names = [keys] if isinstance(keys, str) else list(keys)
        if isinstance(presses, bool) or not isinstance(presses, int):
            raise TypeError(f"presses must be an int, got {presses!r}")
        if presses < 0:
            raise ValueError(f"presses must not be negative, got {presses}")
        stroke: list[KeyEvent] = []
        for name in names:
            layout = self._layout(name)
            self._map_scan_codes(name, layout, with_shift=True)
            stroke.extend(
                press_events(name, scan_codes=self._scan_codes, layout=layout)
            )
        self._failsafe()
        for _ in range(presses):
            self._sender.send_keys(stroke)

    def write(self, message: str, _pause: bool = False) -> None:
        """Type ``message`` as Unicode keystrokes (keyboard-layout independent)."""
        if not isinstance(message, str):
            raise TypeError(f"message must be str, got {type(message).__name__}")
        events = keyboard_events(message)
        self._failsafe()
        self._sender.send_keys(events)

    # -- internals ----------------------------------------------------------

    def _failsafe(self) -> None:
        check_failsafe(self.position, self.size)

    def _wheel(self, records: list[INPUT], x: int | None, y: int | None) -> None:
        self._failsafe()
        self._move_if_given(x, y)
        if records:
            self._sender.send(records, "wheel")

    def _set_cursor(self, x: int, y: int) -> None:
        if not self._user32.set_cursor_pos(x, y):
            raise NativeInputError(
                f"SetCursorPos({x}, {y}) failed (error {self._user32.last_error()})"
            )

    def _move_if_given(self, x: int | None, y: int | None) -> None:
        if x is None and y is None:
            return
        current = self.position()
        self._set_cursor(
            current.x if x is None else round(x), current.y if y is None else round(y)
        )

    def _scan_code(self, code: int) -> None:
        if code not in self._scan_codes:
            self._scan_codes[code] = self._user32.map_virtual_key(code, MAPVK_VK_TO_VSC)

    def _layout(self, key: str) -> tuple[int, int] | None:
        """The active layout's key for a printable character outside the table.

        Not cached: the user can switch layouts at any time.
        """
        if virtual_key(key) is not None or len(key) != 1 or not key.isprintable():
            return None
        return layout_key(self._user32.vk_key_scan(key))

    def _map_scan_codes(
        self, key: str, layout: tuple[int, int] | None, *, with_shift: bool
    ) -> None:
        code = virtual_key(key)
        if code is None and layout is not None:
            code = layout[0]
            if with_shift and layout[1] == SHIFT_STATE_SHIFT:
                self._scan_code(VK_SHIFT)
        if code is not None:
            self._scan_code(code)

    def _key_events(self, key: str, *, up: bool) -> list[KeyEvent]:
        """keyDown/keyUp events: the layout's key without its Shift state."""
        layout = self._layout(key)
        self._map_scan_codes(key, layout, with_shift=False)
        return key_events(key, up=up, scan_codes=self._scan_codes, layout=layout)
