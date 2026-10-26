"""Unit conversion, calibration and DPI awareness (emunium._screen).

Geometry values were measured on Chrome 146 / Windows with the window at
DIP (100, 80). Expected values are the viewport origin in INPUT px.
"""

from __future__ import annotations

import sys
import threading
import types

import pytest

from emunium import _screen
from emunium._screen import (
    Calibration,
    Geometry,
    estimate_to_input,
    input_per_dip,
)


def _geometry(outer, inner, dpr, zoom, screen=(100, 80)):
    payload = {
        "screenX": screen[0],
        "screenY": screen[1],
        "outerWidth": outer[0],
        "outerHeight": outer[1],
        "innerWidth": inner[0],
        "innerHeight": inner[1],
        "devicePixelRatio": dpr,
        "visualViewport": {"offsetLeft": 0, "offsetTop": 0, "scale": 1},
    }
    return Geometry.from_payload(payload, zoom)


# (geometry, true viewport origin in INPUT px)
CASE_A = (_geometry((1200, 800), (1184, 649), 1, 1), (108, 223))
CASE_B = (_geometry((1200, 800), (789, 433), 1.5, 1.5), (109, 223))
CASE_C = (_geometry((1200, 734), (1186, 583), 1.5, 1), (162, 335))
CASE_D = (_geometry((1200, 734), (949, 466), 1.875, 1.25), (161.25, 335))


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(_screen, "_platform", lambda: "win32")


@pytest.mark.parametrize(
    "case, name",
    [(CASE_A, "dsf1 zoom1"), (CASE_B, "dsf1 zoom1.5"), (CASE_C, "dsf1.5 zoom1"),
     (CASE_D, "dsf1.5 zoom1.25")],
)
def test_estimate_origin_within_2px(windows, case, name):
    geometry, (true_x, true_y) = case
    x, y = estimate_to_input(geometry, 0, 0)
    assert abs(x - true_x) <= 2 and abs(y - true_y) <= 2, (name, x, y)


def test_estimate_exact_formula_values(windows):
    assert estimate_to_input(CASE_A[0], 0, 0) == pytest.approx((108, 223))
    assert estimate_to_input(CASE_B[0], 0, 0) == pytest.approx((108.25, 222.25))


def test_estimate_scales_css_by_zoom_and_dpi(windows):
    # zoom 1.5 on a 100 % monitor: 10 CSS px = 15 DIP = 15 INPUT px.
    x0, y0 = estimate_to_input(CASE_B[0], 0, 0)
    x1, y1 = estimate_to_input(CASE_B[0], 10, 20)
    assert (x1 - x0, y1 - y0) == pytest.approx((15, 30))
    # 150 % monitor, zoom 1: 10 CSS px = 10 DIP = 15 physical px.
    x0, y0 = estimate_to_input(CASE_C[0], 0, 0)
    x1, y1 = estimate_to_input(CASE_C[0], 10, 20)
    assert (x1 - x0, y1 - y0) == pytest.approx((15, 30))


def test_input_per_dip_by_platform(monkeypatch):
    geometry = CASE_D[0]
    monkeypatch.setattr(_screen, "_platform", lambda: "win32")
    assert input_per_dip(geometry) == pytest.approx(1.5)
    monkeypatch.setattr(_screen, "_platform", lambda: "linux")
    assert input_per_dip(geometry) == pytest.approx(1.5)
    monkeypatch.setattr(_screen, "_platform", lambda: "darwin")
    assert input_per_dip(geometry) == 1.0


def test_from_payload_defaults_and_validation():
    geometry = Geometry.from_payload(
        {"screenX": 1, "screenY": 2, "outerWidth": 3, "outerHeight": 4,
         "innerWidth": 5, "innerHeight": 6},
        None,
    )
    assert geometry.dpr == 1.0 and geometry.zoom == 1.0
    assert geometry.signature == (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 1.0, 1.0)
    with pytest.raises(ValueError):
        Geometry.from_payload({"screenX": 1}, 1)
    with pytest.raises(ValueError):
        _geometry((1, 1), (1, 1), 1, 0)
    with pytest.raises(ValueError):
        _geometry((1, 1), (1, 1), float("nan"), 1)
    with pytest.raises(ValueError):
        Geometry.from_payload("nope", 1)  # type: ignore[arg-type]


def test_one_sample_calibration_case_c(windows):
    geometry = CASE_C[0]
    calibration = Calibration()
    assert not calibration.is_calibrated(geometry)
    calibration.record(geometry, 412, 150, 780, 560)
    assert calibration.is_calibrated(geometry)
    assert calibration.to_input(geometry, 0, 0) == pytest.approx((162, 335))
    x, y = calibration.to_input(geometry, 892, 407)
    assert abs(x - 1500) <= 1 and abs(y - 946) <= 1


def test_one_sample_beats_estimate(windows):
    geometry = CASE_D[0]
    calibration = Calibration()
    # Synthesize a sample from the true origin with k = dpr (1.875).
    calibration.record(geometry, 100, 200, 161.25 + 100 * 1.875, 335 + 200 * 1.875)
    assert calibration.to_input(geometry, 0, 0) == pytest.approx((161.25, 335))


def test_uncalibrated_falls_back_to_estimate(windows):
    geometry = CASE_A[0]
    assert Calibration().to_input(geometry, 5, 7) == estimate_to_input(geometry, 5, 7)


def test_two_sample_fit_per_axis(windows):
    geometry = CASE_A[0]  # platform k = 1.0
    calibration = Calibration()
    # True scale 1.1 on x, 0.9 on y (e.g. an unexpected OS scale).
    calibration.record(geometry, 100, 100, 50 + 100 * 1.1, 60 + 100 * 0.9)
    calibration.record(geometry, 300, 400, 50 + 300 * 1.1, 60 + 400 * 0.9)
    assert calibration.to_input(geometry, 500, 500) == pytest.approx(
        (50 + 500 * 1.1, 60 + 500 * 0.9)
    )


def test_fit_needs_40px_span_on_that_axis(windows):
    geometry = CASE_A[0]
    calibration = Calibration()
    calibration.record(geometry, 100, 100, 200, 300)
    # x moves 30 CSS px (no fit), y moves 100 (fit).
    calibration.record(geometry, 130, 200, 236, 390)
    x, y = calibration.to_input(geometry, 230, 300)
    # x: latest sample anchors, platform k 1.0 -> 236 + 100.
    assert x == pytest.approx(336)
    # y: fitted k 0.9 -> 390 + 100 * 0.9.
    assert y == pytest.approx(480)


def test_implausible_fit_falls_back_to_platform_k(windows):
    geometry = CASE_A[0]
    calibration = Calibration()
    calibration.record(geometry, 100, 100, 200, 200)
    calibration.record(geometry, 300, 300, 1000, 1000)  # k 4.0: a bad sample
    assert calibration.to_input(geometry, 310, 310) == pytest.approx((1010, 1010))


def test_geometry_change_discards_calibration(windows):
    geometry = CASE_C[0]
    calibration = Calibration()
    calibration.record(geometry, 412, 150, 780, 560)
    moved = _geometry((1200, 734), (1186, 583), 1.5, 1, screen=(101, 80))
    assert not calibration.is_calibrated(moved)
    assert calibration.to_input(moved, 0, 0) == estimate_to_input(moved, 0, 0)
    # The discard sticks: going back does not resurrect the old sample.
    assert not calibration.is_calibrated(geometry)


def test_zoom_change_discards_calibration(windows):
    geometry = CASE_A[0]
    calibration = Calibration()
    calibration.record(geometry, 10, 10, 118, 233)
    zoomed = Geometry.from_payload(
        {"screenX": 100, "screenY": 80, "outerWidth": 1200, "outerHeight": 800,
         "innerWidth": 1184, "innerHeight": 649, "devicePixelRatio": 1}, 1.1,
    )
    assert not calibration.is_calibrated(zoomed)


def test_reset_and_bad_sample(windows):
    geometry = CASE_A[0]
    calibration = Calibration()
    calibration.record(geometry, 1, 1, 1, 1)
    calibration.reset()
    assert not calibration.is_calibrated(geometry)
    with pytest.raises(ValueError):
        calibration.record(geometry, float("inf"), 1, 1, 1)


def test_calibration_is_thread_safe(windows):
    geometry = CASE_C[0]
    calibration = Calibration()
    errors = []

    def worker(seed):
        try:
            for i in range(200):
                calibration.record(geometry, 412, 150, 780, 560)
                calibration.to_input(geometry, seed, i)
        except Exception as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert calibration.to_input(geometry, 0, 0) == pytest.approx((162, 335))


def test_locks():
    assert hasattr(_screen.NATIVE_INPUT_LOCK, "acquire")
    with _screen.NATIVE_INPUT_LOCK:
        with _screen.NATIVE_INPUT_LOCK:  # re-entrant
            pass
    assert _screen.CLIPBOARD_LOCK.acquire(blocking=False)
    _screen.CLIPBOARD_LOCK.release()


class _Fn:
    def __init__(self, name, calls, result=None, error=None):
        self.name, self.calls, self.result, self.error = name, calls, result, error

    def __call__(self, *args):
        self.calls.append(self.name)
        if self.error is not None:
            raise self.error
        return self.result


def _windll(calls, context=0, shcore=1, aware=1, context_missing=False):
    user32 = types.SimpleNamespace(SetProcessDPIAware=_Fn("aware", calls, aware))
    if not context_missing:
        user32.SetProcessDpiAwarenessContext = _Fn("context", calls, context)
    return types.SimpleNamespace(
        user32=user32,
        shcore=types.SimpleNamespace(SetProcessDpiAwareness=_Fn("shcore", calls, shcore)),
    )


def test_dpi_awareness_prefers_per_monitor_v2():
    calls = []
    assert _screen._apply_windows_dpi_awareness(_windll(calls, context=1)) == (
        "SetProcessDpiAwarenessContext"
    )
    assert calls == ["context"]


def test_dpi_awareness_fallback_chain():
    calls = []
    result = _screen._apply_windows_dpi_awareness(_windll(calls, context=0, shcore=0))
    assert result == "SetProcessDpiAwareness" and calls == ["context", "shcore"]

    calls = []
    result = _screen._apply_windows_dpi_awareness(
        _windll(calls, context_missing=True, shcore=0x80070005)
    )
    assert result == "SetProcessDPIAware" and calls == ["shcore", "aware"]

    calls = []
    broken = _windll(calls, aware=0)
    broken.user32.SetProcessDpiAwarenessContext = _Fn("context", calls, error=OSError())
    broken.shcore.SetProcessDpiAwareness = _Fn("shcore", calls, error=OSError())
    assert _screen._apply_windows_dpi_awareness(broken) is None
    assert calls == ["context", "shcore", "aware"]


def test_ensure_dpi_awareness_idempotent_and_never_raises(monkeypatch):
    calls = []

    def boom(windll=None):
        calls.append(1)
        raise RuntimeError("already set")

    monkeypatch.setattr(_screen, "_DPI_DONE", False)
    monkeypatch.setattr(_screen, "_platform", lambda: "win32")
    monkeypatch.setattr(_screen, "_apply_windows_dpi_awareness", boom)
    _screen.ensure_dpi_awareness()
    _screen.ensure_dpi_awareness()
    assert calls == [1]


def test_ensure_dpi_awareness_noop_off_windows(monkeypatch):
    calls = []
    monkeypatch.setattr(_screen, "_DPI_DONE", False)
    monkeypatch.setattr(_screen, "_platform", lambda: "darwin")
    monkeypatch.setattr(_screen, "_apply_windows_dpi_awareness", lambda w=None: calls.append(1))
    _screen.ensure_dpi_awareness()
    assert calls == []


def test_pyautogui_imported_after_dpi_awareness(monkeypatch):
    order = []
    fake = types.ModuleType("pyautogui")
    monkeypatch.setattr(_screen, "_PYAUTOGUI", None)
    monkeypatch.setattr(_screen, "ensure_dpi_awareness", lambda: order.append("dpi"))
    monkeypatch.setitem(sys.modules, "pyautogui", fake)
    assert _screen.pyautogui() is fake
    assert _screen.pyautogui() is fake
    assert order == ["dpi"]


class _Size(tuple):
    @property
    def width(self):
        return self[0]


def test_screenshot_scale(monkeypatch):
    gui = types.SimpleNamespace(
        size=lambda: _Size((1440, 900)),
        screenshot=lambda: types.SimpleNamespace(width=2880, size=(2880, 1800)),
    )
    monkeypatch.setattr(_screen, "_PYAUTOGUI", gui)
    assert _screen.screenshot_scale() == pytest.approx(0.5)
    assert _screen.screenshot_scale(types.SimpleNamespace(size=(1440, 900))) == 1.0
