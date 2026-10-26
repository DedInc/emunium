"""Shared pytest configuration for the emunium test suite.

- Puts the repository root first on ``sys.path`` so ``import emunium`` resolves
  to this checkout, and ``tests/`` itself so helpers import as
  ``from fake_extension import FakeExtension`` (``tests`` has no ``__init__.py``
  and a third-party ``tests`` package in site-packages would shadow it).
- Registers the ``e2e`` marker. End-to-end tests (real Chrome, native input)
  are skipped unless ``--e2e`` is passed or ``EMUNIUM_E2E=1`` is set.
"""

from __future__ import annotations

import os
import sys

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
        "e2e: end-to-end test that launches Chrome; runs only with --e2e or %s=1"
        % E2E_ENV,
    )


def _e2e_enabled(config) -> bool:
    return bool(config.getoption("--e2e")) or os.environ.get(E2E_ENV) == "1"


def pytest_collection_modifyitems(config, items):
    if _e2e_enabled(config):
        return
    skip_e2e = pytest.mark.skip(reason="e2e test: pass --e2e or set %s=1" % E2E_ENV)
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_e2e)
