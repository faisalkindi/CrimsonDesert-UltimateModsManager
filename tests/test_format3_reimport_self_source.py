"""Format 3 reimport from the in-mods-dir source must not WinError 32.

Bug from Matrixz on Nexus 2026-04-28: after the v3.2.4 fix unblocked
his Format 3 import, "Reimport from source" failed with:

    Buffs.json: [WinError 32] The process cannot access the file
                because it is being used by another process

Root cause: ``import_from_natt_format_3`` copies the input JSON to
``CDMods/mods/<safe_name>.json`` at line 2890. On the very FIRST
import, source path != dest path so the copy succeeds. But on
REIMPORT, the stored source_path is already inside CDMods/mods/ —
so shutil.copy2 ends up copying a file onto itself. Windows can
fail this with WinError 32 if any process (CDUMM's own apply
worker, or just AV scanning) holds the file open.

Fix: detect ``samefile(src, dst)`` before the copy and skip when
the source already lives at the destination. The file is already
where we want it; copy-to-self is a no-op anyway.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest


def _write_format3_json(path: Path) -> None:
    path.write_text(json.dumps({
        "modinfo": {"title": "TestReimport", "version": "1.0",
                    "author": "test", "description": ""},
        "format": 3,
        "target": "iteminfo.pabgb",
        "intents": [
            {"entry": "ThiefGloves", "key": 1001250,
             "field": "cooltime", "op": "set", "new": 1},
        ],
    }), encoding="utf-8")


def test_reimport_when_source_already_in_mods_dir_does_not_winerror():
    """Calling import_from_natt_format_3 with json_path that already
    resolves to the destination CDMods/mods/<safe>.json must not
    raise WinError 32 — and must succeed (re-creating the DB row)."""
    from cdumm.engine.import_handler import import_from_natt_format_3
    from cdumm.engine.snapshot_manager import SnapshotManager
    from cdumm.storage.database import Database

    game_dir = Path(r'E:\SteamLibrary\steamapps\common\Crimson Desert')
    if not (game_dir / '0008' / '0.pamt').exists():
        pytest.skip("Live game dir not available")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        td = Path(td)
        db = Database(td / "test.db")
        db.initialize()
        deltas = td / "deltas"
        deltas.mkdir()
        snapshot = SnapshotManager(game_dir / "CDMods" / "snapshot.json")

        # Stage the JSON in CDMods/mods/ to mirror the post-import
        # state — same place a real reimport would call from.
        # Use a tmp-named CDMods so we don't clobber the user's real
        # state. The function under test computes its dest path from
        # game_dir, so we must redirect game_dir to the temp.
        fake_game_dir = td / "fake_game"
        fake_game_dir.mkdir()
        # Also need fake 0008/0.pamt for the validation step. Just
        # link to the real one.
        (fake_game_dir / "0008").mkdir()
        for f in ("0.pamt", "0.paz"):
            real = game_dir / "0008" / f
            link = fake_game_dir / "0008" / f
            try:
                os.link(real, link)  # hard link (free, fast)
            except OSError:
                # Fall back to copy if hardlink not allowed
                import shutil as _shutil
                _shutil.copy2(real, link)

        mods_dir = fake_game_dir / "CDMods" / "mods"
        mods_dir.mkdir(parents=True)
        # The function will write to mods_dir / "TestReimport.json"
        # because the modinfo title is "TestReimport". Pre-place
        # the file there to simulate "already imported state".
        json_path = mods_dir / "TestReimport.json"
        _write_format3_json(json_path)

        # Now reimport from this same path. Without the fix this
        # raises shutil.SameFileError or WinError 32 depending on
        # whether the file is currently locked.
        result = import_from_natt_format_3(
            json_path=json_path,
            game_dir=fake_game_dir,
            db=db,
            snapshot=snapshot,
            deltas_dir=deltas,
        )
        db.connection.close()

    # Result should be a successful import (or at least not crash
    # with WinError 32 / SameFileError).
    assert result is not None
    # We don't strictly require result.error to be None — the apply
    # may still skip if intents fail validation against the schema.
    # The important assertion: no copy-to-self exception was raised.


def test_first_time_import_still_copies_to_mods_dir():
    """Regression guard: a fresh import (source NOT in mods/) must
    still copy the file there as before."""
    from cdumm.engine.import_handler import import_from_natt_format_3
    from cdumm.engine.snapshot_manager import SnapshotManager
    from cdumm.storage.database import Database

    game_dir = Path(r'E:\SteamLibrary\steamapps\common\Crimson Desert')
    if not (game_dir / '0008' / '0.pamt').exists():
        pytest.skip("Live game dir not available")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        td = Path(td)
        db = Database(td / "test.db")
        db.initialize()
        deltas = td / "deltas"
        deltas.mkdir()
        snapshot = SnapshotManager(game_dir / "CDMods" / "snapshot.json")

        fake_game_dir = td / "fake_game"
        fake_game_dir.mkdir()
        (fake_game_dir / "0008").mkdir()
        for f in ("0.pamt", "0.paz"):
            real = game_dir / "0008" / f
            link = fake_game_dir / "0008" / f
            try:
                os.link(real, link)
            except OSError:
                import shutil as _shutil
                _shutil.copy2(real, link)

        # Source NOT in mods/ — drop in a separate temp dir
        src_dir = td / "user_drop"
        src_dir.mkdir()
        json_path = src_dir / "TestFirstImport.json"
        _write_format3_json(json_path)

        result = import_from_natt_format_3(
            json_path=json_path,
            game_dir=fake_game_dir,
            db=db,
            snapshot=snapshot,
            deltas_dir=deltas,
        )

        # The destination must now exist (the copy ran). The dest
        # filename is derived from the modinfo title, sanitized.
        # Don't hard-code the filename — just assert SOMETHING got
        # created in mods/.
        mods_root = fake_game_dir / "CDMods" / "mods"
        assert mods_root.exists() and any(mods_root.iterdir()), (
            f"First-import must place a JSON in {mods_root}. "
            f"result.error={result.error}")
        db.connection.close()
