"""Attach-mode bridge server: the port-bound, server-first handshake for one install.

Contract: ``docs/architecture/attach-mode.md`` section 3 (handshake). The worker
side (``emunium/extension/background_state.js``, ``attachHandshake``) is the
reference: every MAC is HMAC-SHA-256 keyed by the UTF-8 secret over
``label|sN|cN|P[|extra]``, encoded as unpadded base64url, ``P`` being the port
of this server's own bound socket.

Unlike section 3, install tokens are stored in clear in ``hub.json``: the hub
must hold the token itself to verify an HMAC keyed by it, and the file is
private and already holds the enrollment secret in clear.

After the welcome a connection serves requests exactly like a launch-mode
``Transport``. One ``AttachTransport`` serves one install at a time.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import functools
import hashlib
import hmac
import json
import logging
import re
import secrets
import socket
import sys
import threading
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, NamedTuple

from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed
from websockets.typing import Data

from emunium._attach.build import (
    ENROLLMENT_RE,
    ExtensionBuildError,
    _validate_port,
    load_install,
    record_install,
)
from emunium._bridge.transport import (
    CLOSE_UNAUTHORIZED,
    EXTENSION_ORIGIN_PREFIX,
    FIREFOX_EXTENSION_ORIGIN_PREFIX,
    PROTOCOL_VERSION,
    Transport,
    _close_quietly,
)

__all__ = ["CLOSE_BUSY", "CLOSE_HANDSHAKE", "AttachTransport"]

logger = logging.getLogger("emunium.bridge")

LOOPBACK = "127.0.0.1"
# Module-level so tests can shorten them; read at call time.
CLIENT_NONCE_TIMEOUT = 5.0
HELLO_TIMEOUT = 10.0
LISTEN_BACKLOG = 100

NONCE_BYTES = 32
MAC_BYTES = 32
TOKEN_BYTES = 32
MAX_USER_AGENT = 1024

# Frame out of order, malformed or late (the worker uses the same code).
CLOSE_HANDSHAKE = 4005
# Another install owns this session.
CLOSE_BUSY = 4009
CLOSE_INTERNAL = 1011

_BASE64URL_RE = re.compile(r"[A-Za-z0-9_-]*")
# Same pattern as isUuid() in background_state.js.
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: Any) -> bytes | None:
    """Bytes of unpadded base64url ``text``; ``None`` for anything else (base64UrlDecode)."""
    if not isinstance(text, str) or not _BASE64URL_RE.fullmatch(text):
        return None
    if len(text) % 4 == 1:
        return None
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None


def _fixed_bytes(text: Any, size: int) -> bytes | None:
    data = _b64url_decode(text)
    return data if data is not None and len(data) == size else None


def _handshake_message(
    label: str, server_nonce: str, client_nonce: str, port: int, *extra: str
) -> str:
    return "|".join((label, server_nonce, client_nonce, str(port), *extra))


def _mac(secret: str, message: str) -> bytes:
    return hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).digest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _Admission(NamedTuple):
    """An admitted hello: the token to put in the welcome (``""`` for none) and,
    when this handshake bound the session, its ticket for rolling that back."""

    issued: str
    ticket: int | None


class AttachTransport(Transport):
    """Bridge server for one persistent install (attach-mode.md section 3).

    ``state`` is a ``load_or_create_state()`` dict. With ``install_id`` only
    that install is accepted; otherwise the first install to complete the
    handshake is bound (``bound_install_id``) and any other one is closed 4009.
    """

    def __init__(
        self,
        state: dict[str, Any],
        *,
        port: int | None = None,
        install_id: str | None = None,
    ) -> None:
        enrollment = state.get("enrollment")
        if not isinstance(enrollment, str) or not ENROLLMENT_RE.fullmatch(enrollment):
            raise ExtensionBuildError("state carries no valid enrollment secret")
        extension_id = state.get("extension_id")
        if not isinstance(extension_id, str) or not extension_id:
            raise ExtensionBuildError("state carries no extension id")
        if install_id is not None and not (
            isinstance(install_id, str) and _UUID_RE.fullmatch(install_id)
        ):
            raise ValueError(f"install_id must be a UUID, got {install_id!r}")
        super().__init__(LOOPBACK, _validate_port(port or state["port"]))
        self._enrollment = enrollment
        self._extension_origin = EXTENSION_ORIGIN_PREFIX + extension_id
        self._hub_path = str(state["hub_path"])
        self.allow_origin = self._attach_origin_allowed
        self._bind_lock = threading.Lock()
        self._bound_install_id: str | None = install_id
        # Serializes install lookup, binding and hub.json writes across sockets.
        self._install_lock = asyncio.Lock()
        # Handshakes admitted so far (loop thread only); tickets for _release.
        self._admissions = 0

    @property
    def bound_install_id(self) -> str | None:
        with self._bind_lock:
            return self._bound_install_id

    def _attach_origin_allowed(self, origin: str | None) -> bool:
        if origin is None:
            return False
        if origin == self._extension_origin:
            return True
        return origin.startswith(FIREFOX_EXTENSION_ORIGIN_PREFIX) and len(origin) > len(
            FIREFOX_EXTENSION_ORIGIN_PREFIX
        )

    def _may_serve(self, install_id: str) -> bool:
        with self._bind_lock:
            bound = self._bound_install_id
            return bound is None or bound == install_id

    def _bind(self, install_id: str) -> bool:
        """Bind an unbound session; True when this call bound it."""
        with self._bind_lock:
            if self._bound_install_id is not None:
                return False
            self._bound_install_id = install_id
        logger.info("Attach session bound to install %s", install_id)
        return True

    def _listen_socket(self) -> socket.socket:
        """127.0.0.1 only, never shared: no other socket can bind or shadow the port."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if sys.platform == "win32":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                # Rebinds over TIME_WAIT yet refuses a listening port (no SO_REUSEPORT).
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((LOOPBACK, self.port))
            sock.listen(LISTEN_BACKLOG)
            sock.setblocking(False)
        except BaseException:
            sock.close()
            raise
        return sock

    # -- handshake (loop thread) -------------------------------------------

    async def _next_frame(
        self,
        ws: ServerConnection,
        frames: AsyncIterator[Data],
        expected: str,
        timeout: float,
    ) -> dict[str, Any] | None:
        """The next frame when it is a JSON object of type ``expected``; else close 4005."""
        try:
            raw = await asyncio.wait_for(frames.__anext__(), timeout)
        except asyncio.TimeoutError:
            logger.warning("Attach handshake: no %s within %.1f s", expected, timeout)
            await _close_quietly(ws, CLOSE_HANDSHAKE, "handshake timeout")
            return None
        except (StopAsyncIteration, ConnectionClosed):
            return None
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError, RecursionError):
            frame = None
        if not isinstance(frame, dict) or frame.get("type") != expected:
            logger.warning("Attach handshake: expected a %s frame", expected)
            await _close_quietly(ws, CLOSE_HANDSHAKE, "unexpected frame")
            return None
        return frame

    async def _send(self, ws: ServerConnection, frame: dict[str, Any]) -> bool:
        try:
            await ws.send(json.dumps(frame))
        except ConnectionClosed:
            return False
        return True

    async def _accept(
        self, ws: ServerConnection, frames: AsyncIterator[Data]
    ) -> dict[str, Any] | None:
        port = self._actual_port
        if port is None:  # the server publishes its port before it accepts
            await _close_quietly(ws, CLOSE_INTERNAL, "not ready")
            return None

        # Steps 1-2: challenge, then the clientNonce frame (validated before any MAC).
        server_nonce = _b64url_encode(secrets.token_bytes(NONCE_BYTES))
        challenge = {
            "type": "challenge",
            "protocol": PROTOCOL_VERSION,
            "serverNonce": server_nonce,
        }
        if not await self._send(ws, challenge):
            return None
        frame = await self._next_frame(ws, frames, "clientNonce", CLIENT_NONCE_TIMEOUT)
        if frame is None:
            return None
        client_nonce = frame.get("clientNonce")
        if not isinstance(client_nonce, str) or (
            _fixed_bytes(client_nonce, NONCE_BYTES) is None
        ):
            logger.warning("Attach handshake: malformed clientNonce")
            await _close_quietly(ws, CLOSE_HANDSHAKE, "bad clientNonce")
            return None

        # Step 3: the server proves the enrollment secret first, bound to its port.
        server_proof = _mac(
            self._enrollment,
            _handshake_message("emunium-server", server_nonce, client_nonce, port),
        )
        if not await self._send(
            ws, {"type": "serverProof", "serverProof": _b64url_encode(server_proof)}
        ):
            return None

        # Step 4: the hello, shape-checked before its MACs are computed.
        hello = await self._next_frame(ws, frames, "hello", HELLO_TIMEOUT)
        if hello is None:
            return None
        install_id = hello.get("installId")
        protocol = hello.get("protocol")
        proof = _fixed_bytes(hello.get("proof"), MAC_BYTES)
        raw_token_proof = hello.get("tokenProof")
        token_proof = (
            None
            if raw_token_proof is None
            else _fixed_bytes(raw_token_proof, MAC_BYTES)
        )
        if (
            not isinstance(protocol, int)
            or isinstance(protocol, bool)
            or protocol != PROTOCOL_VERSION
            or hello.get("mode") != "attach"
            or not isinstance(install_id, str)
            or not _UUID_RE.fullmatch(install_id)
            or proof is None
            or (raw_token_proof is not None and token_proof is None)
        ):
            logger.warning("Attach handshake: malformed hello")
            await _close_quietly(ws, CLOSE_HANDSHAKE, "bad hello")
            return None

        # Step 5: enrollment proof, then the install's token.
        expected_proof = _mac(
            self._enrollment,
            _handshake_message(
                "emunium-client", server_nonce, client_nonce, port, install_id
            ),
        )
        if not hmac.compare_digest(proof, expected_proof):
            logger.warning("Attach handshake: bad enrollment proof")
            await _close_quietly(ws, CLOSE_UNAUTHORIZED, "unauthorized")
            return None
        user_agent = hello.get("userAgent")
        user_agent = user_agent[:MAX_USER_AGENT] if isinstance(user_agent, str) else ""
        token_message = _handshake_message(
            "emunium-token", server_nonce, client_nonce, port
        )
        admission = await self._admit(
            ws, install_id, token_proof, token_message, user_agent
        )
        if admission is None:
            return None
        issued = admission.issued

        # Step 6: the welcome, its MAC covering the issued token (or "").
        welcome_mac = _mac(
            self._enrollment,
            _handshake_message(
                "emunium-welcome", server_nonce, client_nonce, port, install_id, issued
            ),
        )
        welcome: dict[str, Any] = {
            "type": "welcome",
            "protocol": PROTOCOL_VERSION,
            "installId": install_id,
        }
        if issued:
            welcome["token"] = issued
        welcome["welcomeMac"] = _b64url_encode(welcome_mac)
        if not await self._send(ws, welcome):
            # The install never learned it was accepted: do not keep the session for it.
            self._release(install_id, admission.ticket)
            return None
        return hello

    def _release(self, install_id: str, ticket: int | None) -> None:
        """Undo the bind made by admission ``ticket`` unless something now relies on it.

        Loop thread only. A later admission of the same install, or an active
        connection, keeps the session bound.
        """
        if ticket is None or ticket != self._admissions or self._has_active():
            return
        with self._bind_lock:
            if self._bound_install_id == install_id:
                self._bound_install_id = None
                logger.info(
                    "Attach session unbound: welcome to %s not sent", install_id
                )

    async def _admit(
        self,
        ws: ServerConnection,
        install_id: str,
        token_proof: bytes | None,
        token_message: str,
        user_agent: str,
    ) -> _Admission | None:
        """Apply the session and token policy; ``None`` once the socket was closed."""
        async with self._install_lock:
            admission, close_code, reason = await self._admit_locked(
                install_id, token_proof, token_message, user_agent
            )
        if admission is None:
            # Closed outside the lock: a slow close handshake must not stall other sockets.
            await _close_quietly(ws, close_code, reason)
        return admission

    async def _admit_locked(
        self,
        install_id: str,
        token_proof: bytes | None,
        token_message: str,
        user_agent: str,
    ) -> tuple[_Admission | None, int, str]:
        """``(admission, close_code, reason)``; no admission means the socket must close.

        Token policy: no stored token, or a pending (unconfirmed) one with no
        ``tokenProof`` -> a fresh pending token, since the welcome carrying the
        old one may never have reached the extension. A valid ``tokenProof``
        confirms the stored token. A wrong ``tokenProof``, or none against a
        confirmed token -> 4001.
        """
        if not self._may_serve(install_id):
            logger.warning("Attach session busy; rejecting install %s", install_id)
            return None, CLOSE_BUSY, "busy"
        loop = asyncio.get_running_loop()
        try:
            record = await loop.run_in_executor(
                None, load_install, self._hub_path, install_id
            )
        except (OSError, ExtensionBuildError):
            logger.exception("Cannot read hub state")
            return None, CLOSE_INTERNAL, "hub state unavailable"
        stored = record.get("token") if record is not None else None
        if not isinstance(stored, str) or not stored:
            stored = None
        # Only an explicit false is pending: a record without the flag is trusted as confirmed.
        pending = record is not None and record.get("confirmed") is False
        if stored is not None and token_proof is not None:
            if not hmac.compare_digest(token_proof, _mac(stored, token_message)):
                logger.warning(
                    "Attach handshake: install %s presented a wrong token proof",
                    install_id,
                )
                return None, CLOSE_UNAUTHORIZED, "unauthorized"
            token, issued, confirmed = stored, "", True
        elif stored is not None and not pending:
            logger.warning(
                "Attach handshake: install %s presented no token proof", install_id
            )
            return None, CLOSE_UNAUTHORIZED, "unauthorized"
        else:
            token = secrets.token_urlsafe(TOKEN_BYTES)
            issued, confirmed = token, False
        write = functools.partial(
            record_install,
            self._hub_path,
            install_id,
            token=token,
            confirmed=confirmed,
            user_agent=user_agent,
            now=_now(),
        )
        try:
            await loop.run_in_executor(None, write)
        except (OSError, ExtensionBuildError):
            logger.exception("Cannot store install %s", install_id)
            return None, CLOSE_INTERNAL, "hub state unavailable"
        bound_here = self._bind(install_id)
        self._admissions += 1
        ticket = self._admissions if bound_here else None
        return _Admission(issued, ticket), 0, ""
