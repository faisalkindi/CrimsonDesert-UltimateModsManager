"""Surface multi-file mod's per-file skips to the user.

Round-7 systematic-debugging finding: my v3.2.5 fix to
``import_json_as_entr`` (multi-file mods skip bad files instead of
rejecting whole mod) added a ``skipped_files`` list to the result
dict — but ``import_handler.py`` never reads it. The user gets a
clean "imported successfully" with no indication that 3 of their
116 files silently won't apply.

The right channel exists: ``ModImportResult.info`` is for non-fatal
diagnostics that surface as a yellow InfoBar (already wired up at
import_handler.py:246-252). Just need to populate it from
skipped_files when the import succeeded but some files were
skipped.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


def test_partial_skip_populates_info_field_on_result():
    """A multi-file mod where most files apply but one fails must
    produce a ModImportResult with non-None ``info`` summarizing
    the skipped file(s)."""
    from cdumm.engine.import_handler import import_from_zip
    from cdumm.engine.snapshot_manager import SnapshotManager
    from cdumm.storage.database import Database
    from cdumm.archive.paz_parse import parse_pamt
    from cdumm.engine.json_patch_handler import _extract_from_paz
    import zipfile, json

    game_dir = Path(r'E:\SteamLibrary\steamapps\common\Crimson Desert')
    if not (game_dir / '0008' / '0.pamt').exists():
        pytest.skip("Live game dir not available")

    pamt = game_dir / '0008' / '0.pamt'
    entries = parse_pamt(str(pamt), paz_dir=str(pamt.parent))
    iteminfo = next(e for e in entries
                    if e.path == 'gamedata/iteminfo.pabgb')
    iteminfo_bytes = bytes(_extract_from_paz(iteminfo))
    file_a_offset = 100
    file_a_orig = iteminfo_bytes[file_a_offset:file_a_offset+4].hex()

    patch_data = {
        "modinfo": {"title": "PartialSkipTest", "version": "1.0"},
        "patches": [
            {
                "game_file": "gamedata/iteminfo.pabgb",
                "changes": [
                    {"offset": file_a_offset, "original": file_a_orig,
                     "patched": "ffffffff", "label": "good"},
                ],
            },
            {
                "game_file": "gamedata/vehicleinfo.pabgb",
                "changes": [
                    {"offset": 0,
                     "original": "deadbeefcafebabedeadbeefcafebabe",
                     "patched": "00000000000000000000000000000000",
                     "label": "fails"},
                ],
            },
        ],
    }

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        td = Path(td)
        zip_path = td / "test.zip"
        json_str = json.dumps(patch_data)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("PartialSkipTest.json", json_str)

        db = Database(td / "test.db")
        db.initialize()
        snapshot = SnapshotManager(game_dir / "CDMods" / "snapshot.json")
        deltas = td / "deltas"
        deltas.mkdir()

        result = import_from_zip(zip_path, game_dir, db, snapshot, deltas)
        db.connection.close()

    assert result is not None
    assert result.error is None, (
        f"Mod with one good file should NOT error. error={result.error!r}")
    assert result.info is not None and result.info.strip(), (
        f"Mod with skipped files MUST populate info. "
        f"info={result.info!r}")
    info_lower = result.info.lower()
    # The message should name what happened so the user can act.
    assert "skipped" in info_lower or "1 file" in info_lower, (
        f"info should mention the skip count. info={result.info!r}")
    assert "vehicleinfo" in info_lower, (
        f"info should name the skipped file. info={result.info!r}")


def test_clean_import_no_skipped_files_keeps_info_none():
    """Regression guard: a mod where ALL files apply cleanly must
    not get a misleading 'X files skipped' info populated."""
    from cdumm.engine.import_handler import import_from_zip
    from cdumm.engine.snapshot_manager import SnapshotManager
    from cdumm.storage.database import Database
    from cdumm.archive.paz_parse import parse_pamt
    from cdumm.engine.json_patch_handler import _extract_from_paz
    import zipfile, json

    game_dir = Path(r'E:\SteamLibrary\steamapps\common\Crimson Desert')
    if not (game_dir / '0008' / '0.pamt').exists():
        pytest.skip("Live game dir not available")

    pamt = game_dir / '0008' / '0.pamt'
    entries = parse_pamt(str(pamt), paz_dir=str(pamt.parent))
    iteminfo = next(e for e in entries
                    if e.path == 'gamedata/iteminfo.pabgb')
    iteminfo_bytes = bytes(_extract_from_paz(iteminfo))
    off = 100
    orig = iteminfo_bytes[off:off+4].hex()

    patch_data = {
        "modinfo": {"title": "CleanImport", "version": "1.0"},
        "patches": [{
            "game_file": "gamedata/iteminfo.pabgb",
            "changes": [{"offset": off, "original": orig,
                         "patched": "ffffffff", "label": "good"}],
        }],
    }

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        td = Path(td)
        zip_path = td / "test.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("CleanImport.json", json.dumps(patch_data))

        db = Database(td / "test.db")
        db.initialize()
        snapshot = SnapshotManager(game_dir / "CDMods" / "snapshot.json")
        deltas = td / "deltas"
        deltas.mkdir()

        result = import_from_zip(zip_path, game_dir, db, snapshot, deltas)
        db.connection.close()

    assert result is not None
    assert result.error is None
    # Either info is None or doesn't mention skips.
    if result.info is not None:
        assert "skipped" not in result.info.lower(), (
            f"Clean import shouldn't mention skips. info={result.info!r}")
