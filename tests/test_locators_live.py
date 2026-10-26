"""Live locators (dx-api.md section 4): XPath compiler, resolution, retry rules.

The real ``Bridge`` runs over an in-memory fake transport and element actions
drive a fake interactor (``verify_input=False``), so the real ``Element``
transactions run without Chrome and without OS input (the conftest safety net
traps the real entry points).
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from emunium import ElementLocator
from emunium._bridge.facade import Bridge
from emunium._bridge.transport import PINNED, Reply
from emunium._browser import facade
from emunium._browser import locators as locators_module
from emunium._browser.context import HEADLESS_HINT, BrowserContext
from emunium._browser.facade import Browser
from emunium._browser.locators import (
    TEXT_CAP,
    css_to_xpath_predicate,
    fold,
    get_by_label,
    get_by_placeholder,
    get_by_role,
    get_by_test_id,
    is_transient,
    label_xpath,
    locator,
    role_xpath,
    text_matches,
    xpath_literal,
)
from emunium._standalone.config import ClickType
from emunium.errors import (
    BridgeTimeoutError,
    ContentUnavailableError,
    InvalidArgumentError,
    NativeInputError,
    StaleElementError,
    WaitTimeoutError,
)
from emunium.locator import ROLE_SELECTOR_MAP

TAB = 7
GEOMETRY: dict[str, Any] = {
    "screenX": 0,
    "screenY": 0,
    "outerWidth": 1000,
    "outerHeight": 800,
    "innerWidth": 1000,
    "innerHeight": 700,
    "devicePixelRatio": 1.0,
}
# U+0400-U+042F and their lower-case forms, in table order.
CYRILLIC_UPPER = "ЀЁЂЃЄЅІЇЈЉЊЋЌЍЎЏАБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
CYRILLIC_LOWER = "ѐёђѓєѕіїјљњћќѝўџабвгдежзийклмнопрстуфхцчшщъыьэюя"


def element(element_id: str, text: str = "Go", **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "elementId": element_id,
        "tag": "button",
        "attrs": {"id": element_id},
        "text": text,
        "visible": True,
        "rect": {"x": 10, "y": 10, "width": 40, "height": 20},
        "geometry": dict(GEOMETRY),
    }
    data.update(extra)
    return data


@dataclass
class Call:
    method: str
    params: dict[str, Any]


class FakeTransport:
    """``handlers[method]``: a value, a list consumed per call (the last entry
    stays), or ``fn(params)``; an exception value is raised."""

    def __init__(self) -> None:
        self.pinned_tab_id: int | None = TAB
        self.calls: list[Call] = []
        self.handlers: dict[str, Any] = {
            "waitForSelector": element("doc1.1"),
            "querySelectorAll": [element("doc1.1")],
            "queryXPath": [element("doc1.1")],
            "queryByText": [element("doc1.1")],
            "activateTab": {},
            "scrollIntoView": element("doc1.1"),
            "describeElement": element("doc1.1"),
            "focusState": {"documentHasFocus": True, "activeIsElement": True},
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
        tab = self.pinned_tab_id if tab_id is PINNED else tab_id
        self.calls.append(Call(method, dict(params or {})))
        handler = self.handlers[method]
        if isinstance(handler, Steps):
            handler = handler.next()
        if isinstance(handler, BaseException):
            raise handler
        result = handler(params) if callable(handler) else copy.deepcopy(handler)
        return Reply(result, tab, 1.0)

    def sent(self, method: str) -> list[dict[str, Any]]:
        return [call.params for call in self.calls if call.method == method]

    def methods(self) -> list[str]:
        return [call.method for call in self.calls]


class Steps:
    """Replies consumed one per call; the last one repeats."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)

    def next(self) -> Any:
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


class FakeInteractor:
    """Records OS events instead of sending them."""

    def __init__(self) -> None:
        self.cursor = (0, 0)
        self.events: list[tuple[Any, ...]] = []

    def position(self) -> tuple[int, int]:
        return self.cursor

    def move(
        self,
        x: float,
        y: float,
        *,
        human: bool = True,
        target_size: float | None = None,
    ) -> None:
        self.cursor = (int(x), int(y))
        self.events.append(("move", self.cursor))

    def press(self, button: str = "left", *, clicks: int = 1) -> None:
        self.events.append(("press", button, clicks))

    def button_down(self, button: str = "left") -> None:
        self.events.append(("down", button))

    def button_up(self, button: str = "left") -> None:
        self.events.append(("up", button))

    def press_escape(self) -> None:
        self.events.append(("escape",))

    def can_land(self, x: int, y: int) -> bool:
        return True

    def type_text(
        self,
        text: str,
        *,
        characters_per_minute: int | None = None,
        offset: int | None = None,
        human: bool = True,
    ) -> None:
        self.events.append(("type", text))

    def os_events(self) -> list[tuple[Any, ...]]:
        """Button and key events (moves excluded)."""
        return [event for event in self.events if event[0] != "move"]


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def interactor() -> FakeInteractor:
    return FakeInteractor()


@pytest.fixture
def ctx(transport: FakeTransport, interactor: FakeInteractor) -> BrowserContext:
    return BrowserContext(
        Bridge(transport=transport), interactor=interactor, verify_input=False
    )


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """locators' monotonic clock; its _sleep advances the clock instead of sleeping."""
    now = [100.0]

    def sleep(seconds: float) -> None:
        now[0] = round(now[0] + seconds, 9)

    monkeypatch.setattr(locators_module, "_monotonic", lambda: now[0])
    monkeypatch.setattr(locators_module, "_sleep", sleep)
    return now


# -- XPath compiler -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "literal"),
    [
        ("Save", "'Save'"),
        ("Don't", '"Don\'t"'),
        ('say "hi"', "'say \"hi\"'"),
        ('Don\'t say "no"', "concat('Don', \"'\", 't say \"no\"')"),
        ("'a\"", "concat(\"'\", 'a\"')"),
        ("", "''"),
    ],
)
def test_xpath_literal_handles_both_quote_kinds(value: str, literal: str) -> None:
    assert xpath_literal(value) == literal


def test_case_folding_covers_ascii_and_cyrillic() -> None:
    assert fold("ПРИВЕТ, Ёлка! ABC É") == "привет, ёлка! abc É"
    assert fold(CYRILLIC_UPPER) == CYRILLIC_LOWER


def test_case_folding_covers_ukrainian_and_belarusian_letters() -> None:
    # Є І Ї (Ukrainian), Ў (Belarusian), Ђ (Serbian); Ґ (U+0490) is outside
    # the folded block and stays as it is.
    assert fold("ЄВРОПА ІЇЖАК ЎЗЯЛ ЂАК Ґ") == "європа іїжак ўзял ђак Ґ"
    xpath = text_matches(".", "ЇЖАК", exact=False)
    assert xpath.endswith(", 'їжак')")


def test_text_matches_folds_with_translate_and_normalizes_the_needle() -> None:
    loose = text_matches(".", "  СОХРАНИТЬ\n Файл ", exact=False)
    exact = text_matches("@title", "  Save \t all ", exact=True)

    assert loose.startswith("contains(translate(normalize-space(.), '")
    assert CYRILLIC_UPPER in loose and CYRILLIC_LOWER in loose
    assert loose.endswith(", 'сохранить файл')")
    assert exact == "normalize-space(@title) = 'Save all'"


def test_text_matches_quotes_needles_with_both_quote_kinds() -> None:
    xpath = text_matches(".", 'Don\'t say "no"', exact=True)

    assert xpath == "normalize-space(.) = concat('Don', \"'\", 't say \"no\"')"


def test_every_role_selector_converts_to_xpath() -> None:
    for role, selector in ROLE_SELECTOR_MAP.items():
        predicate = css_to_xpath_predicate(selector)
        assert predicate.count(") or (") == selector.count(","), role


@pytest.mark.parametrize(
    ("selector", "predicate"),
    [
        ("button", "(self::button)"),
        ('[role="button"]', "(@role = 'button')"),
        ("a[href]", "(self::a and @href)"),
        ("input:not([type])", "(self::input and not(@type))"),
        (
            'input[type="submit"]',
            (
                "(self::input and translate(@type, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                "'abcdefghijklmnopqrstuvwxyz') = 'submit')"
            ),
        ),
    ],
)
def test_css_compounds_convert(selector: str, predicate: str) -> None:
    assert css_to_xpath_predicate(selector) == predicate


@pytest.mark.parametrize("selector", ["div > a", "a:hover", "[x='y']", "", "a,"])
def test_unsupported_css_is_refused(selector: str) -> None:
    with pytest.raises(InvalidArgumentError):
        css_to_xpath_predicate(selector)


def test_implicit_roles_follow_the_map_plus_the_role_attribute() -> None:
    button = role_xpath("button")
    custom = role_xpath("tab")

    assert button.startswith("//*[(self::button) or (@role = 'button') or ")
    assert "= 'submit')" in button
    assert button.endswith(" or @role = 'button']")
    assert custom == "//*[@role = 'tab']"


def test_accessible_name_sources() -> None:
    xpath = role_xpath("button", "Save", exact=True)

    for source in (
        "normalize-space(.) = 'Save'",
        "(@aria-label and normalize-space(@aria-label) = 'Save')",
        "(@title and normalize-space(@title) = 'Save')",
        "(@alt and normalize-space(@alt) = 'Save')",
        "(@value and normalize-space(@value) = 'Save')",
        "@id = //label[normalize-space(.) = 'Save']/@for",
        "ancestor::label[normalize-space(.) = 'Save']",
    ):
        assert source in xpath


def test_label_association_by_for_and_by_nesting() -> None:
    xpath = label_xpath("Почта", exact=False)

    assert xpath.startswith(
        "//*[((self::input or self::select or self::textarea or @contenteditable) "
        "and (@id = //label[contains(translate(normalize-space(.), '"
    )
    assert "/@for or ancestor::label[contains(" in xpath
    assert "'почта')" in xpath
    assert xpath.endswith("'почта'))]")
    assert (
        "or (@aria-label and contains(translate(normalize-space(@aria-label)" in xpath
    )


def test_placeholder_and_test_id_xpath(
    ctx: BrowserContext, transport: FakeTransport
) -> None:
    placeholder = get_by_placeholder(ctx, "Иван", exact=True)
    test_id = get_by_test_id(ctx, 'it\'s "x"', attribute="data-qa")

    placeholder.count()
    test_id.count()
    sent = [params["xpath"] for params in transport.sent("queryXPath")]
    assert sent == [
        "//*[(@placeholder and normalize-space(@placeholder) = 'Иван')]",
        "//*[@data-qa = concat('it', \"'\", 's \"x\"')]",
    ]
    with pytest.raises(InvalidArgumentError):
        get_by_test_id(ctx, "x", attribute="data qa]")


def test_browser_test_id_attribute_can_be_overridden(
    monkeypatch: pytest.MonkeyPatch, transport: FakeTransport
) -> None:
    monkeypatch.setattr(facade, "Bridge", lambda port=0: Bridge(transport=transport))
    browser = Browser()

    assert Browser.test_id_attribute == "data-testid"
    browser.get_by_test_id("a").count()
    browser.test_id_attribute = "data-qa"
    browser.get_by_test_id("b").count()
    monkeypatch.setattr(Browser, "test_id_attribute", "data-cy")
    Browser().get_by_test_id("c").count()

    assert [params["xpath"] for params in transport.sent("queryXPath")] == [
        "//*[@data-testid = 'a']",
        "//*[@data-qa = 'b']",
        "//*[@data-cy = 'c']",
    ]


# -- construction, immutability, repr -----------------------------------------------


@pytest.mark.parametrize(
    ("selector", "method", "params"),
    [
        ("button.go", "querySelectorAll", {"selector": "button.go"}),
        ("css=//odd", "querySelectorAll", {"selector": "//odd"}),
        ("xpath=//a", "queryXPath", {"xpath": "//a"}),
        ("//a[@id='x']", "queryXPath", {"xpath": "//a[@id='x']"}),
        ("(//a)[2]", "queryXPath", {"xpath": "(//a)[2]"}),
        ("text=Hello", "queryByText", {"text": "Hello", "exact": False}),
    ],
)
def test_selector_prefixes(
    ctx: BrowserContext,
    transport: FakeTransport,
    selector: str,
    method: str,
    params: dict[str, Any],
) -> None:
    locator(ctx, selector).all()

    assert transport.calls[-1].method == method
    assert transport.calls[-1].params == params


@pytest.mark.parametrize("selector", ["", "css=", "xpath=  ", "text="])
def test_empty_selectors_are_refused(ctx: BrowserContext, selector: str) -> None:
    with pytest.raises(InvalidArgumentError):
        locator(ctx, selector)


def test_locators_are_immutable_and_refinements_are_new(ctx: BrowserContext) -> None:
    base = locator(ctx, "li")
    second = base.nth(1)

    with pytest.raises(AttributeError):
        base._query = second._query
    assert base is not second
    assert base == locator(ctx, "li")
    assert hash(base) == hash(locator(ctx, "li"))
    assert base != second
    assert repr(base) == "ElementLocator(css='li')"
    with pytest.raises(InvalidArgumentError):
        base.nth(-1)


def test_repr_lists_origin_and_refinements(ctx: BrowserContext) -> None:
    save = get_by_role(ctx, "button", name="Save").nth(0)
    rows = locator(ctx, "tr").filter(has_text="x", visible=True).last
    email = get_by_label(ctx, "Email", exact=True)

    assert repr(save) == "ElementLocator(role='button', name='Save', nth=0)"
    assert repr(rows) == (
        "ElementLocator(css='tr', has_text='x', visible=True, last=True)"
    )
    assert repr(email) == "ElementLocator(label='Email', exact=True)"


# -- resolution -----------------------------------------------------------------------


def test_xpath_refinements_fold_into_the_expression(
    ctx: BrowserContext, transport: FakeTransport
) -> None:
    rows = locator(ctx, "//tr").filter(has_text="Ab").nth(1)
    last = locator(ctx, "//tr").last

    rows.element()
    last.count()

    wait = transport.sent("waitForSelector")[-1]
    assert wait["type"] == "xpath"
    assert wait["selector"].startswith("((//tr)[contains(translate(normalize-space(.)")
    assert wait["selector"].endswith(", 'ab')])[2]")
    assert transport.sent("queryXPath")[-1]["xpath"] == "(//tr)[last()]"


def test_visible_filter_and_css_refinements_run_in_python(
    ctx: BrowserContext, transport: FakeTransport
) -> None:
    transport.handlers["querySelectorAll"] = [
        element("a", "Alpha"),
        element("b", "beta", rect={"x": 0, "y": 0, "width": 0, "height": 0}),
        element("c", "Gamma BETA"),
    ]
    transport.handlers["queryXPath"] = transport.handlers["querySelectorAll"]
    items = locator(ctx, "li")

    assert [el.element_id for el in items.filter(has_text="BETA").all()] == ["b", "c"]
    assert [el.element_id for el in items.filter(visible=True).all()] == ["a", "c"]
    assert [el.element_id for el in items.filter(visible=False).all()] == ["b"]
    assert [el.element_id for el in items.nth(1).filter(has_text="gamma").all()] == []
    assert [el.element_id for el in items.filter(has_text="ta").nth(1).all()] == ["c"]
    assert items.last.all()[0].element_id == "c"
    assert items.count() == 3 and items.exists()
    # XPath stops folding at the first visible filter.
    locator(ctx, "//li").filter(visible=True).nth(1).all()
    assert transport.sent("queryXPath")[-1] == {"xpath": "//li"}


def test_element_waits_in_the_extension_when_no_python_refinement(
    ctx: BrowserContext, transport: FakeTransport
) -> None:
    found = locator(ctx, "button").element(timeout=2)

    assert found.element_id == "doc1.1"
    assert transport.sent("waitForSelector") == [
        {"selector": "button", "type": "css", "timeoutMs": 2000}
    ]


def test_css_nth_waits_for_enough_matches_then_picks(
    ctx: BrowserContext, transport: FakeTransport
) -> None:
    transport.handlers["querySelectorAll"] = [element("a"), element("b"), element("c")]

    found = locator(ctx, "li").nth(2).element()
    last = locator(ctx, "li").last.element()

    assert (found.element_id, last.element_id) == ("c", "c")
    waits = transport.sent("waitForSelector")
    assert waits[0]["conditions"] == [{"type": "count_gt", "value": 2}]
    assert "conditions" not in waits[1]
    assert waits[0]["timeoutMs"] == 10000


def test_python_side_waits_poll_until_the_timeout(
    ctx: BrowserContext, transport: FakeTransport, clock: list[float]
) -> None:
    transport.handlers["querySelectorAll"] = [element("a", "Alpha")]
    start = clock[0]

    with pytest.raises(WaitTimeoutError) as info:
        locator(ctx, "li").filter(has_text="zeta").element(timeout=1)

    assert clock[0] - start == pytest.approx(1.0)
    assert len(transport.sent("querySelectorAll")) == 11
    assert "ElementLocator(css='li', has_text='zeta')" in str(info.value)
    assert info.value.hint is not None and "no element matches" in info.value.hint


def test_wait_for_hidden_and_detached_send_conditions(
    ctx: BrowserContext, transport: FakeTransport
) -> None:
    transport.handlers["waitForSelector"] = {
        "element": None,
        "matched": True,
        "count": 0,
    }
    target = locator(ctx, "#x")

    assert target.wait_for("hidden") is None
    assert target.wait_for("detached") is None
    hidden, detached = transport.sent("waitForSelector")
    assert hidden["conditions"] == [
        {
            "type": "any_of",
            "conditions": [[{"type": "detached"}], [{"type": "hidden"}]],
        }
    ]
    assert detached["conditions"] == [{"type": "detached"}]
    with pytest.raises(InvalidArgumentError):
        target.wait_for("gone")


def test_is_visible_is_a_single_check(
    ctx: BrowserContext, transport: FakeTransport
) -> None:
    target = locator(ctx, "#x")

    assert target.is_visible() is True
    transport.handlers["waitForSelector"] = WaitTimeoutError("timed out")
    assert target.is_visible() is False
    assert transport.sent("waitForSelector")[0] == {
        "selector": "#x",
        "type": "css",
        "timeoutMs": 0,
        "state": "visible",
    }


def test_wait_timeouts_name_the_locator(
    ctx: BrowserContext, transport: FakeTransport
) -> None:
    transport.handlers["waitForSelector"] = WaitTimeoutError("timed out")
    transport.handlers["pageInfo"] = {"url": "https://a.test/", "title": "A"}
    transport.handlers["queryXPath"] = []

    with pytest.raises(WaitTimeoutError) as info:
        get_by_role(ctx, "button", name="Save").element(timeout=1)

    assert str(info.value).startswith("ElementLocator(role='button', name='Save'): ")
    assert info.value.hint is not None and "no element matches" in info.value.hint


# -- actions and the stale-retry rule -------------------------------------------------


def test_actions_refuse_headless_before_any_command(
    transport: FakeTransport,
) -> None:
    headless = BrowserContext(Bridge(transport=transport), headless=True)
    save = locator(headless, "#save")

    for action in (
        save.click,
        save.hover,
        lambda: save.type("x"),
        lambda: save.drag_to("#other"),
    ):
        with pytest.raises(NativeInputError) as info:
            action()
        assert info.value.hint == HEADLESS_HINT
    assert transport.calls == []


def stale_once(element_id: str) -> Callable[[Any], Any]:
    """scrollIntoView: stale for ``element_id``, a fresh element otherwise."""

    def handler(params: Any) -> Any:
        if params["elementId"] == element_id:
            raise StaleElementError("element is gone")
        return element(params["elementId"])

    return handler


def test_click_stale_before_the_button_is_retried_once(
    ctx: BrowserContext, transport: FakeTransport, interactor: FakeInteractor
) -> None:
    transport.handlers["waitForSelector"] = Steps(element("old"), element("new"))
    transport.handlers["scrollIntoView"] = stale_once("old")

    clicked = locator(ctx, "#save").click(human=False)

    assert clicked.element_id == "new"
    assert len(transport.sent("waitForSelector")) == 2
    assert transport.sent("waitForSelector")[0]["state"] == "clickable"
    assert interactor.os_events() == [("press", "left", 1)]


@pytest.mark.parametrize(
    ("click_type", "press"),
    [
        (ClickType.DOUBLE, ("press", "left", 2)),
        (ClickType.RIGHT, ("press", "right", 1)),
    ],
)
def test_other_clicks_are_retried_too(
    ctx: BrowserContext,
    transport: FakeTransport,
    interactor: FakeInteractor,
    click_type: ClickType,
    press: tuple[Any, ...],
) -> None:
    transport.handlers["waitForSelector"] = Steps(element("old"), element("new"))
    transport.handlers["scrollIntoView"] = stale_once("old")

    locator(ctx, "#save").click(human=False, click_type=click_type)

    assert interactor.os_events() == [press]


def test_click_is_retried_only_once(
    ctx: BrowserContext, transport: FakeTransport, interactor: FakeInteractor
) -> None:
    transport.handlers["scrollIntoView"] = StaleElementError("element is gone")

    with pytest.raises(StaleElementError):
        locator(ctx, "#save").click(human=False)

    assert len(transport.sent("waitForSelector")) == 2
    assert interactor.os_events() == []


def test_hover_and_scroll_are_retried(
    ctx: BrowserContext, transport: FakeTransport, interactor: FakeInteractor
) -> None:
    transport.handlers["waitForSelector"] = Steps(
        element("old"), element("new"), element("old2"), element("new2")
    )
    transport.handlers["scrollIntoView"] = Steps(
        StaleElementError("gone"),
        element("new"),
        StaleElementError("gone"),
        element("new2"),
    )

    assert locator(ctx, "#a").hover(human=False).element_id == "new"
    assert locator(ctx, "#a").scroll_into_view().element_id == "new2"
    assert interactor.os_events() == []


def test_type_stale_after_the_press_is_not_retried(
    ctx: BrowserContext, transport: FakeTransport, interactor: FakeInteractor
) -> None:
    # Element.type clicks first; focusState then finds the element stale.
    transport.handlers["focusState"] = StaleElementError("element is gone")

    with pytest.raises(StaleElementError):
        locator(ctx, "#field").type("hello", human=False)

    assert interactor.os_events() == [("press", "left", 1)]
    assert len(transport.sent("waitForSelector")) == 1


def test_type_is_never_retried_even_before_the_press(
    ctx: BrowserContext, transport: FakeTransport, interactor: FakeInteractor
) -> None:
    # From outside, a stale error before type's press cannot be told apart
    # from one after it, so type is never retried.
    transport.handlers["scrollIntoView"] = StaleElementError("element is gone")

    with pytest.raises(StaleElementError):
        locator(ctx, "#field").type("hello", human=False)

    assert interactor.os_events() == []
    assert len(transport.sent("waitForSelector")) == 1


def test_drag_is_never_retried(
    ctx: BrowserContext, transport: FakeTransport, interactor: FakeInteractor
) -> None:
    transport.handlers["scrollIntoView"] = StaleElementError("element is gone")

    with pytest.raises(StaleElementError):
        locator(ctx, "#a").drag_to(locator(ctx, "#b"), human=False)

    # One wait for the target, one for the source; no second attempt.
    assert len(transport.sent("waitForSelector")) == 2
    assert interactor.os_events() == []


def test_type_waits_for_visible_and_types_through_element(
    ctx: BrowserContext, transport: FakeTransport, interactor: FakeInteractor
) -> None:
    # Element.type reads the value before typing and until the text arrived.
    transport.handlers["describeElement"] = Steps(
        element("doc1.1", value=""), element("doc1.1", value="hi")
    )

    typed = locator(ctx, "#field").type("hi", human=False)

    assert typed.element_id == "doc1.1"
    assert transport.sent("waitForSelector")[0]["state"] == "visible"
    assert interactor.os_events() == [("press", "left", 1), ("type", "hi")]


def test_reads_wait_for_presence(ctx: BrowserContext, transport: FakeTransport) -> None:
    transport.handlers["getAttribute"] = {"value": "idle"}

    assert locator(ctx, "#s").text() == "Go"
    assert locator(ctx, "#s").get_attribute("data-state", timeout=1) == "idle"
    assert all("state" not in wait for wait in transport.sent("waitForSelector"))
    assert isinstance(locator(ctx, "#s"), ElementLocator)


# -- navigation, one deadline, truncated text ------------------------------------------


def advancing(clock: list[float], seconds: float, reply: Any) -> Callable[[Any], Any]:
    """A handler that takes ``seconds`` on the fake clock, then answers ``reply``."""

    def handler(params: Any) -> Any:
        clock[0] = round(clock[0] + seconds, 9)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    return handler


def test_transient_errors_are_document_swaps_and_early_bridge_timeouts() -> None:
    swap = ContentUnavailableError("swap")
    late = BridgeTimeoutError("no reply")

    assert is_transient(swap, time_left=False)
    assert is_transient(StaleElementError("gone"), time_left=True)
    assert is_transient(late, time_left=True)
    assert not is_transient(late, time_left=False)
    assert not is_transient(WaitTimeoutError("timed out"), time_left=True)


def test_click_waits_through_a_navigation(
    ctx: BrowserContext,
    transport: FakeTransport,
    interactor: FakeInteractor,
    clock: list[float],
) -> None:
    transport.handlers["waitForSelector"] = Steps(
        ContentUnavailableError("content script in tab 7 did not answer"),
        element("after"),
    )
    transport.handlers["scrollIntoView"] = element("after")

    clicked = locator(ctx, "#save").click(human=False, timeout=5)

    assert clicked.element_id == "after"
    assert clock[0] == pytest.approx(100.1)
    assert [wait["timeoutMs"] for wait in transport.sent("waitForSelector")] == [
        5000,
        4900,
    ]
    assert interactor.os_events() == [("press", "left", 1)]


def test_a_wait_that_never_settles_times_out_from_the_last_swap(
    ctx: BrowserContext, transport: FakeTransport, clock: list[float]
) -> None:
    transport.handlers["waitForSelector"] = ContentUnavailableError("swap")

    with pytest.raises(WaitTimeoutError) as info:
        locator(ctx, "#save").element(timeout=1)

    assert isinstance(info.value.__cause__, ContentUnavailableError)
    assert clock[0] == pytest.approx(101.0)


def test_a_bridge_timeout_after_the_deadline_is_raised(
    ctx: BrowserContext, transport: FakeTransport, clock: list[float]
) -> None:
    transport.handlers["waitForSelector"] = advancing(
        clock, 7.0, BridgeTimeoutError("no reply")
    )

    with pytest.raises(BridgeTimeoutError):
        locator(ctx, "#save").element(timeout=2)


def test_click_retry_shares_one_deadline(
    ctx: BrowserContext,
    transport: FakeTransport,
    interactor: FakeInteractor,
    clock: list[float],
) -> None:
    transport.handlers["waitForSelector"] = Steps(
        advancing(clock, 3.0, element("old")), element("new")
    )
    transport.handlers["scrollIntoView"] = stale_once("old")

    locator(ctx, "#save").click(human=False, timeout=5)

    assert [wait["timeoutMs"] for wait in transport.sent("waitForSelector")] == [
        5000,
        2000,
    ]


def test_drag_waits_share_one_deadline(
    ctx: BrowserContext, transport: FakeTransport, clock: list[float]
) -> None:
    transport.handlers["waitForSelector"] = advancing(clock, 3.0, element("doc1.1"))
    transport.handlers["scrollIntoView"] = StaleElementError("gone")

    with pytest.raises(StaleElementError):
        locator(ctx, "#a").drag_to("#b", human=False, timeout=5)

    assert [wait["timeoutMs"] for wait in transport.sent("waitForSelector")] == [
        5000,
        2000,
    ]


def test_css_nth_polls_when_the_matches_shrank_after_the_wait(
    ctx: BrowserContext, transport: FakeTransport, clock: list[float]
) -> None:
    transport.handlers["querySelectorAll"] = Steps(
        [element("a")], [element("a"), element("b"), element("c")]
    )

    found = locator(ctx, "li").nth(2).element(timeout=5)

    assert found.element_id == "c"
    assert clock[0] == pytest.approx(100.1)
    assert len(transport.sent("waitForSelector")) == 2


def test_has_text_on_truncated_css_text_matches_only_the_prefix(
    ctx: BrowserContext, transport: FakeTransport
) -> None:
    long_text = "needle " + "x" * TEXT_CAP
    transport.handlers["querySelectorAll"] = [element("p", long_text[:TEXT_CAP])]
    paragraphs = locator(ctx, "p")

    assert TEXT_CAP == 500
    assert paragraphs.filter(has_text="needle").count() == 1
    # The rest of the text is unknown: a needle beyond the cut does not match.
    assert paragraphs.filter(has_text="tail").count() == 0
