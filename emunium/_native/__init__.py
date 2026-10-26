"""Native OS input backends and the fail-safe they share.

Contract: ``docs/architecture/native-input.md`` section 5a. A backend exposes the
pyautogui-compatible subset ``emunium._element_interactor`` uses, so pyautogui
itself (macOS) satisfies the same protocol. ``emunium._screen.input_backend``
picks the process-wide backend; this package only knows which native backend
fits a platform.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from emunium.errors import NativeInputError

FAILSAFE_ENV = "EMUNIUM_FAILSAFE"
_FAILSAFE_OFF = frozenset({"0", "false", "no", "off"})


class PlatformLayer(Protocol):
    """Raw releases that skip the fail-safe (pyautogui's ``platformModule``)."""

    def _position(self) -> tuple[int, int]: ...

    def _mouseUp(self, x: int, y: int, button: str) -> None: ...

    def _keyUp(self, key: str) -> None: ...


class InputBackend(Protocol):
    """The pyautogui-compatible subset every input backend provides.

    Coordinates are INPUT units (physical px on Windows/Linux). ``_pause`` is
    accepted for pyautogui compatibility; native backends never pause.
    """

    @property
    def platformModule(self) -> PlatformLayer: ...

    def position(self) -> tuple[int, int]: ...

    def size(self) -> tuple[int, int]: ...

    def moveTo(self, x: int, y: int, *, _pause: bool = False) -> None: ...

    def mouseDown(
        self,
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
        *,
        _pause: bool = False,
    ) -> None: ...

    def mouseUp(
        self,
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
        *,
        _pause: bool = False,
    ) -> None: ...

    def scroll(self, clicks: int, *, _pause: bool = False) -> None: ...

    def keyDown(self, key: str, *, _pause: bool = False) -> None: ...

    def keyUp(self, key: str, *, _pause: bool = False) -> None: ...

    def press(self, keys: str | Sequence[str], *, _pause: bool = False) -> None: ...

    def write(self, message: str, *, _pause: bool = False) -> None: ...


def failsafe_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """True unless ``EMUNIUM_FAILSAFE`` is ``0``/``false``/``no``/``off``."""
    value = (os.environ if environ is None else environ).get(FAILSAFE_ENV, "1")
    return value.strip().lower() not in _FAILSAFE_OFF


def in_failsafe_corner(point: tuple[int, int], size: tuple[int, int]) -> bool:
    """True when ``point`` is one of the four primary-screen corner pixels."""
    width, height = size
    x, y = point
    return x in (0, width - 1) and y in (0, height - 1)


def check_failsafe(
    position: Callable[[], tuple[int, int]],
    size: Callable[[], tuple[int, int]],
    environ: Mapping[str, str] | None = None,
) -> None:
    """Raise ``NativeInputError`` when the user parked the cursor in a corner.

    Read on every action (not cached) so ``EMUNIUM_FAILSAFE`` can change at runtime.
    """
    if not failsafe_enabled(environ):
        return
    point = position()
    if in_failsafe_corner(point, size()):
        raise NativeInputError(
            f"fail-safe triggered: the cursor is in a primary-screen corner {point}; "
            f"move it away to continue or set {FAILSAFE_ENV}=0 to disable the check"
        )


def native_backend(platform: str | None = None) -> InputBackend | None:
    """The native backend for ``platform``, or ``None`` to use pyautogui."""
    if (platform or sys.platform) == "win32":
        from emunium._native.win32 import Win32Backend

        return Win32Backend()
    return None
