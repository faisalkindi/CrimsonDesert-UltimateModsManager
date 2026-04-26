"""import_from_natt_format_3 wires Format3 validator into the
import-handler stage so the user-facing message reflects what the
mod actually looks like, instead of a canned 'coming in future'.

Three buckets the wiring needs to handle:
  - malformed file → loader's ValueError message verbatim
  - all intents unapplicable (e.g., kori228's drops-array mod) →
    validator summary + workaround pointer
  - mixed supported / skipped → both counts + summary
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cdumm.engine.import_handler import import_from_natt_format_3


FIXTURE = Path(__file__).parent / "fixtures" / "format3" \
    / "dropsetinfo_5x_drops.json"


def _run(json_path, tmp_path):
    """Run the import_handler stub with throwaway stub deps."""
    db = MagicMock()
    snapshot = MagicMock()
    return import_from_natt_format_3(
        json_path=json_path,
        game_dir=tmp_path,
        db=db,
        snapshot=snapshot,
        deltas_dir=tmp_path,
    )


def test_kori228_dropsetinfo_mod_surfaces_skip_summary(tmp_path):
    """The actual user-submitted Format 3 mod from issue #41
    targets dropsetinfo._list (variable-length drops array).
    All 695 intents are unapplicable in current state. The user
    must see the count + reason in the error message — not the
    old canned 'coming in future' text."""
    result = _run(FIXTURE, tmp_path)
    assert result.error
    msg = result.error
    assert "695" in msg or "intent" in msg.lower()
    assert "dropsetinfo" in msg.lower()
    # Old canned text should be gone
    assert "field-names" not in msg or "skipped" in msg.lower()


def test_malformed_json_surfaces_loader_message(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not valid json {[", encoding="utf-8")
    result = _run(p, tmp_path)
    assert result.error
    # Either parser-error or the structural-validation error
    assert ("invalid" in result.error.lower()
            or "format 3" in result.error.lower()
            or "parse" in result.error.lower())


def test_mod_with_no_intents_passes_validation(tmp_path):
    """A Format 3 file with format=3 + target + empty intents
    list is structurally valid. Validator returns 0 supported,
    0 skipped. Importer should produce a non-confusing message
    rather than a misleading error."""
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({
        "modinfo": {"title": "Empty"},
        "format": 3,
        "target": "iteminfo.pabgb",
        "intents": [],
    }), encoding="utf-8")
    result = _run(p, tmp_path)
    assert result.error
    # Should mention the target, not crash
    assert "iteminfo" in result.error.lower()


def test_mod_with_unknown_target_table_skips_all_intents(tmp_path):
    """A Format 3 mod targeting a .pabgb table CDUMM doesn't have
    a schema for must surface a clear 'no schema' message — not
    silently fail or claim partial success."""
    p = tmp_path / "unknown.json"
    p.write_text(json.dumps({
        "modinfo": {"title": "UnknownTbl"},
        "format": 3,
        "target": "totallyfaketable.pabgb",
        "intents": [
            {"entry": "X", "key": 1, "field": "y",
             "op": "set", "new": 42},
        ],
    }), encoding="utf-8")
    result = _run(p, tmp_path)
    assert result.error
    # Validator's reason mentions "schema" — that should surface
    assert "schema" in result.error.lower()


# ── Persistence — mod row + mod_deltas + stored JSON ────────────────


import sqlite3
from unittest.mock import patch


def _real_db(tmp_path):
    """Build a minimal mods/mod_deltas/mod_config schema."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE mods ("
        "id INTEGER PRIMARY KEY, name TEXT, mod_type TEXT, "
        "enabled INTEGER DEFAULT 0, json_source TEXT, "
        "priority INTEGER, author TEXT, version TEXT, "
        "description TEXT, game_version_hash TEXT, "
        "disabled_patches TEXT)"
    )
    conn.execute(
        "CREATE TABLE mod_deltas ("
        "id INTEGER PRIMARY KEY, mod_id INTEGER, file_path TEXT, "
        "delta_path TEXT, byte_start INTEGER, byte_end INTEGER, "
        "entry_path TEXT, kind TEXT)"
    )
    conn.execute(
        "CREATE TABLE mod_config ("
        "mod_id INTEGER PRIMARY KEY, custom_values TEXT)"
    )
    conn.commit()

    class _DBWrap:
        def __init__(self, c):
            self.connection = c
    return _DBWrap(conn)


def test_mod_with_supported_intents_creates_db_row(
        tmp_path, monkeypatch):
    """When validation has supported intents, the importer must
    create a mods row with json_source pointing at the stored
    Format 3 JSON. Without a row, the apply pipeline can't see
    the mod and won't process it."""
    # Point the importer at a target table that DOES have a schema
    p = tmp_path / "mod.json"
    p.write_text(json.dumps({
        "modinfo": {"title": "Persisted Test Mod"},
        "format": 3,
        "target": "dropsetinfo.pabgb",
        "intents": [
            {"entry": "DropSet_X", "key": 100000,
             "field": "_dropTagNameHash",
             "op": "set", "new": 1234},
        ],
    }), encoding="utf-8")
    db = _real_db(tmp_path)
    snapshot = MagicMock()
    # Stub _find_pamt_entry — it normally walks the vanilla PAZ index
    fake_entry = MagicMock()
    fake_entry.paz_file = "/fake/0008/0.paz"
    fake_entry.path = "dropsetinfo.pabgb"
    fake_entry.offset = 100
    fake_entry.comp_size = 50
    monkeypatch.setattr(
        "cdumm.engine.json_patch_handler._find_pamt_entry",
        lambda gf, gd: fake_entry)

    from cdumm.engine.import_handler import import_from_natt_format_3
    result = import_from_natt_format_3(
        json_path=p, game_dir=tmp_path, db=db,
        snapshot=snapshot, deltas_dir=tmp_path)

    # Mod row exists with the right json_source
    rows = db.connection.execute(
        "SELECT id, name, json_source FROM mods").fetchall()
    assert len(rows) == 1, (
        f"expected exactly one mod row, got {rows}")
    mod_id, name, json_source = rows[0]
    assert "Persisted" in name or "persisted" in name.lower()
    assert json_source and Path(json_source).exists()
    # The result reports the mod_id so the GUI can reference it
    assert result.mod_id == mod_id


def test_mod_persistence_creates_mod_deltas_row_per_target(
        tmp_path, monkeypatch):
    """For conflict detection to see the Format 3 mod modifies a
    file, mod_deltas must have a row per target file. Without
    these rows, the conflict view shows the mod as touching no
    files even though it has supported intents."""
    p = tmp_path / "mod.json"
    p.write_text(json.dumps({
        "modinfo": {"title": "DeltaTest"},
        "format": 3,
        "target": "dropsetinfo.pabgb",
        "intents": [
            {"entry": "X", "key": 1, "field": "_dropTagNameHash",
             "op": "set", "new": 9876},
        ],
    }), encoding="utf-8")
    db = _real_db(tmp_path)
    fake_entry = MagicMock()
    fake_entry.paz_file = "/fake/0008/0.paz"
    fake_entry.path = "dropsetinfo.pabgb"
    fake_entry.offset = 100
    fake_entry.comp_size = 50
    monkeypatch.setattr(
        "cdumm.engine.json_patch_handler._find_pamt_entry",
        lambda gf, gd: fake_entry)

    from cdumm.engine.import_handler import import_from_natt_format_3
    import_from_natt_format_3(
        json_path=p, game_dir=tmp_path, db=db,
        snapshot=MagicMock(), deltas_dir=tmp_path)

    rows = db.connection.execute(
        "SELECT mod_id, entry_path, file_path FROM mod_deltas"
    ).fetchall()
    assert len(rows) >= 1
    # Entry path matches the Format 3 target
    assert rows[0][1] == "dropsetinfo.pabgb"
    # File path resolves through PAMT lookup to the PAZ
    assert rows[0][2].endswith("0.paz")


def test_all_skipped_mod_does_not_create_db_row(
        tmp_path, monkeypatch):
    """A Format 3 mod where every intent is unapplicable (e.g.,
    kori228's _list mod) MUST NOT create a mods row — the user
    would see it in the mods list as 'imported' but Apply would
    do nothing. Better: surface the skip reasons, no row created."""
    p = tmp_path / "mod.json"
    p.write_text(json.dumps({
        "modinfo": {"title": "AllSkipped"},
        "format": 3,
        "target": "dropsetinfo.pabgb",
        "intents": [
            {"entry": "X", "key": 1, "field": "drops",
             "op": "set", "new": []}
        ],
    }), encoding="utf-8")
    db = _real_db(tmp_path)
    from cdumm.engine.import_handler import import_from_natt_format_3
    result = import_from_natt_format_3(
        json_path=p, game_dir=tmp_path, db=db,
        snapshot=MagicMock(), deltas_dir=tmp_path)

    rows = db.connection.execute("SELECT id FROM mods").fetchall()
    assert len(rows) == 0, (
        f"all-skipped mod should not create a row, got {rows}")
    assert result.error  # error message surfacing skip reasons
