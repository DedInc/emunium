"""Tab operations of a ``Browser``; each keeps the bridge's pinned tab current.

Commands without an explicit tab go to the pinned tab, so the pin follows what
the user sees: the initial tab after launch, a navigated or newly opened tab,
a switched-to tab, and the active tab after the pinned one closes.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from emunium._browser.context import BrowserContext
from emunium.errors import BridgeProtocolError, InvalidArgumentError, TargetClosedError
from emunium.results import Record, TabInfo

logger = logging.getLogger("emunium.browser")


def _tab_id_of(result: Mapping[str, Any], method: str) -> int:
    tab_id = result.get("tabId")
    if isinstance(tab_id, bool) or not isinstance(tab_id, int):
        raise BridgeProtocolError(
            f"{method} reply carries no integer tabId: {tab_id!r}", method=method
        )
    return tab_id


def pin_from(ctx: BrowserContext, result: Mapping[str, Any], method: str) -> int:
    """Pin the tab named by ``result["tabId"]`` and return it."""
    tab_id = _tab_id_of(result, method)
    ctx.bridge.pinned_tab_id = tab_id
    return tab_id


def pin_active(ctx: BrowserContext) -> int:
    """Pin the active tab of the last focused normal window."""
    return pin_from(ctx, ctx.bridge.get_tab_info(tab_id=None), "getTabInfo")


def new_tab(
    ctx: BrowserContext, url: str = "about:blank", *, switch: bool = True
) -> Record:
    """Open ``url`` in a new tab; with ``switch`` it is activated and pinned."""
    ctx.mark_action()
    result = ctx.bridge.create_tab(url, active=switch)
    if switch:
        pin_from(ctx, result, "createTab")
    return Record(result)


def tab_id_argument(value: object) -> int:
    """``value`` as a caller's tab id: an int that is not a bool."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidArgumentError(f"tab_id must be an int, got {value!r}")
    return value


def switch_to_tab(ctx: BrowserContext, tab_id: int) -> Record:
    """Activate ``tab_id`` (focusing its window) and pin it."""
    tab_id = tab_id_argument(tab_id)
    result = ctx.bridge.activate_tab(tab_id=tab_id)
    ctx.bridge.pinned_tab_id = tab_id
    return Record(result)


def list_tabs(ctx: BrowserContext) -> list[TabInfo]:
    return [TabInfo(tab) for tab in ctx.bridge.list_tabs()]


def close_tab(ctx: BrowserContext, tab_id: int | None = None) -> Record:
    """Close ``tab_id`` (``None``: the pinned tab).

    When that leaves nothing pinned, the active tab is pinned; when no tab is
    left to pin, the bridge stays unpinned.
    """
    result = ctx.bridge.close_tab(tab_id)
    if ctx.bridge.pinned_tab_id is None:
        try:
            pin_active(ctx)
        except TargetClosedError as exc:
            logger.info("No tab left to pin after close_tab: %s", exc)
    return Record(result)


def tab_info(ctx: BrowserContext) -> TabInfo:
    return TabInfo(ctx.bridge.get_tab_info())
