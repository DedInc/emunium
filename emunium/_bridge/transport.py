"""WebSocket server side of bridge protocol v2.

Contracts: ``docs/architecture/bridge-protocol.md`` sections 1-5 (wire format)
and ``docs/architecture/python-internals.md`` (``Transport``, ``Reply``,
``PINNED``).

Requires websockets 14+ (``websockets.asyncio.server``); the legacy server API
is not supported.

Threading model: one asyncio loop thread owns the server and every socket; one
event thread runs user event handlers so a handler that calls ``request()`` can
never block the loop that would deliver its reply. Callers block on
``concurrent.futures.Future`` objects resolved from the loop thread.

``_Connection`` owns exactly one welcomed socket (pending map, request/reply,
event hand-off, fail-all on close). ``Transport`` owns the server, the hello
check and the single-active-connection policy, so a future multi-connection hub
can reuse ``_Connection`` unchanged.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hmac
import itertools
import json
import logging
import queue
import secrets
import socket
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.typing import Data

from emunium.errors import (
    BridgeDisconnectedError,
    BridgeError,
    BridgeProtocolError,
    BridgeTimeoutError,
    error_from_reply,
)

__all__ = ["MAX_SIZE", "PINNED", "PROTOCOL_VERSION", "Reply", "Transport"]

logger = logging.getLogger("emunium.bridge")

PROTOCOL_VERSION = 2
MAX_SIZE = 64 * 1024 * 1024
# Module-level so tests can shorten them; read at call time.
HELLO_TIMEOUT = 5.0
RECONNECT_WAIT = 15.0
SHUTDOWN_TIMEOUT = 5.0

MAX_QUEUE = 2  # bounds frames buffered per socket, including unauthenticated ones

CLOSE_UNAUTHORIZED = 4001
CLOSE_SUPERSEDED = 4002
CLOSE_FORBIDDEN_ORIGIN = 4003
CLOSE_GOING_AWAY = 1001

EXTENSION_ORIGIN_PREFIX = "chrome-extension://"
FIREFOX_EXTENSION_ORIGIN_PREFIX = "moz-extension://"
EXTENSION_ORIGIN_PREFIXES = (EXTENSION_ORIGIN_PREFIX, FIREFOX_EXTENSION_ORIGIN_PREFIX)

PINNED: Any = object()  # sentinel: use Transport.pinned_tab_id

EventHandler = Callable[[dict], None]


def _default_allow_origin(origin: str | None) -> bool:
    """No Origin (CLI tools, tests) or an extension origin is allowed; web pages are not.

    The per-session token still authenticates every connection; the Origin
    check only keeps web pages (and opaque ``null`` origins) out.
    """
    return origin is None or origin.startswith(EXTENSION_ORIGIN_PREFIXES)


def _request_origin(ws: ServerConnection) -> str | None:
    """The handshake's Origin header, ``None`` when absent.

    websockets already refused a handshake with repeated Origin headers
    (HTTP 400), so at most one value reaches the handler.
    """
    request = ws.request
    if request is None:  # the handler only runs after the handshake
        return None
    return request.headers.get("Origin")


@dataclass(frozen=True)
class Reply:
    result: Any
    tab_id: int | None
    zoom: float | None


def _as_tab_id(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _as_zoom(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


class _NotSent(BridgeDisconnectedError):
    """The request was never registered or written; safe to retry anywhere."""


class _Connection:
    """One welcomed socket. All socket I/O happens on ``loop``."""

    def __init__(
        self,
        ws: ServerConnection,
        hello: dict[str, Any],
        loop: asyncio.AbstractEventLoop,
        on_event: Callable[[_Connection, dict[str, Any]], None],
        on_close: Callable[[_Connection], None],
    ) -> None:
        self.ws = ws
        self.hello = hello
        self._loop = loop
        self._on_event = on_event
        self._on_close = on_close
        self._lock = threading.Lock()
        self._pending: dict[int, concurrent.futures.Future[dict[str, Any]]] = {}
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    # -- requests (any thread) -------------------------------------------

    def submit(
        self, request_id: int, frame: dict[str, Any]
    ) -> concurrent.futures.Future[dict[str, Any]]:
        """Register ``request_id`` and schedule the write; the future gets the reply frame.

        Raises ``_NotSent`` when the frame was neither registered nor written,
        so the caller may retry on another connection regardless of idempotency.
        """
        text = json.dumps(frame)
        future: concurrent.futures.Future[dict[str, Any]] = concurrent.futures.Future()
        with self._lock:
            if self._closed:
                raise _NotSent("extension connection is closed")
            self._pending[request_id] = future
        try:
            sent = asyncio.run_coroutine_threadsafe(self.ws.send(text), self._loop)
        except RuntimeError as exc:  # loop already closed: this socket is unusable
            self.discard(request_id)
            self.fail_all(BridgeDisconnectedError("bridge loop is not running"))
            raise _NotSent(f"bridge loop is not running: {exc}") from exc
        sent.add_done_callback(lambda done: self._after_send(request_id, done))
        return future

    def _after_send(
        self, request_id: int, done: concurrent.futures.Future[None]
    ) -> None:
        # ``request_id`` is failed by fail_all together with the rest: failing it
        # first would wake a retrying caller while this conn still looks open.
        if done.cancelled():  # loop shutting down; nothing to log or close
            self.fail_all(BridgeDisconnectedError("send was cancelled"))
            return
        exc = done.exception()
        if exc is None:
            return
        # A failed write means the socket is dying: retire the whole connection so
        # retries wait for a new one instead of reusing it.
        if not self.fail_all(BridgeDisconnectedError(f"send failed: {exc}")):
            return  # already retired by an earlier failure or close
        logger.warning("Socket write failed; dropping connection: %s", exc)
        try:
            asyncio.run_coroutine_threadsafe(
                self._close_socket(1011, "send failed"), self._loop
            )
        except RuntimeError:
            logger.debug("Bridge loop closed before close(1011) could be scheduled")

    def discard(self, request_id: int) -> None:
        with self._lock:
            self._pending.pop(request_id, None)

    def fail_all(self, error: BaseException) -> bool:
        """Mark closed, release the active slot, fail every pending request.

        Idempotent; returns True only for the call that retired the connection.
        """
        with self._lock:
            first = not self._closed
            self._closed = True
            pending = list(self._pending.values())
            self._pending.clear()
        # Before failing futures, so a retrying caller never sees this conn as active.
        self._on_close(self)
        for future in pending:
            future.set_exception(error)
        return first

    # -- loop side -------------------------------------------------------

    async def run(self, frames: AsyncIterator[Data]) -> None:
        """Read frames until the socket closes, then fail everything pending."""
        try:
            async for raw in frames:
                self._handle_frame(raw)
        except ConnectionClosed:
            logger.debug("Extension socket closed with an error")
        finally:
            self.fail_all(BridgeDisconnectedError("extension disconnected"))

    async def _close_socket(self, code: int, reason: str) -> None:
        await _close_quietly(self.ws, code, reason)

    async def close(self, code: int, reason: str) -> None:
        self.fail_all(BridgeDisconnectedError(f"extension connection closed: {reason}"))
        await self._close_socket(code, reason)

    def _handle_frame(self, raw: Data) -> None:
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError, RecursionError):
            logger.warning("Ignoring non-JSON frame from extension")
            return
        if not isinstance(frame, dict):
            logger.warning("Ignoring non-object frame from extension")
            return
        kind = frame.get("type")
        if kind == "reply":
            request_id = frame.get("id")
            with self._lock:
                future = (
                    self._pending.pop(request_id, None)
                    if isinstance(request_id, int)
                    else None
                )
            if future is None:
                logger.debug("Reply for unknown request id %r", request_id)
            else:
                future.set_result(frame)
        elif kind == "event":
            self._on_event(self, frame)
        else:
            logger.debug("Ignoring frame of type %r", kind)


async def _close_quietly(ws: ServerConnection, code: int, reason: str) -> None:
    try:
        await ws.close(code, reason)
    except Exception:  # the socket may already be gone; closing is best effort
        logger.debug("close(%d) failed", code, exc_info=True)


class Transport:
    """Single-connection bridge server. Public methods are thread-safe."""

    def __init__(
        self, host: str = "127.0.0.1", port: int = 0, *, token: str | None = None
    ) -> None:
        self.host = host
        self.port = port
        self.token: str = token if token is not None else secrets.token_urlsafe(32)
        # Replaceable checks; both run on the loop thread and must not block.
        # ``authenticate(hello) -> bool`` (see _default_authenticate).
        self.authenticate: Callable[[dict[str, Any]], bool] = self._default_authenticate
        # ``allow_origin(origin: str | None) -> bool``, checked before the hello.
        self.allow_origin: Callable[[str | None], bool] = _default_allow_origin

        self._cond = threading.Condition(threading.Lock())
        self._active: _Connection | None = None
        self._started = False
        self._shut = False

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: Server | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._actual_port: int | None = None

        self._ids = itertools.count(1)
        self._id_lock = threading.Lock()

        self._pin_lock = threading.Lock()
        self._pinned_tab_id: int | None = None

        self._handlers_lock = threading.Lock()
        self._handlers: dict[str, list[EventHandler]] = {}
        self._events: queue.Queue[tuple[list[EventHandler], dict[str, Any]] | None] = (
            queue.Queue()
        )
        self._event_thread: threading.Thread | None = None

    # -- properties ------------------------------------------------------

    @property
    def actual_port(self) -> int | None:
        return self._actual_port

    @property
    def pinned_tab_id(self) -> int | None:
        with self._pin_lock:
            return self._pinned_tab_id

    @pinned_tab_id.setter
    def pinned_tab_id(self, value: int | None) -> None:
        with self._pin_lock:
            self._pinned_tab_id = value

    @property
    def connected(self) -> bool:
        active = self._active
        return active is not None and not active.closed

    # -- lifecycle -------------------------------------------------------

    def start(self, timeout: float = 30.0) -> None:
        with self._cond:
            if self._shut:
                raise BridgeError("transport was shut down")
            if self._started:
                return
            self._started = True
        self._event_thread = threading.Thread(
            target=self._run_events, daemon=True, name="emun-bridge-events"
        )
        self._event_thread.start()
        loop = asyncio.new_event_loop()
        self._loop = loop
        self._thread = threading.Thread(
            target=self._run_loop, args=(loop,), daemon=True, name="emun-bridge"
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            self.shutdown()
            raise BridgeError(f"bridge server did not start within {timeout:.1f} s")
        if self._start_error is not None:
            error = self._start_error
            self.shutdown()
            raise BridgeError(
                f"cannot listen on {self.host}:{self.port}: {error}"
            ) from error
        logger.info("Bridge listening on %s:%d", self.host, self._actual_port)

    def shutdown(self) -> None:
        with self._cond:
            if self._shut:
                return
            self._shut = True
            active, self._active = self._active, None
            self._cond.notify_all()
        if active is not None:
            active.fail_all(BridgeDisconnectedError("transport shut down"))
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None:
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                logger.debug("Bridge loop already closed at shutdown")
        current = threading.current_thread()
        if self._thread is not None and self._thread is not current:
            self._thread.join(SHUTDOWN_TIMEOUT * 2)
        self._events.put(None)
        if self._event_thread is not None and self._event_thread is not current:
            self._event_thread.join(SHUTDOWN_TIMEOUT)
        logger.info("Bridge stopped")

    def wait_for_connection(self, timeout: float = 60.0) -> bool:
        with self._cond:
            self._cond.wait_for(lambda: self._shut or self._has_active(), timeout)
            return not self._shut and self._has_active()

    def on(self, event: str, handler: EventHandler) -> None:
        with self._handlers_lock:
            self._handlers.setdefault(event, []).append(handler)

    # -- requests --------------------------------------------------------

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        tab_id: Any = PINNED,
        timeout: float = 30.0,
        idempotent: bool = False,
    ) -> Reply:
        if self._thread is not None and threading.current_thread() is self._thread:
            raise BridgeError("request() cannot be called from the bridge loop thread")
        deadline = time.monotonic() + timeout
        resent = False
        while True:
            conn = self._wait_active(deadline)
            if time.monotonic() >= deadline:
                # Never send a command after the caller's deadline.
                raise BridgeTimeoutError(
                    f"no reply to {method} within {timeout:.1f} s", method=method
                )
            target_tab = self.pinned_tab_id if tab_id is PINNED else tab_id
            with self._id_lock:
                request_id = next(self._ids)
            frame = {
                "type": "request",
                "id": request_id,
                "method": method,
                "params": params if params is not None else {},
                "target": {"tabId": target_tab, "frameId": 0},
            }
            try:
                future = conn.submit(request_id, frame)
            except _NotSent:
                # Closed between _wait_active and submit; nothing went out.
                continue
            try:
                reply = future.result(max(0.0, deadline - time.monotonic()))
            except concurrent.futures.TimeoutError as exc:
                conn.discard(request_id)
                raise BridgeTimeoutError(
                    f"no reply to {method} within {timeout:.1f} s", method=method
                ) from exc
            except BridgeDisconnectedError as exc:
                if idempotent and not resent:
                    resent = True
                    logger.info("Re-sending idempotent %s after disconnect", method)
                    continue
                raise BridgeDisconnectedError(exc.message, method=method) from exc
            return self._interpret(method, reply)

    @staticmethod
    def _interpret(method: str, reply: dict[str, Any]) -> Reply:
        ok = reply.get("ok")
        if ok is True:
            return Reply(
                result=reply.get("result"),
                tab_id=_as_tab_id(reply.get("tabId")),
                zoom=_as_zoom(reply.get("zoom")),
            )
        if ok is False:
            error = reply.get("error")
            if isinstance(error, dict):
                code = error.get("code") or "internal"
                message = error.get("message") or ""
            elif isinstance(error, str):
                code, message = error, error
            else:
                code, message = "internal", ""
            raise error_from_reply(str(code), str(message), method)
        raise BridgeProtocolError(
            f"reply to {method} has no boolean 'ok'", method=method
        )

    def _has_active(self) -> bool:
        return self._active is not None and not self._active.closed

    def _wait_active(self, deadline: float) -> _Connection:
        if self._loop is None:
            raise BridgeDisconnectedError("transport is not started")
        wait_until = min(deadline, time.monotonic() + RECONNECT_WAIT)
        with self._cond:
            while True:
                if self._shut:
                    raise BridgeDisconnectedError("transport shut down")
                active = self._active
                if active is not None:
                    if not active.closed:
                        return active
                    self._active = None  # never hand out a closed connection
                remaining = wait_until - time.monotonic()
                if remaining <= 0:
                    raise BridgeDisconnectedError("extension is not connected")
                self._cond.wait(remaining)

    # -- hello / connection policy (loop thread) -------------------------

    def _default_authenticate(self, hello: dict[str, Any]) -> bool:
        """Default ``authenticate(hello) -> bool``: token and protocol must match.

        A replacement must be fast and non-blocking: it runs on the bridge loop
        thread, so any I/O or lock wait stalls every connection. Raising rejects.
        """
        token = hello.get("token")
        protocol = hello.get("protocol")
        return (
            isinstance(token, str)
            and hmac.compare_digest(token.encode("utf-8"), self.token.encode("utf-8"))
            and isinstance(protocol, int)
            and not isinstance(protocol, bool)
            and protocol == PROTOCOL_VERSION
        )

    def _origin_allowed(self, ws: ServerConnection) -> bool:
        origin = _request_origin(ws)
        try:
            allowed = bool(self.allow_origin(origin))
        except Exception:  # a broken policy must reject, not crash
            logger.exception("allow_origin() raised; rejecting connection")
            return False
        if not allowed:
            logger.warning("Rejecting connection from origin %r", origin)
        return allowed

    async def _handle_socket(self, ws: ServerConnection) -> None:
        if self._shut:
            await _close_quietly(ws, CLOSE_GOING_AWAY, "shutting down")
            return
        if not self._origin_allowed(ws):
            await _close_quietly(ws, CLOSE_FORBIDDEN_ORIGIN, "origin not allowed")
            return
        frames = ws.__aiter__()
        hello = await self._accept(ws, frames)
        if hello is None:
            return
        loop = asyncio.get_running_loop()
        conn = _Connection(ws, hello, loop, self._on_event, self._on_close)
        with self._cond:
            if self._shut:
                previous = None
                rejected = True
            else:
                previous, self._active = self._active, conn
                rejected = False
                self._cond.notify_all()
        if rejected:
            await _close_quietly(ws, CLOSE_GOING_AWAY, "shutting down")
            return
        logger.info("Extension connected (%s)", hello.get("extensionVersion"))
        if previous is not None:
            # Fail the old one's requests now; its close handshake runs on its own.
            previous.fail_all(BridgeDisconnectedError("superseded by a new connection"))
            asyncio.ensure_future(previous.close(CLOSE_SUPERSEDED, "superseded"))
        await conn.run(frames)

    async def _accept(
        self, ws: ServerConnection, frames: AsyncIterator[Data]
    ) -> dict[str, Any] | None:
        """Read the opening frames, authenticate and send the welcome.

        Returns the hello to publish the connection with, or ``None`` once the
        socket was closed (or is closing). Runs on the loop thread after the
        Origin check. The default is the launch-mode token hello.
        """
        try:
            raw = await asyncio.wait_for(frames.__anext__(), HELLO_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("Extension sent no hello within %.1f s", HELLO_TIMEOUT)
            await _close_quietly(ws, CLOSE_UNAUTHORIZED, "hello timeout")
            return None
        except (StopAsyncIteration, ConnectionClosed):
            return None
        hello = self._parse_hello(raw)
        if hello is None:
            await _close_quietly(ws, CLOSE_UNAUTHORIZED, "unauthorized")
            return None
        try:
            await ws.send(json.dumps({"type": "welcome", "protocol": PROTOCOL_VERSION}))
        except ConnectionClosed:
            return None
        return hello

    def _parse_hello(self, raw: Data) -> dict[str, Any] | None:
        try:
            hello = json.loads(raw)
        except (TypeError, ValueError, RecursionError):
            logger.warning("Rejecting extension: first frame is not JSON")
            return None
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            logger.warning("Rejecting extension: first frame is not a hello")
            return None
        try:
            accepted = bool(self.authenticate(hello))
        except Exception:  # a broken check must reject, not crash
            logger.exception("authenticate() raised; rejecting extension")
            return None
        if not accepted:
            logger.warning("Rejecting extension: bad token or protocol")
            return None
        return hello

    def _on_close(self, conn: _Connection) -> None:
        with self._cond:
            # A superseded connection's late close must not clear the newer one.
            if self._active is conn:
                self._active = None
                self._cond.notify_all()
                logger.info("Extension disconnected")

    # -- events ----------------------------------------------------------

    def _on_event(self, conn: _Connection, frame: dict[str, Any]) -> None:
        name = frame.get("event")
        if name == "tabRemoved":
            removed = _as_tab_id(frame.get("tabId"))
            with self._pin_lock:
                if removed is not None and removed == self._pinned_tab_id:
                    self._pinned_tab_id = None
        with self._handlers_lock:
            handlers = (
                list(self._handlers.get(name, ())) if isinstance(name, str) else []
            )
        if handlers:
            self._events.put((handlers, frame))

    def _run_events(self) -> None:
        while True:
            item = self._events.get()
            if item is None:
                return
            handlers, frame = item
            for handler in handlers:
                try:
                    handler(frame)
                except Exception:  # handlers are isolated
                    logger.exception("Handler for event %r raised", frame.get("event"))

    # -- loop thread -----------------------------------------------------

    def _run_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
            tasks = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in tasks:
                task.cancel()
            if tasks:
                loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        finally:
            loop.close()

    def _listen_socket(self) -> socket.socket | None:
        """A bound, listening socket to serve on; ``None`` lets ``serve`` bind ``host:port``.

        Runs on the loop thread inside ``start()``; an exception is raised from
        ``start()`` as ``BridgeError``.
        """
        return None

    async def _serve(self) -> None:
        self._stop = asyncio.Event()
        sock: socket.socket | None = None
        try:
            sock = self._listen_socket()
            if sock is None:
                server = await serve(
                    self._handle_socket,
                    self.host,
                    self.port,
                    max_size=MAX_SIZE,
                    max_queue=MAX_QUEUE,
                )
            else:
                server = await serve(
                    self._handle_socket,
                    sock=sock,
                    max_size=MAX_SIZE,
                    max_queue=MAX_QUEUE,
                )
        except Exception as exc:  # surfaced from start()
            logger.debug("serve() failed", exc_info=True)
            if sock is not None:
                sock.close()
            self._start_error = exc
            self._ready.set()
            return
        self._server = server
        for sock in server.sockets:
            self._actual_port = sock.getsockname()[1]
            break
        self._ready.set()
        if self._shut:
            self._stop.set()
        await self._stop.wait()
        server.close()
        try:
            await asyncio.wait_for(server.wait_closed(), SHUTDOWN_TIMEOUT)
        except Exception:  # shutdown proceeds either way
            logger.debug("server did not close cleanly", exc_info=True)
