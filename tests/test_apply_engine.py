from pathlib import Path

from cdmm.archive.hashlittle import hashlittle, compute_pamt_hash, compute_papgt_hash
from cdmm.engine.apply_engine import ApplyWorker, RevertWorker
from cdmm.engine.delta_engine import generate_delta, save_delta
from cdmm.storage.database import Database


def test_hashlittle_deterministic() -> None:
    data = b"Hello World" * 100
    h1 = hashlittle(data, 0xC5EDE)
    h2 = hashlittle(data, 0xC5EDE)
    assert h1 == h2
    assert isinstance(h1, int)
    assert 0 <= h1 < 0x100000000


def test_hashlittle_different_data() -> None:
    h1 = hashlittle(b"aaa" * 50, 0xC5EDE)
    h2 = hashlittle(b"bbb" * 50, 0xC5EDE)
    assert h1 != h2


def test_compute_pamt_hash() -> None:
    pamt_data = b"\x00" * 12 + b"PAMT_BODY_DATA" * 10
    h = compute_pamt_hash(pamt_data)
    assert isinstance(h, int)
    assert 0 <= h < 0x100000000


def test_compute_papgt_hash() -> None:
    papgt_data = b"\x00" * 12 + b"PAPGT_BODY_DATA" * 10
    h = compute_papgt_hash(papgt_data)
    assert isinstance(h, int)


def _setup_apply_test(tmp_path: Path) -> tuple[Path, Path, Database]:
    """Create game dir with vanilla files, vanilla backup dir, and database."""
    game_dir = tmp_path / "game"
    vanilla_dir = tmp_path / "vanilla"

    # Create game files
    (game_dir / "0008").mkdir(parents=True)
    paz_content = b"ORIGINAL_PAZ_CONTENT" + b"\x00" * 200
    (game_dir / "0008" / "0.paz").write_bytes(paz_content)
    (game_dir / "0008" / "0.pamt").write_bytes(b"\x00" * 12 + b"PAMT_BODY" * 20)

    # Create vanilla backups
    (vanilla_dir / "0008").mkdir(parents=True)
    (vanilla_dir / "0008" / "0.paz").write_bytes(paz_content)
    (vanilla_dir / "0008" / "0.pamt").write_bytes(b"\x00" * 12 + b"PAMT_BODY" * 20)

    # Database
    db = Database(tmp_path / "test.db")
    db.initialize()

    return game_dir, vanilla_dir, db


def test_apply_worker_single_mod(tmp_path: Path) -> None:
    game_dir, vanilla_dir, db = _setup_apply_test(tmp_path)
    deltas_dir = tmp_path / "deltas"

    # Create a mod with a delta
    vanilla_paz = (game_dir / "0008" / "0.paz").read_bytes()
    modified_paz = bytearray(vanilla_paz)
    modified_paz[20:30] = b"\xFF" * 10
    modified_paz = bytes(modified_paz)

    delta = generate_delta(vanilla_paz, modified_paz)
    delta_path = deltas_dir / "1" / "0008_0.paz.bsdiff"
    save_delta(delta, delta_path)

    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled) VALUES (1, 'TestMod', 'paz', 1)"
    )
    db.connection.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, byte_end) "
        "VALUES (1, '0008/0.paz', ?, 20, 30)",
        (str(delta_path),)
    )
    db.connection.commit()

    worker = ApplyWorker(game_dir, vanilla_dir, db.db_path)

    errors = []
    finished = []
    worker.error_occurred.connect(lambda e: errors.append(e))
    worker.finished.connect(lambda: finished.append(True))
    worker.run()

    assert len(errors) == 0, f"Apply errors: {errors}"
    assert len(finished) == 1

    # Verify game file was modified
    result = (game_dir / "0008" / "0.paz").read_bytes()
    assert result[20:30] == b"\xFF" * 10
    db.close()


def test_revert_worker(tmp_path: Path) -> None:
    game_dir, vanilla_dir, db = _setup_apply_test(tmp_path)

    # Modify game file (simulate applied mod)
    modified = bytearray((game_dir / "0008" / "0.paz").read_bytes())
    modified[0:5] = b"MODDD"
    (game_dir / "0008" / "0.paz").write_bytes(bytes(modified))

    worker = RevertWorker(game_dir, vanilla_dir, db.db_path)

    errors = []
    finished = []
    worker.error_occurred.connect(lambda e: errors.append(e))
    worker.finished.connect(lambda: finished.append(True))
    worker.run()

    assert len(errors) == 0, f"Revert errors: {errors}"
    assert len(finished) == 1

    # Verify game file restored to vanilla
    result = (game_dir / "0008" / "0.paz").read_bytes()
    vanilla = (vanilla_dir / "0008" / "0.paz").read_bytes()
    assert result == vanilla
    db.close()


def test_apply_no_enabled_mods(tmp_path: Path) -> None:
    game_dir, vanilla_dir, db = _setup_apply_test(tmp_path)

    worker = ApplyWorker(game_dir, vanilla_dir, db.db_path)
    errors = []
    worker.error_occurred.connect(lambda e: errors.append(e))
    worker.run()

    assert len(errors) == 1
    assert "No enabled mods" in errors[0]
    db.close()


def test_revert_no_backups(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    empty_vanilla = tmp_path / "empty_vanilla"

    db = Database(tmp_path / "test.db")
    db.initialize()

    worker = RevertWorker(game_dir, empty_vanilla, db.db_path)
    errors = []
    worker.error_occurred.connect(lambda e: errors.append(e))
    worker.run()

    assert len(errors) == 1
    assert "No vanilla" in errors[0]
    db.close()
