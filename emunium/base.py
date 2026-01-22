import asyncio
import os
import random
import struct
import tempfile
import time
from enum import Enum
from typing import Tuple, Callable, Optional, Dict

try:
    import keyboard
except ImportError:
    keyboard = None

import pyautogui
from humancursor import SystemCursor


class Config:
    """Centralized configuration constants."""

    DEFAULT_OFFSET_RANGE = (0.0, 1.5)
    SCROLL_STEP_SIZE = 100
    DOUBLE_CLICK_DELAY = 0.1
    SCROLL_DELAY_RANGE = (0.05, 0.1)
    PNG_HEADER_OFFSET = 16
    PNG_DIMENSION_SIZE = 4
    SCREENSHOT_CONFIDENCE = 0.6


def _read_png_dimensions(file_path: str) -> Tuple[int, int]:
    """Read PNG dimensions from file header."""
    try:
        with open(file_path, "rb") as file:
            header = file.read(8)
            if len(header) < 8 or header[:4] != b"\x89PNG":
                raise ValueError(f"Invalid PNG file: {file_path}")
            file.seek(Config.PNG_HEADER_OFFSET)
            width_data = file.read(Config.PNG_DIMENSION_SIZE)
            height_data = file.read(Config.PNG_DIMENSION_SIZE)
            if len(width_data) < 4 or len(height_data) < 4:
                raise ValueError(f"Corrupted PNG file: {file_path}")
            width = struct.unpack(">I", width_data)[0]
            height = struct.unpack(">I", height_data)[0]
            return width, height
    except (IOError, struct.error) as e:
        raise ValueError(f"Failed to read PNG dimensions from {file_path}: {e}")


def _apply_random_offset(value: float, offset_range: Tuple[float, float]) -> float:
    """Apply random offset within specified range."""
    return value + random.uniform(*offset_range)


class ClickType(Enum):
    """Enumeration of supported mouse click types."""

    LEFT = 0
    RIGHT = 1
    MIDDLE = 2
    DOUBLE = 3


class EmuniumBase:
    """Base class for browser automation with human-like cursor movement."""

    def __init__(self):
        self.cursor = SystemCursor()
        self.browser_offsets: Tuple[int, int] = (0, 0)
        self.browser_inner_window: Tuple[int, int] = (0, 0)

    async def _get_browser_properties_if_not_found(
        self, screenshot_func: Callable
    ) -> None:
        """Capture browser properties via screenshot."""
        if self.browser_offsets != (0, 0) and self.browser_inner_window != (0, 0):
            return

        temp_screen_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
                temp_screen_path = temp_file.name

            if asyncio.iscoroutinefunction(screenshot_func):
                await screenshot_func(temp_screen_path)
            else:
                screenshot_func(temp_screen_path)

            if not os.path.exists(temp_screen_path):
                raise IOError(f"Screenshot failed: {temp_screen_path} not created")

            location = pyautogui.locateOnScreen(
                temp_screen_path, confidence=Config.SCREENSHOT_CONFIDENCE
            )
            if location:
                self.browser_offsets = (location.left, location.top)
                self.browser_inner_window = _read_png_dimensions(temp_screen_path)
        except (IOError, ValueError):
            # Log error but don't crash - fallback to default offsets
            pass
        finally:
            if temp_screen_path and os.path.exists(temp_screen_path):
                try:
                    os.remove(temp_screen_path)
                except OSError:
                    pass

    def _calculate_center(
        self, element_location: Dict[str, int], element_size: Dict[str, int]
    ) -> Dict[str, int]:
        """Calculate element center with browser offset."""
        offset_x, offset_y = self.browser_offsets
        return {
            "x": int(element_location["x"] + offset_x + element_size["width"] // 2),
            "y": int(element_location["y"] + offset_y + element_size["height"] // 2),
        }

    def _move_to_position(
        self,
        center: Dict[str, int],
        offset_x: Optional[float] = None,
        offset_y: Optional[float] = None,
    ) -> None:
        """Move cursor with optional offset."""
        offset_x = (
            offset_x
            if offset_x is not None
            else random.uniform(*Config.DEFAULT_OFFSET_RANGE)
        )
        offset_y = (
            offset_y
            if offset_y is not None
            else random.uniform(*Config.DEFAULT_OFFSET_RANGE)
        )

        target_x = round(center["x"] + offset_x)
        target_y = round(center["y"] + offset_y)
        self.cursor.move_to([target_x, target_y])

    def _perform_click(
        self,
        coordinate: Tuple[int, int],
        click_type: ClickType = ClickType.LEFT,
        click_duration: float = 0,
    ) -> None:
        """Perform mouse click at coordinate."""
        x, y = int(coordinate[0]), int(coordinate[1])
        coordinate = (x, y)

        if click_type == ClickType.LEFT:
            self.cursor.click_on(coordinate, click_duration=click_duration)
        elif click_type == ClickType.DOUBLE:
            self.cursor.click_on(coordinate)
            time.sleep(Config.DOUBLE_CLICK_DELAY)
            self.cursor.click_on(coordinate)
        else:
            button_map = {ClickType.RIGHT: "right", ClickType.MIDDLE: "middle"}
            pyautogui.click(x=x, y=y, button=button_map[click_type])

    def _type_text(
        self, text: str, characters_per_minute: int = 280, offset: int = 20
    ) -> None:
        """Type text with human-like timing."""
        time_per_char = 60 / characters_per_minute

        for char in text:
            delay = time_per_char + random.uniform(-offset, offset) / 1000
            if keyboard:
                keyboard.write(char)
            else:
                pyautogui.typewrite(char, interval=0)
            time.sleep(delay)

    def _scroll_to_element(self, element_rect: Dict[str, int]) -> None:
        """Scroll to element smoothly."""
        window_height = (
            self.browser_inner_window[1]
            if self.browser_inner_window != (0, 0)
            else pyautogui.size().height
        )
        scroll_amount = element_rect["y"] - window_height // 2

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
        self, start_coords: Tuple[int, int], end_coords: Tuple[int, int]
    ) -> None:
        """Drag and drop between coordinates."""
        self.cursor.drag_and_drop(start_coords, end_coords)
