# Emunium

**[Documentation](https://emunium.github.io/docs/) · [Quick start](https://emunium.github.io/docs/quick-start/) · [Changelog](https://emunium.github.io/changelog/)**

**Browser automation with real OS mouse and keyboard input. No WebDriver. No CDP-driven page actions.**

Emunium is for browser automation where protocol-generated clicks and keystrokes are not what you want. It reads the page through a small browser extension, then moves the real cursor, clicks, types and drags through the operating system. On Windows that means Win32 `SendInput` / `SetCursorPos`; on Linux, X11 XTest; on macOS, the native desktop path used by `pyautogui`.

It works with installed Chrome, Edge, Brave and Chromium, can attach to your own persistent browser profile, and supports Firefox-family browsers such as Camoufox through `Browser.connect()`. Chrome 137+ is handled without a remote-debugging TCP port for extension loading: Emunium uses a private debugging pipe for that step, while its own extension bridge stays local on `127.0.0.1`.

Human input is built in. The default pointer planner uses sigma-lognormal strokes based on the Plamondon model, Fitts' law timing, drift, tremor and occasional corrective movement. You can switch between `natural`, `careful` and `brisk` presets without tuning the math yourself.

Emunium does **not** claim to bypass Cloudflare, anti-fraud systems or bot detection. Its difference is architectural: browser state is read through an extension, while input is delivered through the OS instead of WebDriver/CDP input commands.

![Preview](https://raw.githubusercontent.com/DedInc/emunium/main/preview.gif)

## Why Emunium?

| | Typical browser automation | Emunium |
|---|---|---|
| Clicks and typing | Browser automation protocol | Real OS mouse and keyboard input |
| Cursor | Browser-level pointer actions | The real system cursor moves |
| Input verification | Framework-dependent | Checks that the page sees the pointer over the target before clicking |
| Browser profile | Often a dedicated automation session | Installed browsers, persistent profiles, or attach to your own browser |
| Human-like motion | Usually custom work | `natural`, `careful`, `brisk` presets built in |
| Browser control | WebDriver / browser protocol stack | Extension bridge for page state + OS-native input |
| Camoufox | Separate integration work | Supported through `Browser.connect()` with a prepared profile |

If you need a headless scraper, Playwright or Selenium may be the simpler tool. If you need a visible browser driven through the same mouse and keyboard path as a desktop user, that is the problem Emunium is built around.

## Install

```bash
pip install emunium
```

Python 3.10+ is required.

See [installation](https://emunium.github.io/docs/installation/) for platform requirements and optional extras.

## Quick start

```python
from emunium import Browser, expect

with Browser() as browser:
    browser.goto("https://example.com/")
    expect(browser).to_have_title("Example Domain")

    with browser.expect_navigation("*iana.org*") as navigation:
        browser.get_by_role("link").first.click()
    print(navigation.value.title)
```

The [locator API](https://emunium.github.io/docs/locators/) supports CSS, XPath, roles, labels, placeholders and test ids. Actions wait for the element and then use native input. The [quick-start guide](https://emunium.github.io/docs/quick-start/) also covers application flows, assertions and network waits.

## Browsers

By default, Emunium launches the first installed browser it finds in this order:

```text
Chrome -> Edge -> Brave -> Chromium -> Chrome for Testing
```

Choose one explicitly when needed:

```python
from emunium import Browser

with Browser(browser="chrome") as browser:
    browser.goto("https://example.com/")
```

Supported launch targets include `chrome`, `edge`, `brave`, `chromium`, `cft`, `auto`, or a Chromium executable path.

See [browser setup](https://emunium.github.io/docs/browsers/) for discovery, extension installation and browser-specific requirements.

### Camoufox and other Firefox-family browsers

Firefox-family browsers use `Browser.connect()` and a persistent Emunium extension. Pass a prepared Camoufox profile instead of asking Emunium to manufacture or modify its fingerprint:

```python
from emunium import Browser

camoufox_path = r"C:\Browsers\Camoufox\camoufox.exe"
camoufox_profile = r"C:\Browsers\Camoufox\profiles\automation"

with Browser.connect(
    browser=camoufox_path,
    user_data_dir=camoufox_profile,
    timeout=120.0,
) as browser:
    browser.goto("https://example.com/")
    print(browser.title)
```

The same attach mode works with your own Chrome, Edge, Brave or Chromium profile when the Emunium extension is installed there.

Follow the [connection guide](https://emunium.github.io/docs/connect/) to install the persistent extension and prepare a Firefox or Camoufox profile before running this example.

## Human input presets

The default is `natural`.

```python
from emunium import Browser

browser = Browser(motion="natural", typing="natural")
browser = Browser(motion="careful", typing="careful")
browser = Browser(motion="brisk", typing="brisk")
```

| Preset | Pointer | Typing |
|---|---|---|
| `natural` | General-purpose human-like movement | 280 characters/minute median |
| `careful` | Slower, more corrective movement | 200 characters/minute median |
| `brisk` | Faster movement | 420 characters/minute median |

You normally do not need to tune anything below this level.

See [human-like input](https://emunium.github.io/docs/human-like-input/) for preset behavior, custom planners and typing options.

<details>
<summary>How the motion model works</summary>

The default `SigmaDriftPlanner` combines:

- sigma-lognormal strokes inspired by Plamondon's model;
- Fitts' law for movement duration: `a + b * log2(D / W + 1)`;
- target-size-aware timing;
- tremor and slow drift;
- occasional overshoot or undershoot followed by a corrective sub-stroke.

Alternative planners are `BezierTremorPlanner`, `WindMousePlanner` and `LinearPlanner`. Custom planners can implement the `MotionPlanner` protocol.

The [motion planner reference](https://emunium.github.io/docs/human-like-input/#motion-planners) covers timing, parameter ranges and validation rules. See [typing rhythm](https://emunium.github.io/docs/human-like-input/#typing-rhythm) for the keystroke model.

</details>

## Native input

On Windows, mouse and keyboard events are sent through Win32 APIs. Unicode text is emitted as keystrokes with `SendInput`, so Cyrillic, CJK, accented characters and emoji do not depend on the active keyboard layout.

On Linux, input goes through X11 XTest and requires X11 or XWayland. On macOS, desktop input uses `pyautogui` and the required Accessibility permissions.

Before a click, Emunium verifies that the page actually sees the pointer over the target. If it cannot verify the target, it raises an error instead of clicking blindly.

See [mouse and keyboard](https://emunium.github.io/docs/mouse-and-keyboard/) for input actions and [coordinates, DPI and zoom](https://emunium.github.io/docs/coordinates/) for click verification and multi-monitor behavior.

## Standalone desktop automation

Emunium can also drive non-browser desktop applications through screenshot template matching and OCR:

```bash
pip install "emunium[standalone,ocr]"
```

```python
from emunium import Emunium

emu = Emunium(ocr=True)
button = emu.find_text_elements("Sign in")
if button:
    emu.click_at(button[0])
```

The [standalone guide](https://emunium.github.io/docs/standalone/) covers image matching, OCR, regions and waiting for screen content. For HTML queries without a live browser, see [PageParser](https://emunium.github.io/docs/parsing/).

## Important limitations

- Native input requires a visible browser window. Headless mode supports DOM/navigation work, not real mouse or keyboard input.
- Browser element automation is top-frame only; iframes are not traversed.
- Linux native input requires X11 or XWayland.
- The real cursor and keyboard are used, so do not fight the automation by using them at the same time.
- Camoufox support means Emunium can attach to a prepared Camoufox profile; Emunium does not create or guarantee any particular fingerprint.
- Emunium makes no promise of being undetectable or of bypassing anti-bot / anti-fraud systems.

See the full [limitations](https://emunium.github.io/docs/limitations/), [headless mode](https://emunium.github.io/docs/headless/) and [parallel execution](https://emunium.github.io/docs/parallel/) guides before choosing a deployment setup.

## Documentation

The full guides and API reference live at **[emunium.github.io](https://emunium.github.io/docs/)**. Continue with [element actions](https://emunium.github.io/docs/elements/), [waits](https://emunium.github.io/docs/waiting/), [network responses](https://emunium.github.io/docs/network-waits/) or [typed errors](https://emunium.github.io/docs/errors/).

For release changes, read the [changelog](https://emunium.github.io/changelog/) or [`CHANGELOG.md`](CHANGELOG.md). Documentation corrections are welcome in [the website repository](https://github.com/emunium/emunium.github.io); library bugs and feature requests belong in [DedInc/emunium](https://github.com/DedInc/emunium/issues).

## License

[MIT](https://github.com/DedInc/emunium/blob/main/LICENSE)
