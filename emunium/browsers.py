import asyncio
from typing import Optional, Dict, Callable, Any

from .base import ClickType, EmuniumBase


class BrowserEmuniumMixin:
    """Mixin providing common browser automation methods."""

    def _click_at_center(
        self, center: Optional[Dict[str, int]], click_type: ClickType
    ) -> None:
        """Perform click at center if valid."""
        if center:
            self._perform_click((center["x"], center["y"]), click_type)

    def _type_at_center(
        self,
        center: Optional[Dict[str, int]],
        text: str,
        characters_per_minute: int,
        offset: int,
        click_type: ClickType,
    ) -> None:
        """Click and type at center if valid."""
        if center:
            self._perform_click((center["x"], center["y"]), click_type)
            self._type_text(text, characters_per_minute, offset)


class EmuniumSelenium(EmuniumBase, BrowserEmuniumMixin):
    """Selenium WebDriver integration."""

    def __init__(self, driver):
        super().__init__()
        self.driver = driver
        self._properties_initialized = False

    def _ensure_properties(self) -> None:
        """Ensure browser properties are initialized."""
        if not self._properties_initialized:
            try:
                loop = asyncio.get_running_loop()
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as pool:
                    loop.run_in_executor(
                        pool,
                        lambda: asyncio.run(
                            self._get_browser_properties_if_not_found(
                                self.driver.save_screenshot
                            )
                        ),
                    )
            except RuntimeError:
                asyncio.run(
                    self._get_browser_properties_if_not_found(
                        self.driver.save_screenshot
                    )
                )
            self._properties_initialized = True

    def get_center(self, element) -> Dict[str, int]:
        """Get element center with browser offset."""
        self._ensure_properties()
        return self._calculate_center(element.location, element.size)

    def move_to(
        self,
        element,
        offset_x: Optional[float] = None,
        offset_y: Optional[float] = None,
    ) -> None:
        """Move cursor to element."""
        self._move_to_position(self.get_center(element), offset_x, offset_y)

    def click_at(self, element, click_type: ClickType = ClickType.LEFT) -> None:
        """Click element."""
        self._click_at_center(self.get_center(element), click_type)

    def type_at(
        self,
        element,
        text: str,
        characters_per_minute: int = 280,
        offset: int = 20,
        click_type: ClickType = ClickType.LEFT,
    ) -> None:
        """Click element and type text."""
        self._type_at_center(
            self.get_center(element), text, characters_per_minute, offset, click_type
        )

    def scroll_to(self, element) -> None:
        """Scroll to element."""
        self._ensure_properties()
        self._scroll_to_element(element.rect)


class AsyncBrowserEmunium(EmuniumBase, BrowserEmuniumMixin):
    """Base class for async browser integrations."""

    def __init__(self, page):
        super().__init__()
        self.page = page

    async def _init_properties(self, screenshot_func: Callable) -> None:
        """Initialize browser properties with screenshot function."""
        await super()._get_browser_properties_if_not_found(screenshot_func)

    async def _get_element_rect(self, element) -> Optional[Dict[str, Any]]:
        """Get element bounding box. Override in subclasses."""
        raise NotImplementedError

    async def get_center(self, element) -> Optional[Dict[str, int]]:
        """Get element center with browser offset."""
        rect = await self._get_element_rect(element)
        return self._calculate_center(rect, rect) if rect else None

    async def move_to(
        self,
        element,
        offset_x: Optional[float] = None,
        offset_y: Optional[float] = None,
    ) -> None:
        """Move cursor to element."""
        center = await self.get_center(element)
        if center:
            self._move_to_position(center, offset_x, offset_y)

    async def click_at(self, element, click_type: ClickType = ClickType.LEFT) -> None:
        """Click element."""
        self._click_at_center(await self.get_center(element), click_type)

    async def type_at(
        self,
        element,
        text: str,
        characters_per_minute: int = 280,
        offset: int = 20,
        click_type: ClickType = ClickType.LEFT,
    ) -> None:
        """Click element and type text."""
        self._type_at_center(
            await self.get_center(element),
            text,
            characters_per_minute,
            offset,
            click_type,
        )

    async def scroll_to(self, element) -> None:
        """Scroll to element."""
        element_rect = await self._get_element_rect(element)
        if element_rect:
            self._scroll_to_element(element_rect)


class EmuniumPpeteer(AsyncBrowserEmunium):
    """Pyppeteer integration. All methods are async."""

    async def _get_browser_properties_if_not_found(self) -> None:
        await self._init_properties(lambda path: self.page.screenshot(path=path))

    async def _get_element_rect(self, element) -> Optional[Dict[str, Any]]:
        await self._get_browser_properties_if_not_found()
        return await element.boundingBox()


class EmuniumPlaywright(AsyncBrowserEmunium):
    """Playwright integration. All methods are async."""

    async def _get_browser_properties_if_not_found(self) -> None:
        async def screenshot_func(path: str) -> None:
            viewport_size = self.page.viewport_size
            clip = {
                "x": 0,
                "y": 0,
                "width": viewport_size["width"] // 2,
                "height": viewport_size["height"] // 2,
            }
            await self.page.screenshot(path=path, clip=clip)

        await self._init_properties(screenshot_func)

    async def _get_element_rect(self, element) -> Optional[Dict[str, Any]]:
        await self._get_browser_properties_if_not_found()
        return await element.bounding_box()
