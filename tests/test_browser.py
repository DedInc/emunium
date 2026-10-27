"""Browser facade contract: one context, tab pinning, action-relative network waits.

Chrome is never started: ``launch``/``close`` are replaced in the facade module,
and the real ``Bridge`` runs over an in-memory fake transport. No native input
is sent; actions that would reach the interactor are refused (headless) or
replaced on ``Element`` (the conftest safety net traps the real OS entry points).
"""

from __future__ import annotations

import copy
import json
import pickle
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from emunium._bridge.facade import Bridge
from emunium._bridge.transport import PINNED, Reply
from emunium._browser import facade, page
from emunium._browser.facade import Browser
from emunium._browser.tabs import tab_id_argument
from emunium._standalone.config import ClickType
from emunium.element import Element, ElementQuery
from emunium.errors import (
    BridgeProtocolError,
    BridgeTimeoutError,
    ContentUnavailableError,
    ElementNotFoundError,
    InvalidArgumentError,
    LaunchError,
    NativeInputError,
    NavigationTimeoutError,
    TargetClosedError,
    WaitTimeoutError,
)
from emunium.results import NetworkResponse, PageInfo, Point, Record, TabInfo
from emunium.wait import Wait, WaitStrategy

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


# -- reprs without I/O (dx-api.md section 5) --------------------------------------


class NoRequestTransport:
    """A transport whose every request fails: reprs must use cached state only."""

    actual_port = 51234
    pinned_tab_id = None

    def request(self, *args: Any, **kwargs: Any) -> Reply:
        raise AssertionError(f"repr sent a request: {args!r}")


def test_reprs_send_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = NoRequestTransport()
    monkeypatch.setattr(facade, "Bridge", lambda port=0: Bridge(transport=transport))
    browser = Browser()

    assert repr(browser) == "Browser(headless=False, launched=False, port=51234)"
    assert repr(Browser(True)) == "Browser(headless=True, launched=False, port=51234)"
    assert repr(browser.expect_response("**/api/*")) == (
        "ResponseExpectation('**/api/*', timeout=10.0)"
    )
    assert repr(browser.expect_response("*/x", 2.5, any_tab=True)) == (
        "ResponseExpectation('*/x', timeout=2.5, any_tab=True)"
    )
    assert repr(browser.expect_navigation("*/next", timeout=5.0)) == (
        "NavigationExpectation(url='*/next', timeout=5.0, wait_until='load')"
    )


def test_browser_repr_follows_launch_and_close(
    make: Callable[..., Browser],
    transport: FakeTransport,
    events: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_launch(session: Any, bridge_timeout: float = 60.0) -> None:
        # pid None: no foreground-window handling for this stand-in process.
        session.process = SimpleNamespace(pid=None)

    def fake_close(session: Any) -> None:
        session.process = None

    monkeypatch.setattr(facade, "launch", fake_launch)
    monkeypatch.setattr(facade, "close", fake_close)
    browser = make().launch()
    sent = len(transport.calls)

    assert repr(browser) == "Browser(headless=False, launched=True, port=40000)"
    browser.close()
    assert repr(browser) == "Browser(headless=False, launched=False, port=40000)"
    assert len(transport.calls) == sent


def test_element_repr_adds_its_query(make: Callable[..., Browser]) -> None:
    ctx = make().context
    rect = {"x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0}
    plain = Element(ctx, "doc1.1", "button", rect=rect, tab_id=3)
    css = Element(ctx, "doc1.2", "a", selector="#go", tab_id=3)
    xpath = Element(ctx, "doc1.3", "a", query=ElementQuery("//a", 2, "xpath"))

    assert repr(plain) == (
        "Element(id='doc1.1', tab=3, tag='button', "
        "rect={'x': 1.0, 'y': 2.0, 'width': 3.0, 'height': 4.0})"
    )
    assert repr(css) == "Element(id='doc1.2', tab=3, tag='a', rect={}, selector='#go')"
    assert repr(xpath) == (
        "Element(id='doc1.3', tab=None, tag='a', rect={}, "
        "selector='//a', kind='xpath', index=2)"
    )


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


@pytest.mark.parametrize("value", ["5", 5.0, None, True])
def test_tab_id_argument_rejects_non_ints(value: object) -> None:
    # switch_to_tab validates its tab id through tab_id_argument.
    with pytest.raises(InvalidArgumentError, match="tab_id must be an int"):
        tab_id_argument(value)
    assert tab_id_argument(5) == 5


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
    with (
        pytest.raises(TimeoutError) as info,
        browser.expect_response("*api*", 0.5) as expectation,
    ):
        pass
    assert isinstance(info.value, WaitTimeoutError)
    assert (info.value.code, info.value.method) == ("wait_timeout", "waitForResponse")
    assert str(info.value) == "no response matching '*api*' within 0.5s"
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


# -- result records and error context (dx-api.md sections 1 and 2) ----------------


def _assert_plain_compatible(record: dict[str, Any], plain: Any) -> None:
    """Every way old code read the plain dict still works on the record."""
    assert isinstance(record, dict)
    assert record == plain
    assert dict(record) == plain
    assert json.loads(json.dumps(record)) == plain
    assert pickle.loads(pickle.dumps(record)) == plain
    assert type(pickle.loads(pickle.dumps(record))) is type(record)


def test_goto_and_page_info_return_page_info(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    navigated = {
        "url": "https://a.test/",
        "title": "A",
        "tabId": 9,
        "contentReady": True,
    }
    info = {"url": "https://a.test/", "title": "A", "readyState": "complete"}
    transport.handlers["navigate"] = navigated
    transport.handlers["pageInfo"] = info
    browser = make()

    result = browser.goto("https://a.test/")
    assert type(result) is PageInfo
    _assert_plain_compatible(result, navigated)
    assert result["tabId"] == 9 and result.tab_id == 9 and result.content_ready
    assert result.url == "https://a.test/" and result.title == "A"

    page_state = browser.page_info()
    assert type(page_state) is PageInfo
    _assert_plain_compatible(page_state, info)
    assert page_state["readyState"] == page_state.ready_state == "complete"


def test_tab_descriptions_are_tab_info_and_replies_are_records(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    # Reply shapes of background_handlers.js: createTab, activateTab and
    # closeTab are command replies, not tab descriptions.
    created_reply = {
        "tabId": 12,
        "url": "about:blank",
        "title": "",
        "contentReady": True,
    }
    switched_reply = {"tabId": 5, "windowId": 1, "activated": True}
    listed = [{"tabId": 3, "active": False}, {"tabId": 5, "active": True}]
    transport.handlers["createTab"] = created_reply
    transport.handlers["activateTab"] = switched_reply
    transport.handlers["listTabs"] = listed
    transport.handlers["getTabInfo"] = {"tabId": 5, "status": "loading"}
    browser = make()

    created = browser.new_tab()
    assert type(created) is Record
    _assert_plain_compatible(created, created_reply)
    assert created.tab_id == 12 and created.content_ready is True
    switched = browser.switch_to_tab(5)
    assert type(switched) is Record
    _assert_plain_compatible(switched, switched_reply)
    assert switched["activated"] is True and switched.activated is True
    assert (switched.tab_id, switched.window_id) == (5, 1)
    tabs = browser.tabs()
    assert tabs == listed and all(type(tab) is TabInfo for tab in tabs)
    assert [tab.tab_id for tab in tabs] == [3, 5] and tabs[1].active is True
    assert tabs[0].status is None
    info = browser.tab_info()
    assert type(info) is TabInfo and info.status == "loading"
    closed = browser.close_tab(8)
    assert type(closed) is Record
    _assert_plain_compatible(closed, {"closed": True})
    assert closed.closed is True and closed["closed"] is True


def test_records_print_like_the_plain_replies(
    make: Callable[..., Browser],
    transport: FakeTransport,
    capsys: pytest.CaptureFixture[str],
) -> None:
    info = {"url": "https://a.test/", "title": "A", "readyState": "complete"}
    transport.handlers["pageInfo"] = info
    browser = make()
    print(browser.page_info())
    print(browser.close_tab(8))
    assert capsys.readouterr().out == f"{info!r}\n{{'closed': True}}\n"
    assert repr(browser.page_info()) == repr(info) == str(browser.page_info())


def test_network_waits_return_network_response(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    entry = {
        "url": "https://a.test/api",
        "statusCode": 204,
        "method": "GET",
        "type": "xmlhttprequest",
        "timeStamp": 1700000000000.0,
        "tabId": 3,
        "requestId": "9",
    }
    transport.handlers["waitForResponse"] = entry
    browser = make()
    browser.bridge.pinned_tab_id = 3

    resp = browser.wait_for_response("*api*")
    assert type(resp) is NetworkResponse
    _assert_plain_compatible(resp, entry)
    assert resp.status == 204 and resp.ok and resp["statusCode"] == 204

    with browser.expect_response("*api*") as expectation:
        pass
    assert type(expectation.value) is NetworkResponse
    assert expectation.value == entry and expectation.value.request_id == "9"


def test_get_center_returns_a_point(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    placed = dict(ELEMENT, absoluteScreenX=140, absoluteScreenY=260)
    transport.handlers["waitForSelector"] = placed
    transport.handlers["scrollIntoView"] = placed
    browser = make()
    browser.bridge.pinned_tab_id = 3

    center = browser.get_center("#go")
    assert type(center) is Point
    assert center == {"x": center.x, "y": center.y}
    assert center["x"] == center.x and isinstance(center.x, int)
    _assert_plain_compatible(center, {"x": center.x, "y": center.y})


def test_element_less_match_raises_element_not_found(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    transport.handlers["waitForSelector"] = {"element": None, "matched": True}
    with pytest.raises(LookupError) as info:
        make().get_center("#gone")
    assert isinstance(info.value, ElementNotFoundError)
    assert str(info.value) == "'#gone' matched without an element to act on"


def test_element_wait_timeout_names_the_page_and_hints(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    transport.handlers["waitForSelector"] = _raise(
        WaitTimeoutError(
            'waitForSelector timed out after 1000 ms waiting for css "#go" '
            '(url: https://a.test/form, title: "Form")',
            method="waitForSelector",
        )
    )
    transport.handlers["querySelectorAll"] = [ELEMENT, ELEMENT, ELEMENT]
    browser = make()
    browser.bridge.pinned_tab_id = 3

    with pytest.raises(TimeoutError) as info:
        browser.wait("#go", condition=Wait().clickable(), timeout=1.0)
    error = info.value
    assert isinstance(error, WaitTimeoutError)
    assert isinstance(error.__cause__, WaitTimeoutError)
    assert error.message == (
        "Element not found after 1.0s: '#go' (url=https://a.test/form, title='Form')"
    )
    assert error.hint == "3 element(s) match but none satisfied conditions clickable"
    assert str(error).endswith("\nhint: " + error.hint)
    probe = transport.last("querySelectorAll")
    assert probe.params == {"selector": "#go"} and probe.timeout <= 3.0
    assert "pageInfo" not in transport.methods()  # the message already had it

    # Every _resolve_element path raises the same error.
    with pytest.raises(WaitTimeoutError):
        browser.get_center("#go", timeout=1.0)
    assert browser.wait("#go", timeout=1.0, raise_on_timeout=False) is None


def test_element_wait_timeout_falls_back_to_one_page_info_probe(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    transport.handlers["waitForSelector"] = _raise(WaitTimeoutError("late"))
    transport.handlers["pageInfo"] = {"url": "https://a.test/", "title": "A"}
    transport.handlers["queryXPath"] = []
    browser = make()

    with pytest.raises(WaitTimeoutError) as info:
        browser.wait_for_xpath("//button", timeout=0.5)
    assert info.value.message == (
        "XPath not found after 0.5s: '//button' (url=https://a.test/, title='A')"
    )
    assert info.value.hint == "no element matches '//button' on this page"
    assert transport.methods().count("pageInfo") == 1
    assert transport.last("pageInfo").timeout <= 3.0
    assert transport.last("queryXPath").timeout <= 3.0


def test_headless_refusal_carries_a_hint(make: Callable[..., Browser]) -> None:
    with pytest.raises(NativeInputError) as info:
        make(headless=True).click("#go")
    assert info.value.hint is not None and "headless=False" in info.value.hint
    assert "\nhint: launch with headless=False" in str(info.value)


# -- xpath/text strategy and condition, URL and navigation waits (section 3) ------


def _sent(call: Call) -> dict[str, Any]:
    return {key: call.params.get(key) for key in ("type", "state", "conditions")}


def test_xpath_and_text_waits_send_strategy_and_condition(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    browser = make()
    either = Wait().visible() | Wait().text_contains("ok")

    assert browser.wait_for_xpath("//a", 2.0) is not None
    assert _sent(transport.last("waitForSelector")) == {
        "type": "xpath",
        "state": None,
        "conditions": None,
    }
    browser.wait_for_xpath("//a", strategy=WaitStrategy.CLICKABLE, condition=either)
    assert _sent(transport.last("waitForSelector")) == {
        "type": "xpath",
        "state": "clickable",
        "conditions": either.to_payload(),
    }
    browser.wait_for_text("Go", 3.0, condition=[{"type": "visible"}])
    sent = transport.last("waitForSelector")
    assert _sent(sent) == {
        "type": "text",
        "state": None,
        "conditions": [{"type": "visible"}],
    }
    browser.wait_for_text("Go", strategy=WaitStrategy.STABLE, condition=~either)
    assert _sent(transport.last("waitForSelector")) == {
        "type": "text",
        "state": "stable",
        "conditions": [{"type": "not", "condition": either.to_payload()}],
    }
    # The same mapping as Browser.wait, which the operators feed too.
    browser.wait("#go", WaitStrategy.VISIBLE, either & Wait().clickable())
    assert transport.last("waitForSelector").params["conditions"] == [
        {
            "type": "all_of",
            "conditions": [either.to_payload(), [{"type": "clickable"}]],
        }
    ]


def test_xpath_timeout_hint_names_the_requested_state(
    make: Callable[..., Browser], transport: FakeTransport
) -> None:
    transport.handlers["waitForSelector"] = _raise(
        WaitTimeoutError('t (url: https://a.test/, title: "A")')
    )
    transport.handlers["queryXPath"] = [ELEMENT]
    with pytest.raises(WaitTimeoutError) as info:
        make().wait_for_xpath("//a", 1.0, strategy=WaitStrategy.VISIBLE)
    assert info.value.hint == "1 element(s) match but none satisfied state 'visible'"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """page's monotonic clock; page._sleep advances it instead of sleeping."""
    now = [1000.0]

    def sleep(seconds: float) -> None:
        # Rounded: ten 0.1 s polls end exactly on a 1 s deadline.
        now[0] = round(now[0] + seconds, 9)

    monkeypatch.setattr(page, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(page, "_sleep", sleep)
    return now


PageStep = str | tuple[str, str] | BaseException


class FakeTab:
    """Scripted getTabInfo/pageInfo replies of one tab (the last entry stays).

    Each getTabInfo call reports the next of ``urls`` and ``statuses``; each
    pageInfo call reports the next of ``states``: a readyState of the document
    at the current tab URL, a ``(readyState, url)`` pair for a document at
    another URL, or an exception to raise.
    """

    def __init__(
        self,
        transport: FakeTransport,
        urls: list[str],
        states: list[PageStep] | None = None,
        statuses: list[str] | None = None,
    ) -> None:
        self.urls = list(urls)
        self.states: list[PageStep] = list(states or ["complete"])
        self.statuses = list(statuses or ["complete"])
        self.url = self.urls[0]
        transport.handlers["getTabInfo"] = self.tab_info
        transport.handlers["pageInfo"] = self.page_info

    @staticmethod
    def _next(steps: list[Any]) -> Any:
        return steps.pop(0) if len(steps) > 1 else steps[0]

    def tab_info(self, params: Any, tab: Any) -> dict[str, Any]:
        self.url = self._next(self.urls)
        status = self._next(self.statuses)
        return {"tabId": tab, "url": self.url, "title": "T", "status": status}

    def page_info(self, params: Any, tab: Any) -> dict[str, Any]:
        step = self._next(self.states)
        if isinstance(step, BaseException):
            raise step
        state, url = step if isinstance(step, tuple) else (step, self.url)
        return {"url": url, "title": "T", "readyState": state}


def _timeouts(transport: FakeTransport, method: str) -> list[float]:
    return [call.timeout for call in transport.calls if call.method == method]


def test_wait_for_url_glob_polls_the_pinned_tab_then_waits_for_load(
    make: Callable[..., Browser], transport: FakeTransport, clock: list[float]
) -> None:
    FakeTab(
        transport,
        ["https://a.test/", "https://a.test/", "https://a.test/next?id=1"],
        [
            ContentUnavailableError("swapping"),
            ("complete", "https://a.test/"),  # the old document still answers
            "loading",
            "complete",
        ],
        statuses=["loading"],
    )
    browser = make()
    browser.bridge.pinned_tab_id = 4

    assert browser.wait_for_url("*/next?id=1") == "https://a.test/next?id=1"
    assert {call.tab for call in transport.calls} == {4}
    assert transport.methods().count("pageInfo") == 4
    # Every call is capped at the time left of the 30 s deadline.
    assert _timeouts(transport, "getTabInfo") == pytest.approx(
        [30.0, 29.9, 29.8, 29.7, 29.6, 29.5]
    )
    assert _timeouts(transport, "pageInfo") == pytest.approx([29.8, 29.7, 29.6, 29.5])
    assert clock[0] == pytest.approx(1000.5)


@pytest.mark.parametrize(
    ("wait_until", "page_info_calls"),
    [("commit", 0), ("domcontentloaded", 1), ("load", 2)],
)
def test_wait_for_url_wait_until_selects_the_ready_state(
    make: Callable[..., Browser],
    transport: FakeTransport,
    clock: list[float],
    wait_until: str,
    page_info_calls: int,
) -> None:
    FakeTab(transport, ["https://a.test/done"], ["interactive", "complete"])
    found = make().wait_for_url(re.compile(r"/done$"), wait_until=wait_until)
    assert found == "https://a.test/done"
    assert transport.methods().count("pageInfo") == page_info_calls


def test_wait_for_url_regex_searches_and_predicate_receives_each_url(
    make: Callable[..., Browser], transport: FakeTransport, clock: list[float]
) -> None:
    FakeTab(transport, ["https://a.test/", "https://b.test/x"])
    browser = make()
    assert browser.wait_for_url(re.compile("b\\.test")) == "https://b.test/x"

    seen: list[str] = []

    def is_b(url: str) -> bool:
        seen.append(url)
        return url.startswith("https://b.")

    assert browser.wait_for_url(is_b, wait_until="commit") == "https://b.test/x"
    assert seen == ["https://b.test/x"]


def test_wait_for_url_timeout_names_the_last_url_and_the_state(
    make: Callable[..., Browser], transport: FakeTransport, clock: list[float]
) -> None:
    FakeTab(transport, ["https://a.test/1", "https://a.test/2"], ["loading"])
    browser = make()
    with pytest.raises(TimeoutError) as info:
        browser.wait_for_url("*/never", timeout=1.0)
    error = info.value
    assert isinstance(error, NavigationTimeoutError)
    assert error.code == "navigation_timeout"
    assert error.message == (
        "URL did not match '*/never' within 1.0s (last url: https://a.test/2)"
    )
    assert clock[0] == pytest.approx(1001.0)
    assert "pageInfo" not in transport.methods()
    assert max(_timeouts(transport, "getTabInfo")) == pytest.approx(1.0)
    assert min(_timeouts(transport, "getTabInfo")) == pytest.approx(0.1)

    with pytest.raises(NavigationTimeoutError) as info:
        browser.wait_for_url("*/2", timeout=0.5)
    assert info.value.message == (
        "URL did match '*/2' but readyState never reached 'load' within 0.5s "
        "(last state: 'loading', last url: https://a.test/2)"
    )


def test_wait_for_url_retries_bridge_timeouts_until_its_own_deadline(
    make: Callable[..., Browser], transport: FakeTransport, clock: list[float]
) -> None:
    tab = FakeTab(transport, ["https://a.test/done"])
    pending = [BridgeTimeoutError("no reply to pageInfo")]

    def page_info(params: Any, tab_id: Any) -> dict[str, Any]:
        clock[0] += 0.3
        if pending:
            raise pending.pop()
        return tab.page_info(params, tab_id)

    transport.handlers["pageInfo"] = page_info
    browser = make()
    assert browser.wait_for_url("*/done", timeout=5.0) == "https://a.test/done"
    assert transport.methods().count("pageInfo") == 2

    def blocked(params: Any, tab_id: Any) -> dict[str, Any]:
        # The call uses up all the time it was given, then times out.
        clock[0] += transport.calls[-1].timeout
        raise BridgeTimeoutError("no reply to getTabInfo")

    transport.handlers["getTabInfo"] = blocked
    transport.calls.clear()
    with pytest.raises(NavigationTimeoutError) as info:
        browser.wait_for_url("*/done", timeout=2.0)
    assert info.value.message == (
        "URL did not match '*/done' within 2.0s (last url: None)"
    )
    assert _timeouts(transport, "getTabInfo") == [2.0]


def test_wait_for_url_falls_back_to_tab_status_without_page_info(
    make: Callable[..., Browser], transport: FakeTransport, clock: list[float]
) -> None:
    # A PDF viewer or chrome-error page never gets a content script.
    unavailable = ContentUnavailableError("no content script")
    pdf = "https://a.test/doc.pdf"
    FakeTab(
        transport,
        ["https://a.test/", pdf],
        [unavailable],
        statuses=["complete", "loading", "complete"],
    )
    browser = make()
    assert browser.wait_for_url("*.pdf") == pdf
    assert transport.methods().count("getTabInfo") == 3
    assert transport.methods().count("pageInfo") == 2

    FakeTab(transport, [pdf], [unavailable], statuses=["loading"])
    with pytest.raises(NavigationTimeoutError) as info:
        browser.wait_for_url("*.pdf", timeout=0.5, wait_until="domcontentloaded")
    assert info.value.message == (
        "URL did match '*.pdf' but readyState never reached 'domcontentloaded' "
        "within 0.5s (last state: pageInfo unavailable, tab status 'loading', "
        f"last url: {pdf})"
    )

    FakeTab(transport, ["https://a.test/", pdf], [unavailable])
    with browser.expect_navigation() as navigation:
        pass
    assert type(navigation.value) is PageInfo
    assert navigation.value == {"url": pdf, "title": "T"}


@pytest.mark.parametrize("method", ["getTabInfo", "pageInfo"])
def test_wait_for_url_propagates_errors_that_are_not_transient(
    make: Callable[..., Browser],
    transport: FakeTransport,
    clock: list[float],
    method: str,
) -> None:
    FakeTab(transport, ["https://a.test/"])
    transport.handlers[method] = _raise(TargetClosedError("gone"))
    with pytest.raises(TargetClosedError):
        make().wait_for_url("*")
    assert transport.methods().count(method) == 1


@pytest.mark.parametrize(
    ("url", "wait_until"),
    [("*", "networkidle"), ("*", "complete"), ("", "load"), (b"*", "load")],
)
def test_url_waits_validate_arguments_before_any_call(
    make: Callable[..., Browser], transport: FakeTransport, url: Any, wait_until: str
) -> None:
    browser = make()
    with pytest.raises(InvalidArgumentError):
        browser.wait_for_url(url, wait_until=wait_until)
    with pytest.raises(InvalidArgumentError):
        browser.expect_navigation(url, wait_until=wait_until)
    assert transport.calls == []


def test_expect_navigation_waits_for_a_different_url_in_the_entry_tab(
    make: Callable[..., Browser], transport: FakeTransport, clock: list[float]
) -> None:
    FakeTab(
        transport,
        ["https://a.test/", "https://a.test/", "https://a.test/next"],
        [("loading", "https://a.test/"), "complete"],
    )
    browser = make()
    browser.bridge.pinned_tab_id = 7

    expectation = browser.expect_navigation()
    assert repr(expectation) == (
        "NavigationExpectation(url=None, timeout=30.0, wait_until='load')"
    )
    assert transport.calls == []  # neither construction nor repr does I/O
    with pytest.raises(RuntimeError):
        _ = expectation.value
    with expectation as navigation:
        assert transport.methods() == ["getTabInfo"]
        browser.bridge.pinned_tab_id = 8  # an action in the block moves the pin
        with pytest.raises(RuntimeError):
            _ = navigation.value
    assert type(navigation.value) is PageInfo
    assert navigation.value == {
        "url": "https://a.test/next",
        "title": "T",
        "readyState": "complete",
    }
    assert {call.tab for call in transport.calls} == {7}
    # Entry, the old URL, then the new URL twice: its first pageInfo reply
    # came from the old document.
    assert transport.methods().count("getTabInfo") == 4


def test_expect_navigation_with_url_needs_a_change_and_a_match(
    make: Callable[..., Browser], transport: FakeTransport, clock: list[float]
) -> None:
    first, second = "https://a.test/a.html", "https://a.test/b.html"
    # The start URL already matches the glob: it is not the navigation.
    FakeTab(transport, [first, first, first, second])
    browser = make()
    with browser.expect_navigation("*/*.html") as navigation:
        pass
    assert navigation.value.url == second
    assert transport.methods().count("getTabInfo") == 4

    FakeTab(transport, [first])
    with (
        pytest.raises(NavigationTimeoutError) as info,
        browser.expect_navigation("*/*.html", timeout=1.0),
    ):
        pass
    assert info.value.message == (
        f"URL did not change from {first!r} and match '*/*.html' within 1.0s "
        f"(last url: {first})"
    )

    FakeTab(
        transport,
        ["https://a.test/", "https://a.test/redirecting", second],
        ["complete"],
    )
    with browser.expect_navigation("*/b.html", timeout=5.0, wait_until="commit") as nav:
        pass
    assert nav.value.ready_state == "complete" and nav.value.url == second
    assert repr(nav) == (
        "NavigationExpectation(url='*/b.html', timeout=5.0, wait_until='commit')"
    )


def test_expect_navigation_timeout_and_errors_in_the_block(
    make: Callable[..., Browser], transport: FakeTransport, clock: list[float]
) -> None:
    FakeTab(transport, ["https://a.test/"])
    browser = make()
    with (
        pytest.raises(NavigationTimeoutError) as info,
        browser.expect_navigation(timeout=1.0),
    ):
        pass
    assert info.value.message == (
        "URL did not change from 'https://a.test/' within 1.0s "
        "(last url: https://a.test/)"
    )

    transport.calls.clear()
    navigation = browser.expect_navigation()
    with pytest.raises(KeyError), navigation:
        raise KeyError("in the block")
    assert transport.methods() == ["getTabInfo"]  # only the entry read
    with pytest.raises(RuntimeError):
        _ = navigation.value
