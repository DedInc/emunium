import asyncio
import keyboard
import time
import random
import pyautogui
import math
import pyclick

class EmuniumSelenium:
    def __init__(self, driver):
        self.driver = driver
        self.clicker = pyclick.HumanClicker()
        
    def get_center(self, element):
        coords = self.driver.execute_script("""
            var element = arguments[0];
            var rect = element.getBoundingClientRect();
            var centerX = rect.left + rect.width / 2 + window.scrollX;
            var centerY = rect.top + rect.height / 2 + (window.scrollY || window.pageYOffset) + (screen.height - window.innerHeight) / 1.5;

            return {x: centerX, y: centerY};
        """, element)

        return {
            'x': coords['x'],
            'y': coords['y']
        }

    def find_and_move(self, element, click=False, offset_x=0, offset_y=0):
        center = self.get_center(element)
        target_x, target_y = round(center['x'] + offset_x), round(center['y'] + offset_y)

        current_x, current_y = pyautogui.position()
        distance = math.sqrt((target_x - current_x) ** 2 + (target_y - current_y) ** 2)

        speed = max(0.3, min(2.0, distance / 500))

        self.clicker.move((target_x, target_y), speed)

        if click:
            self.clicker.click()

    def silent_type(self, text, characters_per_minute=280, offset=20):
        total_chars = len(text)
        time_per_char = 60 / characters_per_minute

        for i, char in enumerate(text):
            randomized_offset = random.uniform(-offset, offset) / 1000
            delay = time_per_char + randomized_offset

            keyboard.write(char)
            time.sleep(delay)

    def scroll_smoothly_to_element(self, element):
        def is_element_in_viewport(element):
            return self.driver.execute_script("""
                var rect = arguments[0].getBoundingClientRect();
                return (
                    rect.top >= 0 &&
                    rect.left >= 0 &&
                    rect.bottom <= (window.innerHeight || document.documentElement.clientHeight) &&
                    rect.right <= (window.innerWidth || document.documentElement.clientWidth)
                );
            """, element)

        def scroll_to_element(element):
            self.driver.execute_script("arguments[0].scrollIntoView({ behavior: 'smooth', block: 'start' });", element)

        interval = 0.1
        start_time = time.time()

        while not is_element_in_viewport(element):
            if time.time() - start_time > 5:
                break

            scroll_to_element(element)
            time.sleep(interval)

class EmuniumPpeteer:
    def __init__(self, page):
        self.page = page        
        self.clicker = pyclick.HumanClicker()

    async def get_center(self, element):
        return await self.page.evaluate('''(element) => {
            const rect = element.getBoundingClientRect();
            const centerX = rect.left + rect.width / 2 + window.scrollX;
            const centerY = rect.top + rect.height / 2 + (window.scrollY || window.pageYOffset) + (screen.height - window.innerHeight) / 1.4;
            return {x: centerX, y: centerY};
        }''', element)

    async def find_and_move(self, element, click=False, offset_x=0, offset_y=0):
        center = await self.get_center(element)        
        target_x, target_y = round(center['x'] + offset_x), round(center['y'] + offset_y)

        current_x, current_y = pyautogui.position()
        distance = math.sqrt((target_x - current_x) ** 2 + (target_y - current_y) ** 2)

        speed = max(0.3, min(2.0, distance / 500))

        self.clicker.move((target_x, target_y), speed)

        if click:
            self.clicker.click()

    async def silent_type(self, text, characters_per_minute=280, offset=20):
        total_chars = len(text)
        time_per_char = 60 / characters_per_minute

        for i, char in enumerate(text):
            randomized_offset = random.uniform(-offset, offset) / 1000
            delay = time_per_char + randomized_offset

            keyboard.write(char)
            await asyncio.sleep(delay)

    async def scroll_smoothly_to_element(self, element):
        is_element_in_viewport = await self.page.evaluate('''(element) => {
            const rect = element.getBoundingClientRect();
            return (
                rect.top >= 0 &&
                rect.left >= 0 &&
                rect.bottom <= (window.innerHeight || document.documentElement.clientHeight) &&
                rect.right <= (window.innerWidth || document.documentElement.clientWidth)
            );
        }''', element)

        scroll_to_element = await self.page.evaluate('''(element) => {
            element.scrollIntoView({ behavior: "smooth", block: "start" });
        }''', element)

        interval = 0.1
        start_time = time.time()

        while not is_element_in_viewport:
            if time.time() - start_time > 5:
                break

            await scroll_to_element
            await asyncio.sleep(interval)