"""Tests for emunium.locator (Locator, PageParser) and emunium.coords (CoordsStore)."""

from __future__ import annotations

import sys

import pytest

from emunium.coords import CoordsStore
from emunium.locator import Locator, PageParser


class _LiveNode:
    """Mimics a live bridge Element: ``text`` is a plain str property."""

    def __init__(self, text):
        self._value = text

    @property
    def text(self):
        return self._value


class _ParsedNode:
    """Mimics a selectolax node: ``text`` is a method."""

    def __init__(self, text):
        self._value = text

    def text(self, deep=True, strip=False):
        return self._value


# --- Locator.filter / inner_text -------------------------------------------


def test_filter_has_text_on_plain_str_text():
    nodes = [_LiveNode("Sign In"), _LiveNode("Register"), _LiveNode(None)]
    loc = Locator(nodes).filter(has_text="sign")
    assert loc.all() == [nodes[0]]


def test_filter_has_text_on_callable_text():
    nodes = [_ParsedNode("Sign In"), _ParsedNode("Register")]
    assert Locator(nodes).filter(has_text="REGISTER").all() == [nodes[1]]


def test_inner_text_plain_and_callable():
    assert Locator([_LiveNode("live")]).inner_text() == "live"
    assert Locator([_ParsedNode("parsed")]).inner_text() == "parsed"
    assert Locator([]).inner_text() is None


# --- PageParser without selectolax -----------------------------------------

HTML = """
<html><head><title> Demo Page </title></head><body>
  <label for="1st.email">Email</label><input id="1st.email" placeholder="you@x">
  <label>Name <input name="n"></label>
  <button data-testid="go">Go now</button>
  <a href="/x">Link</a>
</body></html>
"""


@pytest.fixture
def no_selectolax(monkeypatch):
    # A None entry in sys.modules makes the import raise ImportError.
    monkeypatch.setitem(sys.modules, "selectolax", None)
    monkeypatch.setitem(sys.modules, "selectolax.lexbor", None)


@pytest.mark.parametrize(
    "call",
    [
        lambda p: p.locator("button"),
        lambda p: p.get_by_text("Go"),
        lambda p: p.get_by_role("button"),
        lambda p: p.get_by_placeholder("you"),
        lambda p: p.get_by_label("Email"),
        lambda p: p.get_by_test_id("go"),
    ],
)
def test_queries_raise_import_error_without_selectolax(no_selectolax, call):
    parser = PageParser(HTML)
    with pytest.raises(ImportError, match=r"pip install emunium\[parsing\]"):
        call(parser)


def test_title_regex_fallback_without_selectolax(no_selectolax):
    assert PageParser(HTML).title == "Demo Page"


# --- PageParser with selectolax --------------------------------------------


@pytest.fixture
def parser():
    pytest.importorskip("selectolax.lexbor")
    return PageParser(HTML)


def test_get_by_label_for_id_that_is_not_a_valid_hash_selector(parser):
    loc = parser.get_by_label("email")
    assert loc.count() == 1
    assert loc.get_attribute("placeholder") == "you@x"


def test_get_by_label_wrapping_input(parser):
    assert parser.get_by_label("name").get_attribute("name") == "n"


def test_selectolax_queries_and_filter(parser):
    assert parser.locator("button", has_text="go now").count() == 1
    assert parser.get_by_test_id("go").inner_text() == "Go now"
    assert parser.get_by_role("link").count() == 1


# --- CoordsStore.query -----------------------------------------------------


@pytest.fixture
def store():
    s = CoordsStore()
    s.update(
        {
            "elements": [
                {"tag": "DIV", "attrs": {"class": "wrap"}, "rect": {}},
                {"tag": "BUTTON", "attrs": {"class": "btn primary", "id": "ok"}, "rect": {}},
                {"tag": "A", "attrs": {"href": "/a,b", "class": "btn"}, "rect": {}},
                {"tag": "my-widget", "attrs": {"data-x": "1"}, "rect": {}},
            ]
        }
    )
    return s


def _tags(records):
    return [r.tag.lower() for r in records]


def test_query_compound_selectors(store):
    assert _tags(store.query("button.btn.primary#ok")) == ["button"]
    assert _tags(store.query(".btn#ok")) == ["button"]
    assert _tags(store.query('a[href="/a,b"]')) == ["a"]
    assert _tags(store.query("my-widget[data-x]")) == ["my-widget"]
    assert len(store.query("*")) == 4


def test_query_selector_list(store):
    assert _tags(store.query("div, a")) == ["div", "a"]
    assert _tags(store.query("#ok,[href^='/a,']")) == ["button", "a"]


@pytest.mark.parametrize(
    "selector, message",
    [
        ("div .btn", "combinator"),
        ("div > .btn", "combinator"),
        ("div + a", "combinator"),
        ("div ~ a", "combinator"),
        ("a, div .btn", "combinator"),
        ("button:hover", "pseudo-class"),
        ("a::before", "pseudo-class"),
        ("div,", "empty selector"),
        ("", "empty selector"),
        ("[href|=x]", "unsupported attribute"),
    ],
)
def test_query_rejects_unsupported_selectors(store, selector, message):
    with pytest.raises(ValueError, match=message):
        store.query(selector)
