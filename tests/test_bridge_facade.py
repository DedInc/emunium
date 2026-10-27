"""Bridge facade tests (python-internals.md "Bridge facade"), driven by FakeExtension."""

from __future__ import annotations

import threading
import time

import pytest
from fake_extension import FakeExtension

from emunium import errors
from emunium._bridge.facade import Bridge
from emunium._bridge.transport import PINNED, Reply

TOKEN = "facade-token-0123456789"

ELEMENT = {
    "elementId": "doc1.1",
    "tag": "button",
    "attrs": {},
    "text": "Go",
    "value": None,
    "visible": True,
    "rect": {"x": 1, "y": 2, "width": 30, "height": 10},
    "geometry": {},
    "absoluteScreenX": 0,
    "absoluteScreenY": 0,
}


class Responder:
    """Mutable handler: ``bodies[method]`` is a reply body or a callable(request)."""

    def __init__(self):
        self.bodies = {}

    def __call__(self, request):
        body = self.bodies.get(request["method"])
        if callable(body):
            return body(request)
        return body


@pytest.fixture
def bridge():
    b = Bridge(token=TOKEN)
    b.start(timeout=5)
    try:
        yield b
    finally:
        b.shutdown()


@pytest.fixture
def responder():
    """The ``ext`` fixture's handler; tests set its ``bodies``."""
    return Responder()


@pytest.fixture
def ext(bridge, responder):
    with FakeExtension(bridge.actual_port, TOKEN, handler=responder) as fake:
        assert fake.wait_welcomed(5)
        assert bridge.wait_for_connection(5)
        yield fake


def _last(fake):
    assert fake.wait_requests(1)
    return fake.requests[-1]


# -- error mapping -----------------------------------------------------------

ERROR_CASES = [
    (
        lambda b: b.query_selector("#x"),
        "querySelector",
        "stale_element",
        errors.StaleElementError,
    ),
    (
        lambda b: b.query_selector_all("a"),
        "querySelectorAll",
        "content_unavailable",
        errors.ContentUnavailableError,
    ),
    (
        lambda b: b.get_all_interactive(),
        "getAllInteractive",
        "no_target",
        errors.TargetClosedError,
    ),
    (
        lambda b: b.get_element_by_text("Go"),
        "queryByText",
        "internal",
        errors.CommandError,
    ),
    (
        lambda b: b.query_xpath("//a"),
        "queryXPath",
        "invalid_argument",
        errors.InvalidArgumentError,
    ),
    (
        lambda b: b.describe_element("doc1.1"),
        "describeElement",
        "stale_element",
        errors.StaleElementError,
    ),
    (
        lambda b: b.element_text("doc1.1"),
        "elementText",
        "stale_element",
        errors.StaleElementError,
    ),
    (
        lambda b: b.scroll_into_view("doc1.1"),
        "scrollIntoView",
        "stale_element",
        errors.StaleElementError,
    ),
    (
        lambda b: b.get_element_coords("doc1.1"),
        "scrollIntoView",
        "target_closed",
        errors.TargetClosedError,
    ),
    (
        lambda b: b.wait_for_selector("#x", timeout=0.1),
        "waitForSelector",
        "wait_timeout",
        errors.WaitTimeoutError,
    ),
    (
        lambda b: b.wait_for_selector("#x", conditions=[{"type": "bogus"}]),
        "waitForSelector",
        "unknown_condition",
        errors.InvalidArgumentError,
    ),
    (lambda b: b.focus("doc1.1"), "focus", "stale_element", errors.StaleElementError),
    (
        lambda b: b.focus_state("doc1.1"),
        "focusState",
        "stale_element",
        errors.StaleElementError,
    ),
    (
        lambda b: b.pointer_probe("doc1.1", 1.0),
        "pointerProbe",
        "invalid_argument",
        errors.InvalidArgumentError,
    ),
    (
        lambda b: b.get_attribute("doc1.1", "id"),
        "getAttribute",
        "stale_element",
        errors.StaleElementError,
    ),
    (
        lambda b: b.get_computed_style("doc1.1", "color"),
        "getComputedStyle",
        "stale_element",
        errors.StaleElementError,
    ),
    (
        lambda b: b.navigate("https://a.test"),
        "navigate",
        "navigation_timeout",
        errors.NavigationTimeoutError,
    ),
    (
        lambda b: b.navigate("https://a.test"),
        "navigate",
        "navigation_failed",
        errors.NavigationError,
    ),
    (
        lambda b: b.page_info(),
        "pageInfo",
        "content_unavailable",
        errors.ContentUnavailableError,
    ),
    (
        lambda b: b.scroll_to(0, 10),
        "scrollTo",
        "invalid_argument",
        errors.InvalidArgumentError,
    ),
    (
        lambda b: b.execute_script("1"),
        "executeScript",
        "script_error",
        errors.ScriptError,
    ),
    (
        lambda b: b.execute_script("1"),
        "executeScript",
        "csp_blocked",
        errors.CSPBlockedError,
    ),
    (lambda b: b.get_tab_info(), "getTabInfo", "no_target", errors.TargetClosedError),
    (lambda b: b.list_tabs(), "listTabs", "internal", errors.CommandError),
    (
        lambda b: b.create_tab("https://a.test"),
        "createTab",
        "navigation_failed",
        errors.NavigationError,
    ),
    (lambda b: b.close_tab(4), "closeTab", "target_closed", errors.TargetClosedError),
    (
        lambda b: b.activate_tab(),
        "activateTab",
        "target_closed",
        errors.TargetClosedError,
    ),
    (
        lambda b: b.wait_for_response("*/api"),
        "waitForResponse",
        "timeout",
        errors.CommandTimeoutError,
    ),
    (
        lambda b: b.get_recent_responses(),
        "getRecentResponses",
        "internal",
        errors.CommandError,
    ),
    (
        lambda b: b.send("noSuchMethod"),
        "noSuchMethod",
        "unknown_method",
        errors.BridgeProtocolError,
    ),
]


@pytest.mark.parametrize(
    "call,method,code,exc_type",
    ERROR_CASES,
    ids=[f"{case[1]}-{case[2]}" for case in ERROR_CASES],
)
def test_error_reply_raises_typed_exception(
    bridge, ext, responder, call, method, code, exc_type
):
    responder.bodies[method] = {
        "ok": False,
        "error": {"code": code, "message": "boom"},
    }
    with pytest.raises(exc_type) as info:
        call(bridge)
    assert info.value.code == code
    assert info.value.method == method
    assert info.value.message == "boom"
    assert _last(ext)["method"] == method


def test_malformed_result_shape_raises_protocol_error(bridge, ext, responder):
    responder.bodies["querySelectorAll"] = {"result": {"not": "a list"}}
    with pytest.raises(errors.BridgeProtocolError):
        bridge.query_selector_all("a")
    responder.bodies["executeScript"] = {"result": "raw"}
    with pytest.raises(errors.BridgeProtocolError):
        bridge.execute_script("1")


# -- element enrichment ------------------------------------------------------


@pytest.mark.parametrize(
    "call,method,result",
    [
        (lambda b: [b.query_selector("#go")], "querySelector", ELEMENT),
        (
            lambda b: b.query_selector_all("button"),
            "querySelectorAll",
            [ELEMENT, ELEMENT],
        ),
        (lambda b: b.get_all_interactive(), "getAllInteractive", [ELEMENT]),
        (lambda b: b.get_element_by_text("Go"), "queryByText", [ELEMENT]),
        (lambda b: b.query_xpath("//button"), "queryXPath", [ELEMENT]),
        (lambda b: [b.describe_element("doc1.1")], "describeElement", ELEMENT),
        (lambda b: [b.scroll_into_view("doc1.1")], "scrollIntoView", ELEMENT),
        (lambda b: [b.get_element_coords("doc1.1")], "scrollIntoView", ELEMENT),
        (lambda b: [b.wait_for_selector("#go")], "waitForSelector", ELEMENT),
    ],
    ids=[
        "qs",
        "qsa",
        "interactive",
        "text",
        "xpath",
        "describe",
        "scroll",
        "coords",
        "wait",
    ],
)
def test_element_dicts_carry_tab_id_and_zoom(
    bridge, ext, responder, call, method, result
):
    responder.bodies[method] = {"result": result, "tabId": 41, "zoom": 1.25}
    elements = call(bridge)
    assert elements, "expected at least one element"
    for element in elements:
        assert element["tabId"] == 41
        assert element["zoom"] == 1.25
        assert element["elementId"] == "doc1.1"
        assert element["rect"] == ELEMENT["rect"]
    assert _last(ext)["method"] == method
    assert "tabId" not in ELEMENT  # the payload itself is not mutated


def test_missing_zoom_is_none_and_no_match_is_not_an_error(bridge, ext, responder):
    responder.bodies["describeElement"] = {"result": ELEMENT, "tabId": 3}
    element = bridge.describe_element("doc1.1")
    assert (element["tabId"], element["zoom"]) == (3, None)
    responder.bodies["querySelector"] = {"result": None}
    assert bridge.query_selector("#none") is None
    responder.bodies["queryXPath"] = {"result": []}
    assert bridge.query_xpath("//none") == []


def test_wait_for_selector_match_summary_is_returned_as_is(bridge, ext, responder):
    summary = {"element": None, "matched": True, "count": 0}
    responder.bodies["waitForSelector"] = {
        "result": summary,
        "tabId": 5,
        "zoom": 1.0,
    }
    assert bridge.wait_for_selector("#gone", state="detached") == summary


# -- params and timeouts -----------------------------------------------------


def test_waiting_commands_send_timeout_ms_and_exact_params(bridge, ext, responder):
    responder.bodies["navigate"] = {
        "result": {"url": "u", "title": "", "tabId": 1, "contentReady": True}
    }
    bridge.navigate("https://a.test", timeout=12.5, wait_until="commit")
    assert _last(ext)["params"] == {
        "url": "https://a.test",
        "waitUntil": "commit",
        "timeoutMs": 12500,
    }

    responder.bodies["waitForSelector"] = {"result": ELEMENT}
    conditions = [{"type": "visible"}]
    bridge.wait_for_selector(
        "//a", type="xpath", timeout=2, state="stable", conditions=conditions
    )
    assert _last(ext)["params"] == {
        "selector": "//a",
        "type": "xpath",
        "state": "stable",
        "conditions": conditions,
        "timeoutMs": 2000,
    }

    responder.bodies["createTab"] = {
        "result": {"tabId": 9, "url": "u", "title": "", "contentReady": False}
    }
    assert (
        bridge.create_tab(
            "https://b.test", active=False, wait_until="domcontentloaded"
        )["tabId"]
        == 9
    )
    assert _last(ext)["params"] == {
        "url": "https://b.test",
        "active": False,
        "waitUntil": "domcontentloaded",
        "timeoutMs": 30000,
    }

    responder.bodies["executeScript"] = {"result": {"value": [1, 2]}}
    assert bridge.execute_script("[1,2]", timeout=3) == [1, 2]
    assert _last(ext)["params"] == {"code": "[1,2]", "timeoutMs": 3000}

    responder.bodies["waitForResponse"] = {"result": None}
    assert (
        bridge.wait_for_response(
            "*/api/*", timeout=0.5, since=1700000000000, any_tab=True
        )
        is None
    )
    assert _last(ext)["params"] == {
        "pattern": "*/api/*",
        "since": 1700000000000,
        "anyTab": True,
        "timeoutMs": 500,
    }


def test_element_text_returns_the_full_text_for_the_given_tab(bridge, ext, responder):
    full = "x" * 1999 + "Z"
    responder.bodies["elementText"] = {
        "result": {"text": full, "length": 2000, "truncated": False},
        "tabId": 7,
    }
    assert bridge.element_text("doc1.1", timeout=2.5, tab_id=7) == full
    request = _last(ext)
    assert request["method"] == "elementText"
    assert request["params"] == {"elementId": "doc1.1"}
    assert request["target"]["tabId"] == 7


@pytest.mark.parametrize(
    "result", [None, "raw", {"length": 3}, {"text": None}, {"text": 5}]
)
def test_element_text_rejects_a_malformed_result(bridge, ext, responder, result):
    responder.bodies["elementText"] = {"result": result}
    with pytest.raises(errors.BridgeProtocolError) as info:
        bridge.element_text("doc1.1")
    assert info.value.method == "elementText"


def test_wait_transport_deadline_exceeds_timeout_ms(bridge, ext, responder):
    # The worker replies `timeout` at timeoutMs; Python must still be listening then.
    def late(request):
        time.sleep(0.4)
        return {"ok": False, "error": {"code": "wait_timeout", "message": "late"}}

    responder.bodies["waitForSelector"] = late
    with pytest.raises(errors.WaitTimeoutError):
        bridge.wait_for_selector("#x", timeout=0.2)


def test_non_waiting_commands_send_exact_params(bridge, ext, responder):
    probe = {"event": None, "hit": False, "rect": {}, "geometry": {}}
    responder.bodies["pointerProbe"] = {"result": probe}
    assert (
        bridge.pointer_probe("doc1.1", 1700000000123.5, quiet_ms=60, wait_ms=500)
        == probe
    )
    assert _last(ext)["params"] == {
        "elementId": "doc1.1",
        "since": 1700000000123.5,
        "quietMs": 60,
        "waitMs": 500,
    }

    responder.bodies["getComputedStyle"] = {"result": {"value": "rgb(0, 0, 0)"}}
    assert bridge.get_computed_style("doc1.1", "color") == "rgb(0, 0, 0)"
    assert _last(ext)["params"] == {"elementId": "doc1.1", "property": "color"}

    responder.bodies["getAttribute"] = {"result": {"value": None}}
    assert bridge.get_attribute("doc1.1", "href") is None
    assert _last(ext)["params"] == {"elementId": "doc1.1", "name": "href"}

    responder.bodies["queryByText"] = {"result": []}
    bridge.get_element_by_text("Go", exact=True)
    assert _last(ext)["params"] == {"text": "Go", "exact": True}

    responder.bodies["getRecentResponses"] = {"result": [{"url": "u"}]}
    assert bridge.get_recent_responses() == [{"url": "u"}]
    assert _last(ext)["params"] == {"since": None, "anyTab": True}

    responder.bodies["scrollTo"] = {"result": {"scrollX": 0, "scrollY": 10}}
    assert bridge.scroll_to(0, 10) == {"scrollX": 0, "scrollY": 10}
    assert _last(ext)["params"] == {"x": 0, "y": 10}


def test_tab_targeting_pinned_explicit_and_tabless(bridge, ext, responder):
    bridge.pinned_tab_id = 7
    responder.bodies["querySelector"] = {"result": None}
    bridge.query_selector("#a")
    assert _last(ext)["target"] == {"tabId": 7, "frameId": 0}
    bridge.query_selector("#a", tab_id=12)
    assert _last(ext)["target"]["tabId"] == 12
    responder.bodies["listTabs"] = {"result": [{"tabId": 7}]}
    assert bridge.list_tabs() == [{"tabId": 7}]
    assert _last(ext)["target"]["tabId"] is None
    responder.bodies["activateTab"] = {
        "result": {"tabId": 12, "windowId": 1, "activated": True}
    }
    assert bridge.activate_tab(tab_id=12)["activated"] is True
    assert _last(ext)["target"]["tabId"] == 12


# -- close_tab ---------------------------------------------------------------


def test_close_tab_targets_given_tab_and_keeps_other_pin(bridge, ext, responder):
    responder.bodies["closeTab"] = {"result": {"closed": True}}
    bridge.pinned_tab_id = 7
    assert bridge.close_tab(9) == {"closed": True}
    request = _last(ext)
    assert request["target"]["tabId"] == 9
    assert request["params"] == {}
    assert bridge.pinned_tab_id == 7


def test_close_tab_none_closes_pinned_and_unpins(bridge, ext, responder):
    responder.bodies["closeTab"] = {"result": {"closed": True}}
    bridge.pinned_tab_id = 7
    bridge.close_tab()
    assert _last(ext)["target"]["tabId"] == 7
    assert bridge.pinned_tab_id is None


def test_close_tab_explicit_pinned_unpins_and_error_keeps_pin(bridge, ext, responder):
    responder.bodies["closeTab"] = {"result": {"closed": True}}
    bridge.pinned_tab_id = 7
    bridge.close_tab(7)
    assert bridge.pinned_tab_id is None

    bridge.pinned_tab_id = 8
    responder.bodies["closeTab"] = {
        "ok": False,
        "error": {"code": "internal", "message": "no"},
    }
    with pytest.raises(errors.CommandError):
        bridge.close_tab()
    assert bridge.pinned_tab_id == 8


# -- idempotency -------------------------------------------------------------


class RecordingTransport:
    """Transport-compatible stand-in, as an attach-mode hub session would be."""

    token = "hub-token"
    actual_port = 4242
    connected = True

    def __init__(self, result=None):
        self.pinned_tab_id = None
        self.calls = []
        self.result = result
        self.started = self.stopped = False

    def start(self, timeout=30.0):
        self.started = True

    def shutdown(self):
        self.stopped = True

    def wait_for_connection(self, timeout=60.0):
        return True

    def on(self, event, handler):
        pass

    def request(
        self, method, params=None, *, tab_id=PINNED, timeout=30.0, idempotent=False
    ):
        self.calls.append(
            {
                "method": method,
                "params": params,
                "tab_id": tab_id,
                "timeout": timeout,
                "idempotent": idempotent,
            }
        )
        result = self.result(method) if callable(self.result) else self.result
        return Reply(result=result, tab_id=3, zoom=1.0)


def _shaped(method):
    lists = {
        "querySelectorAll",
        "getAllInteractive",
        "queryByText",
        "queryXPath",
        "listTabs",
        "getRecentResponses",
    }
    if method in lists:
        return []
    if method == "waitForSelector":
        return dict(ELEMENT)
    if method == "ping":
        return {"pong": True}
    if method == "elementText":
        return {"text": "", "length": 0, "truncated": False}
    return {"value": None, "elementId": "doc1.1"}


IDEMPOTENCY_CASES = [
    (lambda b: b.query_selector("a"), "querySelector", True),
    (lambda b: b.query_selector_all("a"), "querySelectorAll", True),
    (lambda b: b.get_all_interactive(), "getAllInteractive", True),
    (lambda b: b.get_element_by_text("a"), "queryByText", True),
    (lambda b: b.query_xpath("//a"), "queryXPath", True),
    (lambda b: b.describe_element("e"), "describeElement", True),
    (lambda b: b.element_text("e"), "elementText", True),
    (lambda b: b.scroll_into_view("e"), "scrollIntoView", True),
    (lambda b: b.get_element_coords("e"), "scrollIntoView", True),
    (lambda b: b.focus("e"), "focus", True),
    (lambda b: b.focus_state("e"), "focusState", True),
    (lambda b: b.pointer_probe("e", 1.0), "pointerProbe", True),
    (lambda b: b.get_attribute("e", "id"), "getAttribute", True),
    (lambda b: b.get_computed_style("e", "color"), "getComputedStyle", True),
    (lambda b: b.page_info(), "pageInfo", True),
    (lambda b: b.scroll_to(1, 2), "scrollTo", True),
    (lambda b: b.ping(), "ping", True),
    (lambda b: b.get_tab_info(), "getTabInfo", True),
    (lambda b: b.list_tabs(), "listTabs", True),
    (lambda b: b.activate_tab(), "activateTab", True),
    (lambda b: b.wait_for_response("*"), "waitForResponse", True),
    (lambda b: b.get_recent_responses(), "getRecentResponses", True),
    (lambda b: b.wait_for_selector("a"), "waitForSelector", False),
    (lambda b: b.navigate("https://a.test"), "navigate", False),
    (lambda b: b.execute_script("1"), "executeScript", False),
    (lambda b: b.create_tab(), "createTab", False),
    (lambda b: b.close_tab(5), "closeTab", False),
    (lambda b: b.send("pageInfo"), "pageInfo", True),
    # elementText is in IDEMPOTENT_METHODS, so the raw path agrees with element_text.
    (lambda b: b.send("elementText", {"elementId": "e"}), "elementText", True),
    (lambda b: b.send("navigate", {"url": "u"}), "navigate", False),
]


@pytest.mark.parametrize(
    "call,method,idempotent",
    IDEMPOTENCY_CASES,
    ids=[f"{i}-{case[1]}" for i, case in enumerate(IDEMPOTENCY_CASES)],
)
def test_idempotent_flag_follows_protocol_table(call, method, idempotent):
    fake = RecordingTransport(result=_shaped)
    call(Bridge(transport=fake))
    assert [c["method"] for c in fake.calls] == [method]
    assert fake.calls[0]["idempotent"] is idempotent


def test_injected_transport_is_used_and_delegates_lifecycle():
    fake = RecordingTransport(result={"value": 5})
    b = Bridge("10.0.0.1", 9999, token="ignored", transport=fake)
    assert b.transport is fake
    assert (b.token, b.actual_port, b.connected) == ("hub-token", 4242, True)
    b.start()
    b.pinned_tab_id = 11
    assert fake.pinned_tab_id == 11
    assert b.execute_script("5", timeout=2) == 5
    call = fake.calls[-1]
    assert (call["tab_id"], call["timeout"], call["params"]["timeoutMs"]) == (
        PINNED,
        7.0,
        2000,
    )
    b.shutdown()
    assert fake.started and fake.stopped


def test_idempotent_query_is_resent_after_reconnect(bridge):
    first = FakeExtension(
        bridge.actual_port, TOKEN, handler=lambda r: {"noreply": True}
    )
    assert first.wait_welcomed(5)
    assert bridge.wait_for_connection(5)
    box = {}

    def run():
        try:
            box["value"] = bridge.query_selector("#go", timeout=10)
        except errors.EmuniumError as exc:  # every bridge failure; inspected below
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert first.wait_requests(1)
    first.close()
    with FakeExtension(
        bridge.actual_port,
        TOKEN,
        handler=lambda r: {"result": ELEMENT, "tabId": 2, "zoom": 1.0},
    ) as second:
        assert second.wait_welcomed(5)
        thread.join(10)
        assert "error" not in box, box.get("error")
        assert box["value"]["tabId"] == 2
        assert [r["method"] for r in second.requests] == ["querySelector"]


def test_non_idempotent_navigate_is_not_resent(bridge):
    first = FakeExtension(
        bridge.actual_port, TOKEN, handler=lambda r: {"noreply": True}
    )
    assert first.wait_welcomed(5)
    assert bridge.wait_for_connection(5)
    box = {}

    def run():
        try:
            bridge.navigate("https://a.test", timeout=10)
        except errors.EmuniumError as exc:  # every bridge failure; inspected below
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert first.wait_requests(1)
    first.close()
    thread.join(10)
    assert isinstance(box.get("error"), errors.BridgeDisconnectedError)
    assert box["error"].method == "navigate"


# -- ping and send -----------------------------------------------------------


def test_ping_true_on_pong(bridge, ext, responder):
    responder.bodies["ping"] = {"result": {"pong": True, "url": "about:blank"}}
    assert bridge.ping() is True


@pytest.mark.parametrize(
    "body",
    [
        {"ok": False, "error": {"code": "content_unavailable", "message": "chrome://"}},
        {"result": {"pong": False}},
        {"result": None},
        {"noreply": True},
    ],
    ids=["error", "no-pong", "null", "no-reply"],
)
def test_ping_false_never_raises(bridge, ext, responder, body):
    responder.bodies["ping"] = body
    assert bridge.ping(timeout=0.3) is False


def test_ping_false_when_disconnected_or_transport_raises():
    b = Bridge(token=TOKEN)  # never started
    assert b.ping(timeout=0.2) is False

    class Exploding(RecordingTransport):
        def request(self, *args, **kwargs):
            raise RuntimeError("hub gone")

    assert Bridge(transport=Exploding()).ping() is False


def test_send_returns_raw_result_with_pinned_target(bridge, ext, responder):
    raw = {"anything": [1, 2, 3], "error": "not an error, just a key"}
    responder.bodies["custom"] = {"result": raw, "tabId": 7}
    bridge.pinned_tab_id = 7
    assert bridge.send("custom", {"a": 1}) == raw
    request = _last(ext)
    assert (request["params"], request["target"]["tabId"]) == ({"a": 1}, 7)
    assert bridge.send("custom", tab_id=None) == raw
    assert _last(ext)["target"]["tabId"] is None


def test_request_returns_reply_envelope(bridge, ext, responder):
    responder.bodies["pageInfo"] = {"result": {"url": "u"}, "tabId": 4, "zoom": 0.9}
    reply = bridge.request("pageInfo", tab_id=4, idempotent=True)
    assert reply == Reply(result={"url": "u"}, tab_id=4, zoom=0.9)
