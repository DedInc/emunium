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
