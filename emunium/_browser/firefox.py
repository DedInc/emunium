"""Firefox profile preferences and Enterprise Policy extension registration."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from emunium._browser.extension_targets import FIREFOX_GECKO_ID
from emunium.errors import LaunchError

__all__ = [
    "FIREFOX_XPI_ENV",
    "firefox_install_url",
    "firefox_policy_path",
    "write_firefox_policy",
    "write_user_prefs",
]

FIREFOX_XPI_ENV = "EMUNIUM_FIREFOX_XPI"
FIREFOX_RELEASE_XPI = (
    "https://github.com/DedInc/emunium/releases/download/"
    "{version}/emunium-extension-firefox.xpi"
)

# Browser startup preferences written to user.js and Enterprise Policy defaults.
USER_PREFS: dict[str, bool | str] = {
    # Set Firefox's WebDriver visibility preference to false by default.
    "dom.webdriver.enabled": False,
    # No default-browser check.
    "browser.shell.checkDefaultBrowser": False,
    # No about:welcome / first-run / what's-new pages.
    "browser.aboutwelcome.enabled": False,
    "trailhead.firstrun.didSeeAboutWelcome": True,
    "startup.homepage_welcome_url": "",
    "startup.homepage_welcome_url.additional": "",
    "browser.startup.homepage_override.mstone": "ignore",
    "browser.startup.firstrunSkipsHomepage": True,
    # No telemetry / data-reporting upload, and no data-policy notice tab.
    "datareporting.policy.firstRunURL": "",
    "datareporting.policy.dataSubmissionEnabled": False,
    "datareporting.healthreport.uploadEnabled": False,
    "toolkit.telemetry.reportingpolicy.firstRun": False,
    # No session restore prompt after an unclean exit.
    "browser.sessionstore.resume_from_crash": False,
}


def write_user_prefs(profile_dir: str | Path) -> Path:
    """Write ``user.js`` into ``profile_dir``; returns its path."""
    directory = Path(profile_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "user.js"
    lines = [
        f"user_pref({json.dumps(name)}, {json.dumps(value)});"
        for name, value in USER_PREFS.items()
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


_POLICY_LOCK = threading.Lock()


def firefox_policy_path(executable: str | Path) -> Path:
    """Return the Enterprise Policies path for a Firefox executable."""
    browser_path = Path(executable).expanduser().resolve()
    if not browser_path.is_file():
        raise LaunchError(f"Firefox executable does not exist: {browser_path}")

    app_root = next(
        (
            parent
            for parent in browser_path.parents
            if parent.name.casefold().endswith(".app")
        ),
        None,
    )
    install_root = (
        app_root / "Contents" / "Resources"
        if app_root is not None
        else browser_path.parent
    )
    return install_root / "distribution" / "policies.json"


def firefox_install_url(source: str | Path | None = None) -> str:
    """Resolve a local signed XPI override or this library version's release XPI."""
    source = source if source is not None else os.environ.get(FIREFOX_XPI_ENV) or None
    if source is None:
        try:
            version = metadata.version("emunium")
        except metadata.PackageNotFoundError as exc:
            raise LaunchError(
                f"Could not determine the installed emunium version for the Firefox XPI URL; "
                f"set {FIREFOX_XPI_ENV} to a local signed XPI"
            ) from exc
        return FIREFOX_RELEASE_XPI.format(version=quote(version, safe="."))

    if isinstance(source, str):
        parsed = urlsplit(source)
        if parsed.scheme.casefold() == "https":
            if (
                parsed.hostname
                and parsed.username is None
                and parsed.password is None
                and parsed.path.casefold().endswith(".xpi")
                and not parsed.fragment
            ):
                return source
            raise LaunchError(
                "Firefox XPI URL must be a valid HTTPS URL ending in .xpi"
            )
        if len(parsed.scheme) > 1:
            raise LaunchError("Firefox XPI source must be a local path or an HTTPS URL")

    try:
        xpi = Path(source).expanduser().resolve()
    except TypeError as exc:
        raise LaunchError(
            "Firefox XPI source must be a local path or an HTTPS URL"
        ) from exc
    if not xpi.is_file():
        raise LaunchError(f"Firefox extension XPI does not exist: {xpi}")
    return xpi.as_uri()


def write_firefox_policy(
    executable: str | Path, xpi_path: str | Path | None = None
) -> Path:
    """Merge a force-installed signed XPI into Firefox's ``policies.json``.

    Existing policy data and fields on this extension's settings are preserved.
    First-run Terms are skipped unless explicitly configured otherwise.
    The file is replaced atomically so an interrupted write cannot truncate it.
    Without an explicit source, use ``EMUNIUM_FIREFOX_XPI`` or the signed release
    artifact for the installed ``emunium`` version.
    """
    browser_path = Path(executable).expanduser().resolve()
    if not browser_path.is_file():
        raise LaunchError(f"Firefox executable does not exist: {browser_path}")
    install_url = firefox_install_url(xpi_path)

    policy_path = firefox_policy_path(browser_path)
    with _POLICY_LOCK:
        document: dict[str, Any] = {}
        previous_mode: int | None = None
        if policy_path.exists():
            try:
                raw = policy_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise LaunchError(
                    f"Could not read Firefox policy file {policy_path}: {exc}"
                ) from exc
            try:
                loaded = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise LaunchError(
                    f"Firefox policy file {policy_path} is not valid JSON: {exc}"
                ) from exc
            if not isinstance(loaded, dict):
                raise LaunchError(
                    f"Firefox policy file {policy_path} must contain a JSON object"
                )
            document = loaded
            try:
                previous_mode = stat.S_IMODE(policy_path.stat().st_mode)
            except OSError as exc:
                raise LaunchError(
                    f"Could not inspect Firefox policy file {policy_path}: {exc}"
                ) from exc

        policies = document.setdefault("policies", {})
        if not isinstance(policies, dict):
            raise LaunchError(
                f"Firefox policy file {policy_path} has a non-object 'policies' value"
            )
        # Firefox 138+: about:welcome preferences do not suppress the Terms modal.
        policies.setdefault("SkipTermsOfUse", True)
        preferences = policies.setdefault("Preferences", {})
        if not isinstance(preferences, dict):
            raise LaunchError(
                f"Firefox policy file {policy_path} has a non-object 'Preferences' value"
            )
        for name, value in USER_PREFS.items():
            preferences.setdefault(name, {"Value": value, "Status": "default"})

        settings = policies.setdefault("ExtensionSettings", {})
        if not isinstance(settings, dict):
            raise LaunchError(
                f"Firefox policy file {policy_path} has a non-object 'ExtensionSettings' value"
            )
        extension = settings.setdefault(FIREFOX_GECKO_ID, {})
        if not isinstance(extension, dict):
            raise LaunchError(
                f"Firefox policy file {policy_path} has invalid settings for {FIREFOX_GECKO_ID}"
            )
        extension["installation_mode"] = "force_installed"
        extension["install_url"] = install_url

        try:
            policy_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LaunchError(
                f"Could not create Firefox policy directory for {policy_path}: {exc}"
            ) from exc
        _write_policy_atomically(policy_path, document, previous_mode)
    return policy_path


def _write_policy_atomically(
    policy_path: Path, document: dict[str, Any], previous_mode: int | None
) -> None:
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{policy_path.name}.", suffix=".tmp", dir=str(policy_path.parent)
        )
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = None
            json.dump(document, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if previous_mode is not None and os.name != "nt":
            os.chmod(str(temporary), previous_mode)
        os.replace(str(temporary), str(policy_path))
        temporary = None
    except OSError as exc:
        raise LaunchError(
            f"Could not write Firefox policy file {policy_path}: {exc}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
