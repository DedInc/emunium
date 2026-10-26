"""EasyOCR text search for standalone mode (results in INPUT units)."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Protocol

from emunium._standalone import vision
from emunium._standalone.config import StandaloneConfig

if TYPE_CHECKING:
    import numpy as np

MISSING_EASYOCR = (
    "OCR was requested (Emunium(ocr_enabled=True)) but easyocr is not installed. "
    'Run: pip install "emunium[ocr]"'
)


class OcrReader(Protocol):
    def readtext(self, image: object) -> list[tuple[object, str, float]]: ...


def initialize_ocr(langs: list[str], use_gpu: bool) -> OcrReader:
    """Build an EasyOCR reader; raises ``ImportError`` when easyocr is missing.

    easyocr and torch are optional backends (``emunium[ocr]``), loaded by name
    at call time; the reader is typed by the ``OcrReader`` protocol.
    """
    try:
        easyocr = importlib.import_module("easyocr")
    except ImportError as exc:
        raise ImportError(MISSING_EASYOCR) from exc

    gpu_available = False
    if use_gpu:
        try:
            torch = importlib.import_module("torch")
        except ImportError:
            torch = None
        if torch is not None:
            gpu_available = bool(torch.cuda.is_available())

    reader: OcrReader = easyocr.Reader(langs, gpu=gpu_available)
    return reader


def to_bgr(image: np.ndarray) -> np.ndarray:
    """BGR from gray, BGR or BGRA (mss captures are BGRA)."""
    import cv2

    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    return image


def find_text_elements(
    ocr_reader: OcrReader,
    query: str,
    min_confidence: float = StandaloneConfig.DEFAULT_MIN_CONFIDENCE,
    max_elements: int = StandaloneConfig.DEFAULT_MAX_ELEMENTS,
    region: vision.Region | None = None,
) -> list[dict[str, int]]:
    """Centers (INPUT units) of OCR boxes whose text contains ``query``."""
    vision.require_standalone()
    import numpy as np

    capture = vision.capture_screen(region)
    results = ocr_reader.readtext(to_bgr(capture.image))
    found: list[dict[str, int]] = []
    query_lower = query.lower()

    for bbox, text, conf in results:
        if conf >= min_confidence and query_lower in text.lower():
            points = np.asarray(bbox, dtype=np.float64).reshape(-1, 2)
            found.append(
                capture.to_input(float(points[:, 0].mean()), float(points[:, 1].mean()))
            )
            if max_elements and len(found) >= max_elements:
                break

    return found
