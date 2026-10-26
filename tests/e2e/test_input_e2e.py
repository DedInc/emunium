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
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import pytest

from emunium import (
    Browser,
    ClickType,
    ClickVerificationError,
    Element,
    NativeInputError,
)

pytestmark = pytest.mark.e2e

MakeBrowser = Callable[..., Browser]

WINDOW_ARGS = ["--window-position=60,40", "--window-size=900,600"]
# Chrome zoom levels are log base 1.2 of the zoom factor.
ZOOM_150_LEVEL = math.log(1.5) / math.log(1.2)
# --force-device-scale-factor=1.5 on a 96 DPI desktop: 1.5 input px per CSS px.
DSF_INPUT_PER_CSS = 1.5
# Physical column residue (x % 3) where the 1 px jiggle is seen by Chrome in
# this window placement (measured: residues 0 and 1 verify, 2 never does).
DSF_GOOD_RESIDUE = 0


def _seed_default_zoom(profile: Path, level: float) -> None:
    """Write Chrome's profile-wide default zoom before the first launch."""
    default_dir = profile / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    prefs = {"partition": {"default_zoom_level": {"x": level}}}
    (default_dir / "Preferences").write_text(json.dumps(prefs), encoding="utf-8")


def _launch(
    make_browser: MakeBrowser, site_url: str, *, extra: list[str] | None = None, **kwargs: Any
) -> Browser:
    browser = make_browser(extra_args=[*WINDOW_ARGS, *(extra or [])], **kwargs)
    browser.goto(site_url + "/input.html")
    _await_foreground(browser)
    return browser


def _await_foreground(browser: Browser, timeout: float = 10.0) -> None:
    """Precondition for real input: the test window is on top and visible.

    Windows' foreground lock stops a Chrome started from a background process
    from coming to the front, so it can open behind the terminal; an occluded
    window receives no pointer events (the product then correctly raises
    ``NativeInputError``). The window is made topmost (z-order only, no input);
    the first verified click then activates it the way a user click does.
    """
    if sys.platform == "win32":
        token = f"e2e-{uuid.uuid4().hex}"
        browser.execute_script(f"document.title = {json.dumps(token)}")
        _raise_window_topmost(token, timeout)
        browser.execute_script("document.title = 'E2E Input'")
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


def _raise_window_topmost(title_prefix: str, timeout: float) -> None:
    """``SetWindowPos(HWND_TOPMOST)`` on the visible top-level window titled ``title_prefix...``."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = (enum_proc, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = (
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    )
    user32.SetWindowPos.restype = wintypes.BOOL

    hwnd_topmost = wintypes.HWND(-1)
    swp_nosize, swp_nomove, swp_showwindow = 0x0001, 0x0002, 0x0040

    found: list[int] = []

    def visit(hwnd: int, _lparam: int) -> bool:
        if user32.IsWindowVisible(hwnd):
            buffer = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buffer, 512)
            if buffer.value.startswith(title_prefix):
                found.append(hwnd)
                return False
        return True

    callback = enum_proc(visit)
    deadline = time.monotonic() + timeout
    while not found:
        user32.EnumWindows(callback, 0)
        if found:
            break
        if time.monotonic() >= deadline:
            pytest.fail(f"no visible window titled {title_prefix!r} within {timeout}s")
        time.sleep(0.1)
    if not user32.SetWindowPos(
        found[0], hwnd_topmost, 0, 0, 0, 0, swp_nosize | swp_nomove | swp_showwindow
    ):
        pytest.fail(f"SetWindowPos(HWND_TOPMOST) failed: {ctypes.get_last_error()}")


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

    if config == "dsf150":
        # Pinned to a column where the 1 px verification jiggle crosses a DIP
        # boundary; random columns hit the defect pinned by the xfail below.
        button.click(human=True, **_dsf_column_offsets(button, DSF_GOOD_RESIDUE))
    else:
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
    if config == "dsf150":
        button.refresh()
        button.click(human=False, **_dsf_column_offsets(button, DSF_GOOD_RESIDUE))
    else:
        browser.click("#btn", human=False)
    assert _state(browser)["clicks"] == 2


@pytest.mark.xfail(
    strict=True,
    raises=NativeInputError,
    reason=(
        "product defect: with --force-device-scale-factor=1.5 a verified click "
        "whose human approach ends at a physical column x % 3 == 2 raises "
        "NativeInputError 'no fresh pointer event' without pressing, although the "
        "cursor is on the target: the page logged no pointermove for the last "
        "approach step nor for Element._verify's fixed JIGGLE_PX = 1 jiggle "
        "(1 px = 0.67 DIP), so random click points fail about 1 in 3 at DSF 1.5 "
        "(measured 9/9 failures at x % 3 == 2, 0 at the other columns)"
    ),
)
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
    assert headed.execute_script(
        "return document.getElementById('field').value"
    ) == text

    # Element.type with a human rhythm; it settles before returning. The field
    # is emptied first: its own click places the caret wherever it lands.
    headed.execute_script("document.getElementById('field').value = ''")
    field.type("Ёж ok ✓ 🙂", human=True, characters_per_minute=1200, offset=5)
    assert headed.execute_script(
        "return document.getElementById('field').value"
    ) == "Ёж ok ✓ 🙂"


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
    assert headed.execute_script(
        "return document.getElementById('hover-target').matches(':hover')"
    ) is True


def test_click_on_covered_element_raises_without_press(headed: Browser) -> None:
    with pytest.raises(ClickVerificationError):
        headed.click("#covered-btn", human=False)

    state = _state(headed)
    assert state["coveredClicks"] == 0
    assert state["overlayClicks"] == 0
    assert state["mousedowns"] == 0
