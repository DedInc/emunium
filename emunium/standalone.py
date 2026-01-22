from itertools import product
from typing import Optional, List, Dict, Tuple

import cv2
import numpy as np
import pyautogui

from .base import ClickType, EmuniumBase


class StandaloneConfig:
    """Configuration for standalone automation."""

    SCALE_FACTORS = (0.9, 1.0, 1.1)
    ROTATION_ANGLES = (-10, 0, 10)
    CONTRAST_FACTORS = (0.9, 1.0, 1.1)
    DUPLICATE_THRESHOLD = 10
    DEFAULT_MIN_CONFIDENCE = 0.8
    DEFAULT_MAX_ELEMENTS = 0


class Emunium(EmuniumBase):
    """Standalone automation with image and OCR detection."""

    def __init__(
        self, ocr: bool = False, use_gpu: bool = True, langs: Optional[List[str]] = None
    ):
        super().__init__()
        self.ocr = ocr
        self.ocr_reader = None
        self.monitor_region: Optional[Tuple[int, int, int, int]] = None

        if self.ocr:
            screen_width, screen_height = pyautogui.size()
            self.monitor_region = (0, 0, screen_width, screen_height)
            self._initialize_ocr(langs or ["en"], use_gpu)

    def _initialize_ocr(self, langs: List[str], use_gpu: bool) -> None:
        """Initialize EasyOCR with GPU if available."""
        try:
            from easyocr import Reader
        except ImportError:
            return

        gpu_available = False
        if use_gpu:
            try:
                from torch import cuda

                gpu_available = cuda.is_available()
            except ImportError:
                pass

        self.ocr_reader = Reader(langs, gpu=gpu_available)

    def _apply_transformations(
        self, template: np.ndarray, scale: float, angle: float, contrast: float
    ) -> np.ndarray:
        """Apply scale, rotation, and contrast to template."""
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

    def _is_duplicate_detection(
        self,
        center_x: int,
        center_y: int,
        detection_boxes: List[Tuple[int, int, int, int]],
    ) -> bool:
        """Check if detection overlaps with existing detections."""
        threshold = StandaloneConfig.DUPLICATE_THRESHOLD
        for dx, dy, dw, dh in detection_boxes:
            existing_center_x, existing_center_y = dx + dw // 2, dy + dh // 2
            if (
                abs(center_x - existing_center_x) < threshold
                and abs(center_y - existing_center_y) < threshold
            ):
                return True
        return False

    def _within_size_constraints(
        self,
        template_height: int,
        template_width: int,
        target_height: Optional[int],
        target_width: Optional[int],
        epsilon: float,
    ) -> bool:
        """Check if template size matches target constraints."""
        if target_height is None or target_width is None:
            return True

        min_height, max_height = (
            round(target_height * (1 - epsilon)),
            round(target_height * (1 + epsilon)),
        )
        min_width, max_width = (
            round(target_width * (1 - epsilon)),
            round(target_width * (1 + epsilon)),
        )

        return (
            min_height <= template_height <= max_height
            and min_width <= template_width <= max_width
        )

    def find_elements(
        self,
        image_path: str,
        min_confidence: float = StandaloneConfig.DEFAULT_MIN_CONFIDENCE,
        target_height: Optional[int] = None,
        target_width: Optional[int] = None,
        max_elements: int = StandaloneConfig.DEFAULT_MAX_ELEMENTS,
    ) -> List[Dict[str, int]]:
        """Find elements matching template image."""
        if not image_path:
            raise ValueError("Image path required.")

        template_original = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
        if template_original is None:
            raise ValueError(f"Template not found: {image_path}")

        screen = pyautogui.screenshot()
        screen_gray = cv2.cvtColor(np.array(screen), cv2.COLOR_RGB2GRAY)
        screen_gray_eq = cv2.equalizeHist(screen_gray)

        filtered = []
        detection_boxes = []
        epsilon = 1.0 - min_confidence
        transformations = product(
            StandaloneConfig.SCALE_FACTORS,
            StandaloneConfig.ROTATION_ANGLES,
            StandaloneConfig.CONTRAST_FACTORS,
        )

        for scale, angle, contrast in transformations:
            transformed = self._apply_transformations(
                template_original, scale, angle, contrast
            )
            t_height, t_width = transformed.shape[:2]

            if not self._within_size_constraints(
                t_height, t_width, target_height, target_width, epsilon
            ):
                continue

            for use_equalized in (False, True):
                current_template = (
                    cv2.equalizeHist(transformed) if use_equalized else transformed
                )
                current_screen = screen_gray_eq if use_equalized else screen_gray

                result = cv2.matchTemplate(
                    current_screen, current_template, cv2.TM_CCOEFF_NORMED
                )
                locations = np.where(result >= min_confidence)

                for x, y in zip(*locations[::-1]):
                    center_x, center_y = x + t_width // 2, y + t_height // 2

                    if self._is_duplicate_detection(
                        center_x, center_y, detection_boxes
                    ):
                        continue

                    filtered.append({"x": center_x, "y": center_y})
                    detection_boxes.append((x, y, t_width, t_height))

                    if max_elements and len(filtered) >= max_elements:
                        return filtered

        return filtered

    def find_text_elements(
        self,
        query: str,
        min_confidence: float = StandaloneConfig.DEFAULT_MIN_CONFIDENCE,
        max_elements: int = StandaloneConfig.DEFAULT_MAX_ELEMENTS,
        region: Optional[Tuple[int, int, int, int]] = None,
    ) -> List[Dict[str, int]]:
        """Find text elements using OCR."""
        if not self.ocr:
            raise RuntimeError("OCR disabled. Initialize with Emunium(ocr=True).")
        if not self.ocr_reader:
            raise ImportError("EasyOCR not installed. Run: pip install easyocr")

        region_offset = (region[0], region[1]) if region else (0, 0)
        img = pyautogui.screenshot(region=region) if region else pyautogui.screenshot()
        img_bgr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

        results = self.ocr_reader.readtext(img_bgr)
        found = []
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

    def move_to(
        self,
        element_center: Dict[str, int],
        offset_x: Optional[float] = None,
        offset_y: Optional[float] = None,
    ) -> None:
        """Move cursor to element."""
        self._move_to_position(element_center, offset_x, offset_y)

    def click_at(
        self, element_center: Dict[str, int], click_type: ClickType = ClickType.LEFT
    ) -> None:
        """Click at element."""
        self._perform_click((element_center["x"], element_center["y"]), click_type)

    def type_at(
        self,
        element_center: Dict[str, int],
        text: str,
        characters_per_minute: int = 280,
        offset: int = 20,
        click_type: ClickType = ClickType.LEFT,
    ) -> None:
        """Click element and type text."""
        self._perform_click((element_center["x"], element_center["y"]), click_type)
        self._type_text(text, characters_per_minute, offset)

    def scroll_to(self, element_center: Dict[str, int]) -> None:
        """Scroll to element."""
        self._scroll_to_element(element_center)
