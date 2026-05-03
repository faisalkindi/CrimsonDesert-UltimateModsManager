"""Adjacent bug from systematic-debugging review of the variant
re-import fix (Faisal NPC Trust Gain 2026-05-03):

After import_multi_variant overwrites a mod with new content (same
existing_mod_id), the json_source PATH is identical to before
(CDMods/mods/<mod_id>/merged.json), and variant mods have no
mod_deltas rows to bust. So _compute_apply_fingerprint hashes the
SAME inputs and produces the SAME hash. The next Apply reads the
stored .apply_fingerprint, sees a match, and fast-paths
'Already up to date' — the new mod content never lands in game.

This forces the user into the same Revert + Apply cycle that
GitHub #59 / DerBambusbjoern reported.

Fix: invalidate the apply fingerprint on every re-import so the
next Apply genuinely re-runs the pipeline.
"""
from __future__ import annotations
import json
from pathlib import Path

import pytest


def _make_preset(tmp_path: Path, name: str, version: str = "1.05.00"
                 ) -> tuple[Path, dict]:
    p = tmp_path / f"{name}.json"
    p.write_text("{}", encoding="utf-8")
    return p, {
        "name": "NPC Trust Gain",
        "label": name,
        "filename": p.name,
        "version": version,
        "author": "GildyBoye",
        "description": f"updated for {version}",
    }


def test_import_multi_variant_invalidates_apply_fingerprint(tmp_path: Path):
    """Re-import must remove CDMods/.apply_fingerprint so the next
    Apply doesn't fast-path 'Already up to date' on a freshly-updated
    mod."""
    from cdumm.engine.variant_handler import import_multi_variant
    from cdumm.storage.database import Database

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    cdmods = game_dir / "CDMods"
    cdmods.mkdir()
    mods_dir = cdmods / "mods"
    mods_dir.mkdir()

    # Simulate: a previous Apply wrote the fingerprint file.
    fp_path = cdmods / ".apply_fingerprint"
    fp_path.write_text("OLD_FINGERPRINT_HASH", encoding="utf-8")
    assert fp_path.exists()

    db = Database(tmp_path / "test.db")
    db.initialize()
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, priority, author, version, "
        "description, configurable) "
        "VALUES (1, 'NPC Trust Gain', 'paz', 1, 'GildyBoye', '1.04.00', "
        "'old desc', 1)"
    )
    db.connection.commit()

    presets = [_make_preset(tmp_path, "Trust Me 10x")]
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

    assert not fp_path.exists(), (
        f"After re-import, .apply_fingerprint must be removed so the "
        f"next Apply genuinely re-runs the pipeline on the new mod "
        f"content. The fingerprint hash inputs (json_source path, "
        f"mod_deltas) don't change for variant re-imports, so without "
        f"explicit invalidation Apply silently skips with 'Already up "
        f"to date'."
    )

    db.close()


def test_invalidation_handles_missing_fingerprint_file(tmp_path: Path):
    """No-op when the fingerprint file doesn't exist (fresh install,
    or already revert-ed). Re-import must not raise FileNotFoundError."""
    from cdumm.engine.variant_handler import import_multi_variant
    from cdumm.storage.database import Database

    game_dir = tmp_path / "game"
    (game_dir / "CDMods").mkdir(parents=True)
    mods_dir = game_dir / "CDMods" / "mods"
    mods_dir.mkdir()

    db = Database(tmp_path / "test.db")
    db.initialize()
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, priority, version, configurable) "
        "VALUES (1, 'X', 'paz', 1, '0.1', 1)"
    )
    db.connection.commit()

    presets = [_make_preset(tmp_path, "v1")]
    # Should not raise
    result = import_multi_variant(
        presets=presets,
        source=tmp_path / "X.zip",
        game_dir=game_dir,
        mods_dir=mods_dir,
        db=db,
        existing_mod_id=1,
        initial_selection={presets[0][0]},
    )
    assert result is not None
    db.close()
