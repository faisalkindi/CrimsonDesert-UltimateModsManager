"""Bug found via systematic-debugging on the just-shipped skip-tracking
work: when a user follows the yellow SKIPPED-badge tooltip's advice
('right-click → Reimport from source') and reimports the mod, the
badge keeps showing the old skip count and tooltip lines. Reimport
regenerates patches against current vanilla bytes, so the old skip
record is no longer relevant — but every existing_mod_id UPDATE in
the import/variant pipelines leaves the skip columns untouched.

The user reads the tooltip, takes the recommended action, and the
badge looks like it never noticed. Worst case: they reimport, see
the badge stuck, and conclude CDUMM is broken.

Fix: every existing_mod_id reimport branch must also reset
``last_apply_skipped_count = 0`` and ``last_apply_skip_summary = NULL``
so the slate is clean and the next Apply re-establishes truth.
"""
from __future__ import annotations
from pathlib import Path

import pytest


def _make_preset(tmp_path: Path, name: str, version: str = "1.0.0"
                 ) -> tuple[Path, dict]:
    p = tmp_path / f"{name}.json"
    p.write_text("{}", encoding="utf-8")
    data = {
        "name": "Test Mod",
        "label": name,
        "filename": p.name,
        "version": version,
        "author": "TestAuthor",
        "description": "test",
    }
    return p, data


def test_variant_reimport_clears_skip_state(tmp_path: Path):
    """import_multi_variant on an existing mod with a populated skip
    summary must reset both columns to (0, NULL)."""
    from cdumm.engine.variant_handler import import_multi_variant
    from cdumm.storage.database import Database

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()

    db = Database(tmp_path / "test.db")
    db.initialize()

    # Mod previously had 3 skipped patches from a prior Apply.
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, priority, author, version, "
        "description, configurable, last_apply_skipped_count, "
        "last_apply_skip_summary) "
        "VALUES (1, 'Test Mod', 'paz', 1, 'A', '1.0.0', 'd', 1, 3, "
        "'[{\"label\":\"old_entry\",\"reason\":\"byte mismatch\"}]')"
    )
    db.connection.commit()

    presets = [_make_preset(tmp_path, "PresetA", "1.1.0")]

    result = import_multi_variant(
        presets=presets,
        source=tmp_path / "TestMod-1-1-0.zip",
        game_dir=game_dir,
        mods_dir=mods_dir,
        db=db,
        existing_mod_id=1,
        initial_selection={presets[0][0]},
    )
    assert result is not None

    row = db.connection.execute(
        "SELECT last_apply_skipped_count, last_apply_skip_summary "
        "FROM mods WHERE id = 1"
    ).fetchone()
    count, summary = row
    assert count == 0, (
        f"Reimport must zero last_apply_skipped_count so the yellow "
        f"badge clears. Got {count!r}, expected 0. The patches the "
        f"old skips referred to no longer exist after reimport."
    )
    assert summary is None, (
        f"Reimport must NULL last_apply_skip_summary. Got {summary!r}. "
        f"Stale tooltip lines pointing at patches that no longer exist "
        f"are worse than no tooltip at all."
    )

    db.close()


def test_json_fast_reimport_clears_skip_state(tmp_path: Path):
    """json_patch_handler.import_json_fast on existing_mod_id must
    reset skip columns. Hit via the standard Reimport-from-source
    action for JSON-patch mods (the most common case for mods that
    produce skips, since byte-mismatch is a JSON-patch-specific
    failure)."""
    from cdumm.engine.json_patch_handler import import_json_fast
    from cdumm.storage.database import Database

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    mods_dir = tmp_path / "mods"

    db = Database(tmp_path / "test.db")
    db.initialize()

    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, priority, author, version, "
        "description, last_apply_skipped_count, last_apply_skip_summary) "
        "VALUES (1, 'JSON Mod', 'paz', 1, 'A', '1.0', 'd', 5, "
        "'[{\"label\":\"x\",\"reason\":\"byte mismatch\"}]')"
    )
    db.connection.commit()

    # Empty patches list skips PAMT lookup entirely — we only care
    # about the DB UPDATE branch behaviour here.
    patch_data = {
        "modinfo": {"title": "JSON Mod", "version": "1.1"},
        "patches": [],
    }

    result = import_json_fast(
        patch_data=patch_data,
        game_dir=game_dir,
        db=db,
        mods_dir=mods_dir,
        mod_name="JSON Mod",
        existing_mod_id=1,
    )
    assert result is not None

    row = db.connection.execute(
        "SELECT last_apply_skipped_count, last_apply_skip_summary "
        "FROM mods WHERE id = 1"
    ).fetchone()
    count, summary = row
    assert count == 0, (
        f"JSON-source reimport must zero last_apply_skipped_count. "
        f"Got {count!r}. Bug surfaces when the badge tooltip directs "
        f"the user to reimport, they comply, and the badge still shows."
    )
    assert summary is None

    db.close()
