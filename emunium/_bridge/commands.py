"""Helpers for the ``Bridge`` facade: method table and result shaping.

Contracts: ``docs/architecture/bridge-protocol.md`` section 6 (methods, params,
result shapes, idempotency) and ``docs/architecture/python-internals.md``
(Bridge facade).
"""

from __future__ import annotations

from typing import Any

from emunium._bridge.transport import Reply
from emunium.errors import BridgeProtocolError

__all__ = [
    "IDEMPOTENT_METHODS",
    "WAIT_GRACE",
    "element_list",
    "element_or_none",
    "enrich_element",
    "expect_dict",
    "expect_list",
    "is_idempotent",
    "to_ms",
]

# bridge-protocol.md section 6, column "idempotent" = yes. Only these are
# re-sent once after a reconnect.
IDEMPOTENT_METHODS: frozenset[str] = frozenset(
    {
        "getTabInfo",
        "listTabs",
        "activateTab",
        "waitForResponse",
        "getRecentResponses",
        "ping",
        "pageInfo",
        "querySelector",
        "querySelectorAll",
        "queryXPath",
        "queryByText",
        "getAllInteractive",
        "describeElement",
        "elementText",
        "scrollIntoView",
        "scrollTo",
        "focus",
        "focusState",
        "pointerProbe",
        "getAttribute",
        "getComputedStyle",
    }
)

# Commands that wait send ``timeoutMs``; the transport waits this much longer so
# the extension's own ``timeout`` reply arrives before the local deadline.
WAIT_GRACE = 5.0


def is_idempotent(method: str) -> bool:
    return method in IDEMPOTENT_METHODS


def to_ms(seconds: float) -> int:
    return round(float(seconds) * 1000)


def enrich_element(element: dict[str, Any], reply: Reply) -> dict[str, Any]:
    """Copy of ``element`` with the reply envelope's ``tabId`` and ``zoom``."""
    enriched = dict(element)
    enriched["tabId"] = reply.tab_id
    enriched["zoom"] = reply.zoom
    return enriched


def element_or_none(method: str, reply: Reply) -> dict[str, Any] | None:
    result = reply.result
    if result is None:
        return None
    if not isinstance(result, dict):
        raise _shape_error(method, "an element or null", result)
    return enrich_element(result, reply)


def element_list(method: str, reply: Reply) -> list[dict[str, Any]]:
    elements = expect_list(method, reply)
    out = []
    for element in elements:
        if not isinstance(element, dict):
            raise _shape_error(method, "a list of elements", element)
        out.append(enrich_element(element, reply))
    return out


def expect_dict(method: str, reply: Reply) -> dict[str, Any]:
    result = reply.result
    if not isinstance(result, dict):
        raise _shape_error(method, "an object", result)
    return result


def expect_list(method: str, reply: Reply) -> list[Any]:
    result = reply.result
    if result is None:
        return []
    if not isinstance(result, list):
        raise _shape_error(method, "a list", result)
    return result


def _shape_error(method: str, expected: str, got: Any) -> BridgeProtocolError:
    return BridgeProtocolError(
        f"{method} returned {type(got).__name__}, expected {expected}",
        method=method,
    )
