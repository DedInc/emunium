"""DOM lookups that return ``Element`` handles.

Every function takes a ``BrowserContext`` (a bare ``Bridge`` is wrapped in its
default context). Transport and command errors propagate; only a
``waitForSelector`` timeout is translated: a ``WaitTimeoutError`` (a
``TimeoutError``) naming the page and carrying a count-probe hint
(``dx-api.md`` section 2), or ``None`` with ``raise_on_timeout=False``.
Element-less wait matches (e.g. ``state="detached"``) return ``None``: there
is no element to hand out.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from typing import Any, Union

from emunium._bridge.facade import Bridge
from emunium._browser.context import BrowserContext
from emunium.element import Element, ElementQuery
from emunium.errors import EmuniumError, WaitTimeoutError
from emunium.wait import Wait, WaitStrategy

logger = logging.getLogger("emunium.browser")

ContextOrBridge = Union[BrowserContext, Bridge]

# Bridge timeout (s) of each best-effort probe made after a wait timed out.
TIMEOUT_PROBE_S = 2.0
# The page context content_wait.js appends to its timeout message.
_PAGE_CONTEXT = re.compile(
    r'\(url: (?P<url>.*), title: (?P<title>"(?:[^"\\]|\\.)*")\)\s*$', re.DOTALL
)


def _context_from_message(message: str) -> tuple[str, str] | None:
    match = _PAGE_CONTEXT.search(message)
    if match is None:
        return None
    try:
        title = json.loads(match["title"])
    except ValueError:
        return None
    return (match["url"], title) if isinstance(title, str) else None


def _page_context(
    context: BrowserContext, exc: WaitTimeoutError
) -> tuple[str, str] | None:
    """(url, title) from the extension's message, else one ``pageInfo`` probe."""
    found = _context_from_message(exc.message)
    if found is not None:
        return found
    try:
        info = context.bridge.page_info(TIMEOUT_PROBE_S)
    except EmuniumError as probe_error:
        # Best effort: the timeout is the error being reported, not the probe.
        logger.debug("pageInfo after a wait timeout failed: %s", probe_error)
        return None
    url, title = info.get("url"), info.get("title")
    if isinstance(url, str) and isinstance(title, str):
        return url, title
    return None


def _match_count(context: BrowserContext, selector: str, kind: str) -> int | None:
    """How many elements match ``selector`` now; None when the probe failed."""
    bridge = context.bridge
    try:
        if kind == "xpath":
            matches = bridge.query_xpath(selector, TIMEOUT_PROBE_S)
        elif kind == "text":
            # waitForSelector's text type matches substrings (exact=False).
            matches = bridge.get_element_by_text(selector, False, TIMEOUT_PROBE_S)
        else:
            matches = bridge.query_selector_all(selector, TIMEOUT_PROBE_S)
    except EmuniumError as probe_error:
        logger.debug("count probe after a wait timeout failed: %s", probe_error)
        return None
    return len(matches)


def _requirement(state: str | None, conditions: list[dict[str, Any]] | None) -> str:
    parts = []
    if state:
        parts.append(f"state {state!r}")
    names = [
        str(condition.get("type"))
        for condition in conditions or []
        if isinstance(condition, Mapping)
    ]
    if names:
        parts.append("conditions " + ", ".join(names))
    return " and ".join(parts) or "the wait before it timed out"


def _timeout_error(
    context: BrowserContext,
    exc: WaitTimeoutError,
    selector: str,
    kind: str,
    timeout: float,
    *,
    what: str,
    state: str | None,
    conditions: list[dict[str, Any]] | None,
) -> WaitTimeoutError:
    message = f"{what} not found after {timeout}s: {selector!r}"
    page = _page_context(context, exc)
    if page is not None:
        message += f" (url={page[0]}, title={page[1]!r})"
    count = _match_count(context, selector, kind)
    if count is None:
        hint = None
    elif count == 0:
        hint = f"no element matches {selector!r} on this page"
    else:
        hint = (
            f"{count} element(s) match but none satisfied "
            f"{_requirement(state, conditions)}"
        )
    return WaitTimeoutError(
        message,
        code="wait_timeout",
        method=exc.method or "waitForSelector",
        hint=hint,
    )


def wait_arguments(
    strategy: WaitStrategy | str | None, condition: Wait | list[dict] | None
) -> tuple[str | None, list[dict[str, Any]] | None]:
    """``(state, conditions)`` for ``waitForSelector``, as ``Browser.wait`` maps them.

    A ``WaitStrategy`` becomes its value (other values pass through); a
    ``Wait`` becomes its payload, a list is sent as given, anything else is
    no conditions.
    """
    state = strategy.value if isinstance(strategy, WaitStrategy) else strategy
    if isinstance(condition, Wait):
        conditions = condition.to_payload()
    elif isinstance(condition, list):
        conditions = condition
    else:
        conditions = None
    return state, conditions


def query_selector(ctx: ContextOrBridge, selector: str) -> Element | None:
    context = BrowserContext.of(ctx)
    data = context.bridge.query_selector(selector)
    if data is None:
        return None
    return Element.from_data(context, data, query=ElementQuery(selector))


def query_selector_all(ctx: ContextOrBridge, selector: str) -> list[Element]:
    context = BrowserContext.of(ctx)
    results = context.bridge.query_selector_all(selector)
    return [
        Element.from_data(context, data, query=ElementQuery(selector, index))
        for index, data in enumerate(results)
    ]


def _wait(
    context: BrowserContext,
    selector: str,
    kind: str,
    timeout: float,
    *,
    what: str,
    state: str | None = None,
    conditions: list[dict[str, Any]] | None = None,
    raise_on_timeout: bool = True,
) -> Element | None:
    logger.info("Waiting for %s %r (timeout=%.1fs)...", what, selector, timeout)
    try:
        data = context.bridge.wait_for_selector(
            selector,
            type=kind,
            timeout=timeout,
            state=state,
            conditions=conditions,
        )
    except WaitTimeoutError as exc:
        if not raise_on_timeout:
            return None
        raise _timeout_error(
            context,
            exc,
            selector,
            kind,
            timeout,
            what=what,
            state=state,
            conditions=conditions,
        ) from exc
    if "elementId" not in data:
        logger.info("%s %r matched without an element", what, selector)
        return None
    # waitForSelector resolves to the first match in document order.
    return Element.from_data(context, data, query=ElementQuery(selector, 0, kind))


def wait_for_element(
    ctx: ContextOrBridge,
    selector: str,
    timeout: float = 10.0,
    state: str | None = None,
    conditions: list[dict[str, Any]] | None = None,
    raise_on_timeout: bool = True,
) -> Element | None:
    return _wait(
        BrowserContext.of(ctx),
        selector,
        "css",
        timeout,
        what="Element",
        state=state,
        conditions=conditions,
        raise_on_timeout=raise_on_timeout,
    )


def wait_for_xpath(
    ctx: ContextOrBridge,
    xpath: str,
    timeout: float = 10.0,
    raise_on_timeout: bool = True,
    *,
    strategy: WaitStrategy | str | None = None,
    condition: Wait | list[dict] | None = None,
) -> Element | None:
    """``strategy``/``condition`` mean what they mean in ``Browser.wait``."""
    state, conditions = wait_arguments(strategy, condition)
    return _wait(
        BrowserContext.of(ctx),
        xpath,
        "xpath",
        timeout,
        what="XPath",
        state=state,
        conditions=conditions,
        raise_on_timeout=raise_on_timeout,
    )


def wait_for_text(
    ctx: ContextOrBridge,
    text: str,
    timeout: float = 10.0,
    raise_on_timeout: bool = True,
    *,
    strategy: WaitStrategy | str | None = None,
    condition: Wait | list[dict] | None = None,
) -> Element | None:
    """``strategy``/``condition`` mean what they mean in ``Browser.wait``."""
    state, conditions = wait_arguments(strategy, condition)
    return _wait(
        BrowserContext.of(ctx),
        text,
        "text",
        timeout,
        what="Text",
        state=state,
        conditions=conditions,
        raise_on_timeout=raise_on_timeout,
    )


def get_by_text(
    ctx: ContextOrBridge, text: str, *, exact: bool = False
) -> list[Element]:
    context = BrowserContext.of(ctx)
    results = context.bridge.get_element_by_text(text, exact=exact)
    return [
        Element.from_data(
            context, data, query=ElementQuery(text, index, "text", exact=exact)
        )
        for index, data in enumerate(results)
    ]


def get_all_interactive(ctx: ContextOrBridge) -> list[Element]:
    context = BrowserContext.of(ctx)
    return [
        Element.from_data(context, data)
        for data in context.bridge.get_all_interactive()
    ]
