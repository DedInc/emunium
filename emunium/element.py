from __future__ import annotations

import logging
import time

from emunium._element_interactor import ElementInteractor
from emunium._standalone.config import ClickType
from emunium.bridge import Bridge

logger = logging.getLogger("emunium.element")
_INTERACTOR = ElementInteractor()


class Element:
    """Element handle backed by WebSocket bridge with physical OS-level interactions."""

    def __init__(
        self,
        bridge: Bridge,
        element_id: str,
        tag: str = "",
        attrs: dict[str, str] | None = None,
        rect: dict[str, float] | None = None,
        text: str = "",
        selector: str | None = None,
        absolute_screen_x: float = 0,
        absolute_screen_y: float = 0,
    ) -> None:
        self._bridge = bridge
        self._element_id = element_id
        self._tag = tag
        self._attrs = attrs or {}
        self._rect = rect or {}
        self._text = text
        self._selector = selector
        self._screen_x = absolute_screen_x
        self._screen_y = absolute_screen_y

    @classmethod
    def from_data(
        cls,
        bridge: Bridge,
        data: dict[str, object],
        selector: str | None = None,
    ) -> Element:
        return cls(
            bridge=bridge,
            element_id=data.get("elementId", ""),
            tag=data.get("tag", ""),
            attrs=data.get("attrs", {}),
            rect=data.get("rect", {}),
            text=data.get("text", ""),
            selector=selector,
            absolute_screen_x=data.get("absoluteScreenX", 0),
            absolute_screen_y=data.get("absoluteScreenY", 0),
        )

    @property
    def element_id(self) -> str:
        return self._element_id

    @property
    def tag(self) -> str:
        return self._tag

    @property
    def attrs(self) -> dict[str, str]:
        return self._attrs

    @property
    def rect(self) -> dict[str, float]:
        return self._rect

    @property
    def text(self) -> str:
        return self._text

    @property
    def center(self) -> tuple[float, float]:
        return (
            self._rect.get("x", 0) + self._rect.get("width", 0) / 2,
            self._rect.get("y", 0) + self._rect.get("height", 0) / 2,
        )

    @property
    def screen_x(self) -> float:
        return self._screen_x

    @property
    def screen_y(self) -> float:
        return self._screen_y

    @property
    def visible(self) -> bool:
        return self._rect.get("width", 0) > 0 and self._rect.get("height", 0) > 0

    def refresh(self) -> Element:
        if self._selector:
            data = self._bridge.query_selector(self._selector)
            if data:
                self._update_from_data(data)
        return self

    def scroll_into_view(self) -> dict:
        result = self._bridge.scroll_into_view(self._element_id)
        if "rect" in result:
            self._rect = result["rect"]
        if "absoluteScreenX" in result:
            self._screen_x = result["absoluteScreenX"]
        if "absoluteScreenY" in result:
            self._screen_y = result["absoluteScreenY"]
        return result

    def _update_from_data(self, data: dict[str, object]) -> None:
        self._element_id = str(data.get("elementId", self._element_id))
        self._tag = str(data.get("tag", self._tag))
        attrs = data.get("attrs", self._attrs)
        rect = data.get("rect", self._rect)
        self._attrs = attrs if isinstance(attrs, dict) else self._attrs
        self._rect = rect if isinstance(rect, dict) else self._rect
        self._text = str(data.get("text", self._text))
        self._screen_x = float(data.get("absoluteScreenX", self._screen_x))
        self._screen_y = float(data.get("absoluteScreenY", self._screen_y))

    def _current_screen_point(self) -> tuple[int, int]:
        return int(self._screen_x), int(self._screen_y)

    def _click(
        self,
        click_type: ClickType = ClickType.LEFT,
        *,
        human: bool = True,
    ) -> None:
        self.scroll_into_view()
        x, y = int(self._screen_x), int(self._screen_y)
        logger.info(
            "Clicking element at Screen(%d, %d) type=%s human=%s",
            x,
            y,
            click_type.name,
            human,
        )
        _INTERACTOR.click(x, y, click_type=click_type, human=human)

    def hover(
        self,
        offset_x: float | None = None,
        offset_y: float | None = None,
        *,
        human: bool = True,
    ) -> None:
        self.scroll_into_view()
        x, y = _INTERACTOR.screen_point(
            self._screen_x,
            self._screen_y,
            offset_x=offset_x,
            offset_y=offset_y,
        )
        logger.info("Hovering element at Screen(%d, %d) human=%s", x, y, human)
        _INTERACTOR.move_cursor(x, y, human=human)

    def move_to(
        self,
        offset_x: float | None = None,
        offset_y: float | None = None,
        *,
        human: bool = True,
    ) -> None:
        self.hover(offset_x=offset_x, offset_y=offset_y, human=human)

    def click(self, *, human: bool = True) -> None:
        self._click(human=human)

    def double_click(self, *, human: bool = True) -> None:
        self._click(ClickType.DOUBLE, human=human)

    def right_click(self, *, human: bool = True) -> None:
        self._click(ClickType.RIGHT, human=human)

    def middle_click(self, *, human: bool = True) -> None:
        self._click(ClickType.MIDDLE, human=human)

    def type(
        self,
        text: str,
        *,
        characters_per_minute: int = 280,
        offset: int = 20,
        human: bool = True,
        click_type: ClickType = ClickType.LEFT,
    ) -> None:
        self._click(click_type, human=human)
        time.sleep(0.1)
        logger.info(
            "Typing %d chars into element at Screen(%d, %d)",
            len(text),
            int(self._screen_x),
            int(self._screen_y),
        )
        _INTERACTOR.type_text(
            text,
            characters_per_minute=characters_per_minute,
            offset=offset,
        )

    def drag_to(self, target: Element, *, human: bool = True) -> None:
        self.scroll_into_view()
        target.scroll_into_view()
        start = self._current_screen_point()
        end = target._current_screen_point()
        logger.info("Dragging from Screen%s to Screen%s", start, end)
        _INTERACTOR.drag(start, end, human=human)

    def focus(self) -> dict:
        return self._bridge.focus(self._element_id)

    def get_attribute(self, name: str) -> str | None:
        return self._bridge.get_attribute(self._element_id, name)

    def get_computed_style(self, prop: str) -> str | None:
        return self._bridge.get_computed_style(self._element_id, prop)

    def __repr__(self) -> str:
        return (
            f"Element(id={self._element_id!r}, "
            f"screen=({self._screen_x:.0f}, {self._screen_y:.0f}))"
        )
