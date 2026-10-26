"""Standalone (screen) mode against fakes.

No test moves the real mouse, types, or takes a real screenshot: pyautogui is
replaced through ``emunium._screen._PYAUTOGUI``, the interactor and OCR reader
are fakes, and screens are synthetic numpy images.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import types

import pytest

from emunium import _screen
from emunium._element_interactor import ElementInteractor
from emunium._screen import NATIVE_INPUT_LOCK
from emunium._standalone import facade, vision
from emunium._standalone import ocr as standalone_ocr
from emunium._standalone.config import ClickType, Config
from emunium.standalone import Emunium

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_SIZE = 32


class FakeShot:
    """Stands in for a PIL screenshot: ``width`` plus the array protocol."""

    def __init__(self, pixels):
        self._pixels = pixels
        self.height, self.width = pixels.shape[:2]

    def __array__(self, dtype=None, copy=None):
        return self._pixels if dtype is None else self._pixels.astype(dtype)


class FakeGui:
    def __init__(self, size=(1920, 1080), pixels=None):
        self.screen = size
        self.pixels = pixels
        self.log = []
        self.pos = (5, 5)

    def size(self):
        return self.screen

    def screenshot(self, *args, **kwargs):
        if args or kwargs:
            raise AssertionError("screenshots are taken full-screen and cropped")
        self.log.append(("screenshot",))
        return FakeShot(self.pixels)

    def scroll(self, clicks, _pause=True):
        self.log.append(("scroll", clicks, _pause))

    def position(self):
        return self.pos

    def moveTo(self, x, y, _pause=True):
        self.pos = (x, y)
        self.log.append(("move", x, y))

    def mouseDown(self, button="left", _pause=True):
        self.log.append(("down", button))

    def mouseUp(self, button="left", _pause=True):
        self.log.append(("up", button))


class FakeInteractor(ElementInteractor):
    """Records calls; any method not overridden still only reaches a FakeGui."""

    def __init__(self):
        super().__init__(backend=FakeGui(), sleep=lambda _seconds: None)
        self.calls = []
        self.lock_held_while_typing = None

    def move(self, x, y, *, human=True):
        self.calls.append(("move", x, y))

    def click(self, x, y, *, click_type=ClickType.LEFT, human=True):
        self.calls.append(("click", x, y, click_type))

    def drag(self, start, end, *, human=True):
        self.calls.append(("drag", tuple(start), tuple(end)))

    def type_text(self, text, *, characters_per_minute=280, offset=20, human=True):
        self.calls.append(("type", text, characters_per_minute, offset))
        acquired = []

        def probe_lock():
            # An RLock is released by the thread that acquired it.
            got = NATIVE_INPUT_LOCK.acquire(blocking=False)
            if got:
                NATIVE_INPUT_LOCK.release()
            acquired.append(got)

        probe = threading.Thread(target=probe_lock)
        probe.start()
        probe.join()
        self.lock_held_while_typing = not acquired[0]


class FakeHumanCursor(types.ModuleType):
    """Stands in for the ``humancursor`` package in ``sys.modules``."""

    def __init__(self, cursor_class):
        super().__init__("humancursor")
        self.SystemCursor = cursor_class


class FakeReader:
    def __init__(self, results):
        self.results = results
        self.images = []

    def readtext(self, image):
        self.images.append(image)
        return self.results


@pytest.fixture
def install_gui(monkeypatch):
    def install(gui):
        monkeypatch.setattr(_screen, "_PYAUTOGUI", gui)
        return gui

    return install


@pytest.fixture
def cv():
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    return cv2, np


def _scene(np, width, height, exact_at, noisy_at):
    """Noise screen with an exact and a noisy copy of a noise template (gray)."""
    rng = np.random.default_rng(7)
    screen = rng.integers(0, 256, size=(height, width), dtype=np.uint8)
    template = rng.integers(0, 256, size=(TEMPLATE_SIZE, TEMPLATE_SIZE), dtype=np.uint8)
    noise = rng.normal(0.0, 25.0, size=template.shape)
    noisy = np.clip(template.astype(np.float64) + noise, 0, 255).astype(np.uint8)
    for (x, y), patch in ((exact_at, template), (noisy_at, noisy)):
        screen[y : y + TEMPLATE_SIZE, x : x + TEMPLATE_SIZE] = patch
    return np.dstack([screen] * 3), template


def _write_template(cv2, tmp_path, template):
    path = str(tmp_path / "template.png")
    assert cv2.imwrite(path, template)
    return path


# -- import hygiene ------------------------------------------------------------


def test_import_does_not_load_pyautogui_or_humancursor():
    code = (
        "import emunium.standalone, sys; "
        "print('pyautogui' in sys.modules, 'humancursor' in sys.modules)"
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
    assert completed.stdout.strip() == "False False"


def test_cursor_is_created_lazily(monkeypatch, install_gui):
    gui = install_gui(FakeGui())
    created = []

    class SystemCursor:
        def __init__(self):
            created.append(self)

    monkeypatch.setitem(sys.modules, "humancursor", FakeHumanCursor(SystemCursor))

    emu = Emunium()
    assert created == []
    cursor = emu.cursor
    assert isinstance(cursor, SystemCursor)
    assert emu.cursor is cursor
    assert len(created) == 1
    assert gui.log == []


# -- OCR construction ------------------------------------------------------------


@pytest.fixture
def fake_init(monkeypatch):
    calls = []

    def initialize_ocr(langs, use_gpu):
        calls.append((langs, use_gpu))
        return FakeReader([])

    monkeypatch.setattr(standalone_ocr, "initialize_ocr", initialize_ocr)
    return calls


def test_ocr_alias_enables_ocr(fake_init, install_gui):
    install_gui(FakeGui(size=(1920, 1080)))
    emu = Emunium(ocr=True)
    assert emu.ocr is True
    assert isinstance(emu.ocr_reader, FakeReader)
    assert emu.monitor_region == (0, 0, 1920, 1080)
    assert fake_init == [(["en"], True)]


def test_ocr_enabled_keyword_and_langs(fake_init, install_gui):
    install_gui(FakeGui())
    emu = Emunium(ocr_enabled=True, use_gpu=False, langs=["ru", "en"])
    assert emu.ocr is True
    assert fake_init == [(["ru", "en"], False)]


def test_ocr_off_by_default(fake_init):
    emu = Emunium()
    assert emu.ocr is False
    assert emu.ocr_reader is None
    assert fake_init == []


def test_conflicting_ocr_arguments_raise(fake_init):
    with pytest.raises(ValueError):
        Emunium(ocr_enabled=True, ocr=False)
    assert fake_init == []


def test_missing_easyocr_fails_at_init(monkeypatch, install_gui):
    gui = install_gui(FakeGui())
    monkeypatch.setitem(sys.modules, "easyocr", None)
    with pytest.raises(ImportError, match=r"ocr_enabled=True"):
        Emunium(ocr=True)
    assert gui.log == []


def test_find_text_without_ocr_names_the_flag():
    with pytest.raises(RuntimeError, match=r"ocr_enabled=True"):
        Emunium().find_text_elements("Sign in")


def test_find_text_scales_and_offsets_region(cv, fake_init, install_gui):
    _, np = cv
    pixels = np.zeros((480, 640, 3), dtype=np.uint8)
    install_gui(FakeGui(size=(320, 240), pixels=pixels))  # Retina: scale 0.5
    emu = Emunium(ocr=True)
    reader = FakeReader(
        [
            ([[10, 10], [30, 10], [30, 20], [10, 20]], "Sign in now", 0.95),
            ([[50, 50], [70, 50], [70, 60], [50, 60]], "Sign in", 0.40),
            ([[0, 0], [4, 0], [4, 4], [0, 4]], "Cancel", 0.99),
        ]
    )
    emu.ocr_reader = reader

    hits = emu.find_text_elements("sign IN", region=(100, 50, 60, 40))

    # Crop origin (200, 100) px; box center (20, 15) px -> (220, 115) px -> INPUT / 2.
    assert hits == [{"x": 110, "y": 58}]
    assert reader.images[0].shape == (80, 120, 3)


# -- template matching -----------------------------------------------------------


def test_matches_are_ranked_by_score_not_raster_order(cv, tmp_path, install_gui):
    cv2, np = cv
    pixels, template = _scene(np, 320, 240, exact_at=(200, 150), noisy_at=(20, 20))
    install_gui(FakeGui(size=(320, 240), pixels=pixels))

    matches = Emunium().find_elements(_write_template(cv2, tmp_path, template))

    assert matches == [{"x": 216, "y": 166}, {"x": 36, "y": 36}]


def test_max_elements_keeps_the_best(cv, tmp_path, install_gui):
    cv2, np = cv
    pixels, template = _scene(np, 320, 240, exact_at=(200, 150), noisy_at=(20, 20))
    install_gui(FakeGui(size=(320, 240), pixels=pixels))

    matches = Emunium().find_elements(
        _write_template(cv2, tmp_path, template), max_elements=1
    )

    assert matches == [{"x": 216, "y": 166}]


def test_retina_screenshot_is_scaled_to_input_units(cv, tmp_path, install_gui):
    cv2, np = cv
    pixels, template = _scene(np, 640, 480, exact_at=(400, 300), noisy_at=(20, 20))
    install_gui(FakeGui(size=(320, 240), pixels=pixels))
    path = _write_template(cv2, tmp_path, template)

    assert Emunium().find_elements(path) == [{"x": 208, "y": 158}, {"x": 18, "y": 18}]


def test_region_is_in_input_units(cv, tmp_path, install_gui):
    cv2, np = cv
    pixels, template = _scene(np, 640, 480, exact_at=(400, 300), noisy_at=(20, 20))
    install_gui(FakeGui(size=(320, 240), pixels=pixels))
    path = _write_template(cv2, tmp_path, template)
    emu = Emunium()

    assert emu.find_elements(path, region=(180, 130, 140, 110)) == [
        {"x": 208, "y": 158}
    ]
    assert emu.find_elements(path, region=(0, 0, 160, 120)) == [{"x": 18, "y": 18}]


def test_invalid_region_raises(cv, install_gui):
    _, np = cv
    install_gui(
        FakeGui(size=(320, 240), pixels=np.zeros((240, 320, 3), dtype=np.uint8))
    )
    with pytest.raises(ValueError, match="outside"):
        vision.capture_screen((400, 400, 10, 10))
    with pytest.raises(ValueError, match="positive"):
        vision.capture_screen((0, 0, 0, 10))


def test_top_candidates_caps_by_score(cv):
    _, np = cv
    result = np.random.default_rng(3).random((40, 50)).astype(np.float32)

    xs, ys, scores = vision.top_candidates(result, 0.2, 500)

    assert scores.size == 500
    expected = np.sort(result.ravel())[-500:]
    assert np.array_equal(np.sort(scores), expected)
    assert np.array_equal(result[ys, xs], scores)


def test_suppress_overlaps_relative_to_box_size(cv):
    _, np = cv
    boxes = np.array(
        [
            (0, 0, 40, 40),  # 0: 56 % of itself covered by box 1 -> duplicate
            (10, 10, 40, 40),  # 1
            (100, 0, 40, 40),  # 2: contains box 3, the smaller box -> duplicate
            (105, 5, 10, 10),  # 3: best score
            (50, 0, 40, 40),  # 4: touches box 1 only at an edge -> kept
        ]
    )
    scores = np.array([0.90, 0.95, 0.85, 0.99, 0.50])

    assert vision.suppress_overlaps(boxes, scores, 0.5).tolist() == [3, 1, 4]
    assert vision.suppress_overlaps(boxes, scores, 0.5, limit=2).tolist() == [3, 1]


def test_large_boxes_offset_beyond_a_fixed_pixel_threshold_are_duplicates(cv):
    _, np = cv
    boxes = np.array([(0, 0, 200, 200), (30, 30, 200, 200), (300, 0, 200, 200)])
    scores = np.array([0.9, 0.8, 0.7])

    assert vision.suppress_overlaps(boxes, scores, 0.5).tolist() == [0, 2]


# -- input -----------------------------------------------------------------------


@pytest.fixture
def no_sleep(monkeypatch):
    calls = []
    monkeypatch.setattr(facade, "_sleep", calls.append)
    return calls


@pytest.mark.parametrize(
    ("platform", "y", "expected"),
    [
        ("win32", 540 + 300, [-120] * 3),
        ("darwin", 540 - 260, [1] * 3),
        ("linux", 540 + 140, [-1]),
        ("win32", 540 + 40, []),
    ],
)
def test_scroll_to_uses_wheel_notches(
    monkeypatch, install_gui, no_sleep, platform, y, expected
):
    gui = install_gui(FakeGui(size=(1920, 1080)))
    monkeypatch.setattr(facade, "_platform", lambda: platform)

    Emunium().scroll_to({"x": 10, "y": y})

    assert [entry[1] for entry in gui.log] == expected
    assert all(entry[2] is False for entry in gui.log)
    assert len(no_sleep) == len(expected)


def test_wheel_units_per_notch():
    assert facade.wheel_units_per_notch("win32") == Config.WINDOWS_WHEEL_DELTA == 120
    assert facade.wheel_units_per_notch("darwin") == 1


def test_type_text_delegates_to_interactor():
    emu = Emunium()
    emu.interactor = fake = FakeInteractor()

    emu.type_text("Привет, мир\n", 300, 5)

    assert fake.calls == [("type", "Привет, мир\n", 300, 5)]


def test_type_at_clicks_then_types_under_one_lock():
    emu = Emunium()
    emu.interactor = fake = FakeInteractor()

    emu.type_at({"x": 10, "y": 20}, "hello", characters_per_minute=200, offset=3)

    assert fake.calls == [("click", 10, 20, ClickType.LEFT), ("type", "hello", 200, 3)]
    assert fake.lock_held_while_typing is True


def test_click_move_and_drag_delegate_to_interactor():
    emu = Emunium()
    emu.interactor = fake = FakeInteractor()

    emu.click_at({"x": 10, "y": 20}, ClickType.DOUBLE)
    emu.move_to({"x": 10, "y": 20}, offset_x=0.5, offset_y=1.0)
    emu.drag_and_drop((1, 2), (3, 4))

    assert fake.calls == [
        ("click", 10, 20, ClickType.DOUBLE),
        ("move", 10.5, 21.0),
        ("drag", (1, 2), (3, 4)),
    ]


def test_double_click_is_one_move_then_two_presses():
    gui = FakeGui(size=(1920, 1080))
    emu = Emunium()
    emu.interactor = ElementInteractor(
        backend=gui, platform="linux", double_click_time=0.2, sleep=lambda _s: None
    )

    emu.click_at({"x": 400, "y": 300}, ClickType.DOUBLE)

    first_down = next(i for i, entry in enumerate(gui.log) if entry[0] == "down")
    tail = gui.log[first_down:]
    assert [entry[0] for entry in tail] == ["down", "up", "down", "up"]
    assert gui.log[first_down - 1] == ("move", 400, 300)
