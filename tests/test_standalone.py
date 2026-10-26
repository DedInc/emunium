"""Standalone (screen) mode against fakes.

No test moves the real mouse, types, or takes a real screenshot: the input
backend is a ``FakeGui`` installed as ``emunium._screen._BACKEND``, ``mss`` in
``sys.modules`` is a fake (or a trap that fails the test), ``_screen.pyautogui``
fails the test if standalone still reaches for it, and screens are synthetic
BGRA numpy images.
"""

from __future__ import annotations

import math
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
from emunium._standalone.config import ClickType
from emunium.standalone import Emunium

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_DIR = os.path.join(REPO_ROOT, "emunium")
TEMPLATE_SIZE = 32
MISSING_EXTRA = r"pip install emunium\[standalone\]"


class FakeGui:
    """Input backend stand-in; ``scroll`` records what standalone passes (notches)."""

    def __init__(self, size=(1920, 1080)):
        self.screen = size
        self.log = []
        self.pos = (5, 5)

    def size(self):
        return self.screen

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


class FakeShot:
    """An mss ``ScreenShot``: ``width``, ``height`` and raw ``bgra`` bytes."""

    def __init__(self, pixels):
        self.height, self.width = pixels.shape[:2]
        self.bgra = pixels.tobytes()


class FakeGrabber:
    def __init__(self, module):
        self._module = module

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    @property
    def monitors(self):
        return self._module.monitor_list

    def grab(self, box):
        module = self._module
        module.grabs.append(dict(box))
        desktop = module.monitor_list[0]
        ratio = module.pixel_ratio
        x0 = (box["left"] - desktop["left"]) * ratio
        y0 = (box["top"] - desktop["top"]) * ratio
        width, height = box["width"] * ratio, box["height"] * ratio
        pixels = module.desktop[y0 : y0 + height, x0 : x0 + width]
        assert pixels.shape[:2] == (height, width), "grab outside the desktop"
        return FakeShot(pixels.copy())


class FakeMss(types.ModuleType):
    """Stands in for ``mss``: ``desktop`` holds ``pixel_ratio`` px per mss unit."""

    def __init__(self, desktop, monitor_list, pixel_ratio=1, factory="MSS"):
        super().__init__("mss")
        self.desktop = desktop
        self.monitor_list = monitor_list
        self.pixel_ratio = pixel_ratio
        self.grabs = []
        setattr(self, factory, lambda: FakeGrabber(self))


class TrapMss(types.ModuleType):
    def __init__(self):
        super().__init__("mss")

    def MSS(self):
        raise AssertionError("a unit test reached a real mss screen capture")

    mss = MSS


class FakeInteractor(ElementInteractor):
    """Records calls; any method not overridden still only reaches a FakeGui."""

    def __init__(self):
        super().__init__(backend=FakeGui(), sleep=lambda _seconds: None)
        self.calls = []
        self.lock_held_while_typing = None

    def move(self, x, y, *, human=True):
        self.calls.append(("move", x, y))

    def press(self, button="left", *, clicks=1):
        self.calls.append(("press", button, clicks))

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


class FakeReader:
    def __init__(self, results):
        self.results = results
        self.images = []

    def readtext(self, image):
        self.images.append(image)
        return self.results


@pytest.fixture(autouse=True)
def no_real_screen_or_pyautogui(monkeypatch):
    """Standalone must use ``input_backend()`` and mss; both are faked here."""

    def forbidden_pyautogui():
        raise AssertionError("standalone reached _screen.pyautogui()")

    monkeypatch.setattr(_screen, "pyautogui", forbidden_pyautogui)
    monkeypatch.setattr(_screen, "_BACKEND", FakeGui())
    monkeypatch.setitem(sys.modules, "mss", TrapMss())


@pytest.fixture
def install_gui(monkeypatch):
    def install(gui):
        monkeypatch.setattr(_screen, "_BACKEND", gui)
        return gui

    return install


@pytest.fixture
def install_mss(monkeypatch):
    def install(desktop, monitor_list, pixel_ratio=1, factory="MSS"):
        fake = FakeMss(desktop, monitor_list, pixel_ratio, factory)
        monkeypatch.setitem(sys.modules, "mss", fake)
        return fake

    return install


@pytest.fixture
def cv():
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    return cv2, np


def _monitor(left, top, width, height, **extra):
    return {"left": left, "top": top, "width": width, "height": height, **extra}


def _monitors(*screens):
    """mss layout: ``[0]`` is the bounding box of every monitor."""
    left = min(s["left"] for s in screens)
    top = min(s["top"] for s in screens)
    right = max(s["left"] + s["width"] for s in screens)
    bottom = max(s["top"] + s["height"] for s in screens)
    return [_monitor(left, top, right - left, bottom - top), *screens]


def _bgra(np, gray):
    alpha = np.full(gray.shape, 255, dtype=np.uint8)
    return np.ascontiguousarray(np.dstack([gray, gray, gray, alpha]))


def _scene(np, width, height, exact_at, noisy_at):
    """Noise screen (BGRA) with an exact and a noisy copy of a gray template."""
    rng = np.random.default_rng(7)
    screen = rng.integers(0, 256, size=(height, width), dtype=np.uint8)
    template = rng.integers(0, 256, size=(TEMPLATE_SIZE, TEMPLATE_SIZE), dtype=np.uint8)
    noise = rng.normal(0.0, 25.0, size=template.shape)
    noisy = np.clip(template.astype(np.float64) + noise, 0, 255).astype(np.uint8)
    for (x, y), patch in ((exact_at, template), (noisy_at, noisy)):
        screen[y : y + TEMPLATE_SIZE, x : x + TEMPLATE_SIZE] = patch
    return _bgra(np, screen), template


def _write_template(cv2, tmp_path, template):
    path = str(tmp_path / "template.png")
    assert cv2.imwrite(path, template)
    return path


# -- import hygiene ------------------------------------------------------------


def test_import_does_not_load_pyautogui_humancursor_or_capture_modules():
    code = (
        "import emunium.standalone, sys; "
        "print(*(m in sys.modules for m in ('pyautogui', 'humancursor', 'mss', 'cv2')))"
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
    assert completed.stdout.strip() == "False False False False"


def test_package_never_references_humancursor():
    offenders = []
    for root, _dirs, files in os.walk(PACKAGE_DIR):
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as source:
                    if "humancursor" in source.read():
                        offenders.append(os.path.relpath(path, REPO_ROOT))
    assert offenders == []


@pytest.mark.parametrize("module", ["mss", "numpy", "cv2"])
def test_missing_dependency_names_the_extra(monkeypatch, module):
    monkeypatch.setitem(sys.modules, module, None)

    with pytest.raises(ImportError, match=MISSING_EXTRA) as raised:
        Emunium().find_elements("template.png")
    assert module in str(raised.value)
    with pytest.raises(ImportError, match=MISSING_EXTRA):
        vision.capture_screen()
    with pytest.raises(ImportError, match=MISSING_EXTRA):
        standalone_ocr.find_text_elements(FakeReader([]), "Sign in")


# -- cursor compatibility --------------------------------------------------------


def test_cursor_is_created_lazily_without_input(install_gui):
    gui = install_gui(FakeGui())
    emu = Emunium()
    assert emu._cursor is None

    cursor = emu.cursor

    assert isinstance(cursor, facade.SystemCursor)
    assert emu.cursor is cursor
    assert gui.log == []


def test_cursor_delegates_to_the_current_interactor():
    emu = Emunium()
    cursor = emu.cursor
    emu.interactor = fake = FakeInteractor()

    cursor.move_to((1, 2))
    cursor.click_on([5, 6])
    cursor.click_on((7, 8), clicks=2)
    cursor.drag_and_drop((1, 2), [3, 4])

    assert fake.calls == [
        ("move", 1.0, 2.0),
        ("move", 5.0, 6.0),
        ("press", "left", 1),
        ("move", 7.0, 8.0),
        ("press", "left", 2),
        ("drag", (1.0, 2.0), (3.0, 4.0)),
    ]


def test_click_on_with_duration_holds_each_press(monkeypatch):
    gui = FakeGui()
    holds = []
    gaps = []
    monkeypatch.setattr(facade, "_sleep", gaps.append)
    emu = Emunium()
    emu.interactor = ElementInteractor(
        backend=gui, platform="linux", double_click_time=0.2, sleep=holds.append
    )

    emu.cursor.click_on((400, 300), clicks=2, click_duration=0.25)

    first_down = next(i for i, entry in enumerate(gui.log) if entry[0] == "down")
    assert gui.log[first_down - 1] == ("move", 400, 300)
    assert gui.log[first_down:] == [
        ("down", "left"),
        ("up", "left"),
        ("down", "left"),
        ("up", "left"),
    ]
    assert holds.count(0.25) == 2
    assert gaps == [pytest.approx(0.1)]


@pytest.mark.parametrize(
    ("args", "kwargs"),
    [
        (((1, 2),), {"clicks": 0}),
        (((1, 2),), {"clicks": True}),
        (((1, 2),), {"click_duration": -1}),
        (((1, 2),), {"click_duration": math.nan}),
        (((1, 2, 3),), {}),
    ],
)
def test_click_on_rejects_bad_arguments_before_any_input(args, kwargs):
    emu = Emunium()
    emu.interactor = fake = FakeInteractor()

    with pytest.raises(ValueError):
        emu.cursor.click_on(*args, **kwargs)
    assert fake.calls == []


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


def test_find_text_scales_and_offsets_region(cv, fake_init, install_gui, install_mss):
    _, np = cv
    install_gui(FakeGui(size=(320, 240)))
    # Retina: mss monitors in points, grabs come back with 2 px per point.
    fake = install_mss(
        _bgra(np, np.zeros((480, 640), dtype=np.uint8)),
        _monitors(_monitor(0, 0, 320, 240)),
        pixel_ratio=2,
    )
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

    # Only the region is grabbed; box center (20, 15) px -> (100, 50) + (10, 7.5).
    assert fake.grabs == [_monitor(100, 50, 60, 40)]
    assert hits == [{"x": 110, "y": 58}]
    assert reader.images[0].shape == (80, 120, 3)


# -- capture -----------------------------------------------------------------------


def test_capture_is_bgra_of_every_monitor(cv, install_gui, install_mss):
    _, np = cv
    install_gui(FakeGui(size=(320, 240)))
    gray = (np.arange(960 * 240).reshape(240, 960) % 251).astype(np.uint8)
    desktop = _bgra(np, gray)
    layout = _monitors(
        _monitor(-640, 0, 640, 240), _monitor(0, 0, 320, 240, is_primary=True)
    )
    fake = install_mss(desktop, layout)

    capture = vision.capture_screen()

    assert fake.grabs == [_monitor(-640, 0, 960, 240)]
    assert capture.image.shape == (240, 960, 4)
    assert np.array_equal(capture.image, desktop)
    assert (capture.origin_x, capture.origin_y, capture.scale) == (-640, 0, 1.0)


def test_capture_falls_back_to_the_pre_10_2_factory(cv, install_gui, install_mss):
    _, np = cv
    install_gui(FakeGui(size=(320, 240)))
    fake = install_mss(
        _bgra(np, np.zeros((240, 320), dtype=np.uint8)),
        _monitors(_monitor(0, 0, 320, 240)),
        factory="mss",
    )

    assert vision.capture_screen((10, 20, 30, 40)).image.shape == (40, 30, 4)
    assert fake.grabs == [_monitor(10, 20, 30, 40)]


def test_invalid_region_raises(cv, install_gui, install_mss):
    _, np = cv
    install_gui(FakeGui(size=(320, 240)))
    fake = install_mss(
        _bgra(np, np.zeros((240, 320), dtype=np.uint8)),
        _monitors(_monitor(0, 0, 320, 240)),
    )
    with pytest.raises(ValueError, match="outside"):
        vision.capture_screen((400, 400, 10, 10))
    with pytest.raises(ValueError, match="positive"):
        vision.capture_screen((0, 0, 0, 10))
    with pytest.raises(ValueError, match="finite"):
        vision.capture_screen((0, math.inf, 10, 10))
    assert fake.grabs == []


# -- template matching -----------------------------------------------------------


def test_matches_are_ranked_by_score_not_raster_order(
    cv, tmp_path, install_gui, install_mss
):
    cv2, np = cv
    pixels, template = _scene(np, 320, 240, exact_at=(200, 150), noisy_at=(20, 20))
    install_gui(FakeGui(size=(320, 240)))
    install_mss(pixels, _monitors(_monitor(0, 0, 320, 240)))

    matches = Emunium().find_elements(_write_template(cv2, tmp_path, template))

    assert matches == [{"x": 216, "y": 166}, {"x": 36, "y": 36}]


def test_max_elements_keeps_the_best(cv, tmp_path, install_gui, install_mss):
    cv2, np = cv
    pixels, template = _scene(np, 320, 240, exact_at=(200, 150), noisy_at=(20, 20))
    install_gui(FakeGui(size=(320, 240)))
    install_mss(pixels, _monitors(_monitor(0, 0, 320, 240)))

    matches = Emunium().find_elements(
        _write_template(cv2, tmp_path, template), max_elements=1
    )

    assert matches == [{"x": 216, "y": 166}]


# Two ways a screenshot has 2 px per INPUT unit: macOS Retina (mss monitors in
# points, grabs in pixels) and a DPI-unaware process (mss px, backend logical).
HALF_SCALE_LAYOUTS = [
    pytest.param(_monitors(_monitor(0, 0, 320, 240)), 2, id="retina-points"),
    pytest.param(_monitors(_monitor(0, 0, 640, 480)), 1, id="unaware-pixels"),
]


@pytest.mark.parametrize(("layout", "ratio"), HALF_SCALE_LAYOUTS)
def test_hidpi_screenshot_is_scaled_to_input_units(
    cv, tmp_path, install_gui, install_mss, layout, ratio
):
    cv2, np = cv
    pixels, template = _scene(np, 640, 480, exact_at=(400, 300), noisy_at=(20, 20))
    install_gui(FakeGui(size=(320, 240)))
    install_mss(pixels, layout, pixel_ratio=ratio)
    path = _write_template(cv2, tmp_path, template)

    assert Emunium().find_elements(path) == [{"x": 208, "y": 158}, {"x": 18, "y": 18}]


@pytest.mark.parametrize(("layout", "ratio"), HALF_SCALE_LAYOUTS)
def test_region_is_in_input_units(
    cv, tmp_path, install_gui, install_mss, layout, ratio
):
    cv2, np = cv
    pixels, template = _scene(np, 640, 480, exact_at=(400, 300), noisy_at=(20, 20))
    install_gui(FakeGui(size=(320, 240)))
    install_mss(pixels, layout, pixel_ratio=ratio)
    path = _write_template(cv2, tmp_path, template)
    emu = Emunium()

    assert emu.find_elements(path, region=(180, 130, 140, 110)) == [
        {"x": 208, "y": 158}
    ]
    assert emu.find_elements(path, region=(0, 0, 160, 120)) == [{"x": 18, "y": 18}]


def test_secondary_monitor_left_of_primary(cv, tmp_path, install_gui, install_mss):
    cv2, np = cv
    # Desktop x 0 is mss x -640; the primary is listed second and is narrower,
    # so taking monitors[1] as primary would halve every coordinate.
    pixels, template = _scene(np, 960, 240, exact_at=(100, 150), noisy_at=(660, 20))
    install_gui(FakeGui(size=(320, 240)))
    layout = _monitors(
        _monitor(-640, 0, 640, 240), _monitor(0, 0, 320, 240, is_primary=True)
    )
    fake = install_mss(pixels, layout)
    path = _write_template(cv2, tmp_path, template)
    emu = Emunium()

    assert emu.find_elements(path) == [{"x": -524, "y": 166}, {"x": 36, "y": 36}]
    assert emu.find_elements(path, region=(-640, 0, 320, 240)) == [
        {"x": -524, "y": 166}
    ]
    assert fake.grabs == [_monitor(-640, 0, 960, 240), _monitor(-640, 0, 320, 240)]


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
    ("y", "expected_notches"),
    [
        (540 + 300, [-1] * 3),
        (540 - 260, [1] * 3),
        (540 + 140, [-1]),
        (540 + 40, []),
    ],
)
def test_scroll_to_sends_one_wheel_notch_per_step(
    install_gui, no_sleep, y, expected_notches
):
    # input_backend().scroll takes NOTCHES on every platform: the Windows
    # backend scales by WHEEL_DELTA (120) itself, so 120 here would be 120 notches.
    gui = install_gui(FakeGui(size=(1920, 1080)))

    Emunium().scroll_to({"x": 10, "y": y})

    assert facade.WHEEL_NOTCH == 1
    assert [entry[1] for entry in gui.log] == expected_notches
    assert all(entry[0] == "scroll" and entry[2] is False for entry in gui.log)
    assert len(no_sleep) == len(expected_notches)


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
