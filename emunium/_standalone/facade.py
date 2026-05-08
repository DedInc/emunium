from __future__ import annotations

import random
import time

import pyautogui
from humancursor import SystemCursor

from emunium._standalone import ocr, vision
from emunium._standalone.config import ClickType, Config, StandaloneConfig


class Emunium:
    def __init__(
        self,
        ocr_enabled: bool = False,
        use_gpu: bool = True,
        langs: list[str] | None = None,
    ) -> None:
        self.cursor = SystemCursor()
        self.ocr = ocr_enabled
        self.ocr_reader = None
        self.monitor_region: tuple[int, int, int, int] | None = None

        if self.ocr:
            screen_width, screen_height = pyautogui.size()
            self.monitor_region = (0, 0, screen_width, screen_height)
            self.ocr_reader = ocr.initialize_ocr(langs or ["en"], use_gpu)

    def find_elements(
        self,
        image_path: str,
        min_confidence: float = StandaloneConfig.DEFAULT_MIN_CONFIDENCE,
        target_height: int | None = None,
        target_width: int | None = None,
        max_elements: int = StandaloneConfig.DEFAULT_MAX_ELEMENTS,
    ) -> list[dict[str, int]]:
        return vision.find_elements(
            image_path, min_confidence, target_height, target_width, max_elements
        )

    def find_text_elements(
        self,
        query: str,
        min_confidence: float = StandaloneConfig.DEFAULT_MIN_CONFIDENCE,
        max_elements: int = StandaloneConfig.DEFAULT_MAX_ELEMENTS,
        region: tuple[int, int, int, int] | None = None,
    ) -> list[dict[str, int]]:
        if not self.ocr:
            raise RuntimeError("OCR disabled. Initialize with Emunium(ocr=True).")
        return ocr.find_text_elements(
            self.ocr_reader, query, min_confidence, max_elements, region
        )

    def wait_for_image(
        self,
        image_path: str,
        timeout: float = 10.0,
        poll_interval: float = 0.3,
        min_confidence: float = 0.8,
        target_height: int | None = None,
        target_width: int | None = None,
        raise_on_timeout: bool = True,
    ) -> dict[str, int] | None:
        from emunium._standalone import wait as _wait

        return _wait.wait_for_image(
            self,
            image_path,
            timeout,
            poll_interval,
            min_confidence,
            target_height,
            target_width,
            raise_on_timeout,
        )

    def wait_for_text_ocr(
        self,
        query: str,
        timeout: float = 10.0,
        poll_interval: float = 0.3,
        min_confidence: float = 0.8,
        region: tuple[int, int, int, int] | None = None,
        raise_on_timeout: bool = True,
    ) -> dict[str, int] | None:
        from emunium._standalone import wait as _wait

        return _wait.wait_for_text_ocr(
            self,
            query,
            timeout,
            poll_interval,
            min_confidence,
            region,
            raise_on_timeout,
        )

    def move_to(
        self,
        element_center: dict[str, int],
        offset_x: float | None = None,
        offset_y: float | None = None,
    ) -> None:
        ox = (
            offset_x
            if offset_x is not None
            else random.uniform(*Config.DEFAULT_OFFSET_RANGE)
        )
        oy = (
            offset_y
            if offset_y is not None
            else random.uniform(*Config.DEFAULT_OFFSET_RANGE)
        )
        target_x = round(element_center["x"] + ox)
        target_y = round(element_center["y"] + oy)
        self.cursor.move_to([target_x, target_y])

    def click_at(
        self,
        element_center: dict[str, int],
        click_type: ClickType = ClickType.LEFT,
    ) -> None:
        x, y = int(element_center["x"]), int(element_center["y"])
        coord = (x, y)

        if click_type == ClickType.LEFT:
            self.cursor.click_on(coord)
        elif click_type == ClickType.DOUBLE:
            self.cursor.click_on(coord)
            time.sleep(Config.DOUBLE_CLICK_DELAY)
            self.cursor.click_on(coord)
        else:
            button_map = {ClickType.RIGHT: "right", ClickType.MIDDLE: "middle"}
            pyautogui.click(x=x, y=y, button=button_map[click_type])

    def type_at(
        self,
        element_center: dict[str, int],
        text: str,
        characters_per_minute: int = 280,
        offset: int = 20,
        click_type: ClickType = ClickType.LEFT,
    ) -> None:
        self.click_at(element_center, click_type)
        self.type_text(text, characters_per_minute, offset)

    @staticmethod
    def type_text(
        text: str, characters_per_minute: int = 280, offset: int = 20
    ) -> None:
        try:
            import keyboard as _keyboard
        except ImportError:
            _keyboard = None

        time_per_char = 60 / characters_per_minute
        for char in text:
            delay = time_per_char + random.uniform(-offset, offset) / 1000
            if _keyboard:
                _keyboard.write(char)
            else:
                pyautogui.typewrite(char, interval=0)
            time.sleep(max(0, delay))

    def scroll_to(self, element_center: dict[str, int]) -> None:
        window_height = pyautogui.size().height
        scroll_amount = element_center["y"] - window_height // 2

        if scroll_amount == 0:
            return

        scroll_direction = -1 if scroll_amount > 0 else 1
        abs_scroll = abs(scroll_amount)

        scroll_steps = int(abs_scroll // Config.SCROLL_STEP_SIZE)
        for _ in range(scroll_steps):
            pyautogui.scroll(scroll_direction * Config.SCROLL_STEP_SIZE)
            time.sleep(random.uniform(*Config.SCROLL_DELAY_RANGE))

        remaining = int(abs_scroll % Config.SCROLL_STEP_SIZE)
        if remaining:
            pyautogui.scroll(scroll_direction * remaining)
            time.sleep(random.uniform(*Config.SCROLL_DELAY_RANGE))

    def drag_and_drop(
        self, start_coords: tuple[int, int], end_coords: tuple[int, int]
    ) -> None:
        self.cursor.drag_and_drop(start_coords, end_coords)
