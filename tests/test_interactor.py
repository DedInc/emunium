"""OS input (emunium._element_interactor) against fakes.

No test moves the real mouse or types: pyautogui, the Windows SendInput
sender, the clipboard, the keyboard package and sleep are all injected.
"""

from __future__ import annotations

import ctypes
import math
import random
import statistics
import sys
import threading
from typing import cast

import pytest

from emunium import _element_interactor as ei
from emunium._bridge.facade import Bridge
from emunium._browser import context as browser_context
from emunium._browser import facade as browser_facade
from emunium._browser.context import BrowserContext, DragInteractor
from emunium._browser.facade import Browser
from emunium._element_interactor import (
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    VK_RETURN,
    VK_TAB,
    ElementInteractor,
    WindowsKeyboardSender,
    keyboard_events,
)
from emunium._motion import (
    DEFAULT_TARGET_SIZE,
    MotionStep,
    ScreenBounds,
    SigmaDriftPlanner,
    TypingRhythm,
    resolve_typing,
)
from emunium._native import win32
from emunium._native.win32 import MonitorRect, Win32Backend
from emunium._screen import NATIVE_INPUT_LOCK, Geometry, input_per_dip
from emunium._standalone import facade as standalone_facade
from emunium._standalone.config import ClickType
from emunium.element import TYPE_CHUNK, Element
from emunium.errors import InvalidArgumentError, NativeInputError
from emunium.standalone import Emunium

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


class FakeClock:
    """The interactor's clock; only ``FakeSleep`` advances it."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeSleep:
    """Records each sleep and advances ``clock`` by exactly the seconds asked."""

    def __init__(self, log=None):
        self.calls = []
        self.log = log
        self.hook = None
        self.clock = FakeClock()

    def __call__(self, seconds):
        self.calls.append(seconds)
        self.clock.now += seconds
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
        clock=sleep.clock,
        rng=random.Random(7),
        double_click_time=kwargs.pop("double_click_time", 0.5),
        **kwargs,
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
    "click_type, button",
    [
        (ClickType.LEFT, "left"),
        (ClickType.RIGHT, "right"),
        (ClickType.MIDDLE, "middle"),
    ],
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
    events = keyboard_events("aé\U0001f600\n\t\r\n")
    assert events == [
        (0, 0x61, U),
        (0, 0x61, U | UP),
        (0, 0xE9, U),
        (0, 0xE9, U | UP),
        (0, 0xD83D, U),
        (0, 0xD83D, U | UP),
        (0, 0xDE00, U),
        (0, 0xDE00, U | UP),
        (VK_RETURN, 0, 0),
        (VK_RETURN, 0, UP),
        (VK_TAB, 0, 0),
        (VK_TAB, 0, UP),
        (VK_RETURN, 0, 0),
        (VK_RETURN, 0, UP),
    ]


def test_windows_typing_uses_sendinput_for_everything():
    sender = FakeSender()
    interactor, gui, sleep, log = make("win32", unicode_sender=sender)
    interactor.type_text("Hi п\U0001f600\n", human=False)
    assert sender.batches == [keyboard_events("Hi п\U0001f600\n")]
    assert sleep.calls == [] and log == []  # no pyautogui keys, no delay


def test_windows_human_typing_keeps_rhythm_per_character():
    sender = FakeSender()
    interactor, _, sleep, _ = make("win32", unicode_sender=sender)
    interactor.type_text("a\U0001f600\r\n", characters_per_minute=600, offset=20)
    assert [len(b) for b in sender.batches] == [2, 4, 2]
    assert len(sleep.calls) == 3
    low, high = interactor.typing.adjusted(600, 20).bounds()
    assert all(low <= s <= high for s in sleep.calls)


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


# -- typing rhythm -------------------------------------------------------------

# Cyrillic keys get neutral bigram and shift factors.
NEUTRAL = "ж"
# Only the lognormal base: no micro-pauses, no jitter.
PLAIN = TypingRhythm(micro_pause_probability=0.0, jitter_ms=0.0)


def draws(rhythm, typed, upcoming, count=4000, seed=1):
    rng = random.Random(seed)
    return [rhythm.interval(typed, upcoming, rng) for _ in range(count)]


@pytest.mark.parametrize("cpm", [60, 280, 900])
def test_rhythm_base_is_lognormal_around_the_requested_speed(cpm):
    rhythm = PLAIN.adjusted(cpm, 0)
    intervals = draws(rhythm, NEUTRAL, NEUTRAL)
    assert statistics.median(intervals) == pytest.approx(60.0 / cpm, rel=0.03)
    logs = [math.log(s) for s in intervals]
    assert statistics.stdev(logs) == pytest.approx(rhythm.sigma, rel=0.08)
    cut = 3.0 * rhythm.sigma
    assert min(intervals) >= 60.0 / cpm * math.exp(-cut) - 1e-12
    assert max(intervals) <= 60.0 / cpm * math.exp(cut) + 1e-12


def test_natural_defaults_feel_human():
    rhythm = TypingRhythm.preset("natural")
    assert rhythm == TypingRhythm()
    assert 0.25 <= rhythm.sigma <= 0.45
    assert rhythm.characters_per_minute == 280
    intervals = draws(rhythm, NEUTRAL, NEUTRAL)
    assert statistics.median(intervals) == pytest.approx(60.0 / 280, rel=0.05)
    assert 70 <= rhythm.dwell * 1000 <= 110


def test_pause_after_space_and_sentence_punctuation():
    base = statistics.median(draws(PLAIN, NEUTRAL, NEUTRAL))
    word = statistics.median(draws(PLAIN, " ", NEUTRAL)) / base
    sentence = statistics.median(draws(PLAIN, ".", " ")) / base
    line = statistics.median(draws(PLAIN, "\n", NEUTRAL)) / base
    assert 1.6 <= word <= 2.5 and 2.5 <= sentence <= 4.0 and 2.5 <= line <= 4.0
    assert sentence > word * 1.2
    end = statistics.median(draws(PLAIN, "!", None)) / base
    assert 2.5 <= end <= 4.0


def test_decimal_point_is_not_a_sentence_end():
    rng = random.Random(3)
    assert PLAIN.factor(".", "1", rng) == PLAIN.alternate_hand  # right ring, left pinky


def test_pause_before_capitals_and_shifted_symbols_only():
    rng = random.Random(4)
    for upcoming in ("A", "?", '"'):
        assert 1.2 <= PLAIN.factor(NEUTRAL, upcoming, rng) <= 1.5
    for upcoming in ("a", "/", NEUTRAL, "Ж"):
        assert PLAIN.factor(NEUTRAL, upcoming, rng) == 1.0


@pytest.mark.parametrize(
    ("typed", "upcoming", "kind"),
    [
        ("a", "j", "alternate_hand"),
        ("T", "h", "alternate_hand"),
        ("!", "p", "alternate_hand"),
        ("a", "s", "roll"),
        ("k", "j", "roll"),
        ("e", "d", "same_finger"),
        ("l", "l", "same_finger"),
        ("f", "t", "same_finger"),
        ("a", "d", None),
        (NEUTRAL, "a", None),
        ("a", " ", None),
    ],
)
def test_bigram_factor_follows_a_qwerty_finger_map(typed, upcoming, kind):
    expected = 1.0 if kind is None else getattr(PLAIN, kind)
    assert PLAIN.bigram(typed, upcoming) == expected
    assert PLAIN.alternate_hand < 1.0 < PLAIN.same_finger


def test_micro_pauses_are_rare_and_bounded():
    rhythm = TypingRhythm(
        characters_per_minute=600,
        sigma=0.1,
        micro_pause_probability=0.03,
        micro_pause=(0.4, 0.6),
        jitter_ms=0.0,
    )
    intervals = draws(rhythm, NEUTRAL, NEUTRAL, count=8000)
    # Base intervals stay below 0.1 * e^0.3 s; a micro-pause adds 0.4-0.6 s.
    paused = [s for s in intervals if s > 0.3]
    assert 0.02 <= len(paused) / len(intervals) <= 0.04
    assert all(0.4 <= s <= 0.6 + 0.1 * math.exp(0.3) for s in paused)


@pytest.mark.parametrize("name", ["natural", "careful", "brisk"])
def test_every_interval_stays_inside_the_rhythm_bounds(name):
    rhythm = TypingRhythm.preset(name)
    low, high = rhythm.bounds()
    rng = random.Random(5)
    text = "The Quick, brown fox! Jumps over 3.14 lazy dogs?\nЖж ж.\t"
    for _ in range(60):
        for index, typed in enumerate(text):
            upcoming = text[index + 1] if index + 1 < len(text) else None
            assert low <= rhythm.interval(typed, upcoming, rng) <= high
    assert low >= 0.015


def test_presets_order_by_speed_and_resolve():
    careful, natural, brisk = (
        TypingRhythm.preset(name) for name in ("careful", "natural", "brisk")
    )
    assert careful.median_ms > natural.median_ms > brisk.median_ms
    assert resolve_typing(None) == natural
    assert resolve_typing("brisk") == brisk
    assert resolve_typing(careful) is careful
    with pytest.raises(InvalidArgumentError, match="preset must be one of"):
        TypingRhythm.preset("sloppy")


@pytest.mark.parametrize("bad", ["sloppy", 3, SigmaDriftPlanner()])
def test_resolve_typing_rejects_unknown_values(bad):
    with pytest.raises(InvalidArgumentError, match="typing must be one of"):
        resolve_typing(bad)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("characters_per_minute", 59),
        ("characters_per_minute", 901),
        ("characters_per_minute", True),
        ("sigma", 0.05),
        ("sigma", float("nan")),
        ("word_pause", (2.5, 1.6)),
        ("sentence_pause", (2.5,)),
        ("shift_pause", (1.2, 2.5)),
        ("alternate_hand", 1.1),
        ("same_finger", 0.9),
        ("micro_pause_probability", 0.2),
        ("micro_pause", (-0.1, 0.5)),
        ("jitter_ms", -1),
        ("dwell", 0.5),
    ],
)
def test_rhythm_rejects_out_of_range_parameters(field, value):
    with pytest.raises(InvalidArgumentError, match=f"TypingRhythm.{field}"):
        TypingRhythm(**{field: value})


def test_jitter_must_stay_below_the_median_interval():
    TypingRhythm(characters_per_minute=600, jitter_ms=99)
    with pytest.raises(InvalidArgumentError, match="below the median interval"):
        TypingRhythm(characters_per_minute=600, jitter_ms=100)


def test_legacy_arguments_map_onto_the_model():
    natural = TypingRhythm.preset("natural")
    assert natural.adjusted() is natural
    mapped = natural.adjusted(600, 40)
    assert (mapped.characters_per_minute, mapped.jitter_ms) == (600, 40)
    assert mapped.sigma == natural.sigma and mapped.word_pause == natural.word_pause


@pytest.mark.parametrize(
    ("cpm", "offset", "match"),
    [
        (59, 20, "characters_per_minute"),
        (901, 20, "characters_per_minute"),
        ("fast", 20, "characters_per_minute"),
        (600, 100, "offset must stay below"),
        (600, -1, "offset"),
    ],
)
def test_legacy_arguments_are_validated(cpm, offset, match):
    with pytest.raises(InvalidArgumentError, match=match):
        TypingRhythm.preset("natural").adjusted(cpm, offset)


def test_type_text_validates_before_sending_anything():
    sender = FakeSender()
    interactor, _, sleep, _ = make("win32", unicode_sender=sender)
    with pytest.raises(InvalidArgumentError, match="characters_per_minute"):
        interactor.type_text("abc", characters_per_minute=1000)
    with pytest.raises(InvalidArgumentError, match="offset"):
        interactor.type_text("abc", characters_per_minute=600, offset=150)
    assert sender.batches == [] and sleep.calls == []
    interactor.type_text("abc", characters_per_minute=1000, offset=500, human=False)
    assert len(sender.batches) == 1 and sleep.calls == []


def test_type_text_uses_the_interactor_rhythm_and_its_overrides():
    slow = TypingRhythm(characters_per_minute=120, micro_pause_probability=0.0)
    sender = FakeSender()
    interactor, _, sleep, _ = make("win32", unicode_sender=sender, typing=slow)
    assert interactor.typing is slow
    interactor.type_text(NEUTRAL * 1500)
    assert statistics.median(sleep.calls) == pytest.approx(0.5, rel=0.05)
    sleep.calls.clear()
    interactor.type_text(NEUTRAL * 1500, characters_per_minute=600, offset=0)
    assert statistics.median(sleep.calls) == pytest.approx(0.1, rel=0.05)
    interactor, _, _, _ = make("win32", unicode_sender=FakeSender(), typing="brisk")
    assert interactor.typing == TypingRhythm.preset("brisk")
    assert make("win32", unicode_sender=FakeSender())[0].typing == TypingRhythm()


def test_windows_typing_pauses_at_word_and_sentence_ends():
    sender = FakeSender()
    interactor, _, sleep, _ = make("win32", unicode_sender=sender, typing=PLAIN)
    unit = NEUTRAL * 2 + ". " + NEUTRAL * 2 + " "
    interactor.type_text(unit * 300)
    # Sleep i follows stroke i of the unit: zh zh . space zh zh space.
    by_stroke = [sleep.calls[i :: len(unit)] for i in range(len(unit))]
    base = statistics.median(by_stroke[0])
    assert 2.5 <= statistics.median(by_stroke[2]) / base <= 4.0
    assert 1.6 <= statistics.median(by_stroke[3]) / base <= 2.5
    assert 1.6 <= statistics.median(by_stroke[6]) / base <= 2.5


def test_posix_typing_passes_the_next_keystroke_to_the_rhythm():
    seen = []

    class Recording(TypingRhythm):
        def interval(self, typed, upcoming, rng):
            seen.append((typed, upcoming))
            return 0.1

    keyboard_log = []
    interactor, _, _, _ = make(
        "linux",
        keyboard=FakeKeyboard(keyboard_log),
        clipboard=FakeClipboard(),
        typing=Recording(),
    )
    interactor.type_text("aéü\nb")
    assert seen == [
        ("a", "é"),
        ("é", "ü"),
        ("ü", "\n"),
        ("\n", "b"),
        ("b", None),
    ]


def test_rhythm_is_deterministic_for_a_seed():
    def run():
        sender = FakeSender()
        interactor, _, sleep, _ = make("win32", unicode_sender=sender)
        interactor.type_text("Hello, World. Again!")
        return sleep.calls

    assert run() == run()


def test_dwell_is_lognormal_around_the_preset_hold():
    rhythm = TypingRhythm.preset("natural")
    rng = random.Random(6)
    holds = [rhythm.hold(rng) for _ in range(4000)]
    assert statistics.median(holds) == pytest.approx(rhythm.dwell, rel=0.03)
    cut = 3.0 * rhythm.dwell_sigma
    assert min(holds) >= rhythm.dwell * math.exp(-cut) - 1e-12
    assert max(holds) <= rhythm.dwell * math.exp(cut) + 1e-12
    middle = sorted(holds)[400:3600]
    assert 0.065 <= middle[0] and middle[-1] <= 0.115


class FakeUser32:
    def __init__(self, deliver=None, held=()):
        self.received = []
        self.deliver = deliver
        self.held: set[int] = set(held)

    def GetAsyncKeyState(self, vk):
        return -32768 if vk in self.held else 0  # SHORT with the high bit set

    def SendInput(self, count, array, size):
        self.received.append(
            [
                (array[i].type, array[i].ki.wVk, array[i].ki.wScan, array[i].ki.dwFlags)
                for i in range(count)
            ]
        )
        self.size = size
        return count if self.deliver is None else self.deliver


def test_sender_builds_keyboard_inputs():
    user32 = FakeUser32()
    sender = WindowsKeyboardSender(user32=user32)
    sender.send(keyboard_events("\U0001f600\n"))
    assert user32.received == [
        [
            (1, 0, 0xD83D, U),
            (1, 0, 0xD83D, U | UP),
            (1, 0, 0xDE00, U),
            (1, 0, 0xDE00, U | UP),
            (1, VK_RETURN, 0, 0),
            (1, VK_RETURN, 0, UP),
        ]
    ]
    assert user32.size == ctypes.sizeof(ei._win_input_types()[0])


def test_sender_chunks_long_text():
    user32 = FakeUser32()
    WindowsKeyboardSender(user32=user32).send(keyboard_events("x" * 40))
    assert [len(batch) for batch in user32.received] == [64, 16]


def test_sender_never_splits_a_surrogate_pair():
    user32 = FakeUser32()
    WindowsKeyboardSender(user32=user32).send(keyboard_events("x" * 31 + "\U0001f600"))
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
    interactor, _, _, _ = make(
        "win32", unicode_sender=WindowsKeyboardSender(user32=user32)
    )
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
        ("write", "h"),
        ("write", "i"),
        ("press", "enter"),
        ("press", "tab"),
        ("keyDown", "ctrl"),
        ("keyDown", "v"),
        ("keyUp", "v"),
        ("keyUp", "ctrl"),
        ("write", "!"),
    ]
    assert ("copy", "пр") in clipboard.log
    assert all(p is False for p in gui.pauses)


def test_macos_paste_uses_command():
    clipboard = FakeClipboard()
    interactor, _, _, log = make("darwin", clipboard=clipboard, keyboard=None)
    interactor.paste_text("ü")
    assert [e for e in log if e[0] == "keyDown"] == [
        ("keyDown", "command"),
        ("keyDown", "v"),
    ]


def test_posix_uses_keyboard_package_when_usable():
    log = []
    interactor, _, _, ilog = make(
        "linux", keyboard=FakeKeyboard(log), clipboard=FakeClipboard()
    )
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
    low, high = interactor.typing.adjusted(300, 0).bounds()
    assert len(sleep.calls) == 3 and all(low <= s <= high for s in sleep.calls)
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
        ("keyDown", "ctrl"),
        ("keyDown", "shift"),
        ("keyDown", "t"),
        ("keyUp", "t"),
        ("keyUp", "shift"),
        ("keyUp", "ctrl"),
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
    assert [e for e in log if e[0] != "sleep"] == [
        ("down", "right"),
        ("raw_up", 0, 0, "right"),
    ]


def test_drag_release_uses_platform_layer():
    interactor, gui, _, log = make_platform()
    interactor.drag((10, 10), (60, 60), human=False)
    assert log[-1] == ("raw_up", 60, 60, "left")


def test_key_release_uses_platform_layer_past_failsafe():
    interactor, _, _, log = make_platform()
    interactor.hotkey("Ctrl", "V")
    assert log == [
        ("keyDown", "Ctrl"),
        ("keyDown", "V"),
        ("raw_keyUp", "V"),
        ("raw_keyUp", "ctrl"),
    ]


def test_paste_release_uses_platform_layer():
    clipboard = FakeClipboard("previous")
    interactor, gui, _, log = make(
        platform="linux", gui=CornerGui(), clipboard=clipboard
    )
    gui.platformModule = FakePlatform(gui)
    interactor.paste_text("new")
    assert ("raw_keyUp", "ctrl") in log and ("raw_keyUp", "v") in log
    assert clipboard.content == "previous"


def test_keyboard_interrupt_mid_release_still_releases_every_key():
    interactor, _, _, log = make_platform(interrupt_keys={"t"})
    with pytest.raises(KeyboardInterrupt):
        interactor.hotkey("ctrl", "shift", "t")
    assert [e for e in log if e[0] == "raw_keyUp"] == [
        ("raw_keyUp", "t"),
        ("raw_keyUp", "shift"),
        ("raw_keyUp", "ctrl"),
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
        ("keyDown", "ctrl"),
        ("keyDown", "alt"),
        ("raw_keyUp", "alt"),
        ("raw_keyUp", "ctrl"),
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
        ("down", "left"),
        ("raw_up", 10, 10, "left"),
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


# -- playback clock --------------------------------------------------------------


def test_fake_sleep_drives_an_explicit_clock():
    interactor, _, sleep, _ = make()
    assert interactor._clock is sleep.clock
    interactor.move(1700, 900)
    assert sleep.clock.now == pytest.approx(sum(sleep.calls))
    assert 0.12 <= sleep.clock.now <= 1.2 + 1e-9


# -- real monitors: no planned point in a dead zone ------------------------------

PRIMARY = MonitorRect(0, 0, 1920, 1080)
# Shorter right-hand monitor: x >= 1920, y >= 720 belongs to no monitor.
SIDE = MonitorRect(1920, 0, 1280, 720)


class MonitorGui(FakeGui):
    def __init__(self, position=(1800, 1000), monitors=(PRIMARY, SIDE)):
        super().__init__(position=position, size=(1920, 1080))
        self.layout = list(monitors)

    def virtual_screen(self):
        return (0, 0, 3200, 1080)

    def monitors(self):
        return self.layout


class ScriptedPlanner:
    """Returns fixed steps and records each call's arguments."""

    def __init__(self, steps):
        self.steps = steps
        self.calls = []

    def plan(self, start, end, *, target_size, rng, bounds=None):
        self.calls.append((start, end, target_size, bounds))
        return list(self.steps)


def on_a_monitor(point, monitors=(PRIMARY, SIDE)):
    return any(
        m.left <= point[0] < m.left + m.width and m.top <= point[1] < m.top + m.height
        for m in monitors
    )


def test_onto_monitor_picks_the_nearest_rectangle():
    monitors = [tuple(PRIMARY), tuple(SIDE)]
    assert ei._onto_monitor((500, 500), monitors) == (500, 500)
    assert ei._onto_monitor((2000, 900), monitors) == (1919, 900)  # 81 px vs 180
    assert ei._onto_monitor((2600, 1000), monitors) == (2600, 719)
    assert ei._onto_monitor((3300, -40), monitors) == (3199, 0)


def test_dead_zone_steps_are_clamped_onto_a_monitor_before_the_corner_rule():
    planner = ScriptedPlanner(
        [
            MotionStep(2000, 900, 0.01),  # dead zone, nearest: primary
            MotionStep(2600, 1000, 0.01),  # dead zone, nearest: side monitor
            MotionStep(1950, 1079, 0.01),  # clamps onto the primary's corner
            MotionStep(2500, 300, 0.01),
        ]
    )
    interactor, _, sleep, log = make(gui=MonitorGui(), motion=planner)
    interactor.move(2500, 300, target_size=40)
    assert moves(log) == [(1919, 900), (2600, 719), (1919, 1078), (2500, 300)]
    assert sleep.calls == pytest.approx([0.01] * 4)
    ((_, _, size, bounds),) = planner.calls
    assert size == 40
    assert bounds == ScreenBounds(0, 0, 3200, 1080, primary=(1920, 1080))


def test_real_planner_path_never_enters_a_dead_zone():
    for seed in range(30):
        interactor, _, _, log = make(gui=MonitorGui(position=(1700, 1050)))
        interactor._rng = random.Random(seed)
        interactor.move(2900, 650)
        path = moves(log)
        assert path[-1] == (2900, 650)
        assert all(on_a_monitor(p) for p in path)
        corners = {(0, 0), (1919, 0), (0, 1079), (1919, 1079)}
        assert not corners & set(path)


def test_unknown_or_failing_monitor_layout_leaves_the_plan_alone():
    steps = [MotionStep(2000, 900, 0.01), MotionStep(2500, 300, 0.01)]

    class FailingGui(MonitorGui):
        def monitors(self):
            raise NativeInputError("EnumDisplayMonitors failed (error 5)")

    for gui in (MonitorGui(monitors=()), FailingGui()):
        interactor, _, _, log = make(gui=gui, motion=ScriptedPlanner(steps))
        interactor.move(2500, 300)
        assert moves(log) == [(2000, 900), (2500, 300)]


class FakeMonitorApi:
    def __init__(self, layout):
        self.layout = layout
        self.calls = 0

    def monitors(self):
        self.calls += 1
        return list(self.layout)


def test_win32_backend_reports_monitors_from_its_monitor_api():
    user32 = cast(win32.User32Api, object())  # never called here
    api = FakeMonitorApi([PRIMARY, SIDE])
    assert Win32Backend(user32=user32, monitors=api).monitors() == [PRIMARY, SIDE]
    assert api.calls == 1
    # An injected user32 without a monitor API: the layout is unknown.
    assert Win32Backend(user32=user32).monitors() == []


def test_monitorinfo_layout():
    assert ctypes.sizeof(win32.MONITORINFO) == 40
    assert win32.MONITORINFO.rcMonitor.offset == 4
    assert win32.MONITORINFO.rcWork.offset == 20


@pytest.mark.skipif(sys.platform != "win32", reason="EnumDisplayMonitors is Windows")
def test_real_monitor_enumeration_is_read_only_and_sane():
    found = win32.Win32Windows().monitors()
    assert found and all(m.width > 0 and m.height > 0 for m in found)
    # Some monitor holds the primary's origin.
    assert on_a_monitor((0, 0), found)


# -- Fitts' target size from the element --------------------------------------

ELEMENT_GEOMETRY = {
    "screenX": 0,
    "screenY": 0,
    "outerWidth": 1000,
    "outerHeight": 800,
    "innerWidth": 1000,
    "innerHeight": 700,
    "devicePixelRatio": 1.5,
}


class PageBridge(Bridge):
    """Answers the two calls a hover without verification makes."""

    def __init__(self, rect):
        super().__init__(transport=object())
        self.rect = rect

    def activate_tab(self, timeout=10.0, *, tab_id=None):
        return {}

    def scroll_into_view(self, element_id, timeout=10.0, *, tab_id=None):
        return {
            "elementId": element_id,
            "rect": dict(self.rect),
            "geometry": dict(ELEMENT_GEOMETRY),
            "tabId": 7,
        }


class AimRecorder(DragInteractor):
    def __init__(self):
        super().__init__(backend=FakeGui())
        self.aims = []
        self.at = (0, 0)

    def move(self, x, y, *, human=True, target_size=DEFAULT_TARGET_SIZE):
        self.aims.append((human, target_size))
        self.at = (round(x), round(y))

    def position(self):
        return self.at


def hover(rect, *, human=True):
    recorder = AimRecorder()
    ctx = BrowserContext(PageBridge(rect), verify_input=False, interactor=recorder)
    element = Element(ctx, "e1", rect=rect, tab_id=7, geometry=ELEMENT_GEOMETRY)
    element.hover(human=human)
    return recorder.aims


def per_css_px():
    geometry = Geometry.from_payload(dict(ELEMENT_GEOMETRY), 1.0)
    return input_per_dip(geometry) * geometry.zoom


def test_element_aims_with_its_smaller_visible_side_in_input_px():
    aims = hover({"x": 100, "y": 50, "width": 200, "height": 30})
    assert aims == [(True, pytest.approx(30 * per_css_px()))]


def test_element_target_size_uses_only_the_visible_part():
    # 100 CSS px tall but cut by the viewport (4 px margin): 76 px visible.
    aims = hover({"x": 100, "y": -20, "width": 200, "height": 100})
    assert aims == [(True, pytest.approx(76 * per_css_px()))]


def test_element_target_size_is_at_least_one_input_px():
    aims = hover({"x": 100, "y": 50, "width": 0.25, "height": 0.25})
    assert aims == [(True, 1.0)]


def test_instant_element_move_passes_no_target_size():
    assert hover({"x": 100, "y": 50, "width": 200, "height": 30}, human=False) == [
        (False, DEFAULT_TARGET_SIZE)
    ]


# -- standalone motion -----------------------------------------------------------


def test_standalone_motion_keyword():
    assert Emunium().interactor.motion == SigmaDriftPlanner.preset("natural")
    assert Emunium(motion="brisk").interactor.motion == SigmaDriftPlanner.preset(
        "brisk"
    )
    planner = ScriptedPlanner([MotionStep(1, 1, 0.0)])
    assert Emunium(motion=planner).interactor.motion is planner
    with pytest.raises(InvalidArgumentError, match="motion"):
        Emunium(motion="warp")


# -- typing rhythm through the public API ---------------------------------------

# Capitals, a word break and a sentence end: shift, word and sentence pauses.
TYPED_TEXT = "Ad hoc. Done now"
TYPING_RECT = {"x": 100.0, "y": 50.0, "width": 200.0, "height": 30.0}
# The interactor's rng is reseeded with this as the first key goes out, so the
# expected waits can be drawn from the same seed whatever the click consumed.
TYPING_SEED = 11


class TypingGui(FakeGui):
    def __init__(self, rng, log):
        super().__init__(log=log)
        self.rng = rng
        self.typed = []

    def write(self, message, _pause=True):
        if not self.typed:
            self.rng.seed(TYPING_SEED)
        self.typed.append(message)
        super().write(message, _pause)


class TypingRig:
    """Fake OS input for every interactor a facade builds: no real input."""

    def __init__(self):
        self.log = []
        self.rng = random.Random(3)
        self.gui = TypingGui(self.rng, self.log)
        self.sleep = FakeSleep(self.log)

    def build(self, cls):
        def factory(**kwargs):
            return cls(
                backend=self.gui,
                platform="linux",
                keyboard=None,
                clipboard=FakeClipboard(),
                sleep=self.sleep,
                clock=self.sleep.clock,
                rng=self.rng,
                double_click_time=0.5,
                **kwargs,
            )

        return factory

    def key_waits(self):
        first = next(i for i, e in enumerate(self.log) if e[0] == "write")
        return [e[1] for e in self.log[first:] if e[0] == "sleep"]


class TypingBridge(PageBridge):
    """A focused input whose value is whatever the rig has typed."""

    def __init__(self, gui):
        super().__init__(TYPING_RECT)
        self.gui = gui

    def focus_state(self, element_id, timeout=10.0, *, tab_id=None):
        return {"documentHasFocus": True, "activeIsElement": True}

    def describe_element(self, element_id, timeout=10.0, *, tab_id=None):
        return {"elementId": element_id, "value": "".join(self.gui.typed)}


@pytest.fixture
def rig(monkeypatch):
    rig = TypingRig()
    monkeypatch.setattr(browser_context, "DragInteractor", rig.build(DragInteractor))
    monkeypatch.setattr(browser_facade, "Bridge", lambda port=0: TypingBridge(rig.gui))
    monkeypatch.setattr(
        standalone_facade, "ElementInteractor", rig.build(ElementInteractor)
    )
    return rig


def typing_target(ctx):
    return Element(ctx, "e1", rect=TYPING_RECT, tab_id=7, geometry=ELEMENT_GEOMETRY)


def via_element(rig, typing, overrides):
    ctx = BrowserContext(TypingBridge(rig.gui), verify_input=False, typing=typing)
    typing_target(ctx).type(TYPED_TEXT, **overrides)
    return TYPE_CHUNK


def via_browser_type(rig, typing, overrides):
    browser = Browser(verify_input=False, typing=typing)
    browser.type(typing_target(browser.context), TYPED_TEXT, **overrides)
    return TYPE_CHUNK


def via_browser_type_at(rig, typing, overrides):
    browser = Browser(verify_input=False, typing=typing)
    browser.type_at(typing_target(browser.context), TYPED_TEXT, **overrides)
    return TYPE_CHUNK


def via_emunium_type_text(rig, typing, overrides):
    Emunium(typing=typing).type_text(TYPED_TEXT, **overrides)
    return len(TYPED_TEXT)


def via_emunium_type_at(rig, typing, overrides):
    Emunium(typing=typing).type_at({"x": 300, "y": 200}, TYPED_TEXT, **overrides)
    return len(TYPED_TEXT)


TYPING_PATHS = [
    via_element,
    via_browser_type,
    via_browser_type_at,
    via_emunium_type_text,
    via_emunium_type_at,
]


def expected_waits(rhythm, chunk):
    """The waits ``rhythm`` draws for TYPED_TEXT sent in ``chunk``-sized parts."""
    rng = random.Random(TYPING_SEED)
    waits = []
    for start in range(0, len(TYPED_TEXT), chunk):
        part = TYPED_TEXT[start : start + chunk]
        for index, typed in enumerate(part):
            upcoming = part[index + 1 : index + 2] or None
            waits.append(rhythm.interval(typed, upcoming, rng))
    return waits


@pytest.mark.parametrize("path", TYPING_PATHS, ids=lambda path: path.__name__)
@pytest.mark.parametrize("name", ["careful", "brisk"])
def test_typing_preset_sets_speed_and_pauses_on_every_public_path(rig, path, name):
    chunk = path(rig, name, {})
    preset = TypingRhythm.preset(name)
    assert "".join(rig.gui.typed) == TYPED_TEXT
    assert rig.key_waits() == expected_waits(preset, chunk)
    # The pre-3.1 defaults (280 cpm, 20 ms) no longer override the preset.
    assert rig.key_waits() != expected_waits(preset.adjusted(280, 20), chunk)


@pytest.mark.parametrize("path", TYPING_PATHS, ids=lambda path: path.__name__)
@pytest.mark.parametrize(
    "overrides",
    [
        {"characters_per_minute": 600, "offset": 0},
        {"characters_per_minute": 600},
        {"offset": 5},
    ],
)
def test_explicit_typing_arguments_override_the_preset(rig, path, overrides):
    chunk = path(rig, "careful", overrides)
    rhythm = TypingRhythm.preset("careful").adjusted(
        overrides.get("characters_per_minute"), overrides.get("offset")
    )
    assert rhythm != TypingRhythm.preset("careful")
    assert rig.key_waits() == expected_waits(rhythm, chunk)


def context_interactor(ctx):
    interactor = ctx.interactor
    assert isinstance(interactor, DragInteractor)
    return interactor


def test_typing_keyword_reaches_each_default_interactor():
    own = TypingRhythm(characters_per_minute=150)
    builders = [
        lambda typing: context_interactor(
            BrowserContext(PageBridge(TYPING_RECT), typing=typing)
        ),
        lambda typing: context_interactor(Browser(typing=typing).context),
        lambda typing: Emunium(typing=typing).interactor,
    ]
    for build in builders:
        assert build(None).typing == TypingRhythm.preset("natural")
        assert build("brisk").typing == TypingRhythm.preset("brisk")
        assert build(own).typing is own
        with pytest.raises(InvalidArgumentError, match="typing must be one of"):
            build("sloppy")
    with pytest.raises(InvalidArgumentError, match="not both"):
        BrowserContext(
            PageBridge(TYPING_RECT), interactor=DragInteractor(), typing="brisk"
        )
