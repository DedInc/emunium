# tests/e2e/test_dom_e2e.py
"""DOM suite against headless Chrome for Testing: no OS input is sent.

Navigation, lookups, waits, staleness, tabs, network waits, scripts, session
isolation and launch cleanup, each against the local site in ``site/``.
"""

from __future__ import annotations

import glob
import os
import socket
import tempfile
import threading
import time
from typing import Any, Callable

import pytest

from emunium import (
    Browser,
    CSPBlockedError,
    InvalidArgumentError,
    LaunchError,
    NativeInputError,
    NavigationError,
    NavigationTimeoutError,
    ScriptError,
    StaleElementError,
    TargetClosedError,
    Wait,
    WaitStrategy,
    WaitTimeoutError,
)

pytestmark = pytest.mark.e2e

MakeBrowser = Callable[..., Browser]


@pytest.fixture
def browser(make_browser: MakeBrowser) -> Browser:
    return make_browser(headless=True)


# -- navigation ------------------------------------------------------------------


def test_goto_load_returns_page_and_pins_tab(browser: Browser, site_url: str) -> None:
    result = browser.goto(site_url + "/index.html")

    assert result["url"] == site_url + "/index.html"
    assert result["title"] == "E2E Index"
    assert isinstance(result["tabId"], int)
    assert browser.bridge.pinned_tab_id == result["tabId"]
    assert browser.title == "E2E Index"
    assert browser.url == site_url + "/index.html"


def test_goto_wait_until_domcontentloaded_returns_before_load(
    browser: Browser, site_url: str
) -> None:
    started = time.monotonic()
    browser.goto(site_url + "/slow_load.html", wait_until="domcontentloaded")
    elapsed = time.monotonic() - started

    # The image answers after 1.5 s, so the load event cannot have fired yet.
    assert elapsed < 1.2, elapsed
    assert browser.execute_script("return document.readyState") == "interactive"


def test_goto_wait_until_load_waits_for_subresources(
    browser: Browser, site_url: str
) -> None:
    started = time.monotonic()
    browser.goto(site_url + "/slow_load.html", wait_until="load")
    elapsed = time.monotonic() - started

    assert elapsed >= 1.4, elapsed
    assert browser.execute_script("return document.readyState") == "complete"


def test_goto_wait_until_commit_returns_before_body(
    browser: Browser, site_url: str
) -> None:
    started = time.monotonic()
    result = browser.goto(site_url + "/delay-body?ms=2500", wait_until="commit")
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, elapsed
    assert result["url"].startswith(site_url + "/delay-body")


def test_goto_unreachable_raises_navigation_error(
    browser: Browser, site_url: str
) -> None:
    with pytest.raises(NavigationError) as info:
        browser.goto("http://127.0.0.1:1/unreachable", timeout=15)

    assert not isinstance(info.value, NavigationTimeoutError)
    # The browser stays usable after a failed navigation.
    browser.goto(site_url + "/other.html")
    assert browser.title == "E2E Other"


def test_goto_timeout_raises_navigation_timeout(
    browser: Browser, site_url: str
) -> None:
    started = time.monotonic()
    with pytest.raises(NavigationTimeoutError) as info:
        browser.goto(site_url + "/slow?ms=6000", timeout=1.0)
    elapsed = time.monotonic() - started

    assert isinstance(info.value, TimeoutError)
    assert isinstance(info.value, NavigationError)
    assert elapsed < 5.0, elapsed


# -- lookups ----------------------------------------------------------------------


def test_query_selector_and_all(browser: Browser, site_url: str) -> None:
    browser.goto(site_url + "/index.html")

    heading = browser.query_selector("#heading")
    assert heading is not None
    assert heading.tag.lower() == "h1"
    assert heading.text == "Hello e2e"
    assert heading.get_attribute("id") == "heading"
    assert [li.text for li in browser.query_selector_all("#list li")] == [
        "alpha",
        "beta",
        "gamma",
    ]
    assert browser.query_selector("#does-not-exist") is None
    assert browser.query_selector_all("#does-not-exist") == []


def test_xpath_and_text_waits(browser: Browser, site_url: str) -> None:
    browser.goto(site_url + "/index.html")

    beta = browser.wait_for_xpath("//ul[@id='list']/li[2]", timeout=5)
    assert beta is not None and beta.text == "beta"
    late = browser.wait_for_text("arrived late", timeout=5)
    assert late is not None and late.get_attribute("id") == "late"


def test_get_by_text_returns_deepest_match(browser: Browser, site_url: str) -> None:
    browser.goto(site_url + "/index.html")

    matches = browser.get_by_text("Sign in")
    # body, div.card and section all contain the text; only the span is deepest.
    assert [m.get_attribute("id") for m in matches] == ["signin-label"]
    assert browser.get_by_text("Sign in", exact=True)[0].tag.lower() == "span"
    assert browser.get_by_text("Sign", exact=True) == []


# -- waits ------------------------------------------------------------------------


def test_wait_for_late_element_and_strategy(browser: Browser, site_url: str) -> None:
    browser.goto(site_url + "/index.html")

    late = browser.wait("#late", strategy=WaitStrategy.VISIBLE, timeout=5)
    assert late is not None and late.text == "arrived late"


def test_wait_detached_and_count_eq_zero_return_none(
    browser: Browser, site_url: str
) -> None:
    browser.goto(site_url + "/index.html")
    assert browser.query_selector("#doomed") is not None

    browser.execute_script(
        "setTimeout(() => document.getElementById('doomed').remove(), 300)"
    )
    assert browser.wait("#doomed", condition=Wait().detached(), timeout=5) is None
    assert browser.query_selector("#doomed") is None
    assert browser.wait("#doomed", condition=Wait().count_eq(0), timeout=3) is None
    assert browser.wait("#never-there", condition=Wait().detached(), timeout=3) is None


def test_wait_hidden_returns_attached_element(browser: Browser, site_url: str) -> None:
    browser.goto(site_url + "/index.html")

    ghost = browser.wait("#ghost", condition=Wait().hidden(), timeout=3)
    assert ghost is not None and ghost.get_attribute("id") == "ghost"


def test_wait_nested_stable_waits_until_movement_stops(
    browser: Browser, site_url: str
) -> None:
    browser.goto(site_url + "/index.html")

    browser.execute_script("startMover(700)")
    started = time.monotonic()
    mover = browser.wait(
        "#mover", condition=Wait().all_of(Wait().stable(400)), timeout=6
    )
    elapsed = time.monotonic() - started

    assert mover is not None
    done, since_stop = browser.execute_script(
        "return [window.moverDone, Date.now() - window.moverStoppedAt]"
    )
    assert done is True
    # 700 ms of movement plus a 400 ms quiet window, minus timer slack.
    assert elapsed >= 0.9, elapsed
    assert since_stop >= 300, since_stop


def test_wait_custom_js(browser: Browser, site_url: str) -> None:
    browser.goto(site_url + "/index.html")

    heading = browser.wait(
        "#heading",
        condition=Wait().custom_js("return el.textContent === 'Hello e2e'"),
        timeout=3,
    )
    assert heading is not None
    assert (
        browser.wait(
            "#heading",
            condition=Wait().custom_js("return el.textContent === 'nope'"),
            timeout=1,
            raise_on_timeout=False,
        )
        is None
    )
    started = time.monotonic()
    with pytest.raises(ScriptError):
        browser.wait(
            "#heading", condition=Wait().custom_js("return ((("), timeout=8
        )
    assert time.monotonic() - started < 4.0


@pytest.mark.xfail(
    strict=True,
    raises=TimeoutError,
    reason=(
        "product defect: README 'Waiting' says a custom_js condition that throws "
        "raises ScriptError and never silently evaluates to False, but the "
        "MAIN-world evaluator (background_handlers.js 'runtime exceptions mean "
        "\"not yet\"') treats a throw as False, so the wait runs into TimeoutError"
    ),
)
def test_wait_custom_js_that_throws_raises_script_error(
    browser: Browser, site_url: str
) -> None:
    browser.goto(site_url + "/index.html")

    with pytest.raises(ScriptError):
        browser.wait(
            "#heading",
            condition=Wait().custom_js("throw new Error('condition broke')"),
            timeout=1.5,
        )


def test_wait_unknown_condition_rejected_before_waiting(
    browser: Browser, site_url: str
) -> None:
    browser.goto(site_url + "/index.html")

    started = time.monotonic()
    with pytest.raises(InvalidArgumentError):
        browser.wait("#heading", condition=[{"type": "bogus"}], timeout=8)
    assert time.monotonic() - started < 4.0


def test_wait_timeout_raises_and_soft_timeout_returns_none(
    browser: Browser, site_url: str
) -> None:
    browser.goto(site_url + "/index.html")

    with pytest.raises(TimeoutError) as info:
        browser.wait_for_element("#never-there", timeout=1)
    assert isinstance(info.value.__cause__, WaitTimeoutError)
    assert (
        browser.wait("#never-there", timeout=1, raise_on_timeout=False) is None
    )


# -- staleness --------------------------------------------------------------------


def test_handle_goes_stale_after_navigation(browser: Browser, site_url: str) -> None:
    browser.goto(site_url + "/index.html")
    heading = browser.query_selector("#heading")
    assert heading is not None

    browser.goto(site_url + "/index.html")

    with pytest.raises(StaleElementError):
        heading.get_attribute("id")
    # refresh() finds the node again with the query that produced it.
    refreshed = heading.refresh()
    assert refreshed.get_attribute("id") == "heading"
    assert refreshed.text == "Hello e2e"

    browser.goto(site_url + "/index.html")
    with pytest.raises(StaleElementError):
        heading.refresh(requery=False)


def test_handle_goes_stale_when_node_removed(browser: Browser, site_url: str) -> None:
    browser.goto(site_url + "/index.html")
    doomed = browser.query_selector("#doomed")
    assert doomed is not None

    browser.execute_script("document.getElementById('doomed').remove()")

    with pytest.raises(StaleElementError):
        doomed.get_attribute("id")


# -- tabs -------------------------------------------------------------------------


def test_new_tab_switch_and_close_follow_the_pin(
    browser: Browser, site_url: str
) -> None:
    first = browser.goto(site_url + "/index.html")["tabId"]

    second = browser.new_tab(site_url + "/other.html")["tabId"]
    assert browser.bridge.pinned_tab_id == second
    assert browser.wait_for_element("#heading", timeout=5).text == "Other page"

    background = browser.new_tab(site_url + "/index.html", switch=False)["tabId"]
    assert browser.bridge.pinned_tab_id == second
    assert {t["tabId"] for t in browser.tabs()} >= {first, second, background}

    browser.switch_to_tab(background)
    assert browser.bridge.pinned_tab_id == background
    assert browser.tab_info()["tabId"] == background
    handle_in_background = browser.wait_for_element("#heading", timeout=5)
    assert handle_in_background.text == "Hello e2e"

    browser.switch_to_tab(first)
    # A handle keeps addressing the tab it was found in.
    assert handle_in_background.tab_id == background
    assert handle_in_background.get_attribute("id") == "heading"

    browser.close_tab(background)
    with pytest.raises(TargetClosedError):
        browser.bridge.get_tab_info(tab_id=background)
    with pytest.raises((TargetClosedError, StaleElementError)):
        handle_in_background.get_attribute("id")

    browser.close_tab()  # the pinned tab: first
    remaining = {t["tabId"] for t in browser.tabs()}
    assert first not in remaining and background not in remaining
    assert browser.bridge.pinned_tab_id in remaining

    with pytest.raises(InvalidArgumentError):
        browser.switch_to_tab(str(second))  # type: ignore[arg-type]


# -- network waits ----------------------------------------------------------------


def test_network_wait_is_scoped_to_pinned_tab(browser: Browser, site_url: str) -> None:
    browser.goto(site_url + "/index.html")
    pinned = browser.bridge.pinned_tab_id

    background = browser.new_tab(site_url + "/net.html?from=bg", switch=False)["tabId"]

    assert browser.wait_for_response("*/api/ping.json?from=bg", timeout=2) is None
    entry = browser.wait_for_response(
        "*/api/ping.json?from=bg", timeout=5, any_tab=True
    )
    assert entry is not None
    assert entry["tabId"] == background
    assert entry["statusCode"] == 200
    assert browser.bridge.pinned_tab_id == pinned


def test_network_wait_since_ignores_earlier_responses(
    browser: Browser, site_url: str
) -> None:
    browser.goto(site_url + "/index.html")

    before = time.time() * 1000.0
    browser.execute_script("return fetch('/api/ping.json?n=1').then(() => true)")
    first = browser.wait_for_response("*/api/ping.json?n=1", timeout=5)
    assert first is not None and first["statusCode"] == 200

    # A newer action moves the default window past the finished response.
    browser.execute_script("return 1")
    assert browser.wait_for_response("*/api/ping.json?n=1", timeout=1) is None
    again = browser.wait_for_response("*/api/ping.json?n=1", timeout=3, since=before)
    assert again is not None and again["url"].endswith("?n=1")

    with browser.expect_response("*/api/ping.json?n=2", timeout=5) as response:
        browser.execute_script("fetch('/api/ping.json?n=2'); return null")
    assert response.value["statusCode"] == 200


# -- scripts ----------------------------------------------------------------------


def test_execute_script_json_conversion(browser: Browser, site_url: str) -> None:
    browser.goto(site_url + "/index.html")

    result: Any = browser.execute_script(
        "const loop = {a: 1}; loop.self = loop;"
        "return {u: undefined, n: NaN, inf: Infinity, loop, w: window,"
        " node: document.getElementById('heading'), err: new TypeError('boom'),"
        " arr: [1, 'x', null, true, undefined, NaN, -Infinity], nested: {s: 'Привет'}}"
    )

    # Object members that are undefined are dropped, as JSON.stringify does.
    assert "u" not in result
    assert result["n"] is None
    assert result["inf"] is None
    assert result["loop"] == {"a": 1, "self": "[Circular]"}
    assert result["w"] == "[Window]"
    assert result["node"]["nodeName"] == "H1"
    assert result["node"]["id"] == "heading"
    assert result["err"] == {"name": "TypeError", "message": "boom"}
    assert result["arr"] == [1, "x", None, True, None, None, None]
    assert result["nested"] == {"s": "Привет"}
    assert browser.execute_script("return undefined") is None
    assert (
        browser.execute_script("return new Promise(r => setTimeout(() => r(7), 50))")
        == 7
    )
    with pytest.raises(ScriptError):
        browser.execute_script("throw new Error('kaput')")


@pytest.mark.xfail(
    strict=True,
    raises=ScriptError,
    reason=(
        "product defect: README 'JavaScript execution' shows "
        "execute_script(\"const r = await fetch('/api/me'); return r.json();\"), but "
        "background_handlers.js compiles the body with new Function(code) (not "
        "async), so a top-level await raises ScriptError 'await is only valid in "
        "async functions'"
    ),
)
def test_execute_script_body_may_await(browser: Browser, site_url: str) -> None:
    browser.goto(site_url + "/index.html")

    assert (
        browser.execute_script(
            "const r = await fetch('/api/ping.json'); return r.json();"
        )
        == {"pong": True}
    )


def test_execute_script_on_csp_page(browser: Browser, site_url: str) -> None:
    browser.goto(site_url + "/csp.html")

    # A fresh profile has no user-scripts permission, so the userScripts retry is
    # unavailable and the CSP block is reported as such, never as a value.
    with pytest.raises(CSPBlockedError) as info:
        browser.execute_script("return {two: 1 + 1}")
    assert isinstance(info.value, ScriptError)
    assert info.value.code == "csp_blocked"
    with pytest.raises(CSPBlockedError):
        browser.wait("#heading", condition=Wait().custom_js("return true"), timeout=5)
    # Queries and condition-free waits do not evaluate strings: they keep working.
    assert browser.query_selector("#heading").text == "Strict CSP"
    assert browser.wait("#heading", condition=Wait().visible(), timeout=3) is not None
    assert browser.title == "E2E CSP"


# -- sessions ---------------------------------------------------------------------


def test_two_concurrent_sessions_are_isolated(
    make_browser: MakeBrowser, site_url: str
) -> None:
    sessions = [make_browser(headless=True), make_browser(headless=True)]
    assert sessions[0].bridge.actual_port != sessions[1].bridge.actual_port
    assert sessions[0].bridge.token != sessions[1].bridge.token

    pages = ["/index.html", "/other.html"]
    errors: list[BaseException] = []

    def drive(index: int) -> None:
        try:
            current = sessions[index]
            for round_ in range(3):
                current.goto(site_url + pages[index])
                current.execute_script(f"window.__session = {index * 10 + round_}")
                assert current.execute_script("return window.__session") == (
                    index * 10 + round_
                )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=drive, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors

    assert sessions[0].title == "E2E Index"
    assert sessions[1].title == "E2E Other"
    assert all(t["url"].endswith("/index.html") for t in sessions[0].tabs())
    assert all(t["url"].endswith("/other.html") for t in sessions[1].tabs())

    sessions[0].new_tab(site_url + "/other.html")
    assert len(sessions[0].tabs()) == 2
    assert len(sessions[1].tabs()) == 1

    sessions[0].close()
    # Closing one session leaves the other working.
    assert sessions[1].execute_script("return window.__session") == 12


def test_headless_refuses_native_input(browser: Browser, site_url: str) -> None:
    browser.goto(site_url + "/index.html")

    with pytest.raises(NativeInputError):
        browser.click("#heading", human=False)


# -- launch cleanup ---------------------------------------------------------------


def _session_dirs() -> set[str]:
    return set(glob.glob(os.path.join(tempfile.gettempdir(), "emunium-*")))


def test_launch_failure_cleans_up(
    make_browser: MakeBrowser, chrome_path: str, tmp_path: Any
) -> None:
    profile = tmp_path / "profile"
    before = _session_dirs()

    failing = Browser(
        headless=True,
        chrome_path=chrome_path,
        user_data_dir=str(profile),
        bridge_timeout=0.1,
    )
    with pytest.raises(LaunchError, match="did not connect"):
        failing.launch()

    # The session directory (extension copy, token file) is gone.
    assert _session_dirs() <= before
    # The bridge listener is closed.
    port = failing.bridge.actual_port
    assert isinstance(port, int)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    # The killed Chrome released the profile: a new browser can open it.
    recovered = make_browser(headless=True, user_data_dir=str(profile))
    assert isinstance(recovered.tabs(), list)
    recovered.close()

    with pytest.raises(LaunchError, match="not found"):
        Browser(chrome_path=str(tmp_path / "no-chrome.exe")).launch()
    assert _session_dirs() <= before
    # A user_data_dir passed in is kept.
    assert profile.is_dir()
