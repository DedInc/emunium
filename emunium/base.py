import asyncio
import os
import random
import struct
import tempfile
import time
from enum import Enum

try:
    import keyboard
except ImportError:
    keyboard = None

import pyautogui
from humancursor import SystemCursor


def get_image_size(file_path):
    """Read PNG image dimensions from file header."""
    with open(file_path, "rb") as file:
        file.seek(16)
        width_bytes = file.read(4)
        height_bytes = file.read(4)
        width = struct.unpack(">I", width_bytes)[0]
        height = struct.unpack(">I", height_bytes)[0]
        return width, height


class ClickType(Enum):
    """Enumeration of supported mouse click types."""

    LEFT = 0
    RIGHT = 1
    MIDDLE = 2
    DOUBLE = 3


class EmuniumBase:
    """Base class for browser automation with human-like cursor movement."""

    DEFAULT_OFFSET_RANGE = (0.0, 1.5)
    SCROLL_STEP_SIZE = 100
    DOUBLE_CLICK_DELAY = 0.1
    SCROLL_DELAY_RANGE = (0.05, 0.1)

    def __init__(self):
        self.cursor = SystemCursor()
        self.browser_offsets = ()
        self.browser_inner_window = ()

    async def _get_browser_properties_if_not_found(self, screenshot_func):
        """Capture browser properties using screenshot for offset calculation."""
        if self.browser_offsets and self.browser_inner_window:
            return

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
            temp_screen_path = temp_file.name

        try:
            if asyncio.iscoroutinefunction(screenshot_func):
                await screenshot_func(temp_screen_path)
            else:
                screenshot_func(temp_screen_path)

            location = pyautogui.locateOnScreen(temp_screen_path, confidence=0.6)
            self.browser_offsets = (
                (location.left, location.top) if location else (0, 0)
            )
            self.browser_inner_window = get_image_size(temp_screen_path)
        finally:
            os.remove(temp_screen_path)

    def _get_center(self, element_location, element_size):
        """Calculate element center position with browser offset."""
        offset_x, offset_y = self.browser_offsets or (0, 0)
        return {
            "x": element_location["x"] + offset_x + element_size["width"] // 2,
            "y": element_location["y"] + offset_y + element_size["height"] // 2,
        }

    def _move(self, center, offset_x=None, offset_y=None):
        """Move cursor to center with optional random offset."""
        if offset_x is None:
            offset_x = random.uniform(*self.DEFAULT_OFFSET_RANGE)
        if offset_y is None:
            offset_y = random.uniform(*self.DEFAULT_OFFSET_RANGE)

        target_x = round(center["x"] + offset_x)
        target_y = round(center["y"] + offset_y)
        self.cursor.move_to([target_x, target_y])

    def _click(self, coordinate, click_type=ClickType.LEFT, click_duration=0):
        """Perform mouse click at coordinate with specified click type."""
        x, y = coordinate[0], coordinate[1]

        if click_type == ClickType.LEFT:
            self.cursor.click_on(coordinate, click_duration=click_duration)
        elif click_type == ClickType.RIGHT:
            pyautogui.click(x=x, y=y, button="right")
        elif click_type == ClickType.MIDDLE:
            pyautogui.click(x=x, y=y, button="middle")
        elif click_type == ClickType.DOUBLE:
            self.cursor.click_on(coordinate)
            time.sleep(self.DOUBLE_CLICK_DELAY)
            self.cursor.click_on(coordinate)

    def _silent_type(self, text, characters_per_minute=280, offset=20):
        """Type text with human-like timing variation."""
        time_per_char = 60 / characters_per_minute
        for char in text:
            delay = time_per_char + random.uniform(-offset, offset) / 1000
            if keyboard is None:
                pyautogui.press(char)
            else:
                keyboard.write(char)
            time.sleep(delay)

    def _scroll_smoothly_to_element(self, element_rect):
        """Scroll to element with smooth, human-like movement."""
        if self.browser_inner_window:
            _, window_height = self.browser_inner_window
        else:
            window_height = pyautogui.size().height

        scroll_amount = element_rect["y"] - window_height // 2
        scroll_direction = -1 if scroll_amount > 0 else 1
        abs_scroll = abs(scroll_amount)

        scroll_steps = int(abs_scroll // self.SCROLL_STEP_SIZE)
        for _ in range(scroll_steps):
            pyautogui.scroll(scroll_direction * self.SCROLL_STEP_SIZE)
            time.sleep(random.uniform(*self.SCROLL_DELAY_RANGE))

        remaining_scroll = int(abs_scroll % self.SCROLL_STEP_SIZE)
        if remaining_scroll:
            pyautogui.scroll(scroll_direction * remaining_scroll)
            time.sleep(random.uniform(*self.SCROLL_DELAY_RANGE))

    def drag_and_drop(self, start_coords, end_coords):
        """Perform drag and drop operation between two coordinates."""
        self.cursor.drag_and_drop(start_coords, end_coords)
