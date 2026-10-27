# tests/e2e/test_browsers_e2e.py
"""One smoke case per browser: headless launch, DOM work, close, no leftovers.

Parametrized by ``browser_name`` (see ``conftest.py``): every discoverable
browser plus Chrome for Testing, narrowed with ``--browsers``. A browser that
is not installed is skipped with the reason. No OS input is sent: the click
goes through the DOM.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time

import pytest

from emunium import Browser

pytestmark = pytest.mark.e2e

# The extension may take a while on a cold profile (Firefox installs it over
# WebDriver BiDi after start-up).
BRIDGE_TIMEOUT = 60.0
# close() stops the process tree before returning; the grace only absorbs the
# OS publishing the exits.
EXIT_GRACE_SECONDS = 10.0
EXIT_POLL_SECONDS = 0.5

# UTF-8 so profile paths outside the OEM code page survive the pipe.
WINDOWS_PROCESS_QUERY = (
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    "Get-CimInstance Win32_Process | Select-Object ProcessId, CommandLine"
    " | ConvertTo-Json -Compress"
)

# Adds a click handler that changes the page shortly after the click (so the
# change has to be waited for), then clicks through the DOM.
DOM_CLICK_JS = (
    "const heading = document.getElementById('heading');"
    " heading.addEventListener('click', () => setTimeout(() => {"
    " heading.textContent = 'clicked';"
    " const note = document.createElement('p'); note.id = 'clicked';"
    " note.textContent = 'click landed'; document.body.appendChild(note);"
    " }, 150));"
    " heading.click();"
    " return heading.textContent"
)


def _cft_installed() -> bool:
    """Whether ``browser='cft'`` would start without downloading anything."""
    from emunium import chrome_installer

    override = os.environ.get(chrome_installer.CHROME_PATH_ENV)
    if override:
        return os.path.isfile(override)
    plat = chrome_installer._get_platform_tag()
    exe = chrome_installer._chrome_executable(chrome_installer._install_dir(), plat)
    return exe.is_file()


def _skip_reason(name: str) -> str | None:
    if name == "cft":
        if _cft_installed():
            return None
        return "Chrome for Testing is not installed (the matrix never downloads it)"
    from emunium._browser.discovery import find_browsers

    installed = [found.name for found in find_browsers()]
    if name in installed:
        return None
    return f"{name} is not installed (installed: {', '.join(installed) or 'none'})"


def _windows_processes() -> list[tuple[int, str]]:
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            WINDOWS_PROCESS_QUERY,
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        pytest.fail(
            "Get-CimInstance Win32_Process failed"
            f" ({completed.returncode}): {completed.stderr.decode(errors='replace')}"
        )
    text = completed.stdout.decode("utf-8", errors="replace").lstrip("﻿").strip()
    rows = json.loads(text) if text else []
    if isinstance(rows, dict):
        rows = [rows]
    return [(int(row["ProcessId"]), row.get("CommandLine") or "") for row in rows]


def _linux_processes() -> list[tuple[int, str]]:
    processes: list[tuple[int, str]] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as cmdline:
                raw = cmdline.read()
        except OSError:
            # Exited between listdir and open, or not ours to read.
            continue
        processes.append(
            (int(entry), raw.replace(b"\0", b" ").decode(errors="replace"))
        )
    return processes


def _processes_using(profile_dir: str) -> list[tuple[int, str]] | None:
    """Running processes whose command line names ``profile_dir``.

    None on an OS where command lines are not inspected here.
    """
    if sys.platform == "win32":
        processes = _windows_processes()
    elif sys.platform.startswith("linux"):
        processes = _linux_processes()
    else:
        return None
    needle = os.path.normcase(profile_dir)
    own_pid = os.getpid()
    return [
        (pid, command)
        for pid, command in processes
        if pid != own_pid and needle in os.path.normcase(command)
    ]


def _leftovers_after_close(profile_dir: str) -> list[tuple[int, str]] | None:
    deadline = time.monotonic() + EXIT_GRACE_SECONDS
    while True:
        leftovers = _processes_using(profile_dir)
        if not leftovers or time.monotonic() >= deadline:
            return leftovers
        time.sleep(EXIT_POLL_SECONDS)


def test_browser_matrix(browser_name: str, site_url: str) -> None:
    reason = _skip_reason(browser_name)
    if reason is not None:
        pytest.skip(reason)

    browser = Browser(
        headless=True, browser=browser_name, bridge_timeout=BRIDGE_TIMEOUT
    )
    with contextlib.ExitStack() as cleanup:
        # Registered before launch(): a half-started browser is closed too.
        cleanup.callback(browser.close)
        browser.launch()
        assert browser.browser_name == browser_name
        # Read before close(), which forgets it; a temporary profile lives in
        # the session directory and its path is on every browser command line.
        profile_dir = browser._session.profile_dir
        assert isinstance(profile_dir, str) and profile_dir

        result = browser.goto(site_url + "/index.html")
        assert result["url"] == site_url + "/index.html"
        assert browser.title == "E2E Index"

        heading = browser.query_selector("#heading")
        assert heading is not None
        assert heading.text == "Hello e2e"
        assert heading.inner_text() == "Hello e2e"
        items = browser.query_selector_all("#list li")
        assert [item.text for item in items] == ["alpha", "beta", "gamma"]

        # The handler changes the page after the script returned.
        assert browser.execute_script(DOM_CLICK_JS) == "Hello e2e"
        clicked = browser.locator("#clicked").wait_for("visible", timeout=10)
        assert clicked is not None
        assert clicked.text == "click landed"
        assert heading.inner_text() == "clicked"
        late = browser.wait_for_element("#late", timeout=10)
        assert late is not None and late.text == "arrived late"

        value = browser.execute_script(
            "return {sum: 1 + 2, title: document.title, agent: navigator.userAgent}"
        )
        assert value["sum"] == 3
        assert value["title"] == "E2E Index"
        engine = "Firefox/" if browser_name == "firefox" else "Chrome/"
        assert engine in value["agent"], value["agent"]

        running = _processes_using(profile_dir)
        if running is not None:
            # The matcher sees this launch, so an empty list after close() means gone.
            assert running, f"no running process names the profile {profile_dir}"

        browser.close()

    leftovers = _leftovers_after_close(profile_dir)
    assert not leftovers, f"still running after close(): {leftovers}"
