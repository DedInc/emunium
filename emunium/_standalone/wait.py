from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from emunium._standalone.facade import Emunium


def wait_for_image(
    emunium: Emunium,
    image_path: str,
    timeout: float = 10.0,
    poll_interval: float = 0.3,
    min_confidence: float = 0.8,
    target_height: int | None = None,
    target_width: int | None = None,
    raise_on_timeout: bool = True,
) -> dict[str, int] | None:
    """Poll for an image template match on screen until found or timeout."""
    deadline = time.monotonic() + timeout
    while True:
        matches = emunium.find_elements(
            image_path,
            min_confidence=min_confidence,
            target_height=target_height,
            target_width=target_width,
            max_elements=1,
        )
        if matches:
            return matches[0]
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_interval)
    if raise_on_timeout:
        msg = f"Image not found after {timeout}s: {image_path!r}"
        raise TimeoutError(msg)
    return None


def wait_for_text_ocr(
    emunium: Emunium,
    query: str,
    timeout: float = 10.0,
    poll_interval: float = 0.3,
    min_confidence: float = 0.8,
    region: tuple[int, int, int, int] | None = None,
    raise_on_timeout: bool = True,
) -> dict[str, int] | None:
    """Poll for OCR text match on screen until found or timeout."""
    deadline = time.monotonic() + timeout
    while True:
        matches = emunium.find_text_elements(
            query,
            min_confidence=min_confidence,
            max_elements=1,
            region=region,
        )
        if matches:
            return matches[0]
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_interval)
    if raise_on_timeout:
        msg = f"Text not found after {timeout}s: {query!r}"
        raise TimeoutError(msg)
    return None
