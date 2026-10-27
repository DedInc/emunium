"""Discovery of browsers already installed on the machine.

Read-only: nothing here launches, downloads, or writes. Every OS dependency
(platform, environment, file existence, ``PATH`` lookup, Windows App Paths
registry) is injectable so the candidate tables can be exercised on any host.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass

BROWSER_ORDER = ("chrome", "edge", "brave", "chromium", "firefox")
# Firefox is never picked implicitly; it is only chosen by name.
DEFAULT_PREFERENCE = ("chrome", "edge", "brave", "chromium")

FAMILIES = {
    "chrome": "chromium",
    "edge": "chromium",
    "brave": "chromium",
    "chromium": "chromium",
    "firefox": "firefox",
}
# Google-branded Chrome ignores --load-extension since 137, so the extension
# has to be installed over the CDP pipe instead.
INSTALL_METHODS = {
    "chrome": "cdp-pipe",
    "edge": "load-extension",
    "brave": "load-extension",
    "chromium": "load-extension",
    "firefox": "bidi",
}

WINDOWS_ROOT_ENV = ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
WINDOWS_RELATIVE = {
    "chrome": r"Google\Chrome\Application\chrome.exe",
    "edge": r"Microsoft\Edge\Application\msedge.exe",
    "brave": r"BraveSoftware\Brave-Browser\Application\brave.exe",
    "chromium": r"Chromium\Application\chrome.exe",
    "firefox": r"Mozilla Firefox\firefox.exe",
}
WINDOWS_APP_PATHS = {
    "chrome": "chrome.exe",
    "edge": "msedge.exe",
    "brave": "brave.exe",
    "firefox": "firefox.exe",
}
APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"

MACOS_ROOTS = ("/Applications", "~/Applications")
MACOS_RELATIVE = {
    "chrome": "Google Chrome.app/Contents/MacOS/Google Chrome",
    "edge": "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "brave": "Brave Browser.app/Contents/MacOS/Brave Browser",
    "chromium": "Chromium.app/Contents/MacOS/Chromium",
    "firefox": "Firefox.app/Contents/MacOS/firefox",
}

LINUX_COMMANDS = {
    "chrome": ("google-chrome", "google-chrome-stable"),
    "edge": ("microsoft-edge", "microsoft-edge-stable"),
    "brave": ("brave-browser",),
    "chromium": ("chromium", "chromium-browser"),
    "firefox": ("firefox",),
}


@dataclass(frozen=True)
class InstalledBrowser:
    """One browser found on disk and how its extension gets installed."""

    name: str
    family: str
    path: str
    install_method: str


def _registry_app_path(exe: str) -> str | None:
    """Default value of ``App Paths\\<exe>`` under HKCU, then HKLM; never raises."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    key_path = f"{APP_PATHS_KEY}\\{exe}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ) as key:
                value, _kind = winreg.QueryValueEx(key, "")
        except OSError:
            continue
        if isinstance(value, str) and value.strip():
            return value
    return None


def _env_lookup(env: Mapping[str, str], name: str) -> str | None:
    # Windows environment names are case-insensitive (``ProgramFiles``), but
    # an injected mapping is a plain dict.
    value = env.get(name)
    if value is None:
        wanted = name.upper()
        value = next((v for k, v in env.items() if k.upper() == wanted), None)
    return value or None


def _windows_candidates(
    name: str,
    env: Mapping[str, str],
    app_paths: Callable[[str], str | None],
) -> list[str]:
    candidates: list[str] = []
    for root_var in WINDOWS_ROOT_ENV:
        root = _env_lookup(env, root_var)
        if root:
            candidates.append(
                ntpath.normpath(ntpath.join(root, WINDOWS_RELATIVE[name]))
            )
    exe = WINDOWS_APP_PATHS.get(name)
    if exe is not None:
        registered = app_paths(exe)
        if registered:
            cleaned = registered.strip().strip('"').strip()
            if cleaned:
                candidates.append(ntpath.normpath(cleaned))
    return candidates


def _macos_candidates(name: str, env: Mapping[str, str]) -> list[str]:
    candidates: list[str] = []
    for root in MACOS_ROOTS:
        if root.startswith("~"):
            home = env.get("HOME")
            if not home:
                continue
            root = posixpath.join(home, root[2:])
        candidates.append(
            posixpath.normpath(posixpath.join(root, MACOS_RELATIVE[name]))
        )
    return candidates


def find_browsers(
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    exists: Callable[[str], bool] | None = None,
    which: Callable[[str], str | None] | None = None,
    app_paths: Callable[[str], str | None] | None = None,
) -> list[InstalledBrowser]:
    """Installed browsers in ``BROWSER_ORDER``, one per name, first candidate wins."""
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env
    exists = os.path.isfile if exists is None else exists
    which = shutil.which if which is None else which
    app_paths = _registry_app_path if app_paths is None else app_paths

    found: list[InstalledBrowser] = []
    for name in BROWSER_ORDER:
        path: str | None = None
        if platform == "win32":
            path = next(
                (c for c in _windows_candidates(name, env, app_paths) if exists(c)),
                None,
            )
        elif platform == "darwin":
            path = next(
                (c for c in _macos_candidates(name, env) if exists(c)),
                None,
            )
        else:
            for command in LINUX_COMMANDS[name]:
                resolved = which(command)
                if resolved:
                    path = posixpath.normpath(resolved)
                    break
        if path is not None:
            found.append(
                InstalledBrowser(
                    name=name,
                    family=FAMILIES[name],
                    path=path,
                    install_method=INSTALL_METHODS[name],
                )
            )
    return found


def find_browser(
    name: str,
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    exists: Callable[[str], bool] | None = None,
    which: Callable[[str], str | None] | None = None,
    app_paths: Callable[[str], str | None] | None = None,
) -> InstalledBrowser | None:
    """The installed browser called ``name``, or ``None`` when absent."""
    browsers = find_browsers(
        platform=platform, env=env, exists=exists, which=which, app_paths=app_paths
    )
    return next((b for b in browsers if b.name == name), None)
