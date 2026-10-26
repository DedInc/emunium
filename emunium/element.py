"""Element handles and their native-input transactions.

Contract: ``docs/architecture/python-internals.md`` (Browser context, Element);
algorithm: ``docs/architecture/native-input.md`` section 4 (verify-before-click)
and section 5 (keyboard). Every bridge call addresses the handle's own tab; a
handle without a known tab is treated as stale. No click, key or button-down
is sent after a failed step. A drag whose drop cannot be verified is cancelled:
the cursor returns to the verified source point (or stays where it is when that
move fails), Escape is sent to cancel native and most scripted drags, and only
then is the button released.
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from emunium._bridge.commands import expect_dict
from emunium._bridge.facade import Bridge
from emunium._browser.context import BrowserContext
from emunium._screen import NATIVE_INPUT_LOCK, Geometry, input_per_dip
from emunium._standalone.config import ClickType
from emunium.errors import (
    ClickVerificationError,
    ContentUnavailableError,
    InvalidArgumentError,
    NativeInputError,
    StaleElementError,
    TargetClosedError,
)

__all__ = ["Element", "ElementQuery"]

logger = logging.getLogger("emunium.element")

# Random points fall inside this part of the visible rect (native-input.md 4.4).
INNER_FRACTION = (0.3, 0.7)
# A pointer sample must sit this close (DIP) to the OS cursor to describe it.
SAMPLE_TOLERANCE_DIP = 1.0
MAX_CORRECTIONS = 2
# Net-zero cursor jiggle that makes the page emit a fresh event at the point.
# Smallest step; the real step is one CSS px rounded up to whole input px.
JIGGLE_PX = 1
# Float noise allowed when rounding the input-px-per-CSS-px scale up (dpr / zoom
# * zoom need not be exactly dpr).
JIGGLE_SCALE_EPSILON = 1e-6
# Pause between the jiggle step and the move back (more than two 60 Hz frames):
# back-to-back moves are coalesced by the OS and Chrome into no event at all.
JIGGLE_SETTLE_S = 0.035
# Classic scrollbar width (CSS px) excluded on a side whose axis scrolls.
SCROLLBAR_CSS = 17.0
# Margin on every side when the document's scroll extent is unknown.
UNKNOWN_SCROLL_MARGIN_CSS = 4.0
PROBE_QUIET_MS = 40
PROBE_WAIT_MS = 300
PROBE_TIMEOUT = 10.0
TYPE_CHUNK = 8
# After type(): how long to wait for queued keystrokes to reach the page.
TYPE_SETTLE_TIMEOUT_S = 2.0
TYPE_SETTLE_POLL_S = 0.025
# A value that changed but does not end with the typed text (type=number,
# type=email, input masks) counts as settled once unchanged for this long.
TYPE_SETTLE_STABLE_S = 0.1
# Overall budget (s) for verifying the drop while the mouse button is held.
DRAG_HOLD_DEADLINE_S = 2.0
MIXED_DPI_HINT = (
    "display scaling differs between monitors; set verify_input=False or move "
    "the window to the primary display"
)

_QUERY_KINDS = ("css", "xpath", "text")
_CLICK_BUTTONS = {
    ClickType.LEFT: ("left", 1),
    ClickType.DOUBLE: ("left", 2),
    ClickType.RIGHT: ("right", 1),
    ClickType.MIDDLE: ("middle", 1),
}
_TYPE_CLICKS = (ClickType.LEFT, ClickType.DOUBLE)
_RNG = random.Random()
_monotonic = time.monotonic
_sleep = time.sleep


@dataclass(frozen=True)
class ElementQuery:
    """How a handle was found, so a stale handle can be found again.

    ``index`` is the position among all matches (document order); ``kind`` is
    ``css``, ``xpath`` or ``text`` (``exact`` applies to ``text``).
    """

    selector: str
    index: int = 0
    kind: str = "css"
    exact: bool = False

    def __post_init__(self) -> None:
        if self.kind not in _QUERY_KINDS:
            raise InvalidArgumentError(
                f"kind must be one of {_QUERY_KINDS}, got {self.kind!r}"
            )
        if isinstance(self.index, bool) or self.index < 0:
            raise InvalidArgumentError(
                f"index must be a non-negative int, got {self.index!r}"
            )


@dataclass(frozen=True)
class _Anchor:
    """Where to point: explicit CSS offsets from the rect's top-left, or
    fractions of the visible part of the rect."""

    fraction_x: float
    fraction_y: float
    offset_x: float | None = None
    offset_y: float | None = None


@dataclass(frozen=True)
class _Box:
    """Half-open CSS box ``[x0, x1) x [y0, y1)``."""

    x0: float
    y0: float
    x1: float
    y1: float


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _parse_geometry(
    geometry: Mapping[str, Any] | Geometry | None, zoom: float | None
) -> Geometry | None:
    if geometry is None or isinstance(geometry, Geometry):
        return geometry
    try:
        return Geometry.from_payload(dict(geometry), zoom)
    except ValueError:
        return None


def _axis_point(
    low: float, high: float, origin: float, offset: float | None, fraction: float
) -> float | None:
    """A coordinate inside ``[low, high)``: ``origin + offset`` or a fraction."""
    if offset is None:
        return low + fraction * (high - low)
    value = origin + offset
    return value if low <= value < high else None


def _jiggle_size(geometry: Geometry) -> int:
    """Input px per jiggle: one CSS px (``input_per_dip * zoom``) rounded up."""
    per_css = input_per_dip(geometry) * geometry.zoom
    return max(JIGGLE_PX, math.ceil(per_css - JIGGLE_SCALE_EPSILON))


class Element:
    """Element handle backed by the bridge, driven with verified OS-level input."""

    def __init__(
        self,
        ctx_or_bridge: BrowserContext | Bridge,
        element_id: str,
        tag: str = "",
        attrs: dict[str, str] | None = None,
        rect: dict[str, float] | None = None,
        text: str = "",
        selector: str | None = None,
        absolute_screen_x: float = 0,
        absolute_screen_y: float = 0,
        *,
        tab_id: int | None = None,
        zoom: float | None = None,
        geometry: Mapping[str, Any] | Geometry | None = None,
        query: ElementQuery | None = None,
        visible: bool | None = None,
    ) -> None:
        self._ctx = BrowserContext.of(ctx_or_bridge)
        self._element_id = element_id
        self._tag = tag
        self._attrs: dict[str, str] = dict(attrs or {})
        self._rect: dict[str, float] = dict(rect or {})
        self._text = text
        self._selector = selector
        self._screen_x = float(absolute_screen_x)
        self._screen_y = float(absolute_screen_y)
        self._tab_id = tab_id
        self._zoom = zoom
        self._geometry = _parse_geometry(geometry, zoom)
        self._geometry_raw: dict[str, Any] = {}
        if geometry is not None and not isinstance(geometry, Geometry):
            self._geometry_raw = dict(geometry)
        if query is None and selector:
            query = ElementQuery(selector)
        self._query = query
        self._visible_flag = visible

    @classmethod
    def from_data(
        cls,
        ctx_or_bridge: BrowserContext | Bridge,
        data: Mapping[str, Any],
        selector: str | None = None,
        *,
        query: ElementQuery | None = None,
    ) -> Element:
        element = cls(
            ctx_or_bridge,
            str(data.get("elementId", "")),
            selector=selector,
            query=query,
        )
        element._update_from_data(data)
        return element

    # -- properties ----------------------------------------------------------

    @property
    def element_id(self) -> str:
        return self._element_id

    @property
    def tag(self) -> str:
        return self._tag

    @property
    def attrs(self) -> dict[str, str]:
        return self._attrs

    @property
    def rect(self) -> dict[str, float]:
        return self._rect

    @property
    def text(self) -> str:
        return self._text

    @property
    def tab_id(self) -> int | None:
        return self._tab_id

    @property
    def zoom(self) -> float | None:
        return self._zoom

    @property
    def geometry(self) -> Geometry | None:
        return self._geometry

    @property
    def query(self) -> ElementQuery | None:
        return self._query

    @property
    def context(self) -> BrowserContext:
        return self._ctx

    @property
    def center(self) -> tuple[float, float]:
        return (
            self._rect.get("x", 0) + self._rect.get("width", 0) / 2,
            self._rect.get("y", 0) + self._rect.get("height", 0) / 2,
        )

    def _center_input(self) -> tuple[float, float]:
        if self._geometry is None:
            # No geometry in the payload: the legacy DIP estimate is all there is.
            return self._screen_x, self._screen_y
        css_x, css_y = self.center
        return self._ctx.calibration.to_input(self._geometry, css_x, css_y)

    @property
    def screen_x(self) -> float:
        """Center in INPUT units (calibrated when possible, legacy estimate otherwise)."""
        return self._center_input()[0]

    @property
    def screen_y(self) -> float:
        return self._center_input()[1]

    @property
    def visible(self) -> bool:
        if self._visible_flag is False:
            return False
        return self._rect.get("width", 0) > 0 and self._rect.get("height", 0) > 0

    # -- bridge state --------------------------------------------------------

    def _tab(self) -> int:
        """The handle's tab; without one the handle cannot be addressed safely."""
        if self._tab_id is None:
            raise StaleElementError(
                f"element handle {self._element_id!r} has no tab id, so it cannot be "
                "addressed without guessing the tab; query the element again"
            )
        return self._tab_id

    def _update_from_data(self, data: Mapping[str, Any]) -> None:
        element_id = data.get("elementId")
        if isinstance(element_id, str) and element_id:
            self._element_id = element_id
        tag = data.get("tag")
        if isinstance(tag, str):
            self._tag = tag
        attrs = data.get("attrs")
        if isinstance(attrs, dict):
            self._attrs = attrs
        self._set_rect(data.get("rect"))
        text = data.get("text")
        if isinstance(text, str):
            self._text = text
        visible = data.get("visible")
        if isinstance(visible, bool):
            self._visible_flag = visible
        screen_x = _number(data.get("absoluteScreenX"))
        screen_y = _number(data.get("absoluteScreenY"))
        if screen_x is not None:
            self._screen_x = screen_x
        if screen_y is not None:
            self._screen_y = screen_y
        tab_id = data.get("tabId")
        if isinstance(tab_id, int) and not isinstance(tab_id, bool):
            self._tab_id = tab_id
        self._set_zoom(data.get("zoom"))
        self._set_geometry(data.get("geometry"))

    def _set_rect(self, rect: Any) -> None:
        if not isinstance(rect, dict):
            return
        parsed: dict[str, float] = {}
        for key in ("x", "y", "width", "height"):
            value = _number(rect.get(key))
            if value is None:
                return
            parsed[key] = value
        self._rect = parsed

    def _set_zoom(self, zoom: Any) -> None:
        value = _number(zoom)
        if value is not None and value > 0:
            self._zoom = value

    def _set_geometry(self, geometry: Any) -> None:
        if isinstance(geometry, dict):
            parsed = _parse_geometry(geometry, self._zoom)
            if parsed is not None:
                self._geometry = parsed
                self._geometry_raw = dict(geometry)

    def refresh(self, *, requery: bool = True) -> Element:
        """Re-read the element by id; when stale, find it again by its stored query."""
        tab = self._tab()
        bridge = self._ctx.bridge
        try:
            data = bridge.describe_element(self._element_id, tab_id=tab)
        except StaleElementError:
            if not requery or self._query is None:
                raise
            data = self._requery(self._query, tab)
        self._update_from_data(data)
        return self

    def _requery(self, query: ElementQuery, tab: int) -> Mapping[str, Any]:
        bridge = self._ctx.bridge
        matches: list[dict[str, Any]]
        if query.kind == "xpath":
            matches = bridge.query_xpath(query.selector, tab_id=tab)
        elif query.kind == "text":
            matches = bridge.get_element_by_text(
                query.selector, exact=query.exact, tab_id=tab
            )
        else:
            matches = bridge.query_selector_all(query.selector, tab_id=tab)
        if query.index >= len(matches):
            raise StaleElementError(
                f"element is stale and {query.kind} query {query.selector!r} has "
                f"{len(matches)} match(es), none at index {query.index}"
            )
        match = matches[query.index]
        tag = match.get("tag")
        if self._tag and isinstance(tag, str) and tag.lower() != self._tag.lower():
            raise StaleElementError(
                f"element is stale and {query.kind} query {query.selector!r} index "
                f"{query.index} is now a <{tag}>, not the <{self._tag}> it was"
            )
        return match

    def scroll_into_view(self) -> dict[str, Any]:
        result = self._ctx.bridge.scroll_into_view(self._element_id, tab_id=self._tab())
        self._update_from_data(result)
        return result

    def _describe(self) -> None:
        data = self._ctx.bridge.describe_element(self._element_id, tab_id=self._tab())
        self._update_from_data(data)

    def _probe(
        self, since: float, budget: float | None = None
    ) -> tuple[dict[str, Any], Geometry]:
        """pointerProbe through ``request`` so geometry uses the reply's zoom.

        ``budget`` (s) caps the bridge timeout and the page-side wait.
        """
        timeout = PROBE_TIMEOUT
        wait_ms = PROBE_WAIT_MS
        if budget is not None:
            timeout = min(timeout, budget)
            wait_ms = max(0, min(wait_ms, int(budget * 1000) - PROBE_QUIET_MS))
        params = {
            "elementId": self._element_id,
            "since": since,
            "quietMs": PROBE_QUIET_MS,
            "waitMs": wait_ms,
        }
        reply = self._ctx.bridge.request(
            "pointerProbe",
            params,
            tab_id=self._tab(),
            timeout=timeout,
            idempotent=True,
        )
        probe = expect_dict("pointerProbe", reply)
        self._set_zoom(reply.zoom)
        self._set_rect(probe.get("rect"))
        self._set_geometry(probe.get("geometry"))
        return probe, self._require_geometry()

    # -- geometry ------------------------------------------------------------

    def _require_geometry(self) -> Geometry:
        if self._geometry is None:
            raise NativeInputError(
                f"element {self._element_id!r} payload carries no usable window "
                "geometry; cannot convert it to screen coordinates"
            )
        return self._geometry

    def _viewport(self, geometry: Geometry) -> _Box:
        """The part of the layout viewport a pointer event can reach the page in."""
        scroll_width = _number(self._geometry_raw.get("scrollWidth"))
        scroll_height = _number(self._geometry_raw.get("scrollHeight"))
        if scroll_width is None or scroll_height is None:
            margin = UNKNOWN_SCROLL_MARGIN_CSS
            return _Box(
                margin,
                margin,
                geometry.inner_width - margin,
                geometry.inner_height - margin,
            )
        # A vertically scrolling document has its scrollbar on the right, a
        # horizontally scrolling one at the bottom.
        right = SCROLLBAR_CSS if scroll_height > geometry.inner_height else 0.0
        bottom = SCROLLBAR_CSS if scroll_width > geometry.inner_width else 0.0
        return _Box(
            0.0, 0.0, geometry.inner_width - right, geometry.inner_height - bottom
        )

    def _visible_box(self, geometry: Geometry) -> _Box | None:
        x = self._rect.get("x", 0.0)
        y = self._rect.get("y", 0.0)
        viewport = self._viewport(geometry)
        box = _Box(
            max(x, viewport.x0),
            max(y, viewport.y0),
            min(x + self._rect.get("width", 0.0), viewport.x1),
            min(y + self._rect.get("height", 0.0), viewport.y1),
        )
        if box.x1 <= box.x0 or box.y1 <= box.y0:
            return None
        return box

    def _anchor(self, offset_x: float | None, offset_y: float | None) -> _Anchor:
        low, high = INNER_FRACTION
        return _Anchor(
            fraction_x=_RNG.uniform(low, high),
            fraction_y=_RNG.uniform(low, high),
            offset_x=offset_x,
            offset_y=offset_y,
        )

    def _css_point(self, anchor: _Anchor) -> tuple[float, float] | None:
        """The anchor inside the rect's visible part, or None when not visible."""
        box = self._visible_box(self._require_geometry())
        if box is None:
            return None
        x = _axis_point(
            box.x0, box.x1, self._rect.get("x", 0.0), anchor.offset_x, anchor.fraction_x
        )
        y = _axis_point(
            box.y0, box.y1, self._rect.get("y", 0.0), anchor.offset_y, anchor.fraction_y
        )
        if x is None or y is None:
            return None
        return x, y

    def _scroll_to_visible(self, anchor: _Anchor) -> tuple[float, float]:
        """Fresh scroll; one more scroll when the point is not visible, then fail."""
        for _ in range(2):
            self.scroll_into_view()
            point = self._css_point(anchor)
            if point is not None:
                return point
        raise NativeInputError(
            f"element {self._element_id!r} (or the requested offset) is not inside "
            f"the visible viewport after scrolling (rect {self._rect!r})"
        )

    # -- pointer transaction -------------------------------------------------

    def _begin(self) -> None:
        """Headless refusal, tab activation (native-input.md 4.1-4.2), then the
        browser window to the foreground.

        The window is matched by the handle's geometry; a handle without one is
        described first.
        """
        ctx = self._ctx
        ctx.require_native_input()
        ctx.bridge.activate_tab(tab_id=self._tab())
        if ctx.manages_foreground:
            if self._geometry is None:
                self._describe()
            ctx.ensure_foreground(self._geometry)

    def _acquire(
        self,
        anchor: _Anchor,
        point: tuple[float, float],
        *,
        human: bool,
        deadline: float | None = None,
    ) -> tuple[int, int]:
        """Move to ``point`` (CSS) and return the verified cursor position."""
        ctx = self._ctx
        input_x, input_y = ctx.calibration.to_input(
            self._require_geometry(), point[0], point[1]
        )
        ctx.interactor.move(input_x, input_y, human=human)
        if not ctx.verify_input:
            return ctx.interactor.position()
        return self._verify(anchor, deadline)

    def _jiggle_step(self, x: int, y: int, anchor: _Anchor) -> int:
        """Signed jiggle step: on the screen, off screen corners, inside the element.

        The step is one CSS px rounded up to whole input px, so the page sees a
        move even where a 1 input px step stays inside one DIP (Chrome then
        emits no event). Candidates in order: +size, -size, then +1, -1 px. A
        step that would leave the element's visible part is avoided; with an
        explicit x offset it is refused outright.
        """
        size = _jiggle_size(self._require_geometry())
        candidates = list(dict.fromkeys((size, -size, JIGGLE_PX, -JIGGLE_PX)))
        interactor = self._ctx.interactor
        landable = [step for step in candidates if interactor.can_land(x + step, y)]
        inside = [step for step in landable if self._step_stays_inside(anchor, step)]
        if inside:
            return inside[0]
        if landable and anchor.offset_x is None:
            return landable[0]
        raise NativeInputError(
            f"no safe jiggle (±{size} or ±{JIGGLE_PX} px) around Input({x}, {y}) "
            f"for element {self._element_id!r}: each neighbour is off-screen, a "
            "screen corner or outside the requested point's element"
        )

    def _step_stays_inside(self, anchor: _Anchor, step: int) -> bool:
        geometry = self._require_geometry()
        box = self._visible_box(geometry)
        point = self._css_point(anchor)
        if box is None or point is None:
            return False
        css_x = point[0] + step / (input_per_dip(geometry) * geometry.zoom)
        return box.x0 <= css_x < box.x1

    def _verify(
        self, anchor: _Anchor, deadline: float | None = None
    ) -> tuple[int, int]:
        """Prove the cursor is over this element; correct at most twice.

        Each round takes ``since`` and then jiggles the cursor by at least one
        CSS px (``_jiggle_step``) and back, so
        only an event produced at the final position can be accepted. The two
        moves are ``JIGGLE_SETTLE_S`` apart: sent back to back, the OS and
        Chrome coalesce them into a net-zero move that produces no event. With
        a ``deadline`` (monotonic s) every round, pause and probe shares that
        budget.
        """
        ctx = self._ctx
        interactor = ctx.interactor
        corrections = 0
        retried_silence = False
        samples = 0
        foreign_hits = 0
        while True:
            _remaining(deadline)
            x, y = interactor.position()
            step = self._jiggle_step(x, y, anchor)
            since = ctx.mark_action()
            interactor.move(x + step, y, human=False)
            _sleep(JIGGLE_SETTLE_S)
            interactor.move(x, y, human=False)
            probe, geometry = self._probe(since, _remaining(deadline))
            event = probe.get("event")
            position = interactor.position()
            if not isinstance(event, dict) or not self._is_fresh(event, since):
                if retried_silence:
                    raise NativeInputError(
                        "no fresh pointer event reached the page after moving and "
                        "jiggling the cursor; the window may be occluded, minimized "
                        "or off-screen"
                    )
                retried_silence = True
                continue
            samples += 1
            hit = probe.get("hit") is True
            describes = self._sample_describes(event, position, geometry)
            if describes:
                ctx.calibration.record(
                    geometry,
                    float(event["clientX"]),
                    float(event["clientY"]),
                    position[0],
                    position[1],
                )
                if hit:
                    return position
            elif hit and event.get("left") is not True:
                foreign_hits += 1
            if corrections >= MAX_CORRECTIONS:
                message = (
                    f"pointer is not verified over element {self._element_id!r} after "
                    f"{MAX_CORRECTIONS} corrections (cursor at {position!r}, sample "
                    f"{event!r}, hit={hit!r})"
                )
                if foreign_hits == samples:
                    # The page sees the element under the cursor but reports a
                    # screen point that does not match the OS cursor.
                    message += f"; {MIXED_DPI_HINT}"
                raise ClickVerificationError(message)
            corrections += 1
            self._correct(anchor, event if describes else None, position, geometry)

    def _correct(
        self,
        anchor: _Anchor,
        event: Mapping[str, Any] | None,
        position: tuple[int, int],
        geometry: Geometry,
    ) -> None:
        """Move toward the anchor using the sample's offset from it."""
        point = self._css_point(anchor)
        if point is None:
            raise ClickVerificationError(
                f"element {self._element_id!r} left the visible viewport during "
                "pointer verification"
            )
        if event is not None:
            k = input_per_dip(geometry) * geometry.zoom
            target_x = position[0] + (point[0] - float(event["clientX"])) * k
            target_y = position[1] + (point[1] - float(event["clientY"])) * k
        else:
            target_x, target_y = self._ctx.calibration.to_input(
                geometry, point[0], point[1]
            )
        logger.debug(
            "pointer missed %s; correcting to Input(%.0f, %.0f)",
            self._element_id,
            target_x,
            target_y,
        )
        self._ctx.interactor.move(target_x, target_y, human=False)

    @staticmethod
    def _is_fresh(event: Mapping[str, Any], since: float) -> bool:
        stamp = _number(event.get("time"))
        return stamp is not None and stamp >= since

    @staticmethod
    def _sample_describes(
        event: Mapping[str, Any], position: tuple[int, int], geometry: Geometry
    ) -> bool:
        """True when the sample is a live, in-document event at the OS cursor."""
        if event.get("left") is True:
            return False
        client_x = _number(event.get("clientX"))
        client_y = _number(event.get("clientY"))
        screen_x = _number(event.get("screenX"))
        screen_y = _number(event.get("screenY"))
        if client_x is None or client_y is None or screen_x is None or screen_y is None:
            return False
        scale = input_per_dip(geometry)
        return (
            abs(screen_x - position[0] / scale) <= SAMPLE_TOLERANCE_DIP
            and abs(screen_y - position[1] / scale) <= SAMPLE_TOLERANCE_DIP
        )

    def _hold(
        self,
        verified: tuple[int, int],
        anchor: _Anchor,
        deadline: float | None = None,
    ) -> tuple[int, int]:
        """Right before a button event: the cursor must still be on the verified point."""
        interactor = self._ctx.interactor
        for _ in range(MAX_CORRECTIONS):
            if interactor.position() == verified:
                return verified
            if not self._ctx.verify_input:
                break
            logger.debug("cursor left the verified point %s; re-verifying", verified)
            interactor.move(verified[0], verified[1], human=False)
            verified = self._verify(anchor, deadline)
        if interactor.position() == verified:
            return verified
        raise NativeInputError(
            f"the cursor moved away from the verified point {verified!r} (now at "
            f"{interactor.position()!r}); not sending a button event"
        )

    def _point_at(
        self, offset_x: float | None, offset_y: float | None, *, human: bool
    ) -> tuple[tuple[int, int], _Anchor]:
        """Steps 1-6: returns the verified cursor position and its anchor."""
        self._begin()
        anchor = self._anchor(offset_x, offset_y)
        point = self._scroll_to_visible(anchor)
        return self._acquire(anchor, point, human=human), anchor

    def _click(
        self,
        click_type: ClickType = ClickType.LEFT,
        *,
        human: bool = True,
        offset_x: float | None = None,
        offset_y: float | None = None,
    ) -> None:
        if click_type not in _CLICK_BUTTONS:
            raise InvalidArgumentError(f"unsupported click_type {click_type!r}")
        button, clicks = _CLICK_BUTTONS[click_type]
        with NATIVE_INPUT_LOCK:
            verified, anchor = self._point_at(offset_x, offset_y, human=human)
            verified = self._hold(verified, anchor)
            logger.info(
                "Pressing %s x%d on %s at Input%s",
                button,
                clicks,
                self._element_id,
                verified,
            )
            self._ctx.interactor.press(button, clicks=clicks)

    def hover(
        self,
        offset_x: float | None = None,
        offset_y: float | None = None,
        *,
        human: bool = True,
    ) -> None:
        """Rest the cursor on the element (offsets: CSS px from the rect's top-left)."""
        with NATIVE_INPUT_LOCK:
            self._point_at(offset_x, offset_y, human=human)

    def move_to(
        self,
        offset_x: float | None = None,
        offset_y: float | None = None,
        *,
        human: bool = True,
    ) -> None:
        self.hover(offset_x=offset_x, offset_y=offset_y, human=human)

    def click(
        self,
        *,
        human: bool = True,
        offset_x: float | None = None,
        offset_y: float | None = None,
    ) -> None:
        self._click(ClickType.LEFT, human=human, offset_x=offset_x, offset_y=offset_y)

    def double_click(
        self,
        *,
        human: bool = True,
        offset_x: float | None = None,
        offset_y: float | None = None,
    ) -> None:
        self._click(ClickType.DOUBLE, human=human, offset_x=offset_x, offset_y=offset_y)

    def right_click(
        self,
        *,
        human: bool = True,
        offset_x: float | None = None,
        offset_y: float | None = None,
    ) -> None:
        self._click(ClickType.RIGHT, human=human, offset_x=offset_x, offset_y=offset_y)

    def middle_click(
        self,
        *,
        human: bool = True,
        offset_x: float | None = None,
        offset_y: float | None = None,
    ) -> None:
        self._click(ClickType.MIDDLE, human=human, offset_x=offset_x, offset_y=offset_y)

    # -- keyboard ------------------------------------------------------------

    def _is_focused(self) -> bool:
        state = self._ctx.bridge.focus_state(self._element_id, tab_id=self._tab())
        return (
            state.get("documentHasFocus") is True
            and state.get("activeIsElement") is True
        )

    def type(
        self,
        text: str,
        *,
        characters_per_minute: int = 280,
        offset: int = 20,
        human: bool = True,
        click_type: ClickType = ClickType.LEFT,
        settle: bool = True,
    ) -> None:
        """Verified click, focus check (one ``focus`` retry), then keystrokes.

        Text goes out in chunks of ``TYPE_CHUNK`` characters; focus is checked
        again before every chunk, so keys stop the moment focus is lost. With
        ``settle`` the call then waits (up to ``TYPE_SETTLE_TIMEOUT_S``) until
        the page has received the keystrokes still queued in the OS; a timeout
        only logs a warning.
        """
        if click_type not in _TYPE_CLICKS:
            raise InvalidArgumentError(
                f"type() clicks with ClickType.LEFT or ClickType.DOUBLE, got {click_type!r}"
            )
        with NATIVE_INPUT_LOCK:
            self._click(click_type, human=human)
            if not self._is_focused():
                self.focus()
                if not self._is_focused():
                    raise NativeInputError(
                        f"element {self._element_id!r} does not hold keyboard focus "
                        f"(or the browser window has no OS focus); typed 0 of "
                        f"{len(text)} characters"
                    )
            baseline = self._typed_content() if settle and text else None
            typed = 0
            for chunk in _chunks(text, TYPE_CHUNK):
                if typed and not self._is_focused():
                    raise NativeInputError(
                        f"element {self._element_id!r} lost keyboard focus; typed "
                        f"{typed} of {len(text)} characters"
                    )
                self._ctx.interactor.type_text(
                    chunk,
                    characters_per_minute=characters_per_minute,
                    offset=offset,
                    human=human,
                )
                typed += len(chunk)
            logger.info("Typed %d chars into %s", typed, self._element_id)
            if baseline is not None:
                self._await_typed(text, baseline)

    def _typed_content(self) -> str:
        """``value`` of inputs and textareas, rendered text otherwise
        (contenteditable), with line breaks normalised to ``\\n``."""
        data = self._ctx.bridge.describe_element(self._element_id, tab_id=self._tab())
        value = data.get("value")
        if not isinstance(value, str):
            value = data.get("text")
        return _normalise_newlines(value if isinstance(value, str) else "")

    def _await_typed(self, text: str, baseline: str) -> None:
        """Poll until the page shows ``text``; on timeout warn and return.

        Settled means: the content ends with the typed text (and changed, when
        it already ended with it before typing), or it changed and then stayed
        unchanged for ``TYPE_SETTLE_STABLE_S`` (inputs that transform text).
        """
        expected = _normalise_newlines(text)
        already_there = baseline.endswith(expected)
        deadline = _monotonic() + TYPE_SETTLE_TIMEOUT_S
        last: str | None = None
        last_change = 0.0
        while True:
            try:
                content = self._typed_content()
            except (StaleElementError, ContentUnavailableError, TargetClosedError):
                # The keys were sent; Enter may have submitted and navigated.
                logger.info(
                    "element %s went away while typed text settled", self._element_id
                )
                return
            now = _monotonic()
            if content != last:
                last, last_change = content, now
            changed = content != baseline
            if content.endswith(expected) and (changed or not already_there):
                return
            if changed and now - last_change >= TYPE_SETTLE_STABLE_S:
                return
            if now >= deadline:
                logger.warning(
                    "typed text did not reach element %s within %g s (%d chars sent)",
                    self._element_id,
                    TYPE_SETTLE_TIMEOUT_S,
                    len(text),
                )
                return
            _sleep(TYPE_SETTLE_POLL_S)

    # -- drag ----------------------------------------------------------------

    def drag_to(self, target: Element, *, human: bool = True) -> None:
        """Press on this element and release on ``target`` (same context and tab).

        The target is hovered and verified first, then the source; the button
        goes down on the verified source, and the target is verified again
        while the button is held, within ``DRAG_HOLD_DEADLINE_S``. Verification
        reads the same probe result shape whether the page sampled pointer
        events or drag events (native HTML5 drags emit only the latter).

        On any failure after the button went down the drag is cancelled: the
        cursor returns to the verified source point (if that move fails it
        stays where it is), Escape is sent, then the button is released. The
        release may then happen over an unverified point, but the drag was
        cancelled first; pages that ignore Escape can still see a drop there.
        """
        if target._ctx is not self._ctx:
            raise InvalidArgumentError(
                "drag target belongs to a different browser context"
            )
        tab = self._tab()
        if target._tab() != tab:
            raise NativeInputError(
                f"cannot drag between tabs {tab} and {target.tab_id}"
            )
        ctx = self._ctx
        interactor = ctx.interactor
        with NATIVE_INPUT_LOCK:
            self._begin()
            self.scroll_into_view()
            target.scroll_into_view()
            # Scrolling the target can move the source: resolve both again.
            self._describe()
            target._describe()
            source_anchor = self._anchor(None, None)
            target_anchor = target._anchor(None, None)
            source_point = self._css_point(source_anchor)
            target_point = target._css_point(target_anchor)
            if source_point is None or target_point is None:
                raise NativeInputError(
                    "drag source and target are not both inside the visible viewport"
                )
            if ctx.verify_input:
                target._acquire(target_anchor, target_point, human=human)
            start = self._acquire(source_anchor, source_point, human=human)
            start = self._hold(start, source_anchor)
            try:
                interactor.button_down("left")
                deadline = (
                    _monotonic() + DRAG_HOLD_DEADLINE_S if ctx.verify_input else None
                )
                end = target._drop_point(target_anchor, human=human, deadline=deadline)
            except BaseException as exc:
                cleanup_error = self._abort_drag(start)
                if cleanup_error is not None and not isinstance(
                    cleanup_error, Exception
                ):
                    # An interrupt during cleanup wins, after cleanup finished.
                    raise cleanup_error from exc
                raise
            interactor.button_up("left")
            logger.info("Dragged from Input%s to Input%s", start, end)

    def _drop_point(
        self, anchor: _Anchor, *, human: bool, deadline: float | None
    ) -> tuple[int, int]:
        """With the button held: move onto this (target) element and verify it."""
        point = self._css_point(anchor)
        if point is None:
            raise ClickVerificationError(
                f"drag target {self._element_id!r} is no longer visible"
            )
        end = self._acquire(anchor, point, human=human, deadline=deadline)
        return self._hold(end, anchor, deadline)

    def _abort_drag(self, start: tuple[int, int]) -> BaseException | None:
        """Cancel a held drag: back to the start, Escape, then release.

        Every step runs even when an earlier one fails or is interrupted (the
        release then happens wherever the cursor is). Returns the first error
        a step raised so the caller can re-raise interrupts after cleanup.
        """
        interactor = self._ctx.interactor
        steps = (
            (
                "return the cursor to the drag start",
                lambda: interactor.move(start[0], start[1], human=False),
            ),
            ("send Escape", interactor.press_escape),
            ("release the mouse button", lambda: interactor.button_up("left")),
        )
        first: BaseException | None = None
        for label, step in steps:
            try:
                step()
            except BaseException as exc:
                logger.warning("drag abort: could not %s", label, exc_info=True)
                if first is None:
                    first = exc
        return first

    # -- plain bridge reads ----------------------------------------------------

    def focus(self) -> dict[str, Any]:
        return self._ctx.bridge.focus(self._element_id, tab_id=self._tab())

    def get_attribute(self, name: str) -> str | None:
        return self._ctx.bridge.get_attribute(
            self._element_id, name, tab_id=self._tab()
        )

    def get_computed_style(self, prop: str) -> str | None:
        return self._ctx.bridge.get_computed_style(
            self._element_id, prop, tab_id=self._tab()
        )

    def __repr__(self) -> str:
        return (
            f"Element(id={self._element_id!r}, tab={self._tab_id!r}, "
            f"tag={self._tag!r}, rect={self._rect!r})"
        )


def _remaining(deadline: float | None) -> float | None:
    """Seconds left before ``deadline`` (monotonic); raises once it has passed."""
    if deadline is None:
        return None
    remaining = deadline - _monotonic()
    if remaining <= 0:
        raise ClickVerificationError(
            f"drop point not verified within the {DRAG_HOLD_DEADLINE_S:g} s "
            "held-button deadline"
        )
    return remaining


def _normalise_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _chunks(text: str, size: int) -> list[str]:
    """Split ``text`` into ``size``-character chunks without splitting ``\\r\\n``."""
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if text[end - 1] == "\r" and text[end : end + 1] == "\n":
            end += 1
        chunks.append(text[start:end])
        start = end
    return chunks
