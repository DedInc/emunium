"""Shared pytest configuration for the emunium test suite.

- Puts the repository root first on ``sys.path`` so ``import emunium`` resolves
  to this checkout, and ``tests/`` itself so helpers import as
  ``from fake_extension import FakeExtension`` (``tests`` has no ``__init__.py``
  and a third-party ``tests`` package in site-packages would shadow it).
- Registers the ``e2e`` marker. End-to-end tests (real Chrome, native input)
  are skipped unless ``--e2e`` is passed or ``EMUNIUM_E2E=1`` is set.
- Arms the input safety net (``input_safety_net``) around every test not
  marked ``e2e``: the real OS input entry points raise ``RuntimeError`` so a
  code path that bypasses an injected fake cannot reach the desktop.
"""

from __future__ import annotations

import ctypes
import importlib.abc
import os
import sys
from collections.abc import Callable, Iterator, Sequence
from importlib.machinery import ModuleSpec
from types import ModuleType
from typing import Any

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
for _path in (TESTS_DIR, REPO_ROOT):
    if _path in sys.path:
        sys.path.remove(_path)
    sys.path.insert(0, _path)

E2E_ENV = "EMUNIUM_E2E"


def pytest_addoption(parser):
    parser.addoption(
        "--e2e",
        action="store_true",
        default=False,
        help="run end-to-end tests that launch Chrome and drive native input",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        f"e2e: end-to-end test that launches Chrome; runs only with --e2e or {E2E_ENV}=1",
    )


def _e2e_enabled(config) -> bool:
    return bool(config.getoption("--e2e")) or os.environ.get(E2E_ENV) == "1"


def pytest_collection_modifyitems(config, items):
    if _e2e_enabled(config):
        return
    skip_e2e = pytest.mark.skip(reason=f"e2e test: pass --e2e or set {E2E_ENV}=1")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_e2e)


# -- input safety net ----------------------------------------------------------

REAL_INPUT_MESSAGE = "real OS input attempted in a unit test"

# Module attributes replaced by traps, per module name. Only names the module
# actually defines are trapped (platform modules differ by OS).
_PYAUTOGUI_PUBLIC = (
    "moveTo",
    "moveRel",
    "move",
    "dragTo",
    "dragRel",
    "drag",
    "click",
    "leftClick",
    "rightClick",
    "middleClick",
    "doubleClick",
    "tripleClick",
    "mouseDown",
    "mouseUp",
    "scroll",
    "hscroll",
    "vscroll",
    "keyDown",
    "keyUp",
    "press",
    "hotkey",
    "hold",
    "typewrite",
    "write",
    "screenshot",
    "locateOnScreen",
    "locateAllOnScreen",
    "locateCenterOnScreen",
    "pixel",
    "pixelMatchesColor",
)
# pyautogui.platformModule (_pyautogui_win / _pyautogui_x11 / _pyautogui_osx).
# _position and _size only read state and stay real.
PYAUTOGUI_PLATFORM_TRAPS = (
    "_moveTo",
    "_mouseDown",
    "_mouseUp",
    "_click",
    "_multiClick",
    "_dragTo",
    "_sendMouseEvent",
    "_scroll",
    "_hscroll",
    "_vscroll",
    "_keyDown",
    "_keyUp",
    "_normalKeyEvent",
    "_specialKeyEvent",
)
MODULE_TRAPS: dict[str, tuple[str, ...]] = {
    "pyautogui": _PYAUTOGUI_PUBLIC,
    "keyboard": ("send", "press_and_release", "write", "press", "release"),
    "pyperclip": ("copy", "paste"),
    "humancursor": ("SystemCursor",),
    "humancursor.system_cursor": ("SystemCursor",),
}
# user32 exports that inject input.
USER32_TRAPS = frozenset({"SendInput", "SetCursorPos", "mouse_event", "keybd_event"})
# libXtst exports that inject input, and the libX11 call that rewrites the
# server's keyboard mapping (emunium._native.x11 types missing characters on a
# temporarily remapped keycode).
XLIB_TRAPS = frozenset(
    {
        "XTestFakeMotionEvent",
        "XTestFakeButtonEvent",
        "XTestFakeKeyEvent",
        "XChangeKeyboardMapping",
    }
)
# Trapped by name on every ctypes library object, whatever library it loaded.
CTYPES_TRAPS = USER32_TRAPS | XLIB_TRAPS

# True only while an e2e test runs. A trap that leaked past teardown (a module
# that bound ``user32.SendInput`` at import inside a unit test) raises unless
# real input is allowed, and then forwards to the real function.
_REAL_INPUT_ALLOWED = False


class InputTrap:
    """Stands in for one real OS input entry point."""

    def __init__(
        self, target: str, resolve: Callable[[], Any], owner: object = None
    ) -> None:
        self.target = target
        self.owner = owner
        self._resolve = resolve

    def real(self) -> Any:
        """The entry point this trap stands in for."""
        return self._resolve()

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if not _REAL_INPUT_ALLOWED:
            raise RuntimeError(REAL_INPUT_MESSAGE)
        return self.real()(*args, **kwargs)

    def __repr__(self) -> str:
        return f"<InputTrap {self.target}>"


def _imported_from_spec(module: ModuleType) -> bool:
    """True for a really imported module, False for a test's stand-in."""
    spec = getattr(module, "__spec__", None)
    return spec is not None and spec.origin is not None


class _ArmingLoader(importlib.abc.Loader):
    """Runs the real loader, then arms the freshly executed module."""

    def __init__(self, loader: Any, arm: Callable[[ModuleType], None]) -> None:
        self._loader = loader
        self._arm = arm

    def create_module(self, spec: ModuleSpec) -> ModuleType | None:
        return self._loader.create_module(spec)

    def exec_module(self, module: ModuleType) -> None:
        self._loader.exec_module(module)
        self._arm(module)

    def __getattr__(self, name: str) -> Any:
        # get_resource_reader, is_package, get_source ... stay the real loader's.
        return getattr(self._loader, name)


class _ArmingFinder(importlib.abc.MetaPathFinder):
    """Arms a trapped module the moment it is first imported during a test."""

    def __init__(self, arm: Callable[[ModuleType], None]) -> None:
        self._arm = arm

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        if fullname not in MODULE_TRAPS:
            return None
        for finder in list(sys.meta_path):
            if finder is self:
                continue
            find = getattr(finder, "find_spec", None)
            if find is None:
                continue
            spec = find(fullname, path, target)
            if spec is None:
                continue
            if spec.loader is not None and hasattr(spec.loader, "exec_module"):
                spec.loader = _ArmingLoader(spec.loader, self._arm)
            return spec
        return None


class SafetyNet:
    """Replaces real input entry points with traps; ``disarm`` restores them."""

    def __init__(self) -> None:
        self._undo: list[tuple[object, str, object]] = []
        self._finder = _ArmingFinder(self.arm_module)

    @staticmethod
    def is_trap(candidate: object) -> bool:
        return isinstance(candidate, InputTrap)

    def _replace(
        self, owner: object, name: str, replacement_for: Callable[[Any], object]
    ) -> None:
        current = getattr(owner, name)
        real = current.real() if isinstance(current, InputTrap) else current
        # This net's own trap, bound from an already armed module during import
        # (humancursor re-exports system_cursor.SystemCursor), is undone to the
        # real object; another (outer) net's trap is put back as it was.
        own = isinstance(current, InputTrap) and current.owner is self
        setattr(owner, name, replacement_for(real))
        self._undo.append((owner, name, real if own else current))

    def _trap_attribute(self, owner: object, name: str, label: str) -> None:
        if hasattr(owner, name):
            self._replace(
                owner, name, lambda real: InputTrap(label, lambda: real, self)
            )

    def arm_module(self, module: ModuleType) -> None:
        name = module.__name__
        for attribute in MODULE_TRAPS.get(name, ()):
            self._trap_attribute(module, attribute, f"{name}.{attribute}")
        if name == "pyautogui":
            platform_module = getattr(module, "platformModule", None)
            if platform_module is not None:
                for attribute in PYAUTOGUI_PLATFORM_TRAPS:
                    self._trap_attribute(
                        platform_module,
                        attribute,
                        f"pyautogui.platformModule.{attribute}",
                    )

    def _arm_ctypes(self) -> None:
        real_getattr = ctypes.CDLL.__getattr__
        real_getitem = ctypes.CDLL.__getitem__

        def make_trap(library: ctypes.CDLL, name: str) -> InputTrap:
            label = f"ctypes {getattr(library, '_name', '?')}.{name}"
            return InputTrap(label, lambda: real_getitem(library, name))

        # Traps are returned uncached so no library object keeps one after
        # disarm; WinDLL, OleDLL, windll.* and the private libX11/libXtst
        # handles of emunium._native.x11 all inherit these two methods.
        def trapped_getattr(library: ctypes.CDLL, name: str) -> Any:
            if name in CTYPES_TRAPS:
                return make_trap(library, name)
            return real_getattr(library, name)

        def trapped_getitem(library: ctypes.CDLL, name_or_ordinal: Any) -> Any:
            if name_or_ordinal in CTYPES_TRAPS:
                return make_trap(library, name_or_ordinal)
            return real_getitem(library, name_or_ordinal)

        self._replace(ctypes.CDLL, "__getattr__", lambda _real: trapped_getattr)
        self._replace(ctypes.CDLL, "__getitem__", lambda _real: trapped_getitem)
        # Function pointers CDLL.__getattr__ already cached on shared windll libs.
        loader = getattr(ctypes, "windll", None)
        if loader is None:
            return
        for library in list(vars(loader).values()):
            if isinstance(library, ctypes.CDLL):
                for name in sorted(CTYPES_TRAPS & set(vars(library))):
                    self._trap_attribute(library, name, f"ctypes cached {name}")

    def arm(self) -> None:
        self._arm_ctypes()
        for name in MODULE_TRAPS:
            module = sys.modules.get(name)
            if isinstance(module, ModuleType) and _imported_from_spec(module):
                self.arm_module(module)
        sys.meta_path.insert(0, self._finder)

    def disarm(self) -> None:
        while self._finder in sys.meta_path:
            sys.meta_path.remove(self._finder)
        while self._undo:
            owner, name, original = self._undo.pop()
            setattr(owner, name, original)


@pytest.fixture(autouse=True)
def input_safety_net(request: pytest.FixtureRequest) -> Iterator[SafetyNet | None]:
    """Trap real OS input for every test not marked ``e2e``.

    Autouse, so it is set up before (and torn down after) the fixtures and
    monkeypatches a test uses: fakes injected by a test sit above the traps.
    """
    global _REAL_INPUT_ALLOWED
    if request.node.get_closest_marker("e2e") is not None:
        _REAL_INPUT_ALLOWED = True
        try:
            yield None
        finally:
            _REAL_INPUT_ALLOWED = False
        return
    net = SafetyNet()
    net.arm()
    try:
        yield net
    finally:
        net.disarm()
