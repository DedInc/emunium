from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass

logger = logging.getLogger("emunium.coords")


@dataclass
class ElementRecord:
    tag: str
    attrs: dict[str, str]
    rect: dict[str, float]

    @property
    def center(self) -> tuple[float, float]:
        return (
            self.rect.get("x", 0) + self.rect.get("width", 0) / 2,
            self.rect.get("y", 0) + self.rect.get("height", 0) / 2,
        )


def _parse_selector(selector: str) -> list[dict]:
    conditions: list[dict] = []
    s = selector.strip()

    m = re.match(r"^([a-zA-Z][a-zA-Z0-9]*)", s)
    if m:
        conditions.append({"type": "tag", "value": m.group(1).lower()})
        s = s[m.end() :]

    m = re.match(r"^#([\w-]+)", s)
    if m:
        conditions.append(
            {"type": "attr", "name": "id", "op": "=", "value": m.group(1)}
        )
        s = s[m.end() :]

    while m := re.match(r"^\.([\w-]+)", s):
        conditions.append({"type": "class", "value": m.group(1)})
        s = s[m.end() :]

    while m := re.match(r"^\[([^\]]+)\]", s):
        inner = m.group(1)
        op_m = re.match(
            r"^([\w:_-]+)\s*(\*=|\^=|\$=|~=|=)\s*[\"']?([^\"']*)[\"']?$", inner
        )
        if op_m:
            conditions.append(
                {
                    "type": "attr",
                    "name": op_m.group(1),
                    "op": op_m.group(2),
                    "value": op_m.group(3),
                }
            )
        elif re.match(r"^[\w:_-]+$", inner.strip()):
            conditions.append({"type": "attr_present", "name": inner.strip()})
        s = s[m.end() :]

    return conditions


def _attr_match(val: str, op: str, target: str) -> bool:
    if op == "=":
        return val == target
    if op == "*=":
        return target in val
    if op == "^=":
        return val.startswith(target)
    if op == "$=":
        return val.endswith(target)
    if op == "~=":
        return target in val.split()
    return False


def _record_matches(record: ElementRecord, conditions: list[dict]) -> bool:
    for cond in conditions:
        t = cond["type"]
        if t == "tag":
            if record.tag.lower() != cond["value"]:
                return False
        elif t == "attr":
            v = record.attrs.get(cond["name"], "")
            if not _attr_match(v, cond["op"], cond["value"]):
                return False
        elif t == "class":
            if cond["value"] not in record.attrs.get("class", "").split():
                return False
        elif t == "attr_present":
            if cond["name"] not in record.attrs:
                return False
    return True


class CoordsStore:
    """Thread-safe store for element bounding rects.

    Used as a local cache populated from bridge queries.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: list[ElementRecord] = []
        self._inner_width: int | None = None
        self._inner_height: int | None = None

    @staticmethod
    def _build_records(elements: list[dict]) -> list[ElementRecord]:
        return [
            ElementRecord(
                tag=el.get("tag", ""),
                attrs=el.get("attrs", {}),
                rect=el.get("rect", {}),
            )
            for el in elements
        ]

    def update(self, payload: dict) -> None:
        self.update_from_bridge(
            payload.get("elements", []),
            payload,
        )

    def update_from_bridge(self, elements: list[dict], page_info: dict) -> None:
        records = self._build_records(elements)
        with self._lock:
            self._records = records
            self._inner_width = page_info.get("innerWidth")
            self._inner_height = page_info.get("innerHeight")

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._inner_width = None
            self._inner_height = None

    @property
    def inner_width(self) -> int | None:
        with self._lock:
            return self._inner_width

    @property
    def inner_height(self) -> int | None:
        with self._lock:
            return self._inner_height

    def query(self, selector: str) -> list[ElementRecord]:
        conds = _parse_selector(selector)
        with self._lock:
            return [r for r in self._records if _record_matches(r, conds)]

    def query_first(self, selector: str) -> ElementRecord | None:
        results = self.query(selector)
        return results[0] if results else None
