"""scottykyzer on Nexus (CDUMM v3.2.13) reported the in-app update banner
dropped them on the GitHub releases page where they were confused by
.dmg files, source-code zips, and tarballs. The fix points the banner
button at the *exact* asset for the user's platform via a direct
download URL.

These tests pin down the URL builder so a refactor can't silently send
users back to the confusing releases-listing page. The format is the
canonical GitHub release-download URL — same shape softprops/action-gh-release
publishes when it attaches assets to a tag-triggered release.
"""
from __future__ import annotations

import pytest

from cdumm.engine.update_checker import (
    GITHUB_REPO,
    WINDOWS_ASSET,
    _release_asset_url,
    asset_for_current_platform,
    macos_asset_name,
)


def test_release_asset_url_windows_exe() -> None:
    """Windows users get pointed at CDUMM3.exe directly."""
    url = _release_asset_url("3.2.14", WINDOWS_ASSET)
    assert url == (
        "https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager"
        "/releases/download/v3.2.14/CDUMM3.exe"
    )


def test_release_asset_url_macos_dmg() -> None:
    """macOS users get pointed at the same-version DMG. The asset name
    matches DMG_NAME in scripts/build-macos.sh and the upload-artifact
    path in .github/workflows/release-macos.yml — refactoring either
    side without the other is a regression caught by this test."""
    asset = macos_asset_name("3.2.14")
    assert asset == "CDUMM-3.2.14-macos-arm64.dmg"
    url = _release_asset_url("3.2.14", asset)
    assert url == (
        "https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager"
        "/releases/download/v3.2.14/CDUMM-3.2.14-macos-arm64.dmg"
    )


def test_release_asset_url_strips_leading_v() -> None:
    """The GitHub API returns tags with a ``v`` prefix
    (``data['tag_name'] == 'v3.2.14'``) but the workflows tag releases
    with that same ``v``. _release_asset_url normalises so callers can
    pass either form without ending up with ``vv3.2.14`` in the URL."""
    with_prefix = _release_asset_url("v3.2.14", WINDOWS_ASSET)
    without_prefix = _release_asset_url("3.2.14", WINDOWS_ASSET)
    assert with_prefix == without_prefix
    # Exactly one ``v`` between ``download/`` and the version digits.
    assert "/download/v3.2.14/" in with_prefix
    assert "/download/vv" not in with_prefix


def test_release_asset_url_uses_canonical_repo() -> None:
    """A typo in ``GITHUB_REPO`` would silently send every CDUMM user
    to a 404. Pin the repo path so a rename / fork doesn't slip in."""
    url = _release_asset_url("1.0.0", "anything.zip")
    assert GITHUB_REPO == "faisalkindi/CrimsonDesert-UltimateModsManager"
    assert f"/{GITHUB_REPO}/" in url


def test_macos_asset_name_strips_v_prefix() -> None:
    """build-macos.sh derives the version from cdumm/__init__.py
    (no ``v`` prefix) so the DMG name never has ``v`` in it. The helper
    must match that even when given a ``v``-prefixed tag from the
    GitHub API."""
    assert macos_asset_name("v3.2.14") == "CDUMM-3.2.14-macos-arm64.dmg"
    assert macos_asset_name("3.2.14") == "CDUMM-3.2.14-macos-arm64.dmg"


def test_asset_for_current_platform_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows, asset_for_current_platform always returns the
    versionless CDUMM3.exe — every release ships it under that exact
    name (cdumm.spec emits ``name='CDUMM3'``)."""
    import cdumm.engine.update_checker as uc
    monkeypatch.setattr(uc.sys, "platform", "win32")
    assert uc.asset_for_current_platform("3.2.14") == "CDUMM3.exe"
    assert uc.asset_for_current_platform("99.0.0") == "CDUMM3.exe"


def test_asset_for_current_platform_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """On macOS, asset_for_current_platform returns the version-stamped
    DMG so the URL builder produces a working link."""
    import cdumm.engine.update_checker as uc
    monkeypatch.setattr(uc.sys, "platform", "darwin")
    assert (uc.asset_for_current_platform("3.2.14")
            == "CDUMM-3.2.14-macos-arm64.dmg")


def test_asset_for_current_platform_linux_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linux has no signed release asset — the banner falls back to
    opening the GitHub release page (legacy behaviour). Returning None
    is the contract that triggers that fallback in
    fluent_window._show_update_banner."""
    import cdumm.engine.update_checker as uc
    monkeypatch.setattr(uc.sys, "platform", "linux")
    assert uc.asset_for_current_platform("3.2.14") is None
