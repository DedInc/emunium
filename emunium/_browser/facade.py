from __future__ import annotations

import logging

from emunium._browser import dom, page, tabs
from emunium._browser.launcher import BrowserSession, close, launch
from emunium._standalone.config import ClickType
from emunium.bridge import Bridge
from emunium.element import Element
from emunium.wait import Wait, WaitStrategy

logger = logging.getLogger("emunium.browser")


class Browser:
    def __init__(
        self,
        headless: bool = False,
        user_data_dir: str | None = None,
        bridge_port: int = 0,
        bridge_timeout: float = 60.0,
    ) -> None:
        self._session = BrowserSession()
        self._session.bridge = Bridge(port=bridge_port)
        self._session.headless = headless
        self._session.user_data_dir = user_data_dir
        self._bridge_timeout = bridge_timeout

    @property
    def bridge(self) -> Bridge:
        return self._session.bridge

    def launch(self) -> Browser:
        launch(self._session, bridge_timeout=self._bridge_timeout)
        return self

    def close(self) -> None:
        close(self._session)

    def goto(self, url: str, *, timeout: float = 30.0) -> dict:
        return page.goto(self._session.bridge, url, timeout=timeout)

    def query_selector(self, selector: str) -> Element | None:
        return dom.query_selector(self._session.bridge, selector)

    def query_selector_all(self, selector: str) -> list[Element]:
        return dom.query_selector_all(self._session.bridge, selector)

    def wait_for_element(self, selector: str, timeout: float = 10.0) -> Element:
        return dom.wait_for_element(self._session.bridge, selector, timeout)

    def wait_for_xpath(self, xpath: str, timeout: float = 10.0) -> Element:
        return dom.wait_for_xpath(self._session.bridge, xpath, timeout)

    def wait_for_text(self, text: str, timeout: float = 10.0) -> Element:
        return dom.wait_for_text(self._session.bridge, text, timeout)

    def wait(
        self,
        selector: str,
        strategy: WaitStrategy | None = None,
        condition: Wait | list[dict] | None = None,
        timeout: float = 10.0,
        raise_on_timeout: bool = True,
    ) -> Element | None:
        state = strategy.value if isinstance(strategy, WaitStrategy) else strategy
        if isinstance(condition, Wait):
            conditions = condition.to_payload()
        elif isinstance(condition, list):
            conditions = condition
        else:
            conditions = None
        return dom.wait_for_element(
            self._session.bridge,
            selector,
            timeout,
            state=state,
            conditions=conditions,
            raise_on_timeout=raise_on_timeout,
        )

    def get_by_text(self, text: str, *, exact: bool = False) -> list[Element]:
        return dom.get_by_text(self._session.bridge, text, exact=exact)

    def get_all_interactive(self) -> list[Element]:
        return dom.get_all_interactive(self._session.bridge)

    def _resolve_element(
        self, target: str | Element, *, timeout: float = 10.0
    ) -> Element:
        if isinstance(target, Element):
            return target
        return dom.wait_for_element(self._session.bridge, target, timeout)

    def click(
        self,
        selector: str,
        *,
        human: bool = True,
        click_type: ClickType = ClickType.LEFT,
        timeout: float = 10.0,
    ) -> Element:
        return self.click_at(selector, click_type, human=human, timeout=timeout)

    def get_center(
        self, target: str | Element, *, timeout: float = 10.0
    ) -> dict[str, int]:
        el = self._resolve_element(target, timeout=timeout)
        el.scroll_into_view()
        return {"x": int(el.screen_x), "y": int(el.screen_y)}

    def move_to(
        self,
        target: str | Element,
        offset_x: float | None = None,
        offset_y: float | None = None,
        *,
        human: bool = True,
        timeout: float = 10.0,
    ) -> Element:
        el = self._resolve_element(target, timeout=timeout)
        el.move_to(offset_x=offset_x, offset_y=offset_y, human=human)
        return el

    def hover(
        self,
        target: str | Element,
        offset_x: float | None = None,
        offset_y: float | None = None,
        *,
        human: bool = True,
        timeout: float = 10.0,
    ) -> Element:
        return self.move_to(
            target,
            offset_x=offset_x,
            offset_y=offset_y,
            human=human,
            timeout=timeout,
        )

    def click_at(
        self,
        target: str | Element,
        click_type: ClickType = ClickType.LEFT,
        *,
        human: bool = True,
        timeout: float = 10.0,
    ) -> Element:
        el = self._resolve_element(target, timeout=timeout)
        if click_type == ClickType.LEFT:
            el.click(human=human)
        elif click_type == ClickType.DOUBLE:
            el.double_click(human=human)
        elif click_type == ClickType.RIGHT:
            el.right_click(human=human)
        else:
            el.middle_click(human=human)
        return el

    def type(
        self,
        selector: str,
        text: str,
        *,
        characters_per_minute: int = 280,
        offset: int = 20,
        human: bool = True,
    ) -> Element:
        el = self._resolve_element(selector)
        el.type(
            text,
            characters_per_minute=characters_per_minute,
            offset=offset,
            human=human,
        )
        return el

    def type_at(
        self,
        target: str | Element,
        text: str,
        *,
        characters_per_minute: int = 280,
        offset: int = 20,
        human: bool = True,
        click_type: ClickType = ClickType.LEFT,
        timeout: float = 10.0,
    ) -> Element:
        el = self._resolve_element(target, timeout=timeout)
        el.type(
            text,
            characters_per_minute=characters_per_minute,
            offset=offset,
            human=human,
            click_type=click_type,
        )
        return el

    def drag_and_drop(
        self, source_selector: str, target_selector: str, *, human: bool = True
    ) -> None:
        source = self._resolve_element(source_selector)
        target = self._resolve_element(target_selector)
        source.drag_to(target, human=human)

    def execute_script(self, code: str) -> str | None:
        return page.execute_script(self._session.bridge, code)

    def page_info(self) -> dict:
        return page.page_info(self._session.bridge)

    def scroll_to(self, target: int | str | Element, y: int | None = None) -> dict:
        if isinstance(target, (str, Element)):
            return self._resolve_element(target).scroll_into_view()
        if y is None:
            raise TypeError("scroll_to() missing y coordinate")
        return page.scroll_to(self._session.bridge, target, y)

    @property
    def url(self) -> str:
        return page.get_url(self._session.bridge)

    @property
    def title(self) -> str:
        return page.get_title(self._session.bridge)

    def new_tab(self, url: str = "about:blank") -> dict:
        return tabs.new_tab(self._session.bridge, url)

    def close_tab(self, tab_id: int | None = None) -> dict:
        return tabs.close_tab(self._session.bridge, tab_id)

    def tab_info(self) -> dict:
        return tabs.tab_info(self._session.bridge)

    def wait_for_idle(self, silence: float = 2.0, timeout: float = 30.0) -> bool:
        return page.wait_for_idle(self._session.bridge, silence, timeout)

    def wait_for_response(self, url_pattern: str, timeout: float = 10.0) -> dict | None:
        """Wait for a network response matching *url_pattern* (glob)."""
        return self._session.bridge.wait_for_response(url_pattern, timeout)

    def __enter__(self) -> Browser:
        return self.launch()

    def __exit__(self, *exc: object) -> None:
        self.close()
