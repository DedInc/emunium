"""DOM-aware browser automation with native OS-level mouse and keyboard input.

DPI awareness is chosen before anything else in the package is imported, so a
later ``import pyautogui`` (or any other DPI-unaware GUI library) cannot pin the
process to scaled coordinates first. No module imported here loads pyautogui,
OpenCV, mss or a display connection; those load on first use.
"""

from emunium import _screen

_screen.ensure_dpi_awareness()

from emunium._motion import (
    BezierTremorPlanner,
    FittsTiming,
    LinearPlanner,
    MotionPlanner,
    MotionStep,
    Overshoot,
    ScreenBounds,
    SigmaDriftPlanner,
    Tremor,
    TypingRhythm,
    WindMousePlanner,
)
from emunium._standalone.config import ClickType
from emunium.bridge import Bridge
from emunium.browser import Browser
from emunium.chrome_installer import ensure_chrome
from emunium.coords import CoordsStore, ElementRecord
from emunium.element import Element
from emunium.errors import (
    BridgeDisconnectedError,
    BridgeError,
    BridgeProtocolError,
    BridgeTimeoutError,
    ClickVerificationError,
    CommandError,
    CommandTimeoutError,
    ContentUnavailableError,
    CSPBlockedError,
    ElementNotFoundError,
    EmuniumError,
    ExpectationError,
    InvalidArgumentError,
    LaunchError,
    NativeInputError,
    NavigationError,
    NavigationTimeoutError,
    ScriptError,
    StaleElementError,
    TargetClosedError,
    WaitTimeoutError,
)
from emunium.locator import Locator, PageParser
from emunium.results import NetworkResponse, PageInfo, Point, Record, TabInfo
from emunium.standalone import Emunium
from emunium.wait import Wait, WaitStrategy

__all__ = [
    "BezierTremorPlanner",
    "Bridge",
    "BridgeDisconnectedError",
    "BridgeError",
    "BridgeProtocolError",
    "BridgeTimeoutError",
    "Browser",
    "CSPBlockedError",
    "ClickType",
    "ClickVerificationError",
    "CommandError",
    "CommandTimeoutError",
    "ContentUnavailableError",
    "CoordsStore",
    "Element",
    "ElementNotFoundError",
    "ElementRecord",
    "Emunium",
    "EmuniumError",
    "ExpectationError",
    "FittsTiming",
    "InvalidArgumentError",
    "LaunchError",
    "LinearPlanner",
    "Locator",
    "MotionPlanner",
    "MotionStep",
    "NativeInputError",
    "NavigationError",
    "NavigationTimeoutError",
    "NetworkResponse",
    "Overshoot",
    "PageInfo",
    "PageParser",
    "Point",
    "Record",
    "ScreenBounds",
    "ScriptError",
    "SigmaDriftPlanner",
    "StaleElementError",
    "TabInfo",
    "TargetClosedError",
    "Tremor",
    "TypingRhythm",
    "Wait",
    "WaitStrategy",
    "WaitTimeoutError",
    "WindMousePlanner",
    "ensure_chrome",
]
