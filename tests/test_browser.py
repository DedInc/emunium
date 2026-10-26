"""Browser facade contract: one context, tab pinning, action-relative network waits.

Chrome is never started: ``launch``/``close`` are replaced in the facade module,
and the real ``Bridge`` runs over an in-memory fake transport. No native input
is sent; actions that would reach the interactor are refused (headless) or
replaced on ``Element`` (the conftest safety net traps the real OS entry points).
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from typing import Any, Callable

import pytest

from emunium._bridge.facade import Bridge
from emunium._bridge.transport import PINNED, Reply
from emunium._browser import facade, page
from emunium._browser.facade import Browser
from emunium._standalone.config import ClickType
from emunium.element import Element
from emunium.errors import (
    BridgeProtocolError,
    ContentUnavailableError,
    InvalidArgumentError,
    LaunchError,
    NativeInputError,
    TargetClosedError,
)
from emunium.wait import Wait

ELEMENT = {
    "elementId": "doc1.1",
    "tag": "button",
    "attrs": {},
    "text": "Go",
    "visible": True,
    "rect": {"x": 10, "y": 10, "width": 40, "height": 20},
    "geometry": {},
}


@dataclass
class Call:
    method: str
    params: dict[str, Any]
    tab: int | None
    timeout: float


class FakeTransport:
    """In-memory transport: ``handlers[method]`` is a value or ``fn(params, tab)``."""

    def __init__(self) -> None:
        self.pinned_tab_id: int | None = None
        self.connected = True
        self.token = "tok"
        self.actual_port = 40000
        self.calls: list[Call] = []
        self.handlers: dict[str, Any] = {
            "getTabInfo": lambda params, tab: {"tabId": 1 if tab is None else tab},
            "waitForSelector": ELEMENT,
            "querySelector": ELEMENT,
            "closeTab": {"closed": True},
        }

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        tab_id: Any = PINNED,
        timeout: float = 30.0,
        idempotent: bool = False,
    ) -> Reply:
        tab = self.pinned_tab_id if tab_id is PINNED else tab_id
        self.calls.append(Call(method, dict(params or {}), tab, timeout))
        handler = self.handlers[method]
        result = handler(params, tab) if callable(handler) else copy.deepcopy(handler)
        return Reply(result, tab, 1.0)

    def methods(self) -> list[str]:
        return [call.method for call in self.calls]

    def last(self, method: str) -> Call:
        return [call for call in self.calls if call.method == method][-1]


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch, transport: FakeTransport) -> list[str]:
    log: list[str] = []
    monkeypatch.setattr(facade, "Bridge", lambda port=0: Bridge(transport=transport))
    monkeypatch.setattr(
        facade, "launch", lambda session, bridge_timeout=60.0: log.append("launch")
    )
    monkeypatch.setattr(facade, "close", lambda session: log.append("close"))
    return log


@pytest.fixture
def make(events: list[str]) -> Callable[..., Browser]:
    return Browser


def _raise(exc: BaseException) -> Callable[[Any, Any], Any]:
    def handler(params: Any, tab: Any) -> Any:
        raise exc

    return handler


# -- construction and launch -------------------------------------------------


def test_constructor_builds_one_session_and_context(
    make: Callable[..., Browser],
) -> None:
    browser = make(
        True,
        "profile",
        0,
        5.0,
        chrome_path="chrome.exe",
        extra_args=["--x"],
        verify_input=False,
    )
    session = browser._session
    assert (session.headless, session.user_data_dir) == (True, "profile")
    assert (session.chrome_path, session.extra_args) == ("chrome.exe", ["--x"])
    assert browser._bridge_timeout == 5.0
    ctx = browser.context
    assert ctx.headless is True and ctx.verify_input is False
    assert ctx.bridge is browser.bridge is session.bridge


def test_launch_pins_the_initial_tab(
    make: Callable[..., Browser], transport: FakeTransport, events: list[str]
) -> None:
    transport.handlers["getTabInfo"] = {"tabId": 4, "url": "about:blank"}
    with make() as browser:
        assert browser.bridge.pinned_tab_id == 4
        assert transport.calls[0].method == "getTabInfo"
        assert transport.calls[0].tab is None  # the active tab, not a stale pin
    assert events == ["launch", "close"]


def test_failed_pin_after_launch_closes_the_session(
    make: Callable[..., Browser], transport: FakeTransport, events: list[str]
) -> None:
    transport.handlers["getTabInfo"] = _raise(TargetClosedError("no active tab"))
    with pytest.raises(TargetClosedError), make():
        pytest.fail("the with body must not run")
    assert events == ["launch", "close"]


def test_launch_error_propagates_before_any_command(
    make: Callable[..., Browser],
    transport: FakeTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_launch(session: Any, bridge_timeout: float = 60.0) -> None:
        raise LaunchError("no chrome")

    monkeypatch.setattr(facade, "launch", failing_launch)
    with pytest.raises(LaunchError):
        make().launch()
    assert transport.calls == []


# -- navigation ----------------------------------------------------------------


def test_goto_pins_the_result_tab_without_sleeping(
    make: Callable[..., Browser],
    transport: FakeTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "sleep", _raise(AssertionError("goto slept")))
    transport.handlers["navigate"] = {"url": "https://a.test/", "tabId": 9}
    browser = make()
    before = time.time() * 1000.0
    result = browser.goto("https://a.test/", timeout=5.0, wait_until="domcontentloaded")
    assert result["tabId"] == 9
    assert browser.bridge.pinned_tab_id == 9
    params = transport.last("navigate").params
    assert params["waitUntil"] == "domcontentloaded" and params["timeoutMs"] == 5000
    last_action = browser.context.last_action_ms
    assert last_action is not None and last_action >= before


def test_goto_without_tab_id_is_a_protocol_error(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    transport.handlers["navigate"] = {"url": "https://a.test/"}
    with pytest.raises(BridgeProtocolError):
        make().goto("https://a.test/")


def test_execute_script_returns_the_json_value(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    transport.handlers["executeScript"] = {"value": {"a": [1, 2], "b": None}}
    browser = make()
    assert browser.execute_script("({a: [1, 2], b: null})") == {"a": [1, 2], "b": None}
    assert browser.context.last_action_ms is not None


def test_scroll_to_coordinates_positionally(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    transport.handlers["scrollTo"] = {"scrollX": 10, "scrollY": 20}
    assert make().scroll_to(10, 20) == {"scrollX": 10, "scrollY": 20}
    assert transport.last("scrollTo").params == {"x": 10, "y": 20}


# -- tabs ------------------------------------------------------------------------


def test_new_tab_creates_an_active_tab_and_pins_it(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    transport.handlers["createTab"] = {"tabId": 12, "url": "https://b.test/"}
    browser = make()
    browser.bridge.pinned_tab_id = 3
    browser.new_tab("https://b.test/")
    assert transport.last("createTab").params["active"] is True
    assert browser.bridge.pinned_tab_id == 12


def test_new_tab_without_switch_keeps_the_pin(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    transport.handlers["createTab"] = {"tabId": 12, "url": "about:blank"}
    browser = make()
    browser.bridge.pinned_tab_id = 3
    browser.new_tab(switch=False)
    assert transport.last("createTab").params["active"] is False
    assert browser.bridge.pinned_tab_id == 3


def test_switch_to_tab_activates_and_pins(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    transport.handlers["activateTab"] = lambda params, tab: {
        "tabId": tab,
        "windowId": 1,
        "activated": True,
    }
    transport.handlers["listTabs"] = [{"tabId": 3}, {"tabId": 5}]
    browser = make()
    browser.bridge.pinned_tab_id = 3
    assert browser.switch_to_tab(5)["activated"] is True
    assert transport.last("activateTab").tab == 5
    assert browser.bridge.pinned_tab_id == 5
    assert browser.tabs() == [{"tabId": 3}, {"tabId": 5}]
    with pytest.raises(InvalidArgumentError):
        browser.switch_to_tab(True)


def test_closing_the_pinned_tab_repins_the_active_tab(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    transport.handlers["getTabInfo"] = lambda params, tab: {
        "tabId": 6 if tab is None else tab
    }
    browser = make()
    browser.bridge.pinned_tab_id = 3
    browser.close_tab()
    assert transport.last("closeTab").tab == 3
    assert transport.last("getTabInfo").tab is None
    assert browser.bridge.pinned_tab_id == 6


def test_closing_another_tab_keeps_the_pin(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    browser = make()
    browser.bridge.pinned_tab_id = 3
    browser.close_tab(8)
    assert transport.last("closeTab").tab == 8
    assert "getTabInfo" not in transport.methods()
    assert browser.bridge.pinned_tab_id == 3


def test_closing_the_last_tab_leaves_the_bridge_unpinned(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    transport.handlers["getTabInfo"] = _raise(TargetClosedError("no active tab"))
    browser = make()
    browser.bridge.pinned_tab_id = 3
    assert browser.close_tab() == {"closed": True}
    assert browser.bridge.pinned_tab_id is None


# -- network waits ---------------------------------------------------------------


def test_wait_for_response_defaults_since_to_the_last_action(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    transport.handlers["waitForResponse"] = {
        "url": "https://a.test/api",
        "statusCode": 200,
    }
    transport.handlers["navigate"] = {"tabId": 2}
    browser = make()
    browser.wait_for_response("*api*")
    assert transport.last("waitForResponse").params["since"] is None

    browser.goto("https://a.test/")
    browser.wait_for_response("*api*", 2.0)
    call = transport.last("waitForResponse")
    assert call.params["since"] == browser.context.last_action_ms
    assert call.params["anyTab"] is False and call.tab == 2
    assert call.params["timeoutMs"] == 2000

    browser.wait_for_response("*api*", since=123.0, any_tab=True)
    params = transport.last("waitForResponse").params
    assert params["since"] == 123.0 and params["anyTab"] is True


def test_wait_for_response_returns_none_on_timeout(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    transport.handlers["waitForResponse"] = None
    assert make().wait_for_response("*api*") is None


def test_expect_response_waits_from_block_entry_on_the_entry_tab(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    entry = {"url": "https://a.test/api", "statusCode": 201, "tabId": 3}
    transport.handlers["waitForResponse"] = entry
    transport.handlers["createTab"] = {"tabId": 12}
    browser = make()
    browser.bridge.pinned_tab_id = 3
    before = time.time() * 1000.0
    with browser.expect_response("*api*", 4.0) as expectation:
        entered = browser.context.last_action_ms
        browser.new_tab()  # moves the pin; the wait stays on tab 3
        assert "waitForResponse" not in transport.methods()
    call = transport.last("waitForResponse")
    assert entered is not None and before <= entered
    assert call.params["since"] == entered
    assert call.params["since"] <= (browser.context.last_action_ms or 0.0)
    assert call.tab == 3 and call.params["timeoutMs"] == 4000
    assert expectation.value == entry


def test_expect_response_raises_timeout_when_nothing_matched(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    transport.handlers["waitForResponse"] = None
    browser = make()
    with pytest.raises(TimeoutError), browser.expect_response(
        "*api*", 0.5
    ) as expectation:
        pass
    with pytest.raises(RuntimeError):
        _ = expectation.value


def test_expect_response_skips_the_wait_when_the_block_raises(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    browser = make()
    with pytest.raises(KeyError), browser.expect_response("*api*"):
        raise KeyError("boom")
    assert "waitForResponse" not in transport.methods()


# -- native input: context, headless refusal, timeouts ----------------------------


NATIVE_ACTIONS: list[Callable[[Browser], Any]] = [
    lambda b: b.click("#go"),
    lambda b: b.click_at("#go", ClickType.RIGHT),
    lambda b: b.move_to("#go"),
    lambda b: b.hover("#go"),
    lambda b: b.type("#q", "text"),
    lambda b: b.type_at("#q", "text"),
    lambda b: b.drag_and_drop("#a", "#b"),
]


@pytest.mark.parametrize("action", NATIVE_ACTIONS)
def test_native_actions_refuse_headless_before_any_command(
    make: Callable[..., Browser],
    transport: FakeTransport,
    action: Callable[[Browser], Any],
) -> None:
    with pytest.raises(NativeInputError):
        action(make(headless=True))
    assert transport.calls == []


def test_elements_share_the_browser_context(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    browser = make(headless=True, verify_input=False)
    element = browser.query_selector("#go")
    assert element is not None and element.context is browser.context
    assert element.context.verify_input is False
    with pytest.raises(NativeInputError):
        element.click()  # the refusal reaches the element transaction itself
    assert transport.methods() == ["querySelector"]


def test_click_at_rejects_unknown_click_type_before_waiting(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    unknown: Any = "triple"
    with pytest.raises(InvalidArgumentError):
        make().click_at("#go", unknown)
    assert transport.calls == []


def test_click_marks_the_action_before_clicking(
    make: Callable[..., Browser],
    transport: FakeTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[float | None] = []

    def fake_right_click(self: Element, *, human: bool = True) -> None:
        seen.append(self.context.last_action_ms)

    monkeypatch.setattr(Element, "right_click", fake_right_click)
    browser = make()
    before = time.time() * 1000.0
    element = browser.click_at("#go", ClickType.RIGHT, timeout=3.0)
    assert element.element_id == "doc1.1"
    assert transport.last("waitForSelector").params["timeoutMs"] == 3000
    assert len(seen) == 1 and seen[0] is not None and seen[0] >= before


def test_type_and_drag_accept_timeout(
    make: Callable[..., Browser],
    transport: FakeTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    typed: list[tuple[str, ClickType]] = []
    dragged: list[tuple[str, str]] = []

    def fake_type(self: Element, text: str, **kwargs: Any) -> None:
        typed.append((text, kwargs["click_type"]))

    def fake_drag_to(self: Element, target: Element, *, human: bool = True) -> None:
        dragged.append((self.element_id, target.element_id))

    monkeypatch.setattr(Element, "type", fake_type)
    monkeypatch.setattr(Element, "drag_to", fake_drag_to)
    browser = make()
    browser.type("#q", "hello", timeout=2.5)
    assert transport.last("waitForSelector").params["timeoutMs"] == 2500
    browser.type_at("#q", "x", click_type=ClickType.DOUBLE, timeout=1.5)
    assert transport.last("waitForSelector").params["timeoutMs"] == 1500
    browser.drag_and_drop("#a", "#b", timeout=4.0)
    waits = [c for c in transport.calls if c.method == "waitForSelector"][-2:]
    assert [c.params["selector"] for c in waits] == ["#a", "#b"]
    assert all(c.params["timeoutMs"] == 4000 for c in waits)
    assert typed == [("hello", ClickType.LEFT), ("x", ClickType.DOUBLE)]
    assert dragged == [("doc1.1", "doc1.1")]


# -- wait_for_idle -----------------------------------------------------------------


def test_wait_for_idle_retries_only_transient_errors(
    make: Callable[..., Browser],
    transport: FakeTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(page, "_sleep", lambda seconds: None)
    replies: list[Any] = [
        ContentUnavailableError("swapping"),
        {"readyState": "complete"},
    ]

    def page_info(params: Any, tab: Any) -> Any:
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, BaseException):
            raise reply
        return reply

    transport.handlers["pageInfo"] = page_info
    assert make().wait_for_idle(silence=0.0, timeout=5.0) is True


def test_wait_for_idle_propagates_other_errors(
    make: Callable[..., Browser],
    transport: FakeTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(page, "_sleep", lambda seconds: None)
    transport.handlers["pageInfo"] = _raise(TargetClosedError("tab closed"))
    with pytest.raises(TargetClosedError):
        make().wait_for_idle(silence=0.0, timeout=5.0)


# -- Wait builder ----------------------------------------------------------------


def test_wait_payload_is_a_deep_copy() -> None:
    inner = Wait().visible()
    wait = Wait().has_attribute("id", "x").any_of(inner)
    payload = wait.to_payload()
    payload[0]["name"] = "changed"
    payload[1]["conditions"][0].append({"type": "hidden"})
    assert wait.to_payload() == [
        {"type": "has_attribute", "name": "id", "value": "x"},
        {"type": "any_of", "conditions": [[{"type": "visible"}]]},
    ]
    inner.hidden()
    assert wait.to_payload()[1]["conditions"] == [[{"type": "visible"}]]


@pytest.mark.parametrize("bad", [-1, True, 1.5, "2", None])
def test_wait_count_rejects_non_negative_int_violations(bad: Any) -> None:
    with pytest.raises(InvalidArgumentError):
        Wait().count_gt(bad)
    with pytest.raises(InvalidArgumentError):
        Wait().count_eq(bad)


def test_wait_count_accepts_non_negative_ints() -> None:
    assert Wait().count_gt(0).count_eq(3).to_payload() == [
        {"type": "count_gt", "value": 0},
        {"type": "count_eq", "value": 3},
    ]
