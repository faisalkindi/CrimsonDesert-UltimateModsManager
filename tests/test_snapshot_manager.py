from pathlib import Path

from cdmm.engine.snapshot_manager import hash_file, SnapshotManager, SnapshotWorker
from cdmm.storage.database import Database


def _create_fake_game_dir(tmp_path: Path) -> Path:
    """Create a minimal fake Crimson Desert game directory for testing."""
    game_dir = tmp_path / "game"

    # Create a couple of PAZ directories with files
    for dir_name in ["0000", "0008"]:
        d = game_dir / dir_name
        d.mkdir(parents=True)
        (d / "0.pamt").write_bytes(b"PAMT_DATA_" + dir_name.encode())
        (d / "0.paz").write_bytes(b"PAZ_DATA_" + dir_name.encode() + b"\x00" * 100)

    # Create PAPGT
    meta = game_dir / "meta"
    meta.mkdir()
    (meta / "0.papgt").write_bytes(b"PAPGT_DATA" + b"\x00" * 50)

    # Create game exe for validation
    (game_dir / "bin64").mkdir()
    (game_dir / "bin64" / "CrimsonDesert.exe").write_bytes(b"FAKE_EXE")

    return game_dir


def test_hash_file(tmp_path: Path) -> None:
    f = tmp_path / "test.bin"
    f.write_bytes(b"hello world")
    h, size = hash_file(f)
    assert len(h) == 64  # SHA-256 hex digest
    assert size == 11
    # Same content produces same hash
    f2 = tmp_path / "test2.bin"
    f2.write_bytes(b"hello world")
    h2, _ = hash_file(f2)
    assert h2 == h


def test_hash_file_different_content(tmp_path: Path) -> None:
    f1 = tmp_path / "a.bin"
    f1.write_bytes(b"aaa")
    f2 = tmp_path / "b.bin"
    f2.write_bytes(b"bbb")
    h1, _ = hash_file(f1)
    h2, _ = hash_file(f2)
    assert h1 != h2


def test_snapshot_worker_creates_snapshot(tmp_path: Path) -> None:
    game_dir = _create_fake_game_dir(tmp_path)
    db = Database(tmp_path / "test.db")
    db.initialize()

    worker = SnapshotWorker(game_dir, db.db_path)

    # Collect signals
    finished_count = []
    worker.finished.connect(lambda n: finished_count.append(n))

    errors = []
    worker.error_occurred.connect(lambda e: errors.append(e))

    worker.run()

    assert len(errors) == 0, f"Errors: {errors}"
    assert len(finished_count) == 1
    assert finished_count[0] == 5  # 2 pamt + 2 paz + 1 papgt

    mgr = SnapshotManager(db)
    assert mgr.has_snapshot()
    assert mgr.get_snapshot_count() == 5
    db.close()


def test_snapshot_manager_no_snapshot(db: Database) -> None:
    mgr = SnapshotManager(db)
    assert mgr.has_snapshot() is False
    assert mgr.get_snapshot_count() == 0


def test_snapshot_manager_get_file_hash(tmp_path: Path) -> None:
    game_dir = _create_fake_game_dir(tmp_path)
    db = Database(tmp_path / "test.db")
    db.initialize()

    worker = SnapshotWorker(game_dir, db.db_path)
    worker.run()

    mgr = SnapshotManager(db)
    h = mgr.get_file_hash("0008/0.pamt")
    assert h is not None
    assert len(h) == 64

    assert mgr.get_file_hash("nonexistent/file.paz") is None
    db.close()


def test_snapshot_detect_changes(tmp_path: Path) -> None:
    game_dir = _create_fake_game_dir(tmp_path)
    db = Database(tmp_path / "test.db")
    db.initialize()

    worker = SnapshotWorker(game_dir, db.db_path)
    worker.run()

    mgr = SnapshotManager(db)

    # No changes initially
    changes = mgr.detect_changes(game_dir)
    assert len(changes) == 0

    # Modify a file
    (game_dir / "0008" / "0.paz").write_bytes(b"MODIFIED_PAZ_DATA")
    changes = mgr.detect_changes(game_dir)
    assert len(changes) == 1
    assert changes[0] == ("0008/0.paz", "modified")
    db.close()


def test_snapshot_detect_deleted_file(tmp_path: Path) -> None:
    game_dir = _create_fake_game_dir(tmp_path)
    db = Database(tmp_path / "test.db")
    db.initialize()

    worker = SnapshotWorker(game_dir, db.db_path)
    worker.run()

    mgr = SnapshotManager(db)

    # Delete a file
    (game_dir / "0000" / "0.paz").unlink()
    changes = mgr.detect_changes(game_dir)
    assert ("0000/0.paz", "deleted") in changes
    db.close()


def test_snapshot_worker_empty_dir(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty_game"
    empty_dir.mkdir()

    db = Database(tmp_path / "test.db")
    db.initialize()

    worker = SnapshotWorker(empty_dir, db.db_path)
    errors = []
    worker.error_occurred.connect(lambda e: errors.append(e))
    worker.run()

    assert len(errors) == 1
    assert "No PAZ/PAMT/PAPGT files found" in errors[0]
    db.close()
