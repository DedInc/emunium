"""Attach-mode server handshake (attach-mode.md section 3) against a Python worker client.

``Worker`` mirrors ``attachHandshake`` in ``emunium/extension/background_state.js``:
the same MAC labels, ``label|sN|cN|P[|extra]`` messages, HMAC-SHA-256 keyed by
the UTF-8 secret and unpadded base64url. Every test runs a real
``AttachTransport`` on 127.0.0.1 with ``EMUNIUM_HOME`` in a temp dir.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, final

import pytest

pytest.importorskip("websockets", minversion="14.0")
pytest.importorskip(
    "cryptography", reason="hub state (RSA extension key) needs cryptography"
)

from fake_extension import _connect_kwargs
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection
from websockets.sync.client import connect as ws_connect
from websockets.typing import Origin

from emunium import errors
from emunium._attach import server as server_mod
from emunium._attach.build import build_extension, load_or_create_state
from emunium._attach.server import AttachTransport

RECV_TIMEOUT = 5.0
USER_AGENT = "Mozilla/5.0 (Worker) Chrome/130.0.0.0"


# -- worker-side crypto (background_state.js) --------------------------------


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hmac_sha256(secret: str, message: str) -> bytes:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()


def handshake_message(label: str, sn: str, cn: str, port: int, *extra: str) -> str:
    return "|".join([label, sn, cn, str(port), *extra])


def verify_mac(secret: str, message: str, received: Any) -> bool:
    if not isinstance(received, str):
        return False
    data = b64url_decode(received)
    return len(data) == 32 and hmac.compare_digest(data, hmac_sha256(secret, message))


def random_nonce() -> str:
    return b64url(os.urandom(32))


@final
class Worker:
    """One worker socket, driven step by step from the test thread."""

    def __init__(
        self,
        port: int,
        enrollment: str,
        origin: str | None,
        *,
        install_id: str | None = None,
        token: str | None = None,
    ) -> None:
        self.port = port
        self.enrollment = enrollment
        self.install_id = install_id or str(uuid.uuid4())
        self.token = token
        self.server_nonce = ""
        self.client_nonce = ""
        kwargs = _connect_kwargs()
        if origin is not None:
            kwargs["origin"] = Origin(origin)
        self.ws: ClientConnection = ws_connect(f"ws://127.0.0.1:{port}", **kwargs)

    def __enter__(self) -> Worker:
        return self

    def __exit__(self, *exc: object) -> None:
        self.ws.close()

    def send(self, frame: Any) -> None:
        self.ws.send(json.dumps(frame))

    def recv(self) -> dict[str, Any]:
        frame = json.loads(self.ws.recv(timeout=RECV_TIMEOUT))
        assert isinstance(frame, dict)
        return frame

    def expect_close(self, code: int) -> None:
        with pytest.raises(ConnectionClosed):
            while True:
                self.ws.recv(timeout=RECV_TIMEOUT)
        assert self.ws.close_code == code

    def challenge(self) -> dict[str, Any]:
        frame = self.recv()
        assert frame["type"] == "challenge" and frame["protocol"] == 2
        assert len(b64url_decode(frame["serverNonce"])) == 32
        self.server_nonce = frame["serverNonce"]
        return frame

    def send_client_nonce(self) -> None:
        self.client_nonce = random_nonce()
        self.send({"type": "clientNonce", "clientNonce": self.client_nonce})

    def server_proof(self) -> dict[str, Any]:
        frame = self.recv()
        assert frame["type"] == "serverProof"
        message = handshake_message(
            "emunium-server", self.server_nonce, self.client_nonce, self.port
        )
        assert verify_mac(self.enrollment, message, frame["serverProof"])
        return frame

    def hello(self, **overrides: Any) -> dict[str, Any]:
        proof = hmac_sha256(
            self.enrollment,
            handshake_message(
                "emunium-client",
                self.server_nonce,
                self.client_nonce,
                self.port,
                self.install_id,
            ),
        )
        token = self.token
        token_proof = None
        if token:
            token_message = handshake_message(
                "emunium-token", self.server_nonce, self.client_nonce, self.port
            )
            token_proof = b64url(hmac_sha256(token, token_message))
        hello: dict[str, Any] = {
            "type": "hello",
            "protocol": 2,
            "mode": "attach",
            "installId": self.install_id,
            "proof": b64url(proof),
            "tokenProof": token_proof,
            "extensionVersion": "3.1.0",
            "userAgent": USER_AGENT,
        }
        hello.update(overrides)
        return hello

    def to_hello(self) -> None:
        """Steps 1-3: challenge, clientNonce, verified serverProof."""
        self.challenge()
        self.send_client_nonce()
        self.server_proof()

    def welcome(self) -> dict[str, Any]:
        frame = self.recv()
        assert frame["type"] == "welcome" and frame["protocol"] == 2
        assert frame["installId"] == self.install_id
        token = frame.get("token") or ""
        message = handshake_message(
            "emunium-welcome",
            self.server_nonce,
            self.client_nonce,
            self.port,
            self.install_id,
            token,
        )
        assert verify_mac(self.enrollment, message, frame["welcomeMac"])
        if token:
            self.token = token
        return frame

    def handshake(self) -> dict[str, Any]:
        self.to_hello()
        self.send(self.hello())
        return self.welcome()


# -- fixtures -----------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def state(tmp_path, monkeypatch) -> dict[str, Any]:
    monkeypatch.setenv("EMUNIUM_HOME", str(tmp_path / "home"))
    return load_or_create_state(_free_port())


@pytest.fixture
def chrome_origin(state) -> str:
    return "chrome-extension://" + state["extension_id"]


def _started(transport: AttachTransport):
    transport.start(timeout=5)
    return transport


@pytest.fixture
def hub(state):
    transport = _started(AttachTransport(state))
    try:
        yield transport
    finally:
        transport.shutdown()


@pytest.fixture
def mac_calls(monkeypatch) -> list[str]:
    """Labels of every MAC the server computes."""
    calls: list[str] = []
    real = server_mod._mac

    def recording(secret: str, message: str) -> bytes:
        calls.append(message.split("|", 1)[0])
        return real(secret, message)

    monkeypatch.setattr(server_mod, "_mac", recording)
    return calls


def _worker(hub: AttachTransport, state, origin: str | None, **kwargs: Any) -> Worker:
    port = hub.actual_port
    assert port is not None
    return Worker(port, state["enrollment"], origin, **kwargs)


def _installs(state) -> dict[str, Any]:
    return json.loads(Path(state["hub_path"]).read_text(encoding="utf-8"))["installs"]


# -- success --------------------------------------------------------------------


def test_handshake_then_request_reply_round_trip(hub, state, chrome_origin):
    with _worker(hub, state, chrome_origin) as worker:
        welcome = worker.handshake()
        assert set(welcome) == {"type", "protocol", "installId", "token", "welcomeMac"}
        assert hub.wait_for_connection(5)
        assert hub.bound_install_id == worker.install_id
        # Events such as claim are ignored.
        worker.send({"type": "event", "event": "claim", "nonce": "n" * 22})
        box: dict[str, Any] = {}
        thread = threading.Thread(
            target=lambda: box.setdefault("reply", hub.request("ping", timeout=5))
        )
        thread.start()
        request = worker.recv()
        assert request["type"] == "request" and request["method"] == "ping"
        worker.send(
            {
                "type": "reply",
                "id": request["id"],
                "ok": True,
                "result": "pong",
                "tabId": 7,
            }
        )
        thread.join(5)
        assert box["reply"].result == "pong" and box["reply"].tab_id == 7
        assert hub.connected


def test_firefox_origin_accepted(hub, state):
    with _worker(
        hub, state, "moz-extension://0b5c1f6e-1d2b-4c3a-9e8f-123456789abc"
    ) as worker:
        assert worker.handshake()["token"]


def test_server_proof_is_bound_to_the_bound_port(hub, state, chrome_origin):
    with _worker(hub, state, chrome_origin) as worker:
        worker.challenge()
        worker.send_client_nonce()
        proof = worker.recv()["serverProof"]
        sn, cn = worker.server_nonce, worker.client_nonce
        assert hub.actual_port == state["port"]
        bound = handshake_message("emunium-server", sn, cn, state["port"])
        assert verify_mac(state["enrollment"], bound, proof)
        # A relay on another port (the port the worker dialed) cannot reuse it.
        relayed = handshake_message("emunium-server", sn, cn, state["port"] + 1)
        assert not verify_mac(state["enrollment"], relayed, proof)
        assert not verify_mac("x" * 43, bound, proof)


def test_bound_install_reconnecting_supersedes_old_connection(
    hub, state, chrome_origin
):
    with _worker(hub, state, chrome_origin) as first:
        first.handshake()
        with _worker(
            hub, state, chrome_origin, install_id=first.install_id, token=first.token
        ) as second:
            assert "token" not in second.handshake()
            first.expect_close(4002)
            assert hub.wait_for_connection(5)


# -- rejection ------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "https://evil.example",
        "chrome-extension://" + "a" * 32,
        "moz-extension://",
    ],
    ids=["none", "web", "other-extension", "empty-moz"],
)
def test_wrong_origin_closed_4003(hub, state, origin, mac_calls):
    with _worker(hub, state, origin) as worker:
        worker.expect_close(4003)
    assert mac_calls == []


def test_chrome_origin_must_match_exactly(hub, state, chrome_origin):
    with _worker(hub, state, chrome_origin + "/") as worker:
        worker.expect_close(4003)


def test_wrong_enrollment_proof_closed_4001(hub, state, chrome_origin):
    with _worker(hub, state, chrome_origin) as worker:
        worker.to_hello()
        worker.enrollment = "y" * 43
        worker.send(worker.hello())
        worker.expect_close(4001)
    assert _installs(state) == {}
    assert hub.bound_install_id is None


def test_proof_for_another_install_id_closed_4001(hub, state, chrome_origin):
    with _worker(hub, state, chrome_origin) as worker:
        worker.to_hello()
        worker.send(worker.hello(installId=str(uuid.uuid4())))
        worker.expect_close(4001)


def test_hello_before_client_nonce_closed_4005(hub, state, chrome_origin, mac_calls):
    with _worker(hub, state, chrome_origin) as worker:
        worker.challenge()
        worker.client_nonce = random_nonce()
        worker.send(worker.hello())
        worker.expect_close(4005)
    assert mac_calls == []


def test_client_nonce_instead_of_hello_closed_4005(hub, state, chrome_origin):
    with _worker(hub, state, chrome_origin) as worker:
        worker.to_hello()
        worker.send_client_nonce()
        worker.expect_close(4005)


@pytest.mark.parametrize(
    "raw", ["{not json", "[1, 2]", '"clientNonce"'], ids=["non-json", "array", "string"]
)
def test_malformed_frame_closed_4005(hub, state, chrome_origin, raw):
    with _worker(hub, state, chrome_origin) as worker:
        worker.challenge()
        worker.ws.send(raw)
        worker.expect_close(4005)


def test_late_client_nonce_closed_4005(hub, state, chrome_origin, monkeypatch):
    monkeypatch.setattr(server_mod, "CLIENT_NONCE_TIMEOUT", 0.3)
    with _worker(hub, state, chrome_origin) as worker:
        worker.challenge()
        worker.expect_close(4005)


def test_late_hello_closed_4005(hub, state, chrome_origin, monkeypatch):
    monkeypatch.setattr(server_mod, "HELLO_TIMEOUT", 0.3)
    with _worker(hub, state, chrome_origin) as worker:
        worker.to_hello()
        worker.expect_close(4005)


@pytest.mark.parametrize(
    "client_nonce",
    [
        None,
        42,
        "",
        b64url(os.urandom(31)),
        b64url(os.urandom(33)),
        b64url(os.urandom(32)) + "=",
        base64.b64encode(b"\xfb" * 32).decode("ascii").rstrip("="),
        "A" * 41,
    ],
    ids=[
        "null",
        "number",
        "empty",
        "31-bytes",
        "33-bytes",
        "padded",
        "std-alphabet",
        "bad-length",
    ],
)
def test_malformed_client_nonce_rejected_before_any_mac(
    hub, state, chrome_origin, mac_calls, client_nonce
):
    with _worker(hub, state, chrome_origin) as worker:
        worker.challenge()
        worker.send({"type": "clientNonce", "clientNonce": client_nonce})
        worker.expect_close(4005)
    assert mac_calls == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"installId": "not-a-uuid"},
        {"installId": str(uuid.uuid4()) + "\n"},
        {"installId": None},
        {"proof": b64url(os.urandom(31))},
        {"proof": None},
        {"tokenProof": "short"},
        {"protocol": 1},
        {"protocol": True},
        {"mode": "launch"},
    ],
    ids=[
        "installId-shape",
        "installId-newline",
        "installId-null",
        "proof-31-bytes",
        "proof-null",
        "tokenProof-shape",
        "protocol-1",
        "protocol-bool",
        "mode",
    ],
)
def test_malformed_hello_rejected_before_any_client_mac(
    hub, state, chrome_origin, mac_calls, overrides
):
    with _worker(hub, state, chrome_origin) as worker:
        worker.to_hello()
        worker.send(worker.hello(**overrides))
        worker.expect_close(4005)
    assert mac_calls == ["emunium-server"]


# -- tokens ------------------------------------------------------------------------


def test_first_time_token_issued_and_persisted(hub, state, chrome_origin):
    hub_path = Path(state["hub_path"])
    before = json.loads(hub_path.read_text(encoding="utf-8"))
    with _worker(hub, state, chrome_origin) as worker:
        welcome = worker.handshake()
    token = welcome["token"]
    assert isinstance(token, str) and len(b64url_decode(token)) == 32
    after = json.loads(hub_path.read_text(encoding="utf-8"))
    assert (after["port"], after["enrollment"]) == (
        before["port"],
        before["enrollment"],
    )
    record = after["installs"][worker.install_id]
    assert record["token"] == token and record["confirmed"] is False
    assert record["userAgent"] == USER_AGENT
    assert record["firstSeen"] and record["lastSeen"]


def _reconnect(hub, state, origin, install_id: str, token: str | None) -> Worker:
    return _worker(hub, state, origin, install_id=install_id, token=token)


def _rejected(worker: Worker, code: int) -> None:
    with worker:
        worker.to_hello()
        worker.send(worker.hello())
        worker.expect_close(code)


def test_first_valid_token_proof_confirms_and_omits_token(hub, state, chrome_origin):
    with _worker(hub, state, chrome_origin) as worker:
        token = worker.handshake()["token"]
    first_seen = _installs(state)[worker.install_id]["firstSeen"]
    with _reconnect(hub, state, chrome_origin, worker.install_id, token) as again:
        welcome = again.handshake()  # welcomeMac verified over an empty token
        assert "token" not in welcome
    record = _installs(state)[worker.install_id]
    assert record["token"] == token and record["confirmed"] is True
    assert record["firstSeen"] == first_seen


def test_lost_welcome_then_null_token_proof_gets_new_token(hub, state, chrome_origin):
    with _worker(hub, state, chrome_origin) as worker:
        lost = worker.handshake()["token"]  # never stored by the extension
    install_id = worker.install_id
    with _reconnect(hub, state, chrome_origin, install_id, None) as again:
        fresh = again.handshake()["token"]
    assert fresh and fresh != lost
    record = _installs(state)[install_id]
    assert record["token"] == fresh and record["confirmed"] is False
    # The replaced pending token is no longer accepted; the fresh one confirms.
    _rejected(_reconnect(hub, state, chrome_origin, install_id, lost), 4001)
    with _reconnect(hub, state, chrome_origin, install_id, fresh) as confirmed:
        assert "token" not in confirmed.handshake()
    assert _installs(state)[install_id]["confirmed"] is True


def test_wrong_token_proof_against_pending_token_closed_4001(hub, state, chrome_origin):
    with _worker(hub, state, chrome_origin) as worker:
        token = worker.handshake()["token"]
    _rejected(
        _reconnect(hub, state, chrome_origin, worker.install_id, "wrong-token"), 4001
    )
    record = _installs(state)[worker.install_id]
    assert record["token"] == token and record["confirmed"] is False


@pytest.mark.parametrize("presented", [None, "wrong-token"], ids=["null", "wrong"])
def test_confirmed_token_without_valid_proof_closed_4001(
    hub, state, chrome_origin, presented
):
    with _worker(hub, state, chrome_origin) as worker:
        token = worker.handshake()["token"]
    with _reconnect(hub, state, chrome_origin, worker.install_id, token) as again:
        again.handshake()
    _rejected(_reconnect(hub, state, chrome_origin, worker.install_id, presented), 4001)
    record = _installs(state)[worker.install_id]
    assert record["token"] == token and record["confirmed"] is True


def test_stored_token_without_confirmed_flag_counts_as_confirmed(state, chrome_origin):
    install_id = str(uuid.uuid4())
    hub_path = Path(state["hub_path"])
    data = json.loads(hub_path.read_text(encoding="utf-8"))
    data["installs"][install_id] = {"token": "t" * 43}
    hub_path.write_text(json.dumps(data), encoding="utf-8")
    transport = _started(AttachTransport(state))
    try:
        _rejected(_reconnect(transport, state, chrome_origin, install_id, None), 4001)
    finally:
        transport.shutdown()
    assert _installs(state)[install_id] == {"token": "t" * 43}


def test_known_install_without_stored_token_gets_new_token(state, chrome_origin):
    install_id = str(uuid.uuid4())
    hub_path = Path(state["hub_path"])
    data = json.loads(hub_path.read_text(encoding="utf-8"))
    data["installs"][install_id] = {
        "firstSeen": "2026-01-01T00:00:00+00:00",
        "label": "k",
    }
    data["extra"] = {"kept": True}
    hub_path.write_text(json.dumps(data), encoding="utf-8")
    transport = _started(AttachTransport(state))
    try:
        with _worker(transport, state, chrome_origin, install_id=install_id) as worker:
            token = worker.handshake()["token"]
    finally:
        transport.shutdown()
    after = json.loads(hub_path.read_text(encoding="utf-8"))
    record = after["installs"][install_id]
    assert record["token"] == token and record["label"] == "k"
    assert record["firstSeen"] == "2026-01-01T00:00:00+00:00"
    assert after["extra"] == {"kept": True}


# -- one session, one install ---------------------------------------------------


def test_second_install_while_bound_closed_4009(hub, state, chrome_origin):
    with _worker(hub, state, chrome_origin) as first:
        first.handshake()
        with _worker(hub, state, chrome_origin) as second:
            second.to_hello()
            second.send(second.hello())
            second.expect_close(4009)
        assert hub.bound_install_id == first.install_id
        assert second.install_id not in _installs(state)
        assert hub.wait_for_connection(5)


def _drop_welcomes(hub: AttachTransport, monkeypatch) -> dict[str, bool]:
    """Make the server's welcome send fail (as on a dead socket) while ``switch["on"]``."""
    switch = {"on": True}
    real_send = hub._send

    async def send(ws: Any, frame: dict[str, Any]) -> bool:
        if switch["on"] and frame.get("type") == "welcome":
            return False
        return await real_send(ws, frame)

    monkeypatch.setattr(hub, "_send", send)
    return switch


def test_failed_welcome_send_rolls_back_bind(hub, state, chrome_origin, monkeypatch):
    switch = _drop_welcomes(hub, monkeypatch)
    with _worker(hub, state, chrome_origin) as lost:
        lost.to_hello()
        lost.send(lost.hello())
        lost.expect_close(1000)
    assert hub.bound_install_id is None
    assert _installs(state)[lost.install_id]["confirmed"] is False
    switch["on"] = False
    # The session is free again: another install binds it ...
    with _worker(hub, state, chrome_origin) as other:
        other.handshake()
        assert hub.bound_install_id == other.install_id
        # ... and the install whose welcome was lost is now the foreign one.
        _rejected(_reconnect(hub, state, chrome_origin, lost.install_id, None), 4009)


def test_failed_welcome_on_reconnect_keeps_existing_bind(
    hub, state, chrome_origin, monkeypatch
):
    with _worker(hub, state, chrome_origin) as first:
        first.handshake()
        switch = _drop_welcomes(hub, monkeypatch)
        with _reconnect(
            hub, state, chrome_origin, first.install_id, first.token
        ) as again:
            again.to_hello()
            again.send(again.hello())
            again.expect_close(1000)
        switch["on"] = False
        assert hub.bound_install_id == first.install_id
        assert hub.connected


def test_install_id_filter(state, chrome_origin):
    wanted = str(uuid.uuid4())
    transport = _started(AttachTransport(state, install_id=wanted))
    try:
        assert transport.bound_install_id == wanted
        with _worker(transport, state, chrome_origin) as other:
            other.to_hello()
            other.send(other.hello())
            other.expect_close(4009)
        with _worker(transport, state, chrome_origin, install_id=wanted) as worker:
            assert worker.handshake()["token"]
            assert transport.wait_for_connection(5)
    finally:
        transport.shutdown()
    assert list(_installs(state)) == [wanted]


def test_install_id_must_be_a_uuid(state):
    with pytest.raises(ValueError):
        AttachTransport(state, install_id="profile-1")


# -- listening socket ----------------------------------------------------------


def test_exclusive_bind_second_transport_fails_to_start(hub, state):
    assert hub.actual_port == state["port"]
    second = AttachTransport(state)
    with pytest.raises(errors.BridgeError):
        second.start(timeout=5)
    second.shutdown()


@pytest.mark.parametrize("reuse", [False, True], ids=["plain", "reuseaddr"])
def test_port_cannot_be_shared(hub, state, reuse):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as intruder:
        if reuse:
            intruder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with pytest.raises(OSError):
            intruder.bind(("127.0.0.1", state["port"]))


def test_listening_socket_is_exclusive_and_loopback_only(hub, state):
    server = hub._server
    assert server is not None
    [listening] = server.sockets
    assert listening.getsockname() == ("127.0.0.1", state["port"])
    if sys.platform == "win32":
        option = socket.SO_EXCLUSIVEADDRUSE
        assert listening.getsockopt(socket.SOL_SOCKET, option) != 0
    else:
        assert listening.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 0
        reuse_port = getattr(socket, "SO_REUSEPORT", None)
        if reuse_port is not None:
            assert listening.getsockopt(socket.SOL_SOCKET, reuse_port) == 0


# -- build artifacts carrying the enrollment secret ----------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
@pytest.mark.parametrize("target", ["chromium", "firefox"])
def test_build_artifacts_with_the_secret_are_owner_only(state, tmp_path, target):
    previous = os.umask(0o022)
    try:
        result = build_extension(tmp_path / "out", state=state, target=target)
    finally:
        os.umask(previous)
    unpacked = Path(result["unpacked_dir"])

    def mode(path: Any) -> int:
        return os.stat(path).st_mode & 0o777

    assert mode(unpacked) == 0o700
    attach = unpacked / "attach.json"
    assert (
        json.loads(attach.read_text(encoding="utf-8"))["enrollment"]
        == state["enrollment"]
    )
    assert mode(attach) == 0o600
    archives = [
        result[key] for key in ("zip_path", "crx_path", "xpi_path") if key in result
    ]
    assert archives
    for archive in archives:
        assert mode(archive) == 0o600, archive


def test_explicit_port_overrides_state_port(state, chrome_origin):
    port = _free_port()
    transport = _started(AttachTransport(state, port=port))
    try:
        assert transport.actual_port == port and transport.host == "127.0.0.1"
        with Worker(port, state["enrollment"], chrome_origin) as worker:
            worker.handshake()
    finally:
        transport.shutdown()
