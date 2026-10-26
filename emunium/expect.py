"""Retrying assertions on locators and pages (``docs/architecture/dx-api.md`` section 6).

``expect(locator)`` returns ``LocatorAssertions``, ``expect(browser)``
``PageAssertions``; ``.not_`` negates. Each assertion retries until it holds or
``timeout`` runs out, then raises ``ExpectationError`` (an ``AssertionError``)
naming the target, the expected value and the last actual value.

Where the extension has a matching wait condition (visible, hidden, detached,
presence, ``count_eq``, case-sensitive ``text_contains``, ``has_attribute``)
and the locator needs no Python-side refinement, the assertion waits in the
page; everything else is polled every 100 ms. One deadline covers the whole
assertion: a document swap (``ContentUnavailableError``, a stale element, a
bridge timeout) is retried with the time left, and a timeout always ends in
``ExpectationError`` chained from the last such error.

Text is the full rendered text: ``Element.text``, or, when that 500-char
preview is at ``TEXT_CAP``, ``Element.inner_text`` (dx-api.md section 7), so
long texts compare in full. Only a text at ``FULL_TEXT_CAP`` (1 048 576)
characters, where ``elementText`` itself cuts, is known up to the cut: a
substring found in it counts, anything else is unknown and never passes (the
failure message says the text was truncated).

The module is shadowed by the function: ``emunium.expect`` is ``expect``; use
``importlib.import_module("emunium.expect")`` to reach the module itself.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any, NamedTuple, Union, overload

from emunium._browser.facade import Browser
from emunium._browser.locators import (
    FULL_TEXT_CAP,
    RETRYABLE,
    ElementLocator,
    full_text,
    is_truncated,
    normalize_space,
)
from emunium._browser.page import UrlMatcher, url_matcher
from emunium.element import Element
from emunium.errors import (
    EmuniumError,
    ExpectationError,
    InvalidArgumentError,
    WaitTimeoutError,
)
from emunium.wait import Wait

__all__ = ["LocatorAssertions", "PageAssertions", "expect"]

DEFAULT_TIMEOUT = 5.0
POLL_S = 0.1
# A failure message shows at most this many chars of an actual text; the whole
# value stays on ``ExpectationError.actual``.
ACTUAL_SHOWN = 200
TRUNCATED_NOTE = (
    f"the text was truncated at {FULL_TEXT_CAP} chars and cannot be fully compared"
)

_monotonic = time.monotonic
_sleep = time.sleep

TextOrPattern = Union[str, "re.Pattern[str]"]
_MISSING = "<no element>"


class Reading(NamedTuple):
    """One read of the page: ``holds`` is None when it cannot be decided."""

    holds: bool | None
    actual: Any
    note: str | None = None


def _seconds(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise InvalidArgumentError(f"timeout must be a number, got {timeout!r}")
    if timeout < 0:
        raise InvalidArgumentError(f"timeout must be >= 0, got {timeout!r}")
    return float(timeout)


def _text_or_pattern(value: TextOrPattern, what: str) -> TextOrPattern:
    if isinstance(value, str):
        return value
    if isinstance(value, re.Pattern) and isinstance(value.pattern, str):
        return value
    raise InvalidArgumentError(
        f"{what} must be a str or a str re.Pattern, got {value!r}"
    )


def _matches(
    actual: str, expected: TextOrPattern, *, normalize: bool, ignore_case: bool
) -> bool:
    """``search`` for a pattern, else equality (whitespace-normalised when
    ``normalize``; ``casefold``-ed when ``ignore_case``)."""
    if isinstance(expected, re.Pattern):
        if ignore_case:
            expected = re.compile(expected.pattern, expected.flags | re.IGNORECASE)
        return expected.search(actual) is not None
    want, got = expected, actual
    if normalize:
        want, got = normalize_space(want), normalize_space(got)
    if ignore_case:
        want, got = want.casefold(), got.casefold()
    return want == got


def _options(**flags: bool) -> str:
    return "".join(f", {name}=True" for name, value in flags.items() if value)


def _shown(actual: Any) -> str:
    """``repr(actual)``; a longer text keeps its first ``ACTUAL_SHOWN`` chars
    and says how long it was: ``'abc...' (2031 chars)``."""
    if isinstance(actual, str) and len(actual) > ACTUAL_SHOWN:
        return f"{actual[:ACTUAL_SHOWN] + '...'!r} ({len(actual)} chars)"
    return repr(actual)


class _Assertions:
    """Retry loop shared by locator and page assertions."""

    def __init__(self, target: Any, timeout: float, negated: bool) -> None:
        self._target = target
        self._timeout = _seconds(timeout)
        self._negated = negated

    def _satisfied(self, reading: Reading) -> bool:
        return reading.holds is not None and reading.holds != self._negated

    def _fail(
        self, name: str, args: str, expected: str, value: Any, reading: Reading
    ) -> ExpectationError:
        """The error for a failed assertion; ``.actual`` keeps the whole last
        reading (the message shows at most ``ACTUAL_SHOWN`` chars of a text),
        ``.expected`` the asserted ``value``."""
        negation = ".not_" if self._negated else ""
        expected_text = f"not {expected}" if self._negated else expected
        message = (
            f"expect({self._target!r}){negation}.{name}({args}) failed after "
            f"{self._timeout}s: expected {expected_text}, "
            f"last actual {_shown(reading.actual)}"
        )
        if reading.note:
            message += f"; {reading.note}"
        error = ExpectationError(message, method=f"expect.{name}")
        # The error's __reduce__ carries __dict__, so pickling keeps both.
        error.actual = reading.actual
        error.expected = value
        return error

    @staticmethod
    def _read(read: Callable[[], Reading]) -> tuple[Reading, EmuniumError | None]:
        """``read()``; a transient error is an undecided reading."""
        try:
            return read(), None
        except RETRYABLE as exc:
            return Reading(None, f"<{type(exc).__name__}: {exc.message}>"), exc

    def _check(
        self,
        name: str,
        args: str,
        expected: str,
        value: Any,
        read: Callable[[], Reading],
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        """Hold within one deadline, or raise ``ExpectationError``.

        ``expected`` describes the expectation in the message; ``value`` is
        the asserted value the error carries as ``.expected``.

        ``wait(time_left)`` is an extension wait for the (negation-aware)
        condition: True when it held, False when it cannot be used (then the
        assertion polls ``read``); its ``WaitTimeoutError`` ends the wait.
        Every error in ``RETRYABLE`` is retried until the deadline.
        """
        deadline = _monotonic() + self._timeout
        last_error: BaseException | None = None
        while wait is not None:
            try:
                if not wait(max(0.0, deadline - _monotonic())):
                    break
                return
            except WaitTimeoutError as exc:
                reading, error = self._read(read)
                if self._satisfied(reading):
                    return
                raise self._fail(name, args, expected, value, reading) from (
                    error or last_error or exc
                )
            except RETRYABLE as exc:
                remaining = deadline - _monotonic()
                last_error = exc
                if remaining <= 0:
                    reading, error = self._read(read)
                    raise self._fail(name, args, expected, value, reading) from (
                        error or exc
                    )
                _sleep(min(POLL_S, remaining))
        while True:
            reading, error = self._read(read)
            if error is not None:
                last_error = error
            if self._satisfied(reading):
                return
            remaining = deadline - _monotonic()
            if remaining <= 0:
                raise self._fail(name, args, expected, value, reading) from last_error
            _sleep(min(POLL_S, remaining))


class LocatorAssertions(_Assertions):
    """Assertions on an ``ElementLocator``; build with ``expect(locator)``.

    An assertion returns None once it holds. Otherwise, after the timeout, it
    raises ``ExpectationError`` (an ``AssertionError``) naming the locator,
    the expectation and the last actual value (texts cut to ``ACTUAL_SHOWN``
    chars); ``.expected`` and ``.actual`` carry both values, ``.actual`` whole.
    """

    _target: ElementLocator

    def __init__(
        self,
        locator: ElementLocator,
        timeout: float = DEFAULT_TIMEOUT,
        negated: bool = False,
    ) -> None:
        super().__init__(locator, timeout, negated)

    @property
    def not_(self) -> LocatorAssertions:
        """The same assertions, negated."""
        return LocatorAssertions(self._target, self._timeout, not self._negated)

    def _server(
        self,
        *,
        state: str | None = None,
        positive: list[dict[str, Any]] | None = None,
        negative: list[dict[str, Any]] | None = None,
        negative_state: str | None = None,
    ) -> Callable[[float], bool]:
        """An extension wait for ``state``/``positive``, or for
        ``negative_state``/``negative`` when negated."""
        conditions = negative if self._negated else positive
        wait_state = negative_state if self._negated else state

        def wait(timeout: float) -> bool:
            return self._target._server_wait(
                timeout, state=wait_state, conditions=conditions
            )

        return wait

    def _first(self) -> Element | None:
        found = self._target.all()
        return found[0] if found else None

    def to_be_visible(self) -> None:
        """Assert that the target exists and is visible.

        Raises:
            ExpectationError: It did not hold within the timeout
                (``.expected`` is ``"visible"``).
        """

        def read() -> Reading:
            visible = self._target.is_visible()
            return Reading(visible, "visible" if visible else "hidden or absent")

        hidden = (Wait().detached() | Wait().hidden()).to_payload()
        self._check(
            "to_be_visible",
            "",
            "visible",
            "visible",
            read,
            self._server(state="visible", negative=hidden),
        )

    def to_be_hidden(self) -> None:
        """Assert that the target is absent or not visible.

        Raises:
            ExpectationError: It did not hold within the timeout
                (``.expected`` is ``"hidden"``).
        """

        def read() -> Reading:
            visible = self._target.is_visible()
            return Reading(not visible, "visible" if visible else "hidden or absent")

        hidden = (Wait().detached() | Wait().hidden()).to_payload()
        self._check(
            "to_be_hidden",
            "",
            "hidden",
            "hidden",
            read,
            self._server(positive=hidden, negative_state="visible"),
        )

    def to_be_attached(self) -> None:
        """Assert that the target is in the document.

        Raises:
            ExpectationError: It did not hold within the timeout
                (``.expected`` is ``"attached"``, ``.actual`` the match count text).
        """

        def read() -> Reading:
            count = self._target.count()
            return Reading(count > 0, f"{count} match(es)")

        detached = Wait().detached().to_payload()
        self._check(
            "to_be_attached",
            "",
            "attached",
            "attached",
            read,
            self._server(negative=detached),
        )

    def to_be_detached(self) -> None:
        """Assert that the target is not in the document.

        Raises:
            ExpectationError: It did not hold within the timeout
                (``.expected`` is ``"detached"``).
        """

        def read() -> Reading:
            count = self._target.count()
            return Reading(count == 0, f"{count} match(es)")

        detached = Wait().detached().to_payload()
        self._check(
            "to_be_detached",
            "",
            "detached",
            "detached",
            read,
            self._server(positive=detached),
        )

    def to_have_count(self, count: int) -> None:
        """Assert that exactly ``count`` elements match.

        Raises:
            InvalidArgumentError: ``count`` is not a non-negative int.
            ExpectationError: It did not hold within the timeout
                (``.expected`` is ``count``, ``.actual`` the last count).
        """
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise InvalidArgumentError(
                f"to_have_count() expects a non-negative int, got {count!r}"
            )

        def read() -> Reading:
            actual = self._target.count()
            return Reading(actual == count, actual)

        equal = Wait().count_eq(count)
        self._check(
            "to_have_count",
            repr(count),
            f"count {count}",
            count,
            read,
            self._server(positive=equal.to_payload(), negative=(~equal).to_payload()),
        )

    def to_have_text(
        self, text: TextOrPattern, *, exact: bool = False, ignore_case: bool = False
    ) -> None:
        """Assert that the target's whole text equals ``text`` (or that the
        pattern is found in it with ``search``).

        Case-sensitive unless ``ignore_case``. ``exact=False`` collapses
        whitespace on both sides first; ``exact=True`` compares the text as it
        is, which is brittle: the rendered text's whitespace depends on
        layout. Long texts compare in full; only a text cut at
        ``FULL_TEXT_CAP`` characters never passes, plain or negated.

        Raises:
            InvalidArgumentError: ``text`` is neither a str nor a str pattern.
            ExpectationError: It did not hold within the timeout; ``.expected``
                is ``text``, ``.actual`` the whole last text (the message
                shows at most ``ACTUAL_SHOWN`` chars of it).
        """
        expected = _text_or_pattern(text, "text")

        def read() -> Reading:
            element = self._first()
            if element is None:
                return Reading(False, _MISSING)
            actual = full_text(element)
            if is_truncated(actual):
                return Reading(None, actual, TRUNCATED_NOTE)
            holds = _matches(
                actual, expected, normalize=not exact, ignore_case=ignore_case
            )
            return Reading(holds, actual)

        args = repr(expected) + _options(exact=exact, ignore_case=ignore_case)
        self._check("to_have_text", args, f"text {expected!r}", expected, read)

    def to_contain_text(self, text: str, *, ignore_case: bool = False) -> None:
        """Assert that the target's text contains ``text``.

        Case-sensitive unless ``ignore_case`` (which polls instead of waiting
        in the page). Long texts are searched in full. Only on a text cut at
        ``FULL_TEXT_CAP`` characters is just the kept prefix known: found
        there it holds, otherwise the result is unknown and the assertion
        (plain or negated) does not pass.

        Raises:
            InvalidArgumentError: ``text`` is not a str.
            ExpectationError: It did not hold within the timeout; ``.expected``
                is ``text``, ``.actual`` the whole last text.
        """
        if not isinstance(text, str):
            raise InvalidArgumentError(f"text must be a str, got {text!r}")

        def read() -> Reading:
            element = self._first()
            if element is None:
                return Reading(False, _MISSING)
            actual = full_text(element)
            needle, haystack = text, actual
            if ignore_case:
                needle, haystack = needle.casefold(), haystack.casefold()
            if needle in haystack:
                return Reading(True, actual)
            if is_truncated(actual):
                return Reading(None, actual, TRUNCATED_NOTE)
            return Reading(False, actual)

        wait = None
        if not ignore_case:
            contains = Wait().text_contains(text)
            wait = self._server(
                positive=contains.to_payload(), negative=(~contains).to_payload()
            )
        args = repr(text) + _options(ignore_case=ignore_case)
        description = f"text containing {text!r}"
        self._check("to_contain_text", args, description, text, read, wait)

    def to_have_attribute(self, name: str, value: TextOrPattern | None = None) -> None:
        """Assert that the target has attribute ``name`` (equal to ``value``,
        or matching it with ``search`` when it is a pattern, when given).

        Raises:
            InvalidArgumentError: ``name`` is empty or ``value`` is neither a
                str nor a str pattern.
            ExpectationError: It did not hold within the timeout; ``.expected``
                is ``(name, value)``, ``.actual`` the last attribute value
                (None when absent).
        """
        if not isinstance(name, str) or not name:
            raise InvalidArgumentError(f"name must be a non-empty str, got {name!r}")
        expected = None if value is None else _text_or_pattern(value, "value")

        def read() -> Reading:
            element = self._first()
            if element is None:
                return Reading(False, _MISSING)
            actual = element.attrs.get(name)
            if actual is None:
                return Reading(False, None)
            if expected is None:
                return Reading(True, actual)
            if isinstance(expected, re.Pattern):
                return Reading(expected.search(actual) is not None, actual)
            return Reading(actual == expected, actual)

        args = repr(name) if expected is None else f"{name!r}, {expected!r}"
        description = f"attribute {name!r}"
        if expected is not None:
            description += f" = {expected!r}"
        wait = None
        if not isinstance(expected, re.Pattern):
            has = Wait().has_attribute(name, expected)
            wait = self._server(positive=has.to_payload(), negative=(~has).to_payload())
        self._check(
            "to_have_attribute", args, description, (name, expected), read, wait
        )


class PageAssertions(_Assertions):
    """Assertions on the pinned tab of a ``Browser``; build with ``expect(browser)``.

    Failures raise ``ExpectationError`` as in ``LocatorAssertions``.
    """

    _target: Browser

    def __init__(
        self, browser: Browser, timeout: float = DEFAULT_TIMEOUT, negated: bool = False
    ) -> None:
        super().__init__(browser, timeout, negated)

    @property
    def not_(self) -> PageAssertions:
        """The same assertions, negated."""
        return PageAssertions(self._target, self._timeout, not self._negated)

    def to_have_url(self, url: UrlMatcher) -> None:
        """Assert that the pinned tab's URL matches ``url``.

        Args:
            url: A glob (``*`` wildcard, whole URL), a ``re.Pattern``
                (``search``) or a ``Callable[[str], bool]``.

        Raises:
            InvalidArgumentError: ``url`` is none of those.
            ExpectationError: It did not hold within the timeout; ``.expected``
                is ``url``, ``.actual`` the last URL.
        """
        matches = url_matcher(url)

        def read() -> Reading:
            current = self._target.url
            return Reading(matches(current), current)

        expected = getattr(url, "__qualname__", None) if callable(url) else None
        description = f"URL matching {expected or url!r}"
        self._check("to_have_url", repr(url), description, url, read)

    def to_have_title(self, title: TextOrPattern, *, ignore_case: bool = False) -> None:
        """Assert that the title equals ``title`` after collapsing whitespace
        (or that the pattern is found in it); case-sensitive unless
        ``ignore_case``.

        Raises:
            InvalidArgumentError: ``title`` is neither a str nor a str pattern.
            ExpectationError: It did not hold within the timeout; ``.expected``
                is ``title``, ``.actual`` the last title.
        """
        expected = _text_or_pattern(title, "title")

        def read() -> Reading:
            current = self._target.title
            holds = _matches(current, expected, normalize=True, ignore_case=ignore_case)
            return Reading(holds, current)

        args = repr(expected) + _options(ignore_case=ignore_case)
        self._check("to_have_title", args, f"title {expected!r}", expected, read)


@overload
def expect(
    target: ElementLocator, *, timeout: float = DEFAULT_TIMEOUT
) -> LocatorAssertions: ...


@overload
def expect(target: Browser, *, timeout: float = DEFAULT_TIMEOUT) -> PageAssertions: ...


def expect(
    target: ElementLocator | Browser, *, timeout: float = DEFAULT_TIMEOUT
) -> LocatorAssertions | PageAssertions:
    """Build retrying assertions on a locator or a browser's page.

    Args:
        target: An ``ElementLocator`` (gives ``LocatorAssertions``) or a
            ``Browser`` (gives ``PageAssertions``).
        timeout: Seconds each assertion retries before it fails.

    Returns:
        The assertions; ``.not_`` negates them.

    Raises:
        InvalidArgumentError: ``target`` is neither, or ``timeout`` is not a
            number >= 0.
    """
    if isinstance(target, ElementLocator):
        return LocatorAssertions(target, timeout)
    if isinstance(target, Browser):
        return PageAssertions(target, timeout)
    raise InvalidArgumentError(
        f"expect() takes an ElementLocator or a Browser, got {target!r}"
    )
