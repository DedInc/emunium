"""``Wait`` operators and the Python URL matchers
(``dx-api.md`` section 3). No browser, no bridge.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from emunium._browser.page import compile_glob, url_matcher
from emunium.errors import InvalidArgumentError
from emunium.wait import Wait

# -- operators ---------------------------------------------------------------------


def _same(left: Wait, right: Wait) -> bool:
    return left.to_payload() == right.to_payload()


def test_or_and_invert_equal_the_builder_methods() -> None:
    a, b = Wait().visible(), Wait().text_contains("ok")
    assert _same(a | b, Wait().any_of(a, b))
    assert _same(a & b, Wait().all_of(a, b))
    assert _same(~a, Wait().not_(a))
    assert (a | b).to_payload() == [
        {
            "type": "any_of",
            "conditions": [
                [{"type": "visible"}],
                [{"type": "text_contains", "value": "ok"}],
            ],
        }
    ]
    assert (~a).to_payload() == [{"type": "not", "condition": [{"type": "visible"}]}]


def test_operators_return_new_waits_and_leave_operands_unchanged() -> None:
    a, b = Wait().visible(), Wait().hidden()
    results = [a | b, a & b, ~a]
    assert all(type(result) is Wait for result in results)
    assert all(result is not a and result is not b for result in results)
    assert a.to_payload() == [{"type": "visible"}]
    assert b.to_payload() == [{"type": "hidden"}]


def test_operator_results_hold_copies_of_the_operands() -> None:
    a, b = Wait().visible(), Wait().hidden()
    either, both, neither = a | b, a & b, ~a
    a.clickable()
    b.detached()
    assert _same(either, Wait().any_of(Wait().visible(), Wait().hidden()))
    assert _same(both, Wait().all_of(Wait().visible(), Wait().hidden()))
    assert _same(neither, Wait().not_(Wait().visible()))
    # Building on the result does not reach back into an operand either.
    either.stable()
    assert a.to_payload() == [{"type": "visible"}, {"type": "clickable"}]


def test_operators_compose() -> None:
    a, b, c = Wait().visible(), Wait().hidden(), Wait().clickable()
    assert _same((a | b) & ~c, Wait().all_of(Wait().any_of(a, b), Wait().not_(c)))
    assert _same(a | b | c, Wait().any_of(Wait().any_of(a, b), c))


@pytest.mark.parametrize("other", [None, 1, "visible", [{"type": "visible"}]])
def test_mixing_with_a_non_wait_is_not_implemented(other: Any) -> None:
    wait = Wait().visible()
    assert wait.__or__(other) is NotImplemented
    assert wait.__and__(other) is NotImplemented
    with pytest.raises(TypeError):
        wait | other
    with pytest.raises(TypeError):
        other & wait
    assert wait.to_payload() == [{"type": "visible"}]


def test_waits_keep_identity_equality_and_stay_hashable() -> None:
    # 3.0 semantics: a Wait equals only itself and can key a dict or a set.
    first, second = Wait().visible(), Wait().visible()
    assert Wait.__eq__ is object.__eq__ and Wait.__hash__ is object.__hash__
    assert first != second
    assert len({first, second}) == 2


# -- glob matcher (background_network.js compilePattern) --------------------------
# The cases of tests/js/network.test.js "glob: only * is a wildcard".

GLOB_CASES = [
    ("https://a.test/x?id=1", "https://a.test/xYid=1", False),
    ("https://a.test/x?id=1", "https://a.test/x?id=1", True),
    ("https://a.test/apiXv1/*", "https://a.test/api.v1/(list)+[x]", False),
    ("https://a.test/api.v1/(list)+[x]", "https://a.test/api.v1/(list)+[x]", True),
    ("*a.test/api*", "https://a.test/api.v1/(list)+[x]", True),
    ("a.test/*", "https://a.test/api.v1/(list)+[x]", False),
    ("*/other.html", "http://127.0.0.1:5000/other.html", True),
    ("*/other.html", "http://127.0.0.1:5000/other.html#top", False),
    ("*", "", True),
    ("a*b", "a\nb", True),
    ("^a$", "^a$", True),
    ("a\\b", "a\\b", True),
]


@pytest.mark.parametrize(("pattern", "url", "expected"), GLOB_CASES)
def test_glob_matches_like_the_extension(
    pattern: str, url: str, expected: bool
) -> None:
    assert (compile_glob(pattern).fullmatch(url) is not None) is expected
    assert url_matcher(pattern)(url) is expected


@pytest.mark.parametrize("pattern", ["", None, 7])
def test_glob_rejects_what_the_extension_rejects(pattern: Any) -> None:
    with pytest.raises(InvalidArgumentError):
        compile_glob(pattern)


def test_url_matcher_regex_searches_and_predicate_is_called() -> None:
    by_regex = url_matcher(re.compile(r"/other\.html$"))
    assert by_regex("http://h/x/other.html") is True
    assert by_regex("http://h/other.html?q=1") is False
    seen: list[str] = []

    def predicate(url: str) -> Any:
        seen.append(url)
        return "#done" in url and 1

    by_call = url_matcher(predicate)
    assert by_call("http://h/#done") is True
    assert by_call("http://h/") is False
    assert seen == ["http://h/#done", "http://h/"]


@pytest.mark.parametrize("bad", [None, 7, re.compile(b"bytes"), ["*"]])
def test_url_matcher_rejects_other_types(bad: Any) -> None:
    with pytest.raises(InvalidArgumentError):
        url_matcher(bad)
