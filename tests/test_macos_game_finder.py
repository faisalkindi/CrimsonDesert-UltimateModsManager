"""Tests for the macOS-specific paths in ``cdumm.storage.game_finder``.

The native macOS Crimson Desert build is an opaque ``.app`` bundle
whose game files live at ``Contents/Resources/packages/``. Unlike the
Windows build there is no ``bin64/CrimsonDesert.exe`` for
``validate_game_directory`` to look for, and the auto-detect scan
needs to look in macOS-specific locations (Steam library under
``~/Library/Application Support/Steam``, ``~/Games``,
``~/Applications``, ``/Applications``).

This file uses ``monkeypatch`` to override the module-level constants
that gate the macOS scan (``MACOS_GAME_LOCATIONS``,
``STEAM_DEFAULT_PATHS_MACOS``) and toggles ``IS_MACOS`` to exercise
the ``validate_game_directory`` macOS branch from any host. Result:
the tests run as part of ``pytest tests/`` on Windows CI (the
maintainer's regression net) without a real macOS, and document the
contract the macOS port relies on.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cdumm.storage import game_finder


# ── fixtures ─────────────────────────────────────────────────────────


def _make_app_bundle(parent: Path, app_name: str = "Crimson Desert.app") -> Path:
    """Build a fixture .app directory that looks like the genuine
    macOS Crimson Desert install layout: empty PAZ + PAMT files
    sufficient for ``_looks_like_game_root`` to accept the inner
    ``Contents/Resources/packages/`` dir.
    """
    app = parent / app_name
    inner = app / "Contents" / "Resources" / "packages"
    (inner / "0008").mkdir(parents=True)
    (inner / "0008" / "0.paz").write_bytes(b"stub")
    (inner / "meta").mkdir()
    (inner / "meta" / "0.papgt").write_bytes(b"stub")
    return app


def _make_inner_packages(parent: Path) -> Path:
    """Bare-bones inner ``packages/`` directory without a wrapping
    .app — the layout a Wine-bottle setup might present."""
    inner = parent / "packages"
    (inner / "0008").mkdir(parents=True)
    (inner / "0008" / "0.paz").write_bytes(b"stub")
    (inner / "meta").mkdir()
    (inner / "meta" / "0.papgt").write_bytes(b"stub")
    return inner


# ── _resolve_macos_game_dir ──────────────────────────────────────────


class TestResolveMacosGameDir:
    """``_resolve_macos_game_dir(candidate)`` walks user-supplied paths
    to the canonical inner ``packages/`` directory CDUMM operates on."""

    def test_inner_packages_returns_self(self, tmp_path):
        inner = _make_inner_packages(tmp_path)
        assert game_finder._resolve_macos_game_dir(inner) == inner

    def test_app_bundle_walks_to_inner_packages(self, tmp_path):
        app = _make_app_bundle(tmp_path)
        result = game_finder._resolve_macos_game_dir(app)
        assert result == app / "Contents" / "Resources" / "packages"

    def test_parent_dir_with_app_finds_inner(self, tmp_path):
        # User points at ~/Games — auto-detect finds the .app inside.
        _make_app_bundle(tmp_path)
        result = game_finder._resolve_macos_game_dir(tmp_path)
        assert result == tmp_path / "Crimson Desert.app" / "Contents" / "Resources" / "packages"

    def test_unrelated_dir_returns_none(self, tmp_path):
        unrelated = tmp_path / "DocumentsBackup"
        unrelated.mkdir()
        (unrelated / "file.txt").write_text("hi")
        assert game_finder._resolve_macos_game_dir(unrelated) is None

    def test_nonexistent_path_returns_none(self):
        # Don't use an absolute Windows-shaped path here — Path("/does/...")
        # is portable.
        assert game_finder._resolve_macos_game_dir(
            Path("/does/not/exist/anywhere")) is None

    def test_empty_app_shell_returns_none(self, tmp_path):
        # A .app exists but has no Contents/Resources/packages tree.
        empty_app = tmp_path / "Empty.app"
        empty_app.mkdir()
        assert game_finder._resolve_macos_game_dir(empty_app) is None


# ── _find_macos_game_directories (auto-detect) ───────────────────────


class TestFindMacosGameDirectories:
    """``find_game_directories`` on macOS routes to
    ``_find_macos_game_directories`` which scans the user's
    ``~/Games``, ``~/Applications``, ``/Applications``, and macOS Steam
    library tree. We monkeypatch the constants to point at fixture
    dirs so the test runs anywhere."""

    def test_finds_app_in_games_location(self, tmp_path, monkeypatch):
        games = tmp_path / "Games"
        games.mkdir()
        _make_app_bundle(games)

        monkeypatch.setattr(
            game_finder, "MACOS_GAME_LOCATIONS", [games])
        monkeypatch.setattr(
            game_finder, "STEAM_DEFAULT_PATHS_MACOS", [])

        results = game_finder._find_macos_game_directories()
        assert len(results) == 1
        assert results[0] == games / "Crimson Desert.app" / "Contents" / "Resources" / "packages"

    def test_finds_app_in_steam_library(self, tmp_path, monkeypatch):
        steam_root = tmp_path / "Steam"
        common = steam_root / "steamapps" / "common"
        common.mkdir(parents=True)
        _make_app_bundle(common)

        monkeypatch.setattr(
            game_finder, "MACOS_GAME_LOCATIONS", [])
        monkeypatch.setattr(
            game_finder, "STEAM_DEFAULT_PATHS_MACOS", [steam_root])

        results = game_finder._find_macos_game_directories()
        assert len(results) == 1
        assert results[0].name == "packages"
        assert "Crimson Desert.app" in str(results[0])

    def test_no_install_returns_empty(self, tmp_path, monkeypatch):
        empty = tmp_path / "Empty"
        empty.mkdir()
        monkeypatch.setattr(
            game_finder, "MACOS_GAME_LOCATIONS", [empty])
        monkeypatch.setattr(
            game_finder, "STEAM_DEFAULT_PATHS_MACOS", [])
        assert game_finder._find_macos_game_directories() == []

    def test_unrelated_app_in_games_is_ignored(self, tmp_path, monkeypatch):
        # The scanner filters by name (".*crimson.*" case-insensitive)
        # so other .app bundles in the same directory don't false-positive.
        games = tmp_path / "Games"
        games.mkdir()
        _make_app_bundle(games, app_name="Some Other Game.app")

        monkeypatch.setattr(
            game_finder, "MACOS_GAME_LOCATIONS", [games])
        monkeypatch.setattr(
            game_finder, "STEAM_DEFAULT_PATHS_MACOS", [])

        assert game_finder._find_macos_game_directories() == []


# ── validate_game_directory on macOS ─────────────────────────────────


class TestValidateGameDirectoryMacos:
    """When ``IS_MACOS`` is True, ``validate_game_directory`` must
    accept the .app bundle (walked into) and the inner packages/
    directly. The Windows ``bin64/CrimsonDesert.exe`` check should NOT
    apply — the native macOS build doesn't ship a Windows exe."""

    def test_accepts_app_bundle(self, tmp_path, monkeypatch):
        monkeypatch.setattr(game_finder, "IS_MACOS", True)
        app = _make_app_bundle(tmp_path)
        assert game_finder.validate_game_directory(app) is True

    def test_accepts_inner_packages(self, tmp_path, monkeypatch):
        monkeypatch.setattr(game_finder, "IS_MACOS", True)
        inner = _make_inner_packages(tmp_path)
        assert game_finder.validate_game_directory(inner) is True

    def test_rejects_unrelated_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(game_finder, "IS_MACOS", True)
        unrelated = tmp_path / "Empty"
        unrelated.mkdir()
        assert game_finder.validate_game_directory(unrelated) is False

    def test_does_not_require_bin64_exe(self, tmp_path, monkeypatch):
        """Sanity check: a directory that has bin64/CrimsonDesert.exe
        but NO PAZ layout (i.e. a Windows-style install path picked
        up by mistake on macOS) should be rejected. macOS only
        validates the .app / inner packages PAZ structure."""
        monkeypatch.setattr(game_finder, "IS_MACOS", True)
        windows_style = tmp_path / "Crimson Desert"
        (windows_style / "bin64").mkdir(parents=True)
        (windows_style / "bin64" / "CrimsonDesert.exe").touch()
        # No 0008/0.paz, no meta/0.papgt → resolve_macos_game_dir
        # returns None → validate returns False.
        assert game_finder.validate_game_directory(windows_style) is False
