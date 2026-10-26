"""expect() assertions (dx-api.md section 6) against a fake bridge and a fake clock.

The real ``Bridge`` runs over an in-memory transport; the expect module's
clock is replaced so polling assertions time out without sleeping.
"""

from __future__ import annotations

import copy
import importlib
import pickle
import re
from collections.abc import Callable
from typing import Any

import pytest

import emunium
from emunium import ElementLocator as ExportedLocator
from emunium import ExpectationError, expect
from emunium._bridge.facade import Bridge
from emunium._bridge.transport import PINNED, Reply
from emunium._browser import facade
from emunium._browser.context import BrowserContext
from emunium._browser.facade import Browser
from emunium._browser.locators import FULL_TEXT_CAP, TEXT_CAP, ElementLocator, locator
from emunium.errors import (
    BridgeTimeoutError,
    ContentUnavailableError,
    InvalidArgumentError,
    StaleElementError,
    WaitTimeoutError,
)
from emunium.expect import TRUNCATED_NOTE, LocatorAssertions, PageAssertions

# ``emunium.expect`` the attribute is the function; the module lives in sys.modules.
expect_module = importlib.import_module("emunium.expect")

TAB = 3


def element(text: str = "Done", **attrs: str) -> dict[str, Any]:
    return {
        "elementId": "doc1.1",
        "tag": "p",
        "attrs": dict(attrs),
        "text": text,
        "visible": True,
        "rect": {"x": 0, "y": 0, "width": 50, "height": 10},
    }


class Steps:
    """Replies consumed one per call; the last one repeats."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)

    def next(self) -> Any:
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


class FakeTransport:
    def __init__(self) -> None:
        self.pinned_tab_id: int | None = TAB
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.handlers: dict[str, Any] = {
            "waitForSelector": element(),
            "querySelectorAll": [element()],
            "queryXPath": [element()],
            "pageInfo": {"url": "https://a.test/", "title": "A"},
            "getTabInfo": {"tabId": TAB, "url": "https://a.test/list", "title": "List"},
        }

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        tab_id: Any = PINNED,
        timeout: float = 30.0,
        idempotent: bool = False,
    ) -> Reply:
        self.calls.append((method, dict(params or {})))
        handler = self.handlers[method]
        if isinstance(handler, Steps):
            handler = handler.next()
        if isinstance(handler, BaseException):
            raise handler
        result = handler(params) if callable(handler) else copy.deepcopy(handler)
        return Reply(result, TAB, 1.0)

    def sent(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.calls if name == method]


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def ctx(transport: FakeTransport) -> BrowserContext:
    return BrowserContext(Bridge(transport=transport))


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [50.0]

    def sleep(seconds: float) -> None:
        now[0] = round(now[0] + seconds, 9)

    monkeypatch.setattr(expect_module, "_monotonic", lambda: now[0])
    monkeypatch.setattr(expect_module, "_sleep", sleep)
    return now


@pytest.fixture
def browser(monkeypatch: pytest.MonkeyPatch, transport: FakeTransport) -> Browser:
    monkeypatch.setattr(facade, "Bridge", lambda port=0: Bridge(transport=transport))
    return Browser()


def fails(check: Callable[[], None]) -> ExpectationError:
    with pytest.raises(ExpectationError) as info:
        check()
    return info.value


def carried(error: ExpectationError) -> tuple[Any, Any]:
    """``(error.expected, error.actual)``, read from the instance dict: they are
    set per instance, ``ExpectationError`` does not declare them."""
    state = vars(error)
    return state["expected"], state["actual"]


# -- dispatch and arguments ------------------------------------------------------------


def test_expect_dispatches_on_the_target(ctx: BrowserContext, browser: Browser) -> None:
    assert {"expect", "ElementLocator"} <= set(emunium.__all__)
    assert ExportedLocator is ElementLocator
    assert expect is expect_module.expect
    assert isinstance(expect(locator(ctx, "p")), LocatorAssertions)
    assert isinstance(expect(browser), PageAssertions)
    with pytest.raises(InvalidArgumentError):
        expect(locator(ctx, "p"), timeout=-1)


def test_expectation_error_is_an_assertion_error() -> None:
    assert issubclass(ExpectationError, AssertionError)


# -- extension waits ----------------------------------------------------------------------


def test_visible_waits_in_the_extension_and_returns_none(
    ctx: BrowserContext, transport: FakeTransport
) -> None:
    assert expect(locator(ctx, "#s"), timeout=2).to_be_visible() is None

    assert transport.sent("waitForSelector") == [
        {"selector": "#s", "type": "css", "timeoutMs": 2000, "state": "visible"}
    ]


def test_failure_names_expected_actual_and_locator(
    ctx: BrowserContext, transport: FakeTransport
) -> None:
    transport.handlers["waitForSelector"] = WaitTimeoutError("timed out")
    transport.handlers["querySelectorAll"] = []

    error = fails(lambda: expect(locator(ctx, "#s"), timeout=1).to_be_visible())

    assert str(error) == (
        "expect(ElementLocator(css='#s')).to_be_visible() failed after 1.0s: "
        "expected visible, last actual 'hidden or absent'"
    )
    assert error.method == "expect.to_be_visible"
    assert isinstance(error.__cause__, WaitTimeoutError)


@pytest.mark.parametrize(
    ("check", "state", "conditions"),
    [
        (
            "to_be_hidden",
            None,
            [
                {
                    "type": "any_of",
                    "conditions": [[{"type": "detached"}], [{"type": "hidden"}]],
                }
            ],
        ),
        (
            "not_.to_be_visible",
            None,
            [
                {
                    "type": "any_of",
                    "conditions": [[{"type": "detached"}], [{"type": "hidden"}]],
                }
            ],
        ),
        ("not_.to_be_hidden", "visible", None),
        ("to_be_attached", None, None),
        ("not_.to_be_detached", None, None),
        ("to_be_detached", None, [{"type": "detached"}]),
        ("not_.to_be_attached", None, [{"type": "detached"}]),
    ],
)
def test_state_assertions_send_the_matching_wait(
    ctx: BrowserContext,
    transport: FakeTransport,
    check: str,
    state: str | None,
    conditions: list[dict[str, Any]] | None,
) -> None:
    assertions: Any = expect(locator(ctx, "#s"))
    for name in check.split("."):
        assertions = getattr(assertions, name)

    assertions()

    sent = transport.sent("waitForSelector")[0]
    assert sent.get("state") == state
    assert sent.get("conditions") == conditions


def test_count_text_and_attribute_waits(
    ctx: BrowserContext, transport: FakeTransport
) -> None:
    target = locator(ctx, "li")

    expect(target).to_have_count(2)
    expect(target).not_.to_have_count(2)
    expect(target).to_contain_text("Do")
    expect(target).not_.to_contain_text("x")
    expect(target).to_have_attribute("data-state", "idle")
    expect(target).not_.to_have_attribute("hidden")

    assert [sent["conditions"] for sent in transport.sent("waitForSelector")] == [
        [{"type": "count_eq", "value": 2}],
        [{"type": "not", "condition": [{"type": "count_eq", "value": 2}]}],
        [{"type": "text_contains", "value": "Do"}],
        [{"type": "not", "condition": [{"type": "text_contains", "value": "x"}]}],
        [{"type": "has_attribute", "name": "data-state", "value": "idle"}],
        [
            {
                "type": "not",
                "condition": [
                    {"type": "has_attribute", "name": "hidden", "value": None}
                ],
            }
        ],
    ]


def test_negated_failure_reports_the_negation(
    ctx: BrowserContext, transport: FakeTransport
) -> None:
    transport.handlers["waitForSelector"] = WaitTimeoutError("timed out")
    transport.handlers["querySelectorAll"] = [element(), element()]

    error = fails(lambda: expect(locator(ctx, "li"), timeout=1).not_.to_have_count(2))

    assert ".not_.to_have_count(2)" in str(error)
    assert "expected not count 2, last actual 2" in str(error)


# -- polling ----------------------------------------------------------------------------------


def test_text_polls_until_it_matches(
    ctx: BrowserContext, transport: FakeTransport, clock: list[float]
) -> None:
    transport.handlers["querySelectorAll"] = Steps(
        [element("Loading")], [element("  Status:\n done ")]
    )

    expect(locator(ctx, "p")).to_have_text("Status: done")

    assert clock[0] == pytest.approx(50.1)
    assert transport.sent("waitForSelector") == []


def test_text_poll_times_out_with_the_last_actual(
    ctx: BrowserContext, transport: FakeTransport, clock: list[float]
) -> None:
    transport.handlers["querySelectorAll"] = [element("Loading")]

    error = fails(lambda: expect(locator(ctx, "p"), timeout=1).to_have_text("Done"))

    assert clock[0] == pytest.approx(51.0)
    assert len(transport.sent("querySelectorAll")) == 11
    assert "expected text 'Done', last actual 'Loading'" in str(error)


def test_text_semantics(
    ctx: BrowserContext, transport: FakeTransport, clock: list[float]
) -> None:
    transport.handlers["querySelectorAll"] = [element("Save  all")]
    target = locator(ctx, "p")
    quick = expect(target, timeout=0)

    # Case-sensitive by default; exact=False collapses whitespace.
    quick.to_have_text(" Save all ")
    quick.not_.to_have_text("save all")
    quick.to_have_text("SAVE ALL", ignore_case=True)
    # exact=True compares the text as it is.
    quick.to_have_text("Save  all", exact=True)
    error = fails(lambda: quick.to_have_text("Save all", exact=True))
    fails(lambda: quick.to_have_text("save  all", exact=True))
    quick.to_have_text("save  ALL", exact=True, ignore_case=True)
    quick.to_have_text(re.compile(r"all$"))
    fails(lambda: quick.to_have_text(re.compile(r"^save")))
    quick.to_have_text(re.compile(r"^save"), ignore_case=True)
    quick.not_.to_have_text("Save")

    assert ".to_have_text('Save all', exact=True)" in str(error)


def test_contain_text_case(
    ctx: BrowserContext, transport: FakeTransport, clock: list[float]
) -> None:
    transport.handlers["querySelectorAll"] = [element("Save all")]
    transport.handlers["waitForSelector"] = WaitTimeoutError("timed out")
    quick = expect(locator(ctx, "p"), timeout=0)

    error = fails(lambda: quick.to_contain_text("SAVE"))
    sent_before = len(transport.sent("waitForSelector"))
    quick.to_contain_text("SAVE", ignore_case=True)
    quick.not_.to_contain_text("XYZ", ignore_case=True)
    fails(lambda: quick.not_.to_contain_text("save", ignore_case=True))

    assert "last actual 'Save all'" in str(error)
    # ignore_case cannot use the page's case-sensitive text_contains: it polls.
    assert len(transport.sent("waitForSelector")) == sent_before


# -- texts longer than the TEXT_CAP preview ---------------------------------------------------

LONG = "prefix words " + "x" * (TEXT_CAP - 13)
FULL = LONG + " middle " + "y" * 1400 + " tail end"


def full_text_reply(text: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def reply(params: dict[str, Any]) -> dict[str, Any]:
        assert params == {"elementId": "doc1.1"}
        return {"text": text, "length": len(text), "truncated": False}

    return reply


def test_long_text_is_read_in_full_and_compared_in_full(
    ctx: BrowserContext, transport: FakeTransport, clock: list[float]
) -> None:
    transport.handlers["querySelectorAll"] = [element(LONG)]
    transport.handlers["elementText"] = full_text_reply(FULL)
    quick = expect(locator(ctx, "p").filter(visible=True), timeout=0)

    assert len(LONG) == TEXT_CAP and len(FULL) > 1900
    quick.to_have_text(FULL)
    quick.to_have_text(re.compile("tail end$"))
    quick.to_contain_text("tail end")
    quick.to_contain_text("TAIL", ignore_case=True)
    quick.not_.to_contain_text("absent")
    quick.not_.to_have_text(LONG)
    error = fails(lambda: quick.to_have_text(LONG))
    negated = fails(lambda: quick.not_.to_contain_text("tail end"))

    for message in (str(error), str(negated)):
        assert "truncated" not in message
    assert transport.sent("elementText")


def test_long_text_assertions_fail_on_a_missing_needle_without_a_note(
    ctx: BrowserContext, transport: FakeTransport, clock: list[float]
) -> None:
    transport.handlers["querySelectorAll"] = [element(LONG)]
    transport.handlers["elementText"] = full_text_reply(FULL)
    polled = expect(locator(ctx, "p").filter(visible=True), timeout=1)

    error = fails(lambda: polled.to_contain_text("absent"))

    # The message shows the first 200 chars; the whole text is on ``actual``.
    shown = repr(FULL[:200] + "...") + f" ({len(FULL)} chars)"
    assert str(error).endswith(f"last actual {shown}")
    assert "tail end" not in str(error)
    assert carried(error) == ("absent", FULL)
    assert TRUNCATED_NOTE not in str(error)
    assert clock[0] == pytest.approx(51.0)


def test_failure_message_shows_at_most_200_chars_of_the_actual_text(
    ctx: BrowserContext, transport: FakeTransport, clock: list[float]
) -> None:
    at_limit, over = "a" * 199 + "Z", "b" * 200 + "Q"
    paragraphs = expect(locator(ctx, "p").filter(visible=True), timeout=0)

    transport.handlers["querySelectorAll"] = [element(at_limit)]
    whole = fails(lambda: paragraphs.to_have_text("other"))
    transport.handlers["querySelectorAll"] = [element(over)]
    cut = fails(lambda: paragraphs.to_have_text("other"))

    assert str(whole).endswith(f"last actual {at_limit!r}")
    assert str(cut).endswith(f"last actual {'b' * 200 + '...'!r} (201 chars)")
    assert "Q" not in str(cut)
    assert carried(whole) == ("other", at_limit)
    assert carried(cut) == ("other", over)


def test_expectation_error_carries_expected_and_actual_through_pickling(
    ctx: BrowserContext, transport: FakeTransport, browser: Browser, clock: list[float]
) -> None:
    transport.handlers["querySelectorAll"] = [element(id="a")]
    target = expect(locator(ctx, "p").filter(visible=True), timeout=0)

    count = fails(lambda: target.to_have_count(2))
    attribute = fails(lambda: target.to_have_attribute("id", "b"))
    title = fails(lambda: expect(browser, timeout=0).not_.to_have_title("List"))

    assert carried(count) == (2, 1)
    assert carried(attribute) == (("id", "b"), "a")
    assert carried(title) == ("List", "List")
    restored = pickle.loads(pickle.dumps(count))
    assert carried(restored) == (2, 1)
    assert str(restored) == str(count)


def test_text_cut_at_the_full_text_cap_is_unknown_beyond_the_cut(
    ctx: BrowserContext, transport: FakeTransport, clock: list[float]
) -> None:
    huge = "head " + "z" * (FULL_TEXT_CAP - 5)
    transport.handlers["querySelectorAll"] = [element(huge[:TEXT_CAP])]
    transport.handlers["elementText"] = {
        "text": huge,
        "length": FULL_TEXT_CAP + 10,
        "truncated": True,
    }
    quick = expect(locator(ctx, "p").filter(visible=True), timeout=0)

    quick.to_contain_text("head")
    for check in (
        lambda: quick.to_contain_text("tail"),
        lambda: quick.not_.to_contain_text("tail"),
        lambda: quick.to_have_text(huge),
        lambda: quick.not_.to_have_text("other"),
    ):
        assert str(fails(check)).endswith(
            "; the text was truncated at 1048576 chars and cannot be fully compared"
        )


def test_stale_element_during_the_full_read_is_retried(
    ctx: BrowserContext, transport: FakeTransport, clock: list[float]
) -> None:
    transport.handlers["querySelectorAll"] = [element(LONG)]
    transport.handlers["elementText"] = Steps(
        StaleElementError("gone", method="elementText"),
        {"text": FULL, "length": len(FULL), "truncated": False},
    )

    expect(locator(ctx, "p").filter(visible=True), timeout=1).to_have_text(FULL)

    assert len(transport.sent("elementText")) == 2


def test_text_shorter_than_the_cap_is_complete(
    ctx: BrowserContext, transport: FakeTransport, clock: list[float]
) -> None:
    transport.handlers["querySelectorAll"] = [element(LONG[:-1])]
    quick = expect(locator(ctx, "p").filter(visible=True), timeout=0)

    quick.to_have_text(LONG[:-1])
    quick.not_.to_contain_text("tail")
    assert transport.sent("elementText") == []


def test_missing_element_fails_text_and_passes_its_negation(
    ctx: BrowserContext, transport: FakeTransport, clock: list[float]
) -> None:
    transport.handlers["querySelectorAll"] = []
    quick = expect(locator(ctx, "p").filter(has_text="x"), timeout=0)

    error = fails(lambda: quick.to_have_text("x"))
    quick.not_.to_have_text("x")
    quick.not_.to_have_attribute("id")

    assert "last actual '<no element>'" in str(error)


def test_python_side_refinements_poll_instead_of_waiting(
    ctx: BrowserContext, transport: FakeTransport, clock: list[float]
) -> None:
    transport.handlers["querySelectorAll"] = [element("a"), element("b"), element("ab")]
    items = locator(ctx, "li").filter(has_text="b")

    expect(items).to_have_count(2)
    expect(items).to_be_attached()
    expect(items).to_be_visible()
    expect(items.nth(5)).to_be_detached()
    expect(items.nth(5)).to_be_hidden()

    assert transport.sent("waitForSelector") == []


def test_attribute_pattern_polls(
    ctx: BrowserContext, transport: FakeTransport, clock: list[float]
) -> None:
    transport.handlers["querySelectorAll"] = [element(**{"data-state": "idle"})]
    target = expect(locator(ctx, "p"), timeout=0)

    target.to_have_attribute("data-state", re.compile("^id"))
    error = fails(lambda: target.to_have_attribute("data-state", re.compile("^b")))

    assert "last actual 'idle'" in str(error)
    assert transport.sent("waitForSelector") == []


def test_transient_errors_are_read_again(
    ctx: BrowserContext, transport: FakeTransport, clock: list[float]
) -> None:
    transport.handlers["querySelectorAll"] = Steps(
        ContentUnavailableError("document swap"), [element("Done")]
    )

    expect(locator(ctx, "p")).to_have_text("Done")

    assert clock[0] == pytest.approx(50.1)


def test_a_transient_read_never_passes_a_negation(
    ctx: BrowserContext, transport: FakeTransport, clock: list[float]
) -> None:
    transport.handlers["querySelectorAll"] = ContentUnavailableError("swap")
    polled = expect(locator(ctx, "p").filter(visible=True), timeout=1)

    error = fails(lambda: polled.not_.to_be_visible())

    assert isinstance(error.__cause__, ContentUnavailableError)
    assert "last actual '<ContentUnavailableError: swap>'" in str(error)


def test_to_be_visible_waits_through_a_navigation(
    ctx: BrowserContext, transport: FakeTransport, clock: list[float]
) -> None:
    transport.handlers["waitForSelector"] = Steps(
        ContentUnavailableError("content script did not answer"),
        BridgeTimeoutError("no reply"),
        element(),
    )

    expect(locator(ctx, "#late"), timeout=5).to_be_visible()

    assert [wait["timeoutMs"] for wait in transport.sent("waitForSelector")] == [
        5000,
        4900,
        4800,
    ]


def test_navigation_until_the_deadline_is_an_expectation_error(
    ctx: BrowserContext, transport: FakeTransport, clock: list[float]
) -> None:
    transport.handlers["waitForSelector"] = ContentUnavailableError("swap")

    error = fails(lambda: expect(locator(ctx, "#late"), timeout=1).to_be_visible())

    assert isinstance(error.__cause__, ContentUnavailableError)
    assert clock[0] == pytest.approx(51.0)


def test_a_late_bridge_timeout_is_an_expectation_error(
    ctx: BrowserContext, transport: FakeTransport, clock: list[float]
) -> None:
    def late(params: Any) -> Any:
        clock[0] += 9.0
        raise BridgeTimeoutError("no reply")

    transport.handlers["waitForSelector"] = late

    error = fails(lambda: expect(locator(ctx, "#late"), timeout=1).to_be_visible())

    assert isinstance(error.__cause__, BridgeTimeoutError)


# -- page assertions -----------------------------------------------------------------------


def test_url_assertions(
    browser: Browser, transport: FakeTransport, clock: list[float]
) -> None:
    page = expect(browser, timeout=0)

    page.to_have_url("https://a.test/*")
    page.to_have_url(re.compile(r"/list$"))
    page.to_have_url(lambda url: url.endswith("list"))
    page.not_.to_have_url("*/other")
    error = fails(lambda: page.to_have_url("*/other"))

    assert "last actual 'https://a.test/list'" in str(error)
    assert "expect(" in str(error) and ".to_have_url('*/other')" in str(error)


def test_url_polls_until_the_navigation_lands(
    browser: Browser, transport: FakeTransport, clock: list[float]
) -> None:
    transport.handlers["getTabInfo"] = Steps(
        {"tabId": TAB, "url": "https://a.test/list", "title": "List"},
        {"tabId": TAB, "url": "https://a.test/done", "title": "Done"},
    )

    expect(browser).to_have_url("*/done")
    expect(browser).to_have_title("Done")

    assert clock[0] == pytest.approx(50.1)


def test_title_assertions(browser: Browser, clock: list[float]) -> None:
    page = expect(browser, timeout=0)

    page.to_have_title("List")
    page.to_have_title(re.compile("^Li"))
    page.not_.to_have_title("list")
    page.to_have_title(" LIST ", ignore_case=True)
    page.to_have_title(re.compile("^li"), ignore_case=True)
    error = fails(lambda: page.to_have_title("Lis"))

    assert "expected title 'Lis', last actual 'List'" in str(error)


def test_assertions_are_reusable_and_not_is_symmetric(
    ctx: BrowserContext, browser: Browser
) -> None:
    target = expect(locator(ctx, "p"))

    assert isinstance(target.not_.not_, LocatorAssertions)
    assert isinstance(expect(browser).not_, PageAssertions)
    assert isinstance(locator(ctx, "p"), ElementLocator)
