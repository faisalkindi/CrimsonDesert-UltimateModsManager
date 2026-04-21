"""#SirFapZalot regression on v3.1.3: packed (zip-imported) JSON mods stop
applying.

v3.1.3 taught ``import_json_as_entr`` to archive the source JSON and set
``mods.json_source``. At apply time, ``_get_file_deltas`` then hard-skips
the mod's ENTR delta rows assuming the Phase 1a aggregator will cover
them (``if json_source and entry_path: continue``). But if the aggregator
silently drops the mod's patch — bytes already match vanilla, patch
mismatch on a non-data-table file, extraction failure, missing
json_source on disk — the mod's ENTR delta is ALSO skipped and the
mod's changes never reach the game.

Workaround the user found: drop the raw .json file (goes through
``import_json_fast`` which doesn't write ENTR deltas, so there's
nothing to skip even if aggregator drops).

Correct behaviour: the ENTR delta must remain in ``file_deltas`` so
Phase 1's compose step routes it into the overlay when the aggregator
doesn't cover the target. When the aggregator DOES cover the target,
``_merge_same_target_overlay_entries`` collapses the duplicate
priority-wins.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from cdumm.engine.apply_engine import ApplyWorker, collect_enabled_json_targets
from cdumm.storage.database import Database


def _prepare_packed_mod_db(tmp_path: Path, source_json_exists: bool):
    """Simulate ``import_json_as_entr`` having stored a packed JSON mod:
    mod row has ``json_source`` pointing at a source.json, ``mod_deltas``
    has an entry_path row pointing at an ENTR delta file."""
    delta_dir = tmp_path / "deltas" / "1"
    delta_dir.mkdir(parents=True, exist_ok=True)

    source_json_path = delta_dir / "source.json"
    if source_json_exists:
        source_json_path.write_text(
            '{"patches":[{"game_file":"gamedata/input_xinput_controller.pabgs",'
            '"changes":[{"offset":100,"original":"00","patched":"FF"}]}]}',
            encoding="utf-8",
        )

    entr_path = delta_dir / "0008_0.pabgs.entr"
    entr_path.write_bytes(b"ENTR\x00\x00\x00\x01" + b"\x00" * 32 + b"FAKE_ENTR_BODY")

    db = Database(tmp_path / "test.db")
    db.initialize()
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled, priority, json_source) "
        "VALUES (1, 'Improved Controller', 'paz', 1, 1, ?)",
        (str(source_json_path),),
    )
    db.connection.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, "
        "byte_end, entry_path) VALUES (1, '0008/0.paz', ?, 100, 200, "
        "'gamedata/input_xinput_controller.pabgs')",
        (str(entr_path),),
    )
    db.connection.commit()
    return db


def _get_file_deltas_via_worker(db: Database, tmp_path: Path) -> dict:
    """Spin up a minimal ApplyWorker just to hit _get_file_deltas."""
    game_dir = tmp_path / "game"
    vanilla_dir = tmp_path / "vanilla"
    game_dir.mkdir(exist_ok=True)
    vanilla_dir.mkdir(exist_ok=True)
    worker = ApplyWorker(game_dir, vanilla_dir, db.db_path)
    worker._db = db
    return worker._get_file_deltas()


def test_entr_delta_preserved_when_json_source_path_missing(tmp_path: Path) -> None:
    """If json_source points at a file that no longer exists on disk
    (disk was full at import, user nuked ``CDMods/deltas``, etc.) the
    aggregator can't read it — so the ENTR delta is the only chance
    to apply the mod. It must stay in file_deltas."""
    db = _prepare_packed_mod_db(tmp_path, source_json_exists=False)
    try:
        file_deltas = _get_file_deltas_via_worker(db, tmp_path)
        assert "0008/0.paz" in file_deltas, (
            "ENTR delta should stay in file_deltas when json_source "
            "file is missing — aggregator can't cover it, skipping "
            "the ENTR row loses the mod entirely")
        deltas = file_deltas["0008/0.paz"]
        assert any(
            d["entry_path"] == "gamedata/input_xinput_controller.pabgs"
            for d in deltas
        ), f"Expected entry_path delta in {deltas}"
    finally:
        db.close()


def test_entr_delta_preserved_when_json_source_exists(tmp_path: Path) -> None:
    """Even when json_source exists, the ENTR delta must stay in
    file_deltas as a safety net. The aggregator's
    process_json_patches_for_overlay can silently drop a patch (no-op
    identity check at json_patch_handler.py:1998, non-data-table
    mismatch warning at line 1960, extraction exception at line 1904).
    All those paths skip the patch without surfacing it upstream.

    When the aggregator DOES cover the entry, the resulting overlay
    entry duplicates with the ENTR's overlay entry — that's fine,
    _merge_same_target_overlay_entries collapses them priority-wins.
    """
    db = _prepare_packed_mod_db(tmp_path, source_json_exists=True)
    try:
        file_deltas = _get_file_deltas_via_worker(db, tmp_path)
        assert "0008/0.paz" in file_deltas, (
            "ENTR delta must remain available as safety net even when "
            "json_source exists; aggregator can still silently drop a "
            "target at process_json_patches_for_overlay time and the "
            "merge step needs both entries to pick a winner")
        deltas = file_deltas["0008/0.paz"]
        assert any(
            d["entry_path"] == "gamedata/input_xinput_controller.pabgs"
            for d in deltas
        )
    finally:
        db.close()


def test_collect_enabled_json_targets_reads_json_source_as_path(
    tmp_path: Path,
) -> None:
    """``collect_enabled_json_targets`` stores a FILE PATH in json_source
    (that's what ``import_json_as_entr`` and ``import_json_fast`` both
    write). The original implementation did ``_json.loads(json_source)``
    directly which silently failed on any path string — masking whether
    a JSON mod targeted a given PAZ-dir override.

    With the path-aware read, a json_source-only mod (no mod_deltas row
    with entry_path) still contributes its targets."""
    src = tmp_path / "mod.json"
    src.write_text(
        '{"patches":[{"game_file":"gamedata/iteminfo.pabgb","changes":[]}]}',
        encoding="utf-8",
    )
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE mods (id INTEGER PRIMARY KEY, name TEXT, "
        "mod_type TEXT, enabled INTEGER, priority INTEGER, json_source TEXT)"
    )
    con.execute(
        "CREATE TABLE mod_deltas (id INTEGER PRIMARY KEY, mod_id INTEGER, "
        "file_path TEXT, delta_path TEXT, entry_path TEXT)"
    )
    con.execute(
        "INSERT INTO mods VALUES (1, 'ExtraSockets', 'paz', 1, 1, ?)",
        (str(src),),
    )
    con.commit()

    class _DbShim:
        connection = con

    targets = collect_enabled_json_targets(_DbShim())
    assert "gamedata/iteminfo.pabgb" in targets, (
        "json_source holds a PATH — loader must read the file, not "
        "parse the path string as JSON")
