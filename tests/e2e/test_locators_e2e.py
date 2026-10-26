# tests/e2e/test_locators_e2e.py
"""Live locators and expect() against headless Chrome for Testing.

Every locator kind resolves the intended element of ``site/locators.html``;
every assertion is run passing and failing, plain and negated. No OS input is
sent: headless browsers refuse native input, so actions are only checked to
raise ``NativeInputError`` with the headless hint before touching the page.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest

from emunium import (
    Browser,
    ElementLocator,
    ExpectationError,
    NativeInputError,
    WaitTimeoutError,
    expect,
)
from emunium._browser.context import HEADLESS_HINT

pytestmark = pytest.mark.e2e

MakeBrowser = Callable[..., Browser]
# Failing assertions give up quickly; passing ones keep the default 5 s.
FAIL_TIMEOUT = 0.4


@pytest.fixture
def browser(make_browser: MakeBrowser, site_url: str) -> Browser:
    browser = make_browser(headless=True)
    browser.goto(site_url + "/locators.html")
    return browser


def ids(locator: ElementLocator) -> list[str | None]:
    return [element.attrs.get("id") for element in locator.all()]


def later(browser: Browser, code: str, delay_ms: int = 300) -> None:
    """Run ``code`` in the page after ``delay_ms``."""
    browser.execute_script(f"setTimeout(() => {{ {code} }}, {delay_ms}); return null;")


# -- locator kinds -----------------------------------------------------------------


@pytest.mark.parametrize(
    "selector",
    [
        "#save",
        "css=#save",
        "xpath=//button[@id='save']",
        "//button[@id='save']",
        "(//button)[1]",
        "text=Save",
    ],
)
def test_selector_prefixes_resolve_the_save_button(
    browser: Browser, selector: str
) -> None:
    element = browser.locator(selector).element(timeout=5)

    assert element.attrs.get("id") == "save"


def test_get_by_role_uses_implicit_roles_and_accessible_names(
    browser: Browser,
) -> None:
    assert ids(browser.get_by_role("button", name="Save", exact=True)) == ["save"]
    # Text, then title; case-insensitive substring.
    assert ids(browser.get_by_role("button", name="save")) == ["save", "save-all"]
    assert ids(browser.get_by_role("button", name="send")) == ["submit-input"]
    assert ids(browser.get_by_role("button", name="custom")) == ["div-button"]
    assert ids(browser.get_by_role("link", name="ALPHA")) == ["link-a"]
    assert ids(browser.get_by_role("img", name="company logo")) == ["logo"]
    assert ids(browser.get_by_role("navigation", name="main")) == ["main-nav"]
    assert ids(browser.get_by_role("heading")) == ["heading"]
    assert browser.get_by_role("listitem").count() == 3


def test_get_by_role_quotes_and_cyrillic_folding(browser: Browser) -> None:
    quoted = browser.get_by_role("button", name='Don\'t say "never"', exact=True)
    cyrillic = browser.get_by_role("button", name="сохранить ФАЙЛ")

    assert ids(quoted) == ["quote-button"]
    assert ids(cyrillic) == ["cyr-button"]


def test_get_by_role_names_controls_by_their_labels(browser: Browser) -> None:
    assert ids(browser.get_by_role("textbox", name="email")) == ["email"]
    assert ids(browser.get_by_role("textbox", name="Full name")) == ["full-name"]


def test_get_by_label_by_for_nesting_and_aria_label(browser: Browser) -> None:
    assert ids(browser.get_by_label("Email address", exact=True)) == ["email"]
    assert ids(browser.get_by_label("full NAME")) == ["full-name"]
    assert ids(browser.get_by_label("notes")) == ["notes"]
    assert ids(browser.get_by_label("country")) == ["country"]
    assert ids(browser.get_by_label("rich editor")) == ["editor"]
    assert browser.get_by_label("Email", exact=True).count() == 0


def test_get_by_placeholder_folds_ascii_and_cyrillic(browser: Browser) -> None:
    assert ids(browser.get_by_placeholder("YOU@")) == ["email"]
    assert ids(browser.get_by_placeholder("иван")) == ["full-name"]
    assert ids(browser.get_by_placeholder("Иван Петров", exact=True)) == ["full-name"]


def test_get_by_test_id_and_attribute_override(browser: Browser) -> None:
    assert ids(browser.get_by_test_id("primary-action")) == ["test-id"]

    browser.test_id_attribute = "data-qa"

    assert ids(browser.get_by_test_id("qa-node")) == ["qa"]
    assert browser.get_by_test_id("primary-action").count() == 0


# -- refinement and resolution ---------------------------------------------------------


def test_nth_first_last_and_filters(browser: Browser) -> None:
    items = browser.locator("li")
    role_items = browser.get_by_role("listitem")

    for kind in (items, role_items):
        assert kind.first.text() == "One"
        assert kind.nth(1).text() == "Two"
        assert kind.last.text() == "Three"
        assert kind.filter(has_text="THR").count() == 1
        assert kind.filter(has_text="t").nth(1).text() == "Three"
        assert kind.nth(5).count() == 0
    assert ids(browser.locator("p").filter(visible=False)) == ["hidden-p"]
    assert ids(browser.locator("p").filter(visible=True)) == ["status"]
    assert browser.locator("#missing").exists() is False
    assert browser.locator("#status").is_visible() is True
    assert browser.locator("#hidden-p").is_visible() is False
    assert browser.locator("#status").get_attribute("data-state") == "idle"


def test_element_waits_for_late_elements(browser: Browser) -> None:
    later(
        browser,
        "const li = document.createElement('li'); li.textContent = 'Late';"
        "document.getElementById('list').appendChild(li);",
    )

    assert browser.locator("li").nth(3).element(timeout=5).text == "Late"
    assert browser.get_by_role("listitem").last.text() == "Late"


def test_wait_for_states(browser: Browser) -> None:
    later(browser, "document.getElementById('status').classList.add('gone');")
    assert browser.locator("#status").wait_for("hidden", timeout=5) is None

    later(browser, "document.getElementById('status').classList.remove('gone');")
    assert browser.locator("#status").wait_for("visible", timeout=5) is not None

    later(browser, "document.getElementById('status').remove();")
    assert browser.locator("#status").wait_for("detached", timeout=5) is None

    with pytest.raises(WaitTimeoutError) as info:
        browser.locator("#missing").element(timeout=0.3)
    assert "ElementLocator(css='#missing')" in str(info.value)


def test_actions_refuse_headless_with_the_hint(browser: Browser) -> None:
    save = browser.locator("#save")
    calls: list[Callable[[], Any]] = [
        save.click,
        save.double_click,
        save.right_click,
        save.hover,
        lambda: save.type("x"),
        lambda: save.drag_to("#save-all"),
    ]
    for call in calls:
        with pytest.raises(NativeInputError) as info:
            call()
        assert info.value.hint == HEADLESS_HINT


# -- assertions ------------------------------------------------------------------------


def passes_and_fails(
    passing: Callable[[], None], failing: Callable[[], None]
) -> ExpectationError:
    passing()
    with pytest.raises(ExpectationError) as info:
        failing()
    return info.value


def test_visibility_assertions(browser: Browser) -> None:
    status = browser.locator("#status")
    hidden = browser.locator("#hidden-p")
    fast = FAIL_TIMEOUT

    passes_and_fails(
        lambda: expect(status).to_be_visible(),
        lambda: expect(hidden, timeout=fast).to_be_visible(),
    )
    passes_and_fails(
        lambda: expect(hidden).to_be_hidden(),
        lambda: expect(status, timeout=fast).to_be_hidden(),
    )
    passes_and_fails(
        lambda: expect(hidden).not_.to_be_visible(),
        lambda: expect(status, timeout=fast).not_.to_be_visible(),
    )
    error = passes_and_fails(
        lambda: expect(status).not_.to_be_hidden(),
        lambda: expect(hidden, timeout=fast).not_.to_be_hidden(),
    )
    assert "ElementLocator(css='#hidden-p')" in str(error)
    # Client-side refinement: polled instead of waited in the page.
    passes_and_fails(
        lambda: expect(browser.locator("p").filter(visible=True)).to_be_visible(),
        lambda: expect(browser.locator("li").nth(7), timeout=fast).to_be_visible(),
    )


def test_attachment_assertions_follow_the_page(browser: Browser) -> None:
    status = browser.locator("#status")
    missing = browser.locator("#missing")
    fast = FAIL_TIMEOUT

    passes_and_fails(
        lambda: expect(status).to_be_attached(),
        lambda: expect(missing, timeout=fast).to_be_attached(),
    )
    passes_and_fails(
        lambda: expect(missing).to_be_detached(),
        lambda: expect(status, timeout=fast).to_be_detached(),
    )
    passes_and_fails(
        lambda: expect(missing).not_.to_be_attached(),
        lambda: expect(status, timeout=fast).not_.to_be_attached(),
    )
    passes_and_fails(
        lambda: expect(status).not_.to_be_detached(),
        lambda: expect(missing, timeout=fast).not_.to_be_detached(),
    )
    later(
        browser,
        "const p = document.createElement('p'); p.id = 'missing';"
        "document.body.appendChild(p);",
    )
    expect(missing).to_be_attached()


def test_count_assertions(browser: Browser) -> None:
    items = browser.locator("li")
    fast = FAIL_TIMEOUT

    passes_and_fails(
        lambda: expect(items).to_have_count(3),
        lambda: expect(items, timeout=fast).to_have_count(4),
    )
    passes_and_fails(
        lambda: expect(items).not_.to_have_count(4),
        lambda: expect(items, timeout=fast).not_.to_have_count(3),
    )
    passes_and_fails(
        lambda: expect(items.filter(has_text="o")).to_have_count(2),
        lambda: expect(items.filter(has_text="o"), timeout=fast).to_have_count(3),
    )
    later(browser, "document.querySelector('li').remove();")
    expect(items).to_have_count(2)


def test_text_assertions(browser: Browser) -> None:
    status = browser.locator("#status")
    fast = FAIL_TIMEOUT

    error = passes_and_fails(
        lambda: expect(status).to_have_text(" Status:  idle "),
        lambda: expect(status, timeout=fast).to_have_text("status: idle"),
    )
    assert "last actual 'Status: idle'" in str(error)
    passes_and_fails(
        lambda: expect(status).to_have_text("STATUS: IDLE", ignore_case=True),
        lambda: expect(status, timeout=fast).to_have_text("idle", ignore_case=True),
    )
    # exact=True: the rendered text as it is (the source has three spaces).
    passes_and_fails(
        lambda: expect(status).to_have_text("Status: idle", exact=True),
        lambda: expect(status, timeout=fast).to_have_text("Status:   idle", exact=True),
    )
    passes_and_fails(
        lambda: expect(status).to_contain_text("IDLE", ignore_case=True),
        lambda: expect(status, timeout=fast).to_contain_text("busy", ignore_case=True),
    )
    passes_and_fails(
        lambda: expect(status).to_have_text(re.compile(r"id\w+$")),
        lambda: expect(status, timeout=fast).to_have_text(re.compile(r"^idle")),
    )
    passes_and_fails(
        lambda: expect(status).not_.to_have_text("status: idle"),
        lambda: expect(status, timeout=fast).not_.to_have_text("Status: idle"),
    )
    passes_and_fails(
        lambda: expect(status).to_contain_text("idle"),
        lambda: expect(status, timeout=fast).to_contain_text("IDLE"),
    )
    passes_and_fails(
        lambda: expect(status).not_.to_contain_text("busy"),
        lambda: expect(status, timeout=fast).not_.to_contain_text("idle"),
    )
    later(browser, "document.getElementById('status').textContent = 'Status: busy';")
    expect(status).to_contain_text("busy")


def test_attribute_assertions(browser: Browser) -> None:
    status = browser.locator("#status")
    fast = FAIL_TIMEOUT

    passes_and_fails(
        lambda: expect(status).to_have_attribute("data-state"),
        lambda: expect(status, timeout=fast).to_have_attribute("data-other"),
    )
    passes_and_fails(
        lambda: expect(status).to_have_attribute("data-state", "idle"),
        lambda: expect(status, timeout=fast).to_have_attribute("data-state", "busy"),
    )
    passes_and_fails(
        lambda: expect(status).to_have_attribute("data-state", re.compile("^id")),
        lambda: expect(status, timeout=fast).to_have_attribute(
            "data-state", re.compile("^b")
        ),
    )
    passes_and_fails(
        lambda: expect(status).not_.to_have_attribute("data-state", "busy"),
        lambda: expect(status, timeout=fast).not_.to_have_attribute("data-state"),
    )
    later(browser, "document.getElementById('status').dataset.state = 'busy';")
    expect(status).to_have_attribute("data-state", "busy")


def test_page_assertions(browser: Browser, site_url: str) -> None:
    fast = FAIL_TIMEOUT

    passes_and_fails(
        lambda: expect(browser).to_have_url("*/locators.html"),
        lambda: expect(browser, timeout=fast).to_have_url("*/other.html"),
    )
    passes_and_fails(
        lambda: expect(browser).to_have_url(re.compile(r"locators\.html$")),
        lambda: expect(browser, timeout=fast).not_.to_have_url(
            lambda url: url.startswith(site_url)
        ),
    )
    error = passes_and_fails(
        lambda: expect(browser).to_have_title("E2E Locators"),
        lambda: expect(browser, timeout=fast).to_have_title("E2E"),
    )
    assert "last actual 'E2E Locators'" in str(error)
    passes_and_fails(
        lambda: expect(browser).to_have_title(re.compile("Locators")),
        lambda: expect(browser, timeout=fast).not_.to_have_title(re.compile("E2E")),
    )
    passes_and_fails(
        lambda: expect(browser).to_have_title("e2e locators", ignore_case=True),
        lambda: expect(browser, timeout=fast).to_have_title("e2e locators"),
    )


# -- navigation during a wait --------------------------------------------------------


def test_expect_waits_through_a_navigation(browser: Browser) -> None:
    arrived = browser.locator("#arrived")
    assert arrived.count() == 0

    # The assertion starts on this document; the element exists only on the next.
    later(browser, "location.href = location.pathname + '?next=1';", delay_ms=200)
    expect(arrived, timeout=10).to_be_visible()

    expect(browser).to_have_url("*?next=1")
    expect(arrived).to_have_text("Arrived")


def test_locator_wait_survives_a_navigation(browser: Browser) -> None:
    later(browser, "location.href = location.pathname + '?next=2';", delay_ms=200)

    heading = browser.get_by_role("heading", name="arrived").element(timeout=10)

    assert heading.attrs.get("id") == "arrived"
    expect(browser).to_have_url("*?next=2")
