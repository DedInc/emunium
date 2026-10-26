"""OS input (emunium._element_interactor) against fakes.

No test moves the real mouse or types: pyautogui, the Windows SendInput
sender, the clipboard, the keyboard package and sleep are all injected.
"""

from __future__ import annotations

import ctypes
import math
import random
import sys
import threading

import pytest

from emunium import _element_interactor as ei
from emunium._element_interactor import (
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    VK_RETURN,
    VK_TAB,
    ElementInteractor,
    WindowsKeyboardSender,
    keyboard_events,
)
from emunium._screen import NATIVE_INPUT_LOCK
from emunium._standalone.config import ClickType
from emunium.errors import NativeInputError

U = KEYEVENTF_UNICODE
UP = KEYEVENTF_KEYUP


class FakeGui:
    def __init__(self, position=(100, 100), size=(1920, 1080), log=None):
        self.PAUSE = 0.1
        self.FAILSAFE = True
        self.pos = position
        self.screen = size
        self.log = log if log is not None else []
        self.pauses = []
        self.fail_on = {}

    def _record(self, event, _pause):
        self.pauses.append(_pause)
        error = self.fail_on.get(event[0])
        self.log.append(event)
        if error is not None:
            raise error

    def position(self):
        return self.pos

    def size(self):
        return self.screen

    def moveTo(self, x, y, _pause=True):
        self.pos = (x, y)
        self._record(("move", x, y), _pause)

    def mouseDown(self, button="left", _pause=True):
        self._record(("down", button), _pause)

    def mouseUp(self, button="left", _pause=True):
        self._record(("up", button), _pause)

    def keyDown(self, key, _pause=True):
        self._record(("keyDown", key), _pause)

    def keyUp(self, key, _pause=True):
        self._record(("keyUp", key), _pause)

    def press(self, keys, _pause=True):
        self._record(("press", keys), _pause)

    def write(self, message, _pause=True):
        self._record(("write", message), _pause)


class FakeSleep:
    def __init__(self, log=None):
        self.calls = []
        self.log = log
        self.hook = None

    def __call__(self, seconds):
        self.calls.append(seconds)
        if self.log is not None:
            self.log.append(("sleep", seconds))
        if self.hook is not None:
            self.hook(seconds)


class FakeSender:
    def __init__(self):
        self.batches = []

    def send(self, events):
        self.batches.append(list(events))


class FakeClipboard:
    def __init__(self, content="previous", log=None, fail_paste=False, fail_copy=False):
        self.content = content
        self.log = log if log is not None else []
        self.fail_paste = fail_paste
        self.fail_copy = fail_copy

    def paste(self):
        if self.fail_paste:
            raise RuntimeError("no clipboard")
        return self.content

    def copy(self, text):
        if self.fail_copy:
            raise RuntimeError("no clipboard")
        self.log.append(("copy", text))
        self.content = text


class FakeKeyboard:
    def __init__(self, log, fail_from=None):
        self.log = log
        self.fail_from = fail_from

    def write(self, character):
        if self.fail_from is not None and character == self.fail_from:
            raise OSError("not permitted")
        self.log.append(("kb", character))


def make(platform="linux", **kwargs):
    log = []
    gui = kwargs.pop("gui", None) or FakeGui(log=log)
    gui.log = log
    sleep = FakeSleep(log)
    interactor = ElementInteractor(
        backend=gui,
        platform=platform,
        sleep=sleep,
        rng=random.Random(7),
        double_click_time=kwargs.pop("double_click_time", 0.5),
        **kwargs
    )
    return interactor, gui, sleep, log


def moves(log):
    return [(e[1], e[2]) for e in log if e[0] == "move"]


def buttons(log):
    return [e for e in log if e[0] in ("down", "up")]


# -- mouse ---------------------------------------------------------------------


def test_human_move_lands_exactly_without_global_settings():
    interactor, gui, sleep, log = make()
    interactor.move(900.4, 600.6)
    path = moves(log)
    assert path[-1] == (900, 601)
    assert gui.position() == (900, 601)
    assert len(path) > 10
    assert all(p is False for p in gui.pauses)
    assert gui.PAUSE == 0.1 and gui.FAILSAFE is True


def test_human_move_is_curved_and_eased():
    interactor, gui, sleep, log = make()
    interactor.move(1100, 100)  # horizontal 1000 px from (100, 100)
    path = moves(log)
    assert max(abs(y - 100) for _, y in path) > 5  # not a straight line
    steps = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:])]
    middle = steps[len(steps) // 2]
    assert steps[0] < middle and steps[-2] < middle


def test_human_move_duration_scales_with_distance():
    near, _, near_sleep, _ = make()
    near.move(150, 100)
    far, _, far_sleep, _ = make()
    far.move(1700, 900)
    assert sum(far_sleep.calls) > sum(near_sleep.calls) > 0
    assert sum(far_sleep.calls) <= 1.2 + 1e-9


def test_human_move_stays_on_screen_and_off_failsafe_corners():
    gui = FakeGui(position=(3, 3), size=(800, 600))
    interactor, gui, _, log = make(gui=gui)
    interactor.move(5, 590)
    for x, y in moves(log)[:-1]:
        assert 1 <= x <= 798 and 1 <= y <= 598


def test_plain_move_is_one_direct_move():
    interactor, gui, sleep, log = make()
    interactor.move(10, 20, human=False)
    assert moves(log) == [(10, 20)] and sleep.calls == []


def test_move_raises_when_cursor_cannot_land():
    class ClampedGui(FakeGui):
        def moveTo(self, x, y, _pause=True):
            super().moveTo(min(x, 500), y, _pause)

    interactor, _, _, log = make(gui=ClampedGui())
    with pytest.raises(NativeInputError):
        interactor.move(900, 100, human=False)
    assert moves(log) == [(500, 100), (500, 100)]


def test_double_click_is_one_move_and_two_presses():
    interactor, gui, sleep, log = make(double_click_time=0.5)
    interactor.click(300, 200, click_type=ClickType.DOUBLE, human=False)
    assert moves(log) == [(300, 200)]
    assert buttons(log) == [("down", "left"), ("up", "left")] * 2
    first_up = log.index(("up", "left"))
    second_down = len(log) - 1 - log[::-1].index(("down", "left"))
    between = [e[1] for e in log[first_up:second_down] if e[0] == "sleep"]
    assert between == [pytest.approx(0.12)]
    assert all(p is False for p in gui.pauses)


def test_double_click_interval_follows_os_time():
    interactor, _, _, _ = make(double_click_time=0.2)
    assert interactor.double_click_interval() == pytest.approx(0.1)
    interactor, _, _, _ = make(double_click_time=1.0)
    assert interactor.double_click_interval() == pytest.approx(0.12)
    default = ElementInteractor(backend=FakeGui(), platform="darwin")
    assert default.double_click_interval() == pytest.approx(0.12)


def test_double_click_total_fits_inside_os_interval():
    interactor, _, sleep, _ = make(double_click_time=0.1)
    interactor.press("left", clicks=2)
    hold, interval, hold2 = sleep.calls
    assert hold + interval < 0.1 and hold2 <= 0.05


@pytest.mark.parametrize(
    "click_type, button", [(ClickType.LEFT, "left"), (ClickType.RIGHT, "right"),
                           (ClickType.MIDDLE, "middle")],
)
def test_single_clicks(click_type, button):
    interactor, _, _, log = make()
    interactor.click(5, 6, click_type=click_type, human=False)
    assert buttons(log) == [("down", button), ("up", button)]


def test_press_validates_arguments():
    interactor, _, _, log = make()
    for bad in ({"button": "x"}, {"clicks": 0}, {"clicks": True}, {"clicks": 1.5}):
        with pytest.raises(ValueError):
            interactor.press(**bad)
    with pytest.raises(ValueError):
        interactor.click(1, 1, click_type="left")  # type: ignore[arg-type]
    assert log == []


def test_mouse_up_sent_when_hold_is_interrupted():
    interactor, _, sleep, log = make()

    def interrupt(seconds):
        raise KeyboardInterrupt

    sleep.hook = interrupt
    with pytest.raises(KeyboardInterrupt):
        interactor.press("right")
    assert buttons(log) == [("down", "right"), ("up", "right")]


def test_mouse_up_attempted_when_mouse_down_fails_and_error_wins():
    interactor, gui, _, log = make()
    gui.fail_on = {"down": RuntimeError("fail-safe"), "up": OSError("secondary")}
    with pytest.raises(RuntimeError, match="fail-safe"):
        interactor.press()
    assert buttons(log) == [("down", "left"), ("up", "left")]


def test_drag_sequence():
    interactor, gui, _, log = make()
    interactor.drag((200, 200), (400, 300), human=True)
    down = log.index(("down", "left"))
    up = log.index(("up", "left"))
    assert moves(log[:down])[-1] == (200, 200)
    assert moves(log[down:up])[-1] == (400, 300)
    assert len(moves(log[down:up])) > 2
    assert up == max(i for i, e in enumerate(log) if e[0] != "sleep")


def test_drag_releases_mouse_when_move_fails():
    interactor, gui, sleep, log = make()
    interactor.move(10, 10, human=False)
    del log[:]
    gui.fail_on = {}

    def fail_after_down(seconds):
        if ("down", "left") in log:
            gui.fail_on = {"move": RuntimeError("moved off")}

    sleep.hook = fail_after_down
    with pytest.raises(RuntimeError, match="moved off"):
        interactor.drag((10, 10), (300, 300), human=False)
    assert buttons(log) == [("down", "left"), ("up", "left")]


def test_input_lock_held_during_os_events():
    interactor, gui, _, _ = make()
    seen = []

    def probe(*args, **kwargs):
        result = []
        thread = threading.Thread(
            target=lambda: result.append(NATIVE_INPUT_LOCK.acquire(blocking=False))
        )
        thread.start()
        thread.join()
        seen.append(result[0])
        if result[0]:
            NATIVE_INPUT_LOCK.release()

    gui.mouseDown = probe
    interactor.press()
    assert seen == [False]


def test_position_and_legacy_names():
    interactor, gui, _, log = make()
    gui.pos = (12.7, 3.2)
    assert interactor.position() == (12, 3)
    assert interactor.screen_point(10, 20, offset_x=0.4, offset_y=1.0) == (10, 21)
    interactor.move_cursor(50, 60, human=False)
    assert moves(log) == [(50, 60)]


# -- Windows typing ------------------------------------------------------------


def test_unicode_event_sequence():
    events = keyboard_events("aé\U0001F600\n\t\r\n")
    assert events == [
        (0, 0x61, U), (0, 0x61, U | UP),
        (0, 0xE9, U), (0, 0xE9, U | UP),
        (0, 0xD83D, U), (0, 0xD83D, U | UP),
        (0, 0xDE00, U), (0, 0xDE00, U | UP),
        (VK_RETURN, 0, 0), (VK_RETURN, 0, UP),
        (VK_TAB, 0, 0), (VK_TAB, 0, UP),
        (VK_RETURN, 0, 0), (VK_RETURN, 0, UP),
    ]


def test_windows_typing_uses_sendinput_for_everything():
    sender = FakeSender()
    interactor, gui, sleep, log = make("win32", unicode_sender=sender)
    interactor.type_text("Hi п\U0001F600\n", human=False)
    assert sender.batches == [keyboard_events("Hi п\U0001F600\n")]
    assert sleep.calls == [] and log == []  # no pyautogui keys, no delay


def test_windows_human_typing_keeps_rhythm_per_character():
    sender = FakeSender()
    interactor, _, sleep, _ = make("win32", unicode_sender=sender)
    interactor.type_text("a\U0001F600\r\n", characters_per_minute=600, offset=20)
    assert [len(b) for b in sender.batches] == [2, 4, 2]
    assert len(sleep.calls) == 3
    assert all(0.08 <= s <= 0.12 for s in sleep.calls)


@pytest.mark.parametrize("cpm", [0, -5])
def test_non_positive_cpm_means_no_delay(cpm):
    sender = FakeSender()
    interactor, _, sleep, _ = make("win32", unicode_sender=sender)
    interactor.type_text("abc", characters_per_minute=cpm)
    assert sleep.calls == [] and len(sender.batches) == 1


def test_type_text_rejects_non_str():
    interactor, _, _, _ = make("win32", unicode_sender=FakeSender())
    with pytest.raises(TypeError):
        interactor.type_text(b"abc")  # type: ignore[arg-type]


class FakeUser32:
    def __init__(self, deliver=None, held=()):
        self.received = []
        self.deliver = deliver
        self.held = set(held)

    def GetAsyncKeyState(self, vk):
        return -32768 if vk in self.held else 0  # SHORT with the high bit set

    def SendInput(self, count, array, size):
        self.received.append(
            [(array[i].type, array[i].ki.wVk, array[i].ki.wScan, array[i].ki.dwFlags)
             for i in range(count)]
        )
        self.size = size
        return count if self.deliver is None else self.deliver


def test_sender_builds_keyboard_inputs():
    user32 = FakeUser32()
    sender = WindowsKeyboardSender(user32=user32)
    sender.send(keyboard_events("\U0001F600\n"))
    assert user32.received == [[
        (1, 0, 0xD83D, U), (1, 0, 0xD83D, U | UP),
        (1, 0, 0xDE00, U), (1, 0, 0xDE00, U | UP),
        (1, VK_RETURN, 0, 0), (1, VK_RETURN, 0, UP),
    ]]
    assert user32.size == ctypes.sizeof(ei._win_input_types()[0])


def test_sender_chunks_long_text():
    user32 = FakeUser32()
    WindowsKeyboardSender(user32=user32).send(keyboard_events("x" * 40))
    assert [len(batch) for batch in user32.received] == [64, 16]


def test_sender_never_splits_a_surrogate_pair():
    user32 = FakeUser32()
    WindowsKeyboardSender(user32=user32).send(keyboard_events("x" * 31 + "\U0001F600"))
    assert [len(batch) for batch in user32.received] == [62, 4]
    assert [e[2] for e in user32.received[1]] == [0xD83D, 0xD83D, 0xDE00, 0xDE00]


def test_windows_typing_refuses_when_user_holds_modifier():
    for vk in (0x11, 0x12, 0x5B, 0x5C):
        user32 = FakeUser32(held={vk})
        sender = WindowsKeyboardSender(user32=user32)
        interactor, _, _, _ = make("win32", unicode_sender=sender)
        with pytest.raises(NativeInputError, match="modifier key held by the user"):
            interactor.type_text("abc", human=False)
        assert user32.received == []


def test_windows_human_typing_rechecks_modifiers_per_stroke():
    user32 = FakeUser32()
    sender = WindowsKeyboardSender(user32=user32)
    interactor, _, sleep, _ = make("win32", unicode_sender=sender)
    sleep.hook = lambda seconds: user32.held.add(0x11)  # user grabs Ctrl mid-text
    with pytest.raises(NativeInputError):
        interactor.type_text("abc", characters_per_minute=600)
    assert len(user32.received) == 1


def test_windows_typing_proceeds_without_modifiers():
    user32 = FakeUser32()
    interactor, _, _, _ = make("win32", unicode_sender=WindowsKeyboardSender(user32=user32))
    interactor.type_text("ab", human=False)
    assert len(user32.received) == 1 and len(user32.received[0]) == 4


@pytest.mark.skipif(sys.platform != "win32", reason="SendInput is Windows-only")
def test_sender_raises_when_input_blocked():
    sender = WindowsKeyboardSender(user32=FakeUser32(deliver=0))
    with pytest.raises(NativeInputError):
        sender.send(keyboard_events("a"))


@pytest.mark.skipif(
    sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8,
    reason="INPUT layout check is for 64-bit Windows",
)
def test_input_struct_size_x64():
    input_type, keybd = ei._win_input_types()
    assert ctypes.sizeof(input_type) == 40
    assert ctypes.sizeof(keybd) == 24


# -- macOS / Linux typing ------------------------------------------------------


def test_posix_ascii_via_pyautogui_and_other_via_paste():
    clipboard = FakeClipboard()
    interactor, gui, sleep, log = make("linux", clipboard=clipboard, keyboard=None)
    interactor.type_text("hi\n\tпр!", human=False)
    keys = [e for e in log if e[0] != "sleep"]
    assert keys == [
        ("write", "h"), ("write", "i"), ("press", "enter"), ("press", "tab"),
        ("keyDown", "ctrl"), ("keyDown", "v"), ("keyUp", "v"), ("keyUp", "ctrl"),
        ("write", "!"),
    ]
    assert ("copy", "пр") in clipboard.log
    assert all(p is False for p in gui.pauses)


def test_macos_paste_uses_command():
    clipboard = FakeClipboard()
    interactor, _, _, log = make("darwin", clipboard=clipboard, keyboard=None)
    interactor.paste_text("ü")
    assert [e for e in log if e[0] == "keyDown"] == [("keyDown", "command"), ("keyDown", "v")]


def test_posix_uses_keyboard_package_when_usable():
    log = []
    interactor, _, _, ilog = make("linux", keyboard=FakeKeyboard(log), clipboard=FakeClipboard())
    interactor.type_text("aéü", human=False)
    assert log == [("kb", "é"), ("kb", "ü")]
    assert ("write", "a") in ilog


def test_keyboard_failure_falls_back_to_paste_for_the_rest():
    log = []
    clipboard = FakeClipboard()
    interactor, _, _, _ = make(
        "linux", keyboard=FakeKeyboard(log, fail_from="ü"), clipboard=clipboard
    )
    interactor.type_text("éüö", human=False)
    assert log == [("kb", "é")]
    assert ("copy", "üö") in clipboard.log


def test_posix_human_typing_paces_and_plain_typing_does_not():
    interactor, _, sleep, _ = make("linux", keyboard=None, clipboard=FakeClipboard())
    interactor.type_text("abc", characters_per_minute=300, offset=0)
    assert sleep.calls == [pytest.approx(0.2)] * 3
    interactor, _, sleep, _ = make("linux", keyboard=None, clipboard=FakeClipboard())
    interactor.type_text("abc", characters_per_minute=300, human=False)
    assert sleep.calls == []


def test_keyboard_package_needs_root_on_posix(monkeypatch):
    monkeypatch.setattr(ei.os, "geteuid", lambda: 1000, raising=False)
    assert ei._usable_keyboard_module("linux") is None
    assert ei._usable_keyboard_module("darwin") is None


# -- clipboard restore rule ----------------------------------------------------


def test_clipboard_restored_after_paste_settles():
    clipboard = FakeClipboard("previous")
    interactor, _, _, log = make("linux", clipboard=clipboard)
    clipboard.log = log
    interactor.paste_text("new")
    assert clipboard.content == "previous"
    settle = log.index(("sleep", ei.PASTE_SETTLE_SECONDS))
    assert log.index(("keyUp", "ctrl")) < settle < log.index(("copy", "previous"))


def test_clipboard_restored_when_settle_is_interrupted():
    clipboard = FakeClipboard("previous")
    interactor, _, sleep, _ = make("linux", clipboard=clipboard)

    def interrupt(seconds):
        raise KeyboardInterrupt

    sleep.hook = interrupt
    with pytest.raises(KeyboardInterrupt):
        interactor.paste_text("new")
    assert clipboard.content == "previous"


def test_clipboard_not_restored_when_changed_meanwhile():
    clipboard = FakeClipboard("previous")
    interactor, _, sleep, _ = make("linux", clipboard=clipboard)

    def user_copies(seconds):
        clipboard.content = "user copied this"

    sleep.hook = user_copies
    interactor.paste_text("new")
    assert clipboard.content == "user copied this"


def test_clipboard_not_restored_when_unreadable():
    clipboard = FakeClipboard("previous", fail_paste=True)
    interactor, _, _, log = make("linux", clipboard=clipboard)
    interactor.paste_text("new")
    assert clipboard.content == "new"
    assert ("keyDown", "v") in log


def test_clipboard_copy_failure_sends_no_keys():
    clipboard = FakeClipboard(fail_copy=True)
    interactor, _, _, log = make("linux", clipboard=clipboard)
    with pytest.raises(NativeInputError):
        interactor.paste_text("new")
    assert [e for e in log if e[0] != "sleep"] == []


# -- hotkey --------------------------------------------------------------------


def test_hotkey_releases_in_reverse_order():
    interactor, _, _, log = make()
    interactor.hotkey("ctrl", "shift", "t")
    assert log == [
        ("keyDown", "ctrl"), ("keyDown", "shift"), ("keyDown", "t"),
        ("keyUp", "t"), ("keyUp", "shift"), ("keyUp", "ctrl"),
    ]


def test_hotkey_releases_pressed_keys_when_a_key_fails():
    class Gui(FakeGui):
        def keyDown(self, key, _pause=True):
            if key == "bogus":
                raise ValueError("unknown key")
            super().keyDown(key, _pause)

    interactor, _, _, log = make(gui=Gui())
    with pytest.raises(ValueError):
        interactor.hotkey("ctrl", "bogus")
    # "bogus" may have been half-sent; its release is attempted too.
    assert log == [("keyDown", "ctrl"), ("keyUp", "bogus"), ("keyUp", "ctrl")]
    with pytest.raises(ValueError):
        interactor.hotkey()


# -- releases bypass the fail-safe and survive interrupts ----------------------


class FailSafeException(Exception):
    """Stands in for pyautogui.FailSafeException."""


class FakePlatform:
    def __init__(self, gui, interrupt_keys=(), interrupt_mouse=False):
        self.gui = gui
        self.interrupt_keys = set(interrupt_keys)
        self.interrupt_mouse = interrupt_mouse

    def _position(self):
        return self.gui.pos

    def _mouseUp(self, x, y, button):
        self.gui.log.append(("raw_up", x, y, button))
        if self.interrupt_mouse:
            raise KeyboardInterrupt

    def _keyUp(self, key):
        self.gui.log.append(("raw_keyUp", key))
        if key in self.interrupt_keys:
            raise KeyboardInterrupt


class CornerGui(FakeGui):
    """Public releases raise like pyautogui's fail-safe with the cursor in a corner."""

    def mouseUp(self, button="left", _pause=True):
        raise FailSafeException("corner")

    def keyUp(self, key, _pause=True):
        raise FailSafeException("corner")


def make_platform(**kwargs):
    interactor, gui, sleep, log = make(gui=CornerGui())
    gui.platformModule = FakePlatform(gui, **kwargs)
    return interactor, gui, sleep, log


def test_mouse_release_uses_platform_layer_past_failsafe():
    interactor, gui, _, log = make_platform()
    gui.pos = (0, 0)
    interactor.press("right")
    assert [e for e in log if e[0] != "sleep"] == [("down", "right"), ("raw_up", 0, 0, "right")]


def test_drag_release_uses_platform_layer():
    interactor, gui, _, log = make_platform()
    interactor.drag((10, 10), (60, 60), human=False)
    assert log[-1] == ("raw_up", 60, 60, "left")


def test_key_release_uses_platform_layer_past_failsafe():
    interactor, _, _, log = make_platform()
    interactor.hotkey("Ctrl", "V")
    assert log == [
        ("keyDown", "Ctrl"), ("keyDown", "V"), ("raw_keyUp", "V"), ("raw_keyUp", "ctrl"),
    ]


def test_paste_release_uses_platform_layer():
    clipboard = FakeClipboard("previous")
    interactor, gui, _, log = make(platform="linux", gui=CornerGui(), clipboard=clipboard)
    gui.platformModule = FakePlatform(gui)
    interactor.paste_text("new")
    assert ("raw_keyUp", "ctrl") in log and ("raw_keyUp", "v") in log
    assert clipboard.content == "previous"


def test_keyboard_interrupt_mid_release_still_releases_every_key():
    interactor, _, _, log = make_platform(interrupt_keys={"t"})
    with pytest.raises(KeyboardInterrupt):
        interactor.hotkey("ctrl", "shift", "t")
    assert [e for e in log if e[0] == "raw_keyUp"] == [
        ("raw_keyUp", "t"), ("raw_keyUp", "shift"), ("raw_keyUp", "ctrl"),
    ]


def test_keyboard_interrupt_during_key_down_releases_pressed_keys():
    interactor, gui, _, log = make_platform()

    def key_down(key, _pause=True):
        log.append(("keyDown", key))
        if key == "alt":
            raise KeyboardInterrupt

    gui.keyDown = key_down
    with pytest.raises(KeyboardInterrupt):
        interactor.hotkey("ctrl", "alt", "del")
    assert log == [
        ("keyDown", "ctrl"), ("keyDown", "alt"), ("raw_keyUp", "alt"), ("raw_keyUp", "ctrl"),
    ]


def test_keyboard_interrupt_in_mouse_release_propagates_after_release():
    interactor, _, _, log = make_platform(interrupt_mouse=True)
    with pytest.raises(KeyboardInterrupt):
        interactor.press()
    assert [e[0] for e in log if e[0] != "sleep"] == ["down", "raw_up"]


def test_keyboard_interrupt_during_drag_releases_button():
    interactor, gui, sleep, log = make_platform()

    def interrupt_after_down(seconds):
        if ("down", "left") in log:
            raise KeyboardInterrupt

    sleep.hook = interrupt_after_down
    with pytest.raises(KeyboardInterrupt):
        interactor.drag((10, 10), (300, 300), human=True)
    assert [e for e in log if e[0] in ("down", "raw_up")] == [
        ("down", "left"), ("raw_up", 10, 10, "left"),
    ]


# -- fail-safe corners on the path ---------------------------------------------


def test_off_corner_nudges_only_corners():
    box = (800, 600)
    assert ei._off_corner((0, 0), box) == (1, 0)
    assert ei._off_corner((799, 0), box) == (798, 0)
    assert ei._off_corner((0, 599), box) == (1, 599)
    assert ei._off_corner((799, 599), box) == (798, 599)
    assert ei._off_corner((0, 300), box) == (0, 300)
    assert ei._off_corner((0, 0), None) == (0, 0)


def test_linear_drag_path_avoids_corner_from_offscreen_endpoint():
    gui = FakeGui(position=(0, 99), size=(800, 600))
    interactor, gui, _, log = make(gui=gui)
    interactor.drag((0, 99), (0, 1099), human=False)
    path = moves(log)
    assert (0, 599) not in path and (1, 599) in path
    assert path[-1] == (0, 1099)


def test_human_path_never_stops_on_a_corner_but_keeps_corner_target():
    for seed in range(40):
        gui = FakeGui(position=(-150, -120), size=(800, 600))
        interactor, gui, _, log = make(gui=gui)
        interactor._rng = random.Random(seed)
        interactor.move(150, 120)
        interactor.move(799, 599)
        path = moves(log)
        corners = {(0, 0), (799, 0), (0, 599), (799, 599)}
        assert path[-1] == (799, 599)  # a corner target is never altered
        assert not corners & {p for p in path if p != (799, 599)}
