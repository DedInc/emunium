"""Per-browser copies of the packaged extension.

The packaged ``manifest.json`` is the Chromium manifest. Firefox gets a top-level
overlay on it: each overlay key replaces the base key and ``None`` deletes it.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Union

TARGETS = ("chromium", "firefox")
BASE_TARGET = "chromium"
EXTENSION_SOURCE_DIR = Path(__file__).resolve().parent.parent / "extension"
MANIFEST_FILE = "manifest.json"
ALWAYS_IGNORE = ("__pycache__",)
DEFAULT_IGNORE = ("port.json", "session.json")

FIREFOX_GECKO_ID = "bridge@emunium.dev"
# Optional in Firefox: moved out of "permissions" into "optional_permissions".
FIREFOX_OPTIONAL_PERMISSIONS = ("userScripts",)
# Measured on Firefox 154: MV3 loads only with background.scripts (listed in the
# importScripts order of background.js); the default extension-page CSP carries
# upgrade-insecure-requests, which turns ws://127.0.0.1 into wss://; a temporary
# install needs a gecko id. "permissions" is derived from the base manifest.
FIREFOX_OVERLAY: dict[str, object] = {
    "background": {
        "scripts": [
            "background_state.js",
            "background_network.js",
            "background_handlers.js",
            "background_runtime.js",
            "background.js",
        ]
    },
    "minimum_chrome_version": None,
    "content_security_policy": {"extension_pages": "script-src 'self'"},
    "browser_specific_settings": {
        "gecko": {"id": FIREFOX_GECKO_ID, "strict_min_version": "128.0"}
    },
    "optional_permissions": list(FIREFOX_OPTIONAL_PERMISSIONS),
}

PathLike = Union[str, "os.PathLike[str]"]


def manifest_for(target: str, source_dir: PathLike | None = None) -> dict[str, Any]:
    """Manifest for ``target``: the base manifest, plus the Firefox overlay."""
    _check_target(target)
    manifest = _read_object(_source(source_dir) / MANIFEST_FILE)
    if target == BASE_TARGET:
        return manifest
    permissions = manifest.get("permissions", [])
    if not isinstance(permissions, list):
        raise TypeError(f"manifest permissions must be a list, got {permissions!r}")
    # Deep copy so callers can never mutate the module-level overlay.
    overlay = copy.deepcopy(FIREFOX_OVERLAY)
    overlay["permissions"] = [
        p for p in permissions if p not in FIREFOX_OPTIONAL_PERMISSIONS
    ]
    return apply_overlay(manifest, overlay)


def apply_overlay(
    base: Mapping[str, Any], overlay: Mapping[str, object]
) -> dict[str, Any]:
    """Replace top-level keys of ``base`` with ``overlay``; ``None`` deletes the key."""
    merged: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def materialize(
    target: str,
    dest_dir: PathLike,
    *,
    source_dir: PathLike | None = None,
    ignore: Iterable[str] = DEFAULT_IGNORE,
) -> Path:
    """Copy the extension for ``target`` into ``dest_dir`` (which must not exist).

    The Chromium copy keeps the packaged ``manifest.json`` bytes untouched; other
    targets get their merged manifest written as JSON.
    """
    # Resolve the manifest first so a bad target leaves nothing behind.
    manifest = manifest_for(target, source_dir)
    dest = Path(dest_dir)
    shutil.copytree(
        str(_source(source_dir)),
        str(dest),
        ignore=shutil.ignore_patterns(*ALWAYS_IGNORE, *ignore),
    )
    if target != BASE_TARGET:
        text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        (dest / MANIFEST_FILE).write_bytes(text.encode("utf-8"))
    return dest


def _check_target(target: object) -> None:
    if not isinstance(target, str) or target not in TARGETS:
        raise ValueError(
            "unknown extension target {!r}; expected one of {}".format(
                target, ", ".join(TARGETS)
            )
        )


def _source(source_dir: PathLike | None) -> Path:
    return EXTENSION_SOURCE_DIR if source_dir is None else Path(source_dir)


def _read_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"{path} must hold a JSON object, got {type(data).__name__}")
    return data
