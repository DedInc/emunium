"""Per-browser extension targets: manifest overlay, materialized copies, Firefox build.

Build tests run against a temporary ``EMUNIUM_HOME`` and skip when ``cryptography``
is absent (hub state needs it for every target).
"""

from __future__ import annotations

import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pytest

from emunium._attach.build import build_extension
from emunium._browser import extension_targets
from emunium._browser.extension_targets import (
    TARGETS,
    apply_overlay,
    manifest_for,
    materialize,
)

SOURCE = extension_targets.EXTENSION_SOURCE_DIR
BACKGROUND_SCRIPTS = [
    "background_state.js",
    "background_network.js",
    "background_handlers.js",
    "background_runtime.js",
    "background.js",
]

needs_crypto = pytest.mark.skipif(
    importlib.util.find_spec("cryptography") is None,
    reason="cryptography is not installed",
)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "home"
    monkeypatch.setenv("EMUNIUM_HOME", str(path))
    return path


def _base_manifest() -> dict:
    return json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))


def _shipped_files() -> set[str]:
    return {
        p.relative_to(SOURCE).as_posix()
        for p in SOURCE.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


# --- manifest ----------------------------------------------------------------


def test_targets_are_chromium_and_firefox():
    assert TARGETS == ("chromium", "firefox")


def test_chromium_manifest_is_the_packaged_manifest():
    assert manifest_for("chromium") == _base_manifest()


def test_firefox_manifest_applies_the_overlay():
    base = _base_manifest()
    firefox = manifest_for("firefox")

    assert firefox["background"] == {"scripts": BACKGROUND_SCRIPTS}
    assert "minimum_chrome_version" not in firefox
    assert firefox["content_security_policy"] == {
        "extension_pages": "script-src 'self'"
    }
    assert firefox["browser_specific_settings"] == {
        "gecko": {"id": "bridge@emunium.dev", "strict_min_version": "128.0"}
    }
    assert firefox["permissions"] == [
        p for p in base["permissions"] if p != "userScripts"
    ]
    assert firefox["optional_permissions"] == ["userScripts"]

    overlaid = {
        "background",
        "minimum_chrome_version",
        "content_security_policy",
        "browser_specific_settings",
        "permissions",
        "optional_permissions",
    }
    assert {k: v for k, v in firefox.items() if k not in overlaid} == {
        k: v for k, v in base.items() if k not in overlaid
    }
    for script in BACKGROUND_SCRIPTS:
        assert (SOURCE / script).is_file()


def test_firefox_manifest_does_not_leak_the_module_overlay():
    first = manifest_for("firefox")
    first["background"]["scripts"].clear()
    first["optional_permissions"].append("tabs")
    second = manifest_for("firefox")
    assert second["background"] == {"scripts": BACKGROUND_SCRIPTS}
    assert second["optional_permissions"] == ["userScripts"]


def test_firefox_permissions_follow_the_base_manifest(tmp_path):
    source = materialize("chromium", tmp_path / "source")
    manifest = _base_manifest()
    manifest["permissions"] = ["userScripts", "storage", "cookies"]
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert manifest_for("firefox", source)["permissions"] == ["storage", "cookies"]


def test_overlay_replaces_top_level_keys_and_null_deletes():
    base = {"a": {"x": 1, "y": 2}, "b": 1, "c": 2}
    merged = apply_overlay(base, {"a": {"x": 3}, "b": None, "d": 4, "e": None})
    assert merged == {"a": {"x": 3}, "c": 2, "d": 4}
    assert base == {"a": {"x": 1, "y": 2}, "b": 1, "c": 2}


@pytest.mark.parametrize("target", ["edge", "", "Firefox", None])
def test_unknown_target_is_rejected(target, tmp_path):
    with pytest.raises(ValueError, match="unknown extension target"):
        manifest_for(target)
    dest = tmp_path / "out"
    with pytest.raises(ValueError, match="unknown extension target"):
        materialize(target, dest)
    assert not dest.exists()


# --- materialize -------------------------------------------------------------


def test_materialize_chromium_is_the_packaged_copy(tmp_path):
    dest = materialize("chromium", tmp_path / "chromium")
    assert dest == tmp_path / "chromium"
    assert _files(dest) == _shipped_files()
    for name in _files(dest):
        assert (dest / name).read_bytes() == (SOURCE / name).read_bytes()


def test_materialize_firefox_writes_the_merged_manifest(tmp_path):
    dest = materialize("firefox", tmp_path / "firefox")
    assert _files(dest) == _shipped_files()
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == manifest_for("firefox")
    for name in _files(dest) - {"manifest.json"}:
        assert (dest / name).read_bytes() == (SOURCE / name).read_bytes()


def test_materialize_skips_session_files_and_pycache(tmp_path):
    source = tmp_path / "source"
    materialize("chromium", source)
    (source / "session.json").write_text("{}", encoding="utf-8")
    (source / "port.json").write_text("{}", encoding="utf-8")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "stale.pyc").write_bytes(b"\x00")

    dest = materialize("firefox", tmp_path / "out", source_dir=source)
    assert _files(dest) == _shipped_files()


# --- builds ------------------------------------------------------------------


@needs_crypto
def test_firefox_build_writes_folder_and_xpi(home, tmp_path):
    out = tmp_path / "dist"
    result = build_extension(str(out), target="firefox")

    unpacked = out / "emunium-extension-firefox"
    xpi = out / "emunium-extension-firefox.xpi"
    assert result["unpacked_dir"] == str(unpacked)
    assert result["xpi_path"] == str(xpi)
    assert result["extension_id"] == "bridge@emunium.dev"
    assert result["hub_url"] == "ws://127.0.0.1:47800"
    assert sorted(p.name for p in out.iterdir()) == [
        "emunium-extension-firefox",
        "emunium-extension-firefox.xpi",
    ]

    names = _files(unpacked)
    assert names == _shipped_files() | {"attach.json"}
    manifest = json.loads((unpacked / "manifest.json").read_text(encoding="utf-8"))
    assert "key" not in manifest
    assert manifest == manifest_for("firefox")
    attach = json.loads((unpacked / "attach.json").read_text(encoding="utf-8"))
    assert attach["hubUrl"] == "ws://127.0.0.1:47800"

    xpi_bytes = xpi.read_bytes()
    with zipfile.ZipFile(io.BytesIO(xpi_bytes)) as archive:
        assert set(archive.namelist()) == names
        for name in archive.namelist():
            assert archive.read(name) == (unpacked / name).read_bytes()

    again = build_extension(str(out), target="firefox")
    assert Path(again["xpi_path"]).read_bytes() == xpi_bytes


def test_build_rejects_unknown_target(tmp_path):
    with pytest.raises(ValueError, match="unknown extension target"):
        build_extension(str(tmp_path / "dist"), target="safari")
    assert not (tmp_path / "dist").exists()
