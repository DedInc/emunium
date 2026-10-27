"""Firefox launch helpers: command line, profile prefs, WebDriver BiDi install.

Firefox loads no unpacked extension from the command line. Started with
``--remote-debugging-port`` it prints ``WebDriver BiDi listening on ws://...``
on stderr; ``webExtension.install`` over ``<url>/session`` then installs the
unpacked extension temporarily (until Firefox exits).

On Windows ``firefox.exe`` is a launcher stub and the browser runs in a child
process, so the spawned pid is not the browser's.

Lifecycle: ``write_user_prefs()`` -> ``Popen(firefox_args(...), stderr=PIPE)``
-> ``read_bidi_url()`` -> ``BidiSession(url)`` -> ``new_session()`` ->
``install_extension()`` -> ``close()``.
"""

from __future__ import annotations

import collections
import inspect
import itertools
import json
import queue
import re
import threading
import time
from pathlib import Path
from typing import IO, Any

from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import ClientConnection
from websockets.sync.client import connect as ws_connect

from emunium.errors import LaunchError

__all__ = [
    "BidiSession",
    "firefox_args",
    "read_bidi_url",
    "write_user_prefs",
]

DEFAULT_CALL_TIMEOUT = 15.0
DEFAULT_CONNECT_TIMEOUT = 10.0
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
STDERR_TAIL_LINES = 20
ERROR_TAIL_LINES = 8
# The whole line, loopback only, no path or query: anything else on stderr
# (page console output, a crafted line) must never pick the BiDi endpoint.
BIDI_LISTENING = re.compile(
    r"[ \t]*WebDriver BiDi listening on "
    r"ws://(?P<host>127\.0\.0\.1|localhost|\[::1\]):(?P<port>[0-9]{1,5})[ \t]*"
)
MAX_PORT = 65535


def _parse_bidi_line(line: str) -> str | None:
    """Normalized ``ws://host:port`` when ``line`` is exactly the BiDi announcement."""
    match = BIDI_LISTENING.fullmatch(line)
    if match is None:
        return None
    port = int(match.group("port"))
    if not 1 <= port <= MAX_PORT:
        return None
    return f"ws://{match.group('host')}:{port}"


# User-experience prefs only: no security, signing or networking changes.
USER_PREFS: dict[str, bool | str] = {
    # No default-browser check.
    "browser.shell.checkDefaultBrowser": False,
    # No about:welcome / first-run / what's-new pages.
    "browser.aboutwelcome.enabled": False,
    "trailhead.firstrun.didSeeAboutWelcome": True,
    "startup.homepage_welcome_url": "",
    "startup.homepage_welcome_url.additional": "",
    "browser.startup.homepage_override.mstone": "ignore",
    # No telemetry / data-reporting upload, and no data-policy notice tab.
    "datareporting.policy.dataSubmissionEnabled": False,
    "datareporting.healthreport.uploadEnabled": False,
    "toolkit.telemetry.reportingpolicy.firstRun": False,
    # No session restore prompt after an unclean exit.
    "browser.sessionstore.resume_from_crash": False,
}


def firefox_args(
    executable: str, profile_dir: str, *, headless: bool, remote_port: int = 0
) -> list[str]:
    """Command line for a private Firefox instance with WebDriver BiDi enabled."""
    args = [
        executable,
        "-profile",
        str(profile_dir),
        "-no-remote",
        f"--remote-debugging-port={int(remote_port)}",
    ]
    if headless:
        args.append("-headless")
    return args


def write_user_prefs(profile_dir: str | Path) -> Path:
    """Write ``user.js`` into ``profile_dir``; returns its path."""
    directory = Path(profile_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "user.js"
    lines = [
        f"user_pref({json.dumps(name)}, {json.dumps(value)});"
        for name, value in USER_PREFS.items()
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class _StderrDrain:
    """Reads ``stream`` to EOF on a daemon thread, so Firefox never blocks on a full pipe."""

    def __init__(self, stream: IO[bytes], sink: IO[bytes] | None) -> None:
        self._stream = stream
        self._sink = sink
        self._lock = threading.Lock()
        self._tail: collections.deque[str] = collections.deque(maxlen=STDERR_TAIL_LINES)
        # Receives the URL once, then ``None`` at EOF.
        self.found: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, name="emunium-firefox-stderr", daemon=True
        )
        self._thread.start()

    def tail(self, count: int) -> list[str]:
        with self._lock:
            lines = list(self._tail)
        return lines[-count:]

    def _run(self) -> None:
        announced = False
        while True:
            try:
                raw = self._stream.readline()
            except (OSError, ValueError):  # closed underneath us
                break
            if not raw:
                break
            self._write_sink(raw)
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line.strip():
                with self._lock:
                    self._tail.append(line)
            if not announced:
                url = _parse_bidi_line(line)
                if url is not None:
                    announced = True
                    self.found.put(url)
        self.found.put(None)

    def _write_sink(self, raw: bytes) -> None:
        sink = self._sink
        if sink is None:
            return
        try:
            sink.write(raw)
            sink.flush()
        except (OSError, ValueError):  # the caller closed it; keep draining
            self._sink = None


def read_bidi_url(
    stream: IO[bytes], timeout: float, *, sink: IO[bytes] | None = None
) -> str:
    """The ``ws://`` URL Firefox announces on ``stream`` (its binary stderr).

    Every line is copied to ``sink`` when given, and the stream keeps being
    drained after the URL is found (and after a timeout) until EOF.
    """
    drain = _StderrDrain(stream, sink)
    try:
        url = drain.found.get(timeout=max(0.0, timeout))
    except queue.Empty:
        reason = f"Firefox did not announce WebDriver BiDi within {timeout:g}s."
    else:
        if url is not None:
            return url
        reason = "Firefox stderr closed before WebDriver BiDi was announced."
    lines = drain.tail(ERROR_TAIL_LINES)
    if lines:
        reason += f"\nLast {len(lines)} stderr line(s):\n" + "\n".join(lines)
    raise LaunchError(reason)


def _connect_kwargs(timeout: float) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"max_size": MAX_MESSAGE_BYTES, "open_timeout": timeout}
    # websockets >= 15 honours HTTP(S)_PROXY by default; BiDi is loopback only.
    if "proxy" in inspect.signature(ws_connect).parameters:
        kwargs["proxy"] = None
    return kwargs


class BidiSession:
    """Synchronous WebDriver BiDi client on ``<url>/session``."""

    def __init__(self, url: str, *, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> None:
        self.url = url.rstrip("/") + "/session"
        self._ids = itertools.count(1)
        self._call_lock = threading.Lock()  # one command in flight
        self._state_lock = threading.Lock()  # never held across I/O
        self._closed = False
        try:
            self._ws: ClientConnection = ws_connect(
                self.url, **_connect_kwargs(timeout)
            )
        except (OSError, WebSocketException) as exc:
            raise LaunchError(
                f"Could not connect to WebDriver BiDi at {self.url}: {exc}"
            ) from exc

    def call(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float = DEFAULT_CALL_TIMEOUT,
    ) -> dict[str, Any]:
        """Send one command and return its ``result``; events are skipped."""
        with self._call_lock:
            with self._state_lock:
                closed = self._closed
            if closed:
                raise LaunchError(f"WebDriver BiDi session is closed ({method}).")
            command_id = next(self._ids)
            frame = {"id": command_id, "method": method, "params": params}
            try:
                self._ws.send(json.dumps(frame))
                return self._await_reply(command_id, method, timeout)
            except ConnectionClosed as exc:
                raise LaunchError(
                    f"WebDriver BiDi connection closed during {method}: {exc}"
                ) from exc

    def _await_reply(
        self, command_id: int, method: str, timeout: float
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LaunchError(
                    f"WebDriver BiDi {method} timed out after {timeout:g}s."
                )
            try:
                raw = self._ws.recv(timeout=remaining)
            except TimeoutError:
                continue  # the deadline check above raises
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(message, dict) or message.get("id") != command_id:
                continue  # events and replies to other commands
            if message.get("type") == "error":
                raise LaunchError(
                    f"WebDriver BiDi {method} failed: "
                    f"{message.get('error', 'unknown error')}: "
                    f"{message.get('message', '')}"
                )
            result = message.get("result")
            if not isinstance(result, dict):
                raise LaunchError(
                    f"WebDriver BiDi {method} returned no result object: {raw!r}"
                )
            return result

    def new_session(self) -> dict[str, Any]:
        return self.call("session.new", {"capabilities": {}})

    def install_extension(self, path: str | Path) -> str:
        """Install the unpacked extension at ``path`` temporarily; returns its id."""
        absolute = str(Path(path).resolve())
        result = self.call(
            "webExtension.install",
            {"extensionData": {"type": "path", "path": absolute}},
        )
        extension = result.get("extension")
        if not isinstance(extension, str) or not extension:
            raise LaunchError(
                f"WebDriver BiDi webExtension.install returned no extension id: {result!r}"
            )
        return extension

    def close(self) -> None:
        """Close the socket (idempotent); a call in flight fails with ``LaunchError``."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._ws.close()
