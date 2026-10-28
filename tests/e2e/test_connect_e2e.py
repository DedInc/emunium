# tests/e2e/test_connect_e2e.py
"""``Browser.connect`` against real browsers carrying the attach build.

The attach build is installed the way a persistent install looks to emunium:
a browser runs headless on its own temporary profile with the build loaded
(``--load-extension`` for Edge, Brave, Chromium and Chrome for Testing,
``Extensions.loadUnpacked`` over the CDP pipe for Google Chrome, and a local
Firefox XPI registered through Enterprise Policies). ``connect`` must then
reach that extension through ``AttachTransport``, detach on ``close()`` without
stopping the browser, and connect again with the stored install token.

Parametrized by ``browser_name`` like the AB7 matrix; a missing browser or a
missing ``cryptography`` package is a skip. No OS input is sent.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import signal
import socket
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from test_browsers_e2e import (
    _leftovers_after_close,
    _linux_processes,
    _processes_using,
    _skip_reason,
    _windows_processes,
)

from emunium import Browser
from emunium._attach.build import (
    HUB_FILE,
    STATE_DIR_ENV,
    build_extension,
    load_install,
    load_or_create_state,
    state_dir,
)
from emunium._browser.cdp_pipe import CdpPipe
from emunium._browser.extension_targets import FIREFOX_GECKO_ID
from emunium._browser.firefox import (
    firefox_policy_path,
    write_firefox_policy,
    write_user_prefs,
)
from emunium._browser.launcher import (
    CDP_PIPE,
    FIREFOX_WAIT_FLAG,
    PROTOCOL_VERSION,
    ResolvedBrowser,
    _attach_job,
    _stop_process_tree,
    resolve_browser,
)
from emunium.errors import LaunchError

pytestmark = pytest.mark.e2e

IS_WINDOWS = os.name == "nt"
# POSIX: own process group, so the tree can be signalled as one.
NEW_SESSION = not IS_WINDOWS
# A cold profile may take a while to start the extension's service worker, and
# a reconnect waits out the extension's back-off (1 s doubling, 30 s cap).
CONNECT_TIMEOUT = 60.0
TASKKILL_TIMEOUT_SECONDS = 30.0
SITE_PAGE = "/index.html"
FIRST_RUN_FLAGS = ("--no-first-run", "--no-default-browser-check")
HEADLESS_FLAG = "--headless=new"
# Browsers the start=True test may use, in order of preference.
START_CANDIDATES = ("edge", "cft")
FIREFOX_XPI_ENV = "EMUNIUM_FIREFOX_XPI"
DEFAULT_FIREFOX_XPI = (
    Path(__file__).resolve().parents[2]
    / "attach_build"
    / "emunium-extension-firefox.xpi"
)


@pytest.fixture
def hub_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """A free hub port, with the hub state in a temporary ``EMUNIUM_HOME``."""
    pytest.importorskip(
        "cryptography",
        reason="attach builds need the optional cryptography package (emunium[hub])",
    )
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "emunium-home"))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _profile_dir(tmp_path: Path) -> str:
    """The profile root as connect() spells it on the command line.

    Resolved after creation: Windows temp paths may carry 8.3 short names that
    ``Path.resolve`` (and so the browser command line) expands, and the leak
    check matches the command line text.
    """
    profile = tmp_path / "profile"
    profile.mkdir()
    return str(profile.resolve())


def _build(resolved: ResolvedBrowser, tmp_path: Path, port: int) -> str:
    target = "firefox" if resolved.family == "firefox" else "chromium"
    build = build_extension(tmp_path / "build", port=port, target=target)
    assert build["port"] == port
    return build["unpacked_dir"]


def _signed_firefox_xpi() -> tuple[Path, int, str]:
    """Read the signed local Firefox artifact and its loopback hub settings."""
    candidate = Path(
        os.environ.get(FIREFOX_XPI_ENV, str(DEFAULT_FIREFOX_XPI))
    ).expanduser()
    if not candidate.is_file():
        pytest.skip(
            f"signed Firefox XPI not found at {candidate}; set {FIREFOX_XPI_ENV}"
        )
    try:
        with zipfile.ZipFile(candidate) as archive:
            names = {name.casefold() for name in archive.namelist()}
            manifest = json.loads(archive.read("manifest.json"))
            attach = json.loads(archive.read("attach.json"))
    except (OSError, zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        pytest.fail(f"could not read signed Firefox XPI metadata: {type(exc).__name__}")

    signature_files = {
        "meta-inf/manifest.mf",
        "meta-inf/mozilla.sf",
        "meta-inf/mozilla.rsa",
    }
    if not signature_files.issubset(names):
        pytest.fail("Firefox XPI is missing Mozilla signature metadata")
    gecko_id = (
        manifest.get("browser_specific_settings", {})
        .get("gecko", {})
        .get("id")
    )
    if gecko_id != FIREFOX_GECKO_ID:
        pytest.fail("Firefox XPI has an unexpected Gecko extension ID")
    if attach.get("protocol") != PROTOCOL_VERSION or attach.get("mode") != "attach":
        pytest.fail("Firefox XPI has incompatible attach metadata")
    endpoint = urlsplit(str(attach.get("hubUrl", "")))
    if (
        endpoint.scheme != "ws"
        or endpoint.hostname not in {"127.0.0.1", "localhost"}
        or endpoint.path
        or endpoint.query
        or endpoint.fragment
        or endpoint.port is None
    ):
        pytest.fail("Firefox XPI must target a loopback WebSocket hub")
    enrollment = attach.get("enrollment")
    if not isinstance(enrollment, str) or len(enrollment) < 43:
        pytest.fail("Firefox XPI has invalid enrollment metadata")
    return candidate.resolve(), endpoint.port, enrollment


def _seed_signed_firefox_hub(port: int, enrollment: str) -> None:
    """Use the signed XPI's fixed port and enrollment in this test's temp hub."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            pytest.skip(f"signed Firefox XPI hub port {port} is already in use")

    state = load_or_create_state(port=port)
    hub_path = Path(state["hub_path"])
    hub = json.loads(hub_path.read_text(encoding="utf-8"))
    if not isinstance(hub, dict) or not isinstance(hub.get("installs"), dict):
        pytest.fail("temporary Firefox hub state has an invalid shape")
    hub["port"] = port
    hub["enrollment"] = enrollment
    hub["installs"] = {}
    hub_path.write_text(json.dumps(hub, indent=2) + "\n", encoding="utf-8")


def _existing_firefox_processes() -> list[str]:
    """Firefox policies affect every profile, so don't race an open instance."""
    if IS_WINDOWS:
        processes = _windows_processes()
    elif sys.platform.startswith("linux"):
        processes = _linux_processes()
    elif sys.platform == "darwin":
        result = subprocess.run(
            ["pgrep", "-fl", "firefox"],
            capture_output=True,
            text=True,
            check=False,
        )
        processes = []
        if result.returncode == 0:
            processes = [
                (int(line.split(maxsplit=1)[0]), line.split(maxsplit=1)[-1])
                for line in result.stdout.splitlines()
                if line.split(maxsplit=1)[0].isdigit()
            ]
    else:
        return []

    def is_firefox(command: str) -> bool:
        command = os.path.normcase(command)
        if IS_WINDOWS:
            return "firefox.exe" in command
        executable = command.split(maxsplit=1)[0].rsplit("/", maxsplit=1)[-1]
        return executable.startswith("firefox")

    return [
        command
        for pid, command in processes
        if pid != os.getpid() and is_firefox(command)
    ]


def _own_tree(
    process: subprocess.Popen, label: str, cleanup: contextlib.ExitStack
) -> None:
    """Kill ``process`` and every descendant when ``cleanup`` unwinds."""
    job = _attach_job(process) if IS_WINDOWS else None
    cleanup.callback(_stop_process_tree, process, job, headless=True, label=label)


def _start_load_extension(
    resolved: ResolvedBrowser,
    unpacked: str,
    profile: str,
    log_path: Path,
    cleanup: contextlib.ExitStack,
) -> subprocess.Popen:
    args = [
        resolved.path,
        f"--user-data-dir={profile}",
        *FIRST_RUN_FLAGS,
        f"--disable-extensions-except={unpacked}",
        f"--load-extension={unpacked}",
        HEADLESS_FLAG,
    ]
    with open(log_path, "wb") as log_file:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            start_new_session=NEW_SESSION,
        )
    _own_tree(process, resolved.label, cleanup)
    return process


def _start_cdp_pipe(
    resolved: ResolvedBrowser,
    unpacked: str,
    profile: str,
    log_path: Path,
    cleanup: contextlib.ExitStack,
) -> subprocess.Popen:
    """Google Chrome ignores --load-extension: load the build over the CDP pipe."""
    pipe = CdpPipe()
    # Releases the pipe ends when the spawn fails (close() is idempotent).
    cleanup.callback(pipe.close)
    args = [
        resolved.path,
        f"--user-data-dir={profile}",
        *FIRST_RUN_FLAGS,
        *pipe.prepare(),
        HEADLESS_FLAG,
    ]
    with open(log_path, "wb") as log_file:
        process = subprocess.Popen(
            pipe.wrap_args(args),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            start_new_session=NEW_SESSION,
            **pipe.popen_kwargs(),
        )
    _own_tree(process, resolved.label, cleanup)
    # Unwinds first: the pipe stays open until cleanup, then the tree goes.
    cleanup.callback(pipe.close)
    pipe.after_spawn()
    pipe.load_unpacked(unpacked)
    return process


def _start_firefox_policy(
    resolved: ResolvedBrowser,
    xpi_path: str,
    profile: str,
    log_path: Path,
    cleanup: contextlib.ExitStack,
) -> subprocess.Popen:
    """Register the attach XPI with Enterprise Policies, then start Firefox."""
    xpi = Path(xpi_path)
    policy_path = firefox_policy_path(resolved.path)
    policy_dir_existed = policy_path.parent.exists()
    try:
        previous_policy = policy_path.read_bytes() if policy_path.exists() else None
    except OSError as exc:
        pytest.skip(f"Firefox Enterprise Policy file is not readable at {policy_path}: {exc}")
    cleanup.callback(
        _restore_firefox_policy, policy_path, previous_policy, policy_dir_existed
    )
    try:
        write_firefox_policy(resolved.path, xpi)
    except LaunchError as exc:
        pytest.skip(f"Firefox Enterprise Policies are unavailable at {policy_path}: {exc}")

    write_user_prefs(profile)
    args = [resolved.path, "-profile", profile, "-no-remote", "-headless"]
    if IS_WINDOWS:
        # The launcher stub would exit at once; the tree kill needs it alive.
        args.append(FIREFOX_WAIT_FLAG)
    with open(log_path, "wb") as log_file:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            start_new_session=NEW_SESSION,
        )
    _own_tree(process, resolved.label, cleanup)
    return process


def _restore_firefox_policy(
    policy_path: Path, previous_policy: bytes | None, policy_dir_existed: bool
) -> None:
    """Restore the exact prior file, removing only test-created policy data."""
    if previous_policy is None:
        with contextlib.suppress(FileNotFoundError):
            policy_path.unlink()
        if not policy_dir_existed:
            with contextlib.suppress(OSError):
                policy_path.parent.rmdir()
        return
    policy_path.write_bytes(previous_policy)


def _start_with_build(
    resolved: ResolvedBrowser,
    extension_path: str,
    profile: str,
    log_path: Path,
    cleanup: contextlib.ExitStack,
) -> subprocess.Popen:
    if resolved.family == "firefox":
        start = _start_firefox_policy
    elif resolved.install_method == CDP_PIPE:
        start = _start_cdp_pipe
    else:
        start = _start_load_extension
    return start(resolved, extension_path, profile, log_path, cleanup)


def _check_session(browser: Browser, name: str, site_url: str) -> None:
    assert browser.browser_name == name
    result = browser.goto(site_url + SITE_PAGE)
    assert result["url"] == site_url + SITE_PAGE
    heading = browser.query_selector("#heading")
    assert heading is not None
    assert heading.text == "Hello e2e"
    assert browser.execute_script("return 6 * 7") == 42


def _assert_profile_in_use(profile: str) -> None:
    running = _processes_using(profile)
    if running is not None:
        # The matcher sees this browser, so an empty list later means gone.
        assert running, f"no running process names the profile {profile}"


def _install_record() -> tuple[str, bool | None, str]:
    """``(install_id, confirmed, token)`` of the only install in ``hub.json``.

    Assertions here never put the token (or the record holding it) into a
    failure message.
    """
    hub_path = state_dir() / HUB_FILE
    hub = json.loads(hub_path.read_text(encoding="utf-8"))
    installs = hub.get("installs") if isinstance(hub, dict) else None
    install_ids = sorted(installs) if isinstance(installs, dict) else []
    assert len(install_ids) == 1, f"hub.json lists installs {install_ids}"
    install_id = install_ids[0]
    record = load_install(hub_path, install_id)
    assert record is not None, f"no record for install {install_id}"
    confirmed = record.get("confirmed")
    token = record.get("token")
    has_token = isinstance(token, str) and bool(token)
    assert has_token, f"install {install_id} has no stored token"
    return install_id, confirmed, str(token)


def _kill_leftovers(profile: str) -> None:
    """Last resort after a failed test: no process may outlive the run."""
    for pid, _command in _processes_using(profile) or []:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=TASKKILL_TIMEOUT_SECONDS,
                check=False,
            )
        else:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def test_connect_to_installed_build(
    browser_name: str, site_url: str, hub_port: int, tmp_path: Path
) -> None:
    reason = _skip_reason(browser_name)
    if reason is not None:
        pytest.skip(reason)
    resolved = resolve_browser(browser_name)
    if resolved.family == "firefox":
        if _existing_firefox_processes():
            pytest.skip(
                "Firefox is already running; its installation-wide policy would "
                "share the signed XPI's fixed hub port with this test"
            )
        xpi, signed_port, enrollment = _signed_firefox_xpi()
        _seed_signed_firefox_hub(signed_port, enrollment)
        extension_path = str(xpi)
    else:
        extension_path = _build(resolved, tmp_path, hub_port)
    profile = _profile_dir(tmp_path)

    try:
        with contextlib.ExitStack() as cleanup:
            process = _start_with_build(
                resolved, extension_path, profile, tmp_path / "browser.log", cleanup
            )

            first = Browser.connect(
                browser_name,
                user_data_dir=profile,
                start=False,
                timeout=CONNECT_TIMEOUT,
            )
            cleanup.callback(first.close)
            _check_session(first, browser_name, site_url)
            first.close()

            # The first welcome issued a token, pending until a hello proves it.
            install_id, confirmed, issued_token = _install_record()
            assert confirmed is False, f"install {install_id}: confirmed={confirmed!r}"

            # close() detaches only: the browser keeps running.
            assert process.poll() is None, f"{browser_name} exited on close()"
            _assert_profile_in_use(profile)

            # The install now holds a token: this is the stored-token handshake.
            second = Browser.connect(
                browser_name,
                user_data_dir=profile,
                start=False,
                timeout=CONNECT_TIMEOUT,
            )
            cleanup.callback(second.close)
            _check_session(second, browser_name, site_url)
            second.close()

            # Only a valid tokenProof confirms the stored token; a hello without
            # one would have been issued a fresh pending token instead.
            reconnected_id, confirmed, stored_token = _install_record()
            assert reconnected_id == install_id
            assert confirmed is True, f"install {install_id}: confirmed={confirmed!r}"
            same_token = hmac.compare_digest(stored_token, issued_token)
            assert same_token, f"install {install_id}: the stored token changed"

        leftovers = _leftovers_after_close(profile)
        assert not leftovers, f"still running after cleanup: {leftovers}"
    finally:
        _kill_leftovers(profile)


def _start_target() -> str:
    reasons = []
    for name in START_CANDIDATES:
        reason = _skip_reason(name)
        if reason is None:
            return name
        reasons.append(reason)
    pytest.skip("; ".join(reasons))


def test_connect_starts_and_stops_browser(
    site_url: str, hub_port: int, tmp_path: Path
) -> None:
    name = _start_target()
    resolved = resolve_browser(name)
    unpacked = _build(resolved, tmp_path, hub_port)
    profile = _profile_dir(tmp_path)
    assert _processes_using(profile) in (None, [])
    extra_args = [
        HEADLESS_FLAG,
        f"--load-extension={unpacked}",
        f"--disable-extensions-except={unpacked}",
        *FIRST_RUN_FLAGS,
    ]

    try:
        browser = Browser.connect(
            name,
            user_data_dir=profile,
            start=True,
            close_browser=True,
            timeout=CONNECT_TIMEOUT,
            extra_args=extra_args,
        )
        try:
            # Nothing used the profile before: connect() started this browser.
            _assert_profile_in_use(profile)
            _check_session(browser, name, site_url)
        finally:
            browser.close()

        leftovers = _leftovers_after_close(profile)
        assert not leftovers, f"still running after close(): {leftovers}"
    finally:
        _kill_leftovers(profile)
