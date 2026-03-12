from __future__ import annotations

from typing import Protocol

import pyautogui

from emunium._standalone.config import StandaloneConfig


class OcrReader(Protocol):
    def readtext(self, image: object) -> list[tuple[object, str, float]]: ...


def initialize_ocr(langs: list[str], use_gpu: bool) -> OcrReader | None:
    try:
        from easyocr import Reader
    except ImportError:
        return None

    gpu_available = False
    if use_gpu:
        try:
            from torch import cuda

            gpu_available = cuda.is_available()
        except ImportError:
            pass

    return Reader(langs, gpu=gpu_available)


def find_text_elements(
    ocr_reader: OcrReader | None,
    query: str,
    min_confidence: float = StandaloneConfig.DEFAULT_MIN_CONFIDENCE,
    max_elements: int = StandaloneConfig.DEFAULT_MAX_ELEMENTS,
    region: tuple[int, int, int, int] | None = None,
) -> list[dict[str, int]]:
    import cv2
    import numpy as np

    if ocr_reader is None:
        raise ImportError("EasyOCR not installed. Run: pip install easyocr")

    region_offset = (region[0], region[1]) if region else (0, 0)
    img = pyautogui.screenshot(region=region) if region else pyautogui.screenshot()
    img_bgr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

    results = ocr_reader.readtext(img_bgr)
    found: list[dict[str, int]] = []
    query_lower = query.lower()

    for bbox, text, conf in results:
        if conf >= min_confidence and query_lower in text.lower():
            pts = np.array(bbox, dtype="int")
            center_x = int(np.mean(pts[:, 0])) + region_offset[0]
            center_y = int(np.mean(pts[:, 1])) + region_offset[1]
            found.append({"x": center_x, "y": center_y})

            if max_elements and len(found) >= max_elements:
                break

    return found
