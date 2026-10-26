"""The conftest input safety net: every real OS input entry point is trapped.

Each test first asserts that the entry point *is* a trap and only then calls
it, so a missing trap fails the assertion instead of reaching the desktop.
"""

from __future__ import annotations

import ctypes
import sys
from types import ModuleType
from typing import Sequence

import pytest
from conftest import (
    CTYPES_TRAPS,
    MODULE_TRAPS,
    PYAUTOGUI_PLATFORM_TRAPS,
    REAL_INPUT_MESSAGE,
    SafetyNet,
)

from emunium._element_interactor import (
    ElementInteractor,
    WindowsKeyboardSender,
    keyboard_events,
)

# SECURITY_MANDATORY_MEDIUM_RID: a non-elevated process's integrity level.
_MEDIUM_INTEGRITY = 0x2000


@pytest.fixture
def net(input_safety_net: SafetyNet | None) -> SafetyNet:
    assert input_safety_net is not None, "safety net must be armed in unit tests"
    return input_safety_net


def _assert_trapped(net: SafetyNet, owner: object, names: Sequence[str]) -> None:
    for name in names:
        entry = getattr(owner, name)
        assert net.is_trap(entry), f"{name} is not trapped"
        with pytest.raises(RuntimeError, match=REAL_INPUT_MESSAGE):
            entry()


def _present(owner: object, names: Sequence[str]) -> list[str]:
    return [name for name in names if hasattr(owner, name)]


def test_pyautogui_public_functions_trapped(net):
    pyautogui = pytest.importorskip("pyautogui")
    names = _present(pyautogui, MODULE_TRAPS["pyautogui"])
    assert {
        "moveTo",
        "click",
        "mouseDown",
        "mouseUp",
        "scroll",
        "keyDown",
        "keyUp",
        "press",
        "hotkey",
        "write",
        "typewrite",
        "screenshot",
    } <= set(names)
    _assert_trapped(net, pyautogui, names)


def test_pyautogui_platform_module_trapped(net):
    pyautogui = pytest.importorskip("pyautogui")
    platform_module = pyautogui.platformModule
    names = _present(platform_module, PYAUTOGUI_PLATFORM_TRAPS)
    assert {
        "_moveTo",
        "_mouseDown",
        "_mouseUp",
        "_keyDown",
        "_keyUp",
        "_scroll",
    } <= set(names)
    _assert_trapped(net, platform_module, names)


def test_interactor_release_path_hits_platform_trap(net):
    pyautogui = pytest.importorskip("pyautogui")
    # The fail-safe-free release bypasses pyautogui's public API.
    assert net.is_trap(pyautogui.platformModule._mouseUp)
    assert net.is_trap(pyautogui.platformModule._keyUp)
    with pytest.raises(RuntimeError, match=REAL_INPUT_MESSAGE):
        ElementInteractor._mouse_release(pyautogui, "left")
    with pytest.raises(RuntimeError, match=REAL_INPUT_MESSAGE):
        ElementInteractor._key_release(pyautogui, "shift")


def test_keyboard_package_trapped(net):
    # Optional extra; on POSIX importing it also needs root (plain ImportError).
    keyboard = pytest.importorskip("keyboard", exc_type=ImportError)
    _assert_trapped(net, keyboard, MODULE_TRAPS["keyboard"])


def test_pyperclip_trapped(net):
    pyperclip = pytest.importorskip("pyperclip")
    _assert_trapped(net, pyperclip, MODULE_TRAPS["pyperclip"])


def test_humancursor_system_cursor_trapped(net):
    humancursor = pytest.importorskip("humancursor")
    _assert_trapped(net, humancursor, MODULE_TRAPS["humancursor"])
    _assert_trapped(net, sys.modules["humancursor.system_cursor"], ["SystemCursor"])


@pytest.mark.skipif(sys.platform != "win32", reason="user32 is Windows-only")
def test_ctypes_user32_input_functions_trapped(net):
    private = ctypes.WinDLL("user32", use_last_error=True)
    shared = ctypes.windll.user32
    names = sorted(CTYPES_TRAPS)
    _assert_trapped(net, private, names)
    _assert_trapped(net, shared, names)
    for name in names:
        assert net.is_trap(private[name])
    # Read-only user32 calls stay real.
    assert not net.is_trap(private.GetAsyncKeyState)


@pytest.mark.skipif(sys.platform != "win32", reason="SendInput is Windows-only")
def test_real_windows_keyboard_sender_hits_send_input_trap(net, monkeypatch):
    sender = WindowsKeyboardSender()
    # The UIPI preflight reads the focused window's integrity level; stub both
    # queries to the same medium level (0x2000) so the result depends only on
    # the SendInput trap, not on whether an elevated window has focus.
    monkeypatch.setattr(sender._api, "own_integrity", lambda: _MEDIUM_INTEGRITY)
    monkeypatch.setattr(sender._api, "foreground_integrity", lambda: _MEDIUM_INTEGRITY)
    sender._sender.preflight()
    assert net.is_trap(sender._user32.SendInput)
    with pytest.raises(RuntimeError, match=REAL_INPUT_MESSAGE):
        sender.send(keyboard_events("a"))


class _ClipboardModule(ModuleType):
    """A test's stand-in for ``pyperclip`` (no import spec)."""

    def __init__(self) -> None:
        super().__init__("pyperclip")
        self.copied: list[str] = []

    def copy(self, text: str) -> None:
        self.copied.append(text)

    def paste(self) -> str:
        return self.copied[-1]


def test_injected_fake_module_is_used_untrapped(net, monkeypatch):
    fake = _ClipboardModule()
    monkeypatch.setitem(sys.modules, "pyperclip", fake)
    clipboard = ElementInteractor(platform="linux")._clip()
    assert clipboard is fake and not net.is_trap(fake.copy)
    clipboard.copy("text")
    assert clipboard.paste() == "text"


def test_arming_skips_stand_in_already_in_sys_modules(monkeypatch):
    fake = _ClipboardModule()
    monkeypatch.setitem(sys.modules, "pyperclip", fake)
    nested = SafetyNet()
    nested.arm()
    try:
        assert not nested.is_trap(fake.copy)
    finally:
        nested.disarm()


def test_disarm_restores_real_attributes():
    module = _ClipboardModule()
    real_copy = module.copy
    local = SafetyNet()
    local.arm_module(module)
    trap = module.copy
    assert local.is_trap(trap)
    local.disarm()
    assert module.copy == real_copy
    # A trap that leaked past disarm still refuses outside e2e tests.
    with pytest.raises(RuntimeError, match=REAL_INPUT_MESSAGE):
        trap("text")
    assert module.copied == []
