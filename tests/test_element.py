"""Element transactions (native-input.md section 4/5) and dom lookups.

Everything runs against a fake bridge and a fake interactor that share one
fake "world": the page's real viewport origin on screen, element rects, and
the pointer samples the page would report for the fake cursor position. No OS
input is sent (the conftest safety net traps the real entry points), and the
fake interactor asserts that ``NATIVE_INPUT_LOCK`` is held for every event.
"""

from __future__ import annotations

import gc
import math
import random
import subprocess
import sys
import threading
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from emunium import _screen as screen_module
from emunium import element as element_module
from emunium._bridge.facade import Bridge
from emunium._bridge.transport import PINNED, Reply
from emunium._browser import context as context_module
from emunium._browser import dom
from emunium._browser import facade as facade_module
from emunium._browser import tabs as tabs_module
from emunium._browser.context import BrowserContext, DragInteractor
from emunium._native.win32 import CHROME_WINDOW_CLASS
from emunium._screen import (
    NATIVE_INPUT_LOCK,
    Calibration,
    Geometry,
    estimate_to_input,
    input_per_dip,
)
from emunium._standalone.config import ClickType
from emunium.element import MIXED_DPI_HINT, Element, ElementQuery
from emunium.errors import (
    BridgeDisconnectedError,
    ClickVerificationError,
    ContentUnavailableError,
    InvalidArgumentError,
    NativeInputError,
    StaleElementError,
    WaitTimeoutError,
)

TAB = 7
# Estimated viewport origin for this geometry: (0, 800 - 700) = (0, 100).
GEOMETRY: dict[str, Any] = {
    "screenX": 0,
    "screenY": 0,
    "outerWidth": 1000,
    "outerHeight": 800,
    "innerWidth": 1000,
    "innerHeight": 700,
    "devicePixelRatio": 1.0,
}
# Largest horizontal non-human step the fake interactor logs as a jiggle.
MAX_JIGGLE_PX = 3
OK_FOCUS = {"documentHasFocus": True, "activeIsElement": True}
LOST_FOCUS = {"documentHasFocus": True, "activeIsElement": False}


def lock_held_by_someone() -> bool:
    """True when another thread cannot take ``NATIVE_INPUT_LOCK`` right now."""
    acquired: list[bool] = []

    def try_lock() -> None:
        got = NATIVE_INPUT_LOCK.acquire(blocking=False)
        if got:
            NATIVE_INPUT_LOCK.release()
        acquired.append(got)

    thread = threading.Thread(target=try_lock)
    thread.start()
    thread.join()
    return acquired == [False]


class World:
    """Shared state of the fake page, fake bridge and fake cursor."""

    def __init__(self) -> None:
        self.log: list[tuple[Any, ...]] = []
        self.cursor = (500, 500)
        self.geometry: dict[str, Any] = dict(GEOMETRY)
        self.scroll_zoom = 1.0  # zoom in element payloads
        self.zoom = 1.0  # live zoom, reported in pointerProbe replies
        self.origin_error = (0.0, 0.0)  # INPUT px the window-chrome estimate is off
        self.rects: dict[str, dict[str, float]] = {}
        self.tags: dict[str, str] = {}
        self.stale: set[str] = set()
        self.covered: set[str] = set()
        self.selectors: dict[str, list[str]] = {}
        self.silent = False
        self.lag_ms: float | None = None
        self.mangle: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        self.on_scroll: dict[str, Callable[[], None]] = {}
        self.after_move: Callable[[bool], None] | None = None
        self.after_probe: Callable[[int], None] | None = None
        self.after_button_down: Callable[[], None] | None = None
        self.focus_states: list[dict[str, Any]] = []
        self.wait_result: Any = None
        self.errors: dict[str, Exception] = {}
        self.drop_geometry = False
        self.probes: list[dict[str, Any]] = []
        self.lock_checks = 0
        self.virtual_screen = (0, 0, 1920, 1080)
        self.blocked: set[tuple[int, int]] = set()  # e.g. screen corners
        self.move_failures: dict[tuple[int, int], BaseException] = {}
        self.button_down_error: BaseException | None = None
        self.sample_type: str | None = None  # e.g. "dragover" for native DnD
        self.clock = 0.0
        self.probe_cost = 0.0  # seconds a probe takes on the fake clock
        self.probe_budgets: list[tuple[float, int]] = []  # (timeout, waitMs)
        # Typing: keys queue in ``typed_queue`` and reach ``typing_target`` (its
        # ``values`` entry when it has one, else its ``texts`` entry) on reads.
        self.values: dict[str, str] = {}
        self.texts: dict[str, str] = {}
        self.typing_target = "doc1.1"
        self.typed_queue = ""
        self.reads_before_delivery = 0  # describeElement reads that see nothing
        self.deliver_per_read: int | None = None  # chars per read; None = all
        self.transform: Callable[[str], str] | None = None  # e.g. type=number
        self.after_type: Callable[[], None] | None = None
        # Like the OS and Chrome: two jiggle moves with no pause between them
        # merge into a net-zero move, so the probe sees no fresh event.
        self.coalesce = False
        # Like Chrome at a fractional scale: a jiggle that stays inside one CSS
        # px column produces no pointer event.
        self.css_px_events = False

    def deliver_typed(self) -> None:
        if not self.typed_queue:
            return
        if self.reads_before_delivery > 0:
            self.reads_before_delivery -= 1
            return
        count = self.deliver_per_read or len(self.typed_queue)
        chunk, self.typed_queue = self.typed_queue[:count], self.typed_queue[count:]
        target = self.typing_target
        if target in self.values:
            value = self.values[target] + chunk.replace("\r\n", "\n")
            self.values[target] = (
                value if self.transform is None else self.transform(value)
            )
        else:
            self.texts[target] = self.texts.get(target, "label") + chunk

    def sleeps(self) -> list[float]:
        return [entry[1] for entry in self.kinds("sleep")]

    def add(self, element_id: str, x: float, y: float, w: float, h: float) -> None:
        self.rects[element_id] = {"x": x, "y": y, "width": w, "height": h}

    def payload(self, element_id: str) -> dict[str, Any]:
        rect = dict(self.rects[element_id])
        payload: dict[str, Any] = {
            "elementId": element_id,
            "tag": self.tags.get(element_id, "button"),
            "attrs": {"id": element_id},
            "text": self.texts.get(element_id, "label"),
            "value": self.values.get(element_id),
            "visible": True,
            "rect": rect,
            "geometry": dict(self.geometry),
            "absoluteScreenX": rect["x"] + 11,
            "absoluteScreenY": rect["y"] + 22,
            "tabId": TAB,
            "zoom": self.scroll_zoom,
        }
        if self.drop_geometry:
            del payload["geometry"]
        return payload

    def live(self) -> Geometry:
        return Geometry.from_payload(self.geometry, self.zoom)

    def client_point(self, position: tuple[int, int]) -> tuple[float, float]:
        live = self.live()
        k = input_per_dip(live) * live.zoom
        origin_x, origin_y = estimate_to_input(live, 0, 0)
        origin_x += self.origin_error[0]
        origin_y += self.origin_error[1]
        return (position[0] - origin_x) / k, (position[1] - origin_y) / k

    def inside(self, element_id: str, x: float, y: float) -> bool:
        rect = self.rects[element_id]
        return (
            rect["x"] <= x <= rect["x"] + rect["width"]
            and rect["y"] <= y <= rect["y"] + rect["height"]
        )

    def input_inside(self, element_id: str, position: tuple[int, int]) -> bool:
        return self.inside(element_id, *self.client_point(position))

    def shift_all(self, dy: float) -> None:
        for rect in self.rects.values():
            rect["y"] += dy

    def kinds(self, kind: str) -> list[tuple[Any, ...]]:
        return [entry for entry in self.log if entry[0] == kind]

    def bridge_methods(self) -> list[str]:
        return [entry[1] for entry in self.kinds("bridge")]

    def presses(self) -> list[tuple[Any, ...]]:
        return self.kinds("press")

    def moves(self) -> list[tuple[Any, ...]]:
        """Aiming moves (jiggles are logged as ``jiggle``)."""
        return self.kinds("move")

    def sequence(self) -> list[str]:
        return [entry[1] if entry[0] == "bridge" else entry[0] for entry in self.log]


class FakeInteractor:
    def __init__(self, world: World) -> None:
        self.world = world
        self._jiggle_from: tuple[int, int] | None = None

    def _check_lock(self) -> None:
        assert lock_held_by_someone(), "OS event sent without NATIVE_INPUT_LOCK"
        self.world.lock_checks += 1

    def position(self) -> tuple[int, int]:
        return self.world.cursor

    def move(self, x: float, y: float, *, human: bool = True) -> None:
        self._check_lock()
        current = self.world.cursor
        target = (round(x), round(y))
        failure = self.world.move_failures.get(target)
        if failure is not None:
            raise failure
        if (
            not human
            and target[1] == current[1]
            and 0 < abs(target[0] - current[0]) <= MAX_JIGGLE_PX
        ):
            kind, self._jiggle_from = "jiggle", current
        elif not human and target == self._jiggle_from:
            kind, self._jiggle_from = "jiggle", None
        else:
            kind, self._jiggle_from = "move", None
        self.world.cursor = target
        self.world.log.append((kind, target, human))
        if self.world.after_move is not None:
            self.world.after_move(human)

    def press(self, button: str = "left", *, clicks: int = 1) -> None:
        self._check_lock()
        self.world.log.append(("press", button, clicks, self.world.cursor))

    def button_down(self, button: str = "left") -> None:
        self._check_lock()
        if self.world.button_down_error is not None:
            raise self.world.button_down_error
        self.world.log.append(("down", button, self.world.cursor))
        if self.world.after_button_down is not None:
            self.world.after_button_down()

    def button_up(self, button: str = "left") -> None:
        self._check_lock()
        self.world.log.append(("up", button, self.world.cursor))

    def press_escape(self) -> None:
        self._check_lock()
        self.world.log.append(("escape", self.world.cursor))

    def can_land(self, x: int, y: int) -> bool:
        left, top, width, height = self.world.virtual_screen
        on_screen = left <= x < left + width and top <= y < top + height
        return on_screen and (x, y) not in self.world.blocked

    def type_text(
        self,
        text: str,
        *,
        characters_per_minute: int = 280,
        offset: int = 20,
        human: bool = True,
    ) -> None:
        self._check_lock()
        self.world.log.append(("type", text, characters_per_minute, offset, human))
        self.world.typed_queue += text
        if self.world.after_type is not None:
            self.world.after_type()


class FakeBridge(Bridge):
    """Bridge whose methods answer from the world instead of a transport."""

    def __init__(self, world: World) -> None:
        super().__init__(transport=object())
        self.world = world

    def _record(self, method: str, element_id: str | None, tab_id: Any) -> None:
        self.world.log.append(("bridge", method, element_id, tab_id))
        error = self.world.errors.get(method)
        if error is not None:
            raise error
        if element_id is not None and element_id in self.world.stale:
            raise StaleElementError(f"{element_id} is stale", method=method)

    def _matches(self, key: str) -> list[dict[str, Any]]:
        return [
            self.world.payload(i)
            for i in self.world.selectors.get(key, [])
            if i not in self.world.stale
        ]

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        tab_id: Any = PINNED,
        timeout: float = 30.0,
        idempotent: bool = False,
    ) -> Reply:
        assert method == "pointerProbe" and params is not None and idempotent
        element_id = params["elementId"]
        self._record(method, element_id, tab_id)
        world = self.world
        merged = world.coalesce and [e[0] for e in world.log[-3:-1]] == [
            "jiggle",
            "jiggle",
        ]
        if world.css_px_events:
            out, back = [e[1] for e in world.kinds("jiggle")][-2:]
            out_column = math.floor(world.client_point(out)[0])
            merged = merged or out_column == math.floor(world.client_point(back)[0])
        world.probe_budgets.append((timeout, params["waitMs"]))
        world.clock += world.probe_cost
        result: dict[str, Any] = {
            "event": None,
            "hit": False,
            "rect": dict(world.rects[element_id]),
            "geometry": dict(world.geometry),
        }
        if not world.silent and not merged:
            position = world.cursor
            client_x, client_y = world.client_point(position)
            scale = input_per_dip(world.live())
            since = params["since"]
            event: dict[str, Any] = {
                "clientX": client_x,
                "clientY": client_y,
                "screenX": position[0] / scale,
                "screenY": position[1] / scale,
                "time": since + 5 if world.lag_ms is None else since - world.lag_ms,
            }
            if world.sample_type is not None:
                event["type"] = world.sample_type
            if world.mangle is not None:
                event = world.mangle(event)
            result["event"] = event
            result["hit"] = (
                event.get("left") is not True
                and element_id not in world.covered
                and world.inside(element_id, client_x, client_y)
            )
        world.probes.append(result)
        if world.after_probe is not None:
            world.after_probe(len(world.probes))
        return Reply(result=result, tab_id=TAB, zoom=world.zoom)

    def query_selector(
        self, selector: str, timeout: float = 10.0, *, tab_id: Any = PINNED
    ) -> dict[str, Any] | None:
        self._record("querySelector", None, tab_id)
        matches = self._matches(selector)
        return matches[0] if matches else None

    def query_selector_all(
        self, selector: str, timeout: float = 10.0, *, tab_id: Any = PINNED
    ) -> list[dict[str, Any]]:
        self._record("querySelectorAll", None, tab_id)
        return self._matches(selector)

    def query_xpath(
        self, xpath: str, timeout: float = 10.0, *, tab_id: Any = PINNED
    ) -> list[dict[str, Any]]:
        self._record("queryXPath", None, tab_id)
        return self._matches(xpath)

    def get_element_by_text(
        self,
        text: str,
        exact: bool = False,
        timeout: float = 10.0,
        *,
        tab_id: Any = PINNED,
    ) -> list[dict[str, Any]]:
        self._record("queryByText", None, tab_id)
        return self._matches(f"{text}|exact={exact}")

    def get_all_interactive(
        self, timeout: float = 10.0, *, tab_id: Any = PINNED
    ) -> list[dict[str, Any]]:
        self._record("getAllInteractive", None, tab_id)
        return [self.world.payload(i) for i in self.world.rects]

    def describe_element(
        self, element_id: str, timeout: float = 10.0, *, tab_id: Any = PINNED
    ) -> dict[str, Any]:
        self._record("describeElement", element_id, tab_id)
        self.world.deliver_typed()
        return self.world.payload(element_id)

    def scroll_into_view(
        self, element_id: str, timeout: float = 10.0, *, tab_id: Any = PINNED
    ) -> dict[str, Any]:
        self._record("scrollIntoView", element_id, tab_id)
        hook = self.world.on_scroll.get(element_id)
        if hook is not None:
            hook()
        return self.world.payload(element_id)

    def wait_for_selector(
        self,
        selector: str,
        type: str = "css",
        timeout: float = 10.0,
        state: str | None = None,
        conditions: list[dict[str, Any]] | None = None,
        *,
        tab_id: Any = PINNED,
    ) -> dict[str, Any]:
        self._record("waitForSelector", None, tab_id)
        if isinstance(self.world.wait_result, Exception):
            raise self.world.wait_result
        return self.world.wait_result

    def focus(
        self, element_id: str, timeout: float = 10.0, *, tab_id: Any = PINNED
    ) -> dict[str, Any]:
        self._record("focus", element_id, tab_id)
        return {"focused": True}

    def focus_state(
        self, element_id: str, timeout: float = 10.0, *, tab_id: Any = PINNED
    ) -> dict[str, Any]:
        self._record("focusState", element_id, tab_id)
        return self.world.focus_states.pop(0)

    def get_attribute(
        self,
        element_id: str,
        name: str,
        timeout: float = 10.0,
        *,
        tab_id: Any = PINNED,
    ) -> str | None:
        self._record("getAttribute", element_id, tab_id)
        return "value"

    def get_computed_style(
        self,
        element_id: str,
        prop: str,
        timeout: float = 10.0,
        *,
        tab_id: Any = PINNED,
    ) -> str | None:
        self._record("getComputedStyle", element_id, tab_id)
        return "block"

    def activate_tab(
        self, timeout: float = 10.0, *, tab_id: Any = PINNED
    ) -> dict[str, Any]:
        self._record("activateTab", None, tab_id)
        return {"tabId": tab_id, "windowId": 1, "activated": True}


@pytest.fixture(autouse=True)
def seeded_rng(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(element_module, "_RNG", random.Random(3))


@pytest.fixture(autouse=True)
def no_real_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """A context that would reach the real window API fails the test instead."""

    def refuse() -> None:
        raise AssertionError("unit test reached the real window API")

    monkeypatch.setattr(context_module, "Win32Windows", refuse)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    w = World()
    w.add("doc1.1", 100, 60, 100, 20)
    monkeypatch.setattr(element_module, "_monotonic", lambda: w.clock)

    def fake_sleep(seconds: float) -> None:
        w.log.append(("sleep", seconds))
        w.clock += seconds

    monkeypatch.setattr(element_module, "_sleep", fake_sleep)
    return w


def make_ctx(world: World, **kwargs: Any) -> BrowserContext:
    return BrowserContext(FakeBridge(world), interactor=FakeInteractor(world), **kwargs)


def handle(ctx: BrowserContext, element_id: str = "doc1.1") -> Element:
    bridge = ctx.bridge
    assert isinstance(bridge, FakeBridge)
    return Element.from_data(ctx, bridge.world.payload(element_id))


def pressed_inside(world: World, element_id: str) -> bool:
    return world.input_inside(element_id, world.presses()[-1][3])


# -- click transaction ----------------------------------------------------------


def test_click_follows_the_transaction_order(world: World) -> None:
    ctx = make_ctx(world)
    handle(ctx).click()

    assert world.sequence() == [
        "activateTab",
        "scrollIntoView",
        "move",
        "jiggle",
        "sleep",
        "jiggle",
        "pointerProbe",
        "press",
    ]
    assert world.sleeps() == [element_module.JIGGLE_SETTLE_S]
    assert world.presses() == [("press", "left", 1, world.cursor)]
    assert pressed_inside(world, "doc1.1")
    assert ctx.calibration.is_calibrated(world.live())


def test_os_events_are_sent_under_the_native_input_lock(world: World) -> None:
    ctx = make_ctx(world)
    world.focus_states = [dict(OK_FOCUS)]
    handle(ctx).type("abc")
    unlocked = len(world.kinds("bridge")) + len(world.kinds("sleep"))
    assert world.lock_checks == len(world.log) - unlocked
    assert world.lock_checks >= 5

    with pytest.raises(AssertionError, match="NATIVE_INPUT_LOCK"):
        FakeInteractor(world).move(1, 1)  # the checker itself catches a bare call


def test_every_bridge_call_uses_the_handle_tab(world: World) -> None:
    _list_world(world, "doc1")
    world.add("src", 100, 300, 100, 40)
    world.add("dst", 600, 400, 100, 40)
    world.focus_states = [dict(OK_FOCUS)]
    ctx = make_ctx(world)
    el = handle(ctx)
    el.type("hi")
    el.get_attribute("id")
    el.get_computed_style("display")
    el.focus()
    el.refresh()
    item = dom.query_selector_all(ctx, ".item")[1]
    world.stale.add(item.element_id)
    world.selectors[".item"] = ["doc1.1", "doc1.3"]
    item.refresh()  # requery by index
    handle(ctx, "src").drag_to(handle(ctx, "dst"))

    methods = set(world.bridge_methods())
    assert {"pointerProbe", "focusState", "getComputedStyle", "focus"} <= methods
    assert {"querySelectorAll", "describeElement", "scrollIntoView"} <= methods
    element_calls = [e for e in world.kinds("bridge") if e[1] != "querySelectorAll"]
    assert {entry[3] for entry in element_calls} == {TAB}
    requery = [e for e in world.kinds("bridge") if e[1] == "querySelectorAll"]
    assert requery[-1][3] == TAB


def test_stale_element_aborts_without_input(world: World) -> None:
    ctx = make_ctx(world)
    el = handle(ctx)
    world.stale.add("doc1.1")

    with pytest.raises(StaleElementError):
        el.click()
    assert world.moves() == [] and world.presses() == []


def test_stale_during_probe_aborts_before_press(world: World) -> None:
    ctx = make_ctx(world)
    el = handle(ctx)
    world.after_move = lambda human: world.stale.add("doc1.1")

    with pytest.raises(StaleElementError):
        el.click()
    assert world.presses() == []


def test_miss_is_corrected_from_the_sample_then_pressed(world: World) -> None:
    world.origin_error = (0.0, 40.0)  # 40 px of window chrome the estimate misses
    ctx = make_ctx(world)
    handle(ctx).click()

    moves = world.moves()
    assert len(moves) == 2
    assert moves[0][2] is True and moves[1][2] is False  # correction moves directly
    assert len(world.probes) == 2
    assert len(world.presses()) == 1 and pressed_inside(world, "doc1.1")
    assert ctx.calibration.to_input(world.live(), 0, 0) == pytest.approx((0.0, 140.0))


def test_persistent_miss_raises_after_two_corrections(world: World) -> None:
    world.covered.add("doc1.1")
    ctx = make_ctx(world)

    with pytest.raises(ClickVerificationError) as info:
        handle(ctx).click()
    assert MIXED_DPI_HINT not in str(info.value)
    assert len(world.moves()) == 3 and len(world.probes) == 3
    assert world.presses() == []


def test_no_fresh_sample_raises_native_input_error(world: World) -> None:
    world.silent = True
    ctx = make_ctx(world)

    with pytest.raises(NativeInputError) as info:
        handle(ctx).click()
    assert not isinstance(info.value, ClickVerificationError)
    assert len(world.probes) == 2 and len(world.kinds("jiggle")) == 4
    assert world.presses() == []


def test_sample_after_second_jiggle_continues(world: World) -> None:
    world.silent = True

    def wake(count: int) -> None:
        world.silent = False

    world.after_probe = wake
    ctx = make_ctx(world)
    handle(ctx).click()
    assert len(world.presses()) == 1 and pressed_inside(world, "doc1.1")


def test_tiny_element_lagging_hit_is_not_accepted(world: World) -> None:
    world.add("tiny", 300, 200, 6, 6)
    world.lag_ms = 30.0  # the page reports an event from before the jiggle
    ctx = make_ctx(world)

    with pytest.raises(NativeInputError):
        handle(ctx, "tiny").click()
    assert world.probes and all(p["hit"] is True for p in world.probes)
    assert world.presses() == []


def test_tiny_element_with_fresh_sample_is_pressed(world: World) -> None:
    world.add("tiny", 300, 200, 6, 6)
    ctx = make_ctx(world)
    handle(ctx, "tiny").click()
    assert pressed_inside(world, "tiny")


def test_hit_with_foreign_screen_point_hints_mixed_dpi(world: World) -> None:
    def far_away(event: dict[str, Any]) -> dict[str, Any]:
        return dict(event, screenX=event["screenX"] + 50)

    world.mangle = far_away
    ctx = make_ctx(world)

    with pytest.raises(ClickVerificationError) as info:
        handle(ctx).click()
    assert MIXED_DPI_HINT in str(info.value)
    assert world.presses() == []
    assert not ctx.calibration.is_calibrated(world.live())


@pytest.mark.parametrize(("delta", "accepted"), [(0.8, True), (1.5, False)])
def test_screen_y_tolerance_is_one_dip(
    world: World, delta: float, accepted: bool
) -> None:
    def shift_y(event: dict[str, Any]) -> dict[str, Any]:
        return dict(event, screenY=event["screenY"] + delta)

    world.mangle = shift_y
    ctx = make_ctx(world)
    if accepted:
        handle(ctx).click()
        assert len(world.presses()) == 1 and len(world.probes) == 1
    else:
        with pytest.raises(ClickVerificationError):
            handle(ctx).click()
        assert world.presses() == []


def test_left_sample_is_a_miss(world: World) -> None:
    def left(event: dict[str, Any]) -> dict[str, Any]:
        return dict(event, left=True)

    world.mangle = left
    ctx = make_ctx(world)

    with pytest.raises(ClickVerificationError):
        handle(ctx).click()
    assert world.presses() == []


def test_high_dpr_click_is_verified(world: World) -> None:
    world.geometry["devicePixelRatio"] = 1.5
    ctx = make_ctx(world)
    handle(ctx).click()

    assert len(world.probes) == 1 and pressed_inside(world, "doc1.1")
    assert input_per_dip(world.live()) in (1.0, 1.5)  # 1.0 on macOS (points)


def test_probe_geometry_uses_the_reply_zoom(world: World) -> None:
    # Zoomed to 125 % after the handle was made: scroll payloads still say 1.0.
    world.geometry.update(devicePixelRatio=1.25, innerWidth=800, innerHeight=560)
    world.zoom = 1.25
    ctx = make_ctx(world)
    el = handle(ctx)
    el.click()

    assert pressed_inside(world, "doc1.1")
    assert el.zoom == 1.25
    assert el.geometry is not None and el.geometry.zoom == 1.25


def test_headless_refuses_before_any_command(world: World) -> None:
    ctx = make_ctx(world, headless=True)

    with pytest.raises(NativeInputError):
        handle(ctx).click()
    assert world.log == []


def test_verify_input_false_skips_the_probe(world: World) -> None:
    ctx = make_ctx(world, verify_input=False)
    handle(ctx).right_click()

    assert world.probes == [] and world.kinds("jiggle") == []
    assert world.sequence()[-2:] == ["move", "press"]
    assert world.presses()[0][1:3] == ("right", 1)


def test_double_click_is_one_move_and_two_presses(world: World) -> None:
    ctx = make_ctx(world)
    handle(ctx).double_click()

    assert len(world.moves()) == 1
    assert world.presses() == [("press", "left", 2, world.cursor)]


def test_cursor_moved_before_press_is_re_verified(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_ctx(world)
    real_verify = Element._verify
    verified: list[tuple[int, int]] = []

    def verify_then_user_moves(
        self: Element, anchor: Any, deadline: float | None = None
    ) -> tuple[int, int]:
        position = real_verify(self, anchor, deadline)
        verified.append(position)
        if len(verified) == 1:
            world.cursor = (5, 5)  # the user nudged the mouse
        return position

    monkeypatch.setattr(Element, "_verify", verify_then_user_moves)
    handle(ctx).click()

    assert len(verified) == 2 and len(world.probes) == 2
    assert world.presses() == [("press", "left", 1, verified[0])]


def test_point_is_inside_the_central_part(world: World) -> None:
    ctx = make_ctx(world, verify_input=False)
    for _ in range(20):
        handle(ctx).hover(human=False)
        x, y = world.cursor
        assert 100 + 30 <= x <= 100 + 70
        assert 100 + 60 + 6 <= y <= 100 + 60 + 14
    assert world.presses() == []


def test_explicit_offset(world: World) -> None:
    ctx = make_ctx(world)
    handle(ctx).click(offset_x=5, offset_y=4)
    assert world.presses()[-1][3] == (105, 164)


def jiggles(world: World) -> list[tuple[int, int]]:
    return [entry[1] for entry in world.kinds("jiggle")]


@pytest.mark.parametrize("blocker", ["screen_edge", "corner"])
def test_jiggle_steps_left_when_right_is_unsafe(world: World, blocker: str) -> None:
    if blocker == "screen_edge":
        world.virtual_screen = (0, 0, 106, 1080)  # x = 106 is off every monitor
    else:
        world.blocked.add((106, 164))
    ctx = make_ctx(world)
    handle(ctx).click(offset_x=5, offset_y=4)

    assert jiggles(world) == [(104, 164), (105, 164)]
    assert world.presses()[-1][3] == (105, 164)


def test_jiggle_stays_inside_at_an_explicit_right_edge(world: World) -> None:
    ctx = make_ctx(world)
    handle(ctx).click(offset_x=99.6, offset_y=4)  # +1 px would leave the element
    assert jiggles(world) == [(199, 164), (200, 164)]
    assert pressed_inside(world, "doc1.1")


def test_jiggle_takes_since_first_and_pauses_between_steps(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_ctx(world)
    real_mark = ctx.mark_action

    def mark() -> float:
        world.log.append(("since",))
        return real_mark()

    monkeypatch.setattr(ctx, "mark_action", mark)
    handle(ctx).click()

    steps = [s for s in world.sequence() if s != "move"]
    assert steps[steps.index("since") :] == [
        "since",
        "jiggle",
        "sleep",
        "jiggle",
        "pointerProbe",
        "press",
    ]
    assert world.sleeps() == [0.035] and element_module.JIGGLE_SETTLE_S > 2 / 60


@pytest.mark.parametrize("human", [True, False])
def test_click_survives_move_coalescing(world: World, human: bool) -> None:
    world.coalesce = True  # back-to-back jiggle moves would produce no event
    ctx = make_ctx(world)
    handle(ctx).click(human=human)
    assert len(world.probes) == 1 and pressed_inside(world, "doc1.1")


def test_no_safe_jiggle_at_an_explicit_edge_is_refused(world: World) -> None:
    world.blocked.add((199, 164))
    ctx = make_ctx(world)
    with pytest.raises(NativeInputError, match="jiggle"):
        handle(ctx).click(offset_x=99.6, offset_y=4)
    assert world.presses() == [] and world.probes == []


def _scale(
    world: World, monkeypatch: pytest.MonkeyPatch, dpr: float, zoom: float
) -> float:
    """Set the page scale (physical input px, as on Windows); return input px per CSS px."""
    monkeypatch.setattr(screen_module, "_platform", lambda: "win32")
    # outer == inner * zoom: no border, origin (0, outerHeight - innerHeight * zoom).
    world.geometry.update(
        devicePixelRatio=dpr,
        outerWidth=1200,
        innerWidth=1200 / zoom,
        outerHeight=900,
        innerHeight=600 / zoom,
    )
    world.zoom = world.scroll_zoom = zoom
    return input_per_dip(world.live()) * zoom


def _column_offset(column: int, per_css: float) -> float:
    """CSS x offset into doc1.1 (x = 100) that puts the cursor on ``column``."""
    return (column + 0.25) / per_css - 100


@pytest.mark.parametrize(
    ("dpr", "zoom", "size"),
    [(1.0, 1.0, 1), (1.5, 1.0, 2), (2.0, 1.0, 2), (1.5, 1.5, 2)],
)
def test_jiggle_crosses_a_css_px_at_every_column(
    world: World, monkeypatch: pytest.MonkeyPatch, dpr: float, zoom: float, size: int
) -> None:
    per_css = _scale(world, monkeypatch, dpr, zoom)
    world.css_px_events = True  # a jiggle inside one CSS px column is not seen
    ctx = make_ctx(world)
    first = 6 * int(150 * per_css // 6)  # a column at CSS x = 100 + ~50
    columns = [first, first + 1, first + 2]  # every residue of 1.5 and 2
    for column in columns:
        handle(ctx).click(offset_x=_column_offset(column, per_css), offset_y=10)

    pressed = [entry[3] for entry in world.presses()]
    assert [x for x, _ in pressed] == columns and len(world.probes) == 3
    expected: list[tuple[int, int]] = []
    for x, y in pressed:
        expected += [(x + size, y), (x, y)]
    assert jiggles(world) == expected


def test_large_jiggle_steps_left_when_right_is_off_screen(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    per_css = _scale(world, monkeypatch, 1.5, 1.0)
    column = 225
    world.virtual_screen = (0, 0, column + 2, 1080)  # column + 2 is off every monitor
    ctx = make_ctx(world)
    handle(ctx).click(offset_x=_column_offset(column, per_css), offset_y=10)

    (x, y) = world.presses()[-1][3]
    assert x == column and jiggles(world) == [(column - 2, y), (column, y)]


def test_jiggle_falls_back_to_one_px_when_only_that_stays_inside(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scale(world, monkeypatch, 2.0, 1.0)
    world.add("sliver", 300, 200, 1.5, 6)  # 3 input px wide: +-2 px leave it
    ctx = make_ctx(world)
    handle(ctx, "sliver").click(offset_x=0.6, offset_y=3)  # Input x = 601

    (x, y) = world.presses()[-1][3]
    assert x == 601 and jiggles(world) == [(602, y), (601, y)]


class _RawKeys:
    def __init__(self, calls: list[tuple[str, str]]) -> None:
        self.calls = calls

    def _keyUp(self, key: str) -> None:
        self.calls.append(("_keyUp", key))


class _RawKeysWithDown(_RawKeys):
    def __init__(self, calls: list[tuple[str, str]]) -> None:
        super().__init__(calls)
        self.error: Exception | None = None

    def _keyDown(self, key: str) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append(("_keyDown", key))


class _FakeBackend:
    """Stands in for the OS backend inside a real DragInteractor."""

    def __init__(self, *, raw_down: bool, virtual: bool = True) -> None:
        self.calls: list[tuple[str, str]] = []
        self.platformModule = (_RawKeysWithDown if raw_down else _RawKeys)(self.calls)
        self.key_down_error: Exception | None = None
        if virtual:
            # A 1280 px monitor left of the 1920 px primary: x in [-1280, 1920).
            self.virtual_screen = lambda: (-1280, 0, 3200, 1080)

    def size(self) -> tuple[int, int]:
        return 1920, 1080

    def keyDown(self, key: str, _pause: bool = False) -> None:
        if self.key_down_error is not None:
            raise self.key_down_error
        self.calls.append(("keyDown", key))


@pytest.mark.parametrize(
    ("raw_down", "expected_down"), [(True, "_keyDown"), (False, "keyDown")]
)
def test_press_escape_uses_the_raw_key_path(raw_down: bool, expected_down: str) -> None:
    backend = _FakeBackend(raw_down=raw_down)
    DragInteractor(backend=backend).press_escape()
    assert backend.calls == [(expected_down, "escape"), ("_keyUp", "escape")]


def test_press_escape_releases_even_when_key_down_fails() -> None:
    backend = _FakeBackend(raw_down=False)
    backend.key_down_error = NativeInputError("fail-safe corner")
    with pytest.raises(NativeInputError):
        DragInteractor(backend=backend).press_escape()
    assert backend.calls == [("_keyUp", "escape")]


def test_press_escape_releases_even_when_the_raw_key_down_fails() -> None:
    backend = _FakeBackend(raw_down=True)
    raw = backend.platformModule
    assert isinstance(raw, _RawKeysWithDown)
    raw.error = NativeInputError("SendInput delivered 0 of 1 keyboard events")
    with pytest.raises(NativeInputError, match="delivered 0 of 1"):
        DragInteractor(backend=backend).press_escape()
    assert backend.calls == [("_keyUp", "escape")]


def test_can_land_rejects_corners_and_off_screen_points() -> None:
    interactor = DragInteractor(backend=_FakeBackend(raw_down=True))
    assert interactor.can_land(1918, 0) and interactor.can_land(-100, 500)
    assert not interactor.can_land(1920, 500)  # right of every monitor
    assert not interactor.can_land(1919, 0)  # primary-screen fail-safe corner
    assert not interactor.can_land(-1280, 0)  # virtual-screen corner
    assert not interactor.can_land(-1281, 5)  # left of every monitor
    single = DragInteractor(backend=_FakeBackend(raw_down=True, virtual=False))
    assert single.can_land(5000, 5) and not single.can_land(0, 1079)


def test_offset_outside_the_element_scrolls_once_more_then_fails(world: World) -> None:
    ctx = make_ctx(world)
    with pytest.raises(NativeInputError):
        handle(ctx).click(offset_x=150)
    assert world.bridge_methods().count("scrollIntoView") == 2
    assert world.moves() == []


def test_point_avoids_the_unknown_scroll_margin(world: World) -> None:
    world.add("edge", 990, 60, 100, 20)  # right part lies outside the viewport
    ctx = make_ctx(world)
    handle(ctx, "edge").click()
    assert 990 <= world.presses()[-1][3][0] < 996  # 4 px margin, scrollbar unknown


def test_point_avoids_the_scrollbar_band(world: World) -> None:
    world.geometry.update(scrollWidth=1000, scrollHeight=3000)  # vertical scrolling
    world.add("edge", 970, 60, 30, 20)
    ctx = make_ctx(world)
    handle(ctx, "edge").click()
    assert 970 <= world.presses()[-1][3][0] < 1000 - 17


def test_element_under_the_scrollbar_is_refused(world: World) -> None:
    world.geometry.update(scrollWidth=1000, scrollHeight=3000)
    world.add("under", 985, 60, 15, 20)
    ctx = make_ctx(world)

    with pytest.raises(NativeInputError):
        handle(ctx, "under").click()
    assert world.bridge_methods().count("scrollIntoView") == 2
    assert world.moves() == [] and world.presses() == []


def test_zero_size_element_is_refused(world: World) -> None:
    world.add("empty", 10, 10, 0, 0)
    ctx = make_ctx(world)

    with pytest.raises(NativeInputError):
        handle(ctx, "empty").click()
    assert world.moves() == [] and world.presses() == []


def test_hover_moves_without_pressing(world: World) -> None:
    ctx = make_ctx(world)
    handle(ctx).move_to()

    assert len(world.moves()) == 1 and len(world.probes) == 1
    assert world.presses() == []


def test_missing_geometry_raises_before_moving(world: World) -> None:
    ctx = make_ctx(world)
    el = Element(ctx, "doc1.1", tab_id=TAB)
    world.drop_geometry = True

    with pytest.raises(NativeInputError):
        el.click()
    assert world.moves() == []


# -- keyboard ------------------------------------------------------------------


def test_type_checks_focus_before_typing(world: World) -> None:
    world.focus_states = [dict(LOST_FOCUS), dict(OK_FOCUS)]
    ctx = make_ctx(world)
    handle(ctx).type("hello", human=False)

    tail = [
        step
        for step in world.sequence()
        if step in ("press", "type", "focusState", "focus")
    ]
    assert tail == ["press", "focusState", "focus", "focusState", "type"]
    assert world.kinds("type") == [("type", "hello", 280, 20, False)]


def test_type_sends_chunks_with_a_focus_check_before_each(world: World) -> None:
    world.focus_states = [dict(OK_FOCUS) for _ in range(3)]
    ctx = make_ctx(world)
    handle(ctx).type("abcdefghijklmnopqrst")

    assert [entry[1] for entry in world.kinds("type")] == [
        "abcdefgh",
        "ijklmnop",
        "qrst",
    ]
    tail = [s for s in world.sequence() if s in ("type", "focusState")]
    assert tail == ["focusState", "type"] * 3


def test_type_stops_when_focus_is_lost(world: World) -> None:
    world.focus_states = [dict(OK_FOCUS), dict(OK_FOCUS), dict(LOST_FOCUS)]
    ctx = make_ctx(world)

    with pytest.raises(NativeInputError, match="typed 16 of 20"):
        handle(ctx).type("abcdefghijklmnopqrst")
    assert len(world.kinds("type")) == 2


def test_type_keeps_crlf_in_one_chunk(world: World) -> None:
    world.focus_states = [dict(OK_FOCUS) for _ in range(2)]
    ctx = make_ctx(world)
    handle(ctx).type("abcdefg\r\nxy")
    assert [entry[1] for entry in world.kinds("type")] == ["abcdefg\r\n", "xy"]


@pytest.mark.parametrize(
    "state",
    [
        {"documentHasFocus": True, "activeIsElement": False},
        {"documentHasFocus": False, "activeIsElement": True},
    ],
)
def test_type_refuses_without_focus(world: World, state: dict[str, Any]) -> None:
    world.focus_states = [dict(state), dict(state)]
    ctx = make_ctx(world)

    with pytest.raises(NativeInputError, match="typed 0 of 6"):
        handle(ctx).type("secret")
    assert world.kinds("type") == []


@pytest.mark.parametrize("click_type", [ClickType.RIGHT, ClickType.MIDDLE])
def test_type_only_left_or_double_click(world: World, click_type: ClickType) -> None:
    ctx = make_ctx(world)
    with pytest.raises(InvalidArgumentError) as info:
        handle(ctx).type("x", click_type=click_type)
    assert isinstance(info.value, ValueError)
    assert world.log == []


@pytest.mark.parametrize(
    "arguments", [{"kind": "regex"}, {"index": -1}, {"index": True}]
)
def test_bad_query_arguments_are_invalid(arguments: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentError):
        ElementQuery("#a", **arguments)


def test_type_with_double_click(world: World) -> None:
    world.focus_states = [dict(OK_FOCUS)]
    ctx = make_ctx(world)
    handle(ctx).type("x", click_type=ClickType.DOUBLE)
    assert world.presses()[0][1:3] == ("left", 2)


def test_type_after_failed_click_sends_no_keys(world: World) -> None:
    world.covered.add("doc1.1")
    ctx = make_ctx(world)

    with pytest.raises(ClickVerificationError):
        handle(ctx).type("x")
    assert world.kinds("type") == [] and "focusState" not in world.bridge_methods()


POLL = element_module.TYPE_SETTLE_POLL_S


def after_typing(world: World) -> list[tuple[Any, ...]]:
    last_type = max(i for i, entry in enumerate(world.log) if entry[0] == "type")
    return world.log[last_type + 1 :]


def settle_reads(world: World) -> int:
    return sum(1 for e in after_typing(world) if e[:2] == ("bridge", "describeElement"))


def test_type_waits_until_the_value_catches_up(world: World) -> None:
    world.values["doc1.1"] = "x "
    world.reads_before_delivery = 3  # keys still queued in the OS for 3 polls
    world.focus_states = [dict(OK_FOCUS)]
    ctx = make_ctx(world)
    handle(ctx).type("abc 123")

    assert world.values["doc1.1"] == "x abc 123" and world.typed_queue == ""
    assert settle_reads(world) == 4
    assert [e for e in after_typing(world) if e[0] == "sleep"] == [("sleep", POLL)] * 3
    assert after_typing(world)[-1][:2] == ("bridge", "describeElement")


def test_type_waits_for_the_whole_text_not_a_prefix(world: World) -> None:
    world.values["doc1.1"] = ""
    world.deliver_per_read = 2
    world.focus_states = [dict(OK_FOCUS)]
    ctx = make_ctx(world)
    handle(ctx).type("abc 123")
    assert world.values["doc1.1"] == "abc 123" and settle_reads(world) == 4


def test_type_settles_on_contenteditable_text_with_crlf(world: World) -> None:
    world.texts["doc1.1"] = "note: "
    world.reads_before_delivery = 1
    world.focus_states = [dict(OK_FOCUS)]
    ctx = make_ctx(world)
    handle(ctx).type("a\r\nb")
    assert world.texts["doc1.1"] == "note: a\r\nb" and settle_reads(world) == 2


def test_type_into_a_transforming_input_settles_when_stable(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.values["doc1.1"] = ""
    world.transform = str.strip  # type=email drops the surrounding spaces
    world.focus_states = [dict(OK_FOCUS)]
    ctx = make_ctx(world)
    with caplog.at_level("WARNING", logger="emunium.element"):
        handle(ctx).type(" a@b.c ")

    waited = sum(e[1] for e in after_typing(world) if e[0] == "sleep")
    assert world.values["doc1.1"] == "a@b.c"
    stable = element_module.TYPE_SETTLE_STABLE_S
    assert stable - 1e-9 <= waited <= stable + POLL + 1e-9
    assert caplog.records == []


def test_type_already_ending_with_the_text_waits_for_a_change(world: World) -> None:
    world.values["doc1.1"] = "abc"
    world.reads_before_delivery = 2
    world.focus_states = [dict(OK_FOCUS)]
    ctx = make_ctx(world)
    handle(ctx).type("abc")
    assert world.values["doc1.1"] == "abcabc" and settle_reads(world) == 3


def test_type_settle_timeout_warns_and_returns(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.values["doc1.1"] = ""
    world.reads_before_delivery = 10**6  # the keys never arrive
    world.focus_states = [dict(OK_FOCUS)]
    ctx = make_ctx(world)
    start = world.clock
    with caplog.at_level("WARNING", logger="emunium.element"):
        handle(ctx).type("abc")

    waited = sum(e[1] for e in after_typing(world) if e[0] == "sleep")
    timeout = element_module.TYPE_SETTLE_TIMEOUT_S
    assert timeout == 2.0
    assert timeout - POLL < waited <= timeout + POLL
    assert world.clock - start == pytest.approx(waited + 0.035)  # + jiggle pause
    assert [r.levelname for r in caplog.records] == ["WARNING"]
    assert "did not reach" in caplog.records[0].getMessage()


def test_type_settle_false_returns_right_after_the_keys(world: World) -> None:
    world.values["doc1.1"] = ""
    world.reads_before_delivery = 10**6
    world.focus_states = [dict(OK_FOCUS)]
    ctx = make_ctx(world)
    handle(ctx).type("abc", settle=False)
    assert after_typing(world) == []
    assert "describeElement" not in world.bridge_methods()


def test_type_settle_ends_quietly_when_the_element_goes_away(world: World) -> None:
    world.values["doc1.1"] = ""
    world.after_type = lambda: world.stale.add("doc1.1")  # Enter navigated away
    world.focus_states = [dict(OK_FOCUS)]
    ctx = make_ctx(world)
    handle(ctx).type("q\n")
    assert settle_reads(world) == 1 and world.kinds("type") != []


# -- drag ------------------------------------------------------------------------


def _drag_world(world: World) -> None:
    world.add("src", 100, 400, 100, 40)
    world.add("dst", 600, 650, 100, 40)
    world.on_scroll["dst"] = lambda: world.shift_all(-200)  # page scrolls 200 px


def _probed_ids(world: World) -> list[str]:
    return [entry[2] for entry in world.kinds("bridge") if entry[1] == "pointerProbe"]


def test_drag_verifies_target_source_then_target_with_button_held(world: World) -> None:
    _drag_world(world)
    ctx = make_ctx(world)
    source, target = handle(ctx, "src"), handle(ctx, "dst")

    source.drag_to(target, human=False)

    assert world.bridge_methods()[:5] == [
        "activateTab",
        "scrollIntoView",
        "scrollIntoView",
        "describeElement",
        "describeElement",
    ]
    assert _probed_ids(world) == ["dst", "src", "dst"]
    steps = [s for s in world.sequence() if s in ("pointerProbe", "down", "up")]
    assert steps == ["pointerProbe", "pointerProbe", "down", "pointerProbe", "up"]
    (_, _, down_at) = world.kinds("down")[0]
    (_, _, up_at) = world.kinds("up")[0]
    assert world.rects["src"]["y"] == 200  # re-resolved after the target scroll
    assert world.input_inside("src", down_at)
    assert world.input_inside("dst", up_at)
    assert world.presses() == []


def test_drag_onto_moving_placeholder_releases_at_the_start(world: World) -> None:
    # Sortable libraries swap in a placeholder once the drag starts: the drop
    # point is no longer over the target.
    _drag_world(world)
    world.after_button_down = lambda: world.covered.add("dst")
    ctx = make_ctx(world)

    with pytest.raises(ClickVerificationError):
        handle(ctx, "src").drag_to(handle(ctx, "dst"))

    assert_cancelled_at_start(world)


def assert_cancelled_at_start(world: World) -> None:
    """The abort tail: back to the press point, Escape there, release there."""
    (_, _, down_at) = world.kinds("down")[0]
    assert world.log[-3:] == [
        ("move", down_at, False),
        ("escape", down_at),
        ("up", "left", down_at),
    ]
    assert len(world.kinds("up")) == 1


def test_drag_stale_target_after_press_releases_at_the_start(world: World) -> None:
    _drag_world(world)
    world.after_button_down = lambda: world.stale.add("dst")
    ctx = make_ctx(world)

    with pytest.raises(StaleElementError):
        handle(ctx, "src").drag_to(handle(ctx, "dst"))
    assert_cancelled_at_start(world)


def test_native_dnd_without_samples_is_cancelled_at_the_start(world: World) -> None:
    # A native HTML5 drag stops pointer events once the button is down.
    _drag_world(world)

    def native_drag_starts() -> None:
        world.silent = True

    world.after_button_down = native_drag_starts
    ctx = make_ctx(world)

    with pytest.raises(NativeInputError):
        handle(ctx, "src").drag_to(handle(ctx, "dst"))
    assert_cancelled_at_start(world)


def test_native_dnd_with_drag_samples_drops_on_the_target(world: World) -> None:
    # The content script reports dragover samples in the same probe shape.
    _drag_world(world)

    def native_drag_starts() -> None:
        world.sample_type = "dragover"

    world.after_button_down = native_drag_starts
    ctx = make_ctx(world)
    handle(ctx, "src").drag_to(handle(ctx, "dst"))

    (_, _, up_at) = world.kinds("up")[0]
    assert world.input_inside("dst", up_at)
    assert world.kinds("escape") == []
    assert world.probes[-1]["event"]["type"] == "dragover"


def test_drag_held_phase_shares_one_deadline(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _drag_world(world)
    world.after_button_down = lambda: world.covered.add("dst")
    # Exact in binary, so the budgets compare exactly.
    world.probe_cost = 1.25
    monkeypatch.setattr(element_module, "JIGGLE_SETTLE_S", 0.25)
    ctx = make_ctx(world)

    with pytest.raises(ClickVerificationError, match="deadline"):
        handle(ctx, "src").drag_to(handle(ctx, "dst"))

    pre_press = 2  # target hover + source, before the button went down
    assert world.probe_budgets[:pre_press] == [(10.0, 300), (10.0, 300)]
    # Held phase: the jiggle pause counts against the 2.0 s, so 1.75 s for the
    # first probe and 0.25 s for the second (page-side wait = remaining - quiet
    # period), then the deadline aborts.
    assert world.probe_budgets[pre_press:] == [(1.75, 300), (0.25, 210)]
    assert_cancelled_at_start(world)


def test_failed_return_move_still_escapes_and_releases(world: World) -> None:
    _drag_world(world)
    world.after_button_down = lambda: world.covered.add("dst")
    ctx = make_ctx(world)
    source, target = handle(ctx, "src"), handle(ctx, "dst")
    real_down = world.after_button_down

    def arm_failure() -> None:
        real_down()
        world.move_failures[world.cursor] = RuntimeError("cursor stuck")

    world.after_button_down = arm_failure

    with pytest.raises(ClickVerificationError):
        source.drag_to(target)
    assert [e[0] for e in world.log[-2:]] == ["escape", "up"]
    assert world.log[-1][2] == world.log[-2][1]  # released in place, after Escape


def test_interrupt_during_return_is_re_raised_after_cleanup(world: World) -> None:
    _drag_world(world)
    ctx = make_ctx(world)
    source, target = handle(ctx, "src"), handle(ctx, "dst")

    def arm_interrupt() -> None:
        world.covered.add("dst")
        world.move_failures[world.cursor] = KeyboardInterrupt()

    world.after_button_down = arm_interrupt

    with pytest.raises(KeyboardInterrupt) as info:
        source.drag_to(target)
    assert isinstance(info.value.__cause__, ClickVerificationError)
    assert [e[0] for e in world.log[-2:]] == ["escape", "up"]


def test_failed_button_down_is_still_cancelled(world: World) -> None:
    _drag_world(world)
    world.button_down_error = NativeInputError("fail-safe corner")
    ctx = make_ctx(world)

    with pytest.raises(NativeInputError, match="fail-safe"):
        handle(ctx, "src").drag_to(handle(ctx, "dst"))
    assert [e[0] for e in world.log[-3:]] == ["move", "escape", "up"]


def test_drag_unverified_target_never_presses(world: World) -> None:
    _drag_world(world)
    world.covered.add("dst")
    ctx = make_ctx(world)

    with pytest.raises(ClickVerificationError):
        handle(ctx, "src").drag_to(handle(ctx, "dst"))
    assert world.kinds("down") == [] and world.kinds("up") == []


def test_drag_unverified_source_never_presses(world: World) -> None:
    _drag_world(world)
    world.covered.add("src")
    ctx = make_ctx(world)

    with pytest.raises(ClickVerificationError):
        handle(ctx, "src").drag_to(handle(ctx, "dst"))
    assert world.kinds("down") == []


def test_drag_requires_same_context_and_known_tab(world: World) -> None:
    world.add("dst", 600, 300, 100, 40)
    ctx = make_ctx(world)

    with pytest.raises(NativeInputError):
        handle(ctx).drag_to(Element(ctx, "dst", tab_id=TAB + 1))
    with pytest.raises(StaleElementError):
        handle(ctx).drag_to(Element(ctx, "dst"))
    with pytest.raises(InvalidArgumentError):
        handle(ctx).drag_to(handle(make_ctx(world), "dst"))
    assert world.log == []


# -- refresh / handle state -------------------------------------------------------


def _list_world(world: World, prefix: str) -> None:
    ids = [f"{prefix}.{n}" for n in (1, 2, 3)]
    for n, element_id in enumerate(ids):
        world.add(element_id, 10, 100 * n, 50, 20)
        world.tags[element_id] = "li"
    world.selectors[".item"] = ids


def test_refresh_by_id_then_by_query_index(world: World) -> None:
    _list_world(world, "doc1")
    ctx = make_ctx(world)
    second = dom.query_selector_all(ctx, ".item")[1]
    assert second.query == ElementQuery(".item", 1)

    second.refresh()
    assert second.element_id == "doc1.2"

    world.stale.update(world.selectors[".item"])  # navigation replaced the document
    _list_world(world, "doc2")
    second.refresh()
    assert second.element_id == "doc2.2"
    assert second.rect["y"] == 100


def test_refresh_by_index_checks_the_tag(world: World) -> None:
    _list_world(world, "doc1")
    ctx = make_ctx(world)
    second = dom.query_selector_all(ctx, ".item")[1]
    world.stale.update(world.selectors[".item"])
    _list_world(world, "doc2")
    world.tags["doc2.2"] = "div"

    with pytest.raises(StaleElementError, match="<div>"):
        second.refresh()
    assert second.element_id == "doc1.2"


def test_refresh_without_requery_propagates_stale(world: World) -> None:
    _list_world(world, "doc1")
    ctx = make_ctx(world)
    el = dom.query_selector_all(ctx, ".item")[2]
    world.stale.add("doc1.3")

    with pytest.raises(StaleElementError):
        el.refresh(requery=False)

    world.selectors[".item"] = ["doc1.1"]
    with pytest.raises(StaleElementError):
        el.refresh()


def test_refresh_without_tab_is_stale(world: World) -> None:
    ctx = make_ctx(world)
    el = Element(ctx, "doc1.1", query=ElementQuery("#a"))

    with pytest.raises(StaleElementError, match="no tab id"):
        el.refresh()
    assert world.log == []


def test_screen_point_from_calibration_with_legacy_fallback(world: World) -> None:
    ctx = make_ctx(world)
    el = handle(ctx)
    assert (el.screen_x, el.screen_y) == pytest.approx((150.0, 170.0))

    ctx.calibration.record(world.live(), 0, 0, 3, 150)
    assert (el.screen_x, el.screen_y) == pytest.approx((153.0, 220.0))

    legacy = Element(ctx, "x", absolute_screen_x=12, absolute_screen_y=34)
    assert (legacy.screen_x, legacy.screen_y) == (12.0, 34.0)


def test_payload_fields_and_public_properties(world: World) -> None:
    el = handle(make_ctx(world))
    assert el.element_id == "doc1.1" and el.tag == "button" and el.text == "label"
    assert el.attrs == {"id": "doc1.1"}
    assert el.rect == {"x": 100, "y": 60, "width": 100, "height": 20}
    assert el.center == (150, 70)
    assert el.visible is True
    assert el.tab_id == TAB and el.zoom == 1.0
    assert el.geometry == Geometry.from_payload(GEOMETRY, 1.0)


def test_bare_bridge_gets_a_shared_default_context(world: World) -> None:
    bridge = FakeBridge(world)
    first = Element(bridge, "a")
    second = Element(bridge, "b")
    assert first.context is second.context
    assert first.context.bridge is bridge
    assert isinstance(first.context.calibration, Calibration)
    assert first.context.verify_input is True and first.context.headless is False


def test_default_context_does_not_keep_the_bridge_alive(world: World) -> None:
    bridge = FakeBridge(world)
    BrowserContext.of(bridge)
    ref = weakref.ref(bridge)
    del bridge
    gc.collect()
    assert ref() is None


def test_element_module_imports_without_pyautogui() -> None:
    code = (
        "import sys, emunium.element, emunium._browser.dom; "
        "sys.exit(1 if 'pyautogui' in sys.modules else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(Path(element_module.__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr


# -- foreground window ---------------------------------------------------------------

BROWSER_PID = 100
BROWSER_HWND = 0x10
OTHER_HWND = 0x30
# GEOMETRY's window in physical px at dpr 1: (left, top, right, bottom).
BROWSER_RECT = (0, 0, 1000, 800)


@dataclass
class FakeWindow:
    hwnd: int
    pid: int
    thread_id: int
    rect: tuple[int, int, int, int]
    class_name: str = CHROME_WINDOW_CLASS
    minimized: bool = False


class FakeWindows:
    """``WindowApi`` over an in-memory desktop, logging into the world.

    The browser (pid 100) owns no window itself; its child (pid 101) owns the
    browser window, and a foreign process (pid 200) owns the window in front.
    """

    def __init__(self, world: World) -> None:
        self.world = world
        self.windows = [
            FakeWindow(OTHER_HWND, 200, 30, BROWSER_RECT, class_name="ConsoleWindow"),
            FakeWindow(BROWSER_HWND, 101, 10, BROWSER_RECT),
        ]
        self.parents = [(BROWSER_PID, 1), (101, BROWSER_PID), (200, 1)]
        self.foreground = OTHER_HWND
        self.refuse = False
        self.enum_error: Exception | None = None

    def _window(self, hwnd: int) -> FakeWindow:
        return next(window for window in self.windows if window.hwnd == hwnd)

    def _log(self, *entry: Any) -> None:
        self.world.log.append(("window", *entry))

    def enum_windows(self) -> list[int]:
        if self.enum_error is not None:
            raise self.enum_error
        return [window.hwnd for window in self.windows]

    def window_thread_process_id(self, hwnd: int) -> tuple[int, int]:
        window = self._window(hwnd)
        return window.thread_id, window.pid

    def is_window_visible(self, hwnd: int) -> bool:
        return True

    def class_name(self, hwnd: int) -> str:
        return self._window(hwnd).class_name

    def window_rect(self, hwnd: int) -> tuple[int, int, int, int] | None:
        return self._window(hwnd).rect

    def is_iconic(self, hwnd: int) -> bool:
        return self._window(hwnd).minimized

    def ex_style(self, hwnd: int) -> int:
        return 0

    def process_parents(self) -> list[tuple[int, int]]:
        return list(self.parents)

    def foreground_window(self) -> int:
        return self.foreground

    def current_thread_id(self) -> int:
        return 1

    def attach_thread_input(self, attach: int, attach_to: int, on: bool) -> bool:
        self._log("attach" if on else "detach", attach_to)
        return True

    def show_window(self, hwnd: int, command: int) -> bool:
        self._log("show", hwnd, command)
        return True

    def set_foreground_window(self, hwnd: int) -> bool:
        self._log("set_foreground", hwnd)
        if self.refuse:
            return False
        self.foreground = hwnd
        return True

    def bring_window_to_top(self, hwnd: int) -> bool:
        self._log("bring_to_top", hwnd)
        return True

    def set_focus(self, hwnd: int) -> int:
        self._log("set_focus", hwnd)
        return self.foreground


def _window_steps(world: World) -> list[str]:
    """The transaction order with window calls named by what they do."""
    return [
        entry[1] if entry[0] in ("bridge", "window") else entry[0]
        for entry in world.log
    ]


@pytest.fixture
def desktop(world: World) -> FakeWindows:
    return FakeWindows(world)


def test_click_raises_the_browser_window_after_activating_the_tab(
    world: World, desktop: FakeWindows
) -> None:
    ctx = make_ctx(world, pid=BROWSER_PID, windows=desktop)
    handle(ctx).click()

    steps = _window_steps(world)
    assert steps[:2] == ["activateTab", "attach"]
    assert steps.index("set_foreground") < steps.index("scrollIntoView")
    assert desktop.foreground == BROWSER_HWND
    attached = [entry[2] for entry in world.kinds("window") if entry[1] == "attach"]
    detached = [entry[2] for entry in world.kinds("window") if entry[1] == "detach"]
    assert sorted(attached) == sorted(detached) == [10, 30]
    assert pressed_inside(world, "doc1.1")


def test_window_matching_the_page_geometry_is_raised(
    world: World, desktop: FakeWindows
) -> None:
    world.geometry = {**GEOMETRY, "screenX": 200, "screenY": 100}
    # A second browser window sits where the page's window is not.
    desktop.windows.insert(0, FakeWindow(0x20, BROWSER_PID, 20, BROWSER_RECT))
    desktop.windows.append(FakeWindow(BROWSER_HWND + 1, 101, 10, (203, 96, 1205, 902)))
    ctx = make_ctx(world, pid=BROWSER_PID, windows=desktop)
    handle(ctx).hover()

    assert desktop.foreground == BROWSER_HWND + 1


def test_window_already_in_front_is_left_alone(
    world: World, desktop: FakeWindows
) -> None:
    desktop.foreground = BROWSER_HWND
    ctx = make_ctx(world, pid=BROWSER_PID, windows=desktop)
    handle(ctx).click()

    assert world.kinds("window") == []
    assert len(world.presses()) == 1


def test_window_kept_behind_refuses_before_any_input(
    world: World, desktop: FakeWindows
) -> None:
    desktop.refuse = True
    ctx = make_ctx(world, pid=BROWSER_PID, windows=desktop)

    with pytest.raises(
        NativeInputError, match="could not be brought to the foreground"
    ):
        handle(ctx).click()
    assert world.bridge_methods() == ["activateTab"]
    assert world.moves() == [] and world.presses() == []
    # Every thread input attachment is undone.
    assert [entry[1] for entry in world.kinds("window")].count("detach") == 2


def test_no_browser_window_refuses_before_any_input(
    world: World, desktop: FakeWindows
) -> None:
    desktop.windows = [desktop.windows[0]]  # only the foreign window is left
    ctx = make_ctx(world, pid=BROWSER_PID, windows=desktop)

    with pytest.raises(
        NativeInputError, match="could not be brought to the foreground"
    ):
        handle(ctx).click()
    assert world.kinds("window") == [] and world.moves() == []


def test_window_api_failure_becomes_native_input_error(
    world: World, desktop: FakeWindows
) -> None:
    desktop.enum_error = OSError("EnumWindows failed")
    ctx = make_ctx(world, pid=BROWSER_PID, windows=desktop)

    with pytest.raises(NativeInputError, match="foreground: EnumWindows") as info:
        handle(ctx).click()
    assert isinstance(info.value.__cause__, OSError)
    assert world.moves() == []


def test_handle_without_geometry_is_described_before_the_raise(
    world: World, desktop: FakeWindows
) -> None:
    ctx = make_ctx(world, pid=BROWSER_PID, windows=desktop)
    Element(ctx, "doc1.1", tab_id=TAB).click()

    steps = _window_steps(world)
    assert steps[:3] == ["activateTab", "describeElement", "attach"]
    assert desktop.foreground == BROWSER_HWND


def test_drag_raises_the_window_once_before_scrolling(
    world: World, desktop: FakeWindows
) -> None:
    world.add("src", 100, 300, 100, 40)
    world.add("dst", 600, 400, 100, 40)
    ctx = make_ctx(world, pid=BROWSER_PID, windows=desktop)
    handle(ctx, "src").drag_to(handle(ctx, "dst"))

    steps = _window_steps(world)
    assert steps.count("set_foreground") == 1
    assert steps.index("set_foreground") < steps.index("scrollIntoView")


@pytest.mark.parametrize("case", ["no pid", "headless", "not windows"])
def test_foreground_is_a_no_op_without_pid_window_or_windows(
    world: World,
    desktop: FakeWindows,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    pid = None if case == "no pid" else BROWSER_PID
    ctx = make_ctx(world, pid=pid, headless=case == "headless", windows=desktop)
    if case == "not windows":
        monkeypatch.setattr(context_module, "_on_windows", lambda: False)

    assert ctx.manages_foreground is False
    ctx.ensure_foreground(world.live())
    if case != "headless":
        Element(ctx, "doc1.1", tab_id=TAB).click()
        # No describe for the missing geometry either: the order is unchanged.
        assert world.sequence()[:2] == ["activateTab", "scrollIntoView"]
    assert world.kinds("window") == [] and desktop.foreground == OTHER_HWND


@pytest.fixture
def launched(
    world: World, desktop: FakeWindows, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """``Browser.launch`` without Chrome: a fake process with ``BROWSER_PID``."""
    events: list[str] = []

    def fake_launch(session: Any, bridge_timeout: float = 60.0) -> None:
        session.process = SimpleNamespace(pid=BROWSER_PID)
        events.append("launch")

    monkeypatch.setattr(facade_module, "Bridge", lambda port=0: FakeBridge(world))
    monkeypatch.setattr(facade_module, "launch", fake_launch)
    monkeypatch.setattr(facade_module, "close", lambda session: events.append("close"))
    monkeypatch.setattr(tabs_module, "pin_active", lambda ctx: events.append("pin"))
    monkeypatch.setattr(context_module, "Win32Windows", lambda: desktop)
    return events


def test_launch_raises_the_window_once_and_close_forgets_the_pid(
    launched: list[str], desktop: FakeWindows
) -> None:
    browser = facade_module.Browser().launch()

    assert launched == ["launch", "pin"]
    assert browser.context.pid == BROWSER_PID
    assert desktop.foreground == BROWSER_HWND
    browser.close()
    assert browser.context.pid is None and launched[-1] == "close"


def test_launch_survives_a_window_that_stays_behind(
    launched: list[str], desktop: FakeWindows, caplog: pytest.LogCaptureFixture
) -> None:
    desktop.refuse = True
    with caplog.at_level("INFO", logger="emunium.browser"):
        browser = facade_module.Browser().launch()

    assert browser.context.pid == BROWSER_PID
    assert desktop.foreground == OTHER_HWND
    assert "could not be brought to the foreground" in caplog.text


def test_headless_launch_leaves_windows_alone(
    launched: list[str], desktop: FakeWindows
) -> None:
    browser = facade_module.Browser(headless=True).launch()

    assert browser.context.pid == BROWSER_PID
    assert desktop.world.kinds("window") == []


# -- dom ----------------------------------------------------------------------------


def test_dom_propagates_transport_errors(world: World) -> None:
    world.errors["querySelector"] = BridgeDisconnectedError("socket closed")
    with pytest.raises(BridgeDisconnectedError):
        dom.query_selector(make_ctx(world), "#a")


def test_dom_wait_returns_element_with_query(world: World) -> None:
    world.wait_result = world.payload("doc1.1")
    el = dom.wait_for_element(make_ctx(world), "#a")
    assert el is not None and el.element_id == "doc1.1" and el.tab_id == TAB
    assert el.query == ElementQuery("#a", 0, "css")

    xpath = dom.wait_for_xpath(make_ctx(world), "//button")
    assert xpath is not None and xpath.query == ElementQuery("//button", 0, "xpath")


def test_dom_wait_timeout_becomes_timeout_error(world: World) -> None:
    world.wait_result = WaitTimeoutError("waited 1000 ms")
    ctx = make_ctx(world)
    with pytest.raises(TimeoutError) as info:
        dom.wait_for_element(ctx, "#a", timeout=1.0)
    assert type(info.value) is TimeoutError
    assert isinstance(info.value.__cause__, WaitTimeoutError)
    assert dom.wait_for_text(ctx, "Go", timeout=1.0, raise_on_timeout=False) is None
    assert world.bridge_methods().count("waitForSelector") == 2  # no retry loop


def test_dom_wait_other_errors_propagate(world: World) -> None:
    world.wait_result = ContentUnavailableError("chrome:// page")
    with pytest.raises(ContentUnavailableError):
        dom.wait_for_element(make_ctx(world), "#a")


def test_dom_elementless_match_is_none(world: World) -> None:
    world.wait_result = {"element": None, "matched": True, "count": 0}
    assert dom.wait_for_element(make_ctx(world), "#gone", state="detached") is None


def test_dom_lookups_accept_a_bare_bridge(world: World) -> None:
    _list_world(world, "doc1")
    world.selectors["Go|exact=True"] = ["doc1.3"]
    bridge = FakeBridge(world)

    first = dom.query_selector(bridge, ".item")
    assert first is not None and first.query == ElementQuery(".item", 0)
    assert first.context is BrowserContext.of(bridge)
    assert dom.query_selector(bridge, ".none") is None
    (by_text,) = dom.get_by_text(bridge, "Go", exact=True)
    assert by_text.query == ElementQuery("Go", 0, "text", exact=True)
    assert len(dom.get_all_interactive(bridge)) == 3
