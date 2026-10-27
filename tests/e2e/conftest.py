# tests/e2e/conftest.py
"""Fixtures for the real-browser end-to-end suite.

- ``site_url``: a local ``http.server`` on 127.0.0.1 serving ``tests/e2e/site``,
  plus two delay routes used by the navigation tests:
  ``/slow?ms=N`` answers after N ms, ``/delay-body?ms=N`` sends its headers at
  once and the HTML body after N ms.
- ``chrome_path``: the installed Chrome for Testing (``ensure_chrome()`` finds
  it locally; the suite never downloads anything).
- ``make_browser``: launches ``Browser`` instances and closes every one of them
  when the test ends, whatever the outcome.

Every test module here sets ``pytestmark = pytest.mark.e2e``, so the root
conftest skips it unless ``--e2e`` or ``EMUNIUM_E2E=1`` is given, and its input
safety net stays disarmed for these tests only.
"""

from __future__ import annotations

import contextlib
import http.server
import logging
import os
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

SITE_DIR = Path(__file__).resolve().parent / "site"
ROOT_CONFTEST = Path(__file__).resolve().parent.parent / "conftest.py"
# Upper bound for any single delay route, so a stuck test cannot pin a thread.
MAX_DELAY_MS = 10_000

logger = logging.getLogger("emunium.e2e")


def pytest_configure(config: pytest.Config) -> None:
    """Give the module name ``conftest`` back to ``tests/conftest.py``.

    Neither conftest lives in a package, so pytest imports both as the module
    ``conftest`` and this one, imported later, replaces the root one in
    ``sys.modules``. Unit tests import helpers with ``from conftest import ...``
    and must get the root module (the same safety-net objects pytest uses).
    pytest calls this hook when it registers this file, after its import check.
    """
    for plugin in config.pluginmanager.get_plugins():
        plugin_file = getattr(plugin, "__file__", None)
        if plugin_file and Path(plugin_file).resolve() == ROOT_CONFTEST:
            sys.modules["conftest"] = plugin
            return


class _SiteHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(SITE_DIR), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def end_headers(self) -> None:
        # Every navigation must reach the server: no cached documents or images.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _delay_ms(self, query: str) -> int:
        values = urllib.parse.parse_qs(query).get("ms", ["0"])
        try:
            return max(0, min(int(values[0]), MAX_DELAY_MS))
        except ValueError:
            return 0

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/slow":
            time.sleep(self._delay_ms(parsed.query) / 1000.0)
            body = b"slow"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._write_quietly(body)
            return
        if parsed.path == "/delay-body":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self._write_quietly(
                b"<!doctype html><html><head><title>E2E Delayed</title>"
            )
            time.sleep(self._delay_ms(parsed.query) / 1000.0)
            self._write_quietly(
                b"</head><body><h1 id='heading'>late body</h1></body></html>"
            )
            self.close_connection = True
            return
        super().do_GET()

    def _write_quietly(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # The browser navigated away mid-response; nothing left to serve.
            pass


class _SiteServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


@pytest.fixture(scope="session")
def site_url() -> Iterator[str]:
    server = _SiteServer(("127.0.0.1", 0), _SiteHandler)
    thread = threading.Thread(target=server.serve_forever, name="e2e-site", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="session")
def chrome_path() -> str:
    from emunium import ensure_chrome

    path = ensure_chrome()
    if not os.path.isfile(path):
        pytest.fail(f"ensure_chrome() returned a missing binary: {path}")
    return path


@pytest.fixture
def make_browser(chrome_path: str) -> Iterator[Callable[..., Any]]:
    """``make_browser(**kwargs)`` -> a launched ``Browser``; all closed at teardown.

    A failed launch is logged and retried once with a new ``Browser`` (new
    bridge, new session directory, so a fresh profile unless the test passed
    its own ``user_data_dir``). Every instance created, launched or not, is
    closed at teardown.
    """
    from emunium import Browser

    # Closes every browser in reverse launch order; each close runs even when
    # an earlier one raised, and the errors propagate chained.
    with contextlib.ExitStack() as closers:

        def launch_one(kwargs: dict[str, Any]) -> Any:
            browser = Browser(**kwargs)
            # Registered before launch(): a half-started browser is closed too.
            closers.callback(browser.close)
            browser.launch()
            return browser

        def factory(**kwargs: Any) -> Any:
            kwargs.setdefault("chrome_path", chrome_path)
            kwargs.setdefault("bridge_timeout", 30.0)
            try:
                return launch_one(kwargs)
            except Exception:
                logger.warning(
                    "Browser launch failed; retrying once with a fresh session",
                    exc_info=True,
                )
            return launch_one(kwargs)

        yield factory
