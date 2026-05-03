"""Bug from Faisal 2026-05-03: clicking 'Click To Update' on a
multi-variant mod (NPC Trust Gain in his case) downloads the new
file, opens the variant picker, the user picks options, the
window closes... and the pill stays red.

Root cause: import_multi_variant has separate code paths for new
imports (INSERT) and re-imports into an existing mod_id (UPDATE).
The INSERT branch sets name/author/version/description/
game_version_hash from the new presets. The UPDATE branch only
rewrites json_source/variants/configurable and leaves all the
identity fields stale, so mods.version stays at the OLD value
even after a successful re-import. The Nexus update check then
compares stale version to latest-on-Nexus and keeps the red pill.

Fix: in the existing_mod_id branch, also UPDATE name, author,
version, description, game_version_hash with the freshly-parsed
values from the new presets.
"""
from __future__ import annotations
from pathlib import Path

import pytest


def _make_preset(tmp_path: Path, name: str, version: str,
                 author: str = "AuthorB",
                 description: str = "new desc") -> tuple[Path, dict]:
    """Create a minimal Format 3 JSON preset on disk."""
    p = tmp_path / f"{name}.json"
    p.write_text("{}", encoding="utf-8")  # variant_handler reads dict separately
    data = {
        "name": "NPC Trust Gain",
        "label": name,
        "filename": p.name,
        "version": version,
        "author": author,
        "description": description,
    }
    return p, data


def test_import_multi_variant_reimport_updates_version(tmp_path: Path):
    """Re-importing into an existing mod_id must overwrite version /
    author / description with the new presets' values, otherwise the
    Nexus update pill stays red after a successful update."""
    from cdumm.engine.variant_handler import import_multi_variant
    from cdumm.storage.database import Database

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()

    db = Database(tmp_path / "test.db")
    db.initialize()

    # Pre-existing mod row: simulates the local v1.04.00 install.
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, priority, author, version, "
        "description, configurable) "
        "VALUES (1, 'NPC Trust Gain', 'paz', 1, 'AuthorA', '1.04.00', "
        "'old desc', 1)"
    )
    db.connection.commit()

    # User just clicked Click-To-Update; downloaded file contains
    # new variants at v1.05.00.
    presets = [
        _make_preset(tmp_path, "Trust Me 10x", "1.05.00",
                     author="GildyBoye", description="updated for 1.05.00"),
        _make_preset(tmp_path, "Trust Me 5x", "1.05.00",
                     author="GildyBoye", description="updated for 1.05.00"),
    ]

    result = import_multi_variant(
        presets=presets,
        source=tmp_path / "NPC Trust Gain-350-1-05-00.zip",
        game_dir=game_dir,
        mods_dir=mods_dir,
        db=db,
        existing_mod_id=1,
        initial_selection={presets[0][0]},
    )
    assert result is not None
    assert result["mod_id"] == 1

    row = db.connection.execute(
        "SELECT version, author, description FROM mods WHERE id = 1"
    ).fetchone()
    version, author, description = row
    assert version == "1.05.00", (
        f"Re-import did not bump mods.version. Got {version!r}, expected "
        f"'1.05.00'. Pill stays red because the next Nexus update check "
        f"compares this stale version against latest. Bug from Faisal's "
        f"NPC Trust Gain test 2026-05-03."
    )
    assert author == "GildyBoye"
    assert description == "updated for 1.05.00"

    db.close()
