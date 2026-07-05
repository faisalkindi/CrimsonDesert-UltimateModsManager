"""Mod-maker foundation (Game Data tab): tests for
``cdumm.engine.format3_builder`` — turning staged field edits into a
Format 3 mod, exporting it, and importing it through the existing pipeline.

The build/import round trip reuses the same real-sqlite + mocked-PAMT
harness as ``test_format3_import_wiring`` so it exercises the actual
``import_from_natt_format_3`` path, not a stub.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cdumm.engine.format3_builder import (
    FieldEdit, build_format3_json, write_field_json, create_mod_from_edits,
)


# ── build_format3_json ──────────────────────────────────────────────

def test_build_shapes_single_target_intents():
    edits = [
        FieldEdit(target="wantedinfo.pabgb", entry="Wanted_A", key=1,
                  field="_increasePrice", new=5000, old=1500),
        FieldEdit(target="wantedinfo.pabgb", entry="Wanted_B", key=2,
                  field="_increasePrice", new=9999),
    ]
    mod = build_format3_json(edits, title="Rich Bounties", author="me")
    assert mod["format"] == 3
    assert mod["target"] == "wantedinfo.pabgb"
    assert mod["modinfo"]["title"] == "Rich Bounties"
    assert mod["modinfo"]["author"] == "me"
    assert len(mod["intents"]) == 2
    assert mod["intents"][0] == {
        "entry": "Wanted_A", "key": 1, "field": "_increasePrice",
        "op": "set", "new": 5000}
    # `old` is display-only and must NOT leak into the mod
    assert "old" not in mod["intents"][0]


def test_build_rejects_empty():
    with pytest.raises(ValueError):
        build_format3_json([], title="x")


def test_build_rejects_mixed_targets():
    with pytest.raises(ValueError):
        build_format3_json([
            FieldEdit("a.pabgb", "E", 1, "f", 1),
            FieldEdit("b.pabgb", "E", 2, "f", 2),
        ], title="x")


def test_build_key_coerced_to_int():
    mod = build_format3_json(
        [FieldEdit("t.pabgb", "E", "42", "f", 1)], title="x")
    assert mod["intents"][0]["key"] == 42
    assert isinstance(mod["intents"][0]["key"], int)


# ── write_field_json (export primitive) ─────────────────────────────

def test_write_field_json_roundtrips(tmp_path):
    mod = build_format3_json(
        [FieldEdit("wantedinfo.pabgb", "W", 1, "_increasePrice", 7000)],
        title="Export Me")
    out = write_field_json(mod, tmp_path / "sub" / "mymod.field.json")
    assert out.exists()
    assert json.loads(out.read_text(encoding="utf-8")) == mod


# ── create_mod_from_edits (build -> import round trip) ───────────────

def _real_db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.execute(
        "CREATE TABLE mods (id INTEGER PRIMARY KEY, name TEXT, mod_type TEXT, "
        "enabled INTEGER DEFAULT 0, json_source TEXT, priority INTEGER, "
        "author TEXT, version TEXT, description TEXT, game_version_hash TEXT, "
        "disabled_patches TEXT)")
    conn.execute(
        "CREATE TABLE mod_deltas (id INTEGER PRIMARY KEY, mod_id INTEGER, "
        "file_path TEXT, delta_path TEXT, byte_start INTEGER, byte_end "
        "INTEGER, entry_path TEXT, kind TEXT)")
    conn.execute(
        "CREATE TABLE mod_config (mod_id INTEGER PRIMARY KEY, "
        "custom_values TEXT)")
    conn.commit()

    class _W:
        def __init__(self, c):
            self.connection = c
    return _W(conn)


def test_create_mod_from_edits_imports_and_persists(tmp_path, monkeypatch):
    """A supported edit builds a Format 3 mod, writes a .field.json, and
    imports it: a mods row lands with json_source pointing at a real file
    that really is Format 3 with our intent."""
    fake_entry = MagicMock()
    fake_entry.paz_file = "/fake/0008/0.paz"
    fake_entry.path = "dropsetinfo.pabgb"
    fake_entry.offset = 100
    fake_entry.comp_size = 50
    monkeypatch.setattr(
        "cdumm.engine.json_patch_handler._find_pamt_entry",
        lambda gf, gd: fake_entry)

    db = _real_db(tmp_path)
    edits = [FieldEdit(target="dropsetinfo.pabgb", entry="DropSet_X",
                       key=100000, field="_dropTagNameHash", new=1234)]

    res = create_mod_from_edits(
        edits, title="Maker Test Mod", game_dir=tmp_path, db=db,
        snapshot=MagicMock(), deltas_dir=tmp_path)

    assert res.error is None, res.error
    assert res.mod_id is not None
    rows = db.connection.execute(
        "SELECT name, json_source FROM mods").fetchall()
    assert len(rows) == 1
    name, json_source = rows[0]
    assert "Maker Test Mod" in name
    assert json_source and Path(json_source).exists()
    saved = json.loads(Path(json_source).read_text(encoding="utf-8"))
    assert saved["format"] == 3
    assert saved["intents"][0]["field"] == "_dropTagNameHash"


def test_create_mod_from_edits_surfaces_unknown_table(tmp_path):
    """An edit to a table with no schema comes back as an error via the
    same validate path a hand-authored mod hits — not a crash, and no row."""
    db = _real_db(tmp_path)
    res = create_mod_from_edits(
        [FieldEdit("totallyfaketable.pabgb", "X", 1, "y", 42)],
        title="Bad", game_dir=tmp_path, db=db,
        snapshot=MagicMock(), deltas_dir=tmp_path)
    assert res.error
    assert ("schema" in res.error.lower()
            or "totallyfaketable" in res.error.lower())
    assert db.connection.execute("SELECT COUNT(*) FROM mods").fetchone()[0] == 0
