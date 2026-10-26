"""Result records: plain dicts with typed attribute access.

Contract: ``docs/architecture/dx-api.md`` section 1. A record *is* the dict the
extension replied with (same camelCase keys, equality, ``json.dumps``,
``dict(...)`` copies, and ``repr``/``str`` identical to the plain dict); typed
properties map snake_case names onto those keys, and ``__getattr__`` covers
every other key (``record.content_ready`` reads ``record["contentReady"]``).
"""

from __future__ import annotations

from typing import Any

__all__ = ["NetworkResponse", "PageInfo", "Point", "Record", "TabInfo"]


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def _property_of(cls: type, name: str) -> property | None:
    for klass in cls.__mro__:
        attr = klass.__dict__.get(name)
        if attr is not None:
            return attr if isinstance(attr, property) else None
    return None


class Record(dict[str, Any]):
    """A ``dict`` whose keys can also be read as snake_case attributes.

    Typed properties raise ``AttributeError`` (not ``KeyError``) when their
    key is absent from the reply.
    """

    # No instance __dict__: an attribute assignment cannot silently shadow a key.
    __slots__ = ()

    # Printed exactly like the plain dict, in every subclass.
    __repr__ = dict.__repr__
    __str__ = dict.__str__

    def __getattr__(self, name: str) -> Any:
        # Python calls this when a property getter raised AttributeError, and
        # drops that error: run the getter again so its message (naming the
        # missing key) is the one raised.
        prop = _property_of(type(self), name)
        if prop is not None and prop.fget is not None:
            return prop.fget(self)
        # Dunder and private probes (pickle, copy, IDEs) must not reach the keys.
        if not name.startswith("_"):
            if name in self:
                return self[name]
            key = _camel(name)
            if key in self:
                return self[key]
        raise AttributeError(
            f"{type(self).__name__!r} object has no attribute {name!r}"
        )

    def _require(self, key: str, name: str) -> Any:
        """``self[key]``; ``AttributeError`` (not ``KeyError``) when absent."""
        try:
            return self[key]
        except KeyError:
            raise AttributeError(
                f"{type(self).__name__} has no {key!r} key for attribute {name!r}"
            ) from None

    def __reduce__(self) -> tuple[Any, ...]:
        # Rebuilt through the class from a plain dict: no __getattr__ during load.
        return (type(self), (dict(self),))


class PageInfo(Record):
    """``Browser.page_info()`` / ``Browser.goto()`` reply."""

    __slots__ = ()

    @property
    def url(self) -> str:
        """The page URL (``url``)."""
        return self._require("url", "url")

    @property
    def title(self) -> str:
        """The page title (``title``)."""
        return self._require("title", "title")

    @property
    def ready_state(self) -> str:
        """``document.readyState`` (``page_info`` only; ``goto`` does not carry it)."""
        return self._require("readyState", "ready_state")

    @property
    def scroll_x(self) -> float:
        """Horizontal scroll offset in CSS px (``scrollX``)."""
        return self._require("scrollX", "scroll_x")

    @property
    def scroll_y(self) -> float:
        """Vertical scroll offset in CSS px (``scrollY``)."""
        return self._require("scrollY", "scroll_y")

    @property
    def scroll_width(self) -> float:
        """Document scroll width in CSS px (``scrollWidth``)."""
        return self._require("scrollWidth", "scroll_width")

    @property
    def scroll_height(self) -> float:
        """Document scroll height in CSS px (``scrollHeight``)."""
        return self._require("scrollHeight", "scroll_height")


class TabInfo(Record):
    """A tab description: ``Browser.tab_info()`` and each ``Browser.tabs()`` entry."""

    __slots__ = ()

    @property
    def tab_id(self) -> int:
        """Chrome tab id (``tabId``)."""
        return self._require("tabId", "tab_id")

    @property
    def url(self) -> str:
        """The page URL (``url``)."""
        return self._require("url", "url")

    @property
    def title(self) -> str:
        """The page title (``title``)."""
        return self._require("title", "title")

    @property
    def active(self) -> bool:
        """True for the active tab of its window."""
        return self._require("active", "active")

    @property
    def window_id(self) -> int:
        """Chrome window id (``windowId``)."""
        return self._require("windowId", "window_id")

    @property
    def status(self) -> str | None:
        """``"loading"`` / ``"complete"``; None when the reply does not carry it."""
        return self.get("status")


class NetworkResponse(Record):
    """A network entry (``wait_for_response``, ``ResponseExpectation.value``)."""

    __slots__ = ()

    @property
    def url(self) -> str:
        """The request URL (``url``)."""
        return self._require("url", "url")

    @property
    def status(self) -> int:
        """HTTP status code (``statusCode``); 0 for a failed request."""
        return self._require("statusCode", "status")

    @property
    def method(self) -> str:
        """HTTP method (``GET``, ``POST``, ...)."""
        return self._require("method", "method")

    @property
    def type(self) -> str:
        """``webRequest`` resource type (``main_frame``, ``xmlhttprequest``, ...)."""
        return self._require("type", "type")

    @property
    def tab_id(self) -> int:
        """Chrome tab id (``tabId``)."""
        return self._require("tabId", "tab_id")

    @property
    def request_id(self) -> str | None:
        """``webRequest`` request id (``requestId``)."""
        return self._require("requestId", "request_id")

    @property
    def error(self) -> str | None:
        """Network error text of a failed request; None otherwise."""
        return self.get("error")

    @property
    def ok(self) -> bool:
        """True for a 2xx/3xx status without a network error."""
        status = self.get("statusCode")
        return (
            isinstance(status, int)
            and not isinstance(status, bool)
            and 200 <= status < 400
            and self.error is None
        )

    @property
    def timestamp(self) -> float:
        """``timeStamp``: epoch milliseconds."""
        return self._require("timeStamp", "timestamp")


class Point(Record):
    """Screen point (``Browser.get_center``)."""

    __slots__ = ()

    @property
    def x(self) -> int:
        """Horizontal screen coordinate in INPUT units."""
        return self._require("x", "x")

    @property
    def y(self) -> int:
        """Vertical screen coordinate in INPUT units."""
        return self._require("y", "y")
