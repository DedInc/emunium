"""Persistent hub state and stable-id extension builds (folder, zip, CRX3).

Contract: ``docs/architecture/attach-mode.md`` section 1 (artifacts and identity)
and section 3 (``attach.json``, enrollment secret, no ``session.json`` in a build).

The RSA-2048 key in the state directory fixes the extension id for every vendor
and profile; the private key is only ever read from that directory to sign the
CRX3 and never copied into a build. RSA needs the optional ``cryptography``
package (``pip install emunium[hub]``).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import struct
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Union

from emunium._browser.extension_targets import FIREFOX_GECKO_ID, TARGETS, materialize
from emunium._browser.launcher import (
    EXTENSION_COPY_IGNORE,
    EXTENSION_SOURCE_DIR,
    PROTOCOL_VERSION,
)
from emunium.errors import EmuniumError

DEFAULT_PORT = 47800
STATE_DIR_ENV = "EMUNIUM_HOME"
STATE_DIR_NAME = "emunium"
KEY_FILE = "extension_key.pem"
HUB_FILE = "hub.json"
ATTACH_FILE = "attach.json"
MANIFEST_FILE = "manifest.json"
ARTIFACT_NAME = "emunium-extension"
FIREFOX_ARTIFACT_NAME = "emunium-extension-firefox"
LOOPBACK = "127.0.0.1"

RSA_KEY_BITS = 2048
RSA_PUBLIC_EXPONENT = 65537
ENROLLMENT_BYTES = 32
# secrets.token_urlsafe(32) yields 43 base64url characters.
ENROLLMENT_RE = re.compile(r"[A-Za-z0-9_-]{43,}")

CRX_MAGIC = b"Cr24"
CRX_VERSION = 3
CRX_SIGNATURE_CONTEXT = b"CRX3 SignedData\x00"
CRX_ID_BYTES = 16
# crx3.proto field numbers.
CRX_HEADER_SHA256_WITH_RSA = 2
CRX_HEADER_SIGNED_HEADER_DATA = 10000
CRX_PROOF_PUBLIC_KEY = 1
CRX_PROOF_SIGNATURE = 2
CRX_SIGNED_DATA_CRX_ID = 1
PROTOBUF_WIRE_LENGTH_DELIMITED = 2

# Fixed entry timestamp so the same tree always yields the same zip bytes.
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
ZIP_FILE_MODE = 0o644

# Checked independently of EXTENSION_COPY_IGNORE: a build must never carry these.
FORBIDDEN_BUILD_NAMES = frozenset(("session.json", "port.json", "__pycache__"))

_INSTALL_HINT = "pip install emunium[hub]"
_ID_ALPHABET = str.maketrans("0123456789abcdef", "abcdefghijklmnop")
_IS_POSIX = os.name != "nt"

PathLike = Union[str, "os.PathLike[str]"]


class ExtensionBuildError(EmuniumError, RuntimeError):
    """Hub state is unusable or an extension build could not be produced safely."""


def state_dir() -> Path:
    """Hub state directory: ``$EMUNIUM_HOME`` or the platform user config dir."""
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    home = Path.home()
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = (
            Path(appdata)
            if appdata and os.path.isabs(appdata)
            else home / "AppData" / "Roaming"
        )
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        # XDG: a relative XDG_CONFIG_HOME is invalid and must be ignored.
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg and os.path.isabs(xdg) else home / ".config"
    return base / STATE_DIR_NAME


def extension_id(public_key_der: bytes) -> str:
    """Chrome extension id: first 32 hex chars of SHA-256(DER SPKI), mapped 0-f -> a-p."""
    if not isinstance(public_key_der, bytes):
        raise TypeError(
            f"public_key_der must be bytes, got {type(public_key_der).__name__}"
        )
    return hashlib.sha256(public_key_der).hexdigest()[:32].translate(_ID_ALPHABET)


def load_or_create_state(port: int = DEFAULT_PORT) -> dict[str, Any]:
    """Load hub state, creating the key and ``hub.json`` on first use.

    ``port`` is only used when ``hub.json`` does not exist yet; an existing
    state keeps its stored port and enrollment secret.
    """
    port = _validate_port(port)
    crypto = _crypto()
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key_path = directory / KEY_FILE
    hub_path = directory / HUB_FILE

    if not key_path.exists():
        private_key = crypto.rsa.generate_private_key(
            public_exponent=RSA_PUBLIC_EXPONENT, key_size=RSA_KEY_BITS
        )
        pem = private_key.private_bytes(
            encoding=crypto.serialization.Encoding.PEM,
            format=crypto.serialization.PrivateFormat.PKCS8,
            encryption_algorithm=crypto.serialization.NoEncryption(),
        )
        _create_exclusive(key_path, pem)
    # Always re-read: a concurrent first run may have won the exclusive create.
    public_der = _public_der(_load_private_key(key_path))

    hub = _read_hub(hub_path)
    if hub is None:
        fresh = {
            "port": port,
            "enrollment": secrets.token_urlsafe(ENROLLMENT_BYTES),
            "installs": {},
        }
        _create_exclusive(hub_path, _dump_json(fresh))
        hub = _read_hub(hub_path)
        if hub is None:
            raise ExtensionBuildError(f"{hub_path} vanished right after it was created")

    return {
        "state_dir": str(directory),
        "key_path": str(key_path),
        "hub_path": str(hub_path),
        "port": hub["port"],
        "enrollment": hub["enrollment"],
        "public_key_der": public_der,
        "extension_id": extension_id(public_der),
    }


def build_extension(
    out_dir: PathLike,
    *,
    port: int | None = None,
    state: dict[str, Any] | None = None,
    target: str = "chromium",
) -> dict[str, Any]:
    """Write the unpacked folder, root-level zip and signed CRX3 into ``out_dir``.

    ``target="firefox"`` writes ``emunium-extension-firefox/`` and a root-level
    ``.xpi`` instead: no manifest ``key`` and no CRX (Firefox ids come from the
    manifest's ``browser_specific_settings.gecko.id``).

    An explicit ``port`` that differs from the stored one is persisted to
    ``hub.json`` so the hub and the built ``attach.json`` agree on the port.
    """
    if target not in TARGETS:
        raise ValueError(
            "unknown extension target {!r}; expected one of {}".format(
                target, ", ".join(TARGETS)
            )
        )
    if state is None:
        state = load_or_create_state() if port is None else load_or_create_state(port)
    if port is not None and _validate_port(port) != state["port"]:
        state = _store_port(state, port)
    hub_port = _validate_port(state["port"])
    enrollment = state["enrollment"]
    if not isinstance(enrollment, str) or not ENROLLMENT_RE.fullmatch(enrollment):
        raise ExtensionBuildError("state carries no valid enrollment secret")

    attach = {
        "protocol": PROTOCOL_VERSION,
        "mode": "attach",
        "hubUrl": f"ws://{LOOPBACK}:{hub_port:d}",
        "enrollment": enrollment,
    }
    out = Path(out_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    if target == "firefox":
        unpacked = out / FIREFOX_ARTIFACT_NAME
        xpi_path = out / (FIREFOX_ARTIFACT_NAME + ".xpi")
        _stage(target, unpacked, attach, public_der=None)
        _write_atomic(xpi_path, _zip_tree(unpacked))
        return {
            "extension_id": FIREFOX_GECKO_ID,
            "unpacked_dir": str(unpacked),
            "xpi_path": str(xpi_path),
            "port": hub_port,
            "hub_url": attach["hubUrl"],
            "state_dir": state["state_dir"],
        }

    private_key = _load_private_key(Path(state["key_path"]))
    public_der = _public_der(private_key)
    ext_id = extension_id(public_der)

    unpacked = out / ARTIFACT_NAME
    zip_path = out / (ARTIFACT_NAME + ".zip")
    crx_path = out / (ARTIFACT_NAME + ".crx")

    _stage(target, unpacked, attach, public_der=public_der)

    zip_bytes = _zip_tree(unpacked)
    _write_atomic(zip_path, zip_bytes)
    _write_atomic(crx_path, _crx3(private_key, public_der, zip_bytes))

    return {
        "extension_id": ext_id,
        "unpacked_dir": str(unpacked),
        "zip_path": str(zip_path),
        "crx_path": str(crx_path),
        "port": hub_port,
        "hub_url": attach["hubUrl"],
        "state_dir": state["state_dir"],
    }


# --- cryptography -----------------------------------------------------------


class _Crypto:
    __slots__ = ("hashes", "padding", "rsa", "serialization")

    def __init__(self) -> None:
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding, rsa
        except ImportError as exc:
            raise ImportError(
                f"emunium extension builds need the optional 'cryptography' package: {_INSTALL_HINT}"
            ) from exc
        self.hashes = hashes
        self.padding = padding
        self.rsa = rsa
        self.serialization = serialization


def _crypto() -> _Crypto:
    return _Crypto()


def _load_private_key(key_path: Path) -> Any:
    crypto = _crypto()
    try:
        key = crypto.serialization.load_pem_private_key(
            key_path.read_bytes(), password=None
        )
    except (ValueError, TypeError) as exc:
        raise ExtensionBuildError(
            f"{key_path} is not an unencrypted PEM private key: {exc}"
        ) from exc
    if not isinstance(key, crypto.rsa.RSAPrivateKey) or key.key_size != RSA_KEY_BITS:
        raise ExtensionBuildError(
            f"{key_path} must hold an RSA-{RSA_KEY_BITS} private key"
        )
    _restrict(key_path)
    return key


def _public_der(private_key: Any) -> bytes:
    crypto = _crypto()
    return private_key.public_key().public_bytes(
        encoding=crypto.serialization.Encoding.DER,
        format=crypto.serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _crx3(private_key: Any, public_der: bytes, zip_bytes: bytes) -> bytes:
    crypto = _crypto()
    crx_id = hashlib.sha256(public_der).digest()[:CRX_ID_BYTES]
    signed_header_data = _pb_field(CRX_SIGNED_DATA_CRX_ID, crx_id)
    signed_message = (
        CRX_SIGNATURE_CONTEXT
        + struct.pack("<I", len(signed_header_data))
        + signed_header_data
        + zip_bytes
    )
    signature = private_key.sign(
        signed_message, crypto.padding.PKCS1v15(), crypto.hashes.SHA256()
    )
    proof = _pb_field(CRX_PROOF_PUBLIC_KEY, public_der) + _pb_field(
        CRX_PROOF_SIGNATURE, signature
    )
    header = _pb_field(CRX_HEADER_SHA256_WITH_RSA, proof) + _pb_field(
        CRX_HEADER_SIGNED_HEADER_DATA, signed_header_data
    )
    return CRX_MAGIC + struct.pack("<II", CRX_VERSION, len(header)) + header + zip_bytes


# --- protobuf (length-delimited fields only) --------------------------------


def _varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint must be non-negative")
    out = bytearray()
    while True:
        low = value & 0x7F
        value >>= 7
        if value:
            out.append(low | 0x80)
        else:
            out.append(low)
            return bytes(out)


def _pb_field(field_number: int, payload: bytes) -> bytes:
    return (
        _varint((field_number << 3) | PROTOBUF_WIRE_LENGTH_DELIMITED)
        + _varint(len(payload))
        + payload
    )


# --- build tree --------------------------------------------------------------


def _stage(
    target: str, unpacked: Path, attach: dict[str, Any], public_der: bytes | None
) -> None:
    """Materialize ``target`` into ``unpacked`` with ``attach.json`` (and the key, if given)."""
    _remove_path(unpacked)
    try:
        materialize(
            target,
            unpacked,
            source_dir=EXTENSION_SOURCE_DIR,
            ignore=EXTENSION_COPY_IGNORE,
        )
        _verify_clean(unpacked)
        if public_der is not None:
            _write_manifest_key(unpacked / MANIFEST_FILE, public_der)
        (unpacked / ATTACH_FILE).write_bytes(_dump_json(attach))
        _verify_clean(unpacked)
    except BaseException:
        # Never leave a half-built (or session-carrying) folder that could be installed.
        shutil.rmtree(str(unpacked), ignore_errors=True)
        raise


def _verify_clean(root: Path) -> None:
    leaked = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.name in FORBIDDEN_BUILD_NAMES
    )
    if leaked:
        raise ExtensionBuildError(
            "extension build must not contain {}".format(", ".join(leaked))
        )


def _write_manifest_key(manifest_path: Path, public_der: bytes) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ExtensionBuildError(f"{manifest_path} is not a JSON object")
    manifest["key"] = base64.b64encode(public_der).decode("ascii")
    manifest_path.write_bytes(_dump_json(manifest))


def _zip_tree(root: Path) -> bytes:
    files: list[Path] = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    with tempfile.SpooledTemporaryFile() as buffer:
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                info = zipfile.ZipInfo(
                    path.relative_to(root).as_posix(), date_time=ZIP_TIMESTAMP
                )
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = ZIP_FILE_MODE << 16
                archive.writestr(info, path.read_bytes())
        buffer.seek(0)
        return buffer.read()


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(str(path))


# --- state files -------------------------------------------------------------


def _validate_port(port: Any) -> int:
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port <= 65535:
        raise ValueError(f"hub port must be an integer in 1..65535, got {port!r}")
    return port


def _read_hub(hub_path: Path) -> dict[str, Any] | None:
    try:
        raw = hub_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        hub = json.loads(raw)
    except ValueError as exc:
        raise ExtensionBuildError(
            f"{hub_path} is not valid JSON ({exc}); repair it, or delete it to re-enroll every install"
        ) from exc
    if not isinstance(hub, dict):
        raise ExtensionBuildError(f"{hub_path} must hold a JSON object")
    try:
        _validate_port(hub.get("port"))
    except ValueError as exc:
        raise ExtensionBuildError(f"{hub_path}: {exc}") from exc
    enrollment = hub.get("enrollment")
    if not isinstance(enrollment, str) or not ENROLLMENT_RE.fullmatch(enrollment):
        raise ExtensionBuildError(f"{hub_path} holds no valid enrollment secret")
    _restrict(hub_path)
    return hub


def _store_port(state: dict[str, Any], port: int) -> dict[str, Any]:
    hub_path = Path(state["hub_path"])
    hub = _read_hub(hub_path)
    if hub is None:
        raise ExtensionBuildError(f"{hub_path} is missing")
    hub["port"] = port
    _write_atomic(hub_path, _dump_json(hub), private=True)
    updated = dict(state)
    updated["port"] = port
    return updated


def _dump_json(data: dict[str, Any]) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _restrict(path: Path) -> None:
    if _IS_POSIX:
        os.chmod(str(path), 0o600)


def _write_temp(path: Path, data: bytes, private: bool) -> str:
    fd, tmp = tempfile.mkstemp(
        prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if _IS_POSIX:
            os.chmod(tmp, 0o600 if private else ZIP_FILE_MODE)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return tmp


def _write_atomic(path: Path, data: bytes, private: bool = False) -> None:
    tmp = _write_temp(path, data, private)
    try:
        os.replace(tmp, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _create_exclusive(path: Path, data: bytes) -> None:
    """Publish a fully written private file only if ``path`` does not exist yet."""
    tmp = _write_temp(path, data, private=True)
    try:
        os.link(tmp, str(path))
    except FileExistsError:
        pass  # A concurrent first run published its file first; the caller re-reads it.
    except OSError:
        # Filesystems without hard links: fall back to a replace guarded by a recheck.
        if not path.exists():
            os.replace(tmp, str(path))
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
