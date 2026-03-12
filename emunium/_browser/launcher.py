from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from emunium.bridge import Bridge
from emunium.chrome_installer import ensure_chrome

logger = logging.getLogger("emunium.browser")

EXTENSION_DIR = str(Path(__file__).resolve().parent.parent / "extension")


class BrowserSession:
    __slots__ = (
        "bridge",
        "process",
        "headless",
        "user_data_dir",
        "tmp_data_dir",
        "chrome_path",
    )

    def __init__(self) -> None:
        self.bridge: Bridge | None = None
        self.process: subprocess.Popen | None = None
        self.headless: bool = False
        self.user_data_dir: str | None = None
        self.tmp_data_dir: str | None = None
        self.chrome_path: str = ""


def launch(session: BrowserSession, bridge_timeout: float = 60.0) -> None:
    session.chrome_path = ensure_chrome()
    session.bridge.start()
    port = session.bridge.actual_port
    logger.info("Bridge started on port %d", port)

    if session.user_data_dir:
        data_dir = str(Path(session.user_data_dir).resolve())
    else:
        session.tmp_data_dir = tempfile.mkdtemp(prefix="emun_profile_")
        data_dir = session.tmp_data_dir
        logger.info("Created temp profile: %s", data_dir)

    _write_master_preferences(session.chrome_path)
    _seed_profile(data_dir)
    _patch_extension_port(port)

    args = [
        session.chrome_path,
        f"--user-data-dir={data_dir}",
        "--disable-features=IsolateOrigins,site-per-process",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        f"--load-extension={EXTENSION_DIR}",
    ]
    if session.headless:
        args.append("--headless=new")

    session.process = subprocess.Popen(args)
    time.sleep(2)

    connected = session.bridge.wait_for_connection(timeout=bridge_timeout)
    if not connected:
        raise RuntimeError("Extension did not connect within timeout")
    logger.info("Extension connected to bridge")


def close(session: BrowserSession) -> None:
    if session.process:
        session.process.terminate()
        try:
            session.process.wait(timeout=5)
        except Exception:
            session.process.kill()
        session.process = None
    session.bridge.shutdown()
    _restore_extension()
    if session.tmp_data_dir:
        try:
            shutil.rmtree(session.tmp_data_dir, ignore_errors=True)
        except Exception:
            pass
        session.tmp_data_dir = None
    logger.info("Browser closed")


def _patch_extension_port(port: int) -> None:
    port_file = Path(EXTENSION_DIR) / "port.json"
    port_file.write_text(json.dumps({"port": port}), encoding="utf-8")


def _restore_extension() -> None:
    port_file = Path(EXTENSION_DIR) / "port.json"
    if port_file.exists():
        port_file.unlink(missing_ok=True)


def _write_master_preferences(chrome_path: str) -> None:
    master = Path(chrome_path).parent / "master_preferences"
    payload = {"extensions": {"ui": {"developer_mode": True}}}
    master.write_text(json.dumps(payload), encoding="utf-8")


def _seed_profile(data_dir: str) -> None:
    default_dir = Path(data_dir) / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    prefs_path = default_dir / "Preferences"
    if prefs_path.exists():
        try:
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
        except Exception:
            prefs = {}
        prefs.setdefault("extensions", {}).setdefault("ui", {})["developer_mode"] = True
        prefs.setdefault("profile", {})["exited_cleanly"] = True
        tracked = prefs.get("prefs", {}).get("tracked_preferences_reset", [])
        if "extensions.ui.developer_mode" in tracked:
            tracked.remove("extensions.ui.developer_mode")
            prefs["prefs"]["tracked_preferences_reset"] = tracked
    else:
        prefs = {
            "extensions": {
                "alerts": {"initialized": True},
                "ui": {"developer_mode": True},
            },
            "profile": {"exited_cleanly": True},
            "browser": {"has_seen_welcome_page": True},
            "prefs": {"tracked_preferences_reset": []},
        }
    prefs_path.write_text(json.dumps(prefs), encoding="utf-8")
    local_state_path = Path(data_dir) / "Local State"
    if not local_state_path.exists():
        local_state = {
            "browser": {"enabled_labs_experiments": []},
            "profile": {"info_cache": {}},
        }
        local_state_path.write_text(json.dumps(local_state), encoding="utf-8")
