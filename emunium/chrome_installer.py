"""Locate or install Chrome for Testing.

An install is complete only when ``chrome-<platform>/.emunium-complete`` exists.
The archive is downloaded to a unique temp file and extracted into a staging
directory next to the target; the finished tree is moved into place with
``os.replace``, so a crash never leaves a half-extracted install that looks
complete. Concurrent installers serialise on an exclusive lock file.
"""

from __future__ import annotations

import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

from emunium.errors import LaunchError

VERSION_URL = (
    "https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_STABLE"
)
ZIP_URL_TEMPLATE = (
    "https://storage.googleapis.com/chrome-for-testing-public/"
    "{version}/{plat}/chrome-{plat}.zip"
)

CHROME_PATH_ENV = "EMUNIUM_CHROME_PATH"
MARKER_NAME = ".emunium-complete"
LOCK_NAME = ".emunium-install.lock"

METADATA_TIMEOUT = 30.0  # version lookup, and every socket operation
DOWNLOAD_TIMEOUT = 300.0  # whole archive download
LOCK_STALE_SECONDS = 15 * 60
LOCK_POLL_SECONDS = 1.0
LOCK_HEARTBEAT_SECONDS = 10.0
DITTO_TIMEOUT = 600.0
CHUNK_SIZE = 1024 * 1024

# Exec bits and symlinks only mean something off Windows.
_POSIX_PERMISSIONS = os.name != "nt"

_VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}$")


def _get_platform_tag() -> str:
    if sys.platform == "win32":
        return "win64" if platform.machine().endswith("64") else "win32"
    elif sys.platform == "darwin":
        return "mac-arm64" if platform.machine() == "arm64" else "mac-x64"
    else:
        return "linux64"


def _install_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        return Path(base) / "CfT_Browser"
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CfT_Browser"
    else:
        return Path.home() / ".local" / "share" / "CfT_Browser"


def _chrome_executable(install_path: Path, plat: str) -> Path:
    if plat.startswith("win"):
        return install_path / f"chrome-{plat}" / "chrome.exe"
    elif plat.startswith("mac"):
        return (
            install_path
            / f"chrome-{plat}"
            / "Google Chrome for Testing.app"
            / "Contents"
            / "MacOS"
            / "Google Chrome for Testing"
        )
    else:
        return install_path / f"chrome-{plat}" / "chrome"


def _print_progress(downloaded: int, total_size: int) -> None:
    if total_size <= 0:
        return
    pct = min(100, downloaded * 100 // total_size)
    bar_len = 40
    filled = bar_len * pct // 100
    bar = "█" * filled + "░" * (bar_len - filled)
    mb_done = downloaded / (1024 * 1024)
    mb_total = total_size / (1024 * 1024)
    print(f"\r  [{bar}] {pct}%  {mb_done:.1f}/{mb_total:.1f} MB", end="", flush=True)
    if pct >= 100:
        print()


def installed_chrome() -> str | None:
    """The Chrome binary ``ensure_chrome`` would return without installing, or
    None. Reads only: no network, no lock, no marker write."""
    override = os.environ.get(CHROME_PATH_ENV)
    if override:
        # ensure_chrome rejects a missing override rather than falling back.
        return override if os.path.isfile(override) else None
    exe = _chrome_executable(_install_dir(), _get_platform_tag())
    return str(exe) if exe.is_file() else None


def ensure_chrome() -> str:
    """Return a Chrome binary, installing Chrome for Testing if needed."""
    override = os.environ.get(CHROME_PATH_ENV)
    if override:
        if not os.path.isfile(override):
            raise LaunchError(f"{CHROME_PATH_ENV} points to a missing file: {override}")
        return override

    plat = _get_platform_tag()
    dest = _install_dir()
    exe = _chrome_executable(dest, plat)
    root = dest / f"chrome-{plat}"

    if _accept_existing(root, exe):
        return str(exe)

    dest.mkdir(parents=True, exist_ok=True)
    lock_path = dest / LOCK_NAME
    while not _try_acquire_lock(lock_path):
        if _accept_existing(root, exe):
            return str(exe)
        time.sleep(LOCK_POLL_SECONDS)
    try:
        # Another installer may have finished while we waited for the lock.
        if not _accept_existing(root, exe):
            _install(dest, plat, lock_path)
    finally:
        _release_lock(lock_path)
    return str(exe)


def _accept_existing(root: Path, exe: Path) -> bool:
    if not exe.is_file():
        return False
    marker = root / MARKER_NAME
    if not marker.exists():
        # Install made before markers existed: trust it and mark it once.
        try:
            marker.write_text("legacy\n", encoding="utf-8")
        except OSError:
            pass
    return True


# -- lock ----------------------------------------------------------------------


def _try_acquire_lock(lock_path: Path) -> bool:
    if _create_lock(lock_path):
        return True
    if not _lock_is_stale(lock_path):
        return False
    # Stale takeover: of several waiters, only the one whose rename succeeds
    # may recreate the lock; the others see the rename fail and retry later.
    grave = lock_path.with_name(
        f"{lock_path.name}.stale-{os.getpid()}-{secrets.token_hex(4)}"
    )
    try:
        os.replace(str(lock_path), str(grave))
    except OSError:
        return False
    if not _lock_is_stale(grave):
        # Another waiter took over between our check and our rename and we
        # moved its fresh lock aside: hand it back unless a new one exists.
        _move_no_replace(grave, lock_path)
        return False
    try:
        grave.unlink()
    except OSError:
        pass
    return _create_lock(lock_path)


def _create_lock(lock_path: Path) -> bool:
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        os.write(fd, f"{os.getpid()} {time.time():f}\n".encode("ascii"))
    finally:
        os.close(fd)
    return True


def _move_no_replace(src: Path, dst: Path) -> None:
    try:
        if os.name == "nt":
            os.rename(str(src), str(dst))  # refuses to overwrite on Windows
        else:
            os.link(str(src), str(dst))  # refuses to overwrite on POSIX
            os.unlink(str(src))
    except OSError:
        try:
            src.unlink()
        except OSError:
            pass


def _lock_is_stale(lock_path: Path) -> bool:
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        return False  # vanished or unreadable: let the next attempt decide
    return age > LOCK_STALE_SECONDS


def _release_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except OSError:
        pass


def _lock_heartbeat(lock_path: Path) -> Callable[[], None]:
    last = [time.monotonic()]

    def beat() -> None:
        now = time.monotonic()
        if now - last[0] >= LOCK_HEARTBEAT_SECONDS:
            last[0] = now
            try:
                os.utime(str(lock_path))
            except OSError:
                pass

    return beat


# -- install -------------------------------------------------------------------


def _install(dest: Path, plat: str, lock_path: Path) -> None:
    version = _fetch_text(VERSION_URL)
    if not _VERSION_RE.match(version):
        raise LaunchError(f"Unexpected Chrome for Testing version string: {version!r}")
    zip_url = ZIP_URL_TEMPLATE.format(version=version, plat=plat)

    fd, zip_name = tempfile.mkstemp(
        prefix=".emunium-download-", suffix=".zip", dir=str(dest)
    )
    os.close(fd)
    zip_path = Path(zip_name)
    staging = Path(tempfile.mkdtemp(prefix=".emunium-staging-", dir=str(dest)))
    try:
        print(f"Downloading Chrome for Testing v{version} ({plat})...")
        _download(zip_url, zip_path, heartbeat=_lock_heartbeat(lock_path))
        print("Extracting...")
        _extract(zip_path, staging)

        staged_root = staging / f"chrome-{plat}"
        staged_exe = _chrome_executable(staging, plat)
        if not staged_exe.is_file():
            raise LaunchError(
                f"Chrome archive {zip_url} did not contain"
                f" {staged_exe.relative_to(staging)}"
            )
        if _POSIX_PERMISSIONS:
            mode = staged_exe.stat().st_mode
            os.chmod(str(staged_exe), mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        (staged_root / MARKER_NAME).write_text(version + "\n", encoding="utf-8")
        _move_into_place(staged_root, dest / f"chrome-{plat}")
    finally:
        try:
            zip_path.unlink()
        except OSError:
            pass
        shutil.rmtree(str(staging), ignore_errors=True)
    print(f"Chrome for Testing installed: {_chrome_executable(dest, plat)}")


def _fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=METADATA_TIMEOUT) as resp:
        return resp.read().decode("utf-8").strip()


def _download(
    url: str, target: Path, heartbeat: Callable[[], None] | None = None
) -> None:
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT
    done = 0
    with urllib.request.urlopen(url, timeout=METADATA_TIMEOUT) as resp:
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total = 0
        with open(str(target), "wb") as out:
            while True:
                chunk = resp.read(CHUNK_SIZE)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                _print_progress(done, total)
                if heartbeat is not None:
                    heartbeat()
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"Chrome download exceeded {DOWNLOAD_TIMEOUT:.0f} s"
                        f" ({done} bytes received)"
                    )
    if total and done != total:
        raise OSError(f"Chrome download truncated: {done} of {total} bytes")


def _use_ditto() -> bool:
    return sys.platform == "darwin" and shutil.which("ditto") is not None


def _extract(zip_path: Path, staging: Path) -> None:
    if _use_ditto():
        # ditto keeps the app bundle's symlinks, permissions and xattrs intact.
        subprocess.run(
            ["ditto", "-x", "-k", str(zip_path), str(staging)],
            check=True,
            stdin=subprocess.DEVNULL,
            timeout=DITTO_TIMEOUT,
        )
        return
    _extract_zip(zip_path, staging)


def _entry_mode(info: zipfile.ZipInfo) -> int:
    """Unix st_mode stored by the archiver, or 0 when absent."""
    return (info.external_attr >> 16) & 0xFFFF


def _extract_zip(zip_path: Path, staging: Path) -> None:
    staging_real = os.path.realpath(str(staging))
    dir_modes: list[tuple[str, int]] = []
    with zipfile.ZipFile(str(zip_path), "r") as zf:
        for info in zf.infolist():
            target = _safe_target(staging_real, info.filename)
            mode = _entry_mode(info)
            if info.is_dir() or stat.S_ISDIR(mode):
                os.makedirs(target, exist_ok=True)
                if _POSIX_PERMISSIONS and stat.S_IMODE(mode):
                    dir_modes.append((target, stat.S_IMODE(mode)))
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if _POSIX_PERMISSIONS and stat.S_ISLNK(mode):
                link_target = zf.read(info).decode("utf-8")
                if os.path.lexists(target):
                    os.remove(target)
                os.symlink(link_target, target)
                continue
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, CHUNK_SIZE)
            if _POSIX_PERMISSIONS and stat.S_IMODE(mode):
                os.chmod(target, stat.S_IMODE(mode))
    # Directories last: a read-only directory must not block its own children.
    for path, mode in reversed(dir_modes):
        os.chmod(path, mode)


def _safe_target(staging_real: str, name: str) -> str:
    target = os.path.realpath(os.path.join(staging_real, name))
    # realpath also follows symlinks extracted earlier, so a link cannot redirect writes.
    if target != staging_real and not target.startswith(staging_real + os.sep):
        raise LaunchError(f"Unsafe path in Chrome archive: {name!r}")
    return target


def _move_into_place(staged_root: Path, final_root: Path) -> None:
    old: Path | None = None
    if os.path.lexists(str(final_root)):
        # A half-extracted or outdated tree; os.replace cannot overwrite a directory.
        old = (
            final_root.parent / f".emunium-old-{os.getpid()}-{int(time.time() * 1000)}"
        )
        os.replace(str(final_root), str(old))
    os.replace(str(staged_root), str(final_root))
    if old is not None:
        shutil.rmtree(str(old), ignore_errors=True)
