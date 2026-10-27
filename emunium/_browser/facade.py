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

from emunium._browser import dom, locators, page
from emunium._browser import tabs as tab_ops
from emunium._browser.context import BrowserContext
from emunium._browser.launcher import BrowserSession, close, launch
from emunium._browser.locators import ElementLocator
from emunium._browser.page import (
    NavigationExpectation,
    ResponseExpectation,
    UrlMatcher,
)
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
    """One Chrome session driven through the extension bridge.

    Use it as a context manager (``with Browser() as browser:``) or call
    ``launch()`` and ``close()``. Commands go to the pinned tab; element
    actions send verified OS-level input (refused when ``headless``).
    """

    # Attribute get_by_test_id matches; override on the class or an instance.
    test_id_attribute: str = "data-testid"

    def __init__(
        self,
        headless: bool = False,
        user_data_dir: str | None = None,
        bridge_port: int = 0,
        bridge_timeout: float = 60.0,
        *,
        browser: str | None = None,
        chrome_path: str | None = None,
        extra_args: list[str] | None = None,
        verify_input: bool = True,
        motion: MotionPlanner | str | None = None,
        typing: TypingRhythm | str | None = None,
    ) -> None:
        """Configure a session; nothing starts until ``launch()``.

        Args:
            headless: Run the browser headless (native input is then refused).
            user_data_dir: Browser profile directory to reuse; None uses a
                temporary profile removed on ``close()``.
            bridge_port: Local bridge port; 0 picks a free one.
            bridge_timeout: Seconds ``launch()`` waits for the extension.
            browser: Which browser to start: an installed ``chrome``,
                ``edge``, ``brave``, ``chromium`` or ``firefox``; ``cft``
                (Chrome for Testing, downloaded on first use); ``auto``; or
                the path of an executable. None uses ``chrome_path``, then
                ``EMUNIUM_CHROME_PATH``, then ``auto``: the first installed
                of chrome, edge, brave, chromium, else Chrome for Testing.
                Google Chrome gets the extension over its debugging pipe,
                Firefox over WebDriver BiDi, the others via
                ``--load-extension``.
            chrome_path: Browser binary, used when ``browser`` is None.
            extra_args: Extra browser command-line flags.
            verify_input: Prove the cursor is over an element before a click.
            motion: Human pointer planner: a preset name (``natural``,
                ``careful``, ``brisk``), a ``MotionPlanner`` or None
                (``natural`` ``SigmaDriftPlanner``).
            typing: Human typing rhythm: a preset name, a ``TypingRhythm`` or
                None (``natural``). ``human=False`` actions stay instant.
        """
        bridge = Bridge(port=bridge_port)
        self._session = BrowserSession()
        self._session.bridge = bridge
        self._session.headless = bool(headless)
        self._session.user_data_dir = user_data_dir
        self._session.browser = browser
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

    def __repr__(self) -> str:
        # Cached state only: a repr never sends a request.
        launched = self._session.process is not None
        port = self._ctx.bridge.actual_port
        return (
            f"Browser(headless={self._session.headless!r}, "
            f"launched={launched!r}, port={port!r})"
        )

    @property
    def bridge(self) -> Bridge:
        """The low-level ``Bridge`` (plain-data replies) this browser uses."""
        return self._ctx.bridge

    @property
    def browser_name(self) -> str | None:
        """Browser the last ``launch()`` resolved, None before the first launch.

        One of ``chrome``, ``edge``, ``brave``, ``chromium``, ``firefox``,
        ``cft`` (Chrome for Testing) or ``custom`` (an unrecognised path).
        """
        resolved = self._session.resolved
        return None if resolved is None else resolved.name

    @property
    def context(self) -> BrowserContext:
        """The ``BrowserContext`` shared by every element and locator of this browser."""
        return self._ctx

    def launch(self) -> Self:
        """Start Chrome and pin its initial tab; on failure nothing stays running.

        Then the window is brought to the foreground once (best effort: a
        browser started from a background process opens behind other windows).

        Returns:
            This browser.

        Raises:
            LaunchError: Chrome or the extension bridge did not start (other
                ``EmuniumError`` subclasses, e.g. from pinning the tab, pass
                through unwrapped).
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
        """Stop Chrome and the bridge and remove the temporary session files.

        Safe to call more than once; a ``user_data_dir`` profile is kept.
        """
        self._ctx.pid = None
        close(self._session)

    # -- navigation and lookups ---------------------------------------------

    def goto(
        self, url: str, *, timeout: float = 30.0, wait_until: str = "load"
    ) -> PageInfo:
        """Navigate the pinned tab to ``url`` and pin the tab it landed in.

        Args:
            url: Address to open.
            timeout: Seconds to wait for ``wait_until``.
            wait_until: ``commit``, ``domcontentloaded`` or ``load``.

        Returns:
            The navigation reply as a ``PageInfo`` (every key it carries).

        Raises:
            NavigationTimeoutError: ``wait_until`` was not reached in time.
            NavigationError: The navigation failed.
        """
        return page.goto(self._ctx, url, timeout=timeout, wait_until=wait_until)

    def query_selector(self, selector: str) -> Element | None:
        """Return the first element matching CSS ``selector`` now, or None (no wait)."""
        return dom.query_selector(self._ctx, selector)

    def query_selector_all(self, selector: str) -> list[Element]:
        """Return every element matching CSS ``selector`` now (no wait)."""
        return dom.query_selector_all(self._ctx, selector)

    def wait_for_element(self, selector: str, timeout: float = 10.0) -> Element | None:
        """Wait for the first element matching CSS ``selector`` and return it.

        Raises:
            WaitTimeoutError: Nothing matched within ``timeout``; the message
                names the page and the hint says how many elements match.
        """
        return dom.wait_for_element(self._ctx, selector, timeout)

    def wait_for_xpath(
        self,
        xpath: str,
        timeout: float = 10.0,
        *,
        strategy: WaitStrategy | None = None,
        condition: Wait | list[dict] | None = None,
    ) -> Element | None:
        """Wait for the first element matching ``xpath`` and return it.

        ``strategy`` and ``condition`` mean what they mean in ``wait``.

        Raises:
            WaitTimeoutError: Nothing satisfied the wait within ``timeout``.
        """
        return dom.wait_for_xpath(
            self._ctx, xpath, timeout, strategy=strategy, condition=condition
        )

    def wait_for_text(
        self,
        text: str,
        timeout: float = 10.0,
        *,
        strategy: WaitStrategy | None = None,
        condition: Wait | list[dict] | None = None,
    ) -> Element | None:
        """Wait for the first element containing ``text`` and return it.

        ``strategy`` and ``condition`` mean what they mean in ``wait``.

        Raises:
            WaitTimeoutError: Nothing satisfied the wait within ``timeout``.
        """
        return dom.wait_for_text(
            self._ctx, text, timeout, strategy=strategy, condition=condition
        )

    def wait(
        self,
        selector: str,
        strategy: WaitStrategy | None = None,
        condition: Wait | list[dict] | None = None,
        timeout: float = 10.0,
        raise_on_timeout: bool = True,
    ) -> Element | None:
        """Wait in the page for a CSS ``selector`` to satisfy a strategy and conditions.

        Args:
            selector: CSS selector; the first match in document order counts.
            strategy: A ``WaitStrategy`` (``presence``, ``visible``,
                ``clickable``, ``stable``, ``unobscured``) or None.
            condition: A ``Wait`` builder or its payload list.
            timeout: Seconds to wait.
            raise_on_timeout: False returns None instead of raising.

        Returns:
            The matching element; None when the match has no element (e.g. a
            ``detached`` condition) or on timeout with ``raise_on_timeout=False``.

        Raises:
            WaitTimeoutError: Nothing satisfied the wait within ``timeout``.
        """
        state, conditions = dom.wait_arguments(strategy, condition)
        return dom.wait_for_element(
            self._ctx,
            selector,
            timeout,
            state=state,
            conditions=conditions,
            raise_on_timeout=raise_on_timeout,
        )

    def get_by_text(self, text: str, *, exact: bool = False) -> list[Element]:
        """Return the elements whose trimmed text contains ``text``
        (case-insensitive; equals it, case-sensitive, with ``exact``) now,
        without waiting. ``locator("text=...")`` is the live counterpart."""
        return dom.get_by_text(self._ctx, text, exact=exact)

    def get_all_interactive(self) -> list[Element]:
        """Return the rendered (non-zero size) inputs, buttons, links, selects,
        textareas and elements with ``role``/``aria-label``/``name``/... now."""
        return dom.get_all_interactive(self._ctx)

    # -- live locators --------------------------------------------------------

    def locator(self, selector: str) -> ElementLocator:
        """Create a lazy locator; every call on it queries the page again.

        Args:
            selector: ``css=``, ``xpath=`` or ``text=`` prefixed; a bare
                string starting with ``//`` or ``(//`` is XPath, anything
                else CSS.

        Raises:
            InvalidArgumentError: ``selector`` is not a str or is empty.
        """
        return locators.locator(self._ctx, selector)

    def get_by_role(
        self, role: str, *, name: str | None = None, exact: bool = False
    ) -> ElementLocator:
        """Locate elements with ``role`` (implicit, or an explicit ``role=``).

        Args:
            role: ARIA role such as ``button`` or ``link``.
            name: Accessible name to match (text, ``aria-label``, ``title``,
                ``alt``, input-button ``value`` or ``<label>`` text).
            exact: Whole-string match after whitespace normalisation instead
                of a case-insensitive substring.
        """
        return locators.get_by_role(self._ctx, role, name=name, exact=exact)

    def get_by_label(self, text: str, *, exact: bool = False) -> ElementLocator:
        """Locate form controls named by a ``<label>`` matching ``text``, and
        elements whose ``aria-label`` matches it."""
        return locators.get_by_label(self._ctx, text, exact=exact)

    def get_by_placeholder(self, text: str, *, exact: bool = False) -> ElementLocator:
        """Locate elements whose ``placeholder`` matches ``text``."""
        return locators.get_by_placeholder(self._ctx, text, exact=exact)

    def get_by_test_id(self, test_id: str) -> ElementLocator:
        """Locate elements whose ``test_id_attribute`` (default ``data-testid``)
        equals ``test_id``."""
        return locators.get_by_test_id(
            self._ctx, test_id, attribute=self.test_id_attribute
        )

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
        """Click ``selector`` (or an element) with verified native input.

        Same as ``click_at``; see there for arguments and errors.
        """
        return self.click_at(selector, click_type, human=human, timeout=timeout)

    def get_center(self, target: str | Element, *, timeout: float = 10.0) -> Point:
        """Scroll ``target`` into view and return its center in input (screen) units.

        Raises:
            WaitTimeoutError: A selector matched nothing within ``timeout``.
            ElementNotFoundError: The selector matched without an element.
        """
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
        """Move the cursor onto ``target`` and verify it is over the element.

        Args:
            target: A CSS selector (waited for up to ``timeout``) or an element.
            offset_x: CSS px from the element's left edge; None picks a
                random point inside it.
            offset_y: CSS px from the element's top edge; None as for x.
            human: Human-like path; False jumps instantly.
            timeout: Seconds to wait for a selector.

        Returns:
            The element.

        Raises:
            NativeInputError: Headless browser, or the input could not be sent.
            ClickVerificationError: The cursor could not be verified over it.
            WaitTimeoutError: A selector matched nothing within ``timeout``.
            ElementNotFoundError: The selector matched without an element.
        """
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
        """Rest the cursor on ``target``; same as ``move_to``."""
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
        """Click ``target`` with OS-level input once the cursor is verified over it.

        Args:
            target: A CSS selector (waited for up to ``timeout``) or an element.
            click_type: ``LEFT``, ``DOUBLE``, ``RIGHT`` or ``MIDDLE``.
            human: Human-like path; False jumps instantly.
            timeout: Seconds to wait for a selector.

        Returns:
            The clicked element.

        Raises:
            InvalidArgumentError: Unsupported ``click_type`` (checked first).
            NativeInputError: Headless browser, or the input could not be sent.
            ClickVerificationError: The cursor could not be verified over the
                element; no button event was sent.
            WaitTimeoutError: A selector matched nothing within ``timeout``.
            ElementNotFoundError: The selector matched without an element.
        """
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
        """Click ``selector`` (left) and type ``text``; see ``type_at``.

        ``characters_per_minute``/``offset`` None keep the typing rhythm's own.
        """
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
        ``click_type`` is ``LEFT`` or ``DOUBLE``.

        Raises:
            InvalidArgumentError: ``click_type`` is neither ``LEFT`` nor ``DOUBLE``.
            NativeInputError: Headless browser, or the element does not hold
                (or lost) keyboard focus.
            ClickVerificationError: The cursor could not be verified over it.
            WaitTimeoutError: A selector matched nothing within ``timeout``.
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
        """Press on the source and release on the target (``Element.drag_to``).

        Each selector waits up to ``timeout``. A drop that cannot be verified
        is cancelled (cursor back, Escape, then the button is released).

        Raises:
            NativeInputError: Headless browser, elements in different tabs or
                not both visible.
            ClickVerificationError: Source or target could not be verified.
            WaitTimeoutError: A selector matched nothing within ``timeout``.
        """
        self._ctx.require_native_input()
        source = self._resolve_element(source_selector, timeout=timeout)
        target = self._resolve_element(target_selector, timeout=timeout)
        self._ctx.mark_action()
        source.drag_to(target, human=human)

    # -- page ---------------------------------------------------------------

    def execute_script(self, code: str) -> Any:
        """Run ``code`` in the pinned tab and return its completion value.

        Returns:
            The JSON value of the script's completion value (None for undefined).

        Raises:
            ScriptError: The script threw.
            CSPBlockedError: The page's Content Security Policy blocked it.
        """
        return page.execute_script(self._ctx, code)

    def page_info(self) -> PageInfo:
        """Return the pinned tab's URL, title, ``readyState``, scroll offsets and sizes.

        Raises:
            ContentUnavailableError: No content script answered (a document
                swap, or a page such as ``chrome://`` where none runs).
        """
        return page.page_info(self._ctx)

    def scroll_to(
        self, target: int | str | Element, y: int | None = None
    ) -> dict[str, Any]:
        """Scroll an element into view, or the page to ``(target, y)`` CSS px.

        Args:
            target: A CSS selector or element to scroll into view, or the x
                coordinate.
            y: The y coordinate; required with a numeric ``target``.

        Returns:
            The extension's reply (the element's fresh data for an element).

        Raises:
            TypeError: A numeric ``target`` without ``y``.
            WaitTimeoutError: A selector matched nothing within 10 s.
        """
        if isinstance(target, (str, Element)):
            el = self._resolve_element(target)
            self._ctx.mark_action()
            return el.scroll_into_view()
        if y is None:
            raise TypeError("scroll_to() missing y coordinate")
        return page.scroll_to(self._ctx, target, y)

    @property
    def url(self) -> str:
        """The pinned tab's current URL (read from the tab on every access)."""
        return page.get_url(self._ctx)

    @property
    def title(self) -> str:
        """The pinned tab's current title (read from the tab on every access)."""
        return page.get_title(self._ctx)

    def wait_for_idle(self, silence: float = 2.0, timeout: float = 30.0) -> bool:
        """Wait until ``readyState`` is ``complete`` before and after ``silence`` seconds.

        A document swap in progress is retried; other errors propagate.

        Returns:
            True once idle; False when ``timeout`` ran out.
        """
        return page.wait_for_idle(self._ctx, silence, timeout)

    def wait_for_url(
        self, url: UrlMatcher, *, timeout: float = 30.0, wait_until: str = "load"
    ) -> str:
        """Wait until the pinned tab's URL matches ``url``; return that URL.

        Args:
            url: A glob (only ``*`` is a wildcard, the whole URL must match,
                as in ``wait_for_response``), a compiled ``re.Pattern``
                (``search``) or a ``Callable[[str], bool]``.
            timeout: Seconds for the whole wait.
            wait_until: Document state reached afterwards (``commit``,
                ``domcontentloaded``, ``load``), as in ``goto``.

        Raises:
            InvalidArgumentError: Bad ``url`` or ``wait_until``.
            NavigationTimeoutError: (a ``TimeoutError``) the deadline passed;
                the message tells "never matched" from "matched but never
                loaded" and names the last URL seen.
        """
        return page.wait_for_url(self._ctx, url, timeout=timeout, wait_until=wait_until)

    def expect_navigation(
        self,
        url: UrlMatcher | None = None,
        *,
        timeout: float = 30.0,
        wait_until: str = "load",
    ) -> NavigationExpectation:
        """Wait, when the ``with`` block exits, for the tab to navigate.

        ``with browser.expect_navigation() as nav: ...``; then ``nav.value``
        (a ``PageInfo``). On a clean exit, waits until the URL differs from
        the one on entry (and matches ``url`` when given) and ``wait_until``
        is reached; a navigation to the same URL is not detected.

        Raises:
            InvalidArgumentError: Bad ``url`` or ``wait_until`` (at the call).
            NavigationTimeoutError: On exit, when no navigation landed in time.
        """
        return NavigationExpectation(
            self._ctx, url, timeout=timeout, wait_until=wait_until
        )

    # -- tabs ---------------------------------------------------------------

    def new_tab(self, url: str = "about:blank", *, switch: bool = True) -> Record:
        """Open ``url`` in a new tab; with ``switch`` it is activated and pinned.

        Returns:
            The extension's reply as a ``Record`` (``.tab_id``, ...).
        """
        return tab_ops.new_tab(self._ctx, url, switch=switch)

    def switch_to_tab(self, tab_id: int) -> Record:
        """Activate ``tab_id`` (focusing its window) and pin it.

        Raises:
            InvalidArgumentError: ``tab_id`` is not an int.
            TargetClosedError: No such tab.
        """
        return tab_ops.switch_to_tab(self._ctx, tab_id)

    def tabs(self) -> list[TabInfo]:
        """Return every open tab."""
        return tab_ops.list_tabs(self._ctx)

    def close_tab(self, tab_id: int | None = None) -> Record:
        """Close ``tab_id`` (None: the pinned tab).

        When that leaves nothing pinned, the active tab is pinned; with no tab
        left the bridge stays unpinned.

        Raises:
            TargetClosedError: No such tab.
        """
        return tab_ops.close_tab(self._ctx, tab_id)

    def tab_info(self) -> TabInfo:
        """Return the pinned tab's description."""
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
        """Wait for the first response matching ``url_pattern`` (glob) in the pinned tab.

        ``since`` (epoch ms) defaults to the start of the last action, so a
        response that finished before it is not matched; ``any_tab`` widens
        the search to every tab.

        Returns:
            The ``NetworkResponse``, or None on timeout.
        """
        return page.wait_for_response(
            self._ctx, url_pattern, timeout, since=since, any_tab=any_tab
        )

    def expect_response(
        self, url_pattern: str, timeout: float = 10.0, *, any_tab: bool = False
    ) -> ResponseExpectation:
        """Wait, when the ``with`` block exits, for a response started inside it.

        ``with browser.expect_response("*/api/*") as resp: ...``; then
        ``resp.value`` (a ``NetworkResponse``). ``timeout`` runs from the exit.

        Raises:
            WaitTimeoutError: On exit, when nothing matched in time.
        """
        return ResponseExpectation(self._ctx, url_pattern, timeout, any_tab=any_tab)

    def __enter__(self) -> Self:
        return self.launch()

    def __exit__(self, *exc: object) -> None:
        self.close()
