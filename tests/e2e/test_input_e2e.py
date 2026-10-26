# tests/e2e/test_input_e2e.py
"""Input suite against headed Chrome for Testing: moves the real mouse and types.

Every action goes through ``Browser`` / ``Element`` APIs (verified clicks,
focus-checked typing) into ``site/input.html`` served by the local
``http.server``, in a window this suite positions at (60, 40).

Scale coverage: page zoom 100 %, real browser zoom 150 % (Chrome's default
zoom level, seeded into the profile's Preferences before launch and confirmed
through ``Element.zoom``, which the extension reads with ``chrome.tabs.getZoom``),
and ``--force-device-scale-factor=1.5``.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from emunium import (
    Browser,
    ClickType,
    ClickVerificationError,
    Element,
)

pytestmark = pytest.mark.e2e

MakeBrowser = Callable[..., Browser]

WINDOW_ARGS = ["--window-position=60,40", "--window-size=900,600"]
# Chrome zoom levels are log base 1.2 of the zoom factor.
ZOOM_150_LEVEL = math.log(1.5) / math.log(1.2)
# --force-device-scale-factor=1.5 on a 96 DPI desktop: 1.5 input px per CSS px.
DSF_INPUT_PER_CSS = 1.5


def _seed_default_zoom(profile: Path, level: float) -> None:
    """Write Chrome's profile-wide default zoom before the first launch."""
    default_dir = profile / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    prefs = {"partition": {"default_zoom_level": {"x": level}}}
    (default_dir / "Preferences").write_text(json.dumps(prefs), encoding="utf-8")


def _launch(
    make_browser: MakeBrowser,
    site_url: str,
    *,
    extra: list[str] | None = None,
    **kwargs: Any,
) -> Browser:
    browser = make_browser(extra_args=[*WINDOW_ARGS, *(extra or [])], **kwargs)
    browser.goto(site_url + "/input.html")
    _await_visible(browser)
    return browser


def _await_visible(browser: Browser, timeout: float = 10.0) -> None:
    """Precondition for real input: the page reports itself visible.

    No foreground workaround here: ``Browser.launch`` raises the window, and
    every native action raises it again before its input, even when another
    window has been put in front since (a Chrome started from a background
    process otherwise opens behind the terminal and receives no pointer events).
    """
    deadline = time.monotonic() + timeout
    while True:
        visibility = browser.execute_script("return document.visibilityState")
        if visibility == "visible":
            # One more beat so the window is fully composed on screen.
            time.sleep(0.3)
            return
        if time.monotonic() >= deadline:
            pytest.fail(f"Chrome window not visible within {timeout}s: {visibility!r}")
        time.sleep(0.1)


def _state(browser: Browser) -> dict[str, Any]:
    return browser.execute_script("return window.__e2e")


def _dsf_column_offsets(element: Element, residue: int) -> dict[str, float]:
    """CSS offsets that put the cursor at the physical column ``x % 3 == residue``.

    ``Element.screen_x`` is the centre in input px (calibrated once a calibration
    exists); the cursor lands on ``int(round(x))``, so the target sits a quarter
    pixel past the wanted column to stay clear of rounding ties.
    """
    centre_x = element.screen_x
    base = math.floor(centre_x)
    column = base + (residue - base) % 3
    rect = element.rect
    return {
        "offset_x": rect["width"] / 2 + (column + 0.25 - centre_x) / DSF_INPUT_PER_CSS,
        "offset_y": rect["height"] / 2,
    }


@pytest.fixture
def headed(make_browser: MakeBrowser, site_url: str) -> Browser:
    return _launch(make_browser, site_url)


# -- verified clicks at three scales --------------------------------------------------


@pytest.mark.parametrize(
    ("config", "expected_zoom", "expected_dpr"),
    [
        ("zoom100", 1.0, 1.0),
        ("zoom150", 1.5, 1.5),
        ("dsf150", 1.0, 1.5),
    ],
)
def test_click_lands_on_target(
    make_browser: MakeBrowser,
    site_url: str,
    tmp_path: Path,
    config: str,
    expected_zoom: float,
    expected_dpr: float,
) -> None:
    if config == "zoom150":
        profile = tmp_path / "profile"
        _seed_default_zoom(profile, ZOOM_150_LEVEL)
        browser = _launch(make_browser, site_url, user_data_dir=str(profile))
    elif config == "dsf150":
        browser = _launch(
            make_browser, site_url, extra=["--force-device-scale-factor=1.5"]
        )
    else:
        browser = _launch(make_browser, site_url)

    button = browser.query_selector("#btn")
    assert button is not None
    # Preconditions: the scale under test is really in effect.
    assert button.zoom == pytest.approx(expected_zoom, abs=0.01)
    assert browser.execute_script("return window.devicePixelRatio") == pytest.approx(
        expected_dpr, abs=0.01
    )

    # Random click points: at DSF 1.5 any physical column must verify.
    browser.click(button, human=True)

    state = _state(browser)
    assert state["clicks"] == 1
    assert state["mousedowns"] == 1
    last = state["lastClick"]
    assert last["target"] == "btn"
    assert last["isTrusted"] is True
    rect = browser.execute_script(
        "const r = document.getElementById('btn').getBoundingClientRect();"
        "return {left: r.left, top: r.top, right: r.right, bottom: r.bottom}"
    )
    assert rect["left"] <= last["clientX"] <= rect["right"]
    assert rect["top"] <= last["clientY"] <= rect["bottom"]

    # A second, direct (non-human) click lands too, now on the calibrated mapping.
    browser.click("#btn", human=False)
    assert _state(browser)["clicks"] == 2


def test_dsf150_click_verifies_at_every_pixel_column(
    make_browser: MakeBrowser, site_url: str
) -> None:
    browser = _launch(make_browser, site_url, extra=["--force-device-scale-factor=1.5"])
    button = browser.query_selector("#btn")
    assert button is not None

    elsewhere = browser.query_selector("#field")
    assert elsewhere is not None
    # Three consecutive physical columns cover every residue of the 1.5 scale;
    # each click approaches with a human path from another element.
    for residue in range(3):
        elsewhere.hover(human=False)
        button.refresh()
        button.click(human=True, **_dsf_column_offsets(button, residue))
    assert _state(browser)["clicks"] == 3


# -- other gestures (zoom 100 %) -------------------------------------------------------


def test_double_click(headed: Browser) -> None:
    # README precondition: a focused window. A verified single click on another
    # element activates it, so the double click is not also the activation click.
    headed.click("#btn", human=False)
    assert headed.execute_script("return document.hasFocus()") is True

    headed.click("#dbl", click_type=ClickType.DOUBLE, human=False)

    state = _state(headed)
    counts = (state["dblclicks"], state["dblSingles"], state["mousedowns"])
    assert counts == (1, 2, 3), state


def test_typing_ascii_cyrillic_and_emoji(headed: Browser) -> None:
    text = "Hello, world 123 Привет, мир Ёж 👋🚀"

    field = headed.type("#field", text, human=False)
    assert (
        headed.execute_script("return document.getElementById('field').value") == text
    )

    # Element.type with a human rhythm; it settles before returning. The field
    # is emptied first: its own click places the caret wherever it lands.
    headed.execute_script("document.getElementById('field').value = ''")
    field.type("Ёж ok ✓ 🙂", human=True, characters_per_minute=900, offset=5)
    assert (
        headed.execute_script("return document.getElementById('field').value")
        == "Ёж ok ✓ 🙂"
    )


def test_drag_and_drop(headed: Browser) -> None:
    headed.drag_and_drop("#drag-src", "#drop-zone", human=True)

    state = _state(headed)
    assert state["dragstarts"] == 1
    assert state["dropped"] == 1
    assert state["dropData"] == "payload-42"


def test_hover(headed: Browser) -> None:
    element = headed.hover("#hover-target", human=True)

    assert element.get_attribute("id") == "hover-target"
    state = _state(headed)
    assert state["enters"] >= 1
    assert state["mousedowns"] == 0
    assert (
        headed.execute_script(
            "return document.getElementById('hover-target').matches(':hover')"
        )
        is True
    )


def test_click_on_covered_element_raises_without_press(headed: Browser) -> None:
    with pytest.raises(ClickVerificationError):
        headed.click("#covered-btn", human=False)

    state = _state(headed)
    assert state["coveredClicks"] == 0
    assert state["overlayClicks"] == 0
    assert state["mousedowns"] == 0
