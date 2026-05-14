"""Tests for the Linux-specific paths in ``cdumm.storage.game_finder``.

Crimson Desert has no native Linux build — it runs under Proton, and
the ``bin64/CrimsonDesert.exe`` layout on disk is identical to the
Windows install (Steam doesn't rewrite game files; Proton runs the
Windows binary via Wine). The Linux port only changes *where Steam
itself lives* on the host filesystem, not what Steam ships into the
library. So the validation rules from ``test_game_finder.py`` still
hold — these tests cover the Linux-side auto-detect that walks the
distro-specific Steam install locations (native package, Flatpak,
Snap) and resolves the ``libraryfolders.vdf`` chain.

Uses ``monkeypatch`` to override the module-level
``STEAM_DEFAULT_PATHS_LINUX`` constant and the ``IS_LINUX`` /
``IS_MACOS`` flags so the suite runs on any host CI (including the
maintainer's Windows regression box) without needing a real Linux
Steam install.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cdumm.storage import game_finder


# ── fixtures ─────────────────────────────────────────────────────────


def _make_game_install(library_root: Path) -> Path:
    """Build a fixture Steam library layout that ``_find_linux_steam_libraries``
    will accept: ``<library>/steamapps/common/Crimson Desert/bin64/CrimsonDesert.exe``.
    Returns the game directory (the one CDUMM treats as the install root).
    """
    game_dir = library_root / "steamapps" / "common" / "Crimson Desert"
    (game_dir / "bin64").mkdir(parents=True)
    (game_dir / "bin64" / "CrimsonDesert.exe").write_bytes(b"EXE")
    return game_dir


def _make_libraryfolders_vdf(steam_root: Path, library_paths: list[Path]) -> None:
    """Write a libraryfolders.vdf that lists ``library_paths``. Matches
    the exact format Steam itself produces — quoted ``path`` keys
    inside numbered library blocks. The parser is regex-based and
    only cares about the ``"path"`` tokens, so whitespace and the
    surrounding scaffolding can be sparse."""
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    vdf.parent.mkdir(parents=True, exist_ok=True)
    blocks = []
    for i, p in enumerate(library_paths):
        # Steam stores paths with forward slashes on Linux/macOS;
        # the parser handles either. Escape backslashes in case a
        # contributor copies a Windows-style fixture in.
        escaped = str(p).replace("\\", "\\\\")
        blocks.append(f'    "{i}"\n    {{\n        "path"\t\t"{escaped}"\n    }}\n')
    vdf.write_text(
        '"libraryfolders"\n{\n' + "".join(blocks) + "}\n",
        encoding="utf-8")


@pytest.fixture
def linux_host(monkeypatch):
    """Make the module believe it's running on Linux."""
    monkeypatch.setattr(game_finder, "IS_LINUX", True)
    monkeypatch.setattr(game_finder, "IS_MACOS", False)
    monkeypatch.setattr(game_finder, "IS_WINDOWS", False)


# ── _find_linux_steam_libraries ──────────────────────────────────────


class TestFindLinuxSteamLibraries:
    """Steam-on-Linux detection: walk ``STEAM_DEFAULT_PATHS_LINUX``,
    parse each install's ``libraryfolders.vdf``, return every
    Crimson Desert install whose ``bin64/CrimsonDesert.exe`` exists."""

    def test_finds_game_in_default_linux_steam_root(
            self, tmp_path: Path, monkeypatch, linux_host) -> None:
        """The common case: native distro Steam install at
        ``~/.local/share/Steam`` with CD in its primary library."""
        steam_root = tmp_path / "local-share-Steam"
        steam_root.mkdir()
        game_dir = _make_game_install(steam_root)

        monkeypatch.setattr(
            game_finder, "STEAM_DEFAULT_PATHS_LINUX", [steam_root])

        found = game_finder._find_linux_steam_libraries()
        assert found == [game_dir]

    def test_follows_libraryfolders_vdf_to_secondary_drive(
            self, tmp_path: Path, monkeypatch, linux_host) -> None:
        """Steam at ``~/.local/share/Steam`` but the user added a
        second library on another drive — typical Steam Deck +
        microSD setup, or anyone with games split across SSDs.
        libraryfolders.vdf is the source of truth, not the primary
        install."""
        steam_root = tmp_path / "primary-Steam"
        steam_root.mkdir()
        secondary = tmp_path / "external-disk" / "SteamLibrary"
        secondary.mkdir(parents=True)
        game_dir = _make_game_install(secondary)
        _make_libraryfolders_vdf(steam_root, [steam_root, secondary])

        monkeypatch.setattr(
            game_finder, "STEAM_DEFAULT_PATHS_LINUX", [steam_root])

        found = game_finder._find_linux_steam_libraries()
        assert game_dir in found

    def test_returns_empty_when_no_steam_install_present(
            self, tmp_path: Path, monkeypatch, linux_host) -> None:
        """Fresh box / Steam not installed: probe every default path,
        find nothing, return empty list. Must not raise."""
        nowhere = tmp_path / "nothing-here"
        monkeypatch.setattr(
            game_finder, "STEAM_DEFAULT_PATHS_LINUX",
            [nowhere, nowhere / "also-missing"])

        assert game_finder._find_linux_steam_libraries() == []

    def test_dedupes_symlinked_steam_roots(
            self, tmp_path: Path, monkeypatch, linux_host) -> None:
        """``~/.steam/steam`` and ``~/.steam/root`` are usually
        symlinks to ``~/.local/share/Steam``. Probing all three
        without dedup would parse the same libraryfolders.vdf
        repeatedly and return the same game three times. The dedup
        is by ``Path.resolve()`` so symlinks collapse to a single
        canonical root."""
        real_root = tmp_path / "local-share-Steam"
        real_root.mkdir()
        _make_game_install(real_root)

        symlink_a = tmp_path / "dot-steam-steam"
        symlink_b = tmp_path / "dot-steam-root"
        symlink_a.symlink_to(real_root, target_is_directory=True)
        symlink_b.symlink_to(real_root, target_is_directory=True)

        monkeypatch.setattr(
            game_finder, "STEAM_DEFAULT_PATHS_LINUX",
            [real_root, symlink_a, symlink_b])

        found = game_finder._find_linux_steam_libraries()
        # One unique resolved root, one game install, one result.
        assert len(found) == 1

    def test_handles_broken_symlink_in_default_paths(
            self, tmp_path: Path, monkeypatch, linux_host) -> None:
        """A broken symlink (target removed after install) must not
        crash the scan. Pre-bcachefs-migration Steam Deck and
        post-distro-reinstall systems can both leave these around."""
        broken = tmp_path / "broken-symlink"
        broken.symlink_to(tmp_path / "target-deleted",
                          target_is_directory=True)

        real_root = tmp_path / "real-Steam"
        real_root.mkdir()
        game_dir = _make_game_install(real_root)

        monkeypatch.setattr(
            game_finder, "STEAM_DEFAULT_PATHS_LINUX", [broken, real_root])

        found = game_finder._find_linux_steam_libraries()
        assert found == [game_dir]

    def test_flatpak_steam_install_location_is_probed(
            self, tmp_path: Path, monkeypatch, linux_host) -> None:
        """Flatpak Steam stores its data under
        ``~/.var/app/com.valvesoftware.Steam/...``. The default
        path list must include this so Flatpak users get
        auto-detected; this test pins the contract."""
        flatpak_root = (tmp_path / ".var" / "app"
                        / "com.valvesoftware.Steam" / "data" / "Steam")
        flatpak_root.mkdir(parents=True)
        game_dir = _make_game_install(flatpak_root)

        monkeypatch.setattr(
            game_finder, "STEAM_DEFAULT_PATHS_LINUX", [flatpak_root])

        found = game_finder._find_linux_steam_libraries()
        assert found == [game_dir]


# ── find_game_directories (Linux dispatcher branch) ─────────────────


class TestFindGameDirectoriesLinux:
    """The top-level entry point must short-circuit to the Linux
    helpers when running on Linux — otherwise the Windows fallback
    iterates A-Z drive letters against literal nonexistent paths,
    which is wasted work and pollutes the logs."""

    def test_uses_linux_branch_on_linux(
            self, tmp_path: Path, monkeypatch, linux_host) -> None:
        steam_root = tmp_path / "Steam"
        steam_root.mkdir()
        game_dir = _make_game_install(steam_root)

        monkeypatch.setattr(
            game_finder, "STEAM_DEFAULT_PATHS_LINUX", [steam_root])

        found = game_finder.find_game_directories()
        assert game_dir in found

    def test_returns_empty_when_no_install_found(
            self, tmp_path: Path, monkeypatch, linux_host) -> None:
        """Linux + no Steam install present + no Crimson Desert
        anywhere = empty result. Must not fall through to the
        Windows drive-scan code (which would iterate ``A:/Steam``
        through ``Z:/Steam`` and waste time)."""
        monkeypatch.setattr(
            game_finder, "STEAM_DEFAULT_PATHS_LINUX",
            [tmp_path / "no-such-dir"])

        assert game_finder.find_game_directories() == []


# ── validate_game_directory on Linux ────────────────────────────────


class TestValidateGameDirectoryLinux:
    """Linux validation uses the same ``bin64/CrimsonDesert.exe``
    check as Windows — Proton runs the Windows binary directly, so
    the install layout is identical. These tests pin that contract
    so a future refactor doesn't accidentally route Linux through
    the macOS ``.app`` resolver."""

    def test_accepts_steam_install_with_bin64_exe(
            self, tmp_path: Path, linux_host) -> None:
        game_dir = _make_game_install(tmp_path / "Steam")
        assert game_finder.validate_game_directory(game_dir) is True

    def test_rejects_directory_without_exe(
            self, tmp_path: Path, linux_host) -> None:
        game_dir = tmp_path / "Empty"
        game_dir.mkdir()
        assert game_finder.validate_game_directory(game_dir) is False
