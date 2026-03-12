from __future__ import annotations

import logging
import time

from emunium.bridge import Bridge
from emunium.element import Element

logger = logging.getLogger("emunium.browser")


def query_selector(bridge: Bridge, selector: str) -> Element | None:
    data = bridge.query_selector(selector)
    if data:
        el = Element.from_data(bridge, data, selector=selector)
        logger.debug(
            "query_selector(%r) -> Screen(%.0f, %.0f)",
            selector,
            el.screen_x,
            el.screen_y,
        )
        return el
    return None


def query_selector_all(bridge: Bridge, selector: str) -> list[Element]:
    results = bridge.query_selector_all(selector)
    return [Element.from_data(bridge, d, selector=selector) for d in results]


def _wait_with_retry(
    bridge: Bridge,
    selector: str,
    type: str,
    timeout: float,
    state: str | None = None,
    conditions: list[dict] | None = None,
) -> dict | None:
    deadline = time.monotonic() + timeout
    data = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            data = bridge.wait_for_selector(
                selector,
                type=type,
                timeout=remaining,
                state=state,
                conditions=conditions,
            )
        except Exception:
            data = None
        if data is not None:
            return data
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        logger.debug("Retrying wait for %r (%.1fs remaining)...", selector, remaining)
        time.sleep(min(0.5, remaining))
    return None


def wait_for_element(
    bridge: Bridge,
    selector: str,
    timeout: float = 10.0,
    state: str | None = None,
    conditions: list[dict] | None = None,
    raise_on_timeout: bool = True,
) -> Element | None:
    logger.info("Waiting for %r (timeout=%.1fs)...", selector, timeout)
    data = _wait_with_retry(
        bridge, selector, "css", timeout, state=state, conditions=conditions
    )
    if data is None:
        if not raise_on_timeout:
            return None
        raise TimeoutError(f"Element not found after {timeout}s: {selector!r}")
    el = Element.from_data(bridge, data, selector=selector)
    logger.info("Element found at Screen(%.0f, %.0f)", el.screen_x, el.screen_y)
    return el


def wait_for_xpath(
    bridge: Bridge,
    xpath: str,
    timeout: float = 10.0,
    raise_on_timeout: bool = True,
) -> Element | None:
    logger.info("Waiting for XPath %r (timeout=%.1fs)...", xpath, timeout)
    data = _wait_with_retry(bridge, xpath, "xpath", timeout)
    if data is None:
        if not raise_on_timeout:
            return None
        raise TimeoutError(f"XPath not found after {timeout}s: {xpath!r}")
    el = Element.from_data(bridge, data)
    logger.info("XPath element found at Screen(%.0f, %.0f)", el.screen_x, el.screen_y)
    return el


def wait_for_text(
    bridge: Bridge,
    text: str,
    timeout: float = 10.0,
    raise_on_timeout: bool = True,
) -> Element | None:
    logger.info("Waiting for text %r (timeout=%.1fs)...", text, timeout)
    data = _wait_with_retry(bridge, text, "text", timeout)
    if data is None:
        if not raise_on_timeout:
            return None
        raise TimeoutError(f"Text not found after {timeout}s: {text!r}")
    el = Element.from_data(bridge, data)
    logger.info("Text element found at Screen(%.0f, %.0f)", el.screen_x, el.screen_y)
    return el


def get_by_text(bridge: Bridge, text: str, *, exact: bool = False) -> list[Element]:
    results = bridge.get_element_by_text(text, exact=exact)
    return [Element.from_data(bridge, d) for d in results]


def get_all_interactive(bridge: Bridge) -> list[Element]:
    results = bridge.get_all_interactive()
    return [Element.from_data(bridge, d) for d in results]
