from __future__ import annotations

import copy
from enum import Enum

from emunium.errors import InvalidArgumentError


def _count(name: str, n: object) -> int:
    """``n`` as a non-negative int; bools and other types are rejected."""
    if isinstance(n, bool) or not isinstance(n, int):
        raise InvalidArgumentError(f"{name}() expects a non-negative int, got {n!r}")
    if n < 0:
        raise InvalidArgumentError(f"{name}() expects a non-negative int, got {n}")
    return n


class WaitStrategy(str, Enum):
    """The element state ``Browser.wait`` waits for before its conditions."""

    PRESENCE = "presence"
    VISIBLE = "visible"
    CLICKABLE = "clickable"
    STABLE = "stable"
    UNOBSCURED = "unobscured"


class Wait:
    """Fluent builder of the conditions a wait checks in the page.

    Each method appends a condition and returns this builder; chained
    conditions are ANDed. The extension checks them on the first element the
    selector matches; ``|``, ``&`` and ``~`` build new groups.
    """

    def __init__(self) -> None:
        self._conditions: list[dict] = []

    def visible(self) -> Wait:
        """Require the element to be rendered: not ``display: none``, not
        ``visibility: hidden``, opacity above 0, with a non-zero box."""
        self._conditions.append({"type": "visible"})
        return self

    def clickable(self) -> Wait:
        """Require the element to be visible, enabled and to accept pointer events."""
        self._conditions.append({"type": "clickable"})
        return self

    def stable(self, duration_ms: int = 300) -> Wait:
        """Require the bounding rect to stay unchanged for ``duration_ms``."""
        self._conditions.append({"type": "stable", "duration": duration_ms})
        return self

    def unobscured(self) -> Wait:
        """Require that no other element covers the element's center point."""
        self._conditions.append({"type": "unobscured"})
        return self

    def hidden(self) -> Wait:
        """Require the element to be absent or not visible."""
        self._conditions.append({"type": "hidden"})
        return self

    def detached(self) -> Wait:
        """Require that no connected element matches."""
        self._conditions.append({"type": "detached"})
        return self

    def text_not_empty(self) -> Wait:
        """Require non-empty element text after trimming."""
        self._conditions.append({"type": "text_not_empty"})
        return self

    def text_contains(self, substring: str) -> Wait:
        """Require the element text to contain ``substring`` (case-sensitive)."""
        self._conditions.append({"type": "text_contains", "value": substring})
        return self

    def has_attribute(self, name: str, value: str | None = None) -> Wait:
        """Require attribute ``name`` (equal to ``value`` when given)."""
        self._conditions.append({"type": "has_attribute", "name": name, "value": value})
        return self

    def without_attribute(self, name: str) -> Wait:
        """Require attribute ``name`` to be absent."""
        self._conditions.append({"type": "without_attribute", "name": name})
        return self

    def has_class(self, name: str) -> Wait:
        """Require CSS class ``name``."""
        self._conditions.append({"type": "has_class", "value": name})
        return self

    def has_style(self, prop: str, value: str) -> Wait:
        """Require the computed style ``prop`` to equal ``value``."""
        self._conditions.append({"type": "has_style", "name": prop, "value": value})
        return self

    def count_gt(self, n: int) -> Wait:
        """Require more than ``n`` matches.

        Raises:
            InvalidArgumentError: ``n`` is not a non-negative int.
        """
        self._conditions.append({"type": "count_gt", "value": _count("count_gt", n)})
        return self

    def count_eq(self, n: int) -> Wait:
        """Require exactly ``n`` matches.

        Raises:
            InvalidArgumentError: ``n`` is not a non-negative int.
        """
        self._conditions.append({"type": "count_eq", "value": _count("count_eq", n)})
        return self

    def custom_js(self, code: str) -> Wait:
        """Require ``code`` (a function body run in the page with ``el`` bound
        to the element) to return a truthy value synchronously."""
        self._conditions.append({"type": "custom_js", "code": code})
        return self

    def any_of(self, *conditions: Wait) -> Wait:
        """Require at least one of the ``conditions`` groups to hold (OR)."""
        self._conditions.append(
            {
                "type": "any_of",
                "conditions": [c.to_payload() for c in conditions],
            }
        )
        return self

    def all_of(self, *conditions: Wait) -> Wait:
        """Require every one of the ``conditions`` groups to hold (explicit AND)."""
        self._conditions.append(
            {
                "type": "all_of",
                "conditions": [c.to_payload() for c in conditions],
            }
        )
        return self

    def not_(self, condition: Wait) -> Wait:
        """Require the ``condition`` group not to hold."""
        self._conditions.append(
            {
                "type": "not",
                "condition": condition.to_payload(),
            }
        )
        return self

    def to_payload(self) -> list[dict]:
        """Return the conditions as sent to the extension (a deep copy)."""
        return copy.deepcopy(self._conditions)

    # Operators build a new Wait from deep copies of both payloads, so neither
    # operand changes and later edits to an operand never reach the result.

    def __or__(self, other: object) -> Wait:
        """``a | b``: a new ``Wait().any_of(a, b)``."""
        if not isinstance(other, Wait):
            return NotImplemented
        return Wait().any_of(self, other)

    def __and__(self, other: object) -> Wait:
        """``a & b``: a new ``Wait().all_of(a, b)``."""
        if not isinstance(other, Wait):
            return NotImplemented
        return Wait().all_of(self, other)

    def __invert__(self) -> Wait:
        """``~a``: a new ``Wait().not_(a)``."""
        return Wait().not_(self)

    def __repr__(self) -> str:
        return f"Wait(conditions={self._conditions})"
