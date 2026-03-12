from __future__ import annotations

from emunium._bridge.transport import Transport


class DomCommands:
    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def query_selector(self, selector: str, timeout: float = 10.0) -> dict | None:
        return self._t._send_optional(
            "querySelector", {"selector": selector}, timeout=timeout
        )

    def query_selector_all(self, selector: str, timeout: float = 10.0) -> list[dict]:
        return self._t._send_list(
            "querySelectorAll", {"selector": selector}, timeout=timeout
        )

    def get_all_interactive(self, timeout: float = 10.0) -> list[dict]:
        return self._t._send_list("getAllInteractive", timeout=timeout)

    def get_element_by_text(
        self, text: str, exact: bool = False, timeout: float = 10.0
    ) -> list[dict]:
        return self._t._send_list(
            "queryByText", {"text": text, "exact": exact}, timeout=timeout
        )

    def query_xpath(self, xpath: str, timeout: float = 10.0) -> list[dict]:
        return self._t._send_list("queryXPath", {"xpath": xpath}, timeout=timeout)

    def get_element_coords(self, element_id: str, timeout: float = 10.0) -> dict | None:
        return self._t._send_optional(
            "getElementCoords", {"elementId": element_id}, timeout=timeout
        )

    def scroll_into_view(self, element_id: str, timeout: float = 10.0) -> dict:
        return self._t._send_with_retry(
            "scrollIntoView", {"elementId": element_id}, timeout=timeout
        )

    def wait_for_selector(
        self,
        selector: str,
        type: str = "css",
        timeout: float = 10.0,
        state: str | None = None,
        conditions: list[dict] | None = None,
    ) -> dict | None:
        params = {"selector": selector, "type": type, "timeout": int(timeout * 1000)}
        if state is not None:
            params["state"] = state
        if conditions is not None:
            params["conditions"] = conditions
        return self._t._send_optional(
            "waitForSelector",
            params,
            timeout=timeout + 5,
        )

    def focus(self, element_id: str, timeout: float = 10.0) -> dict:
        return self._t._send_with_retry(
            "focus", {"elementId": element_id}, timeout=timeout
        )

    def get_attribute(
        self, element_id: str, name: str, timeout: float = 10.0
    ) -> str | None:
        result = self._t._send_with_retry(
            "getAttribute", {"elementId": element_id, "name": name}, timeout=timeout
        )
        return result.get("value") if result else None

    def get_computed_style(
        self, element_id: str, prop: str, timeout: float = 10.0
    ) -> str | None:
        result = self._t._send_with_retry(
            "getComputedStyle",
            {"elementId": element_id, "property": prop},
            timeout=timeout,
        )
        return result.get("value") if result else None


class PageCommands:
    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def navigate(self, url: str, timeout: float = 30.0) -> dict:
        return self._t.send(
            "navigate",
            {"url": url, "timeout": int(timeout * 1000)},
            timeout=timeout + 5,
        )

    def page_info(self, timeout: float = 10.0) -> dict:
        return self._t._send_with_retry("pageInfo", timeout=timeout)

    def scroll_to(self, x: int, y: int, timeout: float = 10.0) -> dict:
        return self._t._send_with_retry("scrollTo", {"x": x, "y": y}, timeout=timeout)

    def execute_script(self, code: str, timeout: float = 10.0) -> dict:
        return self._t.send("executeScript", {"code": code}, timeout=timeout)

    def ping(self, timeout: float = 5.0) -> bool:
        try:
            result = self._t._send_with_retry("ping", timeout=timeout)
            return result.get("pong", False) if result else False
        except Exception:
            return False


class TabCommands:
    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def get_tab_info(self, timeout: float = 10.0) -> dict:
        return self._t.send("getTabInfo", timeout=timeout)

    def create_tab(self, url: str = "about:blank", timeout: float = 10.0) -> dict:
        return self._t.send("createTab", {"url": url}, timeout=timeout)

    def close_tab(self, tab_id: int | None = None, timeout: float = 10.0) -> dict:
        params = {"tabId": tab_id} if tab_id else {}
        return self._t.send("closeTab", params, timeout=timeout)


class NetworkCommands:
    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def wait_for_response(self, pattern: str, timeout: float = 10.0) -> dict | None:
        params = {"pattern": pattern, "timeout": int(timeout * 1000)}
        return self._t._send_optional("waitForResponse", params, timeout=timeout + 5)

    def get_recent_responses(self, timeout: float = 10.0) -> list[dict]:
        result = self._t._send_with_retry("getRecentResponses", timeout=timeout)
        return result.get("responses", []) if result else []
