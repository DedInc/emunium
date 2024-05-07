import asyncio
import math
import os
import random
import tempfile
import time
import struct

import keyboard
import pyautogui
import pyclick

from enum import Enum


class ClickType(Enum):
    LEFT = 0
    RIGHT = 1
    MIDDLE = 2
    DOUBLE = 3


def get_image_size(file_path):
    with open(file_path, "rb") as file:
        file.seek(16)
        width_bytes = file.read(4)
        height_bytes = file.read(4)
        width = struct.unpack(">I", width_bytes)[0]
        height = struct.unpack(">I", height_bytes)[0]
        return (
            width,
            height,
        )


class EmuniumBase:
    def __init__(self):
        self.clicker = pyclick.HumanClicker()
        self._extend_clicker()
        self.browser_offsets = ()
        self.browser_inner_window = ()

    def _extend_clicker(self):
        def right_click(self):
            pyautogui.click(button="right")

        def middle_click(self):
            pyautogui.click(button="middle")

        def double_click(self):
            pyautogui.doubleClick()

        self.clicker.right_click = right_click.__get__(self.clicker)
        self.clicker.middle_click = middle_click.__get__(self.clicker)
        self.clicker.double_click = double_click.__get__(self.clicker)

    async def _get_browser_properties_if_not_found(self, screenshot_func):
        if not self.browser_offsets or not self.browser_inner_window:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
                temp_screen_path = temp_file.name
            if asyncio.iscoroutinefunction(screenshot_func):
                await screenshot_func(temp_screen_path)
            else:
                screenshot_func(temp_screen_path)
            location = pyautogui.locateOnScreen(temp_screen_path, confidence=0.8)
            self.browser_offsets = (
                location.left,
                location.top,
            )
            self.browser_inner_window = get_image_size(temp_screen_path)
            os.remove(temp_screen_path)

    def _get_center(self, element_location, element_size):
        offset_to_screen_x, offset_to_screen_y = self.browser_offsets
        element_x = element_location["x"] + offset_to_screen_x
        element_y = element_location["y"] + offset_to_screen_y

        centered_x = element_x + (element_size["width"] // 2)
        centered_y = element_y + (element_size["height"] // 2)

        return {"x": centered_x, "y": centered_y}

    def _move(
        self,
        center,
        offset_x=random.uniform(0.0, 1.5),
        offset_y=random.uniform(0.0, 1.5),
    ):
        target_x, target_y = round(center["x"] + offset_x), round(
            center["y"] + offset_y
        )

        current_x, current_y = pyautogui.position()
        distance = math.sqrt((target_x - current_x) ** 2 + (target_y - current_y) ** 2)

        speed = max(
            random.uniform(0.3, 0.6),
            min(random.uniform(2.0, 2.5), distance / random.randint(500, 700)),
        )

        self.clicker.move((target_x, target_y), speed)

    def _click(self, click_type=ClickType.LEFT):
        if click_type == ClickType.LEFT:
            self.clicker.click()
        elif click_type == ClickType.RIGHT:
            self.clicker.right_click()
        elif click_type == ClickType.MIDDLE:
            self.clicker.middle_click()
        elif click_type == ClickType.DOUBLE:
            self.clicker.double_click()

    def silent_type(self, text, characters_per_minute=280, offset=20):
        total_chars = len(text)
        time_per_char = 60 / characters_per_minute

        for i, char in enumerate(text):
            randomized_offset = random.uniform(-offset, offset) / 1000
            delay = time_per_char + randomized_offset

            keyboard.write(char)
            time.sleep(delay)

    # Update by Pranav (https://github.com/ps428)
    # keyboard.write used in silent_type needs sudo mode on Linux machines
    # This uses pyautogui.press instead of keyboard.write
    def silent_type_pyautogui(self, text, characters_per_minute=280, offset=20):
        time_per_char = 60 / characters_per_minute

        for i, char in enumerate(text):
            randomized_offset = random.uniform(-offset, offset) / 1000
            delay = time_per_char + randomized_offset

            pyautogui.press(char)
            time.sleep(delay)

    def _scroll_smoothly_to_element(self, element_rect):
        window_width = self.browser_inner_window[0]
        window_height = self.browser_inner_window[1]

        scroll_amount = element_rect["y"] - window_height // 2
        scroll_steps = abs(scroll_amount) // 100

        if scroll_amount > 0:
            scroll_direction = -1
        else:
            scroll_direction = 1

        for _ in range(scroll_steps):
            pyautogui.scroll(scroll_direction * 100)
            time.sleep(random.uniform(0.05, 0.1))

        remaining_scroll = scroll_amount % 100
        if remaining_scroll != 0:
            pyautogui.scroll(scroll_direction * remaining_scroll)
            time.sleep(random.uniform(0.05, 0.1))
