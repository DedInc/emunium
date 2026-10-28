"""Launcher and installer lifecycle (slice S6).

No real Chrome, no network: Popen, taskkill, urlopen and the bridge are fakes.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import stat
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from typing import ClassVar

import pytest

import emunium.chrome_installer as installer
from emunium._browser import launcher
from emunium._browser.discovery import InstalledBrowser
from emunium._browser.facade import Browser
from emunium.errors import BridgeError, LaunchError

TOKEN = "tok-0123456789abcdef"
SITE_ISOLATION_FLAG = "--disable-features=IsolateOrigins,site-per-process"


# -- fakes ---------------------------------------------------------------------


class FakeBridge:
    def __init__(self, connect_after=1, port=48123, token=TOKEN, start_error=None):
        self.connect_after = connect_after  # None -> never connects
        self.actual_port = None
        self.token = None
        self._port = port
        self._token = token
        self.start_error = start_error
        self.calls = []

    def start(self, timeout=30.0):
        self.calls.append("start")
        if self.start_error is not None:
            raise self.start_error
        self.actual_port = self._port
        self.token = self._token

    def wait_for_connection(self, timeout=60.0):
        self.calls.append(("wait", timeout))
        waits = sum(1 for c in self.calls if isinstance(c, tuple) and c[0] == "wait")
        return self.connect_after is not None and waits >= self.connect_after

    def shutdown(self):
        self.calls.append("shutdown")


class FakePopen:
    instances: ClassVar[list[FakePopen]] = []
    events: ClassVar[list[tuple[object, ...]]] = []
    exit_code = None  # poll() result right after spawn; None -> running
    stderr_text = b""

    def __init__(self, args, **kwargs):
        self.args = list(args)
        self.kwargs = kwargs
        self.pid = 4242 + len(FakePopen.instances)
        self._handle = 9000 + self.pid
        self.returncode = FakePopen.exit_code
        self.terminated = False
        self.killed = False
        stderr = kwargs.get("stderr")
        piped = stderr is subprocess.PIPE
        self.stderr = io.BytesIO(FakePopen.stderr_text) if piped else None
        if stderr is not None and not piped and FakePopen.stderr_text:
            stderr.write(FakePopen.stderr_text)
        FakePopen.instances.append(self)

    def poll(self):
        return self.returncode

    def wait(self, timeout: float | None = None):
        FakePopen.events.append(("wait", self.pid, timeout))
        if self.returncode is None:
            if timeout is None:
                raise AssertionError("wait() on a running process would block forever")
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


class FakeJobApi:
    """Stands in for kernel32; never touches a real process."""

    def __init__(self, events):
        self.events = events
        self.assign_error = None
        self.next_job = 700

    def create_kill_on_close_job(self):
        self.next_job += 1
        self.events.append(("create_job", self.next_job))
        return self.next_job

    def assign(self, job, process):
        self.events.append(("assign", job, process.pid))
        if self.assign_error is not None:
            raise self.assign_error

    def terminate(self, job, exit_code):
        self.events.append(("terminate_job", job, exit_code))
        for proc in FakePopen.instances:  # the whole tree dies with the job
            if proc.returncode is None:
                proc.returncode = exit_code

    def close(self, handle):
        self.events.append(("close_job", handle))


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Fake Popen/taskkill/killpg/job layer/ensure_chrome, isolated temp root."""
    FakePopen.instances = []
    FakePopen.events = events = []
    FakePopen.exit_code = None
    FakePopen.stderr_text = b""
    taskkills = []
    state = {"graceful_rc": 128, "graceful_exits_parent": False}

    def fake_run(cmd, **kwargs):
        taskkills.append(list(cmd))
        events.append(("taskkill",) + tuple(cmd[1:]))
        force = "/F" in cmd
        for proc in FakePopen.instances:
            if str(proc.pid) in cmd and (force or state["graceful_exits_parent"]):
                proc.returncode = 1
        return subprocess.CompletedProcess(cmd, 0 if force else state["graceful_rc"])

    killpg_calls = []

    def fake_killpg(pid, sig):
        killpg_calls.append((pid, sig))
        events.append(("killpg", pid, sig))
        for proc in FakePopen.instances:
            if proc.pid == pid:
                proc.returncode = -sig

    job_api = FakeJobApi(events)
    monkeypatch.setattr(launcher, "_job_api", lambda: job_api)

    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    exe = tmp_path / "chrome-bin" / "chrome.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    ensure_calls = []

    def fake_ensure():
        ensure_calls.append(1)
        return str(exe)

    monkeypatch.setattr(launcher.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    monkeypatch.setattr(launcher.os, "killpg", fake_killpg, raising=False)
    monkeypatch.setattr(launcher, "ensure_chrome", fake_ensure)
    # Never the host's browsers: "auto" sees only what a test installs here.
    installed: list[InstalledBrowser] = []
    monkeypatch.setattr(launcher, "find_browsers", lambda: list(installed))
    monkeypatch.setattr(
        launcher,
        "find_browser",
        lambda name: next((b for b in installed if b.name == name), None),
    )
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)
    monkeypatch.setattr(launcher.tempfile, "tempdir", str(tmp_root))
    monkeypatch.delenv(installer.CHROME_PATH_ENV, raising=False)
    return {
        "exe": str(exe),
        "tmp_root": tmp_root,
        "taskkills": taskkills,
        "killpg": killpg_calls,
        "ensure_calls": ensure_calls,
        "events": events,
        "state": state,
        "job_api": job_api,
        "installed": installed,
    }


def _session(bridge=None, **attrs):
    session = launcher.BrowserSession()
    session.bridge = bridge if bridge is not None else FakeBridge()
    for key, value in attrs.items():
        setattr(session, key, value)
    return session


def _snapshot(directory):
    out = {}
    for root, _dirs, files in os.walk(directory):
        for name in files:
            path = os.path.join(root, name)
            st = os.stat(path)
            out[os.path.relpath(path, directory)] = (st.st_size, st.st_mtime_ns)
    return out


# -- launcher: session directory -----------------------------------------------


def test_launch_copies_extension_and_writes_session_json(env):
    session = _session()
    launcher.launch(session, bridge_timeout=5)

    session_dir = Path(session.session_dir)
    assert session_dir.parent == env["tmp_root"]
    assert session_dir.name.startswith("emunium-")
    ext = Path(session.extension_dir)
    assert ext == session_dir / "extension"
    assert (ext / "manifest.json").is_file()
    assert json.loads((ext / "session.json").read_text(encoding="utf-8")) == {
        "protocol": 2,
        "port": 48123,
        "token": TOKEN,
    }
    assert Path(session.profile_dir) == session_dir / "profile"
    assert (session_dir / "profile" / "Default" / "Preferences").is_file()
    assert session.log_path == str(session_dir / "chrome.log")
    assert FakePopen.instances[0].kwargs["stderr"] is not None
    launcher.close(session)
    assert not session_dir.exists()


def test_copy_ignores_runtime_files_and_source_untouched(env, monkeypatch, tmp_path):
    src = tmp_path / "pkg-extension"
    (src / "__pycache__").mkdir(parents=True)
    (src / "manifest.json").write_text("{}", encoding="utf-8")
    (src / "background.js").write_text("//", encoding="utf-8")
    (src / "port.json").write_text('{"port": 1}', encoding="utf-8")
    (src / "session.json").write_text('{"stale": true}', encoding="utf-8")
    (src / "__pycache__" / "x.pyc").write_bytes(b"\0")
    before = _snapshot(str(src))
    monkeypatch.setattr(launcher, "EXTENSION_SOURCE_DIR", str(src))

    session = _session()
    launcher.launch(session, bridge_timeout=5)
    ext = Path(session.extension_dir)
    assert sorted(p.name for p in ext.iterdir()) == [
        "background.js",
        "manifest.json",
        "session.json",
    ]
    assert (
        json.loads((ext / "session.json").read_text(encoding="utf-8"))["token"] == TOKEN
    )
    launcher.close(session)
    assert _snapshot(str(src)) == before


def test_packaged_extension_dir_is_never_written(env):
    pkg = launcher.EXTENSION_SOURCE_DIR
    before = _snapshot(pkg)
    session = _session()
    launcher.launch(session, bridge_timeout=5)
    launcher.close(session)
    assert _snapshot(pkg) == before
    assert not os.path.exists(os.path.join(pkg, "port.json"))
    assert not os.path.exists(os.path.join(pkg, "session.json"))
    exe_dir = os.path.dirname(env["exe"])
    assert not os.path.exists(os.path.join(exe_dir, "master_preferences"))


def test_two_sessions_get_separate_directories(env):
    a, b = _session(FakeBridge(port=40001)), _session(FakeBridge(port=40002))
    launcher.launch(a, bridge_timeout=5)
    launcher.launch(b, bridge_timeout=5)
    assert a.session_dir != b.session_dir
    port_a = json.loads(
        Path(a.extension_dir, "session.json").read_text(encoding="utf-8")
    )["port"]
    port_b = json.loads(
        Path(b.extension_dir, "session.json").read_text(encoding="utf-8")
    )["port"]
    assert (port_a, port_b) == (40001, 40002)
    launcher.close(a)
    launcher.close(b)


# -- launcher: args --------------------------------------------------------------


def test_chrome_args(env):
    session = _session(headless=True, extra_args=["--window-size=800,600"])
    launcher.launch(session, bridge_timeout=5)
    args = FakePopen.instances[0].args
    ext = session.extension_dir
    assert args[0] == env["exe"]
    assert args[1] == f"--user-data-dir={session.profile_dir}"
    for flag in (
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--hide-crash-restore-bubble",
        "--disable-blink-features=AutomationControlled",
        f"--disable-extensions-except={ext}",
        f"--load-extension={ext}",
        "--headless=new",
    ):
        assert flag in args
    assert args[-1] == "--window-size=800,600"
    assert args.index("--headless=new") < args.index("--window-size=800,600")
    assert SITE_ISOLATION_FLAG not in args
    assert not any("IsolateOrigins" in a or "site-per-process" in a for a in args)
    launcher.close(session)


def test_headful_has_no_headless_flag(env):
    session = _session()
    launcher.launch(session, bridge_timeout=5)
    assert not any(a.startswith("--headless") for a in FakePopen.instances[0].args)
    launcher.close(session)


def test_posix_spawn_uses_new_session(env, monkeypatch):
    monkeypatch.setattr(launcher, "_IS_WINDOWS", False)
    session = _session()
    launcher.launch(session, bridge_timeout=5)
    assert FakePopen.instances[0].kwargs.get("start_new_session") is True
    launcher.close(session)


def test_chrome_path_override_skips_installer(env, tmp_path):
    custom = tmp_path / "custom-chrome"
    custom.write_bytes(b"")
    session = _session(chrome_path=str(custom))
    launcher.launch(session, bridge_timeout=5)
    assert FakePopen.instances[0].args[0] == str(custom)
    assert env["ensure_calls"] == []
    launcher.close(session)


def test_env_override_skips_installer(env, monkeypatch, tmp_path):
    custom = tmp_path / "env-chrome"
    custom.write_bytes(b"")
    monkeypatch.setenv(installer.CHROME_PATH_ENV, str(custom))
    session = _session()
    launcher.launch(session, bridge_timeout=5)
    assert FakePopen.instances[0].args[0] == str(custom)
    assert session.executable_path == str(custom)
    assert env["ensure_calls"] == []
    launcher.close(session)


def test_missing_override_raises_launch_error(env, tmp_path):
    bridge = FakeBridge()
    session = _session(bridge, chrome_path=str(tmp_path / "nope"))
    with pytest.raises(LaunchError, match="not found"):
        launcher.launch(session, bridge_timeout=5)
    assert FakePopen.instances == []
    assert "start" not in bridge.calls


def test_user_data_dir_is_used_and_kept(env, tmp_path):
    profile = tmp_path / "my-profile"
    session = _session(user_data_dir=str(profile))
    launcher.launch(session, bridge_timeout=5)
    session_dir = Path(session.session_dir)
    assert not (session_dir / "profile").exists()
    assert f"--user-data-dir={profile.resolve()}" in FakePopen.instances[0].args
    launcher.close(session)
    assert (profile / "Default" / "Preferences").is_file()
    assert not session_dir.exists()


# -- launcher: rollback ------------------------------------------------------------


def _assert_rolled_back(session, bridge, env):
    assert session.process is None
    assert session.session_dir is None
    assert bridge.calls[-1] == "shutdown"
    assert list(env["tmp_root"].iterdir()) == []


def test_timeout_rolls_back_everything(env):
    bridge = FakeBridge(connect_after=None)
    session = _session(bridge)
    with pytest.raises(LaunchError, match="did not connect within 0.2 s"):
        launcher.launch(session, bridge_timeout=0.2)
    proc = FakePopen.instances[0]
    assert proc.returncode is not None  # process tree was stopped
    _assert_rolled_back(session, bridge, env)
    assert ("wait", 0.25) in bridge.calls


def test_early_exit_with_user_data_dir_reports_profile_in_use(env, tmp_path):
    FakePopen.exit_code = 0
    FakePopen.stderr_text = b"".join(b"line %d\n" % i for i in range(30))
    bridge = FakeBridge(connect_after=None)
    session = _session(bridge, user_data_dir=str(tmp_path / "busy"))
    with pytest.raises(LaunchError) as info:
        launcher.launch(session, bridge_timeout=60)
    message = str(info.value)
    assert "exited during startup (exit code 0)" in message
    assert "already in use" in message
    assert "line 29" in message and "line 10" in message
    assert "line 9\n" not in message  # only the last 20 lines
    assert len([c for c in bridge.calls if isinstance(c, tuple)]) == 1  # no 60 s wait
    _assert_rolled_back(session, bridge, env)


def test_early_exit_without_user_data_dir_has_no_profile_hint(env):
    FakePopen.exit_code = 21
    bridge = FakeBridge(connect_after=None)
    session = _session(bridge)
    with pytest.raises(LaunchError) as info:
        launcher.launch(session, bridge_timeout=60)
    assert "exit code 21" in str(info.value)
    assert "already in use" not in str(info.value)
    _assert_rolled_back(session, bridge, env)


def test_spawn_failure_is_wrapped_and_rolled_back(env, monkeypatch):
    def boom(*args, **kwargs):
        raise FileNotFoundError("no such chrome")

    monkeypatch.setattr(launcher.subprocess, "Popen", boom)
    bridge = FakeBridge()
    session = _session(bridge)
    with pytest.raises(LaunchError) as info:
        launcher.launch(session, bridge_timeout=5)
    assert isinstance(info.value.__cause__, FileNotFoundError)
    _assert_rolled_back(session, bridge, env)


def test_emunium_error_is_reraised_unwrapped(env):
    original = BridgeError("port busy")
    bridge = FakeBridge(start_error=original)
    session = _session(bridge)
    with pytest.raises(BridgeError) as info:
        launcher.launch(session, bridge_timeout=5)
    assert info.value is original
    assert bridge.calls == ["start", "shutdown"]
    assert FakePopen.instances == []


# -- launcher: close ------------------------------------------------------------------


def test_close_is_idempotent(env):
    bridge = FakeBridge()
    session = _session(bridge)
    launcher.launch(session, bridge_timeout=5)
    launcher.close(session)
    events = list(env["events"])
    launcher.close(session)
    launcher.close(session)
    assert bridge.calls.count("shutdown") == 1
    assert env["events"] == events
    assert session._job is None
    assert list(env["tmp_root"].iterdir()) == []


def test_close_without_launch_is_noop(env):
    bridge = FakeBridge()
    session = _session(bridge)
    launcher.close(session)
    assert bridge.calls == []


class FakeStderr(io.BytesIO):
    def __init__(self, events, error=None, block=None):
        super().__init__()
        self.events = events
        self.error = error
        # Stands in for the reader lock held by a drain stuck in readline().
        self.block = block

    def close(self):
        self.events.append("stderr_close")
        if self.block is not None:
            self.block.wait()
        super().close()
        if self.error is not None:
            raise self.error


def _session_with_stderr(monkeypatch, error=None, block=None):
    events = []
    monkeypatch.setattr(
        launcher, "_stop_process_tree", lambda *a, **k: events.append("stop")
    )
    proc = FakePopen(["firefox"])
    proc.stderr = FakeStderr(events, error, block)
    log_files = contextlib.ExitStack()
    log_files.callback(events.append, "log_close")
    session = _session(process=proc, headless=True)
    session._log_files = log_files
    return session, proc, events


def test_close_closes_stderr_after_the_tree_and_before_the_log(env, monkeypatch):
    session, proc, events = _session_with_stderr(monkeypatch)
    launcher.close(session)
    assert events == ["stop", "stderr_close", "log_close"]
    assert proc.stderr.closed
    launcher.close(session)
    assert events == ["stop", "stderr_close", "log_close"]


@pytest.mark.parametrize("error", [OSError(9, "bad fd"), ValueError("busy")])
def test_close_survives_a_failing_stderr_close(env, monkeypatch, error):
    session, _, events = _session_with_stderr(monkeypatch, error)
    launcher.close(session)
    assert events == ["stop", "stderr_close", "log_close"]
    assert session.process is None and session._log_files is None


def test_close_does_not_hang_on_a_blocked_stderr_close(env, monkeypatch, caplog):
    assert launcher.STDERR_CLOSE_BUDGET_SECONDS <= 1.0
    budget = 0.3
    monkeypatch.setattr(launcher, "STDERR_CLOSE_BUDGET_SECONDS", budget)
    never_set = threading.Event()
    session, proc, events = _session_with_stderr(monkeypatch, block=never_set)
    try:
        started = time.monotonic()
        with caplog.at_level(logging.DEBUG, logger="emunium.browser"):
            launcher.close(session)
        elapsed = time.monotonic() - started
        # Upper bound only: join() may return a clock tick (~15.6 ms on
        # Windows) before monotonic() shows the full budget.
        assert elapsed < budget + 2.0
        assert events == ["stop", "stderr_close", "log_close"]
        assert not proc.stderr.closed  # left to the reader and GC
        assert session._log_files is None
        assert any("still busy" in r.getMessage() for r in caplog.records)
    finally:
        never_set.set()  # release the daemon closer only after the assertions


def _launch_windows(env, monkeypatch, **attrs):
    monkeypatch.setattr(launcher, "_IS_WINDOWS", True)
    session = _session(**attrs)
    launcher.launch(session, bridge_timeout=5)
    proc = FakePopen.instances[-1]
    del env["events"][:]
    return session, proc


def test_windows_launch_assigns_job(env, monkeypatch):
    monkeypatch.setattr(launcher, "_IS_WINDOWS", True)
    session = _session()
    launcher.launch(session, bridge_timeout=5)
    proc = FakePopen.instances[0]
    assert env["events"][:2] == [("create_job", 701), ("assign", 701, proc.pid)]
    assert session._job == 701
    launcher.close(session)


def test_windows_graceful_nonzero_still_waits_then_terminates_job(env, monkeypatch):
    env["state"]["graceful_rc"] = 128  # windowless renderers -> always non-zero
    session, proc = _launch_windows(env, monkeypatch)
    pid = proc.pid
    launcher.close(session)
    events = env["events"]
    assert events[0] == ("taskkill", "/PID", str(pid), "/T")
    assert events[1] == ("wait", pid, launcher.GRACEFUL_TASKKILL_GRACE_SECONDS)
    assert events[2] == ("terminate_job", 701, 1)
    assert events[3] == ("close_job", 701)
    assert not any(e[0] == "taskkill" and "/F" in e for e in events)
    assert proc.returncode == 1


def test_windows_headless_terminates_job_without_grace(env, monkeypatch):
    session, proc = _launch_windows(env, monkeypatch, headless=True)
    launcher.close(session)
    events = env["events"]
    assert events[0] == ("terminate_job", 701, 1)
    assert events[1] == ("close_job", 701)
    assert not any(e[0] == "taskkill" for e in events)
    assert proc.returncode == 1


def test_windows_headless_without_job_force_kills_at_once(env, monkeypatch):
    env["job_api"].assign_error = OSError(5, "access denied")
    session, proc = _launch_windows(env, monkeypatch, headless=True)
    launcher.close(session)
    assert env["events"][0] == ("taskkill", "/PID", str(proc.pid), "/T", "/F")
    assert ("taskkill", "/PID", str(proc.pid), "/T") not in env["events"]


def test_windows_graceful_exit_still_terminates_job(env, monkeypatch):
    env["state"]["graceful_rc"] = 0
    env["state"]["graceful_exits_parent"] = True
    session, proc = _launch_windows(env, monkeypatch)
    launcher.close(session)
    events = env["events"]
    assert events[0] == ("taskkill", "/PID", str(proc.pid), "/T")
    # parent already gone, but orphaned children may not be: kill the job anyway
    assert ("terminate_job", 701, 1) in events
    assert ("close_job", 701) in events
    assert not any(e[0] == "taskkill" and "/F" in e for e in events)


def test_windows_assign_failure_falls_back_to_taskkill(env, monkeypatch, caplog):
    env["job_api"].assign_error = OSError(5, "access denied")
    with caplog.at_level(logging.DEBUG, logger="emunium.browser"):
        session, proc = _launch_windows(env, monkeypatch)
    assert session._job is None
    assert any(
        "AssignProcessToJobObject failed" in r.getMessage() for r in caplog.records
    )
    launcher.close(session)
    events = env["events"]
    assert events[0] == ("taskkill", "/PID", str(proc.pid), "/T")
    assert events[1] == ("wait", proc.pid, launcher.GRACEFUL_TASKKILL_GRACE_SECONDS)
    assert ("taskkill", "/PID", str(proc.pid), "/T", "/F") in events
    assert not any(e[0] == "terminate_job" for e in events)


def test_windows_assign_failure_closes_unused_job(env, monkeypatch):
    env["job_api"].assign_error = OSError(5, "access denied")
    monkeypatch.setattr(launcher, "_IS_WINDOWS", True)
    session = _session()
    launcher.launch(session, bridge_timeout=5)
    assert ("close_job", 701) in env["events"]
    assert ("terminate_job", 701, 1) not in env["events"]
    launcher.close(session)


def test_windows_no_job_and_parent_gone_warns(env, monkeypatch, caplog):
    env["job_api"].assign_error = OSError(5, "access denied")
    env["state"]["graceful_exits_parent"] = True
    session, _ = _launch_windows(env, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="emunium.browser"):
        launcher.close(session)
    assert not any(e[0] == "taskkill" and "/F" in e for e in env["events"])
    warnings = [
        r.getMessage()
        for r in caplog.records
        if "child processes may remain" in r.getMessage()
    ]
    assert len(warnings) == 1
    # The resolved browser (Chrome for Testing here), never a hard-coded "Chrome".
    assert warnings[0].startswith(f"{session.resolved.label} (pid ")


def test_parent_gone_warning_without_a_resolved_browser_is_neutral(
    env, monkeypatch, caplog
):
    monkeypatch.setattr(launcher, "_IS_WINDOWS", True)
    proc = FakePopen(["msedge"])
    proc.returncode = 0
    session = _session(process=proc, headless=True)
    with caplog.at_level(logging.WARNING, logger="emunium.browser"):
        launcher.close(session)
    expected = (
        f"Browser (pid {proc.pid}) exited before its process tree could be killed;"
        " child processes may remain"
    )
    assert [r.getMessage() for r in caplog.records] == [expected]


def test_windows_job_api_unavailable_falls_back(env, monkeypatch):
    def unavailable():
        raise OSError("no kernel32")

    monkeypatch.setattr(launcher, "_job_api", unavailable)
    session, proc = _launch_windows(env, monkeypatch)
    assert session._job is None
    launcher.close(session)
    assert ("taskkill", "/PID", str(proc.pid), "/T", "/F") in env["events"]


def test_posix_close_path_unchanged(env, monkeypatch):
    import signal

    monkeypatch.setattr(launcher, "_IS_WINDOWS", False)
    session = _session()
    launcher.launch(session, bridge_timeout=5)
    proc = FakePopen.instances[0]
    assert session._job is None
    del env["events"][:]
    launcher.close(session)
    events = env["events"]
    assert events[0] == ("killpg", proc.pid, signal.SIGTERM)
    assert ("killpg", proc.pid, launcher._SIGKILL) in events
    assert env["taskkills"] == []
    assert not any(e[0] in ("create_job", "terminate_job") for e in events)


class TabsBridge(FakeBridge):
    """A connected bridge that serves ``listTabs`` / ``closeTab`` from a list."""

    def __init__(self, events, tab_ids=(11, 12), **kwargs):
        super().__init__(**kwargs)
        self.events = events
        self.tab_ids = list(tab_ids)
        self.connected = True
        self.list_error = None
        self.close_errors = {}
        self.on_close = None  # callable(tab_id), e.g. advance a fake clock

    def list_tabs(self, timeout=10.0):
        self.events.append(("list_tabs", timeout))
        if self.list_error is not None:
            raise self.list_error
        return [{"tabId": tab_id, "url": "about:blank"} for tab_id in self.tab_ids]

    def close_tab(self, tab_id, timeout=10.0):
        self.events.append(("close_tab", tab_id, timeout))
        if self.on_close is not None:
            self.on_close(tab_id)
        if tab_id in self.close_errors:
            raise self.close_errors[tab_id]
        self.tab_ids.remove(tab_id)
        if not self.tab_ids:
            # Chrome exits with its last window; the reply never arrives.
            for proc in FakePopen.instances:
                proc.returncode = 0
            raise BridgeError("disconnected")
        return {"closed": True}


def _launch_windows_with_tabs(env, monkeypatch, **attrs):
    bridge = TabsBridge(env["events"])
    session, proc = _launch_windows(env, monkeypatch, bridge=bridge, **attrs)
    return session, proc, bridge


def test_close_budgets():
    assert launcher.CLOSE_TABS_BUDGET_SECONDS == 1.0
    assert launcher.GRACEFUL_TASKKILL_GRACE_SECONDS == 2.0


def test_windows_headed_close_closes_tabs_then_taskkill_then_job(env, monkeypatch):
    bridge = TabsBridge(env["events"], tab_ids=(11, 12, 13))
    session, proc = _launch_windows(env, monkeypatch, bridge=bridge)
    pid = proc.pid
    launcher.close(session)
    events = env["events"]
    kinds = [e[0] for e in events]
    assert kinds[:4] == ["list_tabs", "close_tab", "close_tab", "close_tab"]
    assert [e[1] for e in events[1:4]] == [11, 12, 13]
    assert events[0][1] == launcher.CLOSE_TABS_BUDGET_SECONDS
    assert all(0 < e[2] <= launcher.CLOSE_TABS_BUDGET_SECONDS for e in events[1:4])
    assert proc.returncode is not None  # the last closeTab took Chrome down
    assert events[4] == ("taskkill", "/PID", str(pid), "/T")
    assert events[5] == ("wait", pid, launcher.GRACEFUL_TASKKILL_GRACE_SECONDS)
    assert events[6:8] == [("terminate_job", 701, 1), ("close_job", 701)]
    # the bridge is shut down only after the process tree is gone
    assert bridge.calls[-1] == "shutdown"


def test_windows_headed_close_waits_two_seconds_when_tabs_do_not_exit(env, monkeypatch):
    session, proc, bridge = _launch_windows_with_tabs(env, monkeypatch)
    bridge.close_errors = {11: BridgeError("gone"), 12: BridgeError("gone")}
    launcher.close(session)
    events = env["events"]
    assert [e[0] for e in events[:3]] == ["list_tabs", "close_tab", "close_tab"]
    assert events[3] == ("taskkill", "/PID", str(proc.pid), "/T")
    assert events[4] == ("wait", proc.pid, 2.0)
    assert events[5] == ("terminate_job", 701, 1)


def test_windows_headed_close_ignores_list_tabs_error(env, monkeypatch):
    session, proc, bridge = _launch_windows_with_tabs(env, monkeypatch)
    bridge.list_error = BridgeError("no reply")
    launcher.close(session)
    events = env["events"]
    assert events[0][0] == "list_tabs"
    assert not any(e[0] == "close_tab" for e in events)
    assert events[1] == ("taskkill", "/PID", str(proc.pid), "/T")
    assert ("terminate_job", 701, 1) in events


def test_windows_headed_close_tab_budget_is_overall(env, monkeypatch):
    session, proc, bridge = _launch_windows_with_tabs(env, monkeypatch)
    bridge.tab_ids = [11, 12, 13, 14]
    clock = {"now": 1000.0}
    monkeypatch.setattr(launcher.time, "monotonic", lambda: clock["now"])

    def slow_close(tab_id):
        clock["now"] += 0.6

    bridge.on_close = slow_close
    launcher.close(session)
    closes = [e for e in env["events"] if e[0] == "close_tab"]
    # 0.6 s per tab: the second close starts with 0.4 s left, the third never starts.
    assert [e[1] for e in closes] == [11, 12]
    assert closes[0][2] == pytest.approx(1.0)
    assert closes[1][2] == pytest.approx(0.4)
    assert ("taskkill", "/PID", str(proc.pid), "/T") in env["events"]


def test_windows_headed_close_skips_tabs_when_disconnected(env, monkeypatch):
    session, proc, bridge = _launch_windows_with_tabs(env, monkeypatch)
    bridge.connected = False
    launcher.close(session)
    assert env["events"][0] == ("taskkill", "/PID", str(proc.pid), "/T")
    assert not any(e[0] in ("list_tabs", "close_tab") for e in env["events"])


def test_windows_headed_close_skips_tabs_when_chrome_already_exited(env, monkeypatch):
    session, proc, _bridge = _launch_windows_with_tabs(env, monkeypatch)
    proc.returncode = 0
    launcher.close(session)
    assert not any(e[0] in ("list_tabs", "close_tab") for e in env["events"])


def test_windows_headless_close_does_not_close_tabs(env, monkeypatch):
    session, _proc, _bridge = _launch_windows_with_tabs(env, monkeypatch, headless=True)
    launcher.close(session)
    assert env["events"][0] == ("terminate_job", 701, 1)
    assert not any(e[0] in ("list_tabs", "close_tab") for e in env["events"])


def test_posix_close_does_not_close_tabs(env, monkeypatch):
    monkeypatch.setattr(launcher, "_IS_WINDOWS", False)
    session = _session(TabsBridge(env["events"]))
    launcher.launch(session, bridge_timeout=5)
    del env["events"][:]
    launcher.close(session)
    assert not any(e[0] in ("list_tabs", "close_tab") for e in env["events"])


@pytest.mark.skipif(os.name != "nt", reason="kernel32 Job Objects are Windows-only")
def test_real_kernel32_job_structs_are_accepted():
    # Creates and closes an empty job: validates the ctypes layout, no process involved.
    api = launcher._Kernel32Jobs()
    job = api.create_kill_on_close_job()
    assert job
    api.close(job)


def test_close_warns_when_session_dir_survives(env, monkeypatch, caplog):
    session = _session()
    launcher.launch(session, bridge_timeout=5)
    session_dir = session.session_dir
    attempts = []

    def locked(path, *a, **k):
        attempts.append(path)
        raise PermissionError("in use")

    monkeypatch.setattr(launcher.shutil, "rmtree", locked)
    with caplog.at_level(logging.WARNING, logger="emunium.browser"):
        launcher.close(session)
    assert len(attempts) == launcher.RMTREE_ATTEMPTS == 10
    assert any(session_dir in r.getMessage() for r in caplog.records)


def test_close_retries_rmtree_until_released(env, monkeypatch):
    session = _session()
    launcher.launch(session, bridge_timeout=5)
    real_rmtree = launcher.shutil.rmtree
    attempts = []

    def flaky(path, *a, **k):
        attempts.append(path)
        if len(attempts) < 3:
            raise PermissionError("still held by chrome")
        real_rmtree(path, *a, **k)

    monkeypatch.setattr(launcher.shutil, "rmtree", flaky)
    launcher.close(session)
    assert len(attempts) == 3
    assert list(env["tmp_root"].iterdir()) == []


def test_facade_session_defaults():
    browser = Browser(headless=True, user_data_dir=None)
    session = browser._session
    assert session.browser is None
    assert session.chrome_path is None
    assert session.resolved is None
    assert browser.browser_name is None
    assert session.extra_args == []
    assert session.session_dir is None
    assert session.extension_dir is None
    assert session.profile_dir is None
    assert session.log_path is None
    assert session.headless is True


# -- browser resolution ------------------------------------------------------------------


def _installed(name, path=None):
    method = {"chrome": "cdp-pipe", "firefox": "bidi"}.get(name, "load-extension")
    family = "firefox" if name == "firefox" else "chromium"
    return InstalledBrowser(name, family, path or f"/opt/{name}/{name}", method)


class Resolver:
    """resolve_browser() with every OS dependency injected and recorded."""

    def __init__(self, installed=(), files=()):
        self.installed = list(installed)
        self.files = set(files)
        self.calls = []

    def discover(self):
        self.calls.append("discover")
        return list(self.installed)

    def find(self, name):
        self.calls.append(("find", name))
        return next((b for b in self.installed if b.name == name), None)

    def install_cft(self):
        self.calls.append("cft")
        return "/cft/chrome-linux64/chrome"

    def __call__(self, browser=None, chrome_path=None, env=None, **kwargs):
        kwargs.setdefault("cft_dir", "/cft")
        return launcher.resolve_browser(
            browser,
            chrome_path,
            env={} if env is None else env,
            discover=self.discover,
            find=self.find,
            install_cft=self.install_cft,
            is_file=lambda p: p in self.files,
            **kwargs,
        )


def test_auto_picks_the_first_of_the_default_preference():
    # find_browsers order is irrelevant: DEFAULT_PREFERENCE decides.
    resolve = Resolver(
        [_installed(n) for n in ("firefox", "chromium", "brave", "edge")]
    )
    picked = resolve()
    assert (picked.name, picked.install_method) == ("edge", "load-extension")
    assert resolve.calls == ["discover"]
    resolve.installed.append(_installed("chrome"))
    picked = resolve(browser="auto")
    assert (picked.name, picked.family, picked.install_method) == (
        "chrome",
        "chromium",
        "cdp-pipe",
    )
    assert picked.path == "/opt/chrome/chrome"


@pytest.mark.parametrize(
    ("names", "expected"),
    [(("brave", "chromium"), "brave"), (("chromium",), "chromium")],
)
def test_auto_order_falls_through(names, expected):
    assert Resolver([_installed(n) for n in names])().name == expected


@pytest.mark.parametrize("installed", [(), ("firefox",)])
def test_auto_falls_back_to_chrome_for_testing(installed):
    # Firefox is never picked implicitly.
    resolve = Resolver([_installed(n) for n in installed])
    picked = resolve()
    assert picked == launcher.ResolvedBrowser(
        "cft", "/cft/chrome-linux64/chrome", "chromium", "load-extension"
    )
    assert resolve.calls == ["discover", "cft"]


def test_browser_name_uses_find_browser():
    resolve = Resolver([_installed("firefox", r"C:\Mozilla Firefox\firefox.exe")])
    picked = resolve(browser="Firefox")
    assert picked == launcher.ResolvedBrowser(
        "firefox", r"C:\Mozilla Firefox\firefox.exe", "firefox", "bidi"
    )
    assert resolve.calls == [("find", "firefox")]


@pytest.mark.parametrize(
    ("platform", "searched"),
    [
        ("win32", r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe"),
        ("win32", r"App Paths\msedge.exe"),
        ("darwin", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ("linux", "PATH for microsoft-edge, microsoft-edge-stable"),
    ],
)
def test_missing_named_browser_names_what_was_searched(platform, searched):
    with pytest.raises(LaunchError, match="'edge' is not installed") as info:
        Resolver()(browser="edge", platform=platform)
    assert searched in str(info.value)


def test_cft_name_installs_without_discovery():
    resolve = Resolver([_installed("chrome")])
    assert resolve(browser="cft").name == "cft"
    assert resolve.calls == ["cft"]


def test_browser_path_must_exist():
    with pytest.raises(LaunchError, match="not found: /nope/chrome") as info:
        Resolver()(browser="/nope/chrome")
    assert "chrome, edge, brave, chromium, firefox, cft, auto" in str(info.value)


@pytest.mark.parametrize(
    ("path", "name", "method"),
    [
        (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            "chrome",
            "cdp-pipe",
        ),
        (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "chrome",
            "cdp-pipe",
        ),
        ("/opt/google/chrome/chrome", "chrome", "cdp-pipe"),
        ("/cft/chrome-win64/chrome.exe", "cft", "load-extension"),
        (
            "/x/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
            "cft",
            "load-extension",
        ),
        (r"C:\Program Files\Mozilla Firefox\firefox.exe", "firefox", "bidi"),
        ("/usr/lib/firefox-esr/firefox-esr", "firefox", "bidi"),
        (r"C:\Apps\camoufox\camoufox.exe", "firefox", "bidi"),
        (r"C:\Apps\waterfox\waterfox.exe", "firefox", "bidi"),
        ("/usr/bin/librewolf", "firefox", "bidi"),
        ("/opt/zen/zen", "firefox", "bidi"),
        (
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            "edge",
            "load-extension",
        ),
        (
            r"C:\BraveSoftware\Brave-Browser\Application\brave.exe",
            "brave",
            "load-extension",
        ),
        # discovery.LINUX_COMMANDS["edge"] names, as resolved on PATH.
        ("/usr/bin/microsoft-edge", "edge", "load-extension"),
        ("/usr/bin/microsoft-edge-stable", "edge", "load-extension"),
        ("/opt/microsoft/msedge/microsoft-edge-beta", "edge", "load-extension"),
        (r"D:\Portable\Edge\Microsoft-Edge-Dev.exe", "edge", "load-extension"),
        ("/usr/lib/chromium/chrome", "chromium", "load-extension"),
        ("/opt/tools/chrome", "custom", "load-extension"),
        ("/opt/tools/edge-microsoft", "custom", "load-extension"),
    ],
)
def test_path_classification(path, name, method):
    resolve = Resolver(files=[path])
    picked = resolve(browser=path)
    assert (picked.name, picked.path, picked.install_method) == (name, path, method)
    assert picked.family == ("firefox" if method == "bidi" else "chromium")
    assert resolve.calls == []


def test_cft_dir_default_is_the_installer_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "_install_dir", lambda: tmp_path / "CfT_Browser")
    exe = str(tmp_path / "CfT_Browser" / "chrome-mac-x64" / "Google" / "Chrome")
    assert Resolver(files=[exe])(browser=exe, cft_dir=None).name == "cft"


def test_precedence_browser_then_chrome_path_then_env_then_auto():
    resolve = Resolver([_installed("edge")], files=["/a/chrome", "/b/chrome"])
    env = {installer.CHROME_PATH_ENV: "/b/chrome"}
    assert resolve("edge", "/a/chrome", env).name == "edge"
    assert resolve(None, "/a/chrome", env).path == "/a/chrome"
    assert resolve(None, None, env).path == "/b/chrome"
    assert resolve.calls == [("find", "edge")]  # no discovery behind an override
    assert resolve().name == "edge"


def test_missing_env_override_names_its_source():
    env = {installer.CHROME_PATH_ENV: "/gone/chrome"}
    with pytest.raises(
        LaunchError, match=r"not found: /gone/chrome \(EMUNIUM_CHROME_PATH\)"
    ):
        Resolver()(env=env)


# -- launch per install method -------------------------------------------------------------


def test_launch_records_the_auto_pick_and_loads_with_flags(env, tmp_path):
    edge = tmp_path / "Edge" / "msedge.exe"
    env["installed"].append(_installed("edge", str(edge)))
    session = _session()
    launcher.launch(session, bridge_timeout=5)
    assert session.resolved == launcher.ResolvedBrowser(
        "edge", str(edge), "chromium", "load-extension"
    )
    assert session.executable_path == str(edge)
    args = FakePopen.instances[0].args
    assert args[0] == str(edge)
    assert f"--load-extension={session.extension_dir}" in args
    assert env["ensure_calls"] == []
    launcher.close(session)
    assert session.resolved.name == "edge"  # kept for browser_name


def test_launch_falls_back_to_chrome_for_testing(env):
    session = _session()
    launcher.launch(session, bridge_timeout=5)
    assert env["ensure_calls"] == [1]
    assert (session.resolved.name, session.resolved.path) == ("cft", env["exe"])
    launcher.close(session)


def test_missing_named_browser_fails_before_the_bridge(env):
    bridge = FakeBridge()
    with pytest.raises(LaunchError, match="'brave' is not installed; searched"):
        launcher.launch(_session(bridge, browser="brave"), bridge_timeout=5)
    assert bridge.calls == [] and FakePopen.instances == []


class FakeCdpPipe:
    instances: ClassVar[list[FakeCdpPipe]] = []
    load_error = None

    def __init__(self, events):
        self.events = events
        FakeCdpPipe.instances.append(self)

    def _log(self, *event):
        # The Popen count shows where each call falls relative to the spawn.
        self.events.append(("pipe", *event, len(FakePopen.instances)))

    def prepare(self):
        self._log("prepare")
        return ["--remote-debugging-pipe", "--enable-unsafe-extension-debugging"]

    def wrap_args(self, args):
        self._log("wrap_args")
        return ["/bin/sh", "-c", "trampoline", "sh", *args]

    def popen_kwargs(self):
        self._log("popen_kwargs")
        return {"pass_fds": (11, 12)}

    def after_spawn(self):
        self._log("after_spawn")

    def load_unpacked(self, path):
        self._log("load_unpacked", str(path))
        if FakeCdpPipe.load_error is not None:
            raise FakeCdpPipe.load_error
        return "extid"

    def close(self):
        self._log("close")


@pytest.fixture
def chrome(env, monkeypatch, tmp_path):
    FakeCdpPipe.instances = []
    FakeCdpPipe.load_error = None
    monkeypatch.setattr(launcher, "CdpPipe", lambda: FakeCdpPipe(env["events"]))
    exe = tmp_path / "Google" / "Chrome" / "Application" / "chrome.exe"
    env["installed"].append(_installed("chrome", str(exe)))
    return str(exe)


def test_cdp_pipe_spawn_sequence(env, chrome, monkeypatch):
    monkeypatch.setattr(launcher, "_IS_WINDOWS", True)
    session = _session(headless=True, extra_args=["--x"])
    launcher.launch(session, bridge_timeout=5)
    ext = session.extension_dir
    proc = FakePopen.instances[0]
    assert proc.args[:4] == ["/bin/sh", "-c", "trampoline", "sh"]
    args = proc.args[4:]
    assert args[0] == chrome and args[1] == f"--user-data-dir={session.profile_dir}"
    assert "--remote-debugging-pipe" in args and args[-2:] == ["--headless=new", "--x"]
    assert not any(
        a.startswith(("--load-extension", "--disable-extensions")) for a in args
    )
    # Pipe kwargs merged with the usual ones.
    assert proc.kwargs["pass_fds"] == (11, 12)
    assert (
        proc.kwargs["stdin"] is subprocess.DEVNULL and proc.kwargs["stderr"] is not None
    )
    assert (
        json.loads(Path(ext, "session.json").read_text(encoding="utf-8"))["port"]
        == 48123
    )
    assert Path(session.profile_dir, "Default", "Preferences").is_file()
    assert env["events"][:7] == [
        ("pipe", "prepare", 0),
        ("pipe", "wrap_args", 0),
        ("pipe", "popen_kwargs", 0),
        ("create_job", 701),
        ("assign", 701, proc.pid),
        ("pipe", "after_spawn", 1),
        ("pipe", "load_unpacked", ext, 1),
    ]
    assert session.bridge.calls[-1][0] == "wait"  # bridge waited for after the load
    assert session.resolved.install_method == "cdp-pipe"
    del env["events"][:]
    launcher.close(session)
    assert [e for e in env["events"] if e[0] != "wait"] == [
        ("terminate_job", 701, 1),
        ("close_job", 701),
        ("pipe", "close", 1),
    ]
    assert session._cdp_pipe is None


def test_cdp_pipe_load_failure_names_the_browser_and_rolls_back(env, chrome):
    FakeCdpPipe.load_error = LaunchError("CDP Extensions.loadUnpacked failed: nope")
    bridge = FakeBridge()
    session = _session(bridge)
    with pytest.raises(LaunchError) as info:
        launcher.launch(session, bridge_timeout=5)
    assert f"into chrome ({chrome}) over CDP pipe" in str(info.value)
    assert ("pipe", "close", 1) in env["events"]
    _assert_rolled_back(session, bridge, env)


def test_cdp_pipe_early_exit_reports_the_exit(env, chrome):
    FakePopen.exit_code = 3
    FakeCdpPipe.load_error = LaunchError(
        "CDP Extensions.loadUnpacked: browser closed the pipe"
    )
    with pytest.raises(
        LaunchError, match=r"chrome \(.*\) exited during startup \(exit code 3\)"
    ):
        launcher.launch(_session(), bridge_timeout=5)


class FakeBidi:
    instances: ClassVar[list[FakeBidi]] = []
    events: ClassVar[list[tuple[object, ...]]] = []

    def __init__(self, url):
        FakeBidi.events.append(("bidi", "connect", url))
        FakeBidi.instances.append(self)

    def new_session(self):
        FakeBidi.events.append(("bidi", "new_session"))
        return {}

    def install_extension(self, path):
        FakeBidi.events.append(("bidi", "install", str(path)))
        return "bridge@emunium.dev"

    def close(self):
        FakeBidi.events.append(("bidi", "close"))


@pytest.fixture
def firefox(env, monkeypatch, tmp_path):
    FakeBidi.instances = []
    FakeBidi.events = env["events"]
    reads = []
    state = {"error": None}

    def fake_read(stream, timeout, *, sink):
        reads.append((stream, timeout, sink))
        env["events"].append(("read_bidi_url", timeout))
        sink.write(b"WebDriver BiDi listening on ws://127.0.0.1:9333\n")
        sink.flush()
        if state["error"] is not None:
            raise state["error"]
        return "ws://127.0.0.1:9333"

    monkeypatch.setattr(launcher, "read_bidi_url", fake_read)
    monkeypatch.setattr(launcher, "BidiSession", FakeBidi)
    exe = tmp_path / "Mozilla Firefox" / "firefox.exe"
    env["installed"].append(_installed("firefox", str(exe)))
    return {"exe": str(exe), "reads": reads, "state": state}


@pytest.mark.parametrize("windows", [True, False])
def test_bidi_spawn_sequence(env, firefox, monkeypatch, windows):
    monkeypatch.setattr(launcher, "_IS_WINDOWS", windows)
    session = _session(browser="firefox", headless=True, extra_args=["-x"])
    launcher.launch(session, bridge_timeout=7)
    proc = FakePopen.instances[0]
    profile = session.profile_dir
    expected = [
        firefox["exe"],
        "-profile",
        profile,
        "-no-remote",
        "--remote-debugging-port=0",
    ]
    expected += ["-headless", *(["-wait-for-browser"] if windows else []), "-x"]
    assert proc.args == expected
    assert proc.kwargs["stderr"] is subprocess.PIPE
    assert proc.kwargs["start_new_session"] is (not windows)
    ext = Path(session.extension_dir)
    manifest = json.loads((ext / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["browser_specific_settings"]["gecko"]["id"] == "bridge@emunium.dev"
    assert (
        json.loads((ext / "session.json").read_text(encoding="utf-8"))["token"] == TOKEN
    )
    assert Path(profile, "user.js").is_file()
    assert not Path(profile, "Default").exists()  # no Chromium prefs
    stream, timeout, sink = firefox["reads"][0]
    assert stream is proc.stderr and timeout == 7
    assert sink.name == session.log_path and session.log_path.endswith("firefox.log")
    assert not sink.closed and session._log_files is not None
    bidi_events = [
        e for e in env["events"] if e[0] in ("bidi", "read_bidi_url", "assign")
    ]
    assert bidi_events == [
        *([("assign", 701, proc.pid)] if windows else []),
        ("read_bidi_url", 7),
        ("bidi", "connect", "ws://127.0.0.1:9333"),
        ("bidi", "new_session"),
        ("bidi", "install", str(ext)),
    ]
    assert session._bidi is FakeBidi.instances[0]
    log_path = session.log_path
    del env["events"][:]
    launcher.close(session)
    assert env["events"][-1] == ("bidi", "close")
    if windows:
        assert env["events"][:2] == [("terminate_job", 701, 1), ("close_job", 701)]
    assert sink.closed and session._bidi is None and session._log_files is None
    assert not os.path.exists(log_path)


def test_bidi_uses_user_data_dir_as_profile(env, firefox, tmp_path):
    profile = tmp_path / "ff-profile"
    session = _session(browser="firefox", user_data_dir=str(profile))
    launcher.launch(session, bridge_timeout=5)
    assert FakePopen.instances[0].args[2] == str(profile.resolve())
    launcher.close(session)
    assert (profile / "user.js").is_file()


def test_bidi_stub_exit_reports_browser_and_log(env, firefox):
    FakePopen.exit_code = 0
    firefox["state"]["error"] = LaunchError(
        "Firefox stderr closed before WebDriver BiDi"
    )
    bridge = FakeBridge()
    session = _session(bridge, browser="firefox")
    with pytest.raises(LaunchError) as info:
        launcher.launch(session, bridge_timeout=5)
    message = str(info.value)
    assert f"firefox ({firefox['exe']}) exited during startup (exit code 0)" in message
    assert "WebDriver BiDi listening" in message  # the log tail came from the sink
    _assert_rolled_back(session, bridge, env)


def test_bidi_failure_while_running_names_the_browser(env, firefox):
    firefox["state"]["error"] = LaunchError(
        "did not announce WebDriver BiDi within 5s."
    )
    bridge = FakeBridge()
    session = _session(bridge, browser="firefox")
    with pytest.raises(LaunchError, match="into firefox .* over WebDriver BiDi"):
        launcher.launch(session, bridge_timeout=5)
    assert FakePopen.instances[0].returncode is not None  # tree stopped
    _assert_rolled_back(session, bridge, env)


def test_timeout_message_names_the_browser(env):
    with pytest.raises(LaunchError, match=r"within 0\.2 s \(cft \("):
        launcher.launch(_session(FakeBridge(connect_after=None)), bridge_timeout=0.2)


# -- installer ---------------------------------------------------------------------------


class FakeResponse(io.BytesIO):
    """A urlopen() response; BytesIO's own context manager closes it on exit."""

    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


def _zip_bytes(entries):
    """entries: (name, data, unix_mode or None)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            if mode is not None:
                info.external_attr = (mode & 0xFFFF) << 16
            zf.writestr(info, data)
    return buf.getvalue()


LINUX_ZIP = [
    ("chrome-linux64/", b"", stat.S_IFDIR | 0o755),
    ("chrome-linux64/chrome", b"ELF", stat.S_IFREG | 0o755),
    ("chrome-linux64/resources.pak", b"pak", stat.S_IFREG | 0o644),
]


@pytest.fixture
def inst(monkeypatch, tmp_path):
    dest = tmp_path / "CfT_Browser"
    state = {"zip": _zip_bytes(LINUX_ZIP), "urls": [], "timeouts": []}

    def fake_urlopen(url, timeout=None):
        state["urls"].append(url)
        state["timeouts"].append(timeout)
        if url == installer.VERSION_URL:
            return FakeResponse(b"146.0.7680.0\n")
        return FakeResponse(state["zip"])

    monkeypatch.delenv(installer.CHROME_PATH_ENV, raising=False)
    monkeypatch.setattr(installer, "_install_dir", lambda: dest)
    monkeypatch.setattr(installer, "_get_platform_tag", lambda: "linux64")
    monkeypatch.setattr(installer.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(installer, "_use_ditto", lambda: False)
    state["dest"] = dest
    return state


def test_installer_downloads_with_timeouts_and_marks(inst):
    exe = installer.ensure_chrome()
    dest = inst["dest"]
    assert exe == str(dest / "chrome-linux64" / "chrome")
    assert (dest / "chrome-linux64" / installer.MARKER_NAME).is_file()
    assert inst["urls"][0] == installer.VERSION_URL
    assert inst["urls"][1].endswith("/146.0.7680.0/linux64/chrome-linux64.zip")
    assert all(t == 30.0 for t in inst["timeouts"])
    assert installer.DOWNLOAD_TIMEOUT == 300.0
    # no leftovers: zip, staging, lock
    assert sorted(p.name for p in dest.iterdir()) == ["chrome-linux64"]


def test_installer_reuses_marked_install(inst):
    installer.ensure_chrome()
    inst["urls"].clear()
    installer.ensure_chrome()
    assert inst["urls"] == []


def test_installer_accepts_and_marks_legacy_install(inst):
    root = inst["dest"] / "chrome-linux64"
    root.mkdir(parents=True)
    (root / "chrome").write_bytes(b"ELF")
    assert installer.ensure_chrome() == str(root / "chrome")
    assert (root / installer.MARKER_NAME).is_file()
    assert inst["urls"] == []


def test_installer_replaces_half_extracted_tree(inst):
    root = inst["dest"] / "chrome-linux64"
    root.mkdir(parents=True)
    (root / "resources.pak").write_bytes(b"partial")
    installer.ensure_chrome()
    assert (root / "chrome").read_bytes() == b"ELF"
    assert (root / installer.MARKER_NAME).is_file()
    assert sorted(p.name for p in inst["dest"].iterdir()) == ["chrome-linux64"]


def test_installer_failed_extract_leaves_no_install(inst):
    inst["zip"] = _zip_bytes([("chrome-linux64/resources.pak", b"pak", None)])
    with pytest.raises(LaunchError, match="did not contain"):
        installer.ensure_chrome()
    assert list(inst["dest"].iterdir()) == []


def test_installer_truncated_download_leaves_no_install(inst, monkeypatch):
    class Short(FakeResponse):
        def __init__(self, data):
            super().__init__(data[: len(data) // 2])
            self.headers = {"Content-Length": str(len(data))}

    def fake_urlopen(url, timeout=None):
        if url == installer.VERSION_URL:
            return FakeResponse(b"146.0.7680.0")
        return Short(inst["zip"])

    monkeypatch.setattr(installer.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(OSError, match="truncated"):
        installer.ensure_chrome()
    assert list(inst["dest"].iterdir()) == []


def test_installer_uses_unique_temp_zip(inst, monkeypatch):
    seen = []
    real_mkstemp = installer.tempfile.mkstemp

    def spy(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        seen.append(name)
        return fd, name

    monkeypatch.setattr(installer.tempfile, "mkstemp", spy)
    installer.ensure_chrome()
    assert len(seen) == 1
    assert Path(seen[0]).parent == inst["dest"]
    assert Path(seen[0]).name != "chrome_download.zip"
    assert not Path(seen[0]).exists()


def test_installer_restores_unix_permissions(inst, monkeypatch):
    chmods = {}
    monkeypatch.setattr(installer, "_POSIX_PERMISSIONS", True)
    real_chmod = installer.os.chmod

    def record(path, mode):
        chmods[os.path.basename(str(path))] = mode
        if os.name != "nt":
            real_chmod(path, mode)

    monkeypatch.setattr(installer.os, "chmod", record)
    installer.ensure_chrome()
    assert chmods["resources.pak"] == 0o644
    assert chmods["chrome"] & 0o111 == 0o111
    assert chmods["chrome-linux64"] == 0o755


@pytest.mark.skipif(os.name == "nt", reason="symlinks need POSIX")
def test_installer_restores_symlinks(inst):
    inst["zip"] = _zip_bytes(
        LINUX_ZIP + [("chrome-linux64/link", b"chrome", stat.S_IFLNK | 0o777)]
    )
    installer.ensure_chrome()
    link = inst["dest"] / "chrome-linux64" / "link"
    assert link.is_symlink() and os.readlink(str(link)) == "chrome"


def test_installer_rejects_path_traversal(inst):
    inst["zip"] = _zip_bytes(LINUX_ZIP + [("../evil.txt", b"x", None)])
    with pytest.raises(LaunchError, match="Unsafe path"):
        installer.ensure_chrome()
    assert not (inst["dest"].parent / "evil.txt").exists()


def test_installer_uses_ditto_on_macos(inst, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(installer, "_use_ditto", lambda: True)

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        with zipfile.ZipFile(cmd[3]) as zf:
            zf.extractall(cmd[4])
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    installer.ensure_chrome()
    assert calls and calls[0][:3] == ["ditto", "-x", "-k"]
    assert calls[0][3].endswith(".zip")


def test_installer_env_override(monkeypatch, tmp_path, inst):
    custom = tmp_path / "my-chrome"
    custom.write_bytes(b"")
    monkeypatch.setenv(installer.CHROME_PATH_ENV, str(custom))
    assert installer.ensure_chrome() == str(custom)
    assert inst["urls"] == []
    monkeypatch.setenv(installer.CHROME_PATH_ENV, str(tmp_path / "missing"))
    with pytest.raises(LaunchError):
        installer.ensure_chrome()


def test_installer_breaks_stale_lock(inst):
    dest = inst["dest"]
    dest.mkdir(parents=True)
    lock = dest / installer.LOCK_NAME
    lock.write_text("999 0\n", encoding="ascii")
    old = os.stat(str(lock)).st_mtime - installer.LOCK_STALE_SECONDS - 60
    os.utime(str(lock), (old, old))
    installer.ensure_chrome()
    assert not lock.exists()
    assert (dest / "chrome-linux64" / installer.MARKER_NAME).is_file()


def _stale_lock(tmp_path):
    lock = tmp_path / installer.LOCK_NAME
    lock.write_text("999 0\n", encoding="ascii")
    old = os.stat(str(lock)).st_mtime - installer.LOCK_STALE_SECONDS - 60
    os.utime(str(lock), (old, old))
    return lock


def test_stale_lock_takeover_renames_then_creates(tmp_path):
    lock = _stale_lock(tmp_path)
    assert installer._try_acquire_lock(lock) is True
    assert lock.read_text(encoding="ascii").startswith(f"{os.getpid()} ")
    assert sorted(p.name for p in tmp_path.iterdir()) == [installer.LOCK_NAME]


def test_stale_lock_takeover_lost_rename_does_not_create(tmp_path, monkeypatch):
    lock = _stale_lock(tmp_path)

    def other_waiter_renamed_first(src, dst):
        raise FileNotFoundError(src)

    monkeypatch.setattr(installer.os, "replace", other_waiter_renamed_first)
    assert installer._try_acquire_lock(lock) is False
    assert lock.read_text(encoding="ascii") == "999 0\n"  # untouched, no new lock


def test_stale_lock_takeover_hands_back_a_fresh_lock(tmp_path, monkeypatch):
    lock = _stale_lock(tmp_path)
    real_replace = os.replace

    def winner_recreated_lock(src, dst):
        # Between our stale check and our rename another waiter took over and
        # created a fresh lock; our rename now moves *its* lock aside.
        os.unlink(src)
        Path(src).write_text("1234 fresh\n", encoding="ascii")
        real_replace(src, dst)

    monkeypatch.setattr(installer.os, "replace", winner_recreated_lock)
    assert installer._try_acquire_lock(lock) is False
    assert lock.read_text(encoding="ascii") == "1234 fresh\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == [installer.LOCK_NAME]


def test_installer_waits_for_fresh_lock_holder(inst, monkeypatch):
    dest = inst["dest"]
    dest.mkdir(parents=True)
    lock = dest / installer.LOCK_NAME
    lock.write_text("999 0\n", encoding="ascii")
    sleeps = []

    def other_installer_finishes(seconds):
        sleeps.append(seconds)
        root = dest / "chrome-linux64"
        root.mkdir(exist_ok=True)
        (root / "chrome").write_bytes(b"ELF")
        (root / installer.MARKER_NAME).write_text("146\n", encoding="utf-8")

    monkeypatch.setattr(installer.time, "sleep", other_installer_finishes)
    assert installer.ensure_chrome() == str(dest / "chrome-linux64" / "chrome")
    assert sleeps == [installer.LOCK_POLL_SECONDS]
    assert inst["urls"] == []
    assert lock.exists()  # not ours to remove
