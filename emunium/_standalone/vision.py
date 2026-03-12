from __future__ import annotations

from itertools import product

import pyautogui

from emunium._standalone.config import StandaloneConfig


def apply_transformations(
    template: object, scale: float, angle: float, contrast: float
) -> object:
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


def is_duplicate_detection(
    center_x: int,
    center_y: int,
    detection_boxes: list[tuple[int, int, int, int]],
) -> bool:
    threshold = StandaloneConfig.DUPLICATE_THRESHOLD
    for dx, dy, dw, dh in detection_boxes:
        existing_center_x, existing_center_y = dx + dw // 2, dy + dh // 2
        if (
            abs(center_x - existing_center_x) < threshold
            and abs(center_y - existing_center_y) < threshold
        ):
            return True
    return False


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


def find_elements(
    image_path: str,
    min_confidence: float = StandaloneConfig.DEFAULT_MIN_CONFIDENCE,
    target_height: int | None = None,
    target_width: int | None = None,
    max_elements: int = StandaloneConfig.DEFAULT_MAX_ELEMENTS,
) -> list[dict[str, int]]:
    import cv2
    import numpy as np

    if not image_path:
        raise ValueError("Image path required.")

    template_original = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
    if template_original is None:
        raise ValueError(f"Template not found: {image_path}")

    screen = pyautogui.screenshot()
    screen_gray = cv2.cvtColor(np.array(screen), cv2.COLOR_RGB2GRAY)
    screen_gray_eq = cv2.equalizeHist(screen_gray)

    filtered: list[dict[str, int]] = []
    detection_boxes: list[tuple[int, int, int, int]] = []
    epsilon = 1.0 - min_confidence
    transformations = product(
        StandaloneConfig.SCALE_FACTORS,
        StandaloneConfig.ROTATION_ANGLES,
        StandaloneConfig.CONTRAST_FACTORS,
    )

    for scale, angle, contrast in transformations:
        transformed = apply_transformations(template_original, scale, angle, contrast)
        t_height, t_width = transformed.shape[:2]

        if not within_size_constraints(
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
                center_x = x + t_width // 2
                center_y = y + t_height // 2

                if is_duplicate_detection(center_x, center_y, detection_boxes):
                    continue

                filtered.append({"x": int(center_x), "y": int(center_y)})
                detection_boxes.append((x, y, t_width, t_height))

                if max_elements and len(filtered) >= max_elements:
                    return filtered

    return filtered
