"""Pure-logic helper for resolving a mod's on-disk source directory.

Used by the right-click "Open source files" context menu. Plain dict + Path
in, Path or None out -- no Qt dependency.
"""
from __future__ import annotations

from pathlib import Path

from cdumm.engine.mod_source_path import resolve_mod_source_path


def test_resolve_prefers_source_path_from_db(tmp_path: Path) -> None:
    """If the mod row's source_path exists on disk, return it."""
    real_source = tmp_path / "my-mod-source"
    real_source.mkdir()
    mod = {"id": 42, "source_path": str(real_source)}
    game_dir = tmp_path / "game"
    game_dir.mkdir()

    assert resolve_mod_source_path(mod, game_dir) == real_source


def test_resolve_falls_back_to_cdmods_sources_dir(tmp_path: Path) -> None:
    """No source_path in mod row, but game_dir/CDMods/sources/<id> exists."""
    game_dir = tmp_path / "game"
    fallback = game_dir / "CDMods" / "sources" / "42"
    fallback.mkdir(parents=True)
    mod = {"id": 42, "source_path": None}

    assert resolve_mod_source_path(mod, game_dir) == fallback


def test_resolve_fallback_used_when_source_path_deleted(tmp_path: Path) -> None:
    """source_path points somewhere that no longer exists -> try fallback."""
    game_dir = tmp_path / "game"
    fallback = game_dir / "CDMods" / "sources" / "99"
    fallback.mkdir(parents=True)
    mod = {"id": 99, "source_path": str(tmp_path / "ghost" / "deleted")}

    assert resolve_mod_source_path(mod, game_dir) == fallback


def test_resolve_returns_none_when_nothing_exists(tmp_path: Path) -> None:
    """Both candidates missing -> None (caller shows an InfoBar)."""
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    mod = {"id": 42, "source_path": None}

    assert resolve_mod_source_path(mod, game_dir) is None


def test_resolve_handles_missing_source_path_key(tmp_path: Path) -> None:
    """Mod dict may not have a source_path key at all."""
    game_dir = tmp_path / "game"
    fallback = game_dir / "CDMods" / "sources" / "7"
    fallback.mkdir(parents=True)
    mod = {"id": 7}

    assert resolve_mod_source_path(mod, game_dir) == fallback


def test_resolve_empty_source_path_string_falls_through(tmp_path: Path) -> None:
    """Empty string in source_path is treated as absent."""
    game_dir = tmp_path / "game"
    fallback = game_dir / "CDMods" / "sources" / "3"
    fallback.mkdir(parents=True)
    mod = {"id": 3, "source_path": ""}

    assert resolve_mod_source_path(mod, game_dir) == fallback


def test_resolve_source_path_to_file_not_dir(tmp_path: Path) -> None:
    """If source_path points to a file (not a dir), its parent is used so
    opening it shows the file in Explorer."""
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    src_file = tmp_path / "archive.zip"
    src_file.write_text("x")
    mod = {"id": 5, "source_path": str(src_file)}

    result = resolve_mod_source_path(mod, game_dir)
    assert result == tmp_path  # parent of the file
