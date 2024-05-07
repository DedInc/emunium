import random
import pyautogui
from .base import EmuniumBase, ClickType

class Emunium(EmuniumBase):
    def __init__(self):
        super().__init__()

    def find_elements(self, image_path=None, min_confidence=0.8, target_height=None, target_width=None, max_elements=0):
        filtered = []

        elements = pyautogui.locateAllOnScreen(image_path, confidence=min_confidence)

        if target_height is not None and target_width is not None:
            epsilon = 1.0 - min_confidence
            min_height = round(target_height - target_height * epsilon)
            max_height = round(target_height + target_height * epsilon)
            min_width = round(target_width - target_width * epsilon)
            max_width = round(target_width + target_width * epsilon)

            for element in elements:
                _, _, width, height = element

                if min_height <= height <= max_height and min_width <= width <= max_width:
                    center_x, center_y = pyautogui.center(element)

                    if not any(abs(element['y'] - center_y) <= max_height for element in filtered):
                        filtered.append({'x': center_x, 'y': center_y})
                        if max_elements and len(filtered) >= max_elements:
                            break
        else:
            for element in elements:
                center_x, center_y = pyautogui.center(element)
                filtered.append({'x': center_x, 'y': center_y})

        if max_elements:
            return filtered[:max_elements]

        return filtered

    def move_to(self, element_center, offset_x=random.uniform(0.0, 1.5), offset_y=random.uniform(0.0, 1.5)):
        self._move(element_center, offset_x, offset_y)

    def click_at(self, element_center, click_type=ClickType.LEFT):
        self._move(element_center)
        self._click(click_type)

    def type_at(self, element_center, text, characters_per_minute=280, offset=20, click_type=ClickType.LEFT):
        self._move(element_center)
        self._click(click_type)
        self.silent_type(text, characters_per_minute, offset)

    def scroll_to(self, element_center):
        raise NotImplementedError("Scroll functionality is not supported for the Emunium class.")