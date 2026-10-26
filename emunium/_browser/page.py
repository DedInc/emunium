"""Page-level operations of a ``Browser``: navigation, scripts, idle and network waits.

Every function takes the browser's ``BrowserContext``. Navigation pins the tab it
landed in; network waits default ``since`` to the context's last action, so a
response that finished before the action is never reported for it.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from types import TracebackType
from typing import TYPE_CHECKING, Any, TypeAlias

from emunium._bridge.transport import PINNED
from emunium._browser.context import BrowserContext
from emunium._browser.tabs import pin_from
from emunium.errors import (
    BridgeTimeoutError,
    ContentUnavailableError,
    InvalidArgumentError,
    NavigationTimeoutError,
    WaitTimeoutError,
)
from emunium.results import NetworkResponse, PageInfo

if TYPE_CHECKING:
    from typing_extensions import Self

logger = logging.getLogger("emunium.browser")

IDLE_POLL_S = 0.3
# Errors a page raises while it is between documents; anything else is real.
TRANSIENT_IDLE_ERRORS = (ContentUnavailableError,)

URL_POLL_S = 0.1
# Retried inside a URL wait: a document swap, or a call cut off by the deadline.
URL_WAIT_RETRIED = (*TRANSIENT_IDLE_ERRORS, BridgeTimeoutError)
# readyState values that satisfy each ``wait_until`` of ``navigate``; None: any.
WAIT_UNTIL_STATES: dict[str, frozenset[str] | None] = {
    "commit": None,
    "domcontentloaded": frozenset({"interactive", "complete"}),
    "load": frozenset({"complete"}),
}

UrlMatcher: TypeAlias = str | re.Pattern[str] | Callable[[str], bool]

_sleep = time.sleep


def compile_glob(pattern: str) -> re.Pattern[str]:
    """The URL glob of ``waitForResponse`` (background_network.js) as a regex.

    Only ``*`` is a wildcard (any run of characters, newlines included); every
    other character is literal; use ``fullmatch`` (anchored at both ends).
    """
    if not isinstance(pattern, str) or not pattern:
        raise InvalidArgumentError(
            f"a URL glob must be a non-empty string, got {pattern!r}"
        )
    return re.compile(".*".join(map(re.escape, pattern.split("*"))), re.DOTALL)


def url_matcher(url: UrlMatcher) -> Callable[[str], bool]:
    """A predicate for a glob, a compiled ``str`` pattern (``search``) or a callable."""
    if isinstance(url, str):
        glob = compile_glob(url)
        return lambda current: glob.fullmatch(current) is not None
    if isinstance(url, re.Pattern):
        if not isinstance(url.pattern, str):
            raise InvalidArgumentError(f"a URL pattern must match str, got {url!r}")
        return lambda current: url.search(current) is not None
    if callable(url):
        return lambda current: bool(url(current))
    raise InvalidArgumentError(
        f"url must be a glob string, a re.Pattern or a callable, got {url!r}"
    )


def _wait_until_states(wait_until: str) -> frozenset[str] | None:
    if wait_until not in WAIT_UNTIL_STATES:
        raise InvalidArgumentError(
            f"wait_until must be one of {', '.join(sorted(WAIT_UNTIL_STATES))}; "
            f"got {wait_until!r}"
        )
    return WAIT_UNTIL_STATES[wait_until]


class _UrlWait:
    """One URL wait: polls getTabInfo, then confirms the state with pageInfo.

    getTabInfo is a worker call, so it answers while the document swaps.
    Every call gets the time left as its timeout; ``URL_WAIT_RETRIED`` errors
    (a document swap, a call cut off by the deadline) are retried, so running
    out of time always ends in ``NavigationTimeoutError``.
    """

    def __init__(
        self,
        ctx: BrowserContext,
        matches: Callable[[str], bool],
        *,
        timeout: float,
        wait_until: str,
        tab_id: Any,
        describe: str,
        need_info: bool,
    ) -> None:
        self._bridge = ctx.bridge
        self._matches = matches
        self._timeout = timeout
        self._wait_until = wait_until
        self._accepted = _wait_until_states(wait_until)
        self._tab_id = tab_id
        self._describe = describe
        self._need_info = need_info
        self._deadline = time.monotonic() + timeout
        self._last_url: str | None = None
        self._matched = False
        self._last_state = "unknown"

    def _remaining(self) -> float:
        return self._deadline - time.monotonic()

    def run(self) -> tuple[str, PageInfo | None]:
        """The matching URL and, when pageInfo was needed, the page's info."""
        while self._remaining() > 0:
            try:
                found = self._poll()
            except URL_WAIT_RETRIED as exc:
                logger.debug("URL wait: transient %s: %s", type(exc).__name__, exc)
            else:
                if found is not None:
                    return found
            remaining = self._remaining()
            if remaining <= 0:
                break
            _sleep(min(URL_POLL_S, remaining))
        raise self._timeout_error()

    def _poll(self) -> tuple[str, PageInfo | None] | None:
        tab = self._bridge.get_tab_info(self._remaining(), tab_id=self._tab_id)
        current = str(tab.get("url") or "")
        self._last_url = current
        self._matched = self._matches(current)
        if not self._matched:
            self._last_state = "unknown"
            return None
        if self._accepted is None and not self._need_info:
            return current, None
        try:
            info = self._bridge.page_info(self._remaining(), tab_id=self._tab_id)
        except ContentUnavailableError:
            # No content script: a swap in progress, or a page that never gets
            # one (chrome-error pages, the PDF viewer, chrome://). The tab's
            # load status stands in for readyState.
            status = tab.get("status")
            self._last_state = f"pageInfo unavailable, tab status {status!r}"
            if self._accepted is None or status == "complete":
                return current, PageInfo(url=current, title=tab.get("title", ""))
            return None
        state = info.get("readyState")
        info_url = info.get("url")
        if not isinstance(info_url, str) or not self._matches(info_url):
            # The document answering is not (or no longer) the matching one.
            self._last_state = f"{state!r} on {info_url!r}"
            return None
        self._last_state = repr(state)
        if self._accepted is None or state in self._accepted:
            return current, PageInfo(info)
        return None

    def _timeout_error(self) -> NavigationTimeoutError:
        if self._matched:
            message = (
                f"URL did {self._describe} but readyState never reached "
                f"{self._wait_until!r} within {self._timeout}s "
                f"(last state: {self._last_state}, last url: {self._last_url})"
            )
        else:
            message = (
                f"URL did not {self._describe} within {self._timeout}s "
                f"(last url: {self._last_url})"
            )
        return NavigationTimeoutError(message, code="navigation_timeout")


def _describe(url: UrlMatcher) -> str:
    """The expectation as a verb phrase: ``"match '*/next'"``."""
    if isinstance(url, (str, re.Pattern)):
        return f"match {url!r}"
    return "match predicate " + getattr(url, "__qualname__", repr(url))


def wait_for_url(
    ctx: BrowserContext,
    url: UrlMatcher,
    *,
    timeout: float = 30.0,
    wait_until: str = "load",
) -> str:
    """The pinned tab's URL once it matches ``url`` and ``wait_until`` holds."""
    _wait_until_states(wait_until)
    matches = url_matcher(url)
    found, _ = _UrlWait(
        ctx,
        matches,
        timeout=timeout,
        wait_until=wait_until,
        tab_id=PINNED,
        describe=_describe(url),
        need_info=False,
    ).run()
    return found


def goto(
    ctx: BrowserContext, url: str, *, timeout: float = 30.0, wait_until: str = "load"
) -> PageInfo:
    """Navigate the pinned tab; the reply keeps every key ``navigate`` returns."""
    ctx.mark_action()
    result = ctx.bridge.navigate(url, timeout, wait_until=wait_until)
    pin_from(ctx, result, "navigate")
    logger.info("Navigated to: %s", url)
    return PageInfo(result)


def get_url(ctx: BrowserContext) -> str:
    return str(ctx.bridge.get_tab_info().get("url", ""))


def get_title(ctx: BrowserContext) -> str:
    return str(ctx.bridge.get_tab_info().get("title", ""))


def execute_script(ctx: BrowserContext, code: str) -> Any:
    """JSON value of the script's completion value (``None`` for undefined)."""
    ctx.mark_action()
    return ctx.bridge.execute_script(code)


def page_info(ctx: BrowserContext) -> PageInfo:
    return PageInfo(ctx.bridge.page_info())


def scroll_to(ctx: BrowserContext, x: int, y: int) -> dict[str, Any]:
    ctx.mark_action()
    return ctx.bridge.scroll_to(x, y)


def _complete(ctx: BrowserContext) -> bool:
    return ctx.bridge.page_info().get("readyState") == "complete"


def wait_for_idle(
    ctx: BrowserContext, silence: float = 2.0, timeout: float = 30.0
) -> bool:
    """True once ``readyState`` is complete before and after ``silence`` seconds.

    Only ``TRANSIENT_IDLE_ERRORS`` (a document swap in progress) are retried;
    every other error propagates. False when ``timeout`` runs out.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _complete(ctx):
                _sleep(silence)
                if _complete(ctx):
                    return True
        except TRANSIENT_IDLE_ERRORS as exc:
            logger.debug("wait_for_idle: transient %s: %s", type(exc).__name__, exc)
        _sleep(IDLE_POLL_S)
    return False


def wait_for_response(
    ctx: BrowserContext,
    url_pattern: str,
    timeout: float = 10.0,
    *,
    since: float | None = None,
    any_tab: bool = False,
) -> NetworkResponse | None:
    """First response matching ``url_pattern`` (glob), or ``None`` on timeout.

    ``since`` (epoch ms) defaults to the last action on this browser.
    """
    if since is None:
        since = ctx.last_action_ms
    entry = ctx.bridge.wait_for_response(
        url_pattern, timeout, since=since, any_tab=any_tab
    )
    return None if entry is None else NetworkResponse(entry)


class ResponseExpectation:
    """``with`` block that waits, on exit, for a response started inside it.

    ``since`` is taken when the block is entered; the ``timeout`` runs from the
    block's exit. ``value`` holds the network entry once the block exited
    cleanly; ``WaitTimeoutError`` (a ``TimeoutError``) is raised on exit when
    nothing matched. When the block raises, nothing is awaited and the
    exception propagates.
    """

    def __init__(
        self,
        ctx: BrowserContext,
        url_pattern: str,
        timeout: float = 10.0,
        *,
        any_tab: bool = False,
    ) -> None:
        self._ctx = ctx
        self._pattern = url_pattern
        self._timeout = timeout
        self._any_tab = any_tab
        self._since: float | None = None
        self._tab_id: Any = PINNED
        self._value: NetworkResponse | None = None

    @property
    def value(self) -> NetworkResponse:
        """The matching ``NetworkResponse``; set after a clean exit.

        Raises:
            RuntimeError: Read inside the block or after it raised.
        """
        if self._value is None:
            raise RuntimeError(
                "expect_response(): the response is available after the with "
                "block exits without an error"
            )
        return self._value

    def __enter__(self) -> Self:
        # The tab is fixed here: an action in the block may move the pin. With
        # no pin yet, the block's own navigation decides the tab on exit.
        pinned = self._ctx.bridge.pinned_tab_id
        self._tab_id = PINNED if pinned is None else pinned
        self._since = self._ctx.mark_action()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            return
        entry = self._ctx.bridge.wait_for_response(
            self._pattern,
            self._timeout,
            since=self._since,
            any_tab=self._any_tab,
            tab_id=self._tab_id,
        )
        if entry is None:
            raise WaitTimeoutError(
                f"no response matching {self._pattern!r} within {self._timeout}s",
                code="wait_timeout",
                method="waitForResponse",
            )
        self._value = NetworkResponse(entry)

    def __repr__(self) -> str:
        any_tab = ", any_tab=True" if self._any_tab else ""
        return (
            f"ResponseExpectation({self._pattern!r}, timeout={self._timeout}{any_tab})"
        )


class NavigationExpectation:
    """``with`` block that waits, on exit, for the tab to navigate.

    The tab and its URL are recorded when the block is entered; the
    ``timeout`` runs from the block's exit. On a clean exit it waits until the
    URL differs from the recorded one (and matches ``url`` when given) and the
    ``wait_until`` state is reached, then ``value`` holds the ``PageInfo``.
    ``NavigationTimeoutError`` (a ``TimeoutError``) is raised on exit when that
    does not happen in time. A navigation to the same URL is not detected.
    When the block raises, nothing is awaited and the exception propagates.
    """

    def __init__(
        self,
        ctx: BrowserContext,
        url: UrlMatcher | None = None,
        *,
        timeout: float = 30.0,
        wait_until: str = "load",
    ) -> None:
        # Validated here, so a bad argument fails before the block runs.
        _wait_until_states(wait_until)
        self._matches = None if url is None else url_matcher(url)
        self._url_phrase = None if url is None else _describe(url)
        self._ctx = ctx
        self._url = url
        self._timeout = timeout
        self._wait_until = wait_until
        self._tab_id: Any = PINNED
        self._start_url: str | None = None
        self._value: PageInfo | None = None

    @property
    def value(self) -> PageInfo:
        """The ``PageInfo`` of the page navigated to; set after a clean exit.

        Raises:
            RuntimeError: Read inside the block or after it raised.
        """
        if self._value is None:
            raise RuntimeError(
                "expect_navigation(): the page is available after the with "
                "block exits without an error"
            )
        return self._value

    def __enter__(self) -> Self:
        # The tab is fixed here: an action in the block may move the pin.
        pinned = self._ctx.bridge.pinned_tab_id
        self._tab_id = PINNED if pinned is None else pinned
        tab = self._ctx.bridge.get_tab_info(tab_id=self._tab_id)
        self._start_url = str(tab.get("url") or "")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            return
        start = self._start_url
        wanted = self._matches
        describe = f"change from {start!r}"
        if self._url_phrase is not None:
            describe += f" and {self._url_phrase}"

        def navigated(current: str) -> bool:
            # A start URL that already matches ``url`` is not the navigation.
            return current != start and (wanted is None or wanted(current))

        found, info = _UrlWait(
            self._ctx,
            navigated,
            timeout=self._timeout,
            wait_until=self._wait_until,
            tab_id=self._tab_id,
            describe=describe,
            need_info=True,
        ).run()
        # need_info=True: run() always returns the page's info.
        self._value = info if info is not None else PageInfo(url=found)

    def __repr__(self) -> str:
        return (
            f"NavigationExpectation(url={self._url!r}, timeout={self._timeout}, "
            f"wait_until={self._wait_until!r})"
        )
