from __future__ import annotations

from enum import Enum


class ClickType(Enum):
    LEFT = 0
    RIGHT = 1
    MIDDLE = 2
    DOUBLE = 3


class Config:
    DEFAULT_OFFSET_RANGE = (0.0, 1.5)
    SCROLL_STEP_SIZE = 100
    DOUBLE_CLICK_DELAY = 0.1
    SCROLL_DELAY_RANGE = (0.05, 0.1)
    SCREENSHOT_CONFIDENCE = 0.6


class StandaloneConfig:
    SCALE_FACTORS = (0.9, 1.0, 1.1)
    ROTATION_ANGLES = (-10, 0, 10)
    CONTRAST_FACTORS = (0.9, 1.0, 1.1)
    DUPLICATE_THRESHOLD = 10
    DEFAULT_MIN_CONFIDENCE = 0.8
    DEFAULT_MAX_ELEMENTS = 0
