"""Firefox launch helpers: args, user.js, stderr BiDi URL, BiDi extension install.

No real browser: stderr is a fake stream and BiDi is a local websockets server.
"""

from __future__ import annotations

import io
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("websockets", minversion="14.0")

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import ServerConnection, serve

from emunium._browser import firefox
from emunium._browser.firefox import (
    BidiSession,
    firefox_args,
    read_bidi_url,
    write_user_prefs,
)
from emunium.errors import LaunchError

EXTENSION_ID = "bridge@emunium.dev"
BIDI_LINE = b"WebDriver BiDi listening on ws://127.0.0.1:61234\n"


# -- firefox_args -----------------------------------------------------------


def test_firefox_args_headed():
    assert firefox_args("ff.exe", "C:/prof", headless=False) == [
        "ff.exe",
        "-profile",
        "C:/prof",
        "-no-remote",
        "--remote-debugging-port=0",
    ]


def test_firefox_args_headless_with_port():
    args = firefox_args("/usr/bin/firefox", "/tmp/p", headless=True, remote_port=9222)
    assert args == [
        "/usr/bin/firefox",
        "-profile",
        "/tmp/p",
        "-no-remote",
        "--remote-debugging-port=9222",
        "-headless",
    ]


# -- write_user_prefs -------------------------------------------------------

PREF_LINE = re.compile(r'^user_pref\("([^"]+)", (.+)\);$')


def _read_prefs(path: Path) -> dict[str, Any]:
    prefs: dict[str, Any] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = PREF_LINE.match(line)
        assert match, f"malformed user.js line: {line!r}"
        prefs[match.group(1)] = json.loads(match.group(2))
    return prefs


def test_write_user_prefs_defaults(tmp_path):
    path = write_user_prefs(tmp_path)
    assert path == tmp_path / "user.js"
    prefs = _read_prefs(path)
    assert prefs["browser.shell.checkDefaultBrowser"] is False
    assert prefs["browser.aboutwelcome.enabled"] is False
    assert prefs["startup.homepage_welcome_url"] == ""
    assert prefs["browser.startup.homepage_override.mstone"] == "ignore"
    assert prefs["datareporting.policy.dataSubmissionEnabled"] is False
    assert prefs["datareporting.healthreport.uploadEnabled"] is False
    assert prefs["browser.sessionstore.resume_from_crash"] is False
    assert prefs["dom.webdriver.enabled"] is False
    # Nothing about signing, security or network transport is changed.
    forbidden = ("xpinstall", "security.", "extensions.", "network.", "remote.")
    assert not [name for name in prefs if name.startswith(forbidden)]
    assert prefs == firefox.USER_PREFS


def test_write_user_prefs_overwrites(tmp_path):
    (tmp_path / "user.js").write_text('user_pref("stale", 1);\n', encoding="utf-8")
    prefs = _read_prefs(write_user_prefs(tmp_path))
    assert "stale" not in prefs


# -- read_bidi_url ------------------------------------------------------------


def test_read_bidi_url_skips_noise_and_copies_to_sink():
    noise = b"*** You are running in headless mode.\nconsole.warn: something\n"
    after = b"JavaScript warning: later line\n"
    stream = io.BytesIO(noise + BIDI_LINE + after)
    sink = io.BytesIO()
    assert read_bidi_url(stream, 5.0, sink=sink) == "ws://127.0.0.1:61234"
    deadline = time.monotonic() + 5
    while sink.getvalue() != noise + BIDI_LINE + after:
        assert time.monotonic() < deadline, sink.getvalue()
        time.sleep(0.01)


def test_read_bidi_url_eof_raises_with_tail():
    stream = io.BytesIO(b"first\nCrash Annotation GraphicsCriticalError\n")
    with pytest.raises(LaunchError) as info:
        read_bidi_url(stream, 5.0)
    message = str(info.value)
    assert "closed before WebDriver BiDi" in message
    assert "Crash Annotation GraphicsCriticalError" in message


@pytest.mark.parametrize(
    ("line", "url"),
    [
        (b"WebDriver BiDi listening on ws://127.0.0.1:9222\n", "ws://127.0.0.1:9222"),
        (b"WebDriver BiDi listening on ws://localhost:1\r\n", "ws://localhost:1"),
        (b"  WebDriver BiDi listening on ws://[::1]:65535  \n", "ws://[::1]:65535"),
    ],
)
def test_read_bidi_url_accepts_loopback(line, url):
    assert read_bidi_url(io.BytesIO(b"noise\n" + line), 5.0) == url


REJECTED_LINES = [
    b"WebDriver BiDi listening on ws://evil.host:1\n",
    b"WebDriver BiDi listening on ws://10.0.0.5:9222\n",
    b"WebDriver BiDi listening on ws://127.0.0.1.evil.host:9222\n",
    b"x WebDriver BiDi listening on ws://127.0.0.1:9222\n",
    b"console.log: WebDriver BiDi listening on ws://127.0.0.1:9222\n",
    b"WebDriver BiDi listening on ws://127.0.0.1:9222 and more\n",
    b"WebDriver BiDi listening on ws://127.0.0.1:9222/p\n",
    b"WebDriver BiDi listening on ws://127.0.0.1:9222/\n",
    b"WebDriver BiDi listening on ws://127.0.0.1:9222?q=1\n",
    b"WebDriver BiDi listening on ws://127.0.0.1:0\n",
    b"WebDriver BiDi listening on ws://127.0.0.1:65536\n",
    b"WebDriver BiDi listening on ws://127.0.0.1:99999\n",
    b"WebDriver BiDi listening on ws://127.0.0.1\n",
    b"WebDriver BiDi listening on wss://127.0.0.1:9222\n",
    b"WebDriver BiDi listening on ws://user@127.0.0.1:9222\n",
]


@pytest.mark.parametrize("line", REJECTED_LINES)
def test_read_bidi_url_ignores_non_strict_line_then_eof(line):
    with pytest.raises(LaunchError, match="closed before WebDriver BiDi"):
        read_bidi_url(io.BytesIO(line), 5.0)


@pytest.mark.parametrize("line", REJECTED_LINES)
def test_read_bidi_url_keeps_reading_past_non_strict_line(line):
    stream = io.BytesIO(line + BIDI_LINE)
    assert read_bidi_url(stream, 5.0) == "ws://127.0.0.1:61234"


def test_read_bidi_url_non_loopback_then_timeout():
    reader, writer = _pipe()
    try:
        writer.write(b"WebDriver BiDi listening on ws://evil.host:1\n")
        writer.flush()
        with pytest.raises(LaunchError, match="within 0.3s"):
            read_bidi_url(reader, 0.3)
    finally:
        writer.close()
        reader.close()


def _pipe() -> tuple[io.BufferedReader, io.BufferedWriter]:
    read_fd, write_fd = os.pipe()
    return os.fdopen(read_fd, "rb"), os.fdopen(write_fd, "wb")


def test_read_bidi_url_timeout_raises_with_tail():
    reader, writer = _pipe()
    try:
        writer.write(b"line one\nline two\n")
        writer.flush()
        start = time.monotonic()
        with pytest.raises(LaunchError) as info:
            read_bidi_url(reader, 0.3)
        assert time.monotonic() - start < 3
        message = str(info.value)
        assert "within 0.3s" in message
        assert "line one" in message and "line two" in message
    finally:
        writer.close()
        reader.close()


def test_read_bidi_url_keeps_draining_after_url():
    reader, writer = _pipe()
    try:
        writer.write(b"startup noise\n" + BIDI_LINE)
        writer.flush()
        assert read_bidi_url(reader, 5.0) == "ws://127.0.0.1:61234"

        # Far more than any OS pipe buffer: blocks forever unless drained.
        chunk = b"x" * 1023 + b"\n"
        done = threading.Event()

        def flood():
            for _ in range(4096):
                writer.write(chunk)
            writer.flush()
            done.set()

        thread = threading.Thread(target=flood, daemon=True)
        thread.start()
        assert done.wait(10), "stderr pipe was not drained after the URL"
    finally:
        writer.close()
        reader.close()


# -- fake BiDi server ---------------------------------------------------------


class FakeBidi:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self.origins: list[str | None] = []
        self.hang = threading.Event()
        # Ids of hung commands; their (stale) replies go out before the next answer.
        self.late_ids: list[int] = []
        self._server = serve(self._handler, "127.0.0.1", 0)
        port = self._server.socket.getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._thread.join(5)

    def _handler(self, ws: ServerConnection) -> None:
        request = ws.request
        assert request is not None
        self.paths.append(request.path)
        self.origins.append(request.headers.get("Origin"))
        try:
            for raw in ws:
                frame = json.loads(raw)
                self.frames.append(frame)
                while self.late_ids:
                    stale = {"sessionId": "stale", "extension": "stale@emunium.dev"}
                    late = {"type": "success", "id": self.late_ids.pop(0)}
                    ws.send(json.dumps({**late, "result": stale}))
                self._answer(ws, frame)
        except ConnectionClosed:
            return

    def _answer(self, ws: ServerConnection, frame: dict[str, Any]) -> None:
        command_id = frame["id"]
        method = frame["method"]
        event = {"type": "event", "method": "log.entryAdded", "params": {}}
        if method == "session.new":
            ws.send(json.dumps(event))
            result = {"sessionId": "s-1", "capabilities": {}}
            ws.send(json.dumps({"type": "success", "id": command_id, "result": result}))
        elif method == "webExtension.install":
            ws.send(json.dumps(event))
            # A reply to some other id must not be taken for ours.
            ws.send(json.dumps({"type": "success", "id": 999, "result": {}}))
            if frame["params"]["extensionData"]["path"].endswith("missing"):
                error = {
                    "type": "error",
                    "id": command_id,
                    "error": "invalid web extension",
                    "message": "manifest.json not found",
                }
                ws.send(json.dumps(error))
                return
            result = {"extension": EXTENSION_ID}
            ws.send(json.dumps({"type": "success", "id": command_id, "result": result}))
        elif method == "test.hang":
            self.hang.set()
        elif method == "test.late":
            self.late_ids.append(command_id)
        elif method == "test.drop":
            ws.close()
        else:
            ws.send(
                json.dumps(
                    {
                        "type": "error",
                        "id": command_id,
                        "error": "unknown command",
                        "message": f"{method} is not supported",
                    }
                )
            )


@pytest.fixture
def bidi():
    server = FakeBidi()
    try:
        yield server
    finally:
        server.stop()


def test_bidi_session_installs_extension(bidi, tmp_path):
    extension_dir = tmp_path / "ext"
    extension_dir.mkdir()
    session = BidiSession(bidi.url + "/")
    try:
        assert session.new_session()["sessionId"] == "s-1"
        relative = os.path.relpath(extension_dir)
        assert session.install_extension(relative) == EXTENSION_ID
    finally:
        session.close()
    assert bidi.paths == ["/session"]
    assert bidi.origins == [None]
    assert [frame["method"] for frame in bidi.frames] == [
        "session.new",
        "webExtension.install",
    ]
    assert bidi.frames[0] == {
        "id": 1,
        "method": "session.new",
        "params": {"capabilities": {}},
    }
    install = bidi.frames[1]
    assert install["id"] == 2
    assert install["params"] == {
        "extensionData": {"type": "path", "path": str(extension_dir.resolve())}
    }
    assert Path(install["params"]["extensionData"]["path"]).is_absolute()


def test_bidi_error_reply_raises_launch_error(bidi, tmp_path):
    session = BidiSession(bidi.url)
    try:
        with pytest.raises(LaunchError) as info:
            session.install_extension(tmp_path / "missing")
        message = str(info.value)
        assert "invalid web extension" in message
        assert "manifest.json not found" in message
        with pytest.raises(LaunchError, match="unknown command"):
            session.call("browser.nope", {})
    finally:
        session.close()


def test_bidi_call_timeout(bidi):
    session = BidiSession(bidi.url)
    try:
        start = time.monotonic()
        with pytest.raises(LaunchError, match="timed out"):
            session.call("test.hang", {}, timeout=0.3)
        assert time.monotonic() - start < 3
        assert bidi.hang.is_set()
    finally:
        session.close()


def test_bidi_late_reply_to_timed_out_id_is_skipped(bidi):
    session = BidiSession(bidi.url)
    try:
        with pytest.raises(LaunchError, match="timed out"):
            session.call("test.late", {}, timeout=0.3)
        # The server sends the stale reply for id 1 first, then id 2's own reply.
        assert session.new_session()["sessionId"] == "s-1"
        assert bidi.late_ids == []
        assert [frame["id"] for frame in bidi.frames] == [1, 2]
    finally:
        session.close()


def test_bidi_connection_closed_raises_launch_error(bidi):
    session = BidiSession(bidi.url)
    try:
        with pytest.raises(LaunchError, match="closed"):
            session.call("test.drop", {}, timeout=5)
    finally:
        session.close()


def test_bidi_close_is_idempotent(bidi):
    session = BidiSession(bidi.url)
    session.close()
    session.close()
    with pytest.raises(LaunchError, match="session is closed"):
        session.new_session()


def test_bidi_connect_failure_raises_launch_error():
    server = FakeBidi()
    url = server.url
    server.stop()
    with pytest.raises(LaunchError, match="Could not connect"):
        BidiSession(url, timeout=2)
