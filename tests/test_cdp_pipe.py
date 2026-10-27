"""CDP debugging pipe (slice AB4).

No browser and no child process: a fake peer thread plays Chrome on duplicates
of Chrome's pipe ends, over real OS pipes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from emunium._browser import cdp_pipe
from emunium._browser.cdp_pipe import CDP_PIPE_FLAGS, CdpPipe
from emunium.errors import LaunchError

Responder = Callable[["FakePeer", dict[str, Any]], None]


class FakePeer:
    """Reads ``\\0``-framed commands and answers through ``respond``."""

    def __init__(self, cdp: CdpPipe, respond: Responder) -> None:
        assert cdp._child_read is not None and cdp._child_write is not None
        # Chrome's own copies; CdpPipe.after_spawn closes the originals.
        self.read_fd = os.dup(cdp._child_read)
        self.write_fd: int | None = os.dup(cdp._child_write)
        self.respond = respond
        self.commands: list[dict[str, Any]] = []
        self.raw = b""
        self.got_command = threading.Condition()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        buffer = b""
        while True:
            chunk = os.read(self.read_fd, 4096)
            if not chunk:
                return
            self.raw += chunk
            buffer += chunk
            *frames, buffer = buffer.split(b"\0")
            for frame in frames:
                command = json.loads(frame)
                with self.got_command:
                    self.commands.append(command)
                    self.got_command.notify_all()
                self.respond(self, command)

    def send(self, data: bytes) -> None:
        assert self.write_fd is not None
        os.write(self.write_fd, data)

    def reply(self, message: dict[str, Any]) -> None:
        self.send(json.dumps(message).encode() + b"\0")

    def wait_commands(self, count: int, timeout: float = 5.0) -> None:
        with self.got_command:
            assert self.got_command.wait_for(
                lambda: len(self.commands) >= count, timeout
            )

    def close_write(self) -> None:
        if self.write_fd is not None:
            os.close(self.write_fd)
            self.write_fd = None

    def close(self) -> None:
        self.close_write()
        # The read loop ends at EOF once CdpPipe closes its command end.
        self.thread.join(5.0)
        if not self.thread.is_alive():
            os.close(self.read_fd)


def echo_result(peer: FakePeer, command: dict[str, Any]) -> None:
    peer.reply({"id": command["id"], "result": {"echo": command["method"]}})


def silent(peer: FakePeer, command: dict[str, Any]) -> None:
    pass


@pytest.fixture
def connect() -> Iterator[Callable[[Responder], tuple[CdpPipe, FakePeer]]]:
    made: list[tuple[CdpPipe, FakePeer]] = []

    def factory(respond: Responder) -> tuple[CdpPipe, FakePeer]:
        cdp = CdpPipe()
        cdp._create_pipes()
        peer = FakePeer(cdp, respond)
        cdp.after_spawn()
        made.append((cdp, peer))
        return cdp, peer

    yield factory
    for cdp, peer in made:
        cdp.close()
        peer.close()


# -- framing and id matching ---------------------------------------------------


def test_call_sends_nul_terminated_json_with_fresh_ids(connect):
    cdp, peer = connect(echo_result)

    assert cdp.call("Browser.getVersion") == {"echo": "Browser.getVersion"}
    assert cdp.call("Target.getTargets", {"filter": []}) == {
        "echo": "Target.getTargets"
    }

    assert peer.commands == [
        {"id": 1, "method": "Browser.getVersion", "params": {}},
        {"id": 2, "method": "Target.getTargets", "params": {"filter": []}},
    ]
    assert peer.raw.count(b"\0") == 2 and peer.raw.endswith(b"\0")


def test_reply_split_across_writes_is_reassembled(connect):
    def split(peer: FakePeer, command: dict[str, Any]) -> None:
        data = json.dumps({"id": command["id"], "result": {"id": "abc"}}).encode()
        for part in (data[:5], data[5:17], data[17:], b"\0"):
            peer.send(part)
            time.sleep(0.05)

    cdp, _ = connect(split)

    assert cdp.call("Extensions.loadUnpacked") == {"id": "abc"}


def test_coalesced_messages_are_routed_by_id(connect):
    def answer_both_at_once(peer: FakePeer, command: dict[str, Any]) -> None:
        if len(peer.commands) < 2:
            return
        first, second = peer.commands
        # Event, junk, an unknown id and both replies (reversed) in one write.
        peer.send(
            b'{"method":"Target.targetCreated","params":{}}\0'
            b"not json\0"
            b'{"id":99,"result":{"who":"nobody"}}\0'
            + json.dumps({"id": second["id"], "result": {"who": "second"}}).encode()
            + b"\0"
            + json.dumps({"id": first["id"], "result": {"who": "first"}}).encode()
            + b"\0"
        )

    cdp, peer = connect(answer_both_at_once)
    results: dict[str, Any] = {}

    def run(name: str) -> None:
        results[name] = cdp.call(name, timeout=5.0)

    first = threading.Thread(target=run, args=("first",))
    first.start()
    peer.wait_commands(1)
    second = threading.Thread(target=run, args=("second",))
    second.start()
    first.join(5.0)
    second.join(5.0)

    assert results == {"first": {"who": "first"}, "second": {"who": "second"}}


# -- failures ------------------------------------------------------------------


def test_error_reply_raises_launch_error_with_message(connect):
    def refuse(peer: FakePeer, command: dict[str, Any]) -> None:
        peer.reply(
            {
                "id": command["id"],
                "error": {"code": -32000, "message": "Method not available."},
            }
        )

    cdp, _ = connect(refuse)

    with pytest.raises(LaunchError, match="Method not available.") as info:
        cdp.call("Extensions.loadUnpacked", {"path": "x"})
    assert info.value.method == "Extensions.loadUnpacked"


def test_timeout_raises_and_late_reply_is_dropped(connect):
    cdp, peer = connect(silent)

    started = time.monotonic()
    with pytest.raises(LaunchError, match="no reply within 0.2s"):
        cdp.call("Browser.getVersion", timeout=0.2)
    assert time.monotonic() - started < 2.0

    peer.respond = echo_result
    peer.reply({"id": 1, "result": {"late": True}})
    assert cdp.call("Target.getTargets", timeout=5.0) == {"echo": "Target.getTargets"}


def test_peer_closing_the_pipe_raises_promptly(connect):
    def hang_up(peer: FakePeer, command: dict[str, Any]) -> None:
        peer.close_write()

    cdp, _ = connect(hang_up)

    started = time.monotonic()
    with pytest.raises(LaunchError, match="browser closed the pipe"):
        cdp.call("Browser.getVersion", timeout=10.0)
    assert time.monotonic() - started < 5.0
    with pytest.raises(LaunchError, match="browser closed the pipe"):
        cdp.call("Browser.getVersion")


def test_call_before_after_spawn_raises():
    cdp = CdpPipe()
    cdp._create_pipes()
    try:
        with pytest.raises(LaunchError, match="not connected"):
            cdp.call("Browser.getVersion")
    finally:
        cdp.close()


# -- close -----------------------------------------------------------------------


def test_close_is_idempotent_and_ends_the_peer(connect):
    cdp, peer = connect(echo_result)
    assert cdp.call("Browser.getVersion")

    cdp.close()
    cdp.close()

    # Chrome exits when its command pipe closes: the peer sees EOF.
    peer.thread.join(5.0)
    assert not peer.thread.is_alive()
    with pytest.raises(LaunchError, match="pipe is closed"):
        cdp.call("Browser.getVersion")


def test_close_wakes_a_pending_call(connect):
    cdp, peer = connect(silent)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            cdp.call("Browser.getVersion", timeout=30.0)
        except LaunchError as exc:
            errors.append(exc)

    caller = threading.Thread(target=run)
    caller.start()
    peer.wait_commands(1)
    cdp.close()
    caller.join(5.0)

    assert not caller.is_alive()
    assert len(errors) == 1 and "pipe is closed" in str(errors[0])


def test_close_before_spawn_releases_every_end():
    cdp = CdpPipe()
    cdp._create_pipes()
    cdp.close()
    cdp.close()

    assert cdp._child_read is None and cdp._child_write is None
    assert cdp._command_write is None and cdp._reply_read is None


# -- load_unpacked ---------------------------------------------------------------


def test_load_unpacked_sends_absolute_path_and_returns_id(
    connect, tmp_path, monkeypatch
):
    def loaded(peer: FakePeer, command: dict[str, Any]) -> None:
        peer.reply({"id": command["id"], "result": {"id": "abcdefghijklmnop"}})

    cdp, peer = connect(loaded)
    monkeypatch.chdir(tmp_path)

    assert cdp.load_unpacked("extension") == "abcdefghijklmnop"
    assert peer.commands == [
        {
            "id": 1,
            "method": "Extensions.loadUnpacked",
            "params": {"path": str((tmp_path / "extension").resolve())},
        }
    ]


def test_load_unpacked_without_id_raises(connect, tmp_path):
    cdp, _ = connect(
        lambda peer, command: peer.reply({"id": command["id"], "result": {}})
    )

    with pytest.raises(LaunchError, match="no extension id"):
        cdp.load_unpacked(tmp_path)


# -- platform preparation ------------------------------------------------------


def test_prepare_on_windows_shares_two_inheritable_handles():
    if sys.platform != "win32":
        pytest.skip("Windows handle inheritance")
    import msvcrt

    cdp = CdpPipe()
    flags = cdp.prepare()
    try:
        assert cdp._child_read is not None and cdp._child_write is not None
        assert cdp._command_write is not None and cdp._reply_read is not None
        read_handle = msvcrt.get_osfhandle(cdp._child_read)
        write_handle = msvcrt.get_osfhandle(cdp._child_write)

        assert flags == [
            *CDP_PIPE_FLAGS,
            f"--remote-debugging-io-pipes={read_handle},{write_handle}",
        ]
        assert os.get_handle_inheritable(read_handle)
        assert os.get_handle_inheritable(write_handle)
        assert not os.get_inheritable(cdp._command_write)
        assert not os.get_inheritable(cdp._reply_read)

        kwargs = cdp.popen_kwargs()
        assert kwargs["close_fds"] is True
        assert kwargs["startupinfo"].lpAttributeList == {
            "handle_list": [read_handle, write_handle]
        }
        assert cdp.wrap_args(["chrome.exe", "--a b"]) == ["chrome.exe", "--a b"]
        with pytest.raises(LaunchError, match="already prepared"):
            cdp.prepare()
    finally:
        cdp.close()
    assert cdp._child_read is None and cdp._child_write is None


def test_unprepared_pipe_refuses_spawn_arguments():
    cdp = CdpPipe()

    with pytest.raises(LaunchError, match="not prepared"):
        cdp.popen_kwargs()
    if sys.platform != "win32":
        with pytest.raises(LaunchError, match="not prepared"):
            cdp.wrap_args(["chromium"])


def test_trampoline_reopens_fds_on_3_and_4_and_passes_argv_verbatim():
    args = ["/opt/chrome", "--user-data-dir=/tmp/a b", "$(touch x)", "'"]

    assert cdp_pipe.trampoline_args(args, 12, 13) == [
        "/bin/sh",
        "-c",
        'exec 3</dev/fd/12 4>/dev/fd/13 && exec "$@"',
        "sh",
        *args,
    ]


def test_trampoline_refuses_fds_that_could_collide_with_3_and_4():
    with pytest.raises(ValueError, match=">= 10"):
        cdp_pipe.trampoline_args(["chromium"], 4, 12)


def test_prepare_on_posix_keeps_chrome_ends_high_and_close_on_exec():
    if sys.platform == "win32":
        pytest.skip("POSIX fd renumbering")
    cdp = CdpPipe()
    flags = cdp.prepare()
    try:
        read_fd, write_fd = cdp._child_read, cdp._child_write
        assert read_fd is not None and write_fd is not None

        assert flags == list(CDP_PIPE_FLAGS)
        assert read_fd >= 10 and write_fd >= 10
        assert not os.get_inheritable(read_fd) and not os.get_inheritable(write_fd)
        assert cdp.popen_kwargs() == {"pass_fds": (read_fd, write_fd)}
        assert cdp.wrap_args(["chromium"]) == cdp_pipe.trampoline_args(
            ["chromium"], read_fd, write_fd
        )
    finally:
        cdp.close()
    assert cdp._child_read is None and cdp._child_write is None


# Stands in for Chrome: one request on fd 3, the reply (with its pid and open
# fds) on fd 4, then it lives until its command pipe closes.
FAKE_BROWSER = """\
import json, os, sys
buffer = b""
while not buffer.endswith(b"\\0"):
    chunk = os.read(3, 4096)
    if not chunk:
        sys.exit(3)
    buffer += chunk
request = json.loads(buffer[:-1])
fds = sorted(int(name) for name in os.listdir("/dev/fd"))
reply = {"id": request["id"], "result": {"pid": os.getpid(), "fds": fds,
         "argv": sys.argv[1:]}}
os.write(4, json.dumps(reply).encode() + b"\\0")
while os.read(3, 4096):
    pass
"""


def test_trampoline_spawn_round_trips_and_keeps_the_pid(tmp_path):
    if sys.platform == "win32":
        pytest.skip("POSIX trampoline")
    script = tmp_path / "fake_browser.py"
    script.write_text(FAKE_BROWSER, encoding="utf-8")
    cdp = CdpPipe()
    cdp.prepare()
    args = [sys.executable, "-I", str(script), "--flag=a b", "$HOME"]
    process = subprocess.Popen(cdp.wrap_args(args), **cdp.popen_kwargs())
    try:
        cdp.after_spawn()
        result = cdp.call("Browser.getVersion", timeout=10.0)

        assert result["pid"] == process.pid
        assert result["argv"] == ["--flag=a b", "$HOME"]
        assert {3, 4} <= set(result["fds"])
        # Closing the pipe ends the browser.
        cdp.close()
        assert process.wait(timeout=10.0) == 0
    finally:
        cdp.close()
        if process.poll() is None:
            process.kill()
            process.wait()


def test_after_spawn_closes_child_ends_in_this_process(connect):
    cdp, _ = connect(echo_result)

    assert cdp._child_read is None and cdp._child_write is None
    assert cdp.call("Browser.getVersion") == {"echo": "Browser.getVersion"}


def test_cdp_pipe_flags():
    assert CDP_PIPE_FLAGS == (
        "--remote-debugging-pipe",
        "--enable-unsafe-extension-debugging",
    )
