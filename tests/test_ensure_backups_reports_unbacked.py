"""_ensure_backups must report files it cannot safely back up.

Audit finding 3 (2026-06-11): ``unbacked_files`` was initialized and
returned but nothing ever appended to it, so the caller's abort path
("these game files don't match vanilla and can't be safely backed up")
was dead code. The two failure points now append:

* no vanilla backup exists AND the live game file diverges from the
  snapshot (composing from a modded base, revert impossible), and
* the existing backup is contaminated AND the game file is also modded
  (restore chain poisoned).
"""
from __future__ import annotations

from pathlib import Path

from cdumm.engine.apply_engine import ApplyWorker
from cdumm.engine.snapshot_manager import hash_file
from cdumm.storage.database import Database


def _make_worker(tmp_path: Path):
    game_dir = tmp_path / "game"
    vanilla_dir = tmp_path / "vanilla"
    (game_dir / "0008").mkdir(parents=True)
    vanilla_dir.mkdir(parents=True)
    db = Database(tmp_path / "t.db")
    db.initialize()
    worker = ApplyWorker(game_dir, vanilla_dir, db.db_path)
    worker._db = db
    return worker, game_dir, vanilla_dir, db


def _snapshot(db: Database, file_path: str, content: bytes,
              tmp_path: Path) -> None:
    probe = tmp_path / "_snap_probe.bin"
    probe.write_bytes(content)
    h, size = hash_file(probe)
    db.connection.execute(
        "INSERT INTO snapshots (file_path, file_hash, file_size) "
        "VALUES (?, ?, ?)", (file_path, h, size))
    db.connection.commit()


def test_modded_game_file_without_backup_is_reported(tmp_path):
    worker, game_dir, vanilla_dir, db = _make_worker(tmp_path)
    try:
        vanilla_content = b"\x00" * 12 + b"PAMT_VANILLA" * 8
        _snapshot(db, "0008/0.pamt", vanilla_content, tmp_path)
        # Live file diverges from the snapshot (different size).
        (game_dir / "0008" / "0.pamt").write_bytes(
            vanilla_content + b"MODDED_TAIL")

        unbacked = worker._ensure_backups(
            {"0008/0.pamt": [{"delta_path": "d", "is_new": False}]}, [])
        assert "0008/0.pamt" in unbacked
        # And no bogus backup was written.
        assert not (vanilla_dir / "0008" / "0.pamt").exists()
    finally:
        db.close()


def test_vanilla_game_file_backs_up_cleanly(tmp_path):
    worker, game_dir, vanilla_dir, db = _make_worker(tmp_path)
    try:
        vanilla_content = b"\x00" * 12 + b"PAMT_VANILLA" * 8
        _snapshot(db, "0008/0.pamt", vanilla_content, tmp_path)
        (game_dir / "0008" / "0.pamt").write_bytes(vanilla_content)

        unbacked = worker._ensure_backups(
            {"0008/0.pamt": [{"delta_path": "d", "is_new": False}]}, [])
        assert unbacked == []
        assert (vanilla_dir / "0008" / "0.pamt").read_bytes() \
            == vanilla_content
    finally:
        db.close()


def test_contaminated_backup_with_modded_game_file_is_reported(tmp_path):
    worker, game_dir, vanilla_dir, db = _make_worker(tmp_path)
    try:
        vanilla_content = b"\x00" * 12 + b"PAMT_VANILLA" * 8
        _snapshot(db, "0008/0.pamt", vanilla_content, tmp_path)
        # Backup exists but does not match the snapshot (wrong size),
        # and the live game file is modded too: restore chain poisoned.
        (vanilla_dir / "0008").mkdir(parents=True)
        (vanilla_dir / "0008" / "0.pamt").write_bytes(b"CONTAMINATED")
        (game_dir / "0008" / "0.pamt").write_bytes(
            vanilla_content + b"MODDED_TAIL")

        unbacked = worker._ensure_backups(
            {"0008/0.pamt": [{"delta_path": "d", "is_new": False}]}, [])
        assert "0008/0.pamt" in unbacked
    finally:
        db.close()
