"""Result records (dx-api.md section 1): dicts first, attributes second.

Every compatibility promise is pinned here: ``isinstance(x, dict)``, ``x["key"]``,
``==`` with a plain dict, ``json.dumps``, ``dict(...)`` copies, pickling and
``copy``; then the typed attributes and the snake_case fallback.
"""

from __future__ import annotations

import copy
import json
import pickle
from typing import Any

import pytest

import emunium
from emunium.results import NetworkResponse, PageInfo, Point, Record, TabInfo

# Shapes the extension replies with (content.js pageInfo, background_handlers.js
# getTabInfo, background_network.js makeEntry).
PAGE: dict[str, Any] = {
    "url": "https://a.test/",
    "title": "A",
    "readyState": "complete",
    "scrollX": 0,
    "scrollY": 120.5,
    "scrollWidth": 1000,
    "scrollHeight": 3000,
    "geometry": {"screenX": 0, "screenY": 0},
}
TAB: dict[str, Any] = {
    "tabId": 4,
    "url": "https://a.test/",
    "title": "A",
    "status": "complete",
    "active": True,
    "windowId": 1,
}
ENTRY: dict[str, Any] = {
    "url": "https://a.test/api",
    "statusCode": 201,
    "method": "POST",
    "type": "xmlhttprequest",
    "timeStamp": 1700000000123.5,
    "tabId": 4,
    "requestId": "77",
}

RECORDS: list[Record] = [
    PageInfo(PAGE),
    TabInfo(TAB),
    NetworkResponse(ENTRY),
    Point(x=3, y=4),
    Record({"someKey": 1}),
]


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: type(r).__name__)
def test_record_is_still_the_plain_dict(record: Record) -> None:
    plain = dict(record)
    assert isinstance(record, dict)
    assert record == plain and plain == record
    assert (record != plain) is False
    assert type(dict(record)) is dict
    assert json.dumps(record, sort_keys=True) == json.dumps(plain, sort_keys=True)
    assert json.loads(json.dumps(record)) == plain
    assert list(record.keys()) == list(plain.keys())
    for key, value in plain.items():
        assert record[key] == value
        assert record.get(key) == value
    with pytest.raises(KeyError):
        _ = record["missingKey"]


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: type(r).__name__)
def test_pickle_and_copy_round_trip_to_the_same_class(record: Record) -> None:
    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        loaded = pickle.loads(pickle.dumps(record, protocol))
        assert type(loaded) is type(record)
        assert loaded == record
    for copied in (copy.copy(record), copy.deepcopy(record)):
        assert type(copied) is type(record)
        assert copied == record and copied is not record


def test_deepcopy_does_not_share_nested_values() -> None:
    info = PageInfo(PAGE)
    clone = copy.deepcopy(info)
    clone["geometry"]["screenX"] = 99
    assert info["geometry"]["screenX"] == 0


def test_page_info_attributes() -> None:
    info = PageInfo(PAGE)
    assert info.url == "https://a.test/" and info.title == "A"
    assert info.ready_state == "complete"
    assert (info.scroll_x, info.scroll_y) == (0, 120.5)
    assert (info.scroll_width, info.scroll_height) == (1000, 3000)
    assert info.geometry == {"screenX": 0, "screenY": 0}  # __getattr__ fallback
    assert info["readyState"] == "complete"


def test_page_info_from_goto_keeps_its_keys() -> None:
    reply = {"url": "https://a.test/", "title": "A", "tabId": 9, "contentReady": True}
    info = PageInfo(reply)
    assert info == reply
    assert (info.tab_id, info.content_ready) == (9, True)
    assert not hasattr(info, "ready_state")
    with pytest.raises(AttributeError):
        _ = info.ready_state


def test_tab_info_attributes() -> None:
    tab = TabInfo(TAB)
    assert (tab.tab_id, tab.url, tab.title) == (4, "https://a.test/", "A")
    assert (tab.active, tab.window_id, tab.status) == (True, 1, "complete")
    # listTabs entries carry no status.
    listed = TabInfo({k: v for k, v in TAB.items() if k != "status"})
    assert listed.status is None


def test_network_response_attributes() -> None:
    resp = NetworkResponse(ENTRY)
    assert (resp.url, resp.status, resp.method) == ("https://a.test/api", 201, "POST")
    assert (resp.type, resp.tab_id, resp.request_id) == ("xmlhttprequest", 4, "77")
    assert resp.timestamp == 1700000000123.5
    assert resp.error is None and resp.ok is True


@pytest.mark.parametrize(
    ("changes", "ok"),
    [
        ({"statusCode": 200}, True),
        ({"statusCode": 399}, True),
        ({"statusCode": 400}, False),
        ({"statusCode": 199}, False),
        ({"statusCode": 0, "error": "net::ERR_FAILED"}, False),
        ({"statusCode": 200, "error": "net::ERR_ABORTED"}, False),
    ],
)
def test_network_response_ok(changes: dict[str, Any], ok: bool) -> None:
    resp = NetworkResponse({**ENTRY, **changes})
    assert resp.ok is ok
    assert resp.error == changes.get("error")


def test_point_attributes() -> None:
    point = Point(x=3, y=4)
    assert (point.x, point.y) == (3, 4)
    assert point == {"x": 3, "y": 4}


def test_getattr_maps_snake_case_and_raises_attribute_error() -> None:
    record = Record({"someKey": 1, "plain": 2, "with_underscore": 3})
    assert record.some_key == 1
    assert record.plain == 2
    assert record.with_underscore == 3
    assert not hasattr(record, "missing")
    assert getattr(record, "missing", "default") == "default"
    with pytest.raises(AttributeError, match="missing"):
        _ = record.missing
    # Private and dunder names never reach the keys.
    hidden = Record({"_x": 1, "__deepcopy__": 2})
    assert not hasattr(hidden, "_x")
    assert type(copy.deepcopy(hidden)) is Record


def test_attributes_cannot_shadow_keys() -> None:
    info = PageInfo(PAGE)
    # "url" is a read-only property; "anything" has no instance __dict__ to land in.
    for name, value in (("url", "https://other.test/"), ("anything", 1)):
        with pytest.raises(AttributeError):
            setattr(info, name, value)
    assert info == PAGE


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: type(r).__name__)
def test_repr_and_str_are_the_plain_dicts(
    record: Record, capsys: pytest.CaptureFixture[str]
) -> None:
    plain = dict(record)
    assert repr(record) == repr(plain)
    assert str(record) == str(plain)
    assert f"{record}" == f"{plain}" and f"{record!r}" == f"{plain!r}"
    assert repr([record]) == repr([plain])
    print(record)
    printed = capsys.readouterr().out
    print(plain)
    assert printed == capsys.readouterr().out


def test_repr_does_not_drift_in_subclasses() -> None:
    for cls in (Record, PageInfo, TabInfo, NetworkResponse, Point):
        assert cls.__repr__ is dict.__repr__
        assert cls.__str__ is dict.__str__


def test_missing_typed_attribute_names_its_key() -> None:
    with pytest.raises(AttributeError, match="'readyState'") as info:
        _ = PageInfo({}).ready_state
    assert "ready_state" in str(info.value)
    with pytest.raises(AttributeError, match="'statusCode'"):
        _ = NetworkResponse({}).status
    with pytest.raises(AttributeError, match="'tabId'"):
        _ = TabInfo({"closed": True}).tab_id
    assert not hasattr(PageInfo({}), "url")
    # Properties that default to None never raise.
    assert TabInfo({}).status is None and NetworkResponse({}).error is None


def test_records_are_exported() -> None:
    for cls in (Record, PageInfo, TabInfo, NetworkResponse, Point):
        assert cls.__name__ in emunium.__all__
        assert getattr(emunium, cls.__name__) is cls
