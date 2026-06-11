"""Batch import must not commit orphan rows from a failed mod
(audit finding 8).

The batch worker shares one SQLite connection across all mods. A
per-mod import that errored (or raised) could return with its
transaction still open; the NEXT mod's commit() then flushed the
failed mod's partial mods/mod_deltas rows into the database as an
orphan half-imported mod. The batch loop now rolls back after every
errored or crashed import.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _write_batch(tmp_path: Path, names: list[str]) -> Path:
    paths = []
    for n in names:
        p = tmp_path / n
        # Real-enough zip so detect_format says "zip".
        import zipfile
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("dummy.txt", "x")
        paths.append(p)
    batch_file = tmp_path / "batch.txt"
    batch_file.write_text(
        "\n".join(str(p) for p in paths), encoding="utf-8")
    return batch_file


def test_failed_import_rows_not_flushed_by_next_commit(
        tmp_path: Path, monkeypatch, capsys):
    import cdumm.engine.import_handler as ih
    from cdumm.worker_process import _run_batch_import
    from cdumm.storage.database import Database

    db_path = tmp_path / "batch.db"
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()

    batch_file = _write_batch(tmp_path, ["broken.zip", "good.zip"])

    def fake_import_from_zip(zip_path, game_dir, db, snapshot,
                             deltas_dir, existing_mod_id=None):
        result = ih.ModImportResult(Path(zip_path).stem)
        if "broken" in Path(zip_path).name:
            # Simulate _process_extracted_files's per-file error path
            # pre-fix: rows inserted, transaction left OPEN, error
            # returned.
            db.connection.execute(
                "INSERT INTO mods (name, mod_type, priority) "
                "VALUES ('Broken Orphan', 'paz', 1)")
            result.error = "Failed to process 0008/0.paz: boom"
            return result
        cur = db.connection.execute(
            "INSERT INTO mods (name, mod_type, priority) "
            "VALUES ('Good Mod', 'paz', 2)")
        db.connection.commit()
        result.mod_id = cur.lastrowid
        return result

    monkeypatch.setattr(ih, "import_from_zip", fake_import_from_zip)

    _run_batch_import(str(batch_file), str(game_dir), str(db_path),
                      str(deltas_dir))

    check = Database(db_path)
    check.initialize()
    names = [r[0] for r in check.connection.execute(
        "SELECT name FROM mods ORDER BY name").fetchall()]
    check.close()
    assert "Good Mod" in names
    assert "Broken Orphan" not in names, (
        "the failed import's uncommitted rows were flushed by the next "
        "mod's commit(); the batch loop must roll back after an "
        "errored import")


def test_crashed_import_rows_not_flushed_by_next_commit(
        tmp_path: Path, monkeypatch, capsys):
    import cdumm.engine.import_handler as ih
    from cdumm.worker_process import _run_batch_import
    from cdumm.storage.database import Database

    db_path = tmp_path / "batch.db"
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()

    batch_file = _write_batch(tmp_path, ["crasher.zip", "good.zip"])

    def fake_import_from_zip(zip_path, game_dir, db, snapshot,
                             deltas_dir, existing_mod_id=None):
        if "crasher" in Path(zip_path).name:
            db.connection.execute(
                "INSERT INTO mods (name, mod_type, priority) "
                "VALUES ('Crash Orphan', 'paz', 1)")
            raise RuntimeError("worker exploded mid-import")
        result = ih.ModImportResult(Path(zip_path).stem)
        cur = db.connection.execute(
            "INSERT INTO mods (name, mod_type, priority) "
            "VALUES ('Good Mod', 'paz', 2)")
        db.connection.commit()
        result.mod_id = cur.lastrowid
        return result

    monkeypatch.setattr(ih, "import_from_zip", fake_import_from_zip)

    _run_batch_import(str(batch_file), str(game_dir), str(db_path),
                      str(deltas_dir))

    check = Database(db_path)
    check.initialize()
    names = [r[0] for r in check.connection.execute(
        "SELECT name FROM mods ORDER BY name").fetchall()]
    check.close()
    assert "Good Mod" in names
    assert "Crash Orphan" not in names
