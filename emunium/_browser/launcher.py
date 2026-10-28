"""Browser process lifecycle for one ``Browser``.

Contract: ``docs/architecture/bridge-protocol.md`` section 1 (session bootstrap).

``launch()`` is transactional: every resource it creates (bridge, session
directory, browser process tree, CDP pipe, BiDi session) is released by
``close()`` before the error propagates. The packaged ``emunium/extension/``
directory is only ever read.

``resolve_browser()`` picks the binary and how the extension gets in:
``load-extension`` (command-line flag), ``cdp-pipe`` (Google Chrome, over
``--remote-debugging-pipe``) or ``bidi`` (Firefox, over WebDriver BiDi).
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from emunium._browser.cdp_pipe import CdpPipe
from emunium._browser.discovery import (
    APP_PATHS_KEY,
    BROWSER_ORDER,
    DEFAULT_PREFERENCE,
    LINUX_COMMANDS,
    MACOS_RELATIVE,
    MACOS_ROOTS,
    WINDOWS_APP_PATHS,
    WINDOWS_RELATIVE,
    WINDOWS_ROOT_ENV,
    InstalledBrowser,
    find_browser,
    find_browsers,
)
from emunium._browser.extension_targets import materialize
from emunium._browser.firefox import (
    BidiSession,
    firefox_args,
    read_bidi_url,
    write_user_prefs,
)
from emunium.chrome_installer import CHROME_PATH_ENV, _install_dir, ensure_chrome
from emunium.errors import EmuniumError, LaunchError

logger = logging.getLogger("emunium.browser")

# Read-only source; each session gets its own copy.
EXTENSION_SOURCE_DIR = str(Path(__file__).resolve().parent.parent / "extension")

PROTOCOL_VERSION = 2
SESSION_DIR_PREFIX = "emunium-"
EXTENSION_COPY_IGNORE = ("port.json", "session.json", "__pycache__")
CONNECT_POLL_SECONDS = 0.25
TERMINATE_GRACE_SECONDS = 5.0
# Headed Windows close: tabs first, then a graceful taskkill, then the kill.
CLOSE_TABS_BUDGET_SECONDS = 1.0
GRACEFUL_TASKKILL_GRACE_SECONDS = 2.0
TASKKILL_TIMEOUT_SECONDS = 10.0
# Upper bound on closing Firefox's stderr pipe (see _close_stream_bounded).
STDERR_CLOSE_BUDGET_SECONDS = 1.0
RMTREE_ATTEMPTS = 10
RMTREE_RETRY_DELAY = 0.3
LOG_TAIL_LINES = 20

_IS_WINDOWS = os.name == "nt"
_SIGKILL = getattr(signal, "SIGKILL", 9)

CHROME_FLAGS = (
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-popup-blocking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--hide-crash-restore-bubble",
)

# Install methods (discovery.INSTALL_METHODS values).
LOAD_EXTENSION = "load-extension"
CDP_PIPE = "cdp-pipe"
BIDI = "bidi"
# Names resolve_browser() accepts besides the discovery names.
CFT_NAME = "cft"
AUTO_NAME = "auto"
CUSTOM_NAME = "custom"
CHROME_FOR_TESTING = "chrome for testing"
# The Windows Firefox launcher stub exits once the real browser is up unless
# told to wait; the stub's pid is the one launch() watches and stops.
FIREFOX_WAIT_FLAG = "-wait-for-browser"
CHROMIUM_LOG_NAME = "chrome.log"
FIREFOX_LOG_NAME = "firefox.log"

_PATH_SEPARATORS = re.compile(r"[\\/]+")


@dataclass(frozen=True)
class ResolvedBrowser:
    """The browser ``launch()`` starts and how the extension gets into it."""

    name: str
    path: str
    family: str
    install_method: str

    @property
    def label(self) -> str:
        return f"{self.name} ({self.path})"


class BrowserSession:
    __slots__ = (
        "_bidi",
        "_bridge_started",
        "_cdp_pipe",
        "_job",
        "_log_files",
        "bridge",
        "browser",
        "chrome_path",
        "executable_path",
        "extension_dir",
        "extra_args",
        "headless",
        "log_path",
        "process",
        "profile_dir",
        "resolved",
        "session_dir",
        "user_data_dir",
    )

    def __init__(self) -> None:
        self.bridge: Any = None
        self.process: subprocess.Popen | None = None
        self.headless: bool = False
        self.user_data_dir: str | None = None
        # Browser name, "cft", "auto" or an executable path; None falls back to
        # chrome_path -> EMUNIUM_CHROME_PATH -> "auto" (see resolve_browser).
        self.browser: str | None = None
        # Explicit Chrome binary, used when ``browser`` is None.
        self.chrome_path: str | None = None
        self.extra_args: list[str] = []
        self.session_dir: str | None = None
        self.extension_dir: str | None = None
        self.profile_dir: str | None = None
        self.log_path: str | None = None
        # Browser and binary actually started by the last launch().
        self.resolved: ResolvedBrowser | None = None
        self.executable_path: str | None = None
        self._bridge_started: bool = False
        # Windows Job Object handle owning the browser process tree, if any.
        self._job: int | None = None
        # Google Chrome: the debugging pipe; closing it makes Chrome exit.
        self._cdp_pipe: CdpPipe | None = None
        # Firefox: the WebDriver BiDi session and the stderr log it copies into.
        self._bidi: BidiSession | None = None
        self._log_files: contextlib.ExitStack | None = None


def launch(session: BrowserSession, bridge_timeout: float = 60.0) -> None:
    if session.bridge is None:
        raise LaunchError("BrowserSession.bridge is not set")
    try:
        _launch(session, float(bridge_timeout))
    except Exception as exc:
        try:
            close(session)
        except Exception:
            logger.exception("Cleanup after a failed launch raised")
        if isinstance(exc, EmuniumError):
            raise
        raise LaunchError(f"{_label(session)} launch failed: {exc}") from exc
    except BaseException:
        # KeyboardInterrupt / SystemExit: still release everything, never wrap.
        try:
            close(session)
        except Exception:
            logger.exception("Cleanup after an interrupted launch raised")
        raise


def _launch(session: BrowserSession, bridge_timeout: float) -> None:
    resolved = _resolve_executable(session)
    extra_args = _validated_extra_args(session.extra_args)

    session._bridge_started = True
    session.bridge.start()
    port = session.bridge.actual_port
    token = session.bridge.token
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port <= 65535:
        raise LaunchError(f"Bridge reported an invalid port: {port!r}")
    if not isinstance(token, str) or not token:
        raise LaunchError("Bridge reported no session token")
    logger.info("Bridge started on port %d", port)

    session.session_dir = tempfile.mkdtemp(prefix=SESSION_DIR_PREFIX)
    session_dir = Path(session.session_dir)
    extension_dir = session_dir / "extension"

    if resolved.install_method == BIDI:
        # The packaged manifest is Chromium's; Firefox gets its overlay.
        materialize(
            "firefox",
            extension_dir,
            source_dir=EXTENSION_SOURCE_DIR,
            ignore=EXTENSION_COPY_IGNORE,
        )
    else:
        shutil.copytree(
            EXTENSION_SOURCE_DIR,
            str(extension_dir),
            ignore=shutil.ignore_patterns(*EXTENSION_COPY_IGNORE),
        )
    session.extension_dir = str(extension_dir)
    _write_session_file(extension_dir, port, token)

    if session.user_data_dir:
        profile_dir = Path(session.user_data_dir).expanduser().resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
    else:
        profile_dir = session_dir / "profile"
        profile_dir.mkdir()
    session.profile_dir = str(profile_dir)

    if resolved.install_method == BIDI:
        write_user_prefs(profile_dir)
        log_path = str(session_dir / FIREFOX_LOG_NAME)
        session.log_path = log_path
        process = _spawn_firefox(
            session,
            resolved,
            _Paths(str(profile_dir), str(extension_dir), log_path),
            extra_args,
            bridge_timeout,
        )
    else:
        _seed_profile(str(profile_dir))
        log_path = str(session_dir / CHROMIUM_LOG_NAME)
        session.log_path = log_path
        process = _spawn_chromium(
            session,
            resolved,
            _Paths(str(profile_dir), str(extension_dir), log_path),
            extra_args,
        )

    _await_connection(session, process, bridge_timeout)
    logger.info("Extension connected to bridge")


@dataclass(frozen=True)
class _Paths:
    profile_dir: str
    extension_dir: str
    log_path: str


def _spawn_chromium(
    session: BrowserSession,
    resolved: ResolvedBrowser,
    paths: _Paths,
    extra_args: list[str],
) -> subprocess.Popen:
    pipe: CdpPipe | None = None
    if resolved.install_method == CDP_PIPE:
        pipe = CdpPipe()
        session._cdp_pipe = pipe
        args = build_args(
            resolved.path,
            paths.profile_dir,
            None,
            headless=session.headless,
            extra_args=extra_args,
            browser_flags=pipe.prepare(),
        )
        process = _spawn(pipe.wrap_args(args), paths.log_path, pipe.popen_kwargs())
    else:
        args = build_args(
            resolved.path,
            paths.profile_dir,
            paths.extension_dir,
            headless=session.headless,
            extra_args=extra_args,
        )
        process = _spawn(args, paths.log_path)
    _register_process(session, resolved, process)

    if pipe is not None:
        try:
            pipe.after_spawn()
            extension_id = pipe.load_unpacked(paths.extension_dir)
        except LaunchError as exc:
            raise _install_failure(session, resolved, process, "CDP pipe", exc) from exc
        logger.info("Extension %s loaded over the CDP pipe", extension_id)
    return process


def _spawn_firefox(
    session: BrowserSession,
    resolved: ResolvedBrowser,
    paths: _Paths,
    extra_args: list[str],
    bridge_timeout: float,
) -> subprocess.Popen:
    args = firefox_args(resolved.path, paths.profile_dir, headless=session.headless)
    if _IS_WINDOWS:
        args.append(FIREFOX_WAIT_FLAG)
    args.extend(extra_args)
    # Kept open until close(): the stderr drain copies every Firefox line into it.
    with contextlib.ExitStack() as stack:
        log_file = stack.enter_context(open(paths.log_path, "wb"))
        # Ownership moves to the session; close() releases it.
        session._log_files = stack.pop_all()
    process = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=not _IS_WINDOWS,
    )
    _register_process(session, resolved, process)

    try:
        stream = process.stderr
        if stream is None:
            raise LaunchError("Firefox stderr is not piped")
        url = read_bidi_url(stream, bridge_timeout, sink=log_file)
        bidi = BidiSession(url)
        session._bidi = bidi
        bidi.new_session()
        extension_id = bidi.install_extension(paths.extension_dir)
    except LaunchError as exc:
        raise _install_failure(
            session, resolved, process, "WebDriver BiDi", exc
        ) from exc
    logger.info("Extension %s installed over WebDriver BiDi", extension_id)
    return process


def _register_process(
    session: BrowserSession, resolved: ResolvedBrowser, process: subprocess.Popen
) -> None:
    session.process = process
    if _IS_WINDOWS:
        session._job = _attach_job(process)
    logger.info(
        "%s started (pid %s), session dir %s",
        resolved.label,
        process.pid,
        session.session_dir,
    )


def _install_failure(
    session: BrowserSession,
    resolved: ResolvedBrowser,
    process: subprocess.Popen,
    channel: str,
    exc: LaunchError,
) -> LaunchError:
    code = process.poll()
    if code is not None:
        return LaunchError(_early_exit_message(session, code))
    return LaunchError(
        f"Could not install the extension into {resolved.label} over {channel}:"
        f" {exc}{_log_tail_suffix(session.log_path)}",
        method=exc.method,
    )


def build_args(
    executable: str,
    profile_dir: str,
    extension_dir: str | None,
    *,
    headless: bool = False,
    extra_args: list[str] | None = None,
    browser_flags: Sequence[str] = (),
) -> list[str]:
    """Chromium command line; ``extension_dir=None`` leaves out the extension flags."""
    args = [executable, f"--user-data-dir={profile_dir}"]
    args.extend(CHROME_FLAGS)
    if extension_dir is not None:
        args.append(f"--disable-extensions-except={extension_dir}")
        args.append(f"--load-extension={extension_dir}")
    args.extend(browser_flags)
    if headless:
        args.append("--headless=new")
    args.extend(extra_args or ())
    return args


def close(session: BrowserSession) -> None:
    """Release everything ``launch()`` created. Safe to call repeatedly."""
    process, session.process = session.process, None
    job, session._job = session._job, None
    if process is not None:
        if _IS_WINDOWS and not session.headless and process.poll() is None:
            # Chrome ignores the graceful taskkill; closing its last tab exits it.
            _close_all_tabs(session.bridge, CLOSE_TABS_BUDGET_SECONDS)
        _stop_process_tree(
            process, job, headless=session.headless, label=_label(session)
        )
    elif job is not None:
        _close_job(job, terminate=True)

    # After the tree is gone: closing the pipe earlier would make Chrome exit
    # on its own, ahead of the tab close / taskkill sequence above.
    pipe, session._cdp_pipe = session._cdp_pipe, None
    if pipe is not None:
        try:
            pipe.close()
        except Exception:
            logger.exception("CDP pipe close raised")
    bidi, session._bidi = session._bidi, None
    if bidi is not None:
        try:
            bidi.close()
        except Exception:
            logger.exception("WebDriver BiDi session close raised")
    # Firefox's stderr pipe, after the tree is gone so the drain thread sees EOF.
    stderr = process.stderr if process is not None else None
    if stderr is not None:
        _close_stream_bounded(stderr, STDERR_CLOSE_BUDGET_SECONDS)
    log_files, session._log_files = session._log_files, None
    if log_files is not None:
        try:
            log_files.close()
        except OSError as exc:
            logger.debug("Closing %s failed: %s", session.log_path, exc)

    if session._bridge_started:
        session._bridge_started = False
        if session.bridge is not None:
            try:
                session.bridge.shutdown()
            except Exception:
                logger.exception("Bridge shutdown raised")

    session_dir, session.session_dir = session.session_dir, None
    session.extension_dir = None
    session.log_path = None
    session.profile_dir = None  # a user_data_dir profile is left in place
    if session_dir is not None:
        _remove_tree(session_dir)
    if process is not None:
        logger.info("Browser closed")


def _close_stream_bounded(stream: Any, budget: float) -> None:
    """Close *stream* without letting ``close()`` wait more than *budget* seconds.

    A surviving child can keep the pipe's write end open; the Firefox stderr
    drain then sits in ``readline()`` holding the reader's lock, and
    ``stream.close()`` would block on that lock forever. The close runs on a
    daemon thread instead; past the budget the stream is left to the drain and GC.
    """

    def run() -> None:
        try:
            stream.close()
        except (OSError, ValueError) as exc:
            logger.debug("Closing the browser stderr pipe failed: %s", exc)

    closer = threading.Thread(target=run, name="emunium-stderr-close", daemon=True)
    closer.start()
    closer.join(budget)
    if closer.is_alive():
        logger.debug(
            "Browser stderr pipe still busy after %.1f s; leaving it to the reader",
            budget,
        )


def _resolve_executable(session: BrowserSession) -> ResolvedBrowser:
    resolved = resolve_browser(session.browser, session.chrome_path)
    session.resolved = resolved
    session.executable_path = resolved.path
    logger.info(
        "Browser %s via %s (%s)",
        resolved.label,
        resolved.install_method,
        resolved.family,
    )
    return resolved


def resolve_browser(
    browser: str | None = None,
    chrome_path: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    discover: Callable[[], Sequence[InstalledBrowser]] | None = None,
    find: Callable[[str], InstalledBrowser | None] | None = None,
    install_cft: Callable[[], str] | None = None,
    is_file: Callable[[str], bool] | None = None,
    cft_dir: str | None = None,
    platform: str | None = None,
) -> ResolvedBrowser:
    """Pick the browser to launch; every OS dependency is injectable.

    ``browser`` is a discovery name (``chrome``, ``edge``, ``brave``,
    ``chromium``, ``firefox``), ``cft`` (Chrome for Testing), ``auto`` or an
    executable path. When it is None: ``chrome_path``, then
    ``EMUNIUM_CHROME_PATH``, then ``auto`` (the first installed browser of
    ``DEFAULT_PREFERENCE``, else Chrome for Testing).
    """
    env = os.environ if env is None else env
    is_file = os.path.isfile if is_file is None else is_file
    if browser is not None:
        if not isinstance(browser, str) or not browser.strip():
            raise LaunchError(f"browser must be a non-empty string, got {browser!r}")
        spec = browser.strip()
        key = spec.lower()
        if key in BROWSER_ORDER:
            found = (find_browser if find is None else find)(key)
            if found is None:
                raise LaunchError(
                    f"Browser {key!r} is not installed;"
                    f" searched {_searched_locations(key, platform)}"
                )
            return _from_installed(found)
        if key == CFT_NAME:
            return _chrome_for_testing(install_cft)
        if key == AUTO_NAME:
            return _auto(discover, install_cft)
        if is_file(spec):
            return _from_path(spec, cft_dir)
        names = ", ".join((*BROWSER_ORDER, CFT_NAME, AUTO_NAME))
        raise LaunchError(
            f"Browser executable not found: {spec} (browser= is neither one of"
            f" {names} nor an existing file)"
        )
    for override, source in (
        (chrome_path, "chrome_path"),
        (env.get(CHROME_PATH_ENV), CHROME_PATH_ENV),
    ):
        if override:
            if not is_file(override):
                raise LaunchError(
                    f"Browser executable not found: {override} ({source})"
                )
            return _from_path(override, cft_dir)
    return _auto(discover, install_cft)


def _from_installed(found: InstalledBrowser) -> ResolvedBrowser:
    return ResolvedBrowser(found.name, found.path, found.family, found.install_method)


def _chrome_for_testing(install_cft: Callable[[], str] | None) -> ResolvedBrowser:
    path = (ensure_chrome if install_cft is None else install_cft)()
    return ResolvedBrowser(CFT_NAME, path, "chromium", LOAD_EXTENSION)


def _auto(
    discover: Callable[[], Sequence[InstalledBrowser]] | None,
    install_cft: Callable[[], str] | None,
) -> ResolvedBrowser:
    installed = {b.name: b for b in (find_browsers if discover is None else discover)()}
    for name in DEFAULT_PREFERENCE:
        found = installed.get(name)
        if found is not None:
            return _from_installed(found)
    logger.info(
        "No installed browser of %s; using Chrome for Testing", DEFAULT_PREFERENCE
    )
    return _chrome_for_testing(install_cft)


def _from_path(path: str, cft_dir: str | None) -> ResolvedBrowser:
    parts = [p.lower() for p in _PATH_SEPARATORS.split(path) if p]
    basename = parts[-1] if parts else ""
    stem = Path(path).stem.lower()
    if (
        stem.endswith("fox")
        or stem.startswith("firefox-")
        or stem in ("librewolf", "floorp", "zen")
    ):
        return ResolvedBrowser("firefox", path, "firefox", BIDI)
    if _is_chrome_for_testing(parts, cft_dir):
        return ResolvedBrowser(CFT_NAME, path, "chromium", LOAD_EXTENSION)
    if any("google" in p for p in parts) and any("chrome" in p for p in parts):
        # Google-branded Chrome ignores --load-extension.
        return ResolvedBrowser("chrome", path, "chromium", CDP_PIPE)
    if (
        "msedge" in basename
        or "microsoft edge" in basename
        # Linux: microsoft-edge, microsoft-edge-stable, -beta, -dev.
        or basename.startswith("microsoft-edge")
    ):
        name = "edge"
    elif "brave" in basename:
        name = "brave"
    elif any("chromium" in p for p in parts):
        name = "chromium"
    else:
        name = CUSTOM_NAME
    return ResolvedBrowser(name, path, "chromium", LOAD_EXTENSION)


def _is_chrome_for_testing(parts: list[str], cft_dir: str | None) -> bool:
    if any(CHROME_FOR_TESTING in p for p in parts):
        return True
    root = str(_install_dir()) if cft_dir is None else cft_dir
    root_parts = [p.lower() for p in _PATH_SEPARATORS.split(root) if p]
    return bool(root_parts) and parts[: len(root_parts)] == root_parts


def _searched_locations(name: str, platform: str | None) -> str:
    platform = sys.platform if platform is None else platform
    if platform == "win32":
        places = [f"%{root}%\\{WINDOWS_RELATIVE[name]}" for root in WINDOWS_ROOT_ENV]
        exe = WINDOWS_APP_PATHS.get(name)
        if exe is not None:
            places.append(f"HKCU/HKLM\\{APP_PATHS_KEY}\\{exe}")
        return ", ".join(places)
    if platform == "darwin":
        return ", ".join(f"{root}/{MACOS_RELATIVE[name]}" for root in MACOS_ROOTS)
    return "PATH for " + ", ".join(LINUX_COMMANDS[name])


def _label(session: BrowserSession) -> str:
    resolved = session.resolved
    return "Browser" if resolved is None else resolved.label


def _validated_extra_args(extra_args: Any) -> list[str]:
    if extra_args is None:
        return []
    if isinstance(extra_args, str):
        raise LaunchError("extra_args must be a list of strings, not a string")
    args = list(extra_args)
    for arg in args:
        if not isinstance(arg, str):
            raise LaunchError(f"extra_args must contain only strings, got {arg!r}")
    return args


def _write_session_file(extension_dir: Path, port: int, token: str) -> None:
    payload = {"protocol": PROTOCOL_VERSION, "port": port, "token": token}
    (extension_dir / "session.json").write_text(json.dumps(payload), encoding="utf-8")


def _spawn(
    args: list[str], log_path: str, popen_kwargs: Mapping[str, Any] | None = None
) -> subprocess.Popen:
    # The child keeps its own handle to the log; ours can close right away.
    with open(log_path, "wb") as log_file:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": log_file,
            # POSIX: own process group, so close() can signal the whole tree
            # with killpg. Windows Popen ignores the flag.
            "start_new_session": not _IS_WINDOWS,
        }
        # The CDP pipe hands its ends over (pass_fds / handle_list startupinfo).
        kwargs.update(popen_kwargs or {})
        return subprocess.Popen(args, **kwargs)


def _await_connection(
    session: BrowserSession, process: subprocess.Popen, bridge_timeout: float
) -> None:
    deadline = time.monotonic() + bridge_timeout
    while True:
        if session.bridge.wait_for_connection(CONNECT_POLL_SECONDS):
            return
        code = process.poll()
        if code is not None:
            raise LaunchError(_early_exit_message(session, code))
        if time.monotonic() >= deadline:
            raise LaunchError(
                f"The emunium extension did not connect within {bridge_timeout:.1f} s"
                f" ({_label(session)}).{_log_tail_suffix(session.log_path)}"
            )


def _early_exit_message(session: BrowserSession, code: int) -> str:
    message = f"{_label(session)} exited during startup (exit code {code})."
    if session.user_data_dir:
        # Chrome hands the command line to the instance owning the profile and exits.
        message += (
            f" The profile {session.profile_dir} is probably already in use by another"
            " browser instance; close that browser or use a different user_data_dir."
        )
    return message + _log_tail_suffix(session.log_path)


def _log_tail_suffix(log_path: str | None) -> str:
    lines = _read_log_tail(log_path)
    if not lines:
        return ""
    tail = "\n".join(lines)
    return f"\nLast {len(lines)} line(s) of {log_path}:\n{tail}"


def _read_log_tail(log_path: str | None, count: int = LOG_TAIL_LINES) -> list[str]:
    if not log_path:
        return []
    try:
        with open(log_path, "rb") as fh:
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-count:]


def _stop_process_tree(
    process: subprocess.Popen,
    job: int | None = None,
    headless: bool = False,
    label: str = "Browser",
) -> None:
    pid = process.pid
    if _IS_WINDOWS:
        if not headless:
            # Graceful request to every window in the tree. The exit code is
            # meaningless: windowless renderer/GPU processes always make it
            # non-zero. Popen.wait returns as soon as the parent exits.
            _run_taskkill(pid, force=False)
            _wait_quietly(process, GRACEFUL_TASKKILL_GRACE_SECONDS)
        # Headless Chrome has no window to close, so it goes straight to the kill.
        # Unconditional: kills every process left in the tree, orphans included.
        job_killed = job is not None and _close_job(job, terminate=True)
        if not job_killed:
            if process.poll() is None:
                if not _run_taskkill(pid, force=True):
                    _kill_quietly(process)
            else:
                logger.warning(
                    "%s (pid %s) exited before its process tree could be killed;"
                    " child processes may remain",
                    label,
                    pid,
                )
    else:
        if not _signal_group(pid, signal.SIGTERM):
            _terminate_quietly(process)
        _wait_quietly(process, TERMINATE_GRACE_SECONDS)
        # Children may outlive the parent; the group outlives both.
        if not _signal_group(pid, _SIGKILL) and process.poll() is None:
            _kill_quietly(process)
    _wait_quietly(process, TERMINATE_GRACE_SECONDS)


# EmuniumError covers every bridge failure; OSError/RuntimeError a closing socket
# or event loop. Anything else is a bug and must not be hidden.
_CLOSE_TABS_ERRORS = (EmuniumError, OSError, RuntimeError)


def _close_all_tabs(bridge: Any, budget: float) -> None:
    """Best effort: ``closeTab`` every listed tab within *budget* seconds overall.

    Bridge errors are expected and ignored: the reply to the last ``closeTab``
    usually never arrives because Chrome exits with its last window.
    """
    if bridge is None or not getattr(bridge, "connected", False):
        return
    deadline = time.monotonic() + budget
    try:
        tabs = bridge.list_tabs(timeout=budget)
    except _CLOSE_TABS_ERRORS as exc:
        logger.debug("listTabs before close failed: %s", exc)
        return
    for tab in tabs if isinstance(tabs, list) else ():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.debug("Tab close budget of %.1f s spent", budget)
            return
        tab_id = tab.get("tabId") if isinstance(tab, dict) else None
        if not isinstance(tab_id, int) or isinstance(tab_id, bool):
            continue
        try:
            bridge.close_tab(tab_id, timeout=remaining)
        except _CLOSE_TABS_ERRORS as exc:
            logger.debug("closeTab(%s) before close failed: %s", tab_id, exc)


# -- Windows Job Object ----------------------------------------------------------
#
# Chrome is assigned to a job right after Popen returns. Children inherit the
# job, so TerminateJobObject reaches renderers even after the browser process
# has exited. KILL_ON_JOB_CLOSE is deliberate: if Python dies abruptly, the OS
# closes our handle and takes Chrome down with it instead of leaking it.
# Race: a child Chrome spawns before AssignProcessToJobObject runs is not in the
# job. Chrome only spawns children after its own startup, so the window is a few
# microseconds and is accepted rather than starting Chrome with CREATE_SUSPENDED.

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100


class _Kernel32Jobs:
    """Thin ctypes wrapper; failures raise OSError carrying GetLastError."""

    _k32: ctypes.CDLL
    _limit_info: type[ctypes.Structure]
    _last_error: Callable[[], int]

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("kernel32 Job Objects are Windows-only")
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        k32.TerminateJobObject.restype = wintypes.BOOL
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle.restype = wintypes.BOOL
        self._k32 = k32

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self._limit_info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        self._last_error = ctypes.get_last_error

    def _fail(self, what: str) -> OSError:
        err = self._last_error()
        return OSError(err, f"{what} failed (winerror {err})")

    def create_kill_on_close_job(self) -> int:
        job = self._k32.CreateJobObjectW(None, None)
        if not job:
            raise self._fail("CreateJobObjectW")
        info = self._limit_info()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = self._k32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            error = self._fail("SetInformationJobObject")
            self._k32.CloseHandle(job)
            raise error
        return int(job)

    def assign(self, job: int, process: subprocess.Popen) -> None:
        handle = getattr(process, "_handle", None)
        opened = None
        if handle is None:
            opened = self._k32.OpenProcess(
                _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, process.pid
            )
            if not opened:
                raise self._fail("OpenProcess")
            handle = opened
        try:
            if not self._k32.AssignProcessToJobObject(job, int(handle)):
                raise self._fail("AssignProcessToJobObject")
        finally:
            if opened:
                self._k32.CloseHandle(opened)

    def terminate(self, job: int, exit_code: int) -> None:
        if not self._k32.TerminateJobObject(job, exit_code):
            raise self._fail("TerminateJobObject")

    def close(self, handle: int) -> None:
        self._k32.CloseHandle(handle)


# What the kernel32 wrapper raises: OSError (DLL load, GetLastError failures,
# non-Windows hosts) and ctypes.ArgumentError (a value the prototype rejects).
_JOB_API_ERRORS = (OSError, ctypes.ArgumentError)

_JOB_API: Any = None


def _job_api() -> Any:
    """Process-wide kernel32 wrapper; tests replace this function."""
    global _JOB_API
    if _JOB_API is None:
        _JOB_API = _Kernel32Jobs()
    return _JOB_API


def _attach_job(process: subprocess.Popen) -> int | None:
    try:
        api = _job_api()
        job = api.create_kill_on_close_job()
    except _JOB_API_ERRORS as exc:
        logger.debug("Job Object unavailable, falling back to taskkill: %s", exc)
        return None
    try:
        api.assign(job, process)
    except _JOB_API_ERRORS as exc:
        # e.g. already inside a job that forbids nesting (pre-Windows 8 hosts).
        logger.debug(
            "AssignProcessToJobObject failed, falling back to taskkill: %s", exc
        )
        _close_job(job, terminate=False)
        return None
    return job


def _close_job(job: int, terminate: bool) -> bool:
    """Terminate (optionally) and close *job*; True when termination succeeded."""
    try:
        api = _job_api()
    except _JOB_API_ERRORS as exc:
        logger.debug("Job Object API unavailable: %s", exc)
        return False
    terminated = False
    if terminate:
        try:
            api.terminate(job, 1)
            terminated = True
        except _JOB_API_ERRORS as exc:
            logger.debug("TerminateJobObject failed: %s", exc)
    try:
        api.close(job)
    except _JOB_API_ERRORS as exc:
        logger.debug("CloseHandle(job) failed: %s", exc)
    return terminated


def _run_taskkill(pid: int, force: bool) -> bool:
    cmd = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        cmd.append("/F")
    try:
        result = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=TASKKILL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("taskkill for pid %s failed: %s", pid, exc)
        return False
    return result.returncode == 0


def _signal_group(pid: int, sig: int) -> bool:
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        return False
    try:
        killpg(pid, sig)
    except ProcessLookupError:
        return True  # group already gone
    except OSError as exc:
        logger.debug("killpg(%s, %s) failed: %s", pid, sig, exc)
        return False
    return True


def _terminate_quietly(process: subprocess.Popen) -> None:
    try:
        process.terminate()
    except OSError:
        pass


def _kill_quietly(process: subprocess.Popen) -> None:
    try:
        process.kill()
    except OSError:
        pass


def _wait_quietly(process: subprocess.Popen, timeout: float) -> None:
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


def _remove_tree(path: str) -> None:
    # Windows Chrome keeps profile files open for a moment after exit.
    for attempt in range(RMTREE_ATTEMPTS):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if attempt + 1 < RMTREE_ATTEMPTS:
                time.sleep(RMTREE_RETRY_DELAY)
            else:
                logger.debug("rmtree(%s) failed: %s", path, exc)
    if os.path.exists(path):
        logger.warning("Could not remove session directory %s", path)


def _seed_profile(data_dir: str) -> None:
    default_dir = Path(data_dir) / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    prefs_path = default_dir / "Preferences"
    if prefs_path.exists():
        try:
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # unreadable, undecodable or invalid JSON
            prefs = {}
        if not isinstance(prefs, dict):
            prefs = {}
        prefs.setdefault("extensions", {}).setdefault("ui", {})["developer_mode"] = True
        prefs.setdefault("profile", {})["exited_cleanly"] = True
        tracked = prefs.get("prefs", {}).get("tracked_preferences_reset", [])
        if "extensions.ui.developer_mode" in tracked:
            tracked.remove("extensions.ui.developer_mode")
            prefs["prefs"]["tracked_preferences_reset"] = tracked
    else:
        prefs = {
            "extensions": {
                "alerts": {"initialized": True},
                "ui": {"developer_mode": True},
            },
            "profile": {"exited_cleanly": True},
            "browser": {"has_seen_welcome_page": True},
            "prefs": {"tracked_preferences_reset": []},
        }
    prefs_path.write_text(json.dumps(prefs), encoding="utf-8")
    local_state_path = Path(data_dir) / "Local State"
    if not local_state_path.exists():
        local_state = {
            "browser": {"enabled_labs_experiments": []},
            "profile": {"info_cache": {}},
        }
        local_state_path.write_text(json.dumps(local_state), encoding="utf-8")
