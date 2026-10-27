"""Screen capture (mss) and template matching for standalone mode.

Captures come from ``mss`` as BGRA numpy arrays (no Pillow) and span every
monitor. Matching runs in screenshot pixels; results are converted to INPUT
units (what ``emunium._screen.input_backend().moveTo`` uses) so Retina (2x)
screenshots are clicked at the right point. Regions are given in INPUT units:
``(left, top, width, height)``.
"""

from __future__ import annotations

import importlib
import math
from collections.abc import Mapping, Sequence
from itertools import product
from typing import TYPE_CHECKING, Any, NamedTuple

from emunium import _screen
from emunium._standalone.config import StandaloneConfig

if TYPE_CHECKING:
    import numpy as np

Region = tuple[float, float, float, float]

STANDALONE_MODULES = ("numpy", "cv2", "mss")
MISSING_STANDALONE = (
    "standalone mode needs mss, numpy and opencv, but {module} is not installed. "
    "Run: pip install emunium[standalone]"
)


def require_standalone() -> None:
    """Raise ``ImportError`` naming the extra when a standalone dependency is missing."""
    for module in STANDALONE_MODULES:
        try:
            importlib.import_module(module)
        except ImportError as exc:
            raise ImportError(MISSING_STANDALONE.format(module=module)) from exc


class Match(NamedTuple):
    """A template match in screenshot pixels; ``(x, y)`` is the top-left corner."""

    x: int
    y: int
    width: int
    height: int
    score: float


class ScreenCapture(NamedTuple):
    """A BGRA screenshot and how to map its pixels back to INPUT units.

    ``(origin_x, origin_y)`` is the INPUT point of pixel ``(0, 0)``; ``scale``
    is INPUT units per screenshot pixel (1.0 on Windows/Linux, 0.5 on Retina).
    """

    image: np.ndarray
    origin_x: float
    origin_y: float
    scale: float

    def to_input(self, x: float, y: float) -> dict[str, int]:
        """Map a point of ``image`` (screenshot px) to INPUT units."""
        return {
            "x": round(self.origin_x + x * self.scale),
            "y": round(self.origin_y + y * self.scale),
        }


def _primary_monitor(monitors: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """mss ``monitors[0]`` is the union of all monitors; the primary is in ``[1:]``."""
    if len(monitors) < 2:
        raise RuntimeError("mss reported no monitor to capture")
    return next((m for m in monitors[1:] if m.get("is_primary")), monitors[1])


def _grab_box(
    region: Region | None, input_per_unit: float, desktop: Mapping[str, Any]
) -> dict[str, int]:
    """INPUT-unit region -> mss box clamped to the all-monitor ``desktop``."""
    d_left, d_top = int(desktop["left"]), int(desktop["top"])
    d_right = d_left + int(desktop["width"])
    d_bottom = d_top + int(desktop["height"])
    if region is None:
        left, top, right, bottom = d_left, d_top, d_right, d_bottom
    else:
        if len(region) != 4:
            raise ValueError(
                f"region must be (left, top, width, height), got {region!r}"
            )
        r_left, r_top, r_width, r_height = (float(value) for value in region)
        if not all(math.isfinite(v) for v in (r_left, r_top, r_width, r_height)):
            raise ValueError(f"region must be finite, got {region!r}")
        if r_width <= 0 or r_height <= 0:
            raise ValueError(
                f"region width and height must be positive, got {region!r}"
            )
        left = max(d_left, math.floor(r_left / input_per_unit))
        top = max(d_top, math.floor(r_top / input_per_unit))
        right = min(d_right, math.ceil((r_left + r_width) / input_per_unit))
        bottom = min(d_bottom, math.ceil((r_top + r_height) / input_per_unit))
    if right <= left or bottom <= top:
        raise ValueError(f"region {region!r} lies outside the screen")
    return {"left": left, "top": top, "width": right - left, "height": bottom - top}


def capture_screen(region: Region | None = None) -> ScreenCapture:
    """Grab ``region`` (INPUT units; ``None``: all monitors) with mss.

    INPUT units per mss unit = ``input_backend().size()`` width / mss primary
    monitor width (both physical px on Windows/Linux, both points on macOS).
    mss units per pixel come from the grab itself: on Retina a box given in
    points comes back with twice the pixels.
    """
    require_standalone()
    import mss
    import numpy as np

    # The backend chooses DPI awareness; it must happen before mss measures.
    backend_width = float(_screen.input_backend().size()[0])
    factory = getattr(mss, "MSS", None) or mss.mss
    with factory() as grabber:
        monitors = grabber.monitors
        unit_width = float(_primary_monitor(monitors)["width"])
        if backend_width <= 0 or unit_width <= 0:
            raise RuntimeError(
                f"cannot scale screenshots: input width {backend_width}, "
                f"monitor width {unit_width}"
            )
        input_per_unit = backend_width / unit_width
        box = _grab_box(region, input_per_unit, monitors[0])
        shot = grabber.grab(box)
    width, height = int(shot.width), int(shot.height)
    image = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(height, width, 4)
    scale = input_per_unit * box["width"] / float(width)
    return ScreenCapture(
        image, box["left"] * input_per_unit, box["top"] * input_per_unit, scale
    )


def to_gray(image: np.ndarray) -> np.ndarray:
    """8-bit gray from gray, BGR or BGRA (mss captures are BGRA)."""
    import cv2

    if image.ndim == 2:
        return image
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def apply_transformations(
    template: np.ndarray, scale: float, angle: float, contrast: float
) -> np.ndarray:
    import cv2

    height, width = template.shape[:2]
    new_width, new_height = int(width * scale), int(height * scale)

    if new_width <= 0 or new_height <= 0:
        return template

    resized = cv2.resize(
        template, (new_width, new_height), interpolation=cv2.INTER_AREA
    )
    contrasted = cv2.convertScaleAbs(resized, alpha=contrast, beta=0)

    h, w = contrasted.shape[:2]
    rotation_matrix = cv2.getRotationMatrix2D((w // 2, h // 2), angle, 1.0)
    return cv2.warpAffine(
        contrasted,
        rotation_matrix,
        (w, h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )


def within_size_constraints(
    template_height: int,
    template_width: int,
    target_height: int | None,
    target_width: int | None,
    epsilon: float,
) -> bool:
    if target_height is None or target_width is None:
        return True

    min_height = round(target_height * (1 - epsilon))
    max_height = round(target_height * (1 + epsilon))
    min_width = round(target_width * (1 - epsilon))
    max_width = round(target_width * (1 + epsilon))

    return (
        min_height <= template_height <= max_height
        and min_width <= template_width <= max_width
    )


def top_candidates(
    result: np.ndarray, min_confidence: float, limit: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(xs, ys, scores)`` of the at most ``limit`` best positions >= min_confidence."""
    import numpy as np

    flat = result.ravel()
    above = np.flatnonzero((flat >= min_confidence) & np.isfinite(flat))
    if limit > 0 and above.size > limit:
        cut = above.size - limit
        above = above[np.argpartition(flat[above], cut)[cut:]]
    ys, xs = np.unravel_index(above, result.shape)
    return xs, ys, flat[above]


def suppress_overlaps(
    boxes: np.ndarray, scores: np.ndarray, overlap: float, limit: int = 0
) -> np.ndarray:
    """Indices of boxes kept by greedy best-first suppression, best score first.

    ``boxes`` rows are ``(x, y, width, height)``. A box is dropped when its
    intersection with an already kept box covers at least ``overlap`` of the
    smaller of the two, so the rule scales with the template size.
    """
    import numpy as np

    boxes = np.asarray(boxes, dtype=np.float64).reshape(-1, 4)
    scores = np.asarray(scores, dtype=np.float64).ravel()
    x1, y1 = boxes[:, 0], boxes[:, 1]
    x2, y2 = x1 + boxes[:, 2], y1 + boxes[:, 3]
    areas = np.maximum(boxes[:, 2], 0.0) * np.maximum(boxes[:, 3], 0.0)
    order = np.argsort(-scores, kind="stable")
    kept = []
    while order.size:
        best = order[0]
        kept.append(best)
        if 0 < limit <= len(kept):
            break
        rest = order[1:]
        inter_w = np.minimum(x2[best], x2[rest]) - np.maximum(x1[best], x1[rest])
        inter_h = np.minimum(y2[best], y2[rest]) - np.maximum(y1[best], y1[rest])
        intersection = np.clip(inter_w, 0.0, None) * np.clip(inter_h, 0.0, None)
        smaller = np.maximum(np.minimum(areas[best], areas[rest]), 1.0)
        order = rest[intersection / smaller < overlap]
    return np.asarray(kept, dtype=np.intp)


def match_template(
    screen_gray: np.ndarray,
    template: np.ndarray,
    min_confidence: float = StandaloneConfig.DEFAULT_MIN_CONFIDENCE,
    target_height: int | None = None,
    target_width: int | None = None,
    max_elements: int = StandaloneConfig.DEFAULT_MAX_ELEMENTS,
) -> list[Match]:
    """Matches of ``template`` in ``screen_gray`` (both 8-bit gray), best first."""
    import cv2
    import numpy as np

    screen_eq = cv2.equalizeHist(screen_gray)
    screen_height, screen_width = screen_gray.shape[:2]
    limit = StandaloneConfig.MAX_CANDIDATES_PER_PASS
    epsilon = 1.0 - min_confidence
    box_parts = []
    score_parts = []
    transformations = product(
        StandaloneConfig.SCALE_FACTORS,
        StandaloneConfig.ROTATION_ANGLES,
        StandaloneConfig.CONTRAST_FACTORS,
    )

    for scale, angle, contrast in transformations:
        transformed = apply_transformations(template, scale, angle, contrast)
        t_height, t_width = transformed.shape[:2]
        if t_height > screen_height or t_width > screen_width:
            continue
        if not within_size_constraints(
            t_height, t_width, target_height, target_width, epsilon
        ):
            continue

        for use_equalized in (False, True):
            current_template = (
                cv2.equalizeHist(transformed) if use_equalized else transformed
            )
            current_screen = screen_eq if use_equalized else screen_gray
            result = cv2.matchTemplate(
                current_screen, current_template, cv2.TM_CCOEFF_NORMED
            )
            xs, ys, scores = top_candidates(result, min_confidence, limit)
            if not scores.size:
                continue
            widths = np.full(xs.shape, t_width)
            heights = np.full(xs.shape, t_height)
            box_parts.append(np.column_stack((xs, ys, widths, heights)))
            score_parts.append(scores)

    if not score_parts:
        return []
    boxes = np.concatenate(box_parts)
    scores = np.concatenate(score_parts)
    kept = suppress_overlaps(
        boxes, scores, StandaloneConfig.DUPLICATE_OVERLAP, max_elements
    )
    return [
        Match(
            int(boxes[index, 0]),
            int(boxes[index, 1]),
            int(boxes[index, 2]),
            int(boxes[index, 3]),
            float(scores[index]),
        )
        for index in kept
    ]


def find_elements(
    image_path: str,
    min_confidence: float = StandaloneConfig.DEFAULT_MIN_CONFIDENCE,
    target_height: int | None = None,
    target_width: int | None = None,
    max_elements: int = StandaloneConfig.DEFAULT_MAX_ELEMENTS,
    region: Region | None = None,
) -> list[dict[str, int]]:
    """Centers (INPUT units) of template matches on screen, best score first."""
    require_standalone()
    import cv2

    if not image_path:
        raise ValueError("Image path required.")

    template = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
    if template is None:
        raise ValueError(f"Template not found: {image_path}")

    capture = capture_screen(region)
    matches = match_template(
        to_gray(capture.image),
        template,
        min_confidence,
        target_height,
        target_width,
        max_elements,
    )
    return [
        capture.to_input(m.x + m.width / 2.0, m.y + m.height / 2.0) for m in matches
    ]
