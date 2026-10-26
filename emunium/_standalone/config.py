from __future__ import annotations

from enum import Enum


class ClickType(Enum):
    LEFT = 0
    RIGHT = 1
    MIDDLE = 2
    DOUBLE = 3


class Config:
    DEFAULT_OFFSET_RANGE = (0.0, 1.5)
    # Pixels scrolled per wheel notch (scroll_to converts pixels to notches).
    SCROLL_STEP_SIZE = 100
    # Raw wheel delta of one notch on Windows (WHEEL_DELTA); pyautogui passes the
    # amount through as-is there, while macOS/Linux take notches (lines/clicks).
    WINDOWS_WHEEL_DELTA = 120
    # Unused since 3.1 (double clicks use the OS double-click interval); kept for
    # compatibility.
    DOUBLE_CLICK_DELAY = 0.1
    SCROLL_DELAY_RANGE = (0.05, 0.1)
    SCREENSHOT_CONFIDENCE = 0.6


class StandaloneConfig:
    SCALE_FACTORS = (0.9, 1.0, 1.1)
    ROTATION_ANGLES = (-10, 0, 10)
    CONTRAST_FACTORS = (0.9, 1.0, 1.1)
    # Unused since 3.1 (replaced by DUPLICATE_OVERLAP); kept for compatibility.
    DUPLICATE_THRESHOLD = 10
    # Two matches are duplicates when their intersection covers at least this
    # fraction of the smaller box.
    DUPLICATE_OVERLAP = 0.5
    # Best-scoring candidates kept per template pass (scale x angle x contrast x
    # equalization) before cross-pass ranking and duplicate suppression.
    MAX_CANDIDATES_PER_PASS = 500
    DEFAULT_MIN_CONFIDENCE = 0.8
    DEFAULT_MAX_ELEMENTS = 0
