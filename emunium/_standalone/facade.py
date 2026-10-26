"""Standalone (screen) automation: template matching, OCR and native input.

Coordinates returned by ``find_*`` and accepted by the input methods are INPUT
units (what ``pyautogui.moveTo`` uses); screenshot pixels are converted with
``emunium._screen.screenshot_scale``. pyautogui and HumanCursor are imported
lazily so DPI awareness is chosen first (see ``docs/architecture/native-input.md``).
"""

from __future__ import annotations

import random
import sys
import time
from typing import TYPE_CHECKING, Sequence, Tuple

from emunium import _screen
from emunium._element_interactor import ElementInteractor
from emunium._screen import NATIVE_INPUT_LOCK
from emunium._standalone import ocr as _ocr
from emunium._standalone import vision
from emunium._standalone.config import ClickType, Config, StandaloneConfig

if TYPE_CHECKING:
    from humancursor import SystemCursor

Region = Tuple[int, int, int, int]


def _platform() -> str:
    return sys.platform


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def wheel_units_per_notch(platform: str) -> int:
    """Amount ``pyautogui.scroll`` needs for one wheel notch on ``platform``."""
    return Config.WINDOWS_WHEEL_DELTA if platform == "win32" else 1


class Emunium:
    def __init__(
        self,
        ocr_enabled: bool = False,
        use_gpu: bool = True,
        langs: list[str] | None = None,
        *,
        ocr: bool | None = None,
    ) -> None:
        if ocr is not None and ocr_enabled and not ocr:
            raise ValueError("conflicting arguments: ocr_enabled=True and ocr=False")
        enabled = bool(ocr_enabled or ocr)
        self.ocr = enabled
        self.ocr_reader: _ocr.OcrReader | None = None
        self.monitor_region: tuple[int, int, int, int] | None = None
        self.interactor = ElementInteractor()
        self._cursor: SystemCursor | None = None

        if enabled:
            # Fails here, not at the first find_text_elements call.
            self.ocr_reader = _ocr.initialize_ocr(
                list(langs) if langs else ["en"], use_gpu
            )
            screen_width, screen_height = _screen.pyautogui().size()
            self.monitor_region = (0, 0, int(screen_width), int(screen_height))

    @property
    def cursor(self) -> SystemCursor:
        """HumanCursor ``SystemCursor``, created on first access.

        Input methods go through ``self.interactor``; this stays for callers
        that drive HumanCursor directly.
        """
        if self._cursor is None:
            # HumanCursor imports pyautogui; DPI awareness must be chosen first.
            _screen.pyautogui()
            from humancursor import SystemCursor

            self._cursor = SystemCursor()
        return self._cursor

    def find_elements(
        self,
        image_path: str,
        min_confidence: float = StandaloneConfig.DEFAULT_MIN_CONFIDENCE,
        target_height: int | None = None,
        target_width: int | None = None,
        max_elements: int = StandaloneConfig.DEFAULT_MAX_ELEMENTS,
        region: Region | None = None,
    ) -> list[dict[str, int]]:
        """Template match centers in INPUT units, best score first.

        ``region`` is ``(left, top, width, height)`` in INPUT units.
        """
        return vision.find_elements(
            image_path,
            min_confidence,
            target_height,
            target_width,
            max_elements,
            region,
        )

    def find_text_elements(
        self,
        query: str,
        min_confidence: float = StandaloneConfig.DEFAULT_MIN_CONFIDENCE,
        max_elements: int = StandaloneConfig.DEFAULT_MAX_ELEMENTS,
        region: Region | None = None,
    ) -> list[dict[str, int]]:
        """OCR text match centers in INPUT units; ``region`` as in ``find_elements``."""
        if not self.ocr or self.ocr_reader is None:
            raise RuntimeError(
                "OCR disabled. Initialize with Emunium(ocr_enabled=True) (alias: ocr=True)."
            )
        return _ocr.find_text_elements(
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
        self.interactor.move(element_center["x"] + ox, element_center["y"] + oy)

    def click_at(
        self,
        element_center: dict[str, int],
        click_type: ClickType = ClickType.LEFT,
    ) -> None:
        """One move, then the button press(es); a double click is never two moves."""
        self.interactor.click(
            element_center["x"], element_center["y"], click_type=click_type
        )

    def type_at(
        self,
        element_center: dict[str, int],
        text: str,
        characters_per_minute: int = 280,
        offset: int = 20,
        click_type: ClickType = ClickType.LEFT,
    ) -> None:
        # One lock span: no other native input lands between the click and the text.
        with NATIVE_INPUT_LOCK:
            self.click_at(element_center, click_type)
            self.type_text(text, characters_per_minute, offset)

    def type_text(
        self, text: str, characters_per_minute: int = 280, offset: int = 20
    ) -> None:
        """Layout-independent typing through ``ElementInteractor``."""
        self.interactor.type_text(
            text, characters_per_minute=characters_per_minute, offset=offset
        )

    def scroll_to(self, element_center: dict[str, int]) -> None:
        """Scroll the wheel until ``element_center`` is near the vertical middle.

        The pixel distance becomes wheel notches of ``Config.SCROLL_STEP_SIZE`` px.
        """
        gui = _screen.pyautogui()
        distance = element_center["y"] - int(gui.size()[1]) // 2
        notches = round(abs(distance) / float(Config.SCROLL_STEP_SIZE))
        if notches == 0:
            return
        direction = -1 if distance > 0 else 1
        amount = direction * wheel_units_per_notch(_platform())
        with NATIVE_INPUT_LOCK:
            for _ in range(notches):
                gui.scroll(amount, _pause=False)
                _sleep(random.uniform(*Config.SCROLL_DELAY_RANGE))

    def drag_and_drop(
        self, start_coords: Sequence[float], end_coords: Sequence[float]
    ) -> None:
        self.interactor.drag(start_coords, end_coords)
