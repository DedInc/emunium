import asyncio
import math
import os
import random
import tempfile
import time
import struct

import keyboard
import pyautogui
import pyclick

def get_image_size(file_path):
    with open(file_path, 'rb') as file:
        file.seek(16)
        width_bytes = file.read(4)
        height_bytes = file.read(4)
        width = struct.unpack('>I', width_bytes)[0]
        height = struct.unpack('>I', height_bytes)[0]
        return (width, height,)

class EmuniumSelenium:
    def __init__(self, driver):
        self.driver = driver
        self.clicker = pyclick.HumanClicker()
        self.browser_offsets = ()
        self.browser_inner_window = ()
        
    def _get_browser_properties_if_not_found(self):
        if not self.browser_offsets or not self.browser_inner_window:
            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as temp_file:
                temp_screen_path = temp_file.name
            self.driver.save_screenshot(temp_screen_path)
            location = pyautogui.locateOnScreen(temp_screen_path, confidence=0.8)
            self.browser_offsets = (location.left, location.top,)
            self.browser_inner_window = get_image_size(temp_screen_path)
            os.remove(temp_screen_path)

    def get_center(self, element):
        self._get_browser_properties_if_not_found()

        element_location = element.location
        offset_to_screen_x, offset_to_screen_y = self.browser_offsets
        element_x = element_location['x'] + offset_to_screen_x
        element_y = element_location['y'] + offset_to_screen_y

        element_size = element.size
        centered_x = element_x + (element_size['width'] // 2)
        centered_y = element_y + (element_size['height'] // 2)
    
        return {
            'x': centered_x,
            'y': centered_y
        }

    def find_and_move(self, element, click=False, offset_x=random.uniform(0.0, 1.5), offset_y=random.uniform(0.0, 1.5)):
        center = self.get_center(element)
        target_x, target_y = round(center['x'] + offset_x), round(center['y'] + offset_y)

        current_x, current_y = pyautogui.position()
        distance = math.sqrt((target_x - current_x) ** 2 + (target_y - current_y) ** 2)

        speed = max(random.uniform(0.3, 0.6), min(random.uniform(2.0, 2.5), distance / random.randint(500, 700)))

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
        self._get_browser_properties_if_not_found()

        element_rect = element.rect        
        window_width = self.browser_inner_window[0]
        window_height = self.browser_inner_window[1]
        
        scroll_amount = element_rect['y'] - window_height // 2
        scroll_steps = abs(scroll_amount) // 100
        
        if scroll_amount > 0:
            scroll_direction = -1
        else:
            scroll_direction = 1
        
        for _ in range(scroll_steps):
            pyautogui.scroll(scroll_direction * 100)
            time.sleep(random.uniform(0.05, 0.1))
        
        remaining_scroll = scroll_amount % 100
        if remaining_scroll != 0:
            pyautogui.scroll(scroll_direction * remaining_scroll)
            time.sleep(random.uniform(0.05, 0.1))

class EmuniumPpeteer:
    def __init__(self, page):
        self.page = page        
        self.clicker = pyclick.HumanClicker()
        self.browser_offsets = ()
        self.browser_inner_window = ()

    async def _get_browser_properties_if_not_found(self):
        if not self.browser_offsets or not self.browser_inner_window:
            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as temp_file:
                temp_screen_path = temp_file.name
            await self.page.screenshot(path=temp_screen_path)
            location = pyautogui.locateOnScreen(temp_screen_path, confidence=0.8)
            self.browser_offsets = (location.left, location.top,)
            self.browser_inner_window = get_image_size(temp_screen_path)
            os.remove(temp_screen_path)

    async def get_center(self, element):
        await self._get_browser_properties_if_not_found()

        rect = await element.boundingBox()
        if rect is None:
            return None

        offset_to_screen_x, offset_to_screen_y = self.browser_offsets
        element_x = rect['x'] + offset_to_screen_x
        element_y = rect['y'] + offset_to_screen_y
        element_width = rect['width']
        element_height = rect['height']
        centered_x = element_x + (element_width // 2)
        centered_y = element_y + (element_height // 2)
        return {
            'x': centered_x,
            'y': centered_y
        }

    async def find_and_move(self, element, click=False, offset_x=random.uniform(0.0, 1.5), offset_y=random.uniform(0.0, 1.5)):
        center = await self.get_center(element)        
        target_x, target_y = round(center['x'] + offset_x), round(center['y'] + offset_y)

        current_x, current_y = pyautogui.position()
        distance = math.sqrt((target_x - current_x) ** 2 + (target_y - current_y) ** 2)

        speed = max(random.uniform(0.3, 0.6), min(random.uniform(2.0, 2.5), distance / random.randint(500, 700)))

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
        await self._get_browser_properties_if_not_found()

        element_rect = await element.boundingBox()
        if element_rect is None:
            return None

        window_width = self.browser_inner_window[0]
        window_height = self.browser_inner_window[1]
        
        scroll_amount = element_rect['y'] - window_height // 2
        scroll_steps = abs(scroll_amount) // 100
        
        if scroll_amount > 0:
            scroll_direction = -1
        else:
            scroll_direction = 1
        
        for _ in range(scroll_steps):
            pyautogui.scroll(scroll_direction * 100)
            await asyncio.sleep(random.uniform(0.05, 0.1))
        
        remaining_scroll = scroll_amount % 100
        if remaining_scroll != 0:
            pyautogui.scroll(scroll_direction * remaining_scroll)
            await asyncio.sleep(random.uniform(0.05, 0.1))