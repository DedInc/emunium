"""Attach mode: persistent, stable-id extension builds for browsers emunium does not start.

Contract: ``docs/architecture/attach-mode.md``.
"""

from emunium._attach.build import (
    DEFAULT_PORT,
    ExtensionBuildError,
    build_extension,
    extension_id,
    load_or_create_state,
    state_dir,
)

__all__ = [
    "DEFAULT_PORT",
    "ExtensionBuildError",
    "build_extension",
    "extension_id",
    "load_or_create_state",
    "state_dir",
]
