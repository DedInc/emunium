"""DPI awareness, browser-to-OS coordinate conversion and pointer calibration.

Units (see ``docs/architecture/native-input.md``):

- CSS: layout-viewport CSS px (``getBoundingClientRect``).
- DIP: Chrome screen space (``window.screenX`` / ``outerWidth`` ...).
- INPUT: what ``input_backend().moveTo`` / ``.position`` use.

On Windows the process is made per-monitor-v2 DPI aware before the input
backend is built, so INPUT is physical px on every monitor. On macOS INPUT is
points.
"""

from __future__ import annotations

import math
import sys
import threading
from dataclasses import dataclass
from typing import Any

from emunium import _native
from emunium._native import InputBackend

NATIVE_INPUT_LOCK = threading.RLock()
CLIPBOARD_LOCK = threading.Lock()

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
PROCESS_PER_MONITOR_DPI_AWARE = 2

# Two calibration samples must be at least this far apart (CSS px) on an axis
# before the per-axis scale is fitted instead of taken from the platform model.
MIN_FIT_SPAN_CSS = 40.0
# A fitted scale this far from the platform model is a bad sample, not a model.
_FIT_RATIO_RANGE = (0.8, 1.25)
_MAX_SAMPLES = 8

_DPI_LOCK = threading.Lock()
_DPI_DONE = False
_PYAUTOGUI_LOCK = threading.Lock()
_PYAUTOGUI: Any = None
_BACKEND_LOCK = threading.Lock()
_BACKEND: InputBackend | None = None


def _platform() -> str:
    return sys.platform


def _apply_windows_dpi_awareness(windll: Any = None) -> str | None:
    """Try PMv2 -> shcore per-monitor -> system aware. Returns the API that won."""
    import ctypes

    if windll is None:
        if sys.platform != "win32":
            return None
        windll = ctypes.windll

    try:
        user32 = windll.user32
    except Exception:
        user32 = None

    if user32 is not None:
        try:
            set_context = getattr(user32, "SetProcessDpiAwarenessContext", None)
            if set_context is not None:
                set_context.restype = ctypes.c_int
                set_context.argtypes = [ctypes.c_void_p]
                if set_context(
                    ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
                ):
                    return "SetProcessDpiAwarenessContext"
        except Exception:
            pass

    try:
        shcore = windll.shcore
        if shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE) == 0:
            return "SetProcessDpiAwareness"
    except Exception:
        pass

    if user32 is not None:
        try:
            if user32.SetProcessDPIAware():
                return "SetProcessDPIAware"
        except Exception:
            pass
    return None


def ensure_dpi_awareness() -> None:
    """Make the process per-monitor-v2 DPI aware on Windows. Idempotent, never raises.

    Fails silently when awareness is already fixed (e.g. pyautogui was imported
    first and called ``SetProcessDPIAware``).
    """
    global _DPI_DONE
    with _DPI_LOCK:
        if _DPI_DONE:
            return
        _DPI_DONE = True
        if _platform() != "win32":
            return
        try:
            _apply_windows_dpi_awareness()
        except Exception:
            pass


def input_backend() -> InputBackend:
    """The process-wide OS input backend, chosen once (native-input.md 5a).

    Windows: ``emunium._native.win32`` (ctypes, pyautogui is never imported).
    Linux with ``DISPLAY`` (X11 or XWayland): ``emunium._native.x11`` (ctypes
    over libX11/libXtst, pyautogui is never imported); Wayland without
    XWayland, or no display at all, raises ``NativeInputError``. macOS:
    pyautogui, imported lazily.
    DPI awareness is chosen first.
    """
    global _BACKEND
    backend = _BACKEND
    if backend is not None:
        return backend
    with _BACKEND_LOCK:
        if _BACKEND is None:
            ensure_dpi_awareness()
            native = _native.native_backend(_platform())
            _BACKEND = native if native is not None else pyautogui()
        return _BACKEND


def pyautogui() -> Any:
    """Import pyautogui lazily, after DPI awareness has been chosen.

    Two callers: ``input_backend`` on macOS, where pyautogui is the backend
    (``native_backend`` returns ``None``), and ``screenshot_scale``.
    """
    global _PYAUTOGUI
    if _PYAUTOGUI is not None:
        return _PYAUTOGUI
    with _PYAUTOGUI_LOCK:
        if _PYAUTOGUI is None:
            ensure_dpi_awareness()
            import pyautogui as module

            _PYAUTOGUI = module
    return _PYAUTOGUI


def _number(payload: dict, key: str, default: float | None = None) -> float:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"geometry.{key} must be a number, got {value!r}")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"geometry.{key} must be finite, got {value!r}")
    return value


@dataclass(frozen=True)
class Geometry:
    screen_x: float
    screen_y: float
    outer_width: float
    outer_height: float
    inner_width: float
    inner_height: float
    dpr: float
    zoom: float

    @classmethod
    def from_payload(cls, geometry: dict, zoom: float | None) -> Geometry:
        if not isinstance(geometry, dict):
            raise ValueError(f"geometry must be a dict, got {geometry!r}")
        if zoom is None:
            zoom_value = 1.0
        elif isinstance(zoom, bool) or not isinstance(zoom, (int, float)):
            raise ValueError(f"zoom must be a number, got {zoom!r}")
        else:
            zoom_value = float(zoom)
        if not math.isfinite(zoom_value) or zoom_value <= 0:
            raise ValueError(f"zoom must be positive, got {zoom!r}")
        result = cls(
            screen_x=_number(geometry, "screenX"),
            screen_y=_number(geometry, "screenY"),
            outer_width=_number(geometry, "outerWidth"),
            outer_height=_number(geometry, "outerHeight"),
            inner_width=_number(geometry, "innerWidth"),
            inner_height=_number(geometry, "innerHeight"),
            dpr=_number(geometry, "devicePixelRatio", 1.0),
            zoom=zoom_value,
        )
        if result.dpr <= 0:
            raise ValueError("geometry.devicePixelRatio must be positive")
        for name in ("outer_width", "outer_height", "inner_width", "inner_height"):
            if getattr(result, name) < 0:
                raise ValueError(f"geometry.{name} must not be negative")
        return result

    @property
    def signature(self) -> tuple:
        return (
            self.screen_x,
            self.screen_y,
            self.outer_width,
            self.outer_height,
            self.inner_width,
            self.inner_height,
            self.dpr,
            self.zoom,
        )


def input_per_dip(geometry: Geometry) -> float:
    """INPUT units per DIP: physical px on Windows/Linux, points (1.0) on macOS."""
    if _platform() == "darwin":
        return 1.0
    return geometry.dpr / geometry.zoom


def _input_per_css(geometry: Geometry) -> float:
    return input_per_dip(geometry) * geometry.zoom


def estimate_to_input(
    geometry: Geometry, css_x: float, css_y: float
) -> tuple[float, float]:
    """Window-chrome estimate of a CSS viewport point in INPUT units."""
    zoom = geometry.zoom
    border = max(0.0, (geometry.outer_width - geometry.inner_width * zoom) / 2.0)
    origin_x = geometry.screen_x + border
    origin_y = (
        geometry.screen_y
        + geometry.outer_height
        - geometry.inner_height * zoom
        - border
    )
    scale = input_per_dip(geometry)
    return (origin_x + css_x * zoom) * scale, (origin_y + css_y * zoom) * scale


def _fit_axis(
    clients: list[float], inputs: list[float], platform_k: float
) -> tuple[float, float]:
    """Return (origin, k) for one axis.

    Least-squares fit when the samples span enough CSS px and the slope is
    plausible; otherwise the latest sample anchors the origin with platform k.
    """
    latest_client = clients[-1]
    latest_input = inputs[-1]
    span = max(abs(c - latest_client) for c in clients)
    if len(clients) >= 2 and span >= MIN_FIT_SPAN_CSS:
        mean_c = sum(clients) / len(clients)
        mean_i = sum(inputs) / len(inputs)
        var = sum((c - mean_c) ** 2 for c in clients)
        if var > 0:
            cov = sum((c - mean_c) * (i - mean_i) for c, i in zip(clients, inputs))
            k = cov / var
            low, high = _FIT_RATIO_RANGE
            if low * platform_k <= k <= high * platform_k:
                return mean_i - mean_c * k, k
    return latest_input - latest_client * platform_k, platform_k


class Calibration:
    """Pointer-sample calibration for one browser window. Thread-safe.

    Samples are kept for one geometry signature; any change discards them.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._signature: tuple | None = None
        self._samples: list[tuple[float, float, float, float]] = []

    def _sync(self, geometry: Geometry) -> None:
        signature = geometry.signature
        if signature != self._signature:
            self._signature = signature
            self._samples = []

    def record(
        self,
        geometry: Geometry,
        client_x: float,
        client_y: float,
        input_x: float,
        input_y: float,
    ) -> None:
        sample = (float(client_x), float(client_y), float(input_x), float(input_y))
        if not all(math.isfinite(v) for v in sample):
            raise ValueError(f"calibration sample must be finite: {sample!r}")
        with self._lock:
            self._sync(geometry)
            self._samples.append(sample)
            del self._samples[:-_MAX_SAMPLES]

    def is_calibrated(self, geometry: Geometry) -> bool:
        with self._lock:
            self._sync(geometry)
            return bool(self._samples)

    def reset(self) -> None:
        with self._lock:
            self._signature = None
            self._samples = []

    def to_input(
        self, geometry: Geometry, css_x: float, css_y: float
    ) -> tuple[float, float]:
        with self._lock:
            self._sync(geometry)
            samples = list(self._samples)
        if not samples:
            return estimate_to_input(geometry, css_x, css_y)
        platform_k = _input_per_css(geometry)
        origin_x, k_x = _fit_axis(
            [s[0] for s in samples], [s[2] for s in samples], platform_k
        )
        origin_y, k_y = _fit_axis(
            [s[1] for s in samples], [s[3] for s in samples], platform_k
        )
        return origin_x + css_x * k_x, origin_y + css_y * k_y


def screenshot_scale(screenshot: Any = None) -> float:
    """INPUT px per screenshot px (1.0 with DPI awareness, 0.5 on Retina)."""
    gui = pyautogui()
    if screenshot is None:
        screenshot = gui.screenshot()
    width = getattr(screenshot, "width", None)
    if width is None:
        width = screenshot.size[0]
    if not width:
        return 1.0
    return float(gui.size()[0]) / float(width)
