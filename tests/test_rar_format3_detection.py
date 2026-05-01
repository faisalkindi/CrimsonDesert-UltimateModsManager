"""RAR / 7z imports were missing the Format 3 detection that ZIP
imports already had. The shared helper `_import_from_extracted`
(used by both `import_from_rar` and `import_from_7z`) only checked
Crimson Browser, loose-file mod, and JSON byte-patch v2 formats.
The same Format 3 JSON dropped as a ZIP imported fine; dropped as
a RAR or 7z it errored with "no recognized mod format".

Source: Faisal's Can It Stack JSON V3 (Lovexvirus007's mod, Nexus
2180) RAR test 2026-05-01. Mod is `format=3, target=iteminfo.pabgb,
field=max_stack_count`, 1827 primitive intents.

Fix: add Format 3 (+ OG_ XML + plain XML drop) detection to
`_import_from_extracted` so the format coverage matches ZIP.
"""
from __future__ import annotations
from pathlib import Path
import json
import zipfile

import pytest


def _make_format3_zip(zip_path: Path, target: str = "iteminfo.pabgb",
                      field: str = "max_stack_count") -> None:
    """Build a synthetic Format 3 mod ZIP."""
    mod_data = {
        "modinfo": {"title": "Test F3 Mod", "version": "1.0",
                    "author": "test"},
        "format": 3,
        "target": target,
        "intents": [
            {"entry": "Money_Copper", "key": 1, "field": field,
             "op": "set", "new": 9999999},
        ],
    }
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("mod.json", json.dumps(mod_data).encode("utf-8"))


def test_extracted_format3_via_helper_detects(tmp_path, monkeypatch):
    """The shared `_import_from_extracted` helper (used by RAR and 7z
    imports) must route Format 3 JSONs to `import_from_natt_format_3`
    instead of falling through to the generic 'no recognized format'
    error.

    We can't easily simulate a real RAR without a 7z install, but we
    CAN call `_import_from_extracted` directly with an extracted dir
    that contains a single Format 3 JSON. If RAR/7z support is missing
    Format 3, this test fails the same way the user's drop did.
    """
    from cdumm.engine import import_handler
    from cdumm.engine.import_handler import _import_from_extracted

    # Stub `import_from_natt_format_3` to capture whether the helper
    # routed to it. We only care about the routing decision.
    captured = {"called": False, "json_path": None}

    def _fake_f3_import(*, json_path, game_dir, db, snapshot, deltas_dir,
                        existing_mod_id=None, modinfo=None):
        captured["called"] = True
        captured["json_path"] = json_path
        from cdumm.engine.import_handler import ModImportResult
        r = ModImportResult("test")
        r.mod_id = 99
        return r

    monkeypatch.setattr(import_handler, "import_from_natt_format_3",
                        _fake_f3_import)

    # Build an extracted dir with a single Format 3 JSON
    extracted = tmp_path / "extract"
    extracted.mkdir()
    f3_data = {
        "modinfo": {"title": "Can It Stack", "version": "1.0",
                    "author": "Lovexvirus007"},
        "format": 3,
        "target": "iteminfo.pabgb",
        "intents": [{"entry": "Money_Copper", "key": 1,
                     "field": "max_stack_count", "op": "set",
                     "new": 9999999}],
    }
    (extracted / "CanItStack.jsonv3.json").write_text(
        json.dumps(f3_data), encoding="utf-8")

    # Minimal DB + snapshot for the helper signature
    from cdumm.storage.database import Database
    from cdumm.engine.snapshot_manager import SnapshotManager
    db = Database(tmp_path / "test.db")
    db.initialize()
    snapshot = SnapshotManager(db)
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()

    result = _import_from_extracted(
        extracted, game_dir, db, snapshot, deltas_dir,
        mod_name="Can It Stack",
    )

    assert captured["called"], (
        "_import_from_extracted should have routed the Format 3 JSON "
        "to import_from_natt_format_3, but the call never happened. "
        "RAR/7z paths are missing Format 3 detection."
    )
    assert result.error is None, (
        f"Expected clean import, got error: {result.error!r}"
    )
