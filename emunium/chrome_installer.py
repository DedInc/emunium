from __future__ import annotations

import os
import platform
import stat
import sys
import urllib.request
import zipfile
from pathlib import Path

VERSION_URL = (
    "https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_STABLE"
)


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


def _progress_bar(block_num: int, block_size: int, total_size: int) -> None:
    downloaded = block_num * block_size
    if total_size > 0:
        pct = min(100, downloaded * 100 // total_size)
        bar_len = 40
        filled = bar_len * pct // 100
        bar = "\u2588" * filled + "\u2591" * (bar_len - filled)
        mb_done = downloaded / (1024 * 1024)
        mb_total = total_size / (1024 * 1024)
        print(
            f"\r  [{bar}] {pct}%  {mb_done:.1f}/{mb_total:.1f} MB", end="", flush=True
        )
        if pct >= 100:
            print()


def ensure_chrome() -> str:
    plat = _get_platform_tag()
    dest = _install_dir()
    exe = _chrome_executable(dest, plat)

    if exe.exists():
        return str(exe)

    with urllib.request.urlopen(VERSION_URL) as resp:
        version = resp.read().decode().strip()

    zip_url = f"https://storage.googleapis.com/chrome-for-testing-public/{version}/{plat}/chrome-{plat}.zip"

    print(f"Downloading Chrome for Testing v{version} ({plat})...")
    dest.mkdir(parents=True, exist_ok=True)
    tmp_zip = dest / "chrome_download.zip"

    urllib.request.urlretrieve(zip_url, str(tmp_zip), reporthook=_progress_bar)

    print("Extracting...")
    with zipfile.ZipFile(tmp_zip, "r") as zf:
        zf.extractall(str(dest))
    tmp_zip.unlink()

    if sys.platform != "win32" and exe.exists():
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    print(f"Chrome for Testing installed: {exe}")
    return str(exe)
