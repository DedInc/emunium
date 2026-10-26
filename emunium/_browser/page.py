"""Page-level operations of a ``Browser``: navigation, scripts, idle and network waits.

Every function takes the browser's ``BrowserContext``. Navigation pins the tab it
landed in; network waits default ``since`` to the context's last action, so a
response that finished before the action is never reported for it.
"""

from __future__ import annotations

import logging
import time
from types import TracebackType
from typing import TYPE_CHECKING, Any

from emunium._bridge.transport import PINNED
from emunium._browser.context import BrowserContext
from emunium._browser.tabs import pin_from
from emunium.errors import ContentUnavailableError, WaitTimeoutError
from emunium.results import NetworkResponse, PageInfo

if TYPE_CHECKING:
    from typing_extensions import Self

logger = logging.getLogger("emunium.browser")

IDLE_POLL_S = 0.3
# Errors a page raises while it is between documents; anything else is real.
TRANSIENT_IDLE_ERRORS = (ContentUnavailableError,)

_sleep = time.sleep


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
