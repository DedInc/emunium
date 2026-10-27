"""Launcher and installer lifecycle (slice S6).

No real Chrome, no network: Popen, taskkill, urlopen and the bridge are fakes.
"""

from __future__ import annotations

import io
import json
import logging
import os
import stat
import subprocess
import zipfile
from pathlib import Path
from typing import ClassVar

import pytest

import emunium.chrome_installer as installer
from emunium._browser import launcher
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
        if stderr is not None and FakePopen.stderr_text:
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
    assert any("child processes may remain" in r.getMessage() for r in caplog.records)


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
    assert session.chrome_path is None
    assert session.extra_args == []
    assert session.session_dir is None
    assert session.extension_dir is None
    assert session.profile_dir is None
    assert session.log_path is None
    assert session.headless is True


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
