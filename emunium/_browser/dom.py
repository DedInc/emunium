"""DOM lookups that return ``Element`` handles.

Every function takes a ``BrowserContext`` (a bare ``Bridge`` is wrapped in its
default context). Transport and command errors propagate; only a
``waitForSelector`` timeout is translated (``TimeoutError``, or ``None`` with
``raise_on_timeout=False``). Element-less wait matches (e.g. ``state="detached"``)
return ``None``: there is no element to hand out.
"""

from __future__ import annotations

import logging
from typing import Any, Union

from emunium._bridge.facade import Bridge
from emunium._browser.context import BrowserContext
from emunium.element import Element, ElementQuery
from emunium.errors import WaitTimeoutError

logger = logging.getLogger("emunium.browser")

ContextOrBridge = Union[BrowserContext, Bridge]


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
        raise TimeoutError(f"{what} not found after {timeout}s: {selector!r}") from exc
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
) -> Element | None:
    return _wait(
        BrowserContext.of(ctx),
        xpath,
        "xpath",
        timeout,
        what="XPath",
        raise_on_timeout=raise_on_timeout,
    )


def wait_for_text(
    ctx: ContextOrBridge,
    text: str,
    timeout: float = 10.0,
    raise_on_timeout: bool = True,
) -> Element | None:
    return _wait(
        BrowserContext.of(ctx),
        text,
        "text",
        timeout,
        what="Text",
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
