"""Regression: NattKh's GameMods JSON Format 3 (field-names + intents)
must be DETECTED so the importer can show a clear 'coming soon'
error instead of falling through to 'unsupported file format'.

Full Format 3 import support is planned for a future release. Until
then this detector + friendly stub gives users a specific message
plus a workaround.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cdumm.engine.json_patch_handler import is_natt_format_3
from cdumm.engine.import_handler import detect_format


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def f3_file(tmp_path: Path) -> Path:
    """A minimal valid Format 3 file."""
    p = tmp_path / "natt_mod.json"
    _write_json(p, {
        "modinfo": {"title": "Sample", "version": "1.0", "author": "NattKh"},
        "format": 3,
        "target": "dropsetinfo.pabgb",
        "intents": [
            {
                "entry": "DropSet_Faction_Graymane",
                "key": 175001,
                "field": "drops",
                "op": "set",
                "new": [{"item_key": 30010, "rates": 1000000}],
            }
        ],
    })
    return p


def test_detector_recognises_real_format_3_file(f3_file: Path):
    assert is_natt_format_3(f3_file) is True


def test_detector_rejects_existing_offset_based_json(tmp_path: Path):
    """The legacy CDUMM/JSON-MM offset-based format must NOT be
    misidentified as Format 3 — it has a 'patches' array, not
    'intents', and no 'format': 3 marker."""
    p = tmp_path / "legacy.json"
    _write_json(p, {
        "name": "Legacy mod",
        "version": "1.0",
        "patches": [{
            "game_file": "skill.pabgb",
            "changes": [{"offset": 100, "original": "00", "patched": "FF"}],
        }],
    })
    assert is_natt_format_3(p) is False


def test_detector_rejects_random_json(tmp_path: Path):
    p = tmp_path / "random.json"
    _write_json(p, {"hello": "world"})
    assert is_natt_format_3(p) is False


def test_detector_rejects_format_3_without_intents(tmp_path: Path):
    """A file that says format: 3 but has no intents list isn't a
    valid Format 3 mod — refuse it."""
    p = tmp_path / "bad.json"
    _write_json(p, {"format": 3, "target": "x.pabgb"})
    assert is_natt_format_3(p) is False


def test_detector_rejects_non_json_file(tmp_path: Path):
    p = tmp_path / "not_json.txt"
    p.write_text("hello", encoding="utf-8")
    assert is_natt_format_3(p) is False


def test_detector_rejects_directory(tmp_path: Path):
    assert is_natt_format_3(tmp_path) is False


def test_detect_format_returns_natt_format_3_for_format_3_file(f3_file: Path):
    """The top-level dispatch in import_handler.detect_format must
    return the new 'natt_format_3' string for Format 3 files so the
    worker can route them to the friendly stub instead of returning
    'unknown'."""
    assert detect_format(f3_file) == "natt_format_3"


def test_friendly_stub_returns_clear_error_message(tmp_path: Path):
    """When NONE of a Format 3 mod's intents have a writer (no
    schema for the table AND no list writer registered), the import
    stub must return a ModImportResult with an error that names
    Format 3 explicitly AND points to the workaround (offset-based
    version of the same mod).

    Use a synthetic target with no schema so every intent skips.
    `dropsetinfo.drops` is now supported via the dropset_writer, so
    we use an unrecognised table to drive the all-skipped path."""
    from cdumm.engine.import_handler import import_from_natt_format_3
    from cdumm.engine.snapshot_manager import SnapshotManager
    from cdumm.storage.database import Database

    # Synthetic Format 3 file targeting a table CDUMM has no schema for.
    f3_file = tmp_path / "unknown_table_mod.json"
    _write_json(f3_file, {
        "modinfo": {"title": "Sample", "version": "1.0"},
        "format": 3,
        "target": "totally_made_up_table.pabgb",
        "intents": [{
            "entry": "X", "key": 1, "field": "anything",
            "op": "set", "new": 42,
        }],
    })

    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    snapshot = SnapshotManager(db)
    deltas = tmp_path / "deltas"
    game_dir = tmp_path / "game"
    game_dir.mkdir()

    result = import_from_natt_format_3(
        f3_file, game_dir, db, snapshot, deltas)
    db.close()
    assert result.error, "stub must populate result.error"
    err = result.error.lower()
    assert "format 3" in err or "field-names" in err, (
        "error message must mention 'Format 3' or 'field-names' so "
        "users searching the error find issue #41 + the roadmap note")
    assert "workaround" in err or "offset-based" in err, (
        "error message should mention the workaround (the older "
        "offset-based version of the same mod still works)")
