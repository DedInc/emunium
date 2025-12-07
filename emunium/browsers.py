import asyncio

from .base import ClickType, EmuniumBase


class EmuniumSelenium(EmuniumBase):
    """Selenium WebDriver integration for human-like automation."""

    def __init__(self, driver):
        super().__init__()
        self.driver = driver

    async def _get_browser_properties_if_not_found(self):
        await super()._get_browser_properties_if_not_found(self.driver.save_screenshot)

    def get_center(self, element):
        """Get element center coordinates with browser offset."""
        asyncio.run(self._get_browser_properties_if_not_found())
        return self._get_center(element.location, element.size)

    def move_to(self, element, offset_x=None, offset_y=None):
        """Move cursor to element with optional offset."""
        center = self.get_center(element)
        self._move(center, offset_x, offset_y)

    def click_at(self, element, click_type=ClickType.LEFT):
        """Click at element center."""
        center = self.get_center(element)
        self._click([center["x"], center["y"]], click_type=click_type)

    def type_at(
        self,
        element,
        text,
        characters_per_minute=280,
        offset=20,
        click_type=ClickType.LEFT,
    ):
        """Click element and type text with human-like timing."""
        center = self.get_center(element)
        self._click([center["x"], center["y"]], click_type=click_type)
        self._silent_type(text, characters_per_minute, offset)

    def scroll_to(self, element):
        """Scroll to bring element into view."""
        asyncio.run(self._get_browser_properties_if_not_found())
        self._scroll_smoothly_to_element(element.rect)


class EmuniumPpeteer(EmuniumBase):
    """Pyppeteer integration for human-like automation."""

    def __init__(self, page):
        super().__init__()
        self.page = page

    async def _get_browser_properties_if_not_found(self):
        async def screenshot_func(path):
            await self.page.screenshot(path=path)

        await super()._get_browser_properties_if_not_found(screenshot_func)

    async def get_center(self, element):
        """Get element center coordinates with browser offset."""
        await self._get_browser_properties_if_not_found()

        rect = await element.boundingBox()
        if rect is None:
            return None

        return self._get_center(rect, rect)

    async def move_to(self, element, offset_x=None, offset_y=None):
        """Move cursor to element with optional offset."""
        center = await self.get_center(element)
        if center:
            self._move(center, offset_x, offset_y)

    async def click_at(self, element, click_type=ClickType.LEFT):
        """Click at element center."""
        center = await self.get_center(element)
        if center:
            self._click([center["x"], center["y"]], click_type=click_type)

    async def type_at(
        self,
        element,
        text,
        characters_per_minute=280,
        offset=20,
        click_type=ClickType.LEFT,
    ):
        """Click element and type text with human-like timing."""
        center = await self.get_center(element)
        if center:
            self._click([center["x"], center["y"]], click_type=click_type)
            self._silent_type(text, characters_per_minute, offset)

    async def scroll_to(self, element):
        """Scroll to bring element into view."""
        await self._get_browser_properties_if_not_found()

        element_rect = await element.boundingBox()
        if element_rect:
            self._scroll_smoothly_to_element(element_rect)


class EmuniumPlaywright(EmuniumBase):
    """Playwright integration for human-like automation."""

    def __init__(self, page):
        super().__init__()
        self.page = page

    async def _get_browser_properties_if_not_found(self):
        async def screenshot_func(path):
            viewport_size = self.page.viewport_size
            clip = {
                "x": 0,
                "y": 0,
                "width": viewport_size["width"] // 2,
                "height": viewport_size["height"] // 2,
            }
            await self.page.screenshot(path=path, clip=clip)

        await super()._get_browser_properties_if_not_found(screenshot_func)

    async def get_center(self, element):
        """Get element center coordinates with browser offset."""
        await self._get_browser_properties_if_not_found()

        rect = await element.bounding_box()
        if rect is None:
            return None

        return self._get_center(rect, rect)

    async def move_to(self, element, offset_x=None, offset_y=None):
        """Move cursor to element with optional offset."""
        center = await self.get_center(element)
        if center:
            self._move(center, offset_x, offset_y)

    async def click_at(self, element, click_type=ClickType.LEFT):
        """Click at element center."""
        center = await self.get_center(element)
        if center:
            self._click([center["x"], center["y"]], click_type=click_type)

    async def type_at(
        self,
        element,
        text,
        characters_per_minute=280,
        offset=20,
        click_type=ClickType.LEFT,
    ):
        """Click element and type text with human-like timing."""
        center = await self.get_center(element)
        if center:
            self._click([center["x"], center["y"]], click_type=click_type)
            self._silent_type(text, characters_per_minute, offset)

    async def scroll_to(self, element):
        """Scroll to bring element into view."""
        await self._get_browser_properties_if_not_found()

        element_rect = await element.bounding_box()
        if element_rect:
            self._scroll_smoothly_to_element(element_rect)
