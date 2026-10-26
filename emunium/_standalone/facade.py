"""Standalone (screen) automation: template matching, OCR and native input.

Coordinates returned by ``find_*`` and accepted by the input methods are INPUT
units (what ``emunium._screen.input_backend().moveTo`` uses); screenshots come
from mss and are converted in ``emunium._standalone.vision``. All input goes
through the shared ``ElementInteractor`` / ``input_backend()`` (see
``docs/architecture/native-input.md``).
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable, Sequence

from emunium import _screen
from emunium._element_interactor import ElementInteractor
from emunium._motion import MotionPlanner, TypingRhythm
from emunium._screen import NATIVE_INPUT_LOCK
from emunium._standalone import ocr as _ocr
from emunium._standalone import vision
from emunium._standalone.config import ClickType, Config, StandaloneConfig

Region = tuple[int, int, int, int]

# ``input_backend().scroll`` takes wheel NOTCHES on every platform (the Windows
# backend multiplies by WHEEL_DELTA itself; pyautogui on macOS scrolls lines).
WHEEL_NOTCH = 1


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _xy(point: Sequence[float]) -> tuple[float, float]:
    if len(point) != 2:
        raise ValueError(f"point must be (x, y), got {point!r}")
    return float(point[0]), float(point[1])


class SystemCursor:
    """Compatibility stand-in for the HumanCursor ``SystemCursor`` of emunium 3.0.

    ``move_to``, ``click_on`` and ``drag_and_drop`` delegate to the owner's
    current ``ElementInteractor`` (human paths, native input, input lock).
    """

    def __init__(self, interactor: Callable[[], ElementInteractor]) -> None:
        self._interactor = interactor

    def move_to(self, point: Sequence[float]) -> None:
        x, y = _xy(point)
        self._interactor().move(x, y)

    def click_on(
        self, point: Sequence[float], clicks: int = 1, click_duration: float = 0
    ) -> None:
        """Move to ``point`` and click ``clicks`` times.

        ``click_duration`` 0 lets the interactor pick the hold (``clicks`` > 1 is
        a real multi-click); a positive value holds each press that long.
        """
        x, y = _xy(point)
        if isinstance(clicks, bool) or not isinstance(clicks, int) or clicks < 1:
            raise ValueError(f"clicks must be a positive int, got {clicks!r}")
        hold = float(click_duration)
        if not math.isfinite(hold) or hold < 0:
            raise ValueError(
                f"click_duration must be finite and >= 0, got {click_duration!r}"
            )
        interactor = self._interactor()
        with NATIVE_INPUT_LOCK:
            interactor.move(x, y)
            if hold == 0:
                interactor.press("left", clicks=clicks)
                return
            gui = interactor._gui()
            for index in range(clicks):
                # The interactor's guarded press: the button is released even
                # when the hold is interrupted.
                interactor._button_down_up(gui, "left", hold)
                if index < clicks - 1:
                    _sleep(interactor.double_click_interval())

    def drag_and_drop(self, start: Sequence[float], end: Sequence[float]) -> None:
        self._interactor().drag(_xy(start), _xy(end))


class Emunium:
    def __init__(
        self,
        ocr_enabled: bool = False,
        use_gpu: bool = True,
        langs: list[str] | None = None,
        *,
        ocr: bool | None = None,
        motion: MotionPlanner | str | None = None,
        typing: TypingRhythm | str | None = None,
    ) -> None:
        """``motion``: human pointer planner, a preset name (``natural``,
        ``careful``, ``brisk``), a ``MotionPlanner`` or None (``natural``);
        resolved like ``Browser(motion=)``. ``typing``: typing rhythm, a preset
        name, a ``TypingRhythm`` or None (``natural``), as ``Browser(typing=)``.
        """
        if ocr is not None and ocr_enabled and not ocr:
            raise ValueError("conflicting arguments: ocr_enabled=True and ocr=False")
        enabled = bool(ocr_enabled or ocr)
        self.ocr = enabled
        self.ocr_reader: _ocr.OcrReader | None = None
        self.monitor_region: tuple[int, int, int, int] | None = None
        self.interactor = ElementInteractor(motion=motion, typing=typing)
        self._cursor: SystemCursor | None = None

        if enabled:
            # Fails here, not at the first find_text_elements call.
            self.ocr_reader = _ocr.initialize_ocr(
                list(langs) if langs else ["en"], use_gpu
            )
            screen_width, screen_height = _screen.input_backend().size()
            self.monitor_region = (0, 0, int(screen_width), int(screen_height))

    @property
    def cursor(self) -> SystemCursor:
        """``SystemCursor``-compatible object (3.0 API), created on first access.

        It delegates to ``self.interactor``, including one assigned later.
        """
        if self._cursor is None:
            self._cursor = SystemCursor(lambda: self.interactor)
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
        characters_per_minute: int | None = None,
        offset: int | None = None,
        click_type: ClickType = ClickType.LEFT,
    ) -> None:
        # One lock span: no other native input lands between the click and the text.
        with NATIVE_INPUT_LOCK:
            self.click_at(element_center, click_type)
            self.type_text(text, characters_per_minute, offset)

    def type_text(
        self,
        text: str,
        characters_per_minute: int | None = None,
        offset: int | None = None,
    ) -> None:
        """Layout-independent typing through ``ElementInteractor``.

        ``characters_per_minute`` and ``offset`` (ms of jitter) override the
        ``typing`` rhythm; None keeps the rhythm's own values.
        """
        self.interactor.type_text(
            text, characters_per_minute=characters_per_minute, offset=offset
        )

    def scroll_to(self, element_center: dict[str, int]) -> None:
        """Scroll the wheel until ``element_center`` is near the vertical middle.

        The pixel distance becomes wheel notches of ``Config.SCROLL_STEP_SIZE`` px,
        sent one ``WHEEL_NOTCH`` at a time.
        """
        backend = _screen.input_backend()
        distance = element_center["y"] - int(backend.size()[1]) // 2
        notches = round(abs(distance) / float(Config.SCROLL_STEP_SIZE))
        if notches == 0:
            return
        direction = -1 if distance > 0 else 1
        with NATIVE_INPUT_LOCK:
            for _ in range(notches):
                backend.scroll(direction * WHEEL_NOTCH, _pause=False)
                _sleep(random.uniform(*Config.SCROLL_DELAY_RANGE))

    def drag_and_drop(
        self, start_coords: Sequence[float], end_coords: Sequence[float]
    ) -> None:
        self.interactor.drag(start_coords, end_coords)
