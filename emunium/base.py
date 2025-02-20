import asyncio
import math
import os
import random
import tempfile
import time
import struct

try:
    import keyboard
except ImportError:
    keyboard = None

import pyautogui
from humancursor import SystemCursor
from enum import Enum

def get_image_size(file_path):
    with open(file_path, "rb") as file:
        file.seek(16)
        width_bytes = file.read(4)
        height_bytes = file.read(4)
        width = struct.unpack(">I", width_bytes)[0]
        height = struct.unpack(">I", height_bytes)[0]
        return (width, height)

class ClickType(Enum):
    LEFT = 0
    RIGHT = 1
    MIDDLE = 2
    DOUBLE = 3

class EmuniumBase:
    def __init__(self):

        self.cursor = SystemCursor()
        self.browser_offsets = ()
        self.browser_inner_window = ()

    async def _get_browser_properties_if_not_found(self, screenshot_func):
        if not self.browser_offsets or not self.browser_inner_window:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
                temp_screen_path = temp_file.name
            if asyncio.iscoroutinefunction(screenshot_func):
                await screenshot_func(temp_screen_path)
            else:
                screenshot_func(temp_screen_path)

            location = pyautogui.locateOnScreen(temp_screen_path, confidence=0.6)
            if location is not None:
                self.browser_offsets = (location.left, location.top)
            else:
                self.browser_offsets = (0, 0)
            self.browser_inner_window = get_image_size(temp_screen_path)
            os.remove(temp_screen_path)

    def _get_center(self, element_location, element_size):
        offset_to_screen_x, offset_to_screen_y = self.browser_offsets if self.browser_offsets else (0, 0)
        element_x = element_location["x"] + offset_to_screen_x
        element_y = element_location["y"] + offset_to_screen_y
        centered_x = element_x + (element_size["width"] // 2)
        centered_y = element_y + (element_size["height"] // 2)
        return {"x": centered_x, "y": centered_y}

    def _move(self, center, offset_x=None, offset_y=None):
        if offset_x is None:
            offset_x = random.uniform(0.0, 1.5)
        if offset_y is None:
            offset_y = random.uniform(0.0, 1.5)
        target_x = round(center["x"] + offset_x)
        target_y = round(center["y"] + offset_y)
        self.cursor.move_to([target_x, target_y])

    def _click(self, coordinate, click_type=ClickType.LEFT, click_duration=0):
        if click_type == ClickType.LEFT:
            self.cursor.click_on(coordinate, click_duration=click_duration)
        elif click_type == ClickType.RIGHT:
            pyautogui.click(x=coordinate[0], y=coordinate[1], button="right")
        elif click_type == ClickType.MIDDLE:
            pyautogui.click(x=coordinate[0], y=coordinate[1], button="middle")
        elif click_type == ClickType.DOUBLE:

            self.cursor.click_on(coordinate)
            time.sleep(0.1)
            self.cursor.click_on(coordinate)

    def _silent_type(self, text, characters_per_minute=280, offset=20):
        time_per_char = 60 / characters_per_minute
        for char in text:
            randomized_offset = random.uniform(-offset, offset) / 1000
            delay = time_per_char + randomized_offset
            if keyboard is None:
                pyautogui.press(char)
            else:
                keyboard.write(char)
            time.sleep(delay)

    def _scroll_smoothly_to_element(self, element_rect):
        if self.browser_inner_window:
            window_width, window_height = self.browser_inner_window
        else:
            screen_size = pyautogui.size()
            window_width, window_height = screen_size.width, screen_size.height

        scroll_amount = element_rect["y"] - window_height // 2
        scroll_steps = abs(scroll_amount) // 100
        scroll_direction = -1 if scroll_amount > 0 else 1

        for _ in range(scroll_steps):
            pyautogui.scroll(scroll_direction * 100)
            time.sleep(random.uniform(0.05, 0.1))

        remaining_scroll = scroll_amount % 100
        if remaining_scroll != 0:
            pyautogui.scroll(scroll_direction * remaining_scroll)
            time.sleep(random.uniform(0.05, 0.1))

    def drag_and_drop(self, start_coords, end_coords):
        self.cursor.drag_and_drop(start_coords, end_coords)
