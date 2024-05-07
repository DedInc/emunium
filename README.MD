# 🤖 Emunium

A Python module for automating interactions to mimic human behavior in standalone apps or browsers when using Selenium, Pyppeteer, or Playwright. Provides utilities to programmatically move the mouse cursor, click on page elements, type text, and scroll as if performed by a human user.


![Emunium preview](https://raw.githubusercontent.com/DedInc/emunium/main/preview.gif)


## 🚀 Quickstart (Standalone)

```python
from emunium import Emunium

emunium = Emunium()

elements = emunium.find_elements('field.png', min_confidence=0.8)

emunium.type_at(elements[0], 'Automating searches')

elements = emunium.find_elements('search_icon.png', min_confidence=0.8)
emunium.click_at(elements[0])
```

## 🚀 Quickstart (with Selenium)

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

## 🚀 Quickstart (with Pyppeteer)

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

asyncio.get_event_loop().run_until_complete(main())
```

## 🚀 Quickstart (with Playwright)

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

## 🖱️ Moving the Mouse

The `move_to()` method moves the mouse cursor smoothly to the provided element with small randomizations in speed and path to seem human.

Options:
- `offset_x` and `offset_y` - offset mouse position from element center

## 🖱️ Clicking Elements

The `click_at()` method moves via `move_to()` and clicks at the center of the provided element.

Emunium supports multiple mouse click types:

```python
from emunium import ClickType

emunium.click_at(element)                   # left click
emunium.click_at(element, ClickType.RIGHT)  # right click  
emunium.click_at(element, ClickType.MIDDLE) # middle click
emunium.click_at(element, ClickType.DOUBLE) # double click
```

## 🔎 Finding Elements

In standalone mode, Emunium can locate elements on the screen using image matching with the `find_elements` method:

```python
elements = emunium.find_elements('search_icon.png', min_confidence=0.8)
```

The `find_elements` method takes the following parameters:

- `image_path` (required): The path to the image file to search for on the screen.
- `min_confidence` (optional, default 0.8): The minimum confidence level (between 0 and 1) for image matching. Higher values result in more precise matching but may miss some elements.
- `target_height` (optional): The expected height of the elements to find. If provided along with `target_width`, elements that don't match the specified size (within a tolerance based on `min_confidence`) will be filtered out.
- `target_width` (optional): The expected width of the elements to find. Must be provided together with `target_height`.
- `max_elements` (optional, default 0): The maximum number of elements to return. If set to 0 or not provided, all matching elements will be returned.

The `find_elements` method returns a list of dictionaries, each containing the 'x' and 'y' coordinates of the center point of a matched element.


## ⌨️ Typing Text

The `type_at()` method moves to the provided element via `move_to()`, clicks it via `click_to()`, and types the provided text in a "silent" way, spreading out key presses over time with small randomizations to mimic human typing.

Options:
- `characters_per_minute` - typing speed in characters per minute (default 280)
- `offset` - randomization (threshold) in milliseconds between key presses (default 20ms)

## 📜 Scrolling Pages

The `scroll_to()` method scrolls the page to bring the provided element into view using smooth scrolling.

Includes timeouts and checks to handle issues with scrolling getting stuck.

## 🏁 Conclusion

Emunium provides a set of utilities to help automate browser interactions in a more human-like way when using Selenium, Pyppeteer, or Playwright. By moving the mouse, clicking, typing, and scrolling in a less robotic fashion, tests can avoid detection and run more reliably.

While basic automation scripts can still get the job done, Emunium aims to make tests appear even more life-like. Using the randomizations and smooth behaviors it offers can be beneficial for automation projects that require avoiding detections.
