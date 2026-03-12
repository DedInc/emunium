from __future__ import annotations

from typing import Callable

from emunium._bridge.commands import (
    DomCommands,
    NetworkCommands,
    PageCommands,
    TabCommands,
)
from emunium._bridge.transport import Transport


class Bridge:
    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._transport = Transport(host=host, port=port)
        self._dom = DomCommands(self._transport)
        self._page = PageCommands(self._transport)
        self._tabs = TabCommands(self._transport)
        self._network = NetworkCommands(self._transport)

    @property
    def actual_port(self) -> int | None:
        return self._transport.actual_port

    @property
    def pinned_tab_id(self) -> int | None:
        return self._transport._pinned_tab_id

    @pinned_tab_id.setter
    def pinned_tab_id(self, value: int | None) -> None:
        self._transport._pinned_tab_id = value

    def start(self, timeout: float = 30.0) -> None:
        self._transport.start(timeout=timeout)

    def on(self, event: str, handler: Callable) -> None:
        self._transport.on(event, handler)

    def wait_for_connection(self, timeout: float = 60.0) -> bool:
        return self._transport.wait_for_connection(timeout=timeout)

    def send(
        self,
        method: str,
        params: dict[str, object] | None = None,
        timeout: float = 30.0,
        tab_id: int | None = None,
    ) -> object:
        return self._transport.send(method, params, timeout=timeout, tab_id=tab_id)

    def shutdown(self) -> None:
        self._transport.shutdown()

    def query_selector(self, selector: str, timeout: float = 10.0) -> dict | None:
        return self._dom.query_selector(selector, timeout)

    def query_selector_all(self, selector: str, timeout: float = 10.0) -> list[dict]:
        return self._dom.query_selector_all(selector, timeout)

    def get_all_interactive(self, timeout: float = 10.0) -> list[dict]:
        return self._dom.get_all_interactive(timeout)

    def get_element_by_text(
        self, text: str, exact: bool = False, timeout: float = 10.0
    ) -> list[dict]:
        return self._dom.get_element_by_text(text, exact, timeout)

    def query_xpath(self, xpath: str, timeout: float = 10.0) -> list[dict]:
        return self._dom.query_xpath(xpath, timeout)

    def get_element_coords(self, element_id: str, timeout: float = 10.0) -> dict | None:
        return self._dom.get_element_coords(element_id, timeout)

    def scroll_into_view(self, element_id: str, timeout: float = 10.0) -> dict:
        return self._dom.scroll_into_view(element_id, timeout)

    def wait_for_selector(
        self,
        selector: str,
        type: str = "css",
        timeout: float = 10.0,
        state: str | None = None,
        conditions: list[dict] | None = None,
    ) -> dict | None:
        return self._dom.wait_for_selector(
            selector, type, timeout, state=state, conditions=conditions
        )

    def focus(self, element_id: str, timeout: float = 10.0) -> dict:
        return self._dom.focus(element_id, timeout)

    def get_attribute(
        self, element_id: str, name: str, timeout: float = 10.0
    ) -> str | None:
        return self._dom.get_attribute(element_id, name, timeout)

    def get_computed_style(
        self, element_id: str, prop: str, timeout: float = 10.0
    ) -> str | None:
        return self._dom.get_computed_style(element_id, prop, timeout)

    def navigate(self, url: str, timeout: float = 30.0) -> dict:
        return self._page.navigate(url, timeout)

    def page_info(self, timeout: float = 10.0) -> dict:
        return self._page.page_info(timeout)

    def scroll_to(self, x: int, y: int, timeout: float = 10.0) -> dict:
        return self._page.scroll_to(x, y, timeout)

    def execute_script(self, code: str, timeout: float = 10.0) -> dict:
        return self._page.execute_script(code, timeout)

    def ping(self, timeout: float = 5.0) -> bool:
        return self._page.ping(timeout)

    def get_tab_info(self, timeout: float = 10.0) -> dict:
        return self._tabs.get_tab_info(timeout)

    def create_tab(self, url: str = "about:blank", timeout: float = 10.0) -> dict:
        return self._tabs.create_tab(url, timeout)

    def close_tab(self, tab_id: int | None = None, timeout: float = 10.0) -> dict:
        return self._tabs.close_tab(tab_id, timeout)

    def wait_for_response(self, pattern: str, timeout: float = 10.0) -> dict | None:
        return self._network.wait_for_response(pattern, timeout)

    def get_recent_responses(self, timeout: float = 10.0) -> list[dict]:
        return self._network.get_recent_responses(timeout)
