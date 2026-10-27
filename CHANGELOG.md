# Changelog

## 3.1.0

Stability release. Several behaviours change in ways existing scripts can
observe; they are listed first.

### Behaviour changes

- **Typed exceptions.** Every failure raises a subclass of `emunium.EmuniumError`
  (`StaleElementError`, `TargetClosedError`, `NavigationTimeoutError`,
  `ScriptError`, `NativeInputError`, `LaunchError`, ...) carrying `.code`,
  `.method` and `.message`. `Bridge` methods no longer return
  `{"error": ...}` dicts. Timeout classes also derive from `TimeoutError`, and
  the other classes from the matching built-in (`ValueError`, `RuntimeError`,
  `ConnectionError`), so existing `except` clauses keep matching. All classes
  are exported from `emunium`.
- **`execute_script` returns JSON values.** The script runs as a function body
  (use `return`; promises are awaited) and the result is converted to a JSON
  value: `undefined` becomes `None`, DOM nodes become small descriptor dicts,
  circular references become `"[Circular]"`. A throwing script raises
  `ScriptError`; a page CSP that blocks evaluation raises `CSPBlockedError`.
- **Detached waits return `None`.** A wait satisfied by the absence of an
  element (for example `Wait().detached()`) returns `None` instead of an element
  handle, since there is no element to act on.
- **`new_tab` switches the pinned tab.** `new_tab()` activates the new tab and
  makes it the target of later commands; `new_tab(url, switch=False)` opens it
  in the background without moving the pin. Closing the pinned tab pins the
  active tab.
- **`PageParser` raises without selectolax.** Query methods raise `ImportError`
  when `emunium[parsing]` is not installed, instead of returning empty results
  that looked like "no match". `PageParser.title` still falls back to a regex.
- **Headless browsers refuse native input.** Click, type, hover and drag
  methods raise `NativeInputError` on a `Browser(headless=True)` instead of
  moving the cursor to positions of an invisible window.
- **Clicks are verified.** Before a button is pressed, the page must report
  the pointer over the target element; otherwise the position is corrected up
  to twice and `ClickVerificationError` is raised. `Browser(verify_input=False)`
  restores unverified clicks.
- **Typing checks focus.** `type()` raises `NativeInputError` when the element
  (or the browser window) does not hold keyboard focus, instead of sending
  keystrokes blind, and waits until the page shows the typed text.
- **Typing speed is validated.** With `human=True`, `characters_per_minute`
  outside 60-900 raises `InvalidArgumentError`, as does an `offset` outside
  0-1000 ms or not below the median interval `60000 / characters_per_minute`
  ms. A `characters_per_minute` of 0 or less still types without delay. Both
  arguments now default to `None`, which keeps the typing rhythm's own values
  (280 characters per minute and 20 ms for the `natural` preset).
- **Custom interactors take a target size.** An interactor passed to
  `BrowserContext(interactor=...)` must accept
  `move(x, y, *, human=True, target_size=None)`; element actions pass the
  element's size on every human move.
- **Network waits are scoped.** `wait_for_response` only matches responses of
  the pinned tab (`any_tab=True` to widen) that finished after the last action
  (`since=` to override). In URL patterns only `*` is a wildcard.
- **Python 3.10+** is required.
- **Results are dict subclasses.** `goto`, `page_info`, `tab_info`, `tabs`,
  `wait_for_response`, `expect_response(...).value` and `get_center` return
  `PageInfo`, `TabInfo`, `NetworkResponse` or `Point`; `new_tab`,
  `switch_to_tab` and `close_tab` return a `Record`. Keys, `x["key"]`, `==`
  with a dict, `json.dumps`, `repr()` and `print()` are those of the plain
  dict, but `type(x) is dict` is now `False`, serializers that accept only
  exact dicts (for example `yaml.safe_dump`) need `dict(x)`, and a pickled
  result needs emunium importable to load.
- **Element-wait timeouts raise `WaitTimeoutError`.** `wait`,
  `wait_for_element`, `wait_for_xpath`, `wait_for_text` and every action given
  a selector raise `WaitTimeoutError` (still a `TimeoutError`, chained from the
  extension's error) instead of a plain `TimeoutError`; so does an unmatched
  `expect_response` block. `str(e)` now ends with ` (url=..., title=...)`
  (element waits) and a `\nhint: ...` line when a hint is known; `e.args` is
  unchanged. A selector resolving to no element raises `ElementNotFoundError`
  (still a `LookupError`) instead of a bare `LookupError`.
- **A timed-out element wait probes the page.** Before raising, it makes up to
  two best-effort requests of at most 2 s each: `pageInfo` (only when the
  extension's message lacks url/title) and a count of the selector's matches
  for the hint. Probe failures are ignored. `raise_on_timeout=False` makes no
  probes.
- **Click-verification hint is generic.** The pointer probe does not report
  which element is under the cursor, so the hint of `ClickVerificationError`
  suggests an overlay or a moving element instead of naming it.
- **Installed browsers are launched by default.** Without `browser=`,
  `chrome_path` or `EMUNIUM_CHROME_PATH`, `launch()` starts the first installed
  of Google Chrome, Edge, Brave and Chromium instead of downloading Chrome for
  Testing; Chrome for Testing is downloaded only when none is installed.
  `Browser(browser="cft")` keeps the previous behaviour.

### Added

- `Browser.goto(..., wait_until="commit" | "domcontentloaded" | "load")`.
- `Browser.switch_to_tab(tab_id)` and `Browser.tabs()`.
- `Browser.expect_response(pattern)` context manager and the `since=` /
  `any_tab=` parameters of `wait_for_response`.
- `Browser(chrome_path=..., extra_args=[...], verify_input=True)`.
- Element handles remember their tab and document: actions address that tab,
  and a handle from a replaced document raises `StaleElementError`.
- `Emunium(ocr=True)` as an alias of `ocr_enabled=True`; OCR initialization
  errors surface in the constructor.
- Motion planners for `human=True` pointer moves: `SigmaDriftPlanner`
  (default; sigma-lognormal strokes with Fitts' law timing),
  `BezierTremorPlanner`, `WindMousePlanner` and `LinearPlanner`, each
  non-linear one with the presets `natural`, `careful` and `brisk`. Select one
  with `Browser(motion=...)` or `Emunium(motion=...)`, or pass any object
  implementing the `MotionPlanner` protocol. Every parameter is checked against
  a documented range and raises `InvalidArgumentError` outside it.
- Typing rhythm for `human=True` typing: `TypingRhythm` with lognormal
  intervals, word and sentence pauses, shift and key-pair (bigram) factors and
  occasional micro-pauses; presets `natural`, `careful`, `brisk`, selected with
  `Browser(typing=...)` or `Emunium(typing=...)`.
- `MotionPlanner`, `MotionStep`, `ScreenBounds`, the planner classes,
  `FittsTiming`, `Tremor`, `Overshoot` and `TypingRhythm` are exported from
  `emunium`.
- On Windows, the browser window is brought to the foreground after `launch()`
  and before every native action, without sending keys or clicks; when Windows
  keeps another window in front, the action raises `NativeInputError`.
- On Windows, `EMUNIUM_FAILSAFE=0` disables the screen-corner fail-safe of the
  built-in input backend (which raises `NativeInputError`); the same holds for
  the Linux backend. On macOS pyautogui's own fail-safe
  (`pyautogui.FailSafeException`) still applies.
- Native X11 input backend on Linux: pointer, buttons, wheel and keys go through
  libX11 and the XTest extension via `ctypes` (needs an X server or XWayland
  and the system packages `libx11-6` and `libxtst6`). A pure Wayland session
  without XWayland raises `NativeInputError`.
- Unicode typing on Linux without the clipboard: Cyrillic, CJK and emoji are
  typed as keystrokes whatever the keyboard layout, characters the layout lacks
  on briefly remapped spare keycodes that are restored afterwards. Run one
  emunium process per X display.
- `python tests/docker/run_linux.py` builds a Docker image (Xvfb, openbox,
  Chromium) and runs the unit and e2e suites on Linux in it.
- Result records (`emunium.Record`, `PageInfo`, `TabInfo`, `NetworkResponse`,
  `Point`): keys can be read as snake_case attributes (`info.ready_state`,
  `tab.tab_id`, `response.status`, `response.ok`); a missing key raises
  `AttributeError` naming it. `Bridge` keeps returning plain dicts.
- Exceptions carry a `.hint` (keyword-only `hint=` argument; shown as a
  `hint: ...` line in `str(exc)` and kept by pickling). Hints are given for
  element-wait timeouts, headless native input, unverified clicks and windows
  that deliver no pointer events.
- `ElementNotFoundError` (also a `LookupError`) and `ExpectationError` (also an
  `AssertionError`), exported from `emunium`.
- `Wait` operators: `a | b`, `a & b` and `~a` build a new `Wait` with the
  conditions of `Wait().any_of(a, b)`, `Wait().all_of(a, b)` and
  `Wait().not_(a)`; the operands are left unchanged.
- Keyword-only `strategy=` and `condition=` on `wait_for_xpath` and
  `wait_for_text`, with the same meaning as in `Browser.wait`.
- `Browser.wait_for_url(url, *, timeout=30.0, wait_until="load")`: waits until
  the pinned tab's URL matches a glob (the `wait_for_response` syntax), a
  compiled `re.Pattern` or a predicate, then for the `wait_until` state; it
  returns the URL and raises `NavigationTimeoutError` naming the last URL seen.
- `Browser.expect_navigation(url=None, *, timeout=30.0, wait_until="load")`
  context manager: on exit, waits until the URL changes (and matches `url`
  when given) and the page has loaded; `.value` is the new `PageInfo`.
- Live locators: `Browser.locator(selector)` (`css=`, `xpath=`, `text=`, bare
  `//` / `(//` XPath, else CSS), `get_by_role(role, *, name=None,
  exact=False)`, `get_by_label`, `get_by_placeholder`, `get_by_test_id` and
  `Browser.test_id_attribute` (default `data-testid`) return an immutable
  `ElementLocator` that queries the page again on every call. Refine with
  `nth`, `first`, `last` and `filter(has_text=..., visible=...)`; resolve with
  `element`, `all`, `count`, `exists`, `is_visible` and `wait_for`; act with
  `click`, `double_click`, `right_click`, `hover`, `type`, `drag_to` and
  `scroll_into_view` after waiting for the actionable state. A click, hover or
  scroll whose element went stale before any button or key event is retried
  once. Role, label, placeholder and test-id locators compile to XPath (case
  folding for ASCII and Cyrillic U+0400-U+042F); the extension is unchanged.
  `Browser.get_by_text` still returns a list of elements.
- `expect(locator)` / `expect(browser)` assertions: `to_be_visible`,
  `to_be_hidden`, `to_be_attached`, `to_be_detached`, `to_have_text`,
  `to_contain_text`, `to_have_attribute`, `to_have_count`, `to_have_url`,
  `to_have_title`, each negated with `.not_`. They retry until the condition
  holds or the timeout (5 s) runs out, then raise `ExpectationError`. Text and
  title assertions are case-sensitive (`ignore_case=True` to fold); long
  texts compare in full (see the full element text read below).
  Navigations during a locator wait or an assertion are retried
  within the same deadline. `ElementLocator` and `expect` are exported from
  `emunium`.
- Full element text: `Element.inner_text()` and
  `Bridge.element_text(element_id, timeout=10.0, *, tab_id=...)` read the
  whole rendered text (`innerText`, else `textContent`, up to 1 048 576
  characters) through the new content method `elementText`. `Element.text`
  stays the 500-character preview. When that preview is at 500 characters,
  `expect` text assertions and `filter(has_text=...)` on CSS and `text=`
  locators read the full text, so needles past character 500 match and
  `to_have_text` / `not_.to_contain_text` decide on long texts. Only a text
  cut at 1 048 576 characters is still known just up to the cut.
  `ElementLocator.text()` returns the full text too (one retry when the
  element went stale), and `elementText` is re-sent once after a reconnect
  like the other reads.
- `ExpectationError` carries `.expected` (the asserted value) and `.actual`
  (the whole last value); its message shows at most the first 200 characters
  of an actual text, followed by its length (`'...' (2031 chars)`).
- Typing: the package ships `py.typed` (PEP 561), so type checkers use its
  annotations. Every public method and property of `Browser`, `Element`,
  `ElementLocator`, `Wait`, `expect`, `Emunium`, `Bridge`,
  `ResponseExpectation`, `NavigationExpectation` and the result records has a
  docstring.
- Reprs that send no request: `Browser(headless=False, launched=True,
  port=51234)`, `ResponseExpectation('**/api/*', timeout=10.0)`; an
  `Element` repr names its query selector (and kind and index when not the
  defaults).
- README quick start built around `with Browser() as browser:`, locators,
  `expect`, `expect_navigation` / `wait_for_url` and result records.
- Installed-browser support: `Browser(browser=...)` takes `"chrome"`,
  `"edge"`, `"brave"`, `"chromium"`, `"firefox"`, `"cft"`, `"auto"` or an
  executable path, and `Browser.browser_name` reports what was launched.
  Google Chrome gets the extension over `--remote-debugging-pipe`
  (`Extensions.loadUnpacked`), since it ignores `--load-extension` from
  version 137; Firefox gets it through WebDriver BiDi `webExtension.install`
  (a temporary install), with a Firefox build of the extension.
- `Browser.connect()` drives a browser profile that has the emunium extension
  installed persistently: it waits for the extension on the hub port
  (default 47800), starts the browser detached when none connects
  (`start=True`), and `close()` detaches, leaving the browser running unless
  `close_browser=True` and `connect` started it. The extension is built with
  `python -m emunium extension build --out DIR [--port N]`; the handshake is
  mutual and port-bound, and each install gets its own token.
- `hub` extra (`pip install "emunium[hub]"`): installs `cryptography`, needed
  for extension builds and `Browser.connect`.

### Changed

- Windows typing sends every character as a Unicode keystroke (`SendInput`),
  independent of the keyboard layout; nothing is pasted through the clipboard.
- On Windows, mouse and keyboard input use the Win32 API directly; pyautogui is
  no longer installed or imported there. `import emunium` makes the process
  per-monitor DPI aware and loads no GUI or imaging library.
- Coordinates account for page zoom, `devicePixelRatio` and multi-monitor
  layouts, and are calibrated from real pointer events.
- Every `Browser` runs in its own session: a private extension copy, a random
  connection token and an OS-assigned port. The shared `port.json` is gone, so
  parallel browsers no longer conflict. Launch failures clean up the Chrome
  process tree and temporary files.
- Standalone mode captures the screen with `mss` and uses
  `opencv-python-headless`; matches are ranked by score and de-duplicated, and
  coordinates are correct on Retina displays.
- On Linux, mouse and keyboard input use the built-in X11 backend; pyautogui is
  no longer installed or imported there.
- Dependencies: `websockets>=14,<18`; `pyautogui` only on macOS, which keeps
  using it for native input; `pyperclip`
  only on macOS and Linux; `HumanCursor` removed. The `standalone` and `ocr`
  extras now include `mss` and `opencv-python-headless` instead of
  `opencv-python`.
- `human=True` pointer moves follow the selected motion planner instead of a
  generic curved path; move duration follows Fitts' law from the distance and
  the target element's size.
- Package description no longer claims to be undetectable.

### Fixed

- `execute_script` bodies may use `await` directly.
- Click verification works at any `devicePixelRatio`: the pointer check moves
  the cursor by at least one CSS pixel, so the page sees an event on high-DPI
  displays too.
- Closing a headed browser on Windows no longer waits out the termination
  grace period: `close()` first closes its tabs through the extension (within
  one second), so Chrome exits on its own and usually closes in under a second.
- `Browser.connect(browser=...)` to an already-running browser names Chrome for
  Testing (`"cft"`, or `"auto"` with no installed browser) from the
  already-downloaded copy; it never downloads one just to name the browser.
- Linux `microsoft-edge` paths (`microsoft-edge-stable`, `-beta`, `-dev`) are
  named `edge` instead of `custom`.
- Logs and errors name the browser neutrally instead of always saying Chrome.
- Closing Firefox's stderr log is bounded, so `close()` no longer hangs when a
  surviving child keeps the pipe open.
- A failure while creating the CDP pipes no longer leaks the first pipe's file
  descriptors.
