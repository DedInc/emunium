"""Public ``Bridge`` facade over ``Transport.request``.

Contract: ``docs/architecture/python-internals.md`` (Bridge facade); wire
methods, params and result shapes: ``docs/architecture/bridge-protocol.md``
section 6. Methods return plain data and raise ``emunium.errors`` exceptions;
every element dict carries ``tabId`` and ``zoom`` from the reply envelope.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from emunium._bridge.commands import (
    WAIT_GRACE,
    element_list,
    element_or_none,
    enrich_element,
    expect_dict,
    expect_list,
    is_idempotent,
    to_ms,
)
from emunium._bridge.transport import PINNED, Reply, Transport
from emunium.errors import BridgeProtocolError

__all__ = ["FULL_TEXT_CAP", "Bridge"]

# content_shared.js FULL_TEXT_CAP: ``elementText`` cuts longer texts at this length.
FULL_TEXT_CAP = 1048576


class Bridge:
    """Low-level command API over the extension's WebSocket bridge.

    Methods return plain data. Every call can raise ``BridgeTimeoutError``
    (no reply in time), ``BridgeDisconnectedError`` (no extension connected)
    or ``BridgeProtocolError`` (malformed reply), plus the ``CommandError``
    subclass mapped from the extension's error code. ``tab_id`` defaults to
    the pinned tab; read commands are re-sent once after a reconnect.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        token: Optional[str] = None,
        transport: Any = None,
    ) -> None:
        # An injected transport (e.g. an attach-mode hub session) owns its own
        # address and token; host/port/token are then ignored.
        if transport is None:
            transport = Transport(host=host, port=port, token=token)
        self._transport = transport

    # -- transport passthrough -------------------------------------------

    @property
    def transport(self) -> Any:
        """The underlying transport (own server, or an injected one)."""
        return self._transport

    @property
    def token(self) -> Optional[str]:
        """The session token the extension must present; None if the transport has none."""
        return getattr(self._transport, "token", None)

    @property
    def actual_port(self) -> Optional[int]:
        """The port the bridge listens on; None before ``start()``."""
        return getattr(self._transport, "actual_port", None)

    @property
    def pinned_tab_id(self) -> Optional[int]:
        """The tab commands go to by default; None sends them to the active tab."""
        return self._transport.pinned_tab_id

    @pinned_tab_id.setter
    def pinned_tab_id(self, value: Optional[int]) -> None:
        self._transport.pinned_tab_id = value

    @property
    def connected(self) -> bool:
        """True while an extension connection is open."""
        return bool(self._transport.connected)

    def start(self, timeout: float = 30.0) -> None:
        """Start listening; returns once the server is up (no-op when started).

        Raises:
            BridgeError: The server did not start within ``timeout``, could
                not listen on the port, or the bridge was shut down.
        """
        self._transport.start(timeout=timeout)

    def on(self, event: str, handler: Callable[[dict], None]) -> None:
        """Register ``handler`` for extension events named ``event``."""
        self._transport.on(event, handler)

    def wait_for_connection(self, timeout: float = 60.0) -> bool:
        """Wait for the extension to connect; False on timeout or after shutdown."""
        return self._transport.wait_for_connection(timeout=timeout)

    def shutdown(self) -> None:
        """Stop the server and fail pending requests; safe to call repeatedly."""
        self._transport.shutdown()

    def request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        tab_id: Any = PINNED,
        timeout: float = 30.0,
        idempotent: bool = False,
    ) -> Reply:
        """Send ``method`` and return the whole ``Reply`` (result, tab id, zoom).

        ``idempotent=True`` allows one re-send after a reconnect; unlike
        ``send`` the protocol table is not consulted.
        """
        return self._transport.request(
            method, params, tab_id=tab_id, timeout=timeout, idempotent=idempotent
        )

    def send(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 30.0,
        tab_id: Any = PINNED,
    ) -> Any:
        """Send ``method`` and return its raw ``result``.

        Idempotency (one re-send after a reconnect) follows ``IDEMPOTENT_METHODS``.
        """
        return self._call(method, params, tab_id, timeout).result

    def _call(
        self,
        method: str,
        params: Optional[Dict[str, Any]],
        tab_id: Any,
        timeout: float,
    ) -> Reply:
        return self._transport.request(
            method,
            params if params is not None else {},
            tab_id=tab_id,
            timeout=timeout,
            idempotent=is_idempotent(method),
        )

    def _wait_call(
        self,
        method: str,
        params: Dict[str, Any],
        tab_id: Any,
        timeout: float,
    ) -> Reply:
        """A command that waits: send ``timeoutMs`` and wait ``timeout + 5 s``."""
        params["timeoutMs"] = to_ms(timeout)
        return self._call(method, params, tab_id, timeout + WAIT_GRACE)

    # -- DOM queries -----------------------------------------------------

    def query_selector(
        self, selector: str, timeout: float = 10.0, *, tab_id: Any = PINNED
    ) -> Optional[Dict[str, Any]]:
        """Return the first element matching CSS ``selector`` now, or None."""
        reply = self._call("querySelector", {"selector": selector}, tab_id, timeout)
        return element_or_none("querySelector", reply)

    def query_selector_all(
        self, selector: str, timeout: float = 10.0, *, tab_id: Any = PINNED
    ) -> List[Dict[str, Any]]:
        """Return every element matching CSS ``selector`` now."""
        reply = self._call("querySelectorAll", {"selector": selector}, tab_id, timeout)
        return element_list("querySelectorAll", reply)

    def get_all_interactive(
        self, timeout: float = 10.0, *, tab_id: Any = PINNED
    ) -> List[Dict[str, Any]]:
        """Return the rendered interactive elements (inputs, buttons, links, ...)."""
        reply = self._call("getAllInteractive", {}, tab_id, timeout)
        return element_list("getAllInteractive", reply)

    def get_element_by_text(
        self,
        text: str,
        exact: bool = False,
        timeout: float = 10.0,
        *,
        tab_id: Any = PINNED,
    ) -> List[Dict[str, Any]]:
        """Return the elements whose text contains ``text`` (case-insensitive),
        or equals it with ``exact``."""
        reply = self._call(
            "queryByText", {"text": text, "exact": bool(exact)}, tab_id, timeout
        )
        return element_list("queryByText", reply)

    def query_xpath(
        self, xpath: str, timeout: float = 10.0, *, tab_id: Any = PINNED
    ) -> List[Dict[str, Any]]:
        """Return every element ``xpath`` selects now.

        Raises:
            InvalidArgumentError: ``xpath`` is not a valid expression.
        """
        reply = self._call("queryXPath", {"xpath": xpath}, tab_id, timeout)
        return element_list("queryXPath", reply)

    def describe_element(
        self, element_id: str, timeout: float = 10.0, *, tab_id: Any = PINNED
    ) -> Dict[str, Any]:
        """Return a fresh payload for ``element_id`` (tag, attrs, rect, text, ...).

        Raises:
            StaleElementError: The element is gone from its document.
        """
        reply = self._call("describeElement", {"elementId": element_id}, tab_id, timeout)
        return enrich_element(expect_dict("describeElement", reply), reply)

    def element_text(
        self, element_id: str, timeout: float = 10.0, *, tab_id: Any = PINNED
    ) -> str:
        """Read the element's full rendered text.

        ``innerText`` (``textContent`` as fallback), cut at ``FULL_TEXT_CAP``
        (1 048 576) chars; ``Element.text`` is only the 500-char preview.
        Idempotent: re-sent once after a reconnect.

        Raises:
            StaleElementError: The element is gone from its document.
            BridgeProtocolError: The reply carries no string ``text``.
        """
        reply = self._call("elementText", {"elementId": element_id}, tab_id, timeout)
        text = expect_dict("elementText", reply).get("text")
        if not isinstance(text, str):
            raise BridgeProtocolError(
                f"elementText returned {type(text).__name__} text, expected a string",
                method="elementText",
            )
        return text

    def scroll_into_view(
        self, element_id: str, timeout: float = 10.0, *, tab_id: Any = PINNED
    ) -> Dict[str, Any]:
        """Scroll ``element_id`` into view and return its fresh payload.

        Raises:
            StaleElementError: The element is gone from its document.
        """
        reply = self._call("scrollIntoView", {"elementId": element_id}, tab_id, timeout)
        return enrich_element(expect_dict("scrollIntoView", reply), reply)

    def get_element_coords(
        self, element_id: str, timeout: float = 10.0, *, tab_id: Any = PINNED
    ) -> Dict[str, Any]:
        """Alias of ``scroll_into_view`` (v3.0 name)."""
        return self.scroll_into_view(element_id, timeout, tab_id=tab_id)

    def wait_for_selector(
        self,
        selector: str,
        type: str = "css",
        timeout: float = 10.0,
        state: Optional[str] = None,
        conditions: Optional[List[Dict[str, Any]]] = None,
        *,
        tab_id: Any = PINNED,
    ) -> Dict[str, Any]:
        """Wait in the page until ``selector`` satisfies ``state`` and ``conditions``.

        ``type`` is ``css``, ``xpath`` or ``text``.

        Returns:
            The element payload, or ``{"element": None, "matched": True,
            "count": n}`` when the match has no element (e.g. ``detached``).

        Raises:
            WaitTimeoutError: Nothing matched in time.
            InvalidArgumentError: An unknown condition.
        """
        params: Dict[str, Any] = {"selector": selector, "type": type}
        if state is not None:
            params["state"] = state
        if conditions is not None:
            params["conditions"] = conditions
        reply = self._wait_call("waitForSelector", params, tab_id, timeout)
        result = expect_dict("waitForSelector", reply)
        if "elementId" in result:
            return enrich_element(result, reply)
        if result.get("element") is None and result.get("matched") is True:
            return result
        raise BridgeProtocolError(
            "waitForSelector returned neither an element nor a match summary",
            method="waitForSelector",
        )

    # -- element state ---------------------------------------------------

    def focus(
        self, element_id: str, timeout: float = 10.0, *, tab_id: Any = PINNED
    ) -> Dict[str, Any]:
        """Focus ``element_id`` in the page (DOM ``focus()``).

        Raises:
            StaleElementError: The element is gone from its document.
        """
        reply = self._call("focus", {"elementId": element_id}, tab_id, timeout)
        return expect_dict("focus", reply)

    def focus_state(
        self, element_id: str, timeout: float = 10.0, *, tab_id: Any = PINNED
    ) -> Dict[str, Any]:
        """Report whether the document has focus and ``element_id`` is the active element.

        Raises:
            StaleElementError: The element is gone from its document.
        """
        reply = self._call("focusState", {"elementId": element_id}, tab_id, timeout)
        return expect_dict("focusState", reply)

    def pointer_probe(
        self,
        element_id: str,
        since_ms: float,
        *,
        quiet_ms: int = 40,
        wait_ms: int = 300,
        timeout: float = 10.0,
        tab_id: Any = PINNED,
    ) -> Dict[str, Any]:
        """Return the latest trusted pointer sample at or after ``since_ms`` (epoch ms)."""
        params = {
            "elementId": element_id,
            "since": since_ms,
            "quietMs": quiet_ms,
            "waitMs": wait_ms,
        }
        reply = self._call("pointerProbe", params, tab_id, timeout)
        return expect_dict("pointerProbe", reply)

    def get_attribute(
        self,
        element_id: str,
        name: str,
        timeout: float = 10.0,
        *,
        tab_id: Any = PINNED,
    ) -> Optional[str]:
        """Read attribute ``name`` of ``element_id``; None when absent.

        Raises:
            StaleElementError: The element is gone from its document.
        """
        params = {"elementId": element_id, "name": name}
        reply = self._call("getAttribute", params, tab_id, timeout)
        return expect_dict("getAttribute", reply).get("value")

    def get_computed_style(
        self,
        element_id: str,
        prop: str,
        timeout: float = 10.0,
        *,
        tab_id: Any = PINNED,
    ) -> Optional[str]:
        """Read the computed CSS property ``prop`` of ``element_id``.

        Raises:
            StaleElementError: The element is gone from its document.
        """
        params = {"elementId": element_id, "property": prop}
        reply = self._call("getComputedStyle", params, tab_id, timeout)
        return expect_dict("getComputedStyle", reply).get("value")

    # -- page ------------------------------------------------------------

    def navigate(
        self,
        url: str,
        timeout: float = 30.0,
        *,
        wait_until: str = "load",
        tab_id: Any = PINNED,
    ) -> Dict[str, Any]:
        """Navigate the tab to ``url`` and wait for ``wait_until``.

        Raises:
            NavigationTimeoutError: ``wait_until`` was not reached in time.
            NavigationError: The navigation failed.
        """
        params = {"url": url, "waitUntil": wait_until}
        reply = self._wait_call("navigate", params, tab_id, timeout)
        return expect_dict("navigate", reply)

    def page_info(self, timeout: float = 10.0, *, tab_id: Any = PINNED) -> Dict[str, Any]:
        """Return the document's URL, title, ``readyState``, scroll offsets and sizes.

        Raises:
            ContentUnavailableError: No content script answered.
        """
        return expect_dict("pageInfo", self._call("pageInfo", {}, tab_id, timeout))

    def scroll_to(
        self, x: float, y: float, timeout: float = 10.0, *, tab_id: Any = PINNED
    ) -> Dict[str, Any]:
        """Scroll the page to ``(x, y)`` CSS px."""
        reply = self._call("scrollTo", {"x": x, "y": y}, tab_id, timeout)
        return expect_dict("scrollTo", reply)

    def execute_script(
        self, code: str, timeout: float = 10.0, *, tab_id: Any = PINNED
    ) -> Any:
        """Run ``code`` and return the JSON value of its completion value (None for undefined).

        Raises:
            ScriptError: The script threw or produced no result.
            CSPBlockedError: The page's Content Security Policy blocked it.
        """
        reply = self._wait_call("executeScript", {"code": code}, tab_id, timeout)
        return expect_dict("executeScript", reply).get("value")

    def ping(self, timeout: float = 5.0, *, tab_id: Any = PINNED) -> bool:
        """Return True when the content script answered; never raises."""
        try:
            result = self._call("ping", {}, tab_id, timeout).result
        except Exception:  # noqa: BLE001 - a liveness probe reports, it does not raise
            return False
        return isinstance(result, dict) and result.get("pong") is True

    # -- tabs ------------------------------------------------------------

    def get_tab_info(self, timeout: float = 10.0, *, tab_id: Any = PINNED) -> Dict[str, Any]:
        """Describe the tab (``tab_id=None``: the active tab of the last focused window).

        Raises:
            TargetClosedError: No such tab.
        """
        return expect_dict("getTabInfo", self._call("getTabInfo", {}, tab_id, timeout))

    def list_tabs(self, timeout: float = 10.0) -> List[Dict[str, Any]]:
        """Describe every open tab."""
        return expect_list("listTabs", self._call("listTabs", {}, None, timeout))

    def create_tab(
        self,
        url: str = "about:blank",
        timeout: float = 30.0,
        *,
        active: bool = True,
        wait_until: str = "load",
        tab_id: Any = PINNED,
    ) -> Dict[str, Any]:
        """Open ``url`` in a new tab (activated when ``active``) and wait for
        ``wait_until``; the pin is not changed."""
        params = {"url": url, "active": bool(active), "waitUntil": wait_until}
        reply = self._wait_call("createTab", params, tab_id, timeout)
        return expect_dict("createTab", reply)

    def close_tab(self, tab_id: Optional[int] = None, timeout: float = 10.0) -> Dict[str, Any]:
        """Close ``tab_id`` (``None``: the pinned tab); unpins it when it was pinned."""
        target = self.pinned_tab_id if tab_id is None else tab_id
        reply = self._call("closeTab", {}, target, timeout)
        result = expect_dict("closeTab", reply)
        closed = reply.tab_id if reply.tab_id is not None else target
        if closed is not None and self.pinned_tab_id == closed:
            self.pinned_tab_id = None
        return result

    def activate_tab(self, timeout: float = 10.0, *, tab_id: Any = PINNED) -> Dict[str, Any]:
        """Activate the tab and focus its window.

        Raises:
            TargetClosedError: No such tab.
        """
        return expect_dict("activateTab", self._call("activateTab", {}, tab_id, timeout))

    # -- network ---------------------------------------------------------

    def wait_for_response(
        self,
        pattern: str,
        timeout: float = 10.0,
        *,
        since: Optional[float] = None,
        any_tab: bool = False,
        tab_id: Any = PINNED,
    ) -> Optional[Dict[str, Any]]:
        """Wait for the first network entry matching glob ``pattern``.

        Returns:
            The entry, or None when none arrived in time.
        """
        params = {"pattern": pattern, "since": since, "anyTab": bool(any_tab)}
        reply = self._wait_call("waitForResponse", params, tab_id, timeout)
        if reply.result is None:
            return None
        return expect_dict("waitForResponse", reply)

    def get_recent_responses(
        self,
        timeout: float = 10.0,
        *,
        since: Optional[float] = None,
        any_tab: bool = True,
        tab_id: Any = PINNED,
    ) -> List[Dict[str, Any]]:
        """Return the buffered network entries (last 500, up to 2 min old)
        since ``since`` (epoch ms); every tab unless ``any_tab=False``."""
        params = {"since": since, "anyTab": bool(any_tab)}
        reply = self._call("getRecentResponses", params, tab_id, timeout)
        return expect_list("getRecentResponses", reply)
