# Emunium

Python library for human-like browser automation. Adds randomized mouse movements, realistic typing patterns, and smooth scrolling to Selenium, Pyppeteer, and Playwright. Also works standalone with image/OCR detection.

![Preview](https://raw.githubusercontent.com/DedInc/emunium/main/preview.gif)

## Installation

```bash
pip install emunium
```

For OCR support (standalone mode only):
```bash
pip install easyocr
```

## Usage

### Standalone mode

Uses image matching to find and interact with screen elements. Useful for automating desktop applications.

```python
from emunium import Emunium, ClickType

emunium = Emunium()

# Find and type into a text field
elements = emunium.find_elements('field.png', min_confidence=0.8)
emunium.type_at(elements[0], 'Automating searches')

# Find and click search button
elements = emunium.find_elements('search_icon.png', min_confidence=0.8)
emunium.click_at(elements[0])
```

### OCR text detection

Requires `easyocr` to be installed. Finds text on screen instead of using images.

```python
from emunium import Emunium

emunium = Emunium(ocr=True, use_gpu=True, langs=['en'])

text_elements = emunium.find_text_elements('Submit', min_confidence=0.8)
if text_elements:
    emunium.click_at(text_elements[0])
```

### Selenium

```python
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from emunium import EmuniumSelenium

driver = webdriver.Chrome()
wait = WebDriverWait(driver, 10)
emunium = EmuniumSelenium(driver)

driver.get('https://duckduckgo.com/')

element = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, '[data-state="suggesting"]')))
emunium.type_at(element, 'Automating searches')

submit = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, '[aria-label="Search"]')))
emunium.click_at(submit)

driver.quit()
```

### Pyppeteer

```python
import asyncio
from pyppeteer import launch
from emunium import EmuniumPpeteer

async def main():
    browser = await launch(headless=False)
    page = await browser.newPage()
    emunium = EmuniumPpeteer(page)

    await page.goto('https://duckduckgo.com/')

    element = await page.waitForSelector('[data-state="suggesting"]')
    await emunium.type_at(element, 'Automating searches')

    submit = await page.waitForSelector('[aria-label="Search"]')
    await emunium.click_at(submit)

    await browser.close()

asyncio.run(main())
```

### Playwright

```python
import asyncio
from playwright.async_api import async_playwright
from emunium import EmuniumPlaywright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        emunium = EmuniumPlaywright(page)

        await page.goto('https://duckduckgo.com/')

        element = await page.wait_for_selector('[data-state="suggesting"]')
        await emunium.type_at(element, 'Automating searches')

        submit = await page.wait_for_selector('[aria-label="Search"]')
        await emunium.click_at(submit)

        await browser.close()

asyncio.run(main())
```

## API

### Mouse

`move_to(element, offset_x=0, offset_y=0)` - Moves cursor to element with bezier curves and random variations

`click_at(element, click_type=ClickType.LEFT)` - Clicks element after moving to it. Click types: `LEFT`, `RIGHT`, `MIDDLE`, `DOUBLE`

```python
from emunium import ClickType

emunium.click_at(element)                    # left click
emunium.click_at(element, ClickType.RIGHT)   # right click
emunium.click_at(element, ClickType.MIDDLE)  # middle click
emunium.click_at(element, ClickType.DOUBLE)  # double click
```

### Keyboard

`type_at(element, text, characters_per_minute=280, offset=20)` - Types text with randomized delays between keystrokes

### Scroll

`scroll_to(element)` - Smoothly scrolls element into view

### Element detection (standalone only)

`find_elements(image_path, min_confidence=0.8, target_width=None, target_height=None, max_elements=None)` - Finds elements on screen by matching image template

`find_text_elements(text, min_confidence=0.8, max_elements=None)` - Finds text on screen using OCR (requires `ocr=True` in constructor)

## License

[MIT](LICENSE)
