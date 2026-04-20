"""Resolve a mod's on-disk source directory for the "Open source files" action.

Two candidates in order:
  1. The `source_path` column from the mods table (if it points at an existing
     path). If it's a file, use its parent so Explorer shows the file.
  2. `<game_dir>/CDMods/sources/<mod_id>/` as the fallback layout CDUMM writes
     when it imports archives.

Returns None when neither candidate exists -- callers show an InfoBar.

Pure-logic module: no Qt imports, no database access.
"""
from __future__ import annotations

from pathlib import Path


def resolve_mod_source_path(mod: dict, game_dir: Path) -> Path | None:
    """Return the best existing source directory for this mod, or None.

    `mod` is a row from the mods table (dict-like). `game_dir` is the
    configured Crimson Desert install root.
    """
    raw_source = mod.get("source_path")
    if raw_source:
        candidate = Path(raw_source)
        if candidate.exists():
            return candidate if candidate.is_dir() else candidate.parent

    mod_id = mod.get("id")
    if mod_id is not None:
        fallback = Path(game_dir) / "CDMods" / "sources" / str(mod_id)
        if fallback.exists() and fallback.is_dir():
            return fallback

    return None
