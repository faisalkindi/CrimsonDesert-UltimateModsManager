"""End-to-end regression: ``import_from_zip`` on a JSON-byte-patch +
sibling-raw-file mod (mod 1543 layout) must register BOTH halves
under the same mod row.

Bug 2026-05-10 (niyaruza, goodygoosey, IIIF0RERUNNER on Nexus):
Even on v3.2.15 / v3.2.16 master, the armor portion of crewny23's
"ALL Weapons and Armor Fully Usable on Every Single Character"
(mod 1543) does not apply. The mod ships:

  * Kliff_Damiane_Runtimepackages.json — JSON byte-patch hitting
    gamedata/characterinfo.pabgb (weapons / movesets /
    walking animation).
  * UniEquip - 1.05.01 Update/files/gamedata/binary__/client/bin/
    iteminfo.pabgb (and three more loose files) — raw replacements
    at the engine's inner path layout. Drives armor equipping.

The unit test ``test_json_plus_raw_file_sibling_import.py`` covers
the helper ``_persist_raw_match_deltas`` in isolation and passes,
but the user-visible failure happens further out in the call
graph. This test exercises the full ``import_from_zip`` entry
point on a synthetic ZIP that mirrors mod 1543's layout and
asserts that BOTH the JSON byte-patch result and the sibling
raw-file delta land in the same ModImportResult.
"""
from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass
class _FakePazEntry:
    path: str
    paz_file: str
    offset: int = 100
    comp_size: int = 200
    orig_size: int = 200
    compression_type: int = 0
    flags: int = 0
    paz_index: int = 0
    encrypted: bool = False


def _build_mod_1543_zip(tmp_path: Path) -> Path:
    """Build a synthetic ZIP that mirrors crewny23's mod 1543 layout.

    Root has a Format 1 JSON byte-patch hitting
    ``gamedata/characterinfo.pabgb``. A sibling subfolder
    ``UniEquip - 1.05.01 Update/files/gamedata/binary__/client/bin/``
    ships a loose ``iteminfo.pabgb`` at the engine's inner path
    layout.
    """
    json_payload = {
        "name": "Kliff_Damiane_Runtimepackages",
        "patches": [
            {
                "game_file": "gamedata/characterinfo.pabgb",
                "changes": [
                    {
                        "label": "weapons_swap",
                        "offset": 100,
                        "original": "DEADBEEF",
                        "patched": "CAFEBABE",
                    }
                ],
            }
        ],
    }

    zip_path = tmp_path / "mod_1543_synthetic.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(
            "Kliff_Damiane_Runtimepackages.json",
            json.dumps(json_payload),
        )
        # Sibling raw-file replacement at the engine's inner path
        zf.writestr(
            "UniEquip - 1.05.01 Update/files/gamedata/"
            "binary__/client/bin/iteminfo.pabgb",
            b"new iteminfo bytes from mod 1543",
        )
    return zip_path


def test_zip_with_json_plus_sibling_raw_file_imports_both(
        tmp_path, monkeypatch, db):
    """import_from_zip on the mod 1543 layout must register BOTH the
    JSON byte-patch delta AND the sibling raw-file delta under the
    same mod row."""
    from cdumm.engine import import_handler as ih
    from cdumm.engine import json_patch_handler as jph

    # Set up a synthetic game dir so import_staging_dir has somewhere
    # to write its working folder.
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()

    zip_path = _build_mod_1543_zip(tmp_path)

    # Fake PazEntry for the iteminfo sibling file. Real PAMT lookup
    # would resolve "gamedata/binary__/client/bin/iteminfo.pabgb"
    # to a row in 0008/0.pamt; the synthetic version gives the
    # importer's _detect_raw_file_replacements_via_pamt helper a
    # concrete entry to attach the delta to.
    iteminfo_rel = "gamedata/binary__/client/bin/iteminfo.pabgb"
    fake_entry = _FakePazEntry(
        path=iteminfo_rel,
        paz_file=str(game_dir / "0008" / "0.paz"),
    )

    def fake_find_pamt_entry(game_file: str, gd):
        # Resolve only the iteminfo sibling. The JSON byte-patch's
        # gamedata/characterinfo.pabgb is handled inside
        # import_json_as_entr — we stub that whole function below
        # so we don't need to model real PAZ extraction.
        if game_file.lower() == iteminfo_rel.lower():
            return fake_entry
        return None

    monkeypatch.setattr(ih, "_find_pamt_entry", fake_find_pamt_entry,
                        raising=False)
    monkeypatch.setattr(jph, "_find_pamt_entry", fake_find_pamt_entry)

    # Stub the JSON byte-patch importer so the test doesn't need a
    # real PAZ file for gamedata/characterinfo.pabgb. Insert a real
    # mod row + delta row so downstream code that reads the DB
    # finds something coherent. Returns the same dict shape
    # import_from_zip's JSON branch consumes.
    def fake_import_json_as_entr(
        patch_data, game_dir, db, deltas_dir, mod_name,
        existing_mod_id=None, modinfo=None, config=None
    ):
        cur = db.connection.execute(
            "INSERT INTO mods (name, mod_type, priority) "
            "VALUES (?, ?, ?)",
            (mod_name, "paz", 1),
        )
        new_mod_id = cur.lastrowid
        # Record one synthetic ENTR delta for the JSON's target file.
        delta_dir = deltas_dir / str(new_mod_id)
        delta_dir.mkdir(parents=True, exist_ok=True)
        delta_path = delta_dir / "gamedata_characterinfo.pabgb.entr"
        delta_path.write_bytes(b"json-derived ENTR delta blob")
        db.connection.execute(
            "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
            "byte_start, byte_end, entry_path) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (new_mod_id, "0001/0.paz", str(delta_path),
             0, 100, "gamedata/characterinfo.pabgb"),
        )
        return {
            "mod_id": new_mod_id,
            "changed_files": ["gamedata/characterinfo.pabgb"],
            "name": mod_name,
        }

    monkeypatch.setattr(
        "cdumm.engine.json_patch_handler.import_json_as_entr",
        fake_import_json_as_entr,
    )

    # Stub version_detector so detect_game_version doesn't try to
    # read a real game install.
    monkeypatch.setattr(
        "cdumm.engine.version_detector.detect_game_version",
        lambda gd: None,
    )

    # Stub save_entry_delta so the sibling raw-file persist path
    # doesn't need a real on-disk PAZ layout.
    saved_calls: list[tuple] = []

    def fake_save_entry_delta(content, metadata, delta_path):
        Path(delta_path).parent.mkdir(parents=True, exist_ok=True)
        Path(delta_path).write_bytes(b"stub")
        saved_calls.append((bytes(content), dict(metadata),
                            str(delta_path)))

    monkeypatch.setattr(
        "cdumm.engine.delta_engine.save_entry_delta",
        fake_save_entry_delta,
    )

    # Stub _store_json_patches: it normally writes the json_patches
    # table; not relevant to the sibling-import contract under test.
    monkeypatch.setattr(ih, "_store_json_patches",
                        lambda db, result, jp_data, gd: None)

    # SnapshotManager: the JSON branch happens before any snapshot
    # use in import_from_zip, so a thin stub is enough.
    class _FakeSnapshot:
        def get_file_hash(self, p):
            return None
        def get_all_files(self):
            return []
    snapshot = _FakeSnapshot()

    # Run the importer
    result = ih.import_from_zip(
        zip_path, game_dir, db, snapshot, deltas_dir,
    )

    assert result is not None
    assert result.error is None, (
        f"import unexpectedly failed: {result.error!r}"
    )

    # The whole point of v3.2.15's "fold sibling raw files into the
    # JSON mod" fix: changed_files MUST include both halves.
    cf = list(result.changed_files or [])
    assert "gamedata/characterinfo.pabgb" in cf, (
        f"JSON byte-patch's target missing from changed_files: {cf!r}"
    )
    assert iteminfo_rel in cf, (
        f"REGRESSION: sibling raw-file iteminfo.pabgb was silently "
        f"dropped, only got changed_files={cf!r}. The v3.2.15 "
        f"changelog promised this layout imports as one mod with "
        f"both halves; users on Nexus 2026-05-10 still report the "
        f"armor portion never applies."
    )

    # The sibling raw-file persistence path must have written its
    # ENTR delta and inserted a mod_deltas row keyed on the SAME
    # mod_id the JSON branch created.
    iteminfo_saves = [
        call for call in saved_calls
        if call[1].get("entry_path") == iteminfo_rel
    ]
    assert len(iteminfo_saves) == 1, (
        f"expected exactly one save_entry_delta call for "
        f"{iteminfo_rel}, got {len(iteminfo_saves)}: "
        f"{[c[1] for c in saved_calls]!r}"
    )

    # Verify both deltas live under the same mod row
    rows = db.connection.execute(
        "SELECT mod_id, entry_path FROM mod_deltas "
        "ORDER BY entry_path"
    ).fetchall()
    mod_ids = {r[0] for r in rows}
    assert len(mod_ids) == 1, (
        f"expected both deltas under one mod_id, got {mod_ids!r} "
        f"with rows {rows!r}"
    )
    entry_paths = {r[1] for r in rows}
    assert entry_paths == {
        "gamedata/characterinfo.pabgb",
        iteminfo_rel,
    }, (
        f"expected both entry_paths under the JSON mod, got "
        f"{entry_paths!r}"
    )


def test_folder_with_json_plus_sibling_raw_file_imports_both(
        tmp_path, monkeypatch, db):
    """import_from_folder on the mod 1543 layout — drag-drop of the
    EXTRACTED folder, not the zip — must register BOTH the JSON
    byte-patch delta AND the sibling raw-file delta under the same
    mod row.

    REGRESSION 2026-05-10: ``import_from_zip``'s JSON branch (line
    2947) was patched in 0442219 to call
    ``_detect_raw_file_replacements_via_pamt`` after a successful
    JSON byte-patch import. ``import_from_folder``'s JSON branch
    (line 3420) was NOT patched and silently drops the sibling
    raw-file replacements. Users who drag-drop the EXTRACTED folder
    of mod 1543 lose the armor portion entirely.
    """
    from cdumm.engine import import_handler as ih
    from cdumm.engine import json_patch_handler as jph

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()

    # Build the EXTRACTED folder (no zip wrapper) — what the user
    # drag-drops after unzipping mod 1543.
    drop_folder = tmp_path / "Kliff_All_Weapons_And_Armor"
    drop_folder.mkdir()
    json_payload = {
        "name": "Kliff_Damiane_Runtimepackages",
        "patches": [
            {
                "game_file": "gamedata/characterinfo.pabgb",
                "changes": [
                    {
                        "label": "weapons_swap",
                        "offset": 100,
                        "original": "DEADBEEF",
                        "patched": "CAFEBABE",
                    }
                ],
            }
        ],
    }
    (drop_folder / "Kliff_Damiane_Runtimepackages.json").write_text(
        json.dumps(json_payload), encoding="utf-8")
    iteminfo_dir = (drop_folder / "UniEquip - 1.05.01 Update"
                    / "files" / "gamedata" / "binary__"
                    / "client" / "bin")
    iteminfo_dir.mkdir(parents=True)
    (iteminfo_dir / "iteminfo.pabgb").write_bytes(
        b"new iteminfo bytes from mod 1543")

    iteminfo_rel = "gamedata/binary__/client/bin/iteminfo.pabgb"
    fake_entry = _FakePazEntry(
        path=iteminfo_rel,
        paz_file=str(game_dir / "0008" / "0.paz"),
    )

    def fake_find_pamt_entry(game_file: str, gd):
        if game_file.lower() == iteminfo_rel.lower():
            return fake_entry
        return None

    monkeypatch.setattr(ih, "_find_pamt_entry", fake_find_pamt_entry,
                        raising=False)
    monkeypatch.setattr(jph, "_find_pamt_entry", fake_find_pamt_entry)

    def fake_import_json_as_entr(
        patch_data, game_dir, db, deltas_dir, mod_name,
        existing_mod_id=None, modinfo=None, config=None
    ):
        cur = db.connection.execute(
            "INSERT INTO mods (name, mod_type, priority) "
            "VALUES (?, ?, ?)",
            (mod_name, "paz", 1),
        )
        new_mod_id = cur.lastrowid
        delta_dir = deltas_dir / str(new_mod_id)
        delta_dir.mkdir(parents=True, exist_ok=True)
        delta_path = delta_dir / "gamedata_characterinfo.pabgb.entr"
        delta_path.write_bytes(b"json-derived ENTR delta blob")
        db.connection.execute(
            "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
            "byte_start, byte_end, entry_path) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (new_mod_id, "0001/0.paz", str(delta_path),
             0, 100, "gamedata/characterinfo.pabgb"),
        )
        return {
            "mod_id": new_mod_id,
            "changed_files": ["gamedata/characterinfo.pabgb"],
            "name": mod_name,
        }

    monkeypatch.setattr(
        "cdumm.engine.json_patch_handler.import_json_as_entr",
        fake_import_json_as_entr,
    )
    monkeypatch.setattr(
        "cdumm.engine.version_detector.detect_game_version",
        lambda gd: None,
    )

    saved_calls: list[tuple] = []

    def fake_save_entry_delta(content, metadata, delta_path):
        Path(delta_path).parent.mkdir(parents=True, exist_ok=True)
        Path(delta_path).write_bytes(b"stub")
        saved_calls.append((bytes(content), dict(metadata),
                            str(delta_path)))

    monkeypatch.setattr(
        "cdumm.engine.delta_engine.save_entry_delta",
        fake_save_entry_delta,
    )

    monkeypatch.setattr(ih, "_store_json_patches",
                        lambda db, result, jp_data, gd: None)

    class _FakeSnapshot:
        def get_file_hash(self, p):
            return None
        def get_all_files(self):
            return []
    snapshot = _FakeSnapshot()

    result = ih.import_from_folder(
        drop_folder, game_dir, db, snapshot, deltas_dir,
    )

    assert result is not None
    assert result.error is None, (
        f"import unexpectedly failed: {result.error!r}"
    )

    cf = list(result.changed_files or [])
    assert "gamedata/characterinfo.pabgb" in cf, (
        f"JSON byte-patch's target missing from changed_files: {cf!r}"
    )
    assert iteminfo_rel in cf, (
        f"REGRESSION: import_from_folder silently dropped the "
        f"sibling raw-file iteminfo.pabgb. The JSON byte-patch "
        f"branch in import_from_folder (line ~3420 in "
        f"src/cdumm/engine/import_handler.py) returns without "
        f"calling _detect_raw_file_replacements_via_pamt the way "
        f"import_from_zip does. Got changed_files={cf!r}."
    )
