"""Fake MV3 service worker speaking bridge protocol v2 (client side).

Connects to a Python bridge server the way ``background*.js`` does
(``docs/architecture/bridge-protocol.md``), so transport tests run without Chrome.

Usage::

    with FakeExtension(port, token) as ext:
        assert ext.wait_welcomed(5)
        ...                                   # drive the server under test
        ext.requests                          # every request frame received

``handler(request) -> dict | None`` builds the reply body. Recognised keys:
``ok``, ``result``, ``error`` (``{"code", "message"}`` or a bare code string),
``tabId``, ``zoom``; ``{"noreply": True}`` sends nothing (timeout tests).
Returning ``None`` uses the default body ``{"ok": True, "result": None,
"tabId": request["target"]["tabId"]}``. A handler that raises produces an
``ok: false`` reply with code ``internal`` and is recorded in
``.handler_errors``. Handlers run on the reader thread: a slow handler delays
every later frame.

Per bridge-protocol section 1.6 requests that arrive before ``welcome`` are
recorded but never answered.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any, final

from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import ClientConnection
from websockets.sync.client import connect as ws_connect

__all__ = ["FakeExtension", "build_reply", "default_handler"]

Handler = Callable[[dict[str, Any]], dict[str, Any] | None]

logger = logging.getLogger("fake_extension")

MAX_SIZE = 64 * 1024 * 1024
EXTENSION_VERSION = "3.1.0"
USER_AGENT = "Mozilla/5.0 (FakeExtension) Chrome/130.0.0.0"
CONNECT_RETRY_INTERVAL = 0.05


def _target_tab_id(request: dict[str, Any]) -> Any:
    target = request.get("target")
    if isinstance(target, dict):
        return target.get("tabId")
    return None


def default_handler(request: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "result": None, "tabId": _target_tab_id(request)}


def build_reply(request: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """Reply frame per bridge-protocol section 3.

    Success: ``{"type","id","ok":true,"result","tabId"[,"zoom"]}``.
    Failure: ``{"type","id","ok":false,"error":{"code","message"},"tabId"[,"zoom"]}``.
    """
    ok = body.get("ok")
    if ok is None:
        ok = "error" not in body
    ok = bool(ok)
    reply: dict[str, Any] = {"type": "reply", "id": request.get("id"), "ok": ok}
    if ok:
        reply["result"] = body.get("result")
    else:
        error = body.get("error")
        if isinstance(error, str):
            error = {"code": error, "message": error}
        elif not isinstance(error, dict):
            error = {"code": "internal", "message": ""}
        reply["error"] = error
    reply["tabId"] = body["tabId"] if "tabId" in body else _target_tab_id(request)
    if "zoom" in body:
        reply["zoom"] = body["zoom"]
    return reply


def _connect_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {"max_size": MAX_SIZE, "open_timeout": 5}
    # websockets >= 15 honours HTTP(S)_PROXY by default; the bridge is loopback only.
    if "proxy" in inspect.signature(ws_connect).parameters:
        kwargs["proxy"] = None
    return kwargs


@final
class FakeExtension:
    """Fake extension client. Starts connecting on construction.

    Attributes: ``requests`` (request frames, in arrival order), ``frames``
    (every parsed frame received), ``sent`` (every frame sent), ``welcome``
    (the welcome frame or ``None``), ``handler_errors``, ``closed_code``
    (``None`` while open; 1006 when the socket dropped without a close frame),
    ``closed_reason``, ``connect_error``.
    """

    def __init__(
        self,
        port: int,
        token: str,
        *,
        protocol: int = 2,
        handler: Handler | None = None,
        send_hello: bool = True,
        host: str = "127.0.0.1",
        connect_timeout: float = 10.0,
    ) -> None:
        self.port = port
        self.token = token
        self.protocol = protocol
        self.handler: Handler = handler or default_handler
        self.send_hello = send_hello
        self.url = f"ws://{host}:{port:d}"
        self.connect_timeout = connect_timeout

        self.requests: list[dict[str, Any]] = []
        self.frames: list[Any] = []
        self.sent: list[Any] = []
        self.handler_errors: list[BaseException] = []
        self.welcome: dict[str, Any] | None = None
        self.closed_code: int | None = None
        self.closed_reason: str | None = None
        self.connect_error: BaseException | None = None

        self._lock = threading.Lock()
        self._ws: ClientConnection | None = None
        self._connected = threading.Event()
        self._welcomed = threading.Event()
        self._closed = threading.Event()
        self._stopping = threading.Event()
        self._request_seen = threading.Condition(self._lock)
        self._thread = threading.Thread(
            target=self._run, name=f"FakeExtension-{port:d}", daemon=True
        )
        self._thread.start()

    # -- context manager -------------------------------------------------

    def __enter__(self) -> FakeExtension:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- waiting ---------------------------------------------------------

    def wait_connected(self, timeout: float = 5.0) -> bool:
        return self._connected.wait(timeout)

    def wait_welcomed(self, timeout: float = 5.0) -> bool:
        return self._welcomed.wait(timeout)

    def wait_closed(self, timeout: float = 5.0) -> bool:
        return self._closed.wait(timeout)

    def wait_requests(self, count: int, timeout: float = 5.0) -> bool:
        """Block until at least ``count`` requests have been recorded."""
        deadline = time.monotonic() + timeout
        with self._request_seen:
            while len(self.requests) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._request_seen.wait(remaining)
            return True

    @property
    def connected(self) -> bool:
        return self._connected.is_set() and not self._closed.is_set()

    @property
    def welcomed(self) -> bool:
        return self._welcomed.is_set() and not self._closed.is_set()

    # -- sending ---------------------------------------------------------

    def send_raw(self, obj: Any, timeout: float = 5.0) -> None:
        """Send ``obj`` as-is: ``str``/``bytes`` verbatim, anything else as JSON."""
        if not self._connected.wait(timeout):
            raise ConnectionError(f"FakeExtension is not connected to {self.url}")
        if isinstance(obj, (str, bytes)):
            payload = obj
        else:
            payload = json.dumps(obj)
        ws = self._ws
        if ws is None or self._closed.is_set():
            raise ConnectionError("FakeExtension connection is closed")
        ws.send(payload)
        with self._lock:
            self.sent.append(obj)

    def send_event(self, name: str, **fields: Any) -> None:
        frame: dict[str, Any] = {"type": "event", "event": name}
        frame.update(fields)
        self.send_raw(frame)

    def close(self, code: int = 1000, reason: str = "", timeout: float = 5.0) -> None:
        self._stopping.set()
        ws = self._ws
        if ws is not None and not self._closed.is_set():
            # The server may have dropped the socket already; closing is best effort.
            with contextlib.suppress(OSError, WebSocketException):
                ws.close(code, reason)
        self._thread.join(timeout)

    # -- reader thread ---------------------------------------------------

    def _open(self) -> ClientConnection:
        deadline = time.monotonic() + self.connect_timeout
        kwargs = _connect_kwargs()
        while True:
            try:
                return ws_connect(self.url, **kwargs)
            except (OSError, TimeoutError):
                # The server under test may not be listening yet.
                if self._stopping.is_set() or time.monotonic() >= deadline:
                    raise
                time.sleep(CONNECT_RETRY_INTERVAL)

    def _hello(self) -> dict[str, Any]:
        return {
            "type": "hello",
            "protocol": self.protocol,
            "token": self.token,
            "extensionVersion": EXTENSION_VERSION,
            "userAgent": USER_AGENT,
        }

    def _run(self) -> None:
        try:
            ws = self._open()
        except (
            OSError,
            WebSocketException,
        ) as exc:  # refused, timed out, bad handshake
            self.connect_error = exc
            self.closed_code = 1006
            self._closed.set()
            return
        self._ws = ws
        self._connected.set()
        # A connection used outside ``with`` is deprecated from websockets 17.1;
        # entering it also closes the socket however the reader ends.
        with ws:
            try:
                if self.send_hello:
                    self.send_raw(self._hello())
                for message in ws:
                    self._dispatch(message)
                # Iteration ends quietly on a 1000/1001 close; the sans-I/O
                # protocol object holds the close frame details.
                self._record_close(ws.protocol.close_code, ws.protocol.close_reason)
            except ConnectionClosed as exc:
                frame = exc.rcvd or exc.sent
                if frame is None:
                    self._record_close(1006, "")
                else:
                    self._record_close(frame.code, frame.reason)
            except Exception as exc:  # surfaced through handler_errors
                logger.debug("FakeExtension reader failed", exc_info=True)
                with self._lock:
                    self.handler_errors.append(exc)
                self._record_close(1006, "")
            finally:
                self._closed.set()

    def _record_close(self, code: int | None, reason: str | None) -> None:
        if self.closed_code is None:
            self.closed_code = 1006 if code is None else code
            self.closed_reason = reason or ""

    def _dispatch(self, message: Any) -> None:
        try:
            frame = json.loads(message)
        except (TypeError, ValueError):
            with self._lock:
                self.frames.append(message)
            return
        with self._lock:
            self.frames.append(frame)
        if not isinstance(frame, dict):
            return
        kind = frame.get("type")
        if kind == "welcome":
            self.welcome = frame
            self._welcomed.set()
        elif kind == "request":
            with self._request_seen:
                self.requests.append(frame)
                self._request_seen.notify_all()
            if self._welcomed.is_set():
                self._answer(frame)

    def _answer(self, request: dict[str, Any]) -> None:
        try:
            body = self.handler(request)
            if body is None:
                body = default_handler(request)
        except Exception as exc:  # mirrors the worker's `internal` reply
            logger.debug("FakeExtension handler raised", exc_info=True)
            with self._lock:
                self.handler_errors.append(exc)
            body = {"ok": False, "error": {"code": "internal", "message": str(exc)}}
        if body.get("noreply"):
            return
        try:
            self.send_raw(build_reply(request, body))
        except (ConnectionClosed, ConnectionError):
            pass
