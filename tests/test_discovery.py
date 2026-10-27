"""Installed browser discovery (slice AB1).

Every OS dependency is a fake: the tables are checked on any host, and the
registry lookup runs against a stand-in ``winreg`` module.
"""

from __future__ import annotations

import contextlib
import dataclasses
import sys
import types
from collections.abc import Callable, Iterable

import pytest

from emunium._browser import discovery
from emunium._browser.discovery import (
    DEFAULT_PREFERENCE,
    InstalledBrowser,
    find_browser,
    find_browsers,
)

WIN_ENV = {
    "PROGRAMFILES": r"C:\Program Files",
    "PROGRAMFILES(X86)": r"C:\Program Files (x86)",
    "LOCALAPPDATA": r"C:\Users\me\AppData\Local",
}


def exists_in(paths: Iterable[str]) -> Callable[[str], bool]:
    present = set(paths)
    return lambda path: path in present


def no_app_paths(exe: str) -> str | None:
    return None


def never_which(command: str) -> str | None:
    raise AssertionError(f"which({command!r}) called off Linux")


def by_name(browsers: list[InstalledBrowser]) -> dict[str, str]:
    return {b.name: b.path for b in browsers}


# -- Windows -------------------------------------------------------------------


def test_windows_table_finds_every_browser_under_program_roots() -> None:
    present = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Users\me\AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Users\me\AppData\Local\Chromium\Application\chrome.exe",
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
    ]
    browsers = find_browsers(
        platform="win32",
        env=WIN_ENV,
        exists=exists_in(present),
        which=never_which,
        app_paths=no_app_paths,
    )
    assert browsers == [
        InstalledBrowser("chrome", "chromium", present[0], "cdp-pipe"),
        InstalledBrowser("edge", "chromium", present[1], "load-extension"),
        InstalledBrowser("brave", "chromium", present[2], "load-extension"),
        InstalledBrowser("chromium", "chromium", present[3], "load-extension"),
        InstalledBrowser("firefox", "firefox", present[4], "bidi"),
    ]


def test_windows_env_names_are_case_insensitive() -> None:
    path = r"C:\PF\Google\Chrome\Application\chrome.exe"
    found = find_browser(
        "chrome",
        platform="win32",
        env={"ProgramFiles": r"C:\PF"},
        exists=exists_in([path]),
        app_paths=no_app_paths,
    )
    assert found is not None and found.path == path


def test_windows_first_candidate_wins_in_root_order() -> None:
    pf = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    x86 = r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
    local = r"C:\Users\me\AppData\Local\Google\Chrome\Application\chrome.exe"
    seen: list[str] = []

    def exists(path: str) -> bool:
        seen.append(path)
        return path in {x86, local}

    browsers = find_browsers(
        platform="win32", env=WIN_ENV, exists=exists, app_paths=no_app_paths
    )
    assert by_name(browsers) == {"chrome": x86}
    assert seen.index(pf) < seen.index(x86)
    assert local not in seen


def test_windows_missing_env_vars_skip_those_roots() -> None:
    probed: list[str] = []

    def exists(path: str) -> bool:
        probed.append(path)
        return False

    assert (
        find_browsers(
            platform="win32",
            env={"LOCALAPPDATA": r"C:\L"},
            exists=exists,
            app_paths=no_app_paths,
        )
        == []
    )
    assert probed and all(p.startswith("C:\\L\\") for p in probed)
    assert find_browsers(platform="win32", env={}, app_paths=no_app_paths) == []


def test_windows_empty_env_value_is_treated_as_missing() -> None:
    probed: list[str] = []

    def exists(path: str) -> bool:
        probed.append(path)
        return False

    find_browsers(
        platform="win32",
        env={"PROGRAMFILES": ""},
        exists=exists,
        app_paths=no_app_paths,
    )
    assert probed == []


def test_windows_app_paths_used_after_roots_and_normalized() -> None:
    registered = {
        "chrome.exe": '"D:/Apps/Chrome/chrome.exe"',
        "msedge.exe": r"D:\Apps\Edge\..\Edge\msedge.exe",
        "brave.exe": "   ",
        "firefox.exe": r"D:\Apps\Firefox\firefox.exe",
    }
    asked: list[str] = []

    def app_paths(exe: str) -> str | None:
        asked.append(exe)
        return registered.get(exe)

    present = [
        r"D:\Apps\Chrome\chrome.exe",
        r"D:\Apps\Edge\msedge.exe",
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"D:\Apps\Firefox\firefox.exe",
    ]
    browsers = find_browsers(
        platform="win32", env=WIN_ENV, exists=exists_in(present), app_paths=app_paths
    )
    assert by_name(browsers) == {
        "chrome": r"D:\Apps\Chrome\chrome.exe",
        "edge": r"D:\Apps\Edge\msedge.exe",
        "firefox": r"C:\Program Files\Mozilla Firefox\firefox.exe",
    }
    # Chromium has no App Paths entry of its own: chrome.exe belongs to Chrome.
    assert asked == ["chrome.exe", "msedge.exe", "brave.exe", "firefox.exe"]


def test_windows_app_paths_entry_must_exist_on_disk() -> None:
    browsers = find_browsers(
        platform="win32",
        env={},
        exists=exists_in([]),
        app_paths=lambda exe: r"D:\gone\chrome.exe",
    )
    assert browsers == []


# -- registry ------------------------------------------------------------------


class FakeKey:
    def __init__(self, value: object) -> None:
        self.value = value


def fake_winreg(
    values: dict[tuple[str, str], object], opened: list[tuple[str, str, int]]
) -> types.ModuleType:
    module = types.ModuleType("winreg")
    hkcu, hklm, key_read = "HKCU", "HKLM", 0x20019

    def open_key(
        hive: str, path: str, reserved: int, access: int
    ) -> contextlib.nullcontext[FakeKey]:
        assert reserved == 0
        opened.append((hive, path, access))
        if (hive, path) not in values:
            raise FileNotFoundError(path)
        value = values[(hive, path)]
        if isinstance(value, OSError):
            raise value
        return contextlib.nullcontext(FakeKey(value))

    def query_value_ex(key: FakeKey, name: str) -> tuple[object, int]:
        assert name == ""
        return key.value, 1

    vars(module).update(
        HKEY_CURRENT_USER=hkcu,
        HKEY_LOCAL_MACHINE=hklm,
        KEY_READ=key_read,
        OpenKey=open_key,
        QueryValueEx=query_value_ex,
    )
    return module


CHROME_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"


def test_registry_prefers_hkcu_and_opens_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[tuple[str, str, int]] = []
    values: dict[tuple[str, str], object] = {
        ("HKCU", CHROME_KEY): r"C:\user\chrome.exe",
        ("HKLM", CHROME_KEY): r"C:\machine\chrome.exe",
    }
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg(values, opened))
    assert discovery._registry_app_path("chrome.exe") == r"C:\user\chrome.exe"
    assert opened == [("HKCU", CHROME_KEY, 0x20019)]


def test_registry_falls_back_to_hklm(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[tuple[str, str, int]] = []
    values: dict[tuple[str, str], object] = {
        ("HKCU", CHROME_KEY): "",
        ("HKLM", CHROME_KEY): r"C:\machine\chrome.exe",
    }
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg(values, opened))
    assert discovery._registry_app_path("chrome.exe") == r"C:\machine\chrome.exe"
    assert [hive for hive, _, _ in opened] == ["HKCU", "HKLM"]


def test_registry_errors_never_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[tuple[str, str, int]] = []
    values: dict[tuple[str, str], object] = {
        ("HKCU", CHROME_KEY): PermissionError("denied"),
        ("HKLM", CHROME_KEY): OSError("broken hive"),
    }
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg(values, opened))
    assert discovery._registry_app_path("chrome.exe") is None
    assert discovery._registry_app_path("msedge.exe") is None


def test_registry_non_string_value_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values: dict[tuple[str, str], object] = {("HKLM", CHROME_KEY): 42}
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg(values, []))
    assert discovery._registry_app_path("chrome.exe") is None


def test_registry_without_winreg_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A None entry makes ``import winreg`` raise ImportError.
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", None)
    assert discovery._registry_app_path("chrome.exe") is None


class PoisonWinreg(types.ModuleType):
    # Dunders stay reachable: pytest's failure report walks sys.modules.
    def __getattribute__(self, name: str) -> object:
        if name.startswith("__"):
            return super().__getattribute__(name)
        pytest.fail(f"winreg.{name} touched off Windows")


def test_registry_off_windows_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "winreg", PoisonWinreg("winreg"))
    assert discovery._registry_app_path("chrome.exe") is None


def test_default_app_paths_is_the_registry_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values: dict[tuple[str, str], object] = {
        ("HKLM", CHROME_KEY): r"C:\machine\chrome.exe"
    }
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg(values, []))
    found = find_browser(
        "chrome",
        platform="win32",
        env={},
        exists=exists_in([r"C:\machine\chrome.exe"]),
    )
    assert found is not None and found.path == r"C:\machine\chrome.exe"


# -- macOS ---------------------------------------------------------------------


def test_macos_table_finds_every_browser() -> None:
    present = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Users/me/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Users/me/Applications/Firefox.app/Contents/MacOS/firefox",
    ]
    browsers = find_browsers(
        platform="darwin",
        env={"HOME": "/Users/me/"},
        exists=exists_in(present),
        which=never_which,
        app_paths=lambda exe: pytest.fail("App Paths queried on macOS"),
    )
    assert [b.path for b in browsers] == present
    assert [b.name for b in browsers] == list(discovery.BROWSER_ORDER)


def test_macos_system_applications_win_over_home() -> None:
    system = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    home = "/Users/me/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    found = find_browser(
        "chrome",
        platform="darwin",
        env={"HOME": "/Users/me"},
        exists=exists_in([system, home]),
    )
    assert found is not None and found.path == system


def test_macos_missing_home_skips_user_applications() -> None:
    probed: list[str] = []

    def exists(path: str) -> bool:
        probed.append(path)
        return False

    assert find_browsers(platform="darwin", env={}, exists=exists) == []
    assert probed and all(p.startswith("/Applications/") for p in probed)


# -- Linux ---------------------------------------------------------------------


def test_linux_table_uses_which_in_order() -> None:
    on_path = {
        "google-chrome-stable": "/usr/bin/google-chrome-stable",
        "microsoft-edge": "/usr/bin/microsoft-edge",
        "brave-browser": "/usr/bin/brave-browser",
        "chromium-browser": "/snap/bin//chromium-browser",
        "firefox": "/usr/bin/firefox",
    }
    asked: list[str] = []

    def which(command: str) -> str | None:
        asked.append(command)
        return on_path.get(command)

    browsers = find_browsers(
        platform="linux",
        env={},
        exists=lambda p: pytest.fail("exists probed on Linux"),
        which=which,
        app_paths=lambda exe: pytest.fail("App Paths queried on Linux"),
    )
    assert by_name(browsers) == {
        "chrome": "/usr/bin/google-chrome-stable",
        "edge": "/usr/bin/microsoft-edge",
        "brave": "/usr/bin/brave-browser",
        "chromium": "/snap/bin/chromium-browser",
        "firefox": "/usr/bin/firefox",
    }
    assert asked == [
        "google-chrome",
        "google-chrome-stable",
        "microsoft-edge",
        "brave-browser",
        "chromium",
        "chromium-browser",
        "firefox",
    ]


def test_linux_first_command_wins() -> None:
    found = find_browser(
        "chrome",
        platform="linux",
        which=lambda c: f"/opt/{c}",
    )
    assert found is not None and found.path == "/opt/google-chrome"


def test_linux_nothing_on_path() -> None:
    assert find_browsers(platform="linux", env={}, which=lambda c: None) == []


# -- ordering and lookup -------------------------------------------------------


def test_result_order_is_fixed_regardless_of_probe_hits() -> None:
    browsers = find_browsers(
        platform="linux",
        which=lambda c: f"/usr/bin/{c}" if c in {"firefox", "chromium"} else None,
    )
    assert [b.name for b in browsers] == ["chromium", "firefox"]


def test_find_browser_absent_and_unknown_names() -> None:
    assert find_browser("edge", platform="linux", which=lambda c: None) is None
    assert find_browser("opera", platform="linux", which=lambda c: f"/bin/{c}") is None


def test_default_preference_excludes_firefox() -> None:
    assert DEFAULT_PREFERENCE == ("chrome", "edge", "brave", "chromium")
    assert "firefox" not in DEFAULT_PREFERENCE


def test_installed_browser_is_frozen() -> None:
    browser = InstalledBrowser("edge", "chromium", "/x", "load-extension")
    for field in ("name", "family", "path", "install_method"):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(browser, field, "changed")
