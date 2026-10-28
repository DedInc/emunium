"""``Browser.connect``: drive a browser that has the attach build installed.

The user installs the persistent extension (``python -m emunium extension
build --out DIR``) into a profile of their own browser. ``connect()`` serves
the hub port with an ``AttachTransport``, waits briefly for that extension to
connect, and otherwise starts the browser detached from this process (no Job
Object, no automation flags, the user's profile) and waits again.

The browser and its profile belong to the user: closing a connection only
shuts the transport, except that ``close_browser=True`` stops a browser that
``connect()`` itself started. A ``user_data_dir`` is never deleted.
"""

from __future__ import annotations

import ctypes
import logging
import ntpath
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, NamedTuple

from emunium._browser.firefox import write_firefox_policy
from emunium._browser.launcher import (
    ResolvedBrowser,
    _stop_process_tree,
    _validated_extra_args,
    resolve_browser,
)
from emunium.chrome_installer import installed_chrome
from emunium.errors import BridgeError, EmuniumError, InvalidArgumentError, LaunchError

logger = logging.getLogger("emunium.browser")

# How long an already running browser gets before connect() starts one.
DETECT_SECONDS = 5.0
# The installed extension's reconnect back-off tops out at about this.
EXTENSION_RETRY_SECONDS = 30
BUILD_COMMAND = "python -m emunium extension build --out DIR"
ARTIFACT_DIR = "DIR/emunium-extension"

# Windows creation flags (subprocess only defines them on Windows).
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_IMAGE_PATH_CHARS = 32768

# The install id format AttachTransport accepts (a UUID).
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
# User-agent markers that prove a browser; Chrome, Brave and Chromium share one UA.
_EDGE_UA_MARKER = " Edg/"
_FIREFOX_UA_MARKER = "Firefox/"


class ProcessEntry(NamedTuple):
    """One running process: pid, parent pid and image path (None if unreadable)."""

    pid: int
    parent: int
    image: str | None


class Connection:
    """What ``connect()`` holds for one ``Browser``.

    ``process`` is the browser ``connect()`` started (None when the extension
    was already connected); it is stopped on ``close()`` only with
    ``close_browser``.
    """

    __slots__ = (
        "_closed",
        "close_browser",
        "pid",
        "port",
        "process",
        "resolved",
        "state_dir",
        "transport",
        "user_agent_name",
    )

    def __init__(
        self,
        transport: Any,
        *,
        port: int,
        state_dir: str,
        close_browser: bool,
    ) -> None:
        self.transport = transport
        self.port = port
        self.state_dir = state_dir
        self.close_browser = bool(close_browser)
        # Known only through a spawn or an explicit browser=; never guessed.
        self.resolved: ResolvedBrowser | None = None
        # Name the connected extension's user agent proves (edge, firefox).
        self.user_agent_name: str | None = None
        self.process: subprocess.Popen | None = None
        # The browser's main process (Windows only, best effort).
        self.pid: int | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def browser_name(self) -> str | None:
        """The resolved browser's name, else the one the user agent proves."""
        if self.resolved is not None:
            return self.resolved.name
        return self.user_agent_name

    @property
    def started_browser(self) -> bool:
        """True when ``connect()`` started the browser process itself."""
        return self.process is not None

    def close(self) -> None:
        """Shut the transport; stop the browser only if connect started it and
        ``close_browser`` is set. Safe to call repeatedly."""
        if self._closed:
            return
        self._closed = True
        try:
            self.transport.shutdown()
        except Exception:
            logger.exception("Attach transport shutdown raised")
        process = self.process
        if process is not None and self.close_browser:
            label = "Browser" if self.resolved is None else self.resolved.label
            _stop_process_tree(process, None, headless=False, label=label)
            logger.info("%s stopped (close_browser=True)", label)


def connect(
    browser: str | None = None,
    *,
    user_data_dir: str | None = None,
    profile_directory: str | None = None,
    port: int | None = None,
    install_id: str | None = None,
    start: bool = True,
    close_browser: bool = False,
    timeout: float = 60.0,
    extra_args: list[str] | None = None,
    load_state: Callable[[int | None], dict[str, Any]] | None = None,
    make_transport: Callable[..., Any] | None = None,
    resolve: Callable[[str | None, bool], ResolvedBrowser] | None = None,
    spawn: Callable[[list[str]], subprocess.Popen] | None = None,
    clock: Callable[[], float] | None = None,
    process_table: Callable[[], list[ProcessEntry]] | None = None,
    platform: str | None = None,
) -> Connection:
    """Serve the hub port until the installed extension connects.

    The keyword-only hooks after ``extra_args`` replace the OS (state files,
    socket server, browser resolution, process spawn, clock, process table)
    and default to the module's own functions, looked up at call time.

    Raises:
        LaunchError: The port is taken, the browser cannot be resolved or
            started, or no extension connected within ``timeout``.
        InvalidArgumentError: A malformed ``timeout``, profile option or
            ``install_id`` (checked before anything starts).
    """
    if install_id is not None and not (
        isinstance(install_id, str) and _UUID_RE.fullmatch(install_id)
    ):
        raise InvalidArgumentError(f"install_id must be a UUID, got {install_id!r}")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise InvalidArgumentError(f"timeout must be a number, got {timeout!r}")
    if not timeout > 0:
        raise InvalidArgumentError(f"timeout must be positive, got {timeout!r}")
    for name, value in (
        ("user_data_dir", user_data_dir),
        ("profile_directory", profile_directory),
    ):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise InvalidArgumentError(
                f"{name} must be a non-empty string, got {value!r}"
            )
    extra = _validated_extra_args(extra_args)
    load_state = _load_state if load_state is None else load_state
    make_transport = _make_transport if make_transport is None else make_transport
    resolve = _resolve if resolve is None else resolve
    spawn = spawn_detached if spawn is None else spawn
    clock = time.monotonic if clock is None else clock

    state = load_state(port)
    hub_port = port or state["port"]
    transport = make_transport(state, port=port, install_id=install_id)
    try:
        transport.start()
    except BridgeError as exc:
        raise LaunchError(_port_error(hub_port, exc)) from exc

    connection = Connection(
        transport,
        port=hub_port,
        state_dir=str(state["state_dir"]),
        close_browser=close_browser,
    )
    try:
        deadline = clock() + float(timeout)
        connected = transport.wait_for_connection(min(DETECT_SECONDS, float(timeout)))
        if not connected and start:
            resolved = resolve(browser, True)
            connection.resolved = resolved
            argv = spawn_argv(resolved, user_data_dir, profile_directory, extra)
            if resolved.family == "firefox":
                write_firefox_policy(resolved.path)
            if user_data_dir is not None:
                Path(user_data_dir).expanduser().resolve().mkdir(
                    parents=True, exist_ok=True
                )
            connection.process = spawn(argv)
            logger.info(
                "%s started detached (pid %s) for the installed extension",
                resolved.label,
                connection.process.pid,
            )
        if not connected:
            connected = transport.wait_for_connection(max(0.0, deadline - clock()))
        if not connected:
            raise LaunchError(_timeout_message(connection, float(timeout)))
    except BaseException:
        connection.close()
        raise

    logger.info("Installed extension connected on hub port %d", hub_port)
    if connection.resolved is None:
        if browser is not None:
            # Nothing spawned, but the caller vouches for the browser.
            connection.resolved = _resolve_name_only(browser, resolve)
        else:
            # Only what the connection proves; "auto" could name another browser.
            agent = connected_user_agent(transport, state.get("hub_path"))
            connection.user_agent_name = browser_from_user_agent(agent)
    connection.pid = _main_pid(connection, browser, process_table, platform)
    return connection


def browser_from_user_agent(user_agent: str | None) -> str | None:
    """``edge`` or ``firefox`` when the user agent proves it, else None.

    Chrome, Brave and Chromium send the same user agent, so none is named.
    """
    if not isinstance(user_agent, str):
        return None
    if _EDGE_UA_MARKER in user_agent:
        return "edge"
    if _FIREFOX_UA_MARKER in user_agent:
        return "firefox"
    return None


def connected_user_agent(transport: Any, hub_path: Any) -> str | None:
    """The user agent the connected extension sent in its hello.

    Read from the active connection's hello, else from the bound install's
    ``hub.json`` record (AttachTransport stores it there on admission).
    """
    active = getattr(transport, "_active", None)
    hello = getattr(active, "hello", None)
    agent = hello.get("userAgent") if isinstance(hello, dict) else None
    if isinstance(agent, str) and agent:
        return agent
    install_id = getattr(transport, "bound_install_id", None)
    if not isinstance(install_id, str) or not hub_path:
        return None
    from emunium._attach.build import load_install

    try:
        record = load_install(str(hub_path), install_id)
    except (OSError, EmuniumError, ValueError) as exc:
        logger.debug("Install record of %s unreadable: %s", install_id, exc)
        return None
    agent = None if record is None else record.get("userAgent")
    return agent if isinstance(agent, str) and agent else None


def spawn_argv(
    resolved: ResolvedBrowser,
    user_data_dir: str | None,
    profile_directory: str | None,
    extra_args: list[str],
) -> list[str]:
    """The detached browser's command line: the profile options, then ``extra_args``.

    Without a profile option the browser opens its default profile. No
    automation flag is added.
    """
    argv = [resolved.path]
    profile = (
        None
        if user_data_dir is None
        else str(Path(user_data_dir).expanduser().resolve())
    )
    if resolved.family == "firefox":
        if profile_directory is not None:
            raise LaunchError(
                "profile_directory is a Chromium option; for Firefox pass the"
                " profile folder as user_data_dir"
            )
        if profile is not None:
            argv.extend(("-profile", profile))
    else:
        if profile is not None:
            argv.append(f"--user-data-dir={profile}")
        if profile_directory is not None:
            argv.append(f"--profile-directory={profile_directory}")
    argv.extend(extra_args)
    return argv


def spawn_options(platform: str | None = None) -> dict[str, Any]:
    """``Popen`` keywords that detach the browser from this process."""
    platform = sys.platform if platform is None else platform
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if platform == "win32":
        options["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    return options


def spawn_detached(argv: list[str]) -> subprocess.Popen:
    """Start ``argv`` outside emunium's console, process group and Job Object."""
    try:
        return subprocess.Popen(argv, **spawn_options())
    except OSError as exc:
        raise LaunchError(f"Cannot start {argv[0]}: {exc}") from exc


def main_process_pid(
    executable: str,
    processes: Iterable[ProcessEntry],
    *,
    prefer: int | None = None,
) -> int | None:
    """The browser's main process: image ``executable``, parent of another image.

    Renderer, GPU and utility processes share the image but have a parent
    that does too. With several main processes (separate profiles) ``prefer``
    picks one when it is among them; otherwise the answer is None.
    """
    target = _path_key(executable)
    entries = list(processes)
    matching = {
        entry.pid
        for entry in entries
        if entry.image is not None and _path_key(entry.image) == target
    }
    roots = sorted(
        {
            entry.pid
            for entry in entries
            if entry.pid in matching
            and (entry.parent == entry.pid or entry.parent not in matching)
        }
    )
    if prefer is not None and prefer in roots:
        return prefer
    if len(roots) == 1:
        return roots[0]
    return None


def windows_process_table() -> list[ProcessEntry]:
    """Every running process with its image path (Toolhelp + QueryFullProcessImageNameW).

    Processes this user may not query (other sessions, protected ones) get
    ``image=None``.
    """
    if sys.platform != "win32":
        raise OSError("the process table is read on Windows only")
    from ctypes import wintypes

    from emunium._native.win32 import PROCESSENTRY32W

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    entry_pointer = ctypes.POINTER(PROCESSENTRY32W)
    k32.Process32FirstW.argtypes = (wintypes.HANDLE, entry_pointer)
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = (wintypes.HANDLE, entry_pointer)
    k32.Process32NextW.restype = wintypes.BOOL
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.CloseHandle.restype = wintypes.BOOL

    snapshot = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        err = ctypes.get_last_error()
        raise OSError(err, f"CreateToolhelp32Snapshot failed (winerror {err})")
    pairs: list[tuple[int, int]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        more = k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            pairs.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
            more = k32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)

    buffer = ctypes.create_unicode_buffer(_IMAGE_PATH_CHARS)
    table: list[ProcessEntry] = []
    for pid, parent in pairs:
        image: str | None = None
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            try:
                size = wintypes.DWORD(_IMAGE_PATH_CHARS)
                if k32.QueryFullProcessImageNameW(
                    handle, 0, buffer, ctypes.byref(size)
                ):
                    image = buffer.value
            finally:
                k32.CloseHandle(handle)
        table.append(ProcessEntry(pid, parent, image))
    return table


# -- defaults the hooks fall back to -----------------------------------------


def _load_state(port: int | None) -> dict[str, Any]:
    # Imported here: the hub state needs the optional cryptography package.
    from emunium._attach.build import load_or_create_state

    return load_or_create_state() if port is None else load_or_create_state(port)


def _make_transport(
    state: dict[str, Any], *, port: int | None, install_id: str | None
) -> Any:
    from emunium._attach.server import AttachTransport

    return AttachTransport(state, port=port, install_id=install_id)


def _installed_cft() -> str:
    path = installed_chrome()
    if path is None:
        raise LaunchError(
            "Chrome for Testing is not installed and is not downloaded just to"
            " name the browser"
        )
    return path


def _resolve(browser: str | None, download: bool) -> ResolvedBrowser:
    """``resolve_browser`` as ``launch()`` uses it; without ``download`` Chrome
    for Testing is only found on disk, never fetched."""
    if download:
        return resolve_browser(browser)
    return resolve_browser(browser, install_cft=_installed_cft)


# -- helpers ----------------------------------------------------------------


def _resolve_name_only(
    browser: str | None, resolve: Callable[[str | None, bool], ResolvedBrowser]
) -> ResolvedBrowser | None:
    try:
        return resolve(browser, False)
    except (EmuniumError, OSError) as exc:
        logger.info("Connected browser not identified: %s", exc)
        return None


def _main_pid(
    connection: Connection,
    browser: str | None,
    process_table: Callable[[], list[ProcessEntry]] | None,
    platform: str | None,
) -> int | None:
    """The main pid, looked up only for a browser known through ``browser=``
    or the spawn; a user-agent name is not enough."""
    if (_current_platform() if platform is None else platform) != "win32":
        return None
    resolved = connection.resolved
    if resolved is None:
        if browser is None and connection.process is None:
            logger.info(
                "Connected browser not identified, so its window is not managed"
                " (no foreground before native input); pass browser= to"
                " Browser.connect to enable it"
            )
        else:
            logger.info(
                "Browser main process unknown: %r could not be resolved", browser
            )
        return None
    table_of = windows_process_table if process_table is None else process_table
    try:
        table = table_of()
    except OSError as exc:
        logger.info("Browser main process unknown: %s", exc)
        return None
    process = connection.process
    pid = main_process_pid(
        resolved.path, table, prefer=None if process is None else process.pid
    )
    if pid is None:
        logger.info(
            "Browser main process of %s not found; the window is not brought"
            " to the foreground by pid",
            resolved.label,
        )
    return pid


def _current_platform() -> str:
    return sys.platform


def _path_key(path: str) -> str:
    # Image paths are Windows paths whatever the host running the tests.
    return ntpath.normcase(ntpath.normpath(path))


def _port_error(port: int, exc: BridgeError) -> str:
    if isinstance(exc.__cause__, OSError):
        return (
            f"Cannot listen on hub port {port}: another emunium connect session"
            f" or another program holds it ({exc})"
        )
    return f"The attach bridge on hub port {port} did not start: {exc}"


def _timeout_message(connection: Connection, timeout: float) -> str:
    resolved = connection.resolved
    where = "" if resolved is None else f" ({resolved.label})"
    return (
        f"The emunium extension did not connect within {timeout:.1f} s{where}."
        f" To install it, run `{BUILD_COMMAND}`, then load {ARTIFACT_DIR} on the"
        " browser's extensions page with developer mode on (Load unpacked)."
        " The build must come from this hub state and port: hub port"
        f" {connection.port}, state dir {connection.state_dir}."
        " A browser that is already running may need up to"
        f" {EXTENSION_RETRY_SECONDS} s to retry the connection."
    )
