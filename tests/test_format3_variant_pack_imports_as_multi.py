"""A Format 3 variant pack (Fat Stacks: fat_stacks_2x … _999999 — 8 stack
sizes in one zip) must import as ONE switchable mod on the engine path,
not dead-end with a "drop it on the main window to pick a variant" error.
GitHub #191 (falobos76).

The GUI folder-variant picker already produces this outcome for drag-drop;
this pins the engine backstop for every other path (re-import from source,
a programmatic/CLI import, or a drop whose pre-extract for the picker
failed). One variant is enabled; the rest live on the mod's cog.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path


def _variant_json(mult: int) -> bytes:
    # Same two intents in every variant (equal counts → ratio 1.0, so the
    # pack detector treats them as one mod's variants); only `new` differs.
    return json.dumps({
        "modinfo": {"name": f"Fat Stacks {mult}x"},
        "format": 3,
        "target": "iteminfo.pabgb",
        "intents": [
            {"entry": "Pyeonjeon_Arrow", "key": 2200,
             "field": "max_stack_count", "op": "set", "new": 1000 * mult},
            {"entry": "Mujeon_Arrow", "key": 2201,
             "field": "max_stack_count", "op": "set", "new": 1000 * mult},
        ],
    }).encode("utf-8")


def _fat_stacks_zip(tmp_path: Path) -> Path:
    zp = tmp_path / "Fat Stacks - JSONv3 157.zip"
    with zipfile.ZipFile(zp, "w") as z:
        for m in (2, 3, 5, 10, 20, 50, 100, 999999):
            z.writestr(f"fat_stacks_{m}x.field.json", _variant_json(m))
    return zp


class _FakeSnapshot:
    def get_file_hash(self, p):
        return None

    def get_all_files(self):
        return []


def test_fat_stacks_variant_pack_imports_as_one_switchable_mod(
        tmp_path, monkeypatch, db):
    from cdumm.engine import import_handler as ih
    # Don't read a real game install for the version stamp.
    monkeypatch.setattr(
        "cdumm.engine.version_detector.detect_game_version", lambda gd: None)

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()
    zip_path = _fat_stacks_zip(tmp_path)

    result = ih.import_from_zip(
        zip_path, game_dir, db, _FakeSnapshot(), deltas_dir)

    assert result is not None
    assert result.error is None, (
        f"variant pack must import, not error: {result.error!r}")
    assert result.mod_id is not None, "a mod row must be created"

    row = db.connection.execute(
        "SELECT variants, configurable FROM mods WHERE id = ?",
        (result.mod_id,)).fetchone()
    assert row is not None
    variants = json.loads(row[0])
    assert len(variants) == 8, (
        f"all 8 stack sizes must be kept as switchable variants, "
        f"got {len(variants)}")
    enabled = [v for v in variants if v.get("enabled")]
    assert len(enabled) == 1, (
        f"exactly one variant enabled by default, got {len(enabled)}")
    assert row[1] == 1, "mod must be configurable (cog switches variant)"
