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
    PRESENCE = "presence"
    VISIBLE = "visible"
    CLICKABLE = "clickable"
    STABLE = "stable"
    UNOBSCURED = "unobscured"


class Wait:
    def __init__(self) -> None:
        self._conditions: list[dict] = []

    def visible(self) -> Wait:
        self._conditions.append({"type": "visible"})
        return self

    def clickable(self) -> Wait:
        self._conditions.append({"type": "clickable"})
        return self

    def stable(self, duration_ms: int = 300) -> Wait:
        self._conditions.append({"type": "stable", "duration": duration_ms})
        return self

    def unobscured(self) -> Wait:
        self._conditions.append({"type": "unobscured"})
        return self

    def hidden(self) -> Wait:
        self._conditions.append({"type": "hidden"})
        return self

    def detached(self) -> Wait:
        self._conditions.append({"type": "detached"})
        return self

    def text_not_empty(self) -> Wait:
        self._conditions.append({"type": "text_not_empty"})
        return self

    def text_contains(self, substring: str) -> Wait:
        self._conditions.append({"type": "text_contains", "value": substring})
        return self

    def has_attribute(self, name: str, value: str | None = None) -> Wait:
        self._conditions.append({"type": "has_attribute", "name": name, "value": value})
        return self

    def without_attribute(self, name: str) -> Wait:
        self._conditions.append({"type": "without_attribute", "name": name})
        return self

    def has_class(self, name: str) -> Wait:
        self._conditions.append({"type": "has_class", "value": name})
        return self

    def has_style(self, prop: str, value: str) -> Wait:
        self._conditions.append({"type": "has_style", "name": prop, "value": value})
        return self

    def count_gt(self, n: int) -> Wait:
        self._conditions.append({"type": "count_gt", "value": _count("count_gt", n)})
        return self

    def count_eq(self, n: int) -> Wait:
        self._conditions.append({"type": "count_eq", "value": _count("count_eq", n)})
        return self

    def custom_js(self, code: str) -> Wait:
        self._conditions.append({"type": "custom_js", "code": code})
        return self

    def any_of(self, *conditions: Wait) -> Wait:
        """At least one group of conditions must be satisfied (OR)."""
        self._conditions.append(
            {
                "type": "any_of",
                "conditions": [c.to_payload() for c in conditions],
            }
        )
        return self

    def all_of(self, *conditions: Wait) -> Wait:
        """Every group of conditions must be satisfied (explicit AND)."""
        self._conditions.append(
            {
                "type": "all_of",
                "conditions": [c.to_payload() for c in conditions],
            }
        )
        return self

    def not_(self, condition: Wait) -> Wait:
        """Negate a group of conditions."""
        self._conditions.append(
            {
                "type": "not",
                "condition": condition.to_payload(),
            }
        )
        return self

    def to_payload(self) -> list[dict]:
        """A deep copy: mutating the payload never changes this builder."""
        return copy.deepcopy(self._conditions)

    def __repr__(self) -> str:
        return f"Wait(conditions={self._conditions})"
