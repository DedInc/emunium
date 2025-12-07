from itertools import product
from typing import Optional

import cv2
import numpy as np
import pyautogui

from .base import ClickType, EmuniumBase


class Emunium(EmuniumBase):
    """Standalone automation with image and OCR-based element detection."""

    SCALE_FACTORS = (0.9, 1.0, 1.1)
    ROTATION_ANGLES = (-10, 0, 10)
    CONTRAST_FACTORS = (0.9, 1.0, 1.1)
    DUPLICATE_THRESHOLD = 10

    def __init__(self, ocr: bool = False, use_gpu: bool = True, langs: Optional[list] = None):
        super().__init__()
        self.ocr = ocr
        self.ocr_reader = None

        if langs is None:
            langs = ["en"]

        if self.ocr:
            screen_width, screen_height = pyautogui.size()
            self.monitor_region = (0, 0, screen_width, screen_height)
            self._initialize_ocr(langs, use_gpu)

    def _initialize_ocr(self, langs: list, use_gpu: bool):
        """Initialize EasyOCR reader with GPU support if available."""
        try:
            from easyocr import Reader
        except ImportError:
            return

        gpu_flag = False
        if use_gpu:
            try:
                from torch import cuda

                gpu_flag = cuda.is_available()
            except ImportError:
                pass

        self.ocr_reader = Reader(langs, gpu=gpu_flag)

    def _transform_template(self, template, scale: float, angle: float, contrast: float):
        """Apply scale, rotation, and contrast transformations to template."""
        height, width = template.shape[:2]
        new_width = int(width * scale)
        new_height = int(height * scale)

        if new_width <= 0 or new_height <= 0:
            return template

        resized = cv2.resize(
            template, (new_width, new_height), interpolation=cv2.INTER_AREA
        )
        contrasted = cv2.convertScaleAbs(resized, alpha=contrast, beta=0)

        h, w = contrasted.shape[:2]
        center = (w // 2, h // 2)
        rotation_matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
        return cv2.warpAffine(
            contrasted,
            rotation_matrix,
            (w, h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REPLICATE,
        )

    def _is_duplicate(self, center_x: int, center_y: int, detection_boxes: list) -> bool:
        """Check if detection is duplicate of existing detection."""
        for dx, dy, dw, dh in detection_boxes:
            existing_center_x = dx + dw // 2
            existing_center_y = dy + dh // 2
            if (
                abs(center_x - existing_center_x) < self.DUPLICATE_THRESHOLD
                and abs(center_y - existing_center_y) < self.DUPLICATE_THRESHOLD
            ):
                return True
        return False

    def _check_size_constraints(
        self,
        template_height: int,
        template_width: int,
        target_height: Optional[int],
        target_width: Optional[int],
        epsilon: float,
    ) -> bool:
        """Check if template size is within target constraints."""
        if target_height is None or target_width is None:
            return True

        min_height = round(target_height * (1 - epsilon))
        max_height = round(target_height * (1 + epsilon))
        min_width = round(target_width * (1 - epsilon))
        max_width = round(target_width * (1 + epsilon))

        return min_height <= template_height <= max_height and min_width <= template_width <= max_width

    def find_elements(
        self,
        image_path: str,
        min_confidence: float = 0.8,
        target_height: Optional[int] = None,
        target_width: Optional[int] = None,
        max_elements: int = 0,
    ) -> list:
        """Find elements matching template image on screen."""
        if image_path is None:
            raise ValueError("Image path must be provided.")

        screen = pyautogui.screenshot()
        screen_bgr = cv2.cvtColor(np.array(screen), cv2.COLOR_RGB2BGR)
        screen_gray = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY)
        screen_gray_eq = cv2.equalizeHist(screen_gray)

        template_original = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
        if template_original is None:
            raise ValueError(f"Template image not found at path: {image_path}")

        filtered = []
        detection_boxes = []
        epsilon = 1.0 - min_confidence

        transformations = product(
            self.SCALE_FACTORS, self.ROTATION_ANGLES, self.CONTRAST_FACTORS
        )

        for scale, angle, contrast in transformations:
            transformed = self._transform_template(template_original, scale, angle, contrast)
            t_height, t_width = transformed.shape[:2]

            if not self._check_size_constraints(
                t_height, t_width, target_height, target_width, epsilon
            ):
                continue

            for use_equalized in (False, True):
                if use_equalized:
                    current_template = cv2.equalizeHist(transformed)
                    current_screen = screen_gray_eq
                else:
                    current_template = transformed
                    current_screen = screen_gray

                result = cv2.matchTemplate(
                    current_screen, current_template, cv2.TM_CCOEFF_NORMED
                )
                locations = np.where(result >= min_confidence)

                for x, y in zip(*locations[::-1]):
                    center_x = x + t_width // 2
                    center_y = y + t_height // 2

                    if self._is_duplicate(center_x, center_y, detection_boxes):
                        continue

                    filtered.append({"x": center_x, "y": center_y})
                    detection_boxes.append((x, y, t_width, t_height))

                    if max_elements and len(filtered) >= max_elements:
                        return filtered

        return filtered

    def find_text_elements(
        self,
        query: str,
        min_confidence: float = 0.8,
        max_elements: int = 0,
        region: Optional[tuple] = None,
    ) -> list:
        """Find text elements matching query using OCR."""
        if not self.ocr:
            raise ImportError("OCR is disabled. Please use Emunium(ocr=True) instance")
        if self.ocr_reader is None:
            raise ImportError(
                "EasyOCR is not installed. Please install it using: pip install easyocr"
            )

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
                    return found

        return found

    def move_to(self, element_center: dict, offset_x: Optional[float] = None, offset_y: Optional[float] = None):
        """Move cursor to element center with optional offset."""
        self._move(element_center, offset_x, offset_y)

    def click_at(self, element_center: dict, click_type: ClickType = ClickType.LEFT):
        """Click at element center."""
        self._click([element_center["x"], element_center["y"]], click_type=click_type)

    def type_at(
        self,
        element_center: dict,
        text: str,
        characters_per_minute: int = 280,
        offset: int = 20,
        click_type: ClickType = ClickType.LEFT,
    ):
        """Click element and type text with human-like timing."""
        self._click([element_center["x"], element_center["y"]], click_type=click_type)
        self._silent_type(text, characters_per_minute, offset)

    def scroll_to(self, element_center: dict):
        """Scroll to bring element into view."""
        self._scroll_smoothly_to_element(element_center)