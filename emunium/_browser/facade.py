"""The ``Browser`` facade: one Chrome session, one ``BrowserContext``.

Every DOM lookup and element handle shares the browser's context, so the
``headless`` refusal and the ``verify_input`` switch reach each native-input
transaction. Commands go to the pinned tab, which follows launch, ``goto``,
``new_tab``, ``switch_to_tab`` and ``close_tab``. Action methods record the
action start (``BrowserContext.mark_action``) that network waits default to.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from emunium._browser import dom, page
from emunium._browser import tabs as tab_ops
from emunium._browser.context import BrowserContext
from emunium._browser.launcher import BrowserSession, close, launch
from emunium._browser.page import ResponseExpectation
from emunium._motion import MotionPlanner, TypingRhythm
from emunium._standalone.config import ClickType
from emunium.bridge import Bridge
from emunium.element import Element
from emunium.errors import ElementNotFoundError, InvalidArgumentError, NativeInputError
from emunium.results import NetworkResponse, PageInfo, Point, Record, TabInfo
from emunium.wait import Wait, WaitStrategy

if TYPE_CHECKING:
    from typing_extensions import Self

logger = logging.getLogger("emunium.browser")

_CLICK_TYPES = (ClickType.LEFT, ClickType.DOUBLE, ClickType.RIGHT, ClickType.MIDDLE)


class Browser:
    def __init__(
        self,
        headless: bool = False,
        user_data_dir: str | None = None,
        bridge_port: int = 0,
        bridge_timeout: float = 60.0,
        *,
        chrome_path: str | None = None,
        extra_args: list[str] | None = None,
        verify_input: bool = True,
        motion: MotionPlanner | str | None = None,
        typing: TypingRhythm | str | None = None,
    ) -> None:
        """``motion``: human pointer planner, a preset name (``natural``,
        ``careful``, ``brisk``), a ``MotionPlanner`` or None (``natural``
        ``SigmaDriftPlanner``); ``typing``: human typing rhythm, a preset name,
        a ``TypingRhythm`` or None (``natural``); ``human=False`` actions stay
        instant.
        """
        bridge = Bridge(port=bridge_port)
        self._session = BrowserSession()
        self._session.bridge = bridge
        self._session.headless = bool(headless)
        self._session.user_data_dir = user_data_dir
        self._session.chrome_path = chrome_path
        # Validated by launch(); a str is reported there rather than split here.
        self._session.extra_args = [] if extra_args is None else extra_args
        self._bridge_timeout = bridge_timeout
        self._ctx = BrowserContext(
            bridge,
            headless=bool(headless),
            verify_input=verify_input,
            motion=motion,
            typing=typing,
        )

    @property
    def bridge(self) -> Bridge:
        return self._ctx.bridge

    @property
    def context(self) -> BrowserContext:
        return self._ctx

    def launch(self) -> Self:
        """Start Chrome and pin its initial tab; on failure nothing stays running.

        Then the window is brought to the foreground once (best effort: a
        browser started from a background process opens behind other windows).
        """
        launch(self._session, bridge_timeout=self._bridge_timeout)
        try:
            tab_ops.pin_active(self._ctx)
        except BaseException:
            try:
                close(self._session)
            except Exception:
                logger.exception("Cleanup after a failed launch raised")
            raise
        process = self._session.process
        self._ctx.pid = None if process is None else process.pid
        try:
            self._ctx.ensure_foreground(None)
        except NativeInputError as exc:
            # Every native action raises the window again before its input.
            logger.info("Browser window not brought to the foreground: %s", exc)
        return self

    def close(self) -> None:
        self._ctx.pid = None
        close(self._session)

    # -- navigation and lookups ---------------------------------------------

    def goto(
        self, url: str, *, timeout: float = 30.0, wait_until: str = "load"
    ) -> PageInfo:
        return page.goto(self._ctx, url, timeout=timeout, wait_until=wait_until)

    def query_selector(self, selector: str) -> Element | None:
        return dom.query_selector(self._ctx, selector)

    def query_selector_all(self, selector: str) -> list[Element]:
        return dom.query_selector_all(self._ctx, selector)

    def wait_for_element(self, selector: str, timeout: float = 10.0) -> Element | None:
        return dom.wait_for_element(self._ctx, selector, timeout)

    def wait_for_xpath(self, xpath: str, timeout: float = 10.0) -> Element | None:
        return dom.wait_for_xpath(self._ctx, xpath, timeout)

    def wait_for_text(self, text: str, timeout: float = 10.0) -> Element | None:
        return dom.wait_for_text(self._ctx, text, timeout)

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
            self._ctx,
            selector,
            timeout,
            state=state,
            conditions=conditions,
            raise_on_timeout=raise_on_timeout,
        )

    def get_by_text(self, text: str, *, exact: bool = False) -> list[Element]:
        return dom.get_by_text(self._ctx, text, exact=exact)

    def get_all_interactive(self) -> list[Element]:
        return dom.get_all_interactive(self._ctx)

    def _resolve_element(
        self, target: str | Element, *, timeout: float = 10.0
    ) -> Element:
        if isinstance(target, Element):
            return target
        el = dom.wait_for_element(self._ctx, target, timeout)
        if el is None:
            # Only an element-less match (e.g. a detached state) returns None.
            raise ElementNotFoundError(
                f"{target!r} matched without an element to act on"
            )
        return el

    # -- native input -------------------------------------------------------

    def click(
        self,
        selector: str | Element,
        *,
        human: bool = True,
        click_type: ClickType = ClickType.LEFT,
        timeout: float = 10.0,
    ) -> Element:
        return self.click_at(selector, click_type, human=human, timeout=timeout)

    def get_center(self, target: str | Element, *, timeout: float = 10.0) -> Point:
        el = self._resolve_element(target, timeout=timeout)
        el.scroll_into_view()
        return Point(x=int(el.screen_x), y=int(el.screen_y))

    def move_to(
        self,
        target: str | Element,
        offset_x: float | None = None,
        offset_y: float | None = None,
        *,
        human: bool = True,
        timeout: float = 10.0,
    ) -> Element:
        self._ctx.require_native_input()
        el = self._resolve_element(target, timeout=timeout)
        self._ctx.mark_action()
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
        if click_type not in _CLICK_TYPES:
            raise InvalidArgumentError(f"unsupported click_type {click_type!r}")
        self._ctx.require_native_input()
        el = self._resolve_element(target, timeout=timeout)
        self._ctx.mark_action()
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
        selector: str | Element,
        text: str,
        *,
        characters_per_minute: int | None = None,
        offset: int | None = None,
        human: bool = True,
        timeout: float = 10.0,
    ) -> Element:
        """``type_at`` with a left click; None keeps the typing rhythm's speed."""
        return self.type_at(
            selector,
            text,
            characters_per_minute=characters_per_minute,
            offset=offset,
            human=human,
            timeout=timeout,
        )

    def type_at(
        self,
        target: str | Element,
        text: str,
        *,
        characters_per_minute: int | None = None,
        offset: int | None = None,
        human: bool = True,
        click_type: ClickType = ClickType.LEFT,
        timeout: float = 10.0,
    ) -> Element:
        """Click ``target`` and type ``text`` (``Element.type``).

        ``characters_per_minute`` and ``offset`` (ms of jitter) override the
        browser's typing rhythm; None keeps the rhythm's own values.
        """
        self._ctx.require_native_input()
        el = self._resolve_element(target, timeout=timeout)
        self._ctx.mark_action()
        el.type(
            text,
            characters_per_minute=characters_per_minute,
            offset=offset,
            human=human,
            click_type=click_type,
        )
        return el

    def drag_and_drop(
        self,
        source_selector: str | Element,
        target_selector: str | Element,
        *,
        human: bool = True,
        timeout: float = 10.0,
    ) -> None:
        self._ctx.require_native_input()
        source = self._resolve_element(source_selector, timeout=timeout)
        target = self._resolve_element(target_selector, timeout=timeout)
        self._ctx.mark_action()
        source.drag_to(target, human=human)

    # -- page ---------------------------------------------------------------

    def execute_script(self, code: str) -> Any:
        """JSON value of the script's completion value (``None`` for undefined)."""
        return page.execute_script(self._ctx, code)

    def page_info(self) -> PageInfo:
        return page.page_info(self._ctx)

    def scroll_to(
        self, target: int | str | Element, y: int | None = None
    ) -> dict[str, Any]:
        if isinstance(target, (str, Element)):
            el = self._resolve_element(target)
            self._ctx.mark_action()
            return el.scroll_into_view()
        if y is None:
            raise TypeError("scroll_to() missing y coordinate")
        return page.scroll_to(self._ctx, target, y)

    @property
    def url(self) -> str:
        return page.get_url(self._ctx)

    @property
    def title(self) -> str:
        return page.get_title(self._ctx)

    def wait_for_idle(self, silence: float = 2.0, timeout: float = 30.0) -> bool:
        return page.wait_for_idle(self._ctx, silence, timeout)

    # -- tabs ---------------------------------------------------------------

    def new_tab(self, url: str = "about:blank", *, switch: bool = True) -> Record:
        return tab_ops.new_tab(self._ctx, url, switch=switch)

    def switch_to_tab(self, tab_id: int) -> Record:
        return tab_ops.switch_to_tab(self._ctx, tab_id)

    def tabs(self) -> list[TabInfo]:
        return tab_ops.list_tabs(self._ctx)

    def close_tab(self, tab_id: int | None = None) -> Record:
        return tab_ops.close_tab(self._ctx, tab_id)

    def tab_info(self) -> TabInfo:
        return tab_ops.tab_info(self._ctx)

    # -- network ------------------------------------------------------------

    def wait_for_response(
        self,
        url_pattern: str,
        timeout: float = 10.0,
        *,
        since: float | None = None,
        any_tab: bool = False,
    ) -> NetworkResponse | None:
        """First response matching ``url_pattern`` (glob) in the pinned tab.

        ``since`` (epoch ms) defaults to the start of the last action, so a
        response that finished before it is not matched; ``None`` on timeout.
        """
        return page.wait_for_response(
            self._ctx, url_pattern, timeout, since=since, any_tab=any_tab
        )

    def expect_response(
        self, url_pattern: str, timeout: float = 10.0, *, any_tab: bool = False
    ) -> ResponseExpectation:
        """``with browser.expect_response("*/api/*") as resp: ...``; then ``resp.value``."""
        return ResponseExpectation(self._ctx, url_pattern, timeout, any_tab=any_tab)

    def __enter__(self) -> Self:
        return self.launch()

    def __exit__(self, *exc: object) -> None:
        self.close()
