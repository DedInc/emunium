# 🤖 Emunium

Emunium is a Python module that helps you automate interactions in a human-like way. It works with standalone applications or browsers when using Selenium, Pyppeteer, or Playwright. Emunium makes the mouse movements, clicks, typing, and scrolling appear more natural, which can help your tests avoid detection.

![Emunium preview](https://raw.githubusercontent.com/DedInc/emunium/main/preview.gif)

---

## 🚀 Quickstart (Standalone)

Below is a basic example that shows how to search for an image on your screen, type some text, and click a button. This example uses standalone mode.

```python
from emunium import Emunium, ClickType

# Create an instance of Emunium
emunium = Emunium()

# Find a text field on the screen using an image of the field
elements = emunium.find_elements('field.png', min_confidence=0.8)

# Type into the first found element
emunium.type_at(elements[0], 'Automating searches')

# Find the search icon using an image and click it
elements = emunium.find_elements('search_icon.png', min_confidence=0.8)
emunium.click_at(elements[0])
```

---

## 🔍 OCR Text Search (only in Standalone)

Emunium can also search for text on the screen using Optical Character Recognition (OCR). To use this feature, create your Emunium instance with OCR enabled. This uses [EasyOCR](https://github.com/JaidedAI/EasyOCR) under the hood.

### How It Works

The new `find_text_elements()` method scans the screen for text that matches your query. You can adjust the minimum confidence and limit the number of results.

### Example

```python
from emunium import Emunium

# Create an Emunium instance with OCR enabled.
emunium = Emunium(ocr=True, use_gpu=True, langs=['en']) # use_gpu is default True, langs is default ['en'], ocr is default False

# Search for text that contains the word "Submit"
text_elements = emunium.find_text_elements('Submit', min_confidence=0.8) # min_confidence is default 0.8

# If the text is found, click on the first occurrence.
if text_elements:
    emunium.click_at(text_elements[0])
```

*Note:* Make sure you have EasyOCR installed by running `pip install easyocr` before using the OCR feature.

---

Quickstarts for one of more cases. The code below opens DuckDuckGo, types a query, and clicks the search button.

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

# Wait for the search field to be clickable and type your query
element = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, '[data-state="suggesting"]')))
emunium.type_at(element, 'Automating searches')

# Find and click the search button
submit = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, '[aria-label="Search"]')))
emunium.click_at(submit)

driver.quit()
```

---

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

asyncio.run(main())
```

---

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

---

## 🖱️ Mouse Movements and Clicks

Emunium simulates natural mouse movements and clicks:

- **Moving the Mouse:**
  The `move_to()` method moves the cursor smoothly to the target position. You can add small random offsets for a more human-like behavior.

- **Clicking Elements:**
  Use `click_at()` to click on an element after moving to it. You can specify the type of click (left, right, middle, or double):

  ```python
  from emunium import ClickType

  emunium.click_at(element)                    # left click
  emunium.click_at(element, ClickType.RIGHT)   # right click
  emunium.click_at(element, ClickType.MIDDLE)  # middle click
  emunium.click_at(element, ClickType.DOUBLE)  # double click
  ```

---

## 🔎 Finding Elements on the Screen (only in Standalone)

Emunium uses image matching to find elements:

- **find_elements():**
  Locate elements on the screen using an image file.

  ```python
  elements = emunium.find_elements('search_icon.png', min_confidence=0.8)
  ```

  You can also set target sizes and limit the number of elements found.

---

## ⌨️ Typing Text

The `type_at()` method moves to an element, clicks on it, and types text in a "silent" way. This method mimics human typing by spreading out key presses with small, random delays.

Options include:
- `characters_per_minute`: Typing speed (default is 280 CPM).
- `offset`: Random delay (default is 20ms).

---

## 📜 Scrolling Pages

The `scroll_to()` method scrolls smoothly to bring an element into view. It uses timeouts and checks to ensure smooth scrolling even when there are minor hiccups.

---

## 🏁 Conclusion

Emunium provides a set of easy-to-use tools for automating user interactions. Whether you need to automate clicks, type text, or even search for text on your screen using OCR, Emunium offers flexible solutions for both browser and standalone applications. Its human-like behavior helps make your tests more robust and less likely to be detected as automation.
