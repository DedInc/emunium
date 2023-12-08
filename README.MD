# 🤖 Emunium

A Python module for automating interactions to mimic human behavior in browsers when using Selenium or Pyppeteer. Provides utilities to programmatically move the mouse cursor, click on page elements, type text, and scroll as if performed by a human user.

## 🚀 Quickstart (with Selenium)

![Emunium preview](https://raw.githubusercontent.com/DedInc/emunium/main/preview.gif)

```python
import time
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
emunium.find_and_move(element, click=True)

emunium.silent_type('Automating searches')

submit = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, '[aria-label="Search"]')))
emunium.find_and_move(submit, click=True)
time.sleep(5)
driver.quit()
```

## 🚀 Quickstart (with Pyppeteer)

![Emunium preview](https://raw.githubusercontent.com/DedInc/emunium/main/preview_ppeteer.gif)

```python
import asyncio
import pyppeteer
from emunium import EmuniumPpeteer

async def main():
    browser = await pyppeteer.launch(headless=False)  
    page = await browser.newPage()
    await page.bringToFront()
    emunium = EmuniumPpeteer(page)

    await page.goto('https://duckduckgo.com/')

    element = await page.waitForSelector('[data-state="suggesting"]')
    await emunium.find_and_move(element, click=True)

    await emunium.silent_type('Automating searches')

    submit = await page.waitForSelector('[aria-label="Search"]')
    await emunium.find_and_move(submit, click=True)
    await asyncio.sleep(5)

    await browser.close()

asyncio.get_event_loop().run_until_complete(main())
```

## 🖱 Moving the Mouse

The `find_and_move()` method moves the mouse cursor smoothly to the provided element with small randomizations in speed and path to seem human. 

Options:

- `click` - click the element after moving to it
- `offset_x` and `offset_y` - offset mouse position from element center

## ⌨ Typing Text

The `silent_type()` method types the provided text in a "silent" way, spreading out key presses over time with small randomizations to mimic human typing.

Options: 

- `characters_per_minute` - typing speed in characters per minute (default 250)
- `offset` - randomization in milliseconds between key presses (default 20ms)

## 🖱 Scrolling Pages

The `scroll_smoothly_to_element()` method scrolls the page to bring the provided element into view using smooth scrolling.

Includes timeouts and checks to handle issues scroll getting stuck.

## 🏁 Conclusion

Emunium provides a set of utilities to help automate browser interactions in a more human-like way when using Selenium. By moving the mouse, clicking, typing, and scrolling in a less robotic fashion, tests can avoid detection and run more reliably.

While basic Selenium or Pyppeteer scripts can still get the job done, Emunium aims to make tests appear even more life-like. Using the randomizations and smooth behaviors it offers can be beneficial for automation projects that require avoiding detections.