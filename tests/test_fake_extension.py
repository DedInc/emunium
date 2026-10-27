"""Live-socket smoke test: FakeExtension against a minimal websockets server.

Checks the hello frame (bridge-protocol section 1.4), that a request is answered
only after ``welcome`` (1.5/1.6), and the reply shape (section 3).
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Any

import pytest
from fake_extension import FakeExtension
from websockets.sync.server import serve

if TYPE_CHECKING:
    from typing_extensions import Self

TOKEN = "smoke-token-0123456789"
TIMEOUT = 5.0


class MiniBridge:
    """One-connection server: read hello, welcome, send one request, read reply."""

    def __init__(self, request: dict[str, Any]) -> None:
        self.request = request
        self.hello: Any = None
        self.reply: Any = None
        self.errors: list[BaseException] = []
        self.done = threading.Event()
        self.server = serve(self._handle, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.server.shutdown()
        self._thread.join(TIMEOUT)

    def _handle(self, ws) -> None:
        try:
            self.hello = json.loads(ws.recv(timeout=TIMEOUT))
            ws.send(json.dumps({"type": "welcome", "protocol": 2}))
            ws.send(json.dumps(self.request))
            self.reply = json.loads(ws.recv(timeout=TIMEOUT))
        except BaseException as exc:
            self.errors.append(exc)  # asserted empty by _run
            raise
        finally:
            self.done.set()


def _run(request: dict[str, Any], handler=None):
    with (
        MiniBridge(request) as bridge,
        FakeExtension(bridge.port, TOKEN, handler=handler) as ext,
    ):
        assert bridge.done.wait(TIMEOUT), "server never finished the exchange"
        assert ext.wait_welcomed(TIMEOUT)
        assert ext.wait_requests(1, TIMEOUT)
    assert bridge.errors == []
    return bridge, ext


def test_hello_welcome_request_reply_roundtrip():
    request = {
        "type": "request",
        "id": 17,
        "method": "pageInfo",
        "params": {},
        "target": {"tabId": 123, "frameId": 0},
    }
    bridge, ext = _run(
        request, handler=lambda req: {"result": {"url": "about:blank"}, "zoom": 1.0}
    )

    hello = bridge.hello
    assert hello["type"] == "hello"
    assert hello["protocol"] == 2
    assert hello["token"] == TOKEN
    assert isinstance(hello["extensionVersion"], str) and hello["extensionVersion"]
    assert isinstance(hello["userAgent"], str) and hello["userAgent"]

    assert ext.welcome == {"type": "welcome", "protocol": 2}
    assert ext.requests == [request]
    assert bridge.reply == {
        "type": "reply",
        "id": 17,
        "ok": True,
        "result": {"url": "about:blank"},
        "tabId": 123,
        "zoom": 1.0,
    }


def test_error_reply_shape():
    request = {
        "type": "request",
        "id": 4,
        "method": "click",
        "params": {"handle": "h1"},
        "target": {"tabId": None, "frameId": 0},
    }
    bridge, _ = _run(
        request,
        handler=lambda req: {"error": {"code": "stale_element", "message": "gone"}},
    )
    reply = bridge.reply
    assert reply == {
        "type": "reply",
        "id": 4,
        "ok": False,
        "error": {"code": "stale_element", "message": "gone"},
        "tabId": None,
    }
    assert "result" not in reply


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
