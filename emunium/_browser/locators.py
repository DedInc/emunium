"""Live, lazy element locators (``docs/architecture/dx-api.md`` section 4).

An ``ElementLocator`` is an immutable query description bound to a
``BrowserContext``; every call queries the page again. Role, label,
placeholder and test-id locators compile to XPath 1.0 here, in Python, so the
extension's ``queryXPath`` / ``waitForSelector`` run them unchanged.

Refinements apply in the order they were chained (``filter`` then ``nth`` is
"the n-th match that has the text"; ``nth`` then ``filter`` is "the n-th
match, if it has the text"):

- XPath-family locators (``xpath=``, role, label, placeholder, test id) fold
  ``has_text`` and ``nth`` into the expression (``(expr)[pred]``,
  ``(expr)[n]``, ``(expr)[last()]``), so the extension waits on exactly the
  element the locator means.
- ``css=`` and ``text=`` locators cannot be rewritten as XPath, and
  ``visible`` has no XPath form: from the first such refinement on, the
  remaining refinements run in Python on the queried ``Element`` handles
  (``has_text`` on ``Element.text``, ``visible`` on ``Element.visible``,
  which reflects the rendered box). Waits on such a locator poll every
  100 ms; a bare ``nth`` on CSS/text waits in the extension for enough
  matches (``count_gt``) and then picks the match.

``has_text`` and ``exact=False`` matching is a case-insensitive substring
test after whitespace normalisation; case folding maps ASCII ``A-Z`` and the
Cyrillic capitals U+0400-U+042F (``Ѐ-Џ``, ``А-Я``: Ё, Є, І, Ї, Ў, Ђ, ...) to
lower case (``translate`` in XPath, the same table in Python); other letters
(``Ґ``, Latin with diacritics) are compared as they are.

``has_text`` on XPath-family locators tests the element's full string value
(``textContent``); on CSS and ``text=`` locators it tests the rendered text:
``Element.text``, or, when that 500-char preview is at ``TEXT_CAP``, the full
text from ``Element.inner_text`` (``full_text``, dx-api.md section 7).

Every wait uses one deadline. A document swap during a wait or a query
(``ContentUnavailableError``, a stale element, a bridge timeout before the
deadline) is retried with the time left.

Actions run through ``Element`` after waiting for the actionable state. A
``StaleElementError`` is retried once, with a freshly resolved element, only
where it provably came before any OS button or key event (see
``_RETRY_SAFE``).
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, TypeVar

from emunium._bridge.facade import FULL_TEXT_CAP
from emunium._browser import dom
from emunium._browser.context import BrowserContext
from emunium._browser.page import TRANSIENT_IDLE_ERRORS
from emunium._standalone.config import ClickType
from emunium.element import Element, ElementQuery
from emunium.errors import (
    BridgeTimeoutError,
    ElementNotFoundError,
    EmuniumError,
    InvalidArgumentError,
    StaleElementError,
    WaitTimeoutError,
)
from emunium.locator import ROLE_SELECTOR_MAP
from emunium.wait import Wait

__all__ = [
    "DEFAULT_TIMEOUT",
    "FULL_TEXT_CAP",
    "TEXT_CAP",
    "ElementLocator",
    "full_text",
    "get_by_label",
    "get_by_placeholder",
    "get_by_role",
    "get_by_test_id",
    "is_transient",
    "is_truncated",
    "locator",
    "role_xpath",
    "text_matches",
    "xpath_literal",
]

logger = logging.getLogger("emunium.browser")

T = TypeVar("T")

# Seconds a resolving call waits when ``timeout`` is None.
DEFAULT_TIMEOUT = 10.0
# Poll interval (s) of waits that filter in Python.
POLL_S = 0.1

_monotonic = time.monotonic
_sleep = time.sleep

# ``Element.text`` length cap: content_shared.js ``getElementText(element,
# maxLength = 500)``. A preview this long may be a prefix: ``full_text`` then
# reads the whole text with ``elementText``.
TEXT_CAP = 500
# Errors of a page between documents (and a stale handle): retried in waits.
_TRANSIENT: tuple[type[EmuniumError], ...] = (*TRANSIENT_IDLE_ERRORS, StaleElementError)
# Everything a wait catches to decide with ``is_transient``.
RETRYABLE: tuple[type[EmuniumError], ...] = (*_TRANSIENT, BridgeTimeoutError)

# Case folding shared by XPath ``translate`` and the Python-side filters:
# ASCII A-Z and Cyrillic U+0400-U+040F -> U+0450-U+045F, U+0410-U+042F ->
# U+0430-U+044F.
_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ" + "".join(map(chr, range(0x0400, 0x0430)))
_LOWER = (
    "abcdefghijklmnopqrstuvwxyz"
    + "".join(map(chr, range(0x0450, 0x0460)))
    + "".join(map(chr, range(0x0430, 0x0450)))
)
_FOLD = str.maketrans(_UPPER, _LOWER)
# XPath normalize-space() strips and collapses only these four characters.
_XPATH_SPACE = re.compile(r"[ \t\r\n]+")
_ATTRIBUTE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")
# Attribute values HTML matches case-insensitively in selectors (``type``).
_FOLDED_ATTRIBUTES = frozenset({"type"})
_INPUT_BUTTON_TYPES = ("button", "submit", "reset", "image")
_CLICK_TYPES = (ClickType.LEFT, ClickType.DOUBLE, ClickType.RIGHT, ClickType.MIDDLE)
_WAIT_STATES = ("visible", "attached", "hidden", "detached")


# -- XPath compiler -------------------------------------------------------------


def xpath_literal(value: str) -> str:
    """``value`` as an XPath 1.0 string literal.

    ``'x'`` when it has no apostrophe, ``"x"`` when it has no double quote,
    else ``concat()`` of apostrophe-free pieces and ``"'"``.
    """
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    pieces = value.split("'")
    parts: list[str] = []
    for index, piece in enumerate(pieces):
        if index:
            parts.append('"\'"')
        if piece:
            parts.append(f"'{piece}'")
    return f"concat({', '.join(parts)})"


def normalize_space(text: str) -> str:
    """XPath ``normalize-space()``: strip and collapse space, tab, CR and LF."""
    return _XPATH_SPACE.sub(" ", text).strip(" ")


def fold(text: str) -> str:
    """Lower-case ASCII and Cyrillic letters (the XPath ``translate`` table)."""
    return text.translate(_FOLD)


def _translate(expr: str) -> str:
    return f"translate({expr}, '{_UPPER}', '{_LOWER}')"


def _ascii_lower(expr: str) -> str:
    """ASCII-only folding for keyword attributes such as ``type``."""
    return f"translate({expr}, '{_UPPER[:26]}', '{_LOWER[:26]}')"


def text_matches(expr: str, text: str, *, exact: bool) -> str:
    """XPath test of ``normalize-space(expr)`` against ``text``.

    ``exact``: equality after whitespace normalisation; otherwise a
    case-insensitive substring test.
    """
    needle = normalize_space(text)
    if exact:
        return f"normalize-space({expr}) = {xpath_literal(needle)}"
    return (
        f"contains({_translate(f'normalize-space({expr})')}, "
        f"{xpath_literal(fold(needle))})"
    )


def _attribute_matches(name: str, text: str, *, exact: bool) -> str:
    return f"(@{name} and {text_matches('@' + name, text, exact=exact)})"


_COMPOUND = re.compile(r"^(?P<tag>[a-zA-Z][a-zA-Z0-9-]*)?(?P<rest>.*)$")
_CSS_PART = re.compile(
    r'(?P<negated>:not\()?\[(?P<name>[a-zA-Z][a-zA-Z0-9-]*)(?:="(?P<value>[^"]*)")?\]\)?'
)


def _css_attribute(name: str, value: str | None) -> str:
    if value is None:
        return f"@{name}"
    subject = _ascii_lower(f"@{name}") if name in _FOLDED_ATTRIBUTES else f"@{name}"
    return f"{subject} = {xpath_literal(value)}"


def css_to_xpath_predicate(selector: str) -> str:
    """The XPath predicate of a CSS selector list of the ``ROLE_SELECTOR_MAP`` shape.

    Supports ``tag``, ``[attr]``, ``[attr="value"]`` and ``:not([...])``
    compounds joined by commas; anything else raises ``InvalidArgumentError``.
    """
    alternatives: list[str] = []
    for compound in selector.split(","):
        compound = compound.strip()
        match = _COMPOUND.match(compound)
        if not compound or match is None:
            raise InvalidArgumentError(f"unsupported role selector {selector!r}")
        tests: list[str] = []
        if match["tag"]:
            tests.append(f"self::{match['tag'].lower()}")
        rest = match["rest"]
        position = 0
        while position < len(rest):
            part = _CSS_PART.match(rest, position)
            negated = part is not None and part["negated"] is not None
            if part is None or negated != part.group(0).endswith(")"):
                raise InvalidArgumentError(f"unsupported role selector {selector!r}")
            test = _css_attribute(part["name"], part["value"])
            tests.append(f"not({test})" if negated else test)
            position = part.end()
        if not tests:
            raise InvalidArgumentError(f"unsupported role selector {selector!r}")
        alternatives.append("(" + " and ".join(tests) + ")")
    return " or ".join(alternatives)


def accessible_name_matches(name: str, *, exact: bool) -> str:
    """XPath predicate: the element's accessible name matches ``name``.

    The name is the normalised text, ``aria-label``, ``title``, ``alt``, the
    ``value`` of input buttons, or the text of a ``<label>`` naming the
    element (``for`` = its ``id``, or a label around it).
    """
    label = text_matches(".", name, exact=exact)
    button_types = " or ".join(
        f"{_ascii_lower('@type')} = '{kind}'" for kind in _INPUT_BUTTON_TYPES
    )
    input_button = (
        f"(self::input and ({button_types}) and "
        f"{_attribute_matches('value', name, exact=exact)})"
    )
    sources = [
        text_matches(".", name, exact=exact),
        _attribute_matches("aria-label", name, exact=exact),
        _attribute_matches("title", name, exact=exact),
        _attribute_matches("alt", name, exact=exact),
        input_button,
        f"@id = //label[{label}]/@for",
        f"ancestor::label[{label}]",
    ]
    return " or ".join(sources)


def role_xpath(role: str, name: str | None = None, *, exact: bool = False) -> str:
    """``//*`` with ``role``: implicit (``ROLE_SELECTOR_MAP``) or ``@role``."""
    explicit = f"@role = {xpath_literal(role)}"
    implicit = ROLE_SELECTOR_MAP.get(role)
    predicate = (
        explicit
        if implicit is None
        else f"{css_to_xpath_predicate(implicit)} or {explicit}"
    )
    if name is None:
        return f"//*[{predicate}]"
    return f"//*[({predicate}) and ({accessible_name_matches(name, exact=exact)})]"


def label_xpath(text: str, *, exact: bool = False) -> str:
    """Controls a ``<label>`` matching ``text`` names, or with a matching ``aria-label``."""
    label = text_matches(".", text, exact=exact)
    control = "self::input or self::select or self::textarea or @contenteditable"
    labelled = f"@id = //label[{label}]/@for or ancestor::label[{label}]"
    aria = _attribute_matches("aria-label", text, exact=exact)
    return f"//*[(({control}) and ({labelled})) or {aria}]"


def placeholder_xpath(text: str, *, exact: bool = False) -> str:
    return f"//*[{_attribute_matches('placeholder', text, exact=exact)}]"


def test_id_xpath(test_id: str, attribute: str = "data-testid") -> str:
    if not isinstance(attribute, str) or not _ATTRIBUTE_NAME.match(attribute):
        raise InvalidArgumentError(
            f"test id attribute must be an attribute name, got {attribute!r}"
        )
    return f"//*[@{attribute} = {xpath_literal(test_id)}]"


# -- query description ----------------------------------------------------------

Step = tuple[str, Any]


@dataclass(frozen=True)
class _Query:
    """``kind`` css|xpath|text, the selector, how it was built (for ``repr``)
    and the refinements in chaining order (``has_text``, ``visible``, ``nth``;
    ``nth`` -1 is ``last``)."""

    kind: str
    selector: str
    origin: tuple[tuple[str, Any], ...]
    steps: tuple[Step, ...] = ()

    def plan(self) -> tuple[str, str, tuple[Step, ...]]:
        """(kind, selector for the extension, refinements left for Python)."""
        if self.kind != "xpath":
            return self.kind, self.selector, self.steps
        expr = self.selector
        for index, (name, value) in enumerate(self.steps):
            if name == "has_text":
                expr = f"({expr})[{text_matches('.', value, exact=False)}]"
            elif name == "nth":
                expr = f"({expr})[{'last()' if value < 0 else value + 1}]"
            else:
                return "xpath", expr, self.steps[index:]
        return "xpath", expr, ()


def _text(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise InvalidArgumentError(f"{what} must be a str, got {value!r}")
    return value


def _flag(value: object, what: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidArgumentError(f"{what} must be a bool, got {value!r}")
    return value


def _timeout(timeout: float | None) -> float:
    if timeout is None:
        return DEFAULT_TIMEOUT
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise InvalidArgumentError(f"timeout must be a number, got {timeout!r}")
    if timeout < 0:
        raise InvalidArgumentError(f"timeout must be >= 0, got {timeout!r}")
    return float(timeout)


@dataclass(frozen=True)
class _Budget:
    """One deadline (monotonic s) for a whole call; ``total`` for messages."""

    total: float
    deadline: float

    @classmethod
    def start(cls, timeout: float | None) -> _Budget:
        total = _timeout(timeout)
        return cls(total, _monotonic() + total)

    def remaining(self) -> float:
        return max(0.0, self.deadline - _monotonic())


def is_transient(exc: BaseException, *, time_left: bool) -> bool:
    """A document swap or stale handle, or a bridge timeout while time is left."""
    if isinstance(exc, BridgeTimeoutError):
        return time_left
    return isinstance(exc, _TRANSIENT)


def full_text(el: Element) -> str:
    """The rendered text of ``el``: its ``text`` preview when shorter than
    ``TEXT_CAP``, else the full text read now (``Element.inner_text``)."""
    preview = el.text
    if len(preview) < TEXT_CAP:
        return preview
    return el.inner_text()


def is_truncated(text: str) -> bool:
    """True when ``text`` (a ``full_text``) may be cut at ``FULL_TEXT_CAP``."""
    return len(text) >= FULL_TEXT_CAP


def _has_text(el: Element, needle: str) -> bool:
    """``needle`` (folded, normalised) is in the full text of ``el``; an element
    that went stale before its full text was read does not match."""
    try:
        text = full_text(el)
    except StaleElementError:
        return False
    return needle in fold(normalize_space(text))


def _apply(step: Step, elements: list[Element]) -> list[Element]:
    name, value = step
    if name == "has_text":
        needle = fold(normalize_space(value))
        return [el for el in elements if _has_text(el, needle)]
    if name == "visible":
        return [el for el in elements if el.visible is value]
    if value < 0:
        return elements[-1:]
    return elements[value : value + 1]


def _clickable(el: Element) -> bool:
    return el.visible and "disabled" not in el.attrs


# Retry proof (element.py). ``Element._click`` ends with
# ``interactor.press(...)``: every bridge call that can raise
# ``StaleElementError`` (``_tab``, ``activateTab``, ``describeElement``,
# ``scrollIntoView``, ``pointerProbe``) runs before it in ``_point_at`` /
# ``_hold``, and the interactor sends OS events without touching the bridge, so
# a ``StaleElementError`` out of click/double_click/right_click/middle_click
# means no button event was sent. ``Element.hover`` and ``scroll_into_view``
# send no button or key event at all, and reads only read. ``Element.type``
# presses the button first and then calls ``focusState`` / ``focus`` /
# ``describeElement`` (any of which can be stale) before and between key
# chunks; ``Element.drag_to`` probes the target after ``button_down``. Their
# StaleElementError can follow an OS event, so they are never retried.
_RETRY_SAFE = frozenset(
    {"click", "double_click", "right_click", "hover", "scroll_into_view", "read"}
)


class ElementLocator:
    """A lazy element query; immutable, every call queries the page again.

    Built by ``Browser.locator`` / ``get_by_role`` / ``get_by_label`` /
    ``get_by_placeholder`` / ``get_by_test_id``; refined with ``nth``,
    ``first``, ``last`` and ``filter``.
    """

    __slots__ = ("_context", "_query")

    _context: BrowserContext
    _query: _Query

    def __init__(self, context: BrowserContext, query: _Query) -> None:
        object.__setattr__(self, "_context", context)
        object.__setattr__(self, "_query", query)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"ElementLocator is immutable; cannot set {name!r}")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"ElementLocator is immutable; cannot delete {name!r}")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ElementLocator):
            return NotImplemented
        return self._context is other._context and self._query == other._query

    def __hash__(self) -> int:
        return hash((id(self._context), self._query))

    def __repr__(self) -> str:
        parts = [f"{key}={value!r}" for key, value in self._query.origin]
        for name, value in self._query.steps:
            if name == "nth" and value < 0:
                parts.append("last=True")
            else:
                parts.append(f"{name}={value!r}")
        return f"ElementLocator({', '.join(parts)})"

    @property
    def context(self) -> BrowserContext:
        """The browser context every query and action runs in."""
        return self._context

    # -- refinement ---------------------------------------------------------

    def _refined(self, step: Step) -> ElementLocator:
        query = replace(self._query, steps=(*self._query.steps, step))
        return ElementLocator(self._context, query)

    def nth(self, index: int) -> ElementLocator:
        """The ``index``-th (0-based, document order) match."""
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise InvalidArgumentError(
                f"nth() expects a non-negative int, got {index!r}"
            )
        return self._refined(("nth", index))

    @property
    def first(self) -> ElementLocator:
        """The first match (``nth(0)``)."""
        return self.nth(0)

    @property
    def last(self) -> ElementLocator:
        """The last match at the time of each call."""
        return self._refined(("nth", -1))

    def filter(
        self, *, has_text: str | None = None, visible: bool | None = None
    ) -> ElementLocator:
        """Matches whose text contains ``has_text`` (case-insensitive) and/or
        whose rendered box is (``True``) or is not (``False``) visible.

        On CSS and ``text=`` locators ``has_text`` tests the full rendered
        text (read with ``Element.inner_text`` when the ``Element.text``
        preview is at ``TEXT_CAP``); XPath-based locators test the full
        ``textContent``.
        """
        result = self
        if has_text is not None:
            result = result._refined(("has_text", _text(has_text, "has_text")))
        if visible is not None:
            result = result._refined(("visible", _flag(visible, "visible")))
        return result

    # -- queries ------------------------------------------------------------

    def _query_all(self, kind: str, selector: str) -> list[Element]:
        context = self._context
        if kind == "xpath":
            matches = context.bridge.query_xpath(selector)
            return [
                Element.from_data(
                    context, data, query=ElementQuery(selector, index, "xpath")
                )
                for index, data in enumerate(matches)
            ]
        if kind == "text":
            return dom.get_by_text(context, selector)
        return dom.query_selector_all(context, selector)

    def all(self) -> list[Element]:
        """Every element the locator matches now (no wait)."""
        kind, selector, steps = self._query.plan()
        elements = self._query_all(kind, selector)
        for step in steps:
            elements = _apply(step, elements)
        return elements

    def count(self) -> int:
        """How many elements match now (no wait)."""
        return len(self.all())

    def exists(self) -> bool:
        """True when at least one element matches now (no wait)."""
        return bool(self.all())

    def is_visible(self) -> bool:
        """True when the target is visible now (no wait); False when absent."""
        kind, selector, steps = self._query.plan()
        if not steps:
            try:
                self._context.bridge.wait_for_selector(
                    selector, type=kind, timeout=0, state="visible"
                )
            except WaitTimeoutError:
                return False
            return True
        found = self.all()
        return bool(found) and found[0].visible

    # -- waits --------------------------------------------------------------

    def _extension_wait(
        self,
        kind: str,
        selector: str,
        timeout: float,
        *,
        state: str | None = None,
        conditions: list[dict[str, Any]] | None = None,
    ) -> Element | None:
        try:
            if kind == "xpath":
                return dom.wait_for_xpath(
                    self._context,
                    selector,
                    timeout,
                    strategy=state,
                    condition=conditions,
                )
            if kind == "text":
                return dom.wait_for_text(
                    self._context,
                    selector,
                    timeout,
                    strategy=state,
                    condition=conditions,
                )
            return dom.wait_for_element(
                self._context, selector, timeout, state=state, conditions=conditions
            )
        except WaitTimeoutError as exc:
            raise WaitTimeoutError(
                f"{self!r}: {exc.message}",
                code=exc.code,
                method=exc.method,
                hint=exc.hint,
            ) from exc

    def _server_wait(
        self,
        timeout: float,
        *,
        state: str | None = None,
        conditions: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Wait in the extension when it can see exactly this locator's matches.

        Returns False (without waiting) when refinements must run in Python;
        raises ``WaitTimeoutError`` when the wait times out.
        """
        kind, selector, steps = self._query.plan()
        if steps:
            return False
        self._extension_wait(
            kind, selector, timeout, state=state, conditions=conditions
        )
        return True

    def _until(
        self,
        budget: _Budget,
        attempt: Callable[[float], T | None],
        seen: list[int],
    ) -> T:
        """Run ``attempt(time_left)`` until it returns a value.

        ``None`` and transient errors (``is_transient``) mean "not yet": wait
        ``POLL_S`` and try again with the time left. An extension wait's
        ``WaitTimeoutError`` is final (it had all the time left). At the
        deadline the ``WaitTimeoutError`` names the locator and chains the last
        transient error. ``seen`` holds the last match count, for the hint.
        """
        last_error: EmuniumError | None = None
        while True:
            try:
                result = attempt(budget.remaining())
            except RETRYABLE as exc:
                if not is_transient(exc, time_left=budget.remaining() > 0):
                    raise
                logger.info("%r: page changing (%s); retrying", self, exc)
                last_error = exc
                result = None
            if result is not None:
                return result
            remaining = budget.remaining()
            if remaining <= 0:
                raise self._wait_timeout(budget.total, seen) from last_error
            _sleep(min(POLL_S, remaining))

    def _wait_timeout(self, timeout: float, seen: list[int]) -> WaitTimeoutError:
        if not seen:
            hint = None
        elif seen[-1]:
            hint = f"{seen[-1]} element(s) match but none satisfied the wait"
        else:
            hint = f"no element matches {self!r} on this page"
        return WaitTimeoutError(
            f"Element not found after {timeout}s: {self!r}",
            code="wait_timeout",
            method="ElementLocator",
            hint=hint,
        )

    def _matches(self, seen: list[int]) -> list[Element]:
        found = self.all()
        seen.append(len(found))
        return found

    def _resolve(self, state: str, budget: _Budget) -> Element:
        """The target once it is ``attached``, ``visible`` or ``clickable``."""
        kind, selector, steps = self._query.plan()
        extension_state = None if state == "attached" else state
        seen: list[int] = []
        attempt: Callable[[float], Element | None]
        if not steps:

            def attempt(time_left: float) -> Element | None:
                element = self._extension_wait(
                    kind, selector, time_left, state=extension_state
                )
                if element is None:
                    raise ElementNotFoundError(f"{self!r} matched without an element")
                return element

        elif state == "attached" and len(steps) == 1 and steps[0][0] == "nth":
            # CSS/text nth: wait in the page for enough matches, then pick; a
            # short list (the page changed in between) polls again.
            index = steps[0][1]
            conditions = None if index < 0 else Wait().count_gt(index).to_payload()

            def attempt(time_left: float) -> Element | None:
                self._extension_wait(kind, selector, time_left, conditions=conditions)
                found = self._matches(seen)
                return found[0] if found else None

        else:
            check = _clickable if state == "clickable" else None
            if state == "visible":
                check = _is_rendered

            def attempt(time_left: float) -> Element | None:
                found = self._matches(seen)
                if found and (check is None or check(found[0])):
                    return found[0]
                return None

        return self._until(budget, attempt, seen)

    def element(self, *, timeout: float | None = None) -> Element:
        """Wait for the target to be present (default 10 s) and return it."""
        return self._resolve("attached", _Budget.start(timeout))

    def wait_for(
        self, state: str = "visible", *, timeout: float | None = None
    ) -> Element | None:
        """Wait for ``visible``/``attached`` (returns the element) or
        ``hidden``/``detached`` (returns None). Raises ``WaitTimeoutError``."""
        if state not in _WAIT_STATES:
            raise InvalidArgumentError(
                f"state must be one of {_WAIT_STATES}, got {state!r}"
            )
        budget = _Budget.start(timeout)
        if state in ("visible", "attached"):
            return self._resolve(state, budget)
        gone = Wait().detached()
        condition = gone if state == "detached" else gone | Wait().hidden()
        conditions = condition.to_payload()
        kind, selector, steps = self._query.plan()
        seen: list[int] = []
        attempt: Callable[[float], bool | None]
        if not steps:

            def attempt(time_left: float) -> bool | None:
                self._extension_wait(kind, selector, time_left, conditions=conditions)
                return True

        else:

            def attempt(time_left: float) -> bool | None:
                found = self._matches(seen)
                if not found or (state == "hidden" and not found[0].visible):
                    return True
                return None

        self._until(budget, attempt, seen)
        return None

    # -- actions ------------------------------------------------------------

    def _act(
        self,
        action: str,
        state: str,
        budget: _Budget,
        act: Callable[[Element], T],
    ) -> tuple[Element, T]:
        """Resolve, then ``act``; retried once on a stale element when
        ``action`` is in ``_RETRY_SAFE``. Both resolutions share ``budget``.
        Reads do not mark an action start."""
        marks = action != "read"
        element = self._resolve(state, budget)
        if marks:
            self._context.mark_action()
        try:
            return element, act(element)
        except StaleElementError:
            if action not in _RETRY_SAFE:
                raise
            logger.info("%r went stale before any OS button event; retrying", self)
        element = self._resolve(state, budget)
        if marks:
            self._context.mark_action()
        return element, act(element)

    def click(
        self,
        *,
        human: bool = True,
        click_type: ClickType = ClickType.LEFT,
        offset_x: float | None = None,
        offset_y: float | None = None,
        timeout: float | None = None,
    ) -> Element:
        """Wait until clickable, then a verified native click; returns the element."""
        if click_type not in _CLICK_TYPES:
            raise InvalidArgumentError(f"unsupported click_type {click_type!r}")
        self._context.require_native_input()
        clicks = {
            ClickType.LEFT: Element.click,
            ClickType.DOUBLE: Element.double_click,
            ClickType.RIGHT: Element.right_click,
            ClickType.MIDDLE: Element.middle_click,
        }
        method = clicks[click_type]
        element, _ = self._act(
            "click",
            "clickable",
            _Budget.start(timeout),
            lambda el: method(el, human=human, offset_x=offset_x, offset_y=offset_y),
        )
        return element

    def double_click(
        self,
        *,
        human: bool = True,
        offset_x: float | None = None,
        offset_y: float | None = None,
        timeout: float | None = None,
    ) -> Element:
        """``click`` with ``ClickType.DOUBLE``."""
        return self.click(
            human=human,
            click_type=ClickType.DOUBLE,
            offset_x=offset_x,
            offset_y=offset_y,
            timeout=timeout,
        )

    def right_click(
        self,
        *,
        human: bool = True,
        offset_x: float | None = None,
        offset_y: float | None = None,
        timeout: float | None = None,
    ) -> Element:
        """``click`` with ``ClickType.RIGHT``."""
        return self.click(
            human=human,
            click_type=ClickType.RIGHT,
            offset_x=offset_x,
            offset_y=offset_y,
            timeout=timeout,
        )

    def hover(self, *, human: bool = True, timeout: float | None = None) -> Element:
        """Wait until visible, then rest the cursor on the element."""
        self._context.require_native_input()
        element, _ = self._act(
            "hover",
            "visible",
            _Budget.start(timeout),
            lambda el: el.hover(human=human),
        )
        return element

    def type(
        self,
        text: str,
        *,
        characters_per_minute: int | None = None,
        offset: int | None = None,
        human: bool = True,
        timeout: float | None = None,
    ) -> Element:
        """Wait until visible, then click and type (``Element.type``); never retried."""
        self._context.require_native_input()
        element, _ = self._act(
            "type",
            "visible",
            _Budget.start(timeout),
            lambda el: el.type(
                text,
                characters_per_minute=characters_per_minute,
                offset=offset,
                human=human,
            ),
        )
        return element

    def drag_to(
        self,
        target: ElementLocator | Element | str,
        *,
        human: bool = True,
        timeout: float | None = None,
    ) -> None:
        """Drag this element onto ``target`` (a locator, an element or a
        selector as in ``Browser.locator``); never retried. Both waits share
        one ``timeout``."""
        self._context.require_native_input()
        budget = _Budget.start(timeout)
        if isinstance(target, str):
            target = locator(self._context, target)
        drop = (
            target._resolve("visible", budget)
            if isinstance(target, ElementLocator)
            else target
        )
        self._act(
            "drag_to", "visible", budget, lambda el: el.drag_to(drop, human=human)
        )

    def scroll_into_view(self, *, timeout: float | None = None) -> Element:
        """Wait for presence, then scroll the element into view."""
        element, _ = self._act(
            "scroll_into_view",
            "attached",
            _Budget.start(timeout),
            lambda el: el.scroll_into_view(),
        )
        return element

    # -- reads --------------------------------------------------------------

    def text(self, *, timeout: float | None = None) -> str:
        """The rendered text of the target, once present (capped at ``TEXT_CAP``
        characters by the extension)."""
        return self._resolve("attached", _Budget.start(timeout)).text

    def get_attribute(self, name: str, *, timeout: float | None = None) -> str | None:
        """Attribute ``name`` of the target, once present (None when absent)."""
        _, value = self._act(
            "read",
            "attached",
            _Budget.start(timeout),
            lambda el: el.get_attribute(name),
        )
        return value


def _is_rendered(el: Element) -> bool:
    return el.visible


# -- constructors (used by Browser) ---------------------------------------------

_PREFIXES = (("css=", "css"), ("xpath=", "xpath"), ("text=", "text"))


def locator(context: BrowserContext, selector: str) -> ElementLocator:
    """``css=``/``xpath=``/``text=`` prefixed, ``//`` or ``(//`` XPath, else CSS."""
    _text(selector, "selector")
    kind, body = "css", selector
    for prefix, prefix_kind in _PREFIXES:
        if selector.startswith(prefix):
            kind, body = prefix_kind, selector[len(prefix) :]
            break
    else:
        if selector.startswith(("//", "(//")):
            kind = "xpath"
    if not body.strip():
        raise InvalidArgumentError(f"empty selector {selector!r}")
    return ElementLocator(context, _Query(kind, body, ((kind, body),)))


def _compiled(
    context: BrowserContext, xpath: str, origin: tuple[tuple[str, Any], ...]
) -> ElementLocator:
    return ElementLocator(context, _Query("xpath", xpath, origin))


def get_by_role(
    context: BrowserContext, role: str, *, name: str | None = None, exact: bool = False
) -> ElementLocator:
    _text(role, "role")
    if not role:
        raise InvalidArgumentError("role must not be empty")
    origin: tuple[tuple[str, Any], ...] = (("role", role),)
    if name is not None:
        origin += (("name", _text(name, "name")),)
    if _flag(exact, "exact"):
        origin += (("exact", True),)
    return _compiled(context, role_xpath(role, name, exact=exact), origin)


def get_by_label(
    context: BrowserContext, text: str, *, exact: bool = False
) -> ElementLocator:
    origin: tuple[tuple[str, Any], ...] = (("label", _text(text, "text")),)
    if _flag(exact, "exact"):
        origin += (("exact", True),)
    return _compiled(context, label_xpath(text, exact=exact), origin)


def get_by_placeholder(
    context: BrowserContext, text: str, *, exact: bool = False
) -> ElementLocator:
    origin: tuple[tuple[str, Any], ...] = (("placeholder", _text(text, "text")),)
    if _flag(exact, "exact"):
        origin += (("exact", True),)
    return _compiled(context, placeholder_xpath(text, exact=exact), origin)


def get_by_test_id(
    context: BrowserContext, test_id: str, *, attribute: str = "data-testid"
) -> ElementLocator:
    xpath = test_id_xpath(_text(test_id, "test_id"), attribute)
    origin: tuple[tuple[str, Any], ...] = (("test_id", test_id),)
    if attribute != "data-testid":
        origin += (("attribute", attribute),)
    return _compiled(context, xpath, origin)
