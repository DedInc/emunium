"""Windows input through user32, ctypes only.

``SetCursorPos`` / ``GetCursorPos`` move and read the pointer; ``SendInput``
carries buttons, the wheel and keys. The window helpers at the end find the
browser's top-level window and raise it without sending any input. ``emunium._screen.input_backend`` makes the
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
from collections.abc import Collection, Mapping, Sequence
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
    """``platformModule`` of the backend: input that skips the fail-safe.

    A release must happen even with the cursor in a fail-safe corner, or the
    button / key would stay down. Coordinates are ignored and never read.
    ``_keyDown`` serves cleanup only (the Escape that cancels an aborted drag
    before its button is released).
    """

    def __init__(self, backend: Win32Backend) -> None:
        self._backend = backend

    def _position(self) -> tuple[int, int]:
        return self._backend.position()

    def _mouseUp(self, x: int, y: int, button: str) -> None:
        # Like pyautogui's platform layer, the release lands where the cursor is.
        self._backend.release_button(button)

    def _keyDown(self, key: str) -> None:
        self._backend.raw_key_down(key)

    def _keyUp(self, key: str) -> None:
        self._backend.release_key(key)


class Win32Backend:
    """pyautogui-compatible input backend (``emunium._native.InputBackend``).

    Every action checks the fail-safe first (``EMUNIUM_FAILSAFE``), then the
    UIPI preflight before any button, wheel or key input. Releases and the raw
    key-down of ``platformModule`` skip both.
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

    def raw_key_down(self, key: str) -> None:
        """Key down for a cancel path; no fail-safe, no preflight.

        A UIPI block must not stop the cleanup that follows (the button
        release), so the send is attempted anyway. A short send still raises;
        the cleanup records it and continues.
        """
        self._sender.send_keys(self._key_events(key, up=False), preflight=False)

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


# -- top-level windows and the foreground ------------------------------------------
#
# A browser launched from a background process opens behind other windows (the
# foreground lock), and ``chrome.windows.update(focused)`` cannot override it.
# These helpers find the browser's top-level window and raise it through window
# management calls only: nothing here sends keyboard or mouse input.

LPARAM = ctypes.c_ssize_t
LONG_PTR = ctypes.c_ssize_t

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
SW_RESTORE = 9
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
CLASS_NAME_CHARS = 256

# Class of Chrome's top-level browser windows (owned by the browser process).
CHROME_WINDOW_CLASS = "Chrome_WidgetWin_1"
# Largest edge distance, in physical px, at which a window still matches the
# geometry Chrome reports: rounding of DIPs and the frame's invisible resize
# borders (7-8 px on Windows 10/11) both fit inside it.
WINDOW_MATCH_TOLERANCE = 16


class RECT(ctypes.Structure):
    _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]


class PROCESSENTRY32W(ctypes.Structure):
    """568 bytes on x64, 556 on x86; ``dwSize`` must be set before use."""

    _fields_ = [
        ("dwSize", DWORD),
        ("cntUsage", DWORD),
        ("th32ProcessID", DWORD),
        ("th32DefaultHeapID", ULONG_PTR),
        ("th32ModuleID", DWORD),
        ("cntThreads", DWORD),
        ("th32ParentProcessID", DWORD),
        ("pcPriClassBase", LONG),
        ("dwFlags", DWORD),
        ("szExeFile", WORD * 260),  # WCHAR[MAX_PATH]; never read here
    ]


class WindowInfo(NamedTuple):
    """One visible top-level window; the rectangle is in physical px."""

    hwnd: int
    pid: int
    thread_id: int
    class_name: str
    left: int
    top: int
    right: int
    bottom: int
    minimized: bool

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


class WindowApi(Protocol):
    """The window-management calls the helpers make (a fake in tests)."""

    def enum_windows(self) -> list[int]: ...

    def window_thread_process_id(self, hwnd: int) -> tuple[int, int]: ...

    def is_window_visible(self, hwnd: int) -> bool: ...

    def class_name(self, hwnd: int) -> str: ...

    def window_rect(self, hwnd: int) -> tuple[int, int, int, int] | None: ...

    def is_iconic(self, hwnd: int) -> bool: ...

    def ex_style(self, hwnd: int) -> int: ...

    def process_parents(self) -> list[tuple[int, int]]: ...

    def foreground_window(self) -> int: ...

    def current_thread_id(self) -> int: ...

    def attach_thread_input(self, attach: int, attach_to: int, on: bool) -> bool: ...

    def show_window(self, hwnd: int, command: int) -> bool: ...

    def set_foreground_window(self, hwnd: int) -> bool: ...

    def bring_window_to_top(self, hwnd: int) -> bool: ...

    def set_focus(self, hwnd: int) -> int: ...


if TYPE_CHECKING:
    _WindowCType: TypeAlias = (
        _CType
        | ctypes.c_ssize_t
        | ctypes.c_wchar_p
        | ctypes._CFunctionType
        | ctypes._Pointer[RECT]
        | ctypes._Pointer[PROCESSENTRY32W]
    )


class Win32Windows:
    """``WindowApi`` over private user32/kernel32 ``WinDLL`` objects.

    Like ``User32``, every call resolves its export afresh with explicit
    argtypes and restype, so nothing leaks onto the shared ``windll``.
    """

    _library: ctypes.CDLL
    _kernel32: ctypes.CDLL

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise NativeInputError("window management needs Windows")
        self._library = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # WNDENUMPROC: BOOL CALLBACK (HWND, LPARAM).
        self._enum_proc = ctypes.WINFUNCTYPE(BOOL, HANDLE, LPARAM)

    @staticmethod
    def _function(
        library: ctypes.CDLL,
        name: str,
        restype: type[_WindowCType],
        argtypes: list[type[_WindowCType]],
    ) -> ctypes._NamedFuncPointer:
        function = library[name]
        function.restype = restype
        function.argtypes = argtypes
        return function

    def _user32(
        self,
        name: str,
        restype: type[_WindowCType],
        argtypes: list[type[_WindowCType]],
    ) -> ctypes._NamedFuncPointer:
        return self._function(self._library, name, restype, argtypes)

    def _kernel(
        self,
        name: str,
        restype: type[_WindowCType],
        argtypes: list[type[_WindowCType]],
    ) -> ctypes._NamedFuncPointer:
        return self._function(self._kernel32, name, restype, argtypes)

    # -- reading ------------------------------------------------------------

    def enum_windows(self) -> list[int]:
        handles: list[int] = []

        def collect(hwnd: int | None, _lparam: int) -> bool:
            if hwnd:
                handles.append(hwnd)
            return True

        callback = self._enum_proc(collect)
        enum_windows = self._user32("EnumWindows", BOOL, [self._enum_proc, LPARAM])
        if not enum_windows(callback, 0):
            raise NativeInputError(f"EnumWindows failed (error {last_error()})")
        return handles

    def window_thread_process_id(self, hwnd: int) -> tuple[int, int]:
        """``(thread id, process id)``; ``(0, 0)`` for a vanished window."""
        pid = DWORD(0)
        get_window_thread_process_id = self._user32(
            "GetWindowThreadProcessId", DWORD, [HANDLE, ctypes.POINTER(DWORD)]
        )
        thread_id = get_window_thread_process_id(hwnd, ctypes.byref(pid))
        return int(thread_id), int(pid.value)

    def is_window_visible(self, hwnd: int) -> bool:
        return bool(self._user32("IsWindowVisible", BOOL, [HANDLE])(hwnd))

    def class_name(self, hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(CLASS_NAME_CHARS)
        get_class_name = self._user32(
            "GetClassNameW", ctypes.c_int, [HANDLE, ctypes.c_wchar_p, ctypes.c_int]
        )
        if not get_class_name(hwnd, buffer, CLASS_NAME_CHARS):
            return ""
        return buffer.value

    def window_rect(self, hwnd: int) -> tuple[int, int, int, int] | None:
        rect = RECT()
        get_window_rect = self._user32(
            "GetWindowRect", BOOL, [HANDLE, ctypes.POINTER(RECT)]
        )
        if not get_window_rect(hwnd, ctypes.byref(rect)):
            return None
        return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)

    def is_iconic(self, hwnd: int) -> bool:
        return bool(self._user32("IsIconic", BOOL, [HANDLE])(hwnd))

    def ex_style(self, hwnd: int) -> int:
        # GetWindowLongPtrW is a macro for GetWindowLongW in 32-bit user32.
        name = (
            "GetWindowLongPtrW"
            if ctypes.sizeof(ctypes.c_void_p) == 8
            else "GetWindowLongW"
        )
        get_window_long = self._user32(name, LONG_PTR, [HANDLE, ctypes.c_int])
        return int(get_window_long(hwnd, GWL_EXSTYLE))

    def process_parents(self) -> list[tuple[int, int]]:
        """``(pid, parent pid)`` of every running process (a Toolhelp snapshot)."""
        create_snapshot = self._kernel(
            "CreateToolhelp32Snapshot", HANDLE, [DWORD, DWORD]
        )
        snapshot = create_snapshot(TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == INVALID_HANDLE_VALUE:
            raise NativeInputError(
                f"CreateToolhelp32Snapshot failed (error {last_error()})"
            )
        try:
            entry_pointer = ctypes.POINTER(PROCESSENTRY32W)
            first = self._kernel("Process32FirstW", BOOL, [HANDLE, entry_pointer])
            following = self._kernel("Process32NextW", BOOL, [HANDLE, entry_pointer])
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            pairs: list[tuple[int, int]] = []
            more = first(snapshot, ctypes.byref(entry))
            while more:
                pairs.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
                more = following(snapshot, ctypes.byref(entry))
            return pairs
        finally:
            self._kernel("CloseHandle", BOOL, [HANDLE])(snapshot)

    def foreground_window(self) -> int:
        return int(self._user32("GetForegroundWindow", HANDLE, [])() or 0)

    def current_thread_id(self) -> int:
        return int(self._kernel("GetCurrentThreadId", DWORD, [])())

    # -- window management (no input) ----------------------------------------

    def attach_thread_input(self, attach: int, attach_to: int, on: bool) -> bool:
        attach_thread_input = self._user32(
            "AttachThreadInput", BOOL, [DWORD, DWORD, BOOL]
        )
        return bool(attach_thread_input(attach, attach_to, on))

    def show_window(self, hwnd: int, command: int) -> bool:
        show_window = self._user32("ShowWindow", BOOL, [HANDLE, ctypes.c_int])
        return bool(show_window(hwnd, command))

    def set_foreground_window(self, hwnd: int) -> bool:
        return bool(self._user32("SetForegroundWindow", BOOL, [HANDLE])(hwnd))

    def bring_window_to_top(self, hwnd: int) -> bool:
        return bool(self._user32("BringWindowToTop", BOOL, [HANDLE])(hwnd))

    def set_focus(self, hwnd: int) -> int:
        """The previously focused window; 0 when focus was refused."""
        return int(self._user32("SetFocus", HANDLE, [HANDLE])(hwnd) or 0)


def top_level_windows(
    pid_set: Collection[int],
    api: WindowApi | None = None,
    *,
    class_name: str = CHROME_WINDOW_CLASS,
) -> list[WindowInfo]:
    """Visible top-level ``class_name`` windows owned by ``pid_set``, in Z order.

    Invisible, zero-size and tool windows are skipped, as is a window that
    vanishes while it is being read. Minimized windows stay in the list.
    """
    windows = api if api is not None else Win32Windows()
    wanted = frozenset(pid_set)
    found: list[WindowInfo] = []
    for hwnd in windows.enum_windows():
        thread_id, pid = windows.window_thread_process_id(hwnd)
        if not thread_id or pid not in wanted:
            continue
        if not windows.is_window_visible(hwnd):
            continue
        if windows.ex_style(hwnd) & WS_EX_TOOLWINDOW:
            continue
        name = windows.class_name(hwnd)
        if name != class_name:
            continue
        rect = windows.window_rect(hwnd)
        if rect is None:
            continue
        left, top, right, bottom = rect
        if right <= left or bottom <= top:
            continue
        found.append(
            WindowInfo(
                hwnd=hwnd,
                pid=pid,
                thread_id=thread_id,
                class_name=name,
                left=left,
                top=top,
                right=right,
                bottom=bottom,
                minimized=windows.is_iconic(hwnd),
            )
        )
    return found


def descendant_pids(root_pid: int, api: WindowApi | None = None) -> frozenset[int]:
    """``root_pid`` and every process below it in the parent chain.

    Toolhelp reports a parent pid even after the parent exited, so a reused
    pid can adopt strangers; the walk stays finite on such cycles.
    """
    windows = api if api is not None else Win32Windows()
    children: dict[int, list[int]] = {}
    for pid, parent in windows.process_parents():
        if pid != parent:
            children.setdefault(parent, []).append(pid)
    found = {root_pid}
    pending = [root_pid]
    while pending:
        for child in children.get(pending.pop(), ()):
            if child not in found:
                found.add(child)
                pending.append(child)
    return frozenset(found)


def pick_browser_window(
    windows: Sequence[WindowInfo],
    dip_rect: tuple[float, float, float, float] | None,
    scale: float,
    *,
    tolerance: float = WINDOW_MATCH_TOLERANCE,
) -> WindowInfo | None:
    """The window whose rectangle matches Chrome's window geometry.

    ``dip_rect`` is ``(left, top, width, height)`` in DIPs as
    ``chrome.windows`` reports it and ``scale`` the device pixel ratio. The
    window with the smallest largest-edge distance within ``tolerance``
    physical px wins (the topmost on a tie). Minimized windows sit at a parking
    position, so they never match; without a match a single candidate is
    unambiguous and returned, otherwise ``None``.
    """
    if scale <= 0:
        raise ValueError(f"scale must be positive, got {scale!r}")
    if dip_rect is not None:
        left, top, width, height = dip_rect
        expected = (
            left * scale,
            top * scale,
            (left + width) * scale,
            (top + height) * scale,
        )
        best: WindowInfo | None = None
        best_error = 0.0
        for window in windows:
            if window.minimized:
                continue
            edges = (window.left, window.top, window.right, window.bottom)
            error = max(abs(edge - want) for edge, want in zip(edges, expected))
            if error <= tolerance and (best is None or error < best_error):
                best, best_error = window, error
        if best is not None:
            return best
    return windows[0] if len(windows) == 1 else None


def is_foreground(hwnd: int, api: WindowApi | None = None) -> bool:
    windows = api if api is not None else Win32Windows()
    return bool(hwnd) and windows.foreground_window() == hwnd


def bring_to_foreground(hwnd: int, api: WindowApi | None = None) -> bool:
    """Raise ``hwnd`` above every window; True when it ends up in the foreground.

    Restores a minimized window, then attaches this thread's input state to
    the current foreground thread (and the window's own thread, so ``SetFocus``
    is allowed) to lift the foreground lock, raises the window and always
    detaches again. Sends no keyboard or mouse input.
    """
    windows = api if api is not None else Win32Windows()
    if not hwnd:
        return False
    if windows.is_iconic(hwnd):
        windows.show_window(hwnd, SW_RESTORE)
    current = windows.current_thread_id()
    foreground = windows.foreground_window()
    targets: list[int] = []
    if foreground and foreground != hwnd:
        targets.append(windows.window_thread_process_id(foreground)[0])
    targets.append(windows.window_thread_process_id(hwnd)[0])
    attached: list[int] = []
    try:
        for thread_id in targets:
            if (
                thread_id
                and thread_id != current
                and thread_id not in attached
                and windows.attach_thread_input(current, thread_id, True)
            ):
                attached.append(thread_id)
        windows.set_foreground_window(hwnd)
        windows.bring_window_to_top(hwnd)
        if attached:
            windows.set_focus(hwnd)
    finally:
        for thread_id in reversed(attached):
            windows.attach_thread_input(current, thread_id, False)
    return windows.foreground_window() == hwnd
