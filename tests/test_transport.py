"""Transport protocol v2 tests (bridge-protocol.md sections 1-5), driven by FakeExtension."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import socket
import threading
import time

import pytest

# The transport is built on websockets.asyncio.server; websockets < 14 (such as
# a global interpreter pinned for other software) is not a supported target.
pytest.importorskip("websockets", minversion="14.0")

from fake_extension import FakeExtension, _connect_kwargs
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect as ws_connect

from emunium import errors
from emunium._bridge import transport as transport_mod
from emunium._bridge.transport import PINNED, Reply, Transport

TOKEN = "test-token-0123456789"


@pytest.fixture
def transport():
    t = Transport(token=TOKEN)
    t.start(timeout=5)
    try:
        yield t
    finally:
        t.shutdown()


def _connect(t, **kwargs):
    ext = FakeExtension(t.actual_port, kwargs.pop("token", TOKEN), **kwargs)
    return ext


def _welcomed_conn(t, previous=None, timeout=5.0):
    """The transport's active connection once the welcomed one is activated.

    The server sends ``welcome`` before it publishes the connection as
    ``_active``, so ``ext.wait_welcomed()`` alone races. Waits on the
    transport's condition (notified on activation) until the active connection
    is set, open, and not ``previous`` (the one it supersedes).
    """

    def activated():
        active = t._active
        return active is not None and not active.closed and active is not previous

    with t._cond:
        assert t._cond.wait_for(activated, timeout), (
            "welcomed connection never activated"
        )
        return t._active


def _in_thread(fn):
    """Run ``fn`` in a thread; returns (thread, box) where box gets 'value' or 'error'."""
    box = {}

    def run():
        start = time.monotonic()
        try:
            box["value"] = fn()
        except BaseException as exc:  # inspected by the test
            logging.getLogger(__name__).debug("_in_thread call raised", exc_info=True)
            box["error"] = exc
        box["elapsed"] = time.monotonic() - start

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


# -- handshake -----------------------------------------------------------


def test_valid_hello_is_welcomed_and_extra_fields_kept(transport):
    with _connect(transport, send_hello=False) as ext:
        assert ext.wait_connected(5)
        ext.send_raw(
            {
                "type": "hello",
                "protocol": 2,
                "token": TOKEN,
                "extensionVersion": "3.1.0",
                "userAgent": "UA",
                "installId": "inst-1",
                "label": "work",
                "mode": "attached",
            }
        )
        assert ext.wait_welcomed(5)
        assert ext.welcome == {"type": "welcome", "protocol": 2}
        assert transport.wait_for_connection(5)
        assert transport.connected
        hello = _welcomed_conn(transport).hello
        assert (hello["installId"], hello["label"], hello["mode"]) == (
            "inst-1",
            "work",
            "attached",
        )


@pytest.mark.parametrize(
    "kwargs",
    [{"token": "wrong-token"}, {"protocol": 1}],
    ids=["bad-token", "bad-protocol"],
)
def test_bad_hello_closed_with_4001(transport, kwargs):
    with _connect(transport, **kwargs) as ext:
        assert ext.wait_closed(5)
        assert ext.closed_code == 4001
        assert ext.welcome is None
        assert not transport.connected


def test_non_hello_first_frame_closed_with_4001(transport):
    with _connect(transport, send_hello=False) as ext:
        assert ext.wait_connected(5)
        ext.send_raw({"type": "event", "event": "ping"})
        assert ext.wait_closed(5)
        assert ext.closed_code == 4001
        assert not transport.connected


def test_missing_hello_times_out_with_4001(transport, monkeypatch):
    monkeypatch.setattr(transport_mod, "HELLO_TIMEOUT", 0.3)
    with _connect(transport, send_hello=False) as ext:
        assert ext.wait_closed(5)
        assert ext.closed_code == 4001
        assert ext.welcome is None


def test_authenticate_is_replaceable(transport):
    transport.authenticate = lambda hello: hello.get("installId") == "ok"
    with _connect(transport) as ext:  # right token, but no installId
        assert ext.wait_closed(5)
        assert ext.closed_code == 4001
    with _connect(transport, send_hello=False) as ext:
        ext.send_raw({"type": "hello", "protocol": 2, "token": "x", "installId": "ok"})
        assert ext.wait_welcomed(5)


def test_raising_authenticate_rejects(transport):
    def boom(hello):
        raise RuntimeError("broken check")

    transport.authenticate = boom
    with _connect(transport) as ext:
        assert ext.wait_closed(5)
        assert ext.closed_code == 4001


DEEP_JSON = "[" * 100000 + "]" * 100000


def test_deeply_nested_hello_closed_with_4001(transport):
    with _connect(transport, send_hello=False) as ext:
        assert ext.wait_connected(5)
        ext.send_raw(DEEP_JSON)
        assert ext.wait_closed(5)
        assert ext.closed_code == 4001


def test_deeply_nested_frame_on_active_connection_is_ignored(transport):
    with _connect(transport, handler=lambda r: {"result": "alive"}) as ext:
        assert ext.wait_welcomed(5)
        ext.send_raw(DEEP_JSON)
        ext.send_raw("not json at all")
        assert transport.request("ping", timeout=5).result == "alive"
        assert ext.closed_code is None


def test_silent_client_does_not_delay_valid_client(transport):
    with _connect(transport, send_hello=False) as silent:
        assert silent.wait_connected(5)
        start = time.monotonic()
        with _connect(transport) as ext:
            assert ext.wait_welcomed(5)
            assert time.monotonic() - start < 1.5
        assert not silent.wait_closed(0)  # still inside its 5 s hello window


def _raw_client(t, origin):
    """Plain client that can set an Origin header (FakeExtension sends none)."""
    return ws_connect(
        f"ws://127.0.0.1:{t.actual_port}", origin=origin, **_connect_kwargs()
    )


def test_foreign_origin_rejected_with_4003(transport):
    with _raw_client(transport, "http://evil.example") as ws:
        with pytest.raises(ConnectionClosed) as info:
            ws.recv(timeout=5)
        assert info.value.rcvd is not None and info.value.rcvd.code == 4003
    assert not transport.connected


def test_extension_origin_accepted(transport):
    with _raw_client(transport, "chrome-extension://abc") as ws:
        ws.send(json.dumps({"type": "hello", "protocol": 2, "token": TOKEN}))
        assert json.loads(ws.recv(timeout=5)) == {"type": "welcome", "protocol": 2}
        assert transport.wait_for_connection(5)


def test_firefox_extension_origin_accepted(transport):
    with _raw_client(
        transport, "moz-extension://1b2c3d4e-0000-4000-8000-000000000000"
    ) as ws:
        ws.send(json.dumps({"type": "hello", "protocol": 2, "token": TOKEN}))
        assert json.loads(ws.recv(timeout=5)) == {"type": "welcome", "protocol": 2}
        assert transport.wait_for_connection(5)


def test_firefox_extension_origin_still_needs_token(transport):
    with _raw_client(transport, "moz-extension://abc") as ws:
        ws.send(json.dumps({"type": "hello", "protocol": 2, "token": "wrong"}))
        with pytest.raises(ConnectionClosed) as info:
            ws.recv(timeout=5)
        assert info.value.rcvd is not None and info.value.rcvd.code == 4001
    assert not transport.connected


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        (None, True),
        ("chrome-extension://abc", True),
        ("moz-extension://abc", True),
        ("null", False),
        ("http://evil.example", False),
        ("https://evil.example", False),
        ("moz-extension:abc", False),
        ("http://moz-extension://abc", False),
    ],
)
def test_default_allow_origin(origin, allowed):
    assert transport_mod._default_allow_origin(origin) is allowed


def test_repeated_origin_headers_refused_at_handshake(transport):
    """Two Origin headers never reach allow_origin as one picked value."""
    seen = []

    def record(origin):
        seen.append(origin)
        return True

    transport.allow_origin = record
    with pytest.raises(InvalidStatus) as info:
        ws_connect(
            f"ws://127.0.0.1:{transport.actual_port}",
            additional_headers=[
                ("Origin", "chrome-extension://abc"),
                ("Origin", "http://evil.example"),
            ],
            **_connect_kwargs(),
        )
    assert info.value.response.status_code == 400
    assert seen == []
    assert not transport.connected


def test_allow_origin_is_replaceable(transport):
    seen = []

    def only_none(origin):
        seen.append(origin)
        return origin is None

    transport.allow_origin = only_none
    with _raw_client(transport, "chrome-extension://abc") as ws:
        with pytest.raises(ConnectionClosed) as info:
            ws.recv(timeout=5)
        assert info.value.rcvd is not None and info.value.rcvd.code == 4003
    with _connect(transport) as ext:
        assert ext.wait_welcomed(5)
    assert seen == ["chrome-extension://abc", None]


# -- single active connection -------------------------------------------


def test_new_connection_supersedes_old_and_old_close_keeps_new(transport):
    first = _connect(transport, handler=lambda r: {"result": "first"})
    try:
        assert first.wait_welcomed(5)
        with _connect(transport, handler=lambda r: {"result": "second"}) as second:
            assert second.wait_welcomed(5)
            assert first.wait_closed(5)
            assert first.closed_code == 4002
            time.sleep(0.1)  # let the old socket's teardown run
            assert transport.connected
            assert transport.request("ping", timeout=5).result == "second"
            assert len(first.requests) == 0
    finally:
        first.close()


# -- requests ------------------------------------------------------------


def test_request_frame_shape_and_reply_envelope(transport):
    with _connect(
        transport, handler=lambda r: {"result": {"n": 1}, "tabId": 9, "zoom": 1.25}
    ) as ext:
        assert ext.wait_welcomed(5)
        reply = transport.request("pageInfo", timeout=5)
        assert reply == Reply(result={"n": 1}, tab_id=9, zoom=1.25)

        transport.pinned_tab_id = 42
        transport.request("querySelector", {"selector": "a"}, timeout=5)
        transport.request("querySelector", {"selector": "b"}, tab_id=7, timeout=5)
        transport.request("listTabs", tab_id=None, timeout=5)
        frames = ext.requests
        assert frames[0]["type"] == "request"
        assert frames[0]["params"] == {}
        assert frames[0]["target"] == {"tabId": None, "frameId": 0}
        assert frames[1]["target"] == {"tabId": 42, "frameId": 0}
        assert frames[1]["params"] == {"selector": "a"}
        assert frames[2]["target"]["tabId"] == 7
        assert frames[3]["target"]["tabId"] is None
        assert all("tabId" not in f for f in frames)
        assert len({f["id"] for f in frames}) == 4
    assert PINNED is transport_mod.PINNED


ERROR_TABLE = [
    ("stale_element", errors.StaleElementError),
    ("no_target", errors.TargetClosedError),
    ("target_closed", errors.TargetClosedError),
    ("content_unavailable", errors.ContentUnavailableError),
    ("navigation_timeout", errors.NavigationTimeoutError),
    ("navigation_failed", errors.NavigationError),
    ("timeout", errors.CommandTimeoutError),
    ("wait_timeout", errors.WaitTimeoutError),
    ("script_error", errors.ScriptError),
    ("csp_blocked", errors.CSPBlockedError),
    ("invalid_argument", errors.InvalidArgumentError),
    ("unknown_condition", errors.InvalidArgumentError),
    ("unknown_method", errors.BridgeProtocolError),
    ("internal", errors.CommandError),
    ("something_new", errors.CommandError),
]


def test_error_replies_raise_mapped_exceptions(transport):
    def handler(request):
        code = request["params"]["code"]
        return {"ok": False, "error": {"code": code, "message": "msg " + code}}

    with _connect(transport, handler=handler) as ext:
        assert ext.wait_welcomed(5)
        for code, cls in ERROR_TABLE:
            with pytest.raises(cls) as info:
                transport.request("doThing", {"code": code}, timeout=5)
            assert type(info.value) is cls, code
            assert info.value.code == code
            assert info.value.method == "doThing"
            assert info.value.message == "msg " + code


def test_reply_without_ok_is_protocol_error(transport):
    with _connect(transport, handler=lambda r: {"noreply": True}) as ext:
        assert ext.wait_welcomed(5)
        thread, box = _in_thread(lambda: transport.request("ping", timeout=5))
        assert ext.wait_requests(1)
        ext.send_raw({"type": "reply", "id": ext.requests[0]["id"], "result": 1})
        thread.join(5)
        assert isinstance(box.get("error"), errors.BridgeProtocolError)


def test_timeout_raises_bridge_timeout_and_cleans_pending(transport):
    with _connect(transport, handler=lambda r: {"noreply": True}) as ext:
        assert ext.wait_welcomed(5)
        start = time.monotonic()
        with pytest.raises(errors.BridgeTimeoutError):
            transport.request("ping", timeout=0.3)
        assert time.monotonic() - start < 2
        assert _welcomed_conn(transport)._pending == {}


def test_five_megabyte_payloads_both_directions(transport):
    big = "x" * (5 * 1024 * 1024)
    with _connect(
        transport, handler=lambda r: {"result": big + r["params"]["tail"]}
    ) as ext:
        assert ext.wait_welcomed(5)
        reply = transport.request(
            "executeScript", {"tail": "!", "code": big}, timeout=20
        )
        assert len(reply.result) == len(big) + 1
        assert len(ext.requests[0]["params"]["code"]) == len(big)


# -- connection loss -----------------------------------------------------


def test_pending_request_fails_immediately_on_disconnect(transport):
    ext = _connect(transport, handler=lambda r: {"noreply": True})
    try:
        assert ext.wait_welcomed(5)
        thread, box = _in_thread(lambda: transport.request("navigate", timeout=20))
        assert ext.wait_requests(1)
        ext.close()
        thread.join(5)
        assert isinstance(box.get("error"), errors.BridgeDisconnectedError)
        assert isinstance(box["error"], ConnectionError)
        assert box["elapsed"] < 3
    finally:
        ext.close()


def test_idempotent_request_resent_once_after_reconnect(transport):
    first = _connect(transport, handler=lambda r: {"noreply": True})
    try:
        assert first.wait_welcomed(5)
        thread, box = _in_thread(
            lambda: transport.request("pageInfo", timeout=20, idempotent=True)
        )
        assert first.wait_requests(1)
        first.close()
        with _connect(transport, handler=lambda r: {"result": "again"}) as second:
            thread.join(10)
            assert box.get("value") == Reply(result="again", tab_id=None, zoom=None)
            assert len(second.requests) == 1
            assert second.requests[0]["method"] == "pageInfo"
    finally:
        first.close()


def test_idempotent_request_resent_only_once(transport):
    first = _connect(transport, handler=lambda r: {"noreply": True})
    second = None
    try:
        assert first.wait_welcomed(5)
        thread, box = _in_thread(
            lambda: transport.request("pageInfo", timeout=20, idempotent=True)
        )
        assert first.wait_requests(1)
        first.close()
        second = _connect(transport, handler=lambda r: {"noreply": True})
        assert second.wait_requests(1)
        second.close()
        thread.join(5)
        assert isinstance(box.get("error"), errors.BridgeDisconnectedError)
    finally:
        first.close()
        if second is not None:
            second.close()


def test_request_while_disconnected_waits_for_connection(transport):
    thread, box = _in_thread(lambda: transport.request("ping", timeout=10))
    time.sleep(0.3)
    assert thread.is_alive()
    with _connect(transport, handler=lambda r: {"result": "late"}) as ext:
        thread.join(5)
        assert box.get("value").result == "late"
        assert len(ext.requests) == 1


def test_request_while_disconnected_gives_up_at_timeout(transport):
    start = time.monotonic()
    with pytest.raises(errors.BridgeDisconnectedError):
        transport.request("ping", timeout=0.4)
    assert 0.3 < time.monotonic() - start < 2


def test_failed_send_fails_request_at_once(transport):
    with _connect(transport) as ext:
        assert ext.wait_welcomed(5)

        async def broken_send(_text):
            raise OSError("socket write failed")

        _welcomed_conn(transport).ws.send = broken_send
        start = time.monotonic()
        with pytest.raises(errors.BridgeDisconnectedError):
            transport.request("navigate", timeout=20)
        assert time.monotonic() - start < 2
        assert not transport.connected  # the dying connection is retired
        assert ext.wait_closed(5)


def test_failed_send_idempotent_request_succeeds_on_new_connection(transport):
    first = _connect(transport)
    try:
        assert first.wait_welcomed(5)

        async def broken_send(_text):
            raise OSError("socket write failed")

        _welcomed_conn(transport).ws.send = broken_send
        thread, box = _in_thread(
            lambda: transport.request("pageInfo", timeout=10, idempotent=True)
        )
        assert first.wait_closed(5)
        assert (
            thread.is_alive()
        )  # waiting for a reconnect, not failed on the dying conn
        with _connect(transport, handler=lambda r: {"result": "fresh"}) as second:
            thread.join(5)
            assert box.get("value") is not None, box.get("error")
            assert box["value"].result == "fresh"
            assert len(second.requests) == 1
    finally:
        first.close()


def test_failed_send_retires_conn_before_waking_retry(transport):
    """Stall retirement until the caller re-submits; the old order lost that race."""
    first = _connect(transport)
    try:
        assert first.wait_welcomed(5)
        conn = _welcomed_conn(transport)

        async def broken_send(_text):
            raise OSError("socket write failed")

        conn.ws.send = broken_send
        resubmitted = threading.Event()
        submits = []
        real_submit, real_fail_all = conn.submit, conn.fail_all

        def spy_submit(request_id, frame):
            submits.append(request_id)
            if len(submits) > 1:
                resubmitted.set()
            return real_submit(request_id, frame)

        stalled = []

        def stalled_fail_all(error):
            # If the request was already failed, the caller re-submits here.
            if not stalled:
                stalled.append(True)
                resubmitted.wait(0.5)
            return real_fail_all(error)

        conn.submit, conn.fail_all = spy_submit, stalled_fail_all
        thread, box = _in_thread(
            lambda: transport.request("pageInfo", timeout=10, idempotent=True)
        )
        assert first.wait_closed(5)
        assert not resubmitted.is_set()
        assert submits and len(submits) == 1
        with _connect(transport, handler=lambda r: {"result": "fresh"}):
            thread.join(5)
            assert box.get("value") is not None, box.get("error")
            assert box["value"].result == "fresh"
    finally:
        first.close()


def test_concurrent_failed_sends_retire_once(transport, caplog):
    with _connect(transport) as ext:
        assert ext.wait_welcomed(5)
        conn = _welcomed_conn(transport)
        closes = []
        real_close = conn._close_socket

        async def counting_close(code, reason):
            closes.append(code)
            await real_close(code, reason)

        async def slow_broken_send(_text):
            await asyncio.sleep(0.2)
            raise OSError("socket write failed")

        conn.ws.send = slow_broken_send
        conn._close_socket = counting_close
        caplog.set_level(logging.WARNING, logger="emunium.bridge")
        threads = [
            _in_thread(lambda: transport.request("navigate", timeout=1))[0]
            for _ in range(4)
        ]
        for thread in threads:
            thread.join(5)
        assert ext.wait_closed(5)
        warnings = [
            r for r in caplog.records if "Socket write failed" in r.getMessage()
        ]
        assert len(warnings) == 1
        assert closes == [1011]


def test_cancelled_send_fails_quietly(transport, caplog):
    with _connect(transport) as ext:
        assert ext.wait_welcomed(5)
        conn = _welcomed_conn(transport)
        pending = concurrent.futures.Future()
        conn._pending[999] = pending
        cancelled = concurrent.futures.Future()
        cancelled.cancel()
        caplog.set_level(logging.WARNING, logger="emunium.bridge")
        conn._after_send(999, cancelled)
        assert conn.closed and not transport.connected
        assert isinstance(pending.exception(0), errors.BridgeDisconnectedError)
        assert not [
            r for r in caplog.records if "Socket write failed" in r.getMessage()
        ]
        assert not ext.wait_closed(0.3)  # no close(1011) scheduled


def test_no_send_after_deadline(transport, monkeypatch):
    with _connect(transport) as ext:
        assert ext.wait_welcomed(5)
        real_wait = transport._wait_active

        def slow_wait(deadline):
            conn = real_wait(deadline)
            time.sleep(max(0.0, deadline - time.monotonic()) + 0.05)
            return conn

        monkeypatch.setattr(transport, "_wait_active", slow_wait)
        with pytest.raises(errors.BridgeTimeoutError):
            transport.request("navigate", timeout=0.2)
        assert not ext.wait_requests(1, timeout=0.3)


def test_unsent_request_retried_on_next_connection_even_if_not_idempotent(
    transport, monkeypatch
):
    with _connect(transport, handler=lambda r: {"result": "ok"}) as ext:
        assert ext.wait_welcomed(5)
        # A connection that closed between _wait_active() and submit(); its
        # socket is never touched because submit() refuses before writing.
        stale = transport_mod._Connection(
            _welcomed_conn(transport).ws,
            {},
            transport._loop,
            lambda c, f: None,
            lambda c: None,
        )
        stale.fail_all(errors.BridgeDisconnectedError("gone"))
        real_wait = transport._wait_active
        calls = []

        def racing_wait(deadline):
            calls.append(deadline)
            return stale if len(calls) == 1 else real_wait(deadline)

        monkeypatch.setattr(transport, "_wait_active", racing_wait)
        assert transport.request("navigate", timeout=5).result == "ok"
        assert len(calls) == 2
        assert len(ext.requests) == 1


# -- events --------------------------------------------------------------


def test_tab_removed_clears_matching_pin_only(transport):
    seen = threading.Event()
    transport.on("tabRemoved", lambda frame: seen.set())
    with _connect(transport) as ext:
        assert ext.wait_welcomed(5)
        transport.pinned_tab_id = 5
        ext.send_event("tabRemoved", tabId=7)
        assert seen.wait(5)
        assert transport.pinned_tab_id == 5
        seen.clear()
        ext.send_event("tabRemoved", tabId=5)
        assert seen.wait(5)
        assert transport.pinned_tab_id is None


def test_event_handlers_isolated_and_may_call_request(transport):
    results = []
    done = threading.Event()

    def broken(frame):
        raise RuntimeError("handler bug")

    def calls_back(frame):
        results.append(transport.request("ping", timeout=5).result)
        done.set()

    transport.on("tabReady", broken)
    transport.on("tabReady", calls_back)
    with _connect(transport, handler=lambda r: {"result": "pong"}) as ext:
        assert ext.wait_welcomed(5)
        ext.send_event("tabReady", tabId=1, url="about:blank")
        assert done.wait(5)
        assert results == ["pong"]


# -- lifecycle -----------------------------------------------------------


def test_bind_failure_raised_from_start():
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        t = Transport(port=port, token=TOKEN)
        with pytest.raises(errors.BridgeError):
            t.start(timeout=5)
        t.shutdown()
    finally:
        blocker.close()


def test_shutdown_is_idempotent_and_fails_pending():
    t = Transport(token=TOKEN)
    t.start(timeout=5)
    assert t.token == TOKEN and isinstance(t.actual_port, int)
    ext = FakeExtension(t.actual_port, TOKEN, handler=lambda r: {"noreply": True})
    try:
        assert ext.wait_welcomed(5)
        thread, box = _in_thread(lambda: t.request("ping", timeout=20, idempotent=True))
        assert ext.wait_requests(1)
        t.shutdown()
        t.shutdown()
        thread.join(5)
        assert isinstance(box.get("error"), errors.BridgeDisconnectedError)
        assert box["elapsed"] < 3
        assert not t.connected
        with pytest.raises(errors.BridgeDisconnectedError):
            t.request("ping", timeout=1)
    finally:
        ext.close()
        t.shutdown()


def test_default_token_is_generated():
    t = Transport()
    assert isinstance(t.token, str) and len(t.token) >= 32
    assert Transport().token != t.token
