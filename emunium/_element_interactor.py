from __future__ import annotations

import random
import time

import pyautogui
import pyperclip

from emunium._standalone.config import ClickType

try:
    from humancursor import SystemCursor
except ImportError:
    SystemCursor = None

try:
    import keyboard as _keyboard
except ImportError:
    _keyboard = None


class ElementInteractor:
    def screen_point(
        self,
        screen_x: float,
        screen_y: float,
        *,
        offset_x: float | None = None,
        offset_y: float | None = None,
    ) -> tuple[int, int]:
        x_offset = offset_x if offset_x is not None else random.uniform(0.0, 1.5)
        y_offset = offset_y if offset_y is not None else random.uniform(0.0, 1.5)
        return round(screen_x + x_offset), round(screen_y + y_offset)

    def move_cursor(self, x: int, y: int, *, human: bool = True) -> None:
        if human and SystemCursor is not None:
            SystemCursor().move_to([x, y])
            return
        pyautogui.moveTo(x=x, y=y)

    def click(
        self,
        x: int,
        y: int,
        *,
        click_type: ClickType = ClickType.LEFT,
        human: bool = True,
    ) -> None:
        if click_type == ClickType.LEFT:
            self._click_left(x, y, human=human)
            return
        if click_type == ClickType.DOUBLE:
            self._double_click(x, y, human=human)
            return
        self.move_cursor(x, y, human=human)
        pyautogui.click(x=x, y=y, button=self._button_name(click_type))

    def type_text(
        self,
        text: str,
        *,
        characters_per_minute: int = 280,
        offset: int = 20,
    ) -> None:
        if characters_per_minute > 0:
            self._type_with_rhythm(
                text,
                characters_per_minute=characters_per_minute,
                offset=offset,
            )
            return
        if self._is_ascii_text(text):
            pyautogui.typewrite(text, interval=0.03)
            return
        self._paste_text(text)

    def drag(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        *,
        human: bool = True,
    ) -> None:
        if human and SystemCursor is not None:
            SystemCursor().drag_and_drop(start, end)
            return
        pyautogui.moveTo(*start)
        pyautogui.drag(end[0] - start[0], end[1] - start[1], duration=0.5)

    def _click_left(self, x: int, y: int, *, human: bool) -> None:
        if human and SystemCursor is not None:
            SystemCursor().click_on([x, y])
            return
        pyautogui.click(x=x, y=y)

    def _double_click(self, x: int, y: int, *, human: bool) -> None:
        if human and SystemCursor is not None:
            cursor = SystemCursor()
            cursor.click_on([x, y])
            time.sleep(0.08)
            cursor.click_on([x, y])
            return
        pyautogui.doubleClick(x=x, y=y)

    def _type_with_rhythm(
        self,
        text: str,
        *,
        characters_per_minute: int,
        offset: int,
    ) -> None:
        delay = 60.0 / characters_per_minute
        for character in text:
            jitter = delay + random.uniform(-offset, offset) / 1000
            if _keyboard is not None:
                _keyboard.write(character)
            else:
                pyautogui.typewrite(character, interval=0)
            time.sleep(max(0, jitter))

    @staticmethod
    def _is_ascii_text(text: str) -> bool:
        return all(
            (character.isascii() and character.isprintable()) or character in "\t\n"
            for character in text
        )

    @staticmethod
    def _button_name(click_type: ClickType) -> str:
        button_map = {
            ClickType.RIGHT: "right",
            ClickType.MIDDLE: "middle",
        }
        return button_map[click_type]

    @staticmethod
    def _paste_text(text: str) -> None:
        previous_clipboard = ""
        should_restore = False
        try:
            previous_clipboard = pyperclip.paste()
            should_restore = True
        except pyperclip.PyperclipException:
            should_restore = False
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.05)
        if should_restore:
            try:
                pyperclip.copy(previous_clipboard)
            except pyperclip.PyperclipException:
                pass
