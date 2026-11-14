"""``Browser.connect`` contract: attach transport, detached spawn, cleanup, pid lookup.

Nothing real runs: the hub state, the attach transport, browser resolution,
the spawner, the clock, the process table and ``_stop_process_tree`` are fakes.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from emunium import chrome_installer
from emunium._bridge.transport import PINNED, Reply
from emunium._browser import connect as connect_mod
from emunium._browser import facade
from emunium._browser import launcher as launcher_mod
from emunium._browser.connect import (
    CREATE_NEW_PROCESS_GROUP,
    DETACHED_PROCESS,
    Connection,
    ProcessEntry,
    main_process_pid,
    spawn_options,
)
from emunium._browser.facade import Browser
from emunium._browser.launcher import ResolvedBrowser
from emunium.errors import (
    BridgeError,
    InvalidArgumentError,
    LaunchError,
    TargetClosedError,
)

STATE_DIR = "C:/Users/me/AppData/Roaming/emunium"
STATE = {"port": 47800, "state_dir": STATE_DIR, "hub_path": STATE_DIR + "/hub.json"}
CHROME = ResolvedBrowser("chrome", "C:/Chrome/chrome.exe", "chromium", "cdp-pipe")
EDGE = ResolvedBrowser("edge", "C:/Edge/msedge.exe", "chromium", "load-extension")
FIREFOX = ResolvedBrowser(
    "firefox", "C:/Firefox/firefox.exe", "firefox", "enterprise-policies"
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeAttachTransport:
    """Connects at the wait ``connect_on_wait`` (0-based); None never connects.

    A wait that does not connect advances the fake clock by its timeout.
    """

    def __init__(self, clock: FakeClock, connect_on_wait: int | None = 0) -> None:
        self.clock = clock
        self.connect_on_wait = connect_on_wait
        self.start_error: BaseException | None = None
        self.waits: list[float] = []
        self.started = False
        self.shutdowns = 0
        self.connected = False
        self.actual_port: int | None = None
        self.pinned_tab_id: int | None = None
        self.requests: list[str] = []
        self.handlers: dict[str, Any] = {"getTabInfo": {"tabId": 7}}
        self.made_with: dict[str, Any] = {}
        # What the connected extension sent; read like Transport._active.hello.
        self.hello_user_agent: str | None = None
        self.bound_install_id: str | None = None

    @property
    def _active(self) -> Any:
        if self.hello_user_agent is None:
            return None
        return SimpleNamespace(hello={"userAgent": self.hello_user_agent})

    def start(self, timeout: float = 30.0) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.started = True
        self.actual_port = 47800

    def wait_for_connection(self, timeout: float = 60.0) -> bool:
        index = len(self.waits)
        self.waits.append(timeout)
        if self.connect_on_wait is not None and index >= self.connect_on_wait:
            self.connected = True
        else:
            self.clock.now += timeout
        return self.connected

    def shutdown(self) -> None:
        self.shutdowns += 1
        self.connected = False

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        tab_id: Any = PINNED,
        timeout: float = 30.0,
        idempotent: bool = False,
    ) -> Reply:
        tab = self.pinned_tab_id if tab_id is PINNED else tab_id
        self.requests.append(method)
        handler = self.handlers[method]
        if isinstance(handler, BaseException):
            raise handler
        return Reply(copy.deepcopy(handler), tab, 1.0)


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid


class Rig:
    """Records every hook call ``connect()`` makes.

    By default no extension is connected until the spawned browser is up;
    ``transport.connect_on_wait = 0`` models one that is already connected.
    """

    def __init__(self, resolved: ResolvedBrowser | None = CHROME) -> None:
        self.clock = FakeClock()
        self.transport = FakeAttachTransport(self.clock, connect_on_wait=None)
        self.connect_after_spawn = True
        self.resolved = resolved
        self.resolve_calls: list[tuple[str | None, bool]] = []
        self.spawned: list[list[str]] = []
        self.policy_calls: list[str] = []
        self.events: list[str] = []
        self.stopped: list[Any] = []
        self.loaded_ports: list[int | None] = []
        self.table: list[ProcessEntry] = []
        self.next_pid = 500

    def load_state(self, port: int | None) -> dict[str, Any]:
        self.loaded_ports.append(port)
        return dict(STATE)

    def make_transport(
        self, state: dict[str, Any], *, port: int | None, install_id: str | None
    ) -> FakeAttachTransport:
        self.transport.made_with = {
            "state": state,
            "port": port,
            "install_id": install_id,
        }
        return self.transport

    def resolve(self, browser: str | None, download: bool) -> ResolvedBrowser:
        self.resolve_calls.append((browser, download))
        if self.resolved is None:
            raise LaunchError("no browser installed")
        return self.resolved

    def spawn(self, argv: list[str]) -> Any:
        self.events.append("spawn")
        self.spawned.append(argv)
        if self.connect_after_spawn:
            # The extension connects once the browser is up.
            self.transport.connect_on_wait = len(self.transport.waits)
        return FakeProcess(self.next_pid)

    def register_firefox_policy(self, executable: str) -> None:
        self.events.append("policy")
        self.policy_calls.append(executable)

    def process_table(self) -> list[ProcessEntry]:
        return self.table

    def connect(self, **kwargs: Any) -> Connection:
        hooks: dict[str, Any] = {
            "load_state": self.load_state,
            "make_transport": self.make_transport,
            "resolve": self.resolve,
            "spawn": self.spawn,
            "clock": self.clock,
            "process_table": self.process_table,
            "platform": "linux",
        }
        hooks.update(kwargs)
        return connect_mod.connect(**hooks)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Rig:
    rig = Rig()
    monkeypatch.setattr(
        connect_mod, "write_firefox_policy", rig.register_firefox_policy
    )
    monkeypatch.setattr(
        connect_mod,
        "_stop_process_tree",
        lambda process, job, headless=False, label="Browser": rig.stopped.append(
            (process, job, headless, label)
        ),
    )
    return rig


# -- connect(): already connected -----------------------------------------------


def test_already_connected_never_spawns(rig: Rig) -> None:
    rig.transport.connect_on_wait = 0
    connection = rig.connect(browser="edge", timeout=60.0)
    assert rig.spawned == []
    assert rig.transport.started and rig.transport.waits == [5.0]
    assert rig.transport.made_with == {
        "state": STATE,
        "port": None,
        "install_id": None,
    }
    # Resolved for the name only, never downloading Chrome for Testing.
    assert rig.resolve_calls == [("edge", False)]
    assert connection.resolved is CHROME and not connection.started_browser
    assert (connection.port, connection.state_dir) == (47800, STATE_DIR)


def test_port_and_install_id_reach_the_state_and_transport(rig: Rig) -> None:
    install = "0f8fad5b-d9cb-469f-a165-70867728950e"
    connection = rig.connect(port=47811, install_id=install)
    assert rig.loaded_ports == [47811]
    assert rig.transport.made_with["port"] == 47811
    assert rig.transport.made_with["install_id"] == install
    assert connection.port == 47811


def test_short_timeout_bounds_the_detection_wait(rig: Rig) -> None:
    rig.connect(timeout=2.0)
    # 2 s of detection used the whole timeout; the spawned browser gets 0 s more.
    assert rig.transport.waits == [2.0, 0.0]


def test_failed_name_resolution_leaves_resolved_none() -> None:
    rig = Rig(resolved=None)
    rig.transport.connect_on_wait = 0
    connection = rig.connect(browser="brave")
    assert connection.resolved is None and rig.resolve_calls == [("brave", False)]
    assert connection.browser_name is None


@pytest.fixture
def cft_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An empty Chrome for Testing install dir; any download attempt fails."""

    def no_download(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Chrome for Testing download attempted")

    monkeypatch.delenv(chrome_installer.CHROME_PATH_ENV, raising=False)
    monkeypatch.setattr(chrome_installer, "_install_dir", lambda: tmp_path)
    monkeypatch.setattr(chrome_installer, "_get_platform_tag", lambda: "win64")
    monkeypatch.setattr(chrome_installer, "_install", no_download)
    monkeypatch.setattr(launcher_mod, "ensure_chrome", no_download)
    return tmp_path


def test_downloaded_cft_is_named_without_a_download(cft_dir: Path) -> None:
    exe = cft_dir / "chrome-win64" / "chrome.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    resolved = connect_mod._resolve_name_only("cft", connect_mod._resolve)
    assert resolved is not None
    assert (resolved.name, resolved.path) == ("cft", str(exe))
    assert not (exe.parent / chrome_installer.MARKER_NAME).exists()


def test_cft_override_is_named_without_a_download(
    cft_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = cft_dir / "custom-chrome.exe"
    override.write_bytes(b"")
    monkeypatch.setenv(chrome_installer.CHROME_PATH_ENV, str(override))
    resolved = connect_mod._resolve_name_only("cft", connect_mod._resolve)
    assert resolved is not None
    assert (resolved.name, resolved.path) == ("cft", str(override))


def test_absent_cft_is_not_named(cft_dir: Path) -> None:
    assert connect_mod._resolve_name_only("cft", connect_mod._resolve) is None
    assert list(cft_dir.iterdir()) == []


# -- connect(): spawn path --------------------------------------------------------


@pytest.mark.parametrize(
    ("options", "tail"),
    [
        ({}, []),
        ({"profile_directory": "Profile 1"}, ["--profile-directory=Profile 1"]),
        (
            {"extra_args": ["--new-window", "about:blank"]},
            ["--new-window", "about:blank"],
        ),
    ],
)
def test_chromium_spawn_argv_without_user_data_dir(
    rig: Rig, options: dict[str, Any], tail: list[str]
) -> None:
    connection = rig.connect(browser="chrome", **options)
    assert rig.spawned == [[CHROME.path, *tail]]
    assert rig.resolve_calls == [("chrome", True)]
    assert connection.started_browser and connection.process is not None
    assert connection.process.pid == 500


def test_chromium_spawn_argv_with_both_profile_options(
    rig: Rig, tmp_path: Path
) -> None:
    profile = tmp_path / "profiles"
    rig.connect(
        user_data_dir=str(profile),
        profile_directory="Default",
        extra_args=["--x"],
    )
    assert rig.spawned == [
        [
            CHROME.path,
            f"--user-data-dir={profile.resolve()}",
            "--profile-directory=Default",
            "--x",
        ]
    ]
    assert profile.is_dir()  # created for the browser, never removed


def test_chromium_spawn_argv_with_user_data_dir_only(rig: Rig, tmp_path: Path) -> None:
    rig.connect(user_data_dir=str(tmp_path))
    assert rig.spawned == [[CHROME.path, f"--user-data-dir={tmp_path.resolve()}"]]


def test_spawn_argv_carries_no_automation_flag(rig: Rig) -> None:
    rig.resolved = EDGE
    rig.connect()
    argv = rig.spawned[0]
    assert argv == [EDGE.path]
    assert not any("load-extension" in arg or "remote-debugging" in arg for arg in argv)


def test_firefox_spawn_argv_uses_profile(rig: Rig, tmp_path: Path) -> None:
    rig.resolved = FIREFOX
    rig.connect(browser="firefox", user_data_dir=str(tmp_path), extra_args=["-x"])
    assert rig.spawned == [[FIREFOX.path, "-profile", str(tmp_path.resolve()), "-x"]]


def test_firefox_policy_is_registered_before_spawn(rig: Rig) -> None:
    rig.resolved = FIREFOX

    rig.connect(browser="firefox")

    assert rig.policy_calls == [FIREFOX.path]
    assert rig.events == ["policy", "spawn"]


def test_firefox_policy_is_not_touched_when_extension_is_already_connected(
    rig: Rig,
) -> None:
    rig.resolved = FIREFOX
    rig.transport.connect_on_wait = 0

    connection = rig.connect(browser="firefox")

    assert not connection.started_browser
    assert rig.policy_calls == []
    assert rig.spawned == []


def test_firefox_without_profile_uses_its_default(rig: Rig) -> None:
    rig.resolved = FIREFOX
    rig.connect(browser="firefox")
    assert rig.spawned == [[FIREFOX.path]]


def test_firefox_with_profile_directory_raises_and_cleans_up(rig: Rig) -> None:
    rig.resolved = FIREFOX
    rig.transport.connect_on_wait = None
    with pytest.raises(LaunchError, match="profile_directory"):
        rig.connect(browser="firefox", profile_directory="Profile 1")
    assert rig.spawned == []
    assert rig.transport.shutdowns == 1


def test_timeout_is_overall_after_a_spawn(rig: Rig) -> None:
    rig.connect(timeout=20.0)
    # 5 s of detection, then the 15 s left of the overall timeout.
    assert rig.transport.waits == [5.0, 15.0]


def test_invalid_arguments_fail_before_any_io(rig: Rig) -> None:
    for kwargs in (
        {"timeout": 0},
        {"timeout": True},
        {"user_data_dir": ""},
        {"profile_directory": 3},
    ):
        with pytest.raises(InvalidArgumentError):
            rig.connect(**kwargs)
    with pytest.raises(LaunchError, match="list of strings"):
        rig.connect(extra_args="--x")
    assert rig.loaded_ports == [] and not rig.transport.started


@pytest.mark.parametrize("install_id", ["not-a-uuid", "", 42])
def test_non_uuid_install_id_is_an_invalid_argument(rig: Rig, install_id: Any) -> None:
    with pytest.raises(InvalidArgumentError, match="install_id must be a UUID"):
        rig.connect(install_id=install_id)
    assert rig.loaded_ports == [] and rig.transport.made_with == {}


# -- detached spawn ---------------------------------------------------------------


def test_spawn_options_detach_on_windows() -> None:
    options = spawn_options("win32")
    assert options["creationflags"] == DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    assert options["creationflags"] == 0x00000208
    assert "start_new_session" not in options
    assert {options[key] for key in ("stdin", "stdout", "stderr")} == {
        connect_mod.subprocess.DEVNULL
    }


def test_spawn_options_start_a_new_session_on_posix() -> None:
    options = spawn_options("linux")
    assert options["start_new_session"] is True
    assert "creationflags" not in options
    assert options["stdout"] == connect_mod.subprocess.DEVNULL


def test_spawn_detached_passes_the_options_to_popen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[list[str], dict[str, Any]]] = []

    def fake_popen(argv: list[str], **kwargs: Any) -> FakeProcess:
        seen.append((argv, kwargs))
        return FakeProcess(42)

    monkeypatch.setattr(connect_mod.subprocess, "Popen", fake_popen)
    process = connect_mod.spawn_detached(["C:/Chrome/chrome.exe"])
    assert process.pid == 42
    assert seen == [(["C:/Chrome/chrome.exe"], spawn_options())]


def test_spawn_detached_reports_a_missing_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_popen(argv: list[str], **kwargs: Any) -> FakeProcess:
        raise FileNotFoundError(2, "not found")

    monkeypatch.setattr(connect_mod.subprocess, "Popen", failing_popen)
    with pytest.raises(LaunchError, match="Cannot start C:/x.exe"):
        connect_mod.spawn_detached(["C:/x.exe"])


# -- timeout and port errors ------------------------------------------------------


@pytest.mark.parametrize("close_browser", [False, True])
def test_timeout_error_text_and_cleanup(rig: Rig, close_browser: bool) -> None:
    rig.connect_after_spawn = False
    rig.next_pid = 501
    with pytest.raises(LaunchError) as info:
        rig.connect(timeout=12.0, close_browser=close_browser)
    message = str(info.value)
    assert "did not connect within 12.0 s" in message
    assert "python -m emunium extension build --out DIR" in message
    assert "DIR/emunium-extension" in message and "developer mode on" in message
    assert "hub port 47800" in message and STATE_DIR in message
    assert "up to 30 s" in message
    assert rig.transport.shutdowns == 1
    assert len(rig.spawned) == 1
    if close_browser:
        assert [entry[0].pid for entry in rig.stopped] == [501]
        assert rig.stopped[0][1:] == (None, False, CHROME.label)
    else:
        assert rig.stopped == []


def test_start_false_times_out_without_spawning(rig: Rig) -> None:
    rig.transport.connect_on_wait = None
    with pytest.raises(LaunchError, match="did not connect within 8.0 s"):
        rig.connect(start=False, timeout=8.0, close_browser=True)
    assert rig.spawned == [] and rig.stopped == []
    assert rig.resolve_calls == []
    assert rig.transport.waits == [5.0, 3.0]
    assert rig.transport.shutdowns == 1


def test_start_false_still_waits_for_a_late_extension(rig: Rig) -> None:
    rig.transport.connect_on_wait = 1
    connection = rig.connect(start=False)
    assert rig.spawned == [] and not connection.started_browser


def test_port_in_use_is_a_launch_error(rig: Rig) -> None:
    error = BridgeError("cannot listen on 127.0.0.1:47800: in use")
    error.__cause__ = OSError(10048, "address in use")
    rig.transport.start_error = error
    with pytest.raises(LaunchError) as info:
        rig.connect()
    message = str(info.value)
    assert "47800" in message
    assert "another emunium connect session or another program holds it" in message
    assert rig.spawned == [] and rig.transport.waits == []


def test_other_start_failure_names_the_port(rig: Rig) -> None:
    rig.transport.start_error = BridgeError("bridge server did not start within 30 s")
    with pytest.raises(LaunchError, match="hub port 47800 did not start"):
        rig.connect()


# -- Connection.close -------------------------------------------------------------


def test_close_detaches_without_stopping_and_is_idempotent(rig: Rig) -> None:
    connection = rig.connect()  # spawned, close_browser False
    connection.close()
    connection.close()
    assert connection.closed and rig.transport.shutdowns == 1
    assert rig.stopped == []


def test_close_browser_stops_only_a_browser_connect_started(rig: Rig) -> None:
    started = rig.connect(close_browser=True)
    started.close()
    started.close()
    assert [entry[0].pid for entry in rig.stopped] == [500]

    other = Rig()
    other.stopped = rig.stopped
    other.transport.connect_on_wait = 0  # the browser was already running
    already_running = other.connect(close_browser=True)
    already_running.close()
    assert len(rig.stopped) == 1 and other.transport.shutdowns == 1


# -- Windows pid lookup -------------------------------------------------------------


CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def test_main_process_is_the_image_whose_parent_is_another_image() -> None:
    table = [
        ProcessEntry(4, 0, None),
        ProcessEntry(900, 4, r"C:\Windows\explorer.exe"),
        ProcessEntry(1200, 900, CHROME_EXE),
        ProcessEntry(1300, 1200, CHROME_EXE),  # renderer
        ProcessEntry(1310, 1200, CHROME_EXE),  # gpu
        ProcessEntry(1400, 900, r"C:\Edge\msedge.exe"),
    ]
    assert main_process_pid(CHROME_EXE, table) == 1200


def test_main_process_lookup_ignores_case_and_separators() -> None:
    table = [
        ProcessEntry(1200, 900, CHROME_EXE.upper()),
        ProcessEntry(1300, 1200, CHROME_EXE),
    ]
    assert main_process_pid(CHROME_EXE.replace("\\", "/"), table) == 1200


def test_main_process_lookup_without_a_match_or_with_ambiguity() -> None:
    assert main_process_pid(CHROME_EXE, [ProcessEntry(5, 1, "C:/x.exe")]) is None
    two_profiles = [
        ProcessEntry(1200, 900, CHROME_EXE),
        ProcessEntry(2200, 900, CHROME_EXE),
        ProcessEntry(2300, 2200, CHROME_EXE),
    ]
    assert main_process_pid(CHROME_EXE, two_profiles) is None
    assert main_process_pid(CHROME_EXE, two_profiles, prefer=2200) == 2200
    # A preferred pid that is only a child does not count.
    assert main_process_pid(CHROME_EXE, two_profiles, prefer=2300) is None


def test_main_process_whose_parent_died_and_pid_was_reused() -> None:
    table = [
        ProcessEntry(1200, 1200, CHROME_EXE),
        ProcessEntry(1300, 1200, CHROME_EXE),
    ]
    assert main_process_pid(CHROME_EXE, table) == 1200


def test_connect_sets_the_pid_on_windows_only(rig: Rig) -> None:
    rig.resolved = ResolvedBrowser("chrome", CHROME_EXE, "chromium", "cdp-pipe")
    rig.table = [
        ProcessEntry(1200, 900, CHROME_EXE),
        ProcessEntry(1300, 1200, CHROME_EXE),
    ]
    assert rig.connect(platform="win32").pid == 1200  # spawned
    assert rig.connect(browser="chrome", platform="linux").pid is None


def test_windows_pid_lookup_failure_leaves_pid_none(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> list[ProcessEntry]:
        raise OSError(5, "access denied")

    with caplog.at_level("INFO", logger="emunium.browser"):
        assert rig.connect(platform="win32", process_table=broken).pid is None
        # Already connected, browser named: the lookup runs on an empty table.
        assert rig.connect(browser="chrome", platform="win32").pid is None
    assert "Browser main process" in caplog.text


def test_explicit_browser_drives_the_pid_without_a_spawn(rig: Rig) -> None:
    rig.transport.connect_on_wait = 0
    rig.resolved = ResolvedBrowser("chrome", CHROME_EXE, "chromium", "cdp-pipe")
    rig.table = [ProcessEntry(1200, 900, CHROME_EXE)]
    connection = rig.connect(browser="chrome", platform="win32")
    assert rig.spawned == [] and rig.resolve_calls == [("chrome", False)]
    assert (connection.browser_name, connection.pid) == ("chrome", 1200)


# -- identity without browser= and without a spawn ------------------------------------

EDGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like"
    " Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
)
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like"
    " Gecko) Chrome/130.0.0.0 Safari/537.36"
)
FIREFOX_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0"
)


@pytest.mark.parametrize(
    ("agent", "name"),
    [
        (EDGE_UA, "edge"),
        (FIREFOX_UA, "firefox"),
        (CHROME_UA, None),  # also Brave and Chromium: same user agent
        ("", None),
        (None, None),
    ],
)
def test_browser_from_user_agent(agent: str | None, name: str | None) -> None:
    assert connect_mod.browser_from_user_agent(agent) == name


@pytest.mark.parametrize(("agent", "name"), [(EDGE_UA, "edge"), (CHROME_UA, None)])
def test_unnamed_connected_browser_is_named_by_its_user_agent_only(
    rig: Rig, caplog: pytest.LogCaptureFixture, agent: str, name: str | None
) -> None:
    rig.transport.connect_on_wait = 0
    rig.transport.hello_user_agent = agent
    # Chrome is "installed" and its process runs: auto would have picked it.
    rig.table = [ProcessEntry(1200, 900, CHROME.path)]
    looked_up: list[bool] = []
    with caplog.at_level("INFO", logger="emunium.browser"):
        connection = rig.connect(
            platform="win32",
            process_table=lambda: looked_up.append(True) or rig.table,
        )
    assert rig.resolve_calls == [] and rig.spawned == []
    assert connection.resolved is None and connection.browser_name == name
    assert connection.pid is None and looked_up == []
    assert "pass browser=" in caplog.text


def test_user_agent_falls_back_to_the_install_record(tmp_path: Path) -> None:
    install = "0f8fad5b-d9cb-469f-a165-70867728950e"
    hub = tmp_path / "hub.json"
    hub.write_text(
        json.dumps(
            {
                "port": 47800,
                "enrollment": "e" * 43,
                "installs": {install: {"token": "t", "userAgent": FIREFOX_UA}},
            }
        ),
        encoding="utf-8",
    )
    transport = FakeAttachTransport(FakeClock())
    assert connect_mod.connected_user_agent(transport, str(hub)) is None
    transport.bound_install_id = install
    assert connect_mod.connected_user_agent(transport, str(hub)) == FIREFOX_UA
    transport.hello_user_agent = EDGE_UA  # the live hello wins
    assert connect_mod.connected_user_agent(transport, str(hub)) == EDGE_UA
    transport.hello_user_agent = None
    assert connect_mod.connected_user_agent(transport, str(tmp_path / "no")) is None


def test_spawned_browser_without_browser_arg_keeps_its_resolution(rig: Rig) -> None:
    rig.resolved = ResolvedBrowser("chrome", CHROME_EXE, "chromium", "cdp-pipe")
    rig.table = [ProcessEntry(1200, 900, CHROME_EXE)]
    rig.transport.hello_user_agent = EDGE_UA  # ignored: the spawn names it
    connection = rig.connect(platform="win32")
    assert rig.resolve_calls == [(None, True)]
    assert (connection.browser_name, connection.pid) == ("chrome", 1200)


# -- Browser.connect facade ---------------------------------------------------------


@pytest.fixture
def hooked(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Rig:
    """Route Browser.connect's default hooks to the rig; no foreground calls."""
    monkeypatch.setattr(connect_mod, "_load_state", rig.load_state)
    monkeypatch.setattr(connect_mod, "_make_transport", rig.make_transport)
    monkeypatch.setattr(connect_mod, "_resolve", rig.resolve)
    monkeypatch.setattr(connect_mod, "spawn_detached", rig.spawn)
    monkeypatch.setattr(connect_mod, "windows_process_table", rig.process_table)
    monkeypatch.setattr(
        facade.BrowserContext, "ensure_foreground", lambda self, geometry: None
    )
    return rig


def test_browser_connect_pins_the_active_tab_and_names_the_browser(
    hooked: Rig,
) -> None:
    browser = Browser.connect("chrome", timeout=30.0, verify_input=False)
    assert hooked.transport.requests == ["getTabInfo"]
    assert browser.bridge.pinned_tab_id == 7
    assert browser.bridge.transport is hooked.transport
    assert browser.context.bridge is browser.bridge
    assert browser.browser_name == "chrome"
    ctx = browser.context
    assert ctx.headless is False and ctx.verify_input is False
    assert repr(browser) == "Browser(connected=True, port=47800)"
    browser.close()
    assert repr(browser) == "Browser(connected=False, port=47800)"
    assert hooked.transport.requests == ["getTabInfo"]  # repr and close send nothing


def test_browser_connect_sets_the_main_pid(
    hooked: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connect_mod, "_current_platform", lambda: "win32")
    hooked.resolved = ResolvedBrowser("chrome", CHROME_EXE, "chromium", "cdp-pipe")
    hooked.table = [ProcessEntry(1200, 900, CHROME_EXE)]
    browser = Browser.connect()
    assert browser.context.pid == 1200
    browser.close()
    assert browser.context.pid is None


def test_browser_close_detaches_and_is_idempotent(hooked: Rig) -> None:
    browser = Browser.connect()  # spawned by connect, close_browser False
    browser.close()
    browser.close()
    assert hooked.transport.shutdowns == 1 and hooked.stopped == []


def test_browser_close_browser_stops_the_started_browser(hooked: Rig) -> None:
    with Browser.connect(close_browser=True) as browser:
        assert browser.browser_name == "chrome"
    assert [entry[0].pid for entry in hooked.stopped] == [500]
    assert hooked.transport.shutdowns == 1


def test_browser_name_is_none_when_resolution_fails(hooked: Rig) -> None:
    hooked.resolved = None
    hooked.transport.connect_on_wait = 0  # connected on detection: name only
    browser = Browser.connect("brave")
    assert browser.browser_name is None
    browser.close()


def test_unnamed_connected_edge_is_named_without_a_pid(
    hooked: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connect_mod, "_current_platform", lambda: "win32")
    hooked.transport.connect_on_wait = 0
    hooked.transport.hello_user_agent = EDGE_UA
    hooked.table = [ProcessEntry(1200, 900, CHROME.path)]  # auto would say chrome
    browser = Browser.connect()
    assert browser.browser_name == "edge"
    assert browser.context.pid is None and hooked.resolve_calls == []
    browser.close()


def test_install_id_is_validated_before_the_browser_connects(hooked: Rig) -> None:
    with pytest.raises(InvalidArgumentError):
        Browser.connect(install_id="1234")
    assert hooked.loaded_ports == [] and not hooked.transport.started


def test_failed_pin_cleans_up_like_a_failed_launch(hooked: Rig) -> None:
    hooked.transport.handlers["getTabInfo"] = TargetClosedError("no active tab")
    with pytest.raises(TargetClosedError):
        Browser.connect(close_browser=True)
    assert hooked.transport.shutdowns == 1
    assert [entry[0].pid for entry in hooked.stopped] == [500]


def test_connected_browser_refuses_launch(hooked: Rig) -> None:
    browser = Browser.connect()
    with pytest.raises(LaunchError, match="cannot launch"):
        browser.launch()
    browser.close()


def test_bad_motion_preset_fails_before_connecting(hooked: Rig) -> None:
    with pytest.raises(InvalidArgumentError):
        Browser.connect(motion="no-such-preset")
    assert hooked.loaded_ports == []
