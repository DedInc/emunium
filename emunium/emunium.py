import pyautogui
import keyboard
import time
import random

class Emunium:
    def __init__(self, driver):
        self.driver = driver
        
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
        target_x, target_y = center['x'], center['y']

        target_x += offset_x
        target_y += offset_y

        current_x, current_y = pyautogui.position()
        distance = ((target_x - current_x) ** 2 + (target_y - current_y) ** 2) ** 0.5
        steps = random.randint(5, 10)
        speed = random.uniform(0.1, 0.15)

        steps = round(steps / 1.5)

        x_distance = (target_x - current_x) / steps
        y_distance = (target_y - current_y) / steps

        for step in range(steps):
            pyautogui.moveTo(current_x + x_distance, current_y + y_distance, duration=speed)
            current_x, current_y = pyautogui.position()
            remaining_distance = ((target_x - current_x) ** 2 + (target_y - current_y) ** 2) ** 0.5
            time.sleep(0.01)

            if remaining_distance < distance / 2:
                speed /= 2

        pyautogui.moveTo(target_x, target_y, duration=speed)

        if click:
            pyautogui.click(target_x, target_y)

    def silent_type(self, text, characters_per_minute=250, offset=20):
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