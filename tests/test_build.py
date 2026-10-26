"""Attach-mode extension builds: state, stable id, folder/zip/CRX3 artifacts and CLI.

Contract: ``docs/architecture/attach-mode.md`` sections 1 and 3. Every test runs
against a temporary ``EMUNIUM_HOME``; RSA tests skip when ``cryptography`` is absent.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import struct
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from emunium.__main__ import main
from emunium._attach import build
from emunium._attach.build import (
    ExtensionBuildError,
    build_extension,
    extension_id,
    load_or_create_state,
    state_dir,
)

HAVE_CRYPTOGRAPHY = importlib.util.find_spec("cryptography") is not None
needs_crypto = pytest.mark.skipif(
    not HAVE_CRYPTOGRAPHY, reason="cryptography is not installed"
)

# Same pattern as HUB_URL_RE in emunium/extension/background_state.js.
HUB_URL_RE = re.compile(r"^ws://127\.0\.0\.1:([1-9][0-9]{0,4})/?$")
ID_RE = re.compile(r"^[a-p]{32}$")


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "home"
    monkeypatch.setenv("EMUNIUM_HOME", str(path))
    return path


@pytest.fixture
def source(tmp_path, monkeypatch) -> Path:
    """A copy of the packaged extension polluted with per-session files."""
    path = tmp_path / "source"
    shutil.copytree(
        build.EXTENSION_SOURCE_DIR,
        str(path),
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (path / "session.json").write_text(
        '{"protocol": 2, "port": 1, "token": "x"}', encoding="utf-8"
    )
    (path / "port.json").write_text('{"port": 1}', encoding="utf-8")
    (path / "__pycache__").mkdir()
    (path / "__pycache__" / "stale.pyc").write_bytes(b"\x00")
    monkeypatch.setattr(build, "EXTENSION_SOURCE_DIR", str(path))
    return path


# --- tiny protobuf reader (length-delimited fields only) ---------------------


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, pos


def _fields(buf: bytes) -> dict[int, list[bytes]]:
    out: dict[int, list[bytes]] = {}
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        assert key & 7 == 2, "only length-delimited fields are expected"
        length, pos = _read_varint(buf, pos)
        out.setdefault(key >> 3, []).append(buf[pos : pos + length])
        pos += length
    assert pos == len(buf)
    return out


def _parse_crx3(data: bytes) -> tuple[bytes, bytes, bytes, bytes]:
    """Return (public_key_der, signature, signed_header_data, zip_bytes)."""
    assert data[:4] == b"Cr24"
    version, header_len = struct.unpack("<II", data[4:12])
    assert version == 3
    header = _fields(data[12 : 12 + header_len])
    assert set(header) == {2, 10000}
    (proof_bytes,) = header[2]
    (signed_header_data,) = header[10000]
    proof = _fields(proof_bytes)
    (public_der,) = proof[1]
    (signature,) = proof[2]
    return public_der, signature, signed_header_data, data[12 + header_len :]


def _verify_crx_signature(
    public_der: bytes, signature: bytes, signed_header_data: bytes, zip_bytes: bytes
) -> None:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    public_key = serialization.load_der_public_key(public_der)
    assert isinstance(public_key, rsa.RSAPublicKey)
    message = (
        b"CRX3 SignedData\x00"
        + struct.pack("<I", len(signed_header_data))
        + signed_header_data
        + zip_bytes
    )
    public_key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())


# --- identity ----------------------------------------------------------------


def test_extension_id_matches_chromium_known_pair():
    # Chromium components/crx_file/id_util_unittest.cc: GenerateId("test").
    assert extension_id(b"test") == "jpignaibiiemhngfjkcpokkamffknabf"


@pytest.mark.parametrize("value", ["test", bytearray(b"test"), None])
def test_extension_id_rejects_non_bytes(value: Any):
    with pytest.raises(TypeError):
        extension_id(value)


# --- state dir -----------------------------------------------------------------


def test_state_dir_env_override(home):
    assert state_dir() == home


def test_state_dir_platform_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("EMUNIUM_HOME", raising=False)
    monkeypatch.setattr(build.Path, "home", classmethod(lambda cls: tmp_path))
    appdata = str(tmp_path / "Roaming")
    xdg = str(tmp_path / "xdg")
    monkeypatch.setenv("APPDATA", appdata)
    monkeypatch.setenv("XDG_CONFIG_HOME", xdg)

    monkeypatch.setattr(sys, "platform", "win32")
    assert state_dir() == Path(appdata) / "emunium"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert state_dir() == tmp_path / "Library" / "Application Support" / "emunium"
    monkeypatch.setattr(sys, "platform", "linux")
    assert state_dir() == Path(xdg) / "emunium"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/ignored")
    assert state_dir() == tmp_path / ".config" / "emunium"


def test_missing_cryptography_names_the_extra(home, monkeypatch):
    for name in [
        n for n in sys.modules if n == "cryptography" or n.startswith("cryptography.")
    ]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "cryptography", None)
    with pytest.raises(ImportError, match=re.escape("pip install emunium[hub]")):
        load_or_create_state()
    assert not (home / "extension_key.pem").exists()


# --- state -----------------------------------------------------------------------


@needs_crypto
def test_state_is_created_once_and_reused(home):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    first = load_or_create_state(port=48123)
    key = serialization.load_pem_private_key(
        (home / "extension_key.pem").read_bytes(), password=None
    )
    assert isinstance(key, rsa.RSAPrivateKey) and key.key_size == 2048
    hub = json.loads((home / "hub.json").read_text(encoding="utf-8"))
    assert hub["port"] == 48123 and hub["installs"] == {}
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", hub["enrollment"])
    assert first["enrollment"] == hub["enrollment"]
    assert ID_RE.match(first["extension_id"])
    assert first["extension_id"] == extension_id(first["public_key_der"])

    second = load_or_create_state(port=49000)
    assert second["port"] == 48123, "an existing state keeps its port"
    assert second["enrollment"] == first["enrollment"]
    assert second["extension_id"] == first["extension_id"]


@needs_crypto
def test_enrollment_differs_per_state(tmp_path, monkeypatch):
    monkeypatch.setenv("EMUNIUM_HOME", str(tmp_path / "a"))
    a = load_or_create_state()
    monkeypatch.setenv("EMUNIUM_HOME", str(tmp_path / "b"))
    b = load_or_create_state()
    assert a["enrollment"] != b["enrollment"]
    assert a["extension_id"] != b["extension_id"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@needs_crypto
def test_state_files_are_private(home):
    load_or_create_state()
    assert (home / "extension_key.pem").stat().st_mode & 0o777 == 0o600
    assert (home / "hub.json").stat().st_mode & 0o777 == 0o600


@needs_crypto
def test_corrupt_hub_json_is_not_silently_replaced(home):
    load_or_create_state()
    (home / "hub.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ExtensionBuildError, match="not valid JSON"):
        load_or_create_state()
    (home / "hub.json").write_text(
        '{"port": 47800, "enrollment": "short"}', encoding="utf-8"
    )
    with pytest.raises(ExtensionBuildError, match="enrollment"):
        load_or_create_state()


@pytest.mark.parametrize("port", [0, 65536, -1, True, "47800"])
def test_invalid_port_is_rejected(home, port):
    with pytest.raises(ValueError):
        load_or_create_state(port=port)


# --- build -----------------------------------------------------------------------


@needs_crypto
def test_build_writes_all_three_artifacts(home, source, tmp_path):
    out = tmp_path / "dist"
    result = build_extension(str(out))
    state = load_or_create_state()

    unpacked = out / "emunium-extension"
    assert result["unpacked_dir"] == str(unpacked)
    assert result["zip_path"] == str(out / "emunium-extension.zip")
    assert result["crx_path"] == str(out / "emunium-extension.crx")
    assert result["extension_id"] == state["extension_id"]

    names = {p.relative_to(unpacked).as_posix() for p in unpacked.rglob("*")}
    assert not names & {
        "session.json",
        "port.json",
        "__pycache__",
        "__pycache__/stale.pyc",
    }
    expected = {p.name for p in Path(source).iterdir()} - {
        "session.json",
        "port.json",
        "__pycache__",
    }
    assert names == expected | {"attach.json"}

    # manifest.key round-trips to the same id.
    manifest = json.loads((unpacked / "manifest.json").read_text(encoding="utf-8"))
    key_der = base64.b64decode(manifest["key"])
    assert key_der == state["public_key_der"]
    assert extension_id(key_der) == result["extension_id"]
    source_manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    assert {k: v for k, v in manifest.items() if k != "key"} == source_manifest

    attach = json.loads((unpacked / "attach.json").read_text(encoding="utf-8"))
    assert attach == {
        "protocol": 2,
        "mode": "attach",
        "hubUrl": "ws://127.0.0.1:47800",
        "enrollment": state["enrollment"],
    }
    match = HUB_URL_RE.match(attach["hubUrl"])
    assert match is not None and match.group(1) == "47800"

    # zip: files at the archive root, identical to the folder.
    zip_bytes = Path(result["zip_path"]).read_bytes()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        assert set(archive.namelist()) == names
        for name in archive.namelist():
            assert archive.read(name) == (unpacked / name).read_bytes()

    # CRX3: header parses back, crx_id and signature verify, payload is the zip.
    public_der, signature, signed_header_data, payload = _parse_crx3(
        Path(result["crx_path"]).read_bytes()
    )
    assert public_der == state["public_key_der"]
    assert _fields(signed_header_data) == {
        1: [hashlib.sha256(public_der).digest()[:16]]
    }
    assert payload == zip_bytes
    _verify_crx_signature(public_der, signature, signed_header_data, payload)


@needs_crypto
def test_crx_signature_covers_the_archive(home, source, tmp_path):
    from cryptography.exceptions import InvalidSignature

    result = build_extension(str(tmp_path / "dist"))
    public_der, signature, signed_header_data, payload = _parse_crx3(
        Path(result["crx_path"]).read_bytes()
    )
    tampered = payload[:-1] + bytes([payload[-1] ^ 0x01])
    with pytest.raises(InvalidSignature):
        _verify_crx_signature(public_der, signature, signed_header_data, tampered)


@needs_crypto
def test_build_with_leaked_session_json_fails(home, source, tmp_path, monkeypatch):
    # Simulate a copy step that lets per-session files through.
    monkeypatch.setattr(build, "EXTENSION_COPY_IGNORE", ())
    out = tmp_path / "dist"
    with pytest.raises(ExtensionBuildError, match="session.json"):
        build_extension(str(out))
    assert not (out / "emunium-extension").exists()
    assert not (out / "emunium-extension.zip").exists()
    assert not (out / "emunium-extension.crx").exists()


@needs_crypto
def test_build_port_override_is_persisted(home, source, tmp_path):
    result = build_extension(str(tmp_path / "dist"), port=48001)
    assert result["port"] == 48001 and result["hub_url"] == "ws://127.0.0.1:48001"
    attach = json.loads(
        (Path(result["unpacked_dir"]) / "attach.json").read_text(encoding="utf-8")
    )
    assert attach["hubUrl"] == "ws://127.0.0.1:48001"
    assert json.loads((home / "hub.json").read_text(encoding="utf-8"))["port"] == 48001
    with pytest.raises(ValueError):
        build_extension(str(tmp_path / "dist"), port=70000)


@needs_crypto
def test_rebuild_is_stable_and_replaces_the_folder(home, source, tmp_path):
    out = tmp_path / "dist"
    first = build_extension(str(out))
    (Path(first["unpacked_dir"]) / "stray.txt").write_text("old", encoding="utf-8")
    first_zip = Path(first["zip_path"]).read_bytes()
    second = build_extension(str(out), state=load_or_create_state())
    assert second["extension_id"] == first["extension_id"]
    assert not (Path(second["unpacked_dir"]) / "stray.txt").exists()
    assert Path(second["zip_path"]).read_bytes() == first_zip


# --- CLI -------------------------------------------------------------------------


@needs_crypto
def test_cli_id_and_build(home, source, tmp_path, capsys):
    assert main(["extension", "id"]) == 0
    ext_id = capsys.readouterr().out.strip()
    assert ID_RE.match(ext_id)

    out = tmp_path / "cli-dist"
    assert main(["extension", "build", "--out", str(out), "--port", "48002"]) == 0
    printed = capsys.readouterr().out
    assert ext_id in printed and "ws://127.0.0.1:48002" in printed
    assert (out / "emunium-extension.crx").is_file()
    assert (out / "emunium-extension.zip").is_file()
    assert (out / "emunium-extension" / "attach.json").is_file()


def test_cli_rejects_bad_port(home, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["extension", "build", "--out", str(tmp_path), "--port", "0"])
    assert exc.value.code == 2
    assert "1..65535" in capsys.readouterr().err


def test_cli_reports_missing_cryptography(home, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "cryptography", None)
    for name in [n for n in sys.modules if n.startswith("cryptography.")]:
        monkeypatch.setitem(sys.modules, name, None)
    assert main(["extension", "id"]) == 1
    assert "pip install emunium[hub]" in capsys.readouterr().err
