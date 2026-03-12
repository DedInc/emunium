from __future__ import annotations

from emunium.bridge import Bridge


def new_tab(bridge: Bridge, url: str = "about:blank") -> dict:
    return bridge.create_tab(url)


def close_tab(bridge: Bridge, tab_id: int | None = None) -> dict:
    return bridge.close_tab(tab_id)


def tab_info(bridge: Bridge) -> dict:
    return bridge.get_tab_info()
