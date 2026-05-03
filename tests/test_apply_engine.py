from pathlib import Path

from cdumm.archive.hashlittle import hashlittle, compute_pamt_hash, compute_papgt_hash
from cdumm.engine.apply_engine import ApplyWorker, RevertWorker
from cdumm.engine.delta_engine import generate_delta, save_delta
from cdumm.storage.database import Database


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

    # RevertWorker needs mod_deltas rows to know which files to revert.
    # Simulate a mod that touched 0008/0.paz.
    db.connection.execute(
        "INSERT INTO mods (name, mod_type, priority) VALUES (?, ?, ?)",
        ("test_mod", "paz", 1),
    )
    db.connection.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, byte_end, is_new) "
        "VALUES (1, '0008/0.paz', 'dummy', 0, 5, 0)",
    )
    db.connection.commit()

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
    assert "No mod changes to apply or revert" in errors[0]
    db.close()


def test_revert_no_backups(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    empty_vanilla = tmp_path / "empty_vanilla"

    db = Database(tmp_path / "test.db")
    db.initialize()

    # Add a mod delta so RevertWorker has something to attempt
    db.connection.execute(
        "INSERT INTO mods (name, mod_type, priority) VALUES (?, ?, ?)",
        ("test_mod", "paz", 1),
    )
    db.connection.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, byte_end, is_new) "
        "VALUES (1, '0008/0.paz', 'dummy', 0, 5, 0)",
    )
    db.connection.commit()

    worker = RevertWorker(game_dir, empty_vanilla, db.db_path)
    errors = []
    worker.error_occurred.connect(lambda e: errors.append(e))
    worker.run()

    assert len(errors) == 1
    assert "No vanilla" in errors[0]
    db.close()


def _seed_two_enabled_mods(db: Database) -> None:
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled, priority) "
        "VALUES (1, 'ModA', 'paz', 1, 1)"
    )
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled, priority) "
        "VALUES (2, 'ModB', 'paz', 1, 2)"
    )
    db.connection.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, byte_end) "
        "VALUES (1, 'gamedata/x.pabgb', 'a.delta', 0, 10)"
    )
    db.connection.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, byte_end) "
        "VALUES (2, 'gamedata/x.pabgb', 'b.delta', 0, 10)"
    )
    db.connection.execute(
        "INSERT INTO mod_config (mod_id, custom_values) VALUES (1, '{\"k\":1}')"
    )
    db.connection.commit()


def test_apply_fingerprint_changes_on_priority_swap(tmp_path: Path) -> None:
    """GitHub #59: swapping priority between two enabled mods must change
    the apply fingerprint so the next Apply re-runs instead of fast-pathing
    'Already up to date'."""
    game_dir, vanilla_dir, db = _setup_apply_test(tmp_path)
    _seed_two_enabled_mods(db)

    worker = ApplyWorker(game_dir, vanilla_dir, db.db_path)
    worker._db = db  # _compute_apply_fingerprint expects _db (set in run())
    fp_before = worker._compute_apply_fingerprint()

    # Swap priorities (drag-reorder simulation)
    db.connection.execute("UPDATE mods SET priority = 2 WHERE id = 1")
    db.connection.execute("UPDATE mods SET priority = 1 WHERE id = 2")
    db.connection.commit()

    fp_after = worker._compute_apply_fingerprint()
    assert fp_before != fp_after, (
        "Apply fingerprint must include priority — otherwise reorder is "
        "silently skipped by the 'already up to date' fast-path."
    )
    db.close()


def test_apply_fingerprint_changes_on_custom_values_edit(tmp_path: Path) -> None:
    """Editing a custom value (e.g. inventory expand slots) must change the
    fingerprint — aggregator at apply_engine.py:254 reads custom_values."""
    game_dir, vanilla_dir, db = _setup_apply_test(tmp_path)
    _seed_two_enabled_mods(db)

    worker = ApplyWorker(game_dir, vanilla_dir, db.db_path)
    worker._db = db  # _compute_apply_fingerprint expects _db (set in run())
    fp_before = worker._compute_apply_fingerprint()

    db.connection.execute(
        "UPDATE mod_config SET custom_values = '{\"k\":99}' WHERE mod_id = 1"
    )
    db.connection.commit()

    fp_after = worker._compute_apply_fingerprint()
    assert fp_before != fp_after, (
        "Apply fingerprint must include mod_config.custom_values."
    )
    db.close()


def test_apply_fingerprint_changes_on_conflict_mode_flip(tmp_path: Path) -> None:
    """conflict_mode='override' is checked at apply_engine.py:3777 — flipping
    it changes apply order, so it must change the fingerprint."""
    game_dir, vanilla_dir, db = _setup_apply_test(tmp_path)
    _seed_two_enabled_mods(db)

    worker = ApplyWorker(game_dir, vanilla_dir, db.db_path)
    worker._db = db  # _compute_apply_fingerprint expects _db (set in run())
    fp_before = worker._compute_apply_fingerprint()

    db.connection.execute(
        "UPDATE mods SET conflict_mode = 'override' WHERE id = 1"
    )
    db.connection.commit()

    fp_after = worker._compute_apply_fingerprint()
    assert fp_before != fp_after, (
        "Apply fingerprint must include conflict_mode."
    )
    db.close()


def test_apply_fingerprint_changes_on_force_inplace_flip(tmp_path: Path) -> None:
    """force_inplace toggles the overlay-vs-inplace path at apply_engine.py:2287."""
    game_dir, vanilla_dir, db = _setup_apply_test(tmp_path)
    _seed_two_enabled_mods(db)

    worker = ApplyWorker(game_dir, vanilla_dir, db.db_path)
    worker._db = db  # _compute_apply_fingerprint expects _db (set in run())
    fp_before = worker._compute_apply_fingerprint()

    db.connection.execute(
        "UPDATE mods SET force_inplace = 1 WHERE id = 1"
    )
    db.connection.commit()

    fp_after = worker._compute_apply_fingerprint()
    assert fp_before != fp_after, (
        "Apply fingerprint must include force_inplace."
    )
    db.close()
