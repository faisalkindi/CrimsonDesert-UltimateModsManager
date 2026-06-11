"""Loader files staged by import_from_zip must survive EVERY detection
branch (audit finding 5).

import_from_zip stages .asi / .addon64 files into deltas/_asi_staging/
early, then walks a detection ladder where most branches build a FRESH
ModImportResult via helpers. The CB, loose-file-mod, single-Format-3,
and texture branches used to return that fresh result without
re-attaching asi_staged, so the GUI never installed the loaders and
the next-launch sweep deleted the staging dir.

The CB branch is exercised here by monkeypatching the heavy detection
and conversion helpers (a real CB payload needs a populated game dir
with PAMTs); the fix is one shared _with_asi() re-attach helper used
by every branch, so covering one branch covers the shape of all four.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest


@pytest.fixture
def env(tmp_path: Path):
    from cdumm.storage.database import Database
    from cdumm.engine.snapshot_manager import SnapshotManager

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()
    db = Database(tmp_path / "test.db")
    db.initialize()
    snapshot = SnapshotManager(db)
    yield game_dir, deltas_dir, db, snapshot
    db.close()


def _make_cb_zip(zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("CrimsonWeather.addon64", b"MZ\x00\x00fake")
        zf.writestr("CrimsonWeather.ini", b"[Settings]\nKey=Value\n")
        zf.writestr("payload/hair.xml", b"<hair/>")


def test_cb_branch_reattaches_staged_loaders(env, monkeypatch,
                                             tmp_path: Path):
    game_dir, deltas_dir, db, snapshot = env
    import cdumm.engine.import_handler as ih

    zip_path = tmp_path / "cbmod.zip"
    _make_cb_zip(zip_path)

    # Simulate a CB-detected payload without a populated game dir.
    monkeypatch.setattr(
        ih, "detect_crimson_browser",
        lambda root: {"id": "fake-cb", "_base_dir": None})

    converted_dir = tmp_path / "converted"
    converted_dir.mkdir()
    monkeypatch.setattr(
        ih, "convert_to_paz_mod",
        lambda manifest, gd, work, **kw: converted_dir)

    cb_result = ih.ModImportResult("Fake CB Mod")
    cb_result.mod_id = 42
    cb_result.changed_files = [{"file_path": "0009/0.paz"}]
    monkeypatch.setattr(
        ih, "_process_extracted_files",
        lambda *a, **kw: cb_result)

    result = ih.import_from_zip(
        zip_path=zip_path, game_dir=game_dir, db=db,
        snapshot=snapshot, deltas_dir=deltas_dir)

    assert result is cb_result, "CB branch should have been taken"
    assert result.asi_staged, (
        "CB branch returned a fresh ModImportResult without "
        "re-attaching the staged .addon64/.ini; the GUI never installs "
        "them and the next-launch sweep deletes the staging dir")
    staged_names = sorted(Path(p).name for p in result.asi_staged)
    assert staged_names == ["CrimsonWeather.addon64",
                            "CrimsonWeather.ini"]
    for p in result.asi_staged:
        assert Path(p).exists()
        assert str(deltas_dir.resolve()) in str(Path(p).resolve())
