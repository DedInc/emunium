"""Firefox policy registration and profile preferences; no browser or network."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from emunium._browser import firefox
from emunium._browser.firefox import (
    FIREFOX_XPI_ENV,
    firefox_install_url,
    firefox_policy_path,
    write_firefox_policy,
    write_user_prefs,
)
from emunium.errors import LaunchError

EXTENSION_ID = "bridge@emunium.dev"


# -- write_user_prefs -------------------------------------------------------

PREF_LINE = re.compile(r'^user_pref\("([^"]+)", (.+)\);$')


def _read_prefs(path: Path) -> dict[str, Any]:
    prefs: dict[str, Any] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = PREF_LINE.match(line)
        assert match, f"malformed user.js line: {line!r}"
        prefs[match.group(1)] = json.loads(match.group(2))
    return prefs


def test_write_user_prefs_defaults(tmp_path):
    path = write_user_prefs(tmp_path)
    assert path == tmp_path / "user.js"
    prefs = _read_prefs(path)
    assert prefs["browser.shell.checkDefaultBrowser"] is False
    assert prefs["browser.aboutwelcome.enabled"] is False
    assert prefs["startup.homepage_welcome_url"] == ""
    assert prefs["browser.startup.homepage_override.mstone"] == "ignore"
    assert prefs["datareporting.policy.dataSubmissionEnabled"] is False
    assert prefs["datareporting.healthreport.uploadEnabled"] is False
    assert prefs["browser.sessionstore.resume_from_crash"] is False
    assert prefs["dom.webdriver.enabled"] is False
    # Nothing about signing, security or network transport is changed.
    forbidden = ("xpinstall", "security.", "extensions.", "network.", "remote.")
    assert not [name for name in prefs if name.startswith(forbidden)]
    assert prefs == firefox.USER_PREFS


def test_write_user_prefs_overwrites(tmp_path):
    (tmp_path / "user.js").write_text('user_pref("stale", 1);\n', encoding="utf-8")
    prefs = _read_prefs(write_user_prefs(tmp_path))
    assert "stale" not in prefs


# -- Enterprise Policies -----------------------------------------------------


def test_firefox_policy_path_next_to_executable(tmp_path):
    executable = tmp_path / "Mozilla Firefox" / "firefox.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"firefox")

    assert firefox_policy_path(executable) == (
        executable.parent / "distribution" / "policies.json"
    )


def test_firefox_policy_path_inside_macos_app_resources(tmp_path):
    executable = tmp_path / "Firefox.app" / "Contents" / "MacOS" / "firefox"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"firefox")

    assert firefox_policy_path(executable) == (
        tmp_path
        / "Firefox.app"
        / "Contents"
        / "Resources"
        / "distribution"
        / "policies.json"
    )


def test_write_firefox_policy_creates_distribution_and_uses_file_uri(tmp_path):
    executable = tmp_path / "firefox" / "firefox.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"firefox")
    xpi = tmp_path / "build with spaces" / "emunium-extension-firefox.xpi"
    xpi.parent.mkdir()
    xpi.write_bytes(b"xpi")

    policy_path = write_firefox_policy(executable, xpi)

    assert policy_path == executable.parent / "distribution" / "policies.json"
    document = json.loads(policy_path.read_text(encoding="utf-8"))
    extension = document["policies"]["ExtensionSettings"][EXTENSION_ID]
    assert extension == {
        "installation_mode": "force_installed",
        "install_url": xpi.resolve().as_uri(),
    }
    assert extension["install_url"].startswith("file:///")


def test_write_firefox_policy_merges_existing_policy_data(tmp_path):
    executable = tmp_path / "firefox.exe"
    executable.write_bytes(b"firefox")
    xpi = tmp_path / "emunium.xpi"
    xpi.write_bytes(b"xpi")
    policy_path = firefox_policy_path(executable)
    policy_path.parent.mkdir()
    original = {
        "custom": {"keep": True},
        "policies": {
            "BlockAboutConfig": True,
            "ExtensionSettings": {
                "*": {"installation_mode": "blocked"},
                "another@example.com": {"installation_mode": "allowed"},
                EXTENSION_ID: {
                    "installation_mode": "allowed",
                    "updates_disabled": True,
                },
            },
        },
    }
    policy_path.write_text(json.dumps(original), encoding="utf-8")

    assert write_firefox_policy(executable, xpi) == policy_path

    updated = json.loads(policy_path.read_text(encoding="utf-8"))
    assert updated["custom"] == original["custom"]
    assert updated["policies"]["BlockAboutConfig"] is True
    settings = updated["policies"]["ExtensionSettings"]
    assert settings["*"] == original["policies"]["ExtensionSettings"]["*"]
    assert settings["another@example.com"] == original["policies"]["ExtensionSettings"][
        "another@example.com"
    ]
    assert settings[EXTENSION_ID] == {
        "installation_mode": "force_installed",
        "install_url": xpi.resolve().as_uri(),
        "updates_disabled": True,
    }


@pytest.mark.parametrize(
    "contents",
    [
        "{invalid json",
        json.dumps({"policies": []}),
        json.dumps({"policies": {"ExtensionSettings": []}}),
        json.dumps({"policies": {"ExtensionSettings": {EXTENSION_ID: "blocked"}}}),
    ],
)
def test_write_firefox_policy_rejects_invalid_existing_file_without_overwriting(
    tmp_path, contents
):
    executable = tmp_path / "firefox.exe"
    executable.write_bytes(b"firefox")
    xpi = tmp_path / "emunium.xpi"
    xpi.write_bytes(b"xpi")
    policy_path = firefox_policy_path(executable)
    policy_path.parent.mkdir()
    policy_path.write_text(contents, encoding="utf-8")

    with pytest.raises(LaunchError, match="policies.json"):
        write_firefox_policy(executable, xpi)

    assert policy_path.read_text(encoding="utf-8") == contents


def test_firefox_install_url_uses_installed_package_version(monkeypatch):
    monkeypatch.delenv(FIREFOX_XPI_ENV, raising=False)
    monkeypatch.setattr(firefox.metadata, "version", lambda package: "3.1.0")

    assert firefox_install_url() == (
        "https://github.com/DedInc/emunium/releases/download/3.1.0/"
        "emunium-extension-firefox.xpi"
    )


def test_firefox_install_url_uses_local_environment_override(tmp_path, monkeypatch):
    xpi = tmp_path / "signed build.xpi"
    xpi.write_bytes(b"signed XPI")
    monkeypatch.setenv(FIREFOX_XPI_ENV, str(xpi))

    assert firefox_install_url() == xpi.resolve().as_uri()


def test_firefox_install_url_accepts_explicit_https_source():
    source = "https://example.invalid/releases/firefox.xpi"
    assert firefox_install_url(source) == source


@pytest.mark.parametrize(
    "source",
    [
        "http://example.invalid/firefox.xpi",
        "https://example.invalid/firefox.zip",
        "https://user:pass@example.invalid/firefox.xpi",
    ],
)
def test_firefox_install_url_rejects_unsafe_or_invalid_source(source):
    with pytest.raises(LaunchError, match="Firefox XPI"):
        firefox_install_url(source)


def test_write_firefox_policy_defaults_to_versioned_release_url(tmp_path, monkeypatch):
    executable = tmp_path / "firefox.exe"
    executable.write_bytes(b"firefox")
    monkeypatch.delenv(FIREFOX_XPI_ENV, raising=False)
    monkeypatch.setattr(firefox.metadata, "version", lambda package: "3.1.0")

    policy_path = write_firefox_policy(executable)

    document = json.loads(policy_path.read_text(encoding="utf-8"))
    extension = document["policies"]["ExtensionSettings"][EXTENSION_ID]
    assert extension == {
        "installation_mode": "force_installed",
        "install_url": (
            "https://github.com/DedInc/emunium/releases/download/3.1.0/"
            "emunium-extension-firefox.xpi"
        ),
    }
