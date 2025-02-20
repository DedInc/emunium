import random
import pyautogui
import cv2
import numpy as np
from .base import EmuniumBase, ClickType

class Emunium(EmuniumBase):
    def __init__(self, ocr=False, use_gpu=True, langs=['en']):
        super().__init__()
        self.ocr = ocr
        self.ocr_reader = None

        if self.ocr:
            screen_width, screen_height = pyautogui.size()
            self.monitor_region = (0, 0, screen_width, screen_height)
            try:
                from easyocr import Reader
            except ImportError:
                Reader = None

            if Reader:
                if use_gpu:
                    try:
                        from torch import cuda
                        gpu_flag = cuda.is_available()
                    except ImportError:
                        gpu_flag = False
                else:
                    gpu_flag = False
                self.ocr_reader = Reader(langs, gpu=gpu_flag)

    def transform_template(self, template, scale, angle, contrast):
        height, width = template.shape[:2]
        new_width = int(width * scale)
        new_height = int(height * scale)
        if new_width <= 0 or new_height <= 0:
            return template
        resized = cv2.resize(template, (new_width, new_height), interpolation=cv2.INTER_AREA)
        contrasted = cv2.convertScaleAbs(resized, alpha=contrast, beta=0)
        (h, w) = contrasted.shape[:2]
        center = (w // 2, h // 2)
        M = cv2.getRotationMatrix2D(center, angle, 1.0)
        rotated = cv2.warpAffine(contrasted, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        return rotated

    def find_elements(self, image_path=None, min_confidence=0.8, target_height=None, target_width=None, max_elements=0):
        if image_path is None:
            raise ValueError("Image path must be provided.")

        screen = pyautogui.screenshot()
        screen = cv2.cvtColor(np.array(screen), cv2.COLOR_RGB2BGR)
        screen_gray = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)
        screen_gray_eq = cv2.equalizeHist(screen_gray)

        template_original = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
        if template_original is None:
            raise ValueError("Template image not found at path: " + image_path)

        filtered = []
        detection_boxes = []

        scale_factors = [0.9, 1.0, 1.1]
        rotation_angles = [-10, 0, 10]
        contrast_factors = [0.9, 1.0, 1.1]

        for scale in scale_factors:
            for angle in rotation_angles:
                for contrast in contrast_factors:
                    transformed = self.transform_template(template_original, scale, angle, contrast)
                    tH, tW = transformed.shape[:2]

                    if target_height is not None and target_width is not None:
                        epsilon = 1.0 - min_confidence
                        min_height = round(target_height - target_height * epsilon)
                        max_height = round(target_height + target_height * epsilon)
                        min_width = round(target_width - target_width * epsilon)
                        max_width = round(target_width + target_width * epsilon)
                        if not (min_height <= tH <= max_height and min_width <= tW <= max_width):
                            continue

                    for use_filter in [False, True]:
                        if use_filter:
                            current_template = cv2.equalizeHist(transformed)
                            current_screen = screen_gray_eq
                        else:
                            current_template = transformed
                            current_screen = screen_gray

                        result = cv2.matchTemplate(current_screen, current_template, cv2.TM_CCOEFF_NORMED)
                        loc = np.where(result >= min_confidence)
                        for pt in zip(*loc[::-1]):
                            x, y = pt
                            center_x = x + tW // 2
                            center_y = y + tH // 2

                            duplicate = False
                            for (dx, dy, dw, dh) in detection_boxes:
                                if abs(center_x - (dx + dw // 2)) < 10 and abs(center_y - (dy + dh // 2)) < 10:
                                    duplicate = True
                                    break
                            if duplicate:
                                continue

                            filtered.append({'x': center_x, 'y': center_y})
                            detection_boxes.append((x, y, tW, tH))
                            if max_elements and len(filtered) >= max_elements:
                                return filtered
        return filtered

    def find_text_elements(self, query, min_confidence=0.8, max_elements=0, region=None):
        if not self.ocr:
            raise ImportError("OCR is disabled. Please use Emunium(ocr=True) instance")
        if self.ocr_reader is None:
            raise ImportError("EasyOCR is not installed. Please install it using: pip install easyocr")

        region_offset = (0, 0)

        if region is not None:
            region_offset = (region[0], region[1])
            img = pyautogui.screenshot(region=region)
        else:
            img = pyautogui.screenshot()

        img = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

        results = self.ocr_reader.readtext(img)
        found = []
        for bbox, text, conf in results:
            if conf >= min_confidence and query.lower() in text.lower():
                pts = np.array(bbox, dtype="int")
                center_x = int(np.mean(pts[:, 0]))
                center_y = int(np.mean(pts[:, 1]))
                found.append({'x': center_x + region_offset[0], 'y': center_y + region_offset[1]})
                if max_elements and len(found) >= max_elements:
                    return found
        return found

    def move_to(self, element_center, offset_x=random.uniform(0.0, 1.5), offset_y=random.uniform(0.0, 1.5)):
        self._move(element_center, offset_x, offset_y)

    def click_at(self, element_center, click_type=ClickType.LEFT):
        self._click([element_center['x'], element_center['y']], click_type=click_type)

    def type_at(self, element_center, text, characters_per_minute=280, offset=20, click_type=ClickType.LEFT):
        self._click([element_center['x'], element_center['y']], click_type=click_type)
        self._silent_type(text, characters_per_minute, offset)

    def scroll_to(self, element_center):
        self._scroll_smoothly_to_element(element_center)
