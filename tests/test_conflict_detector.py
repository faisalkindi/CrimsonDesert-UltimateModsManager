from cdumm.engine.conflict_detector import ConflictDetector
from cdumm.storage.database import Database


def _insert_mod_with_deltas(db: Database, mod_id: int, name: str,
                             deltas: list[tuple[str, int, int]]) -> None:
    """Helper: insert a mod with byte-range deltas."""
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled) VALUES (?, ?, ?, ?)",
        (mod_id, name, "paz", 1),
    )
    for file_path, byte_start, byte_end in deltas:
        db.connection.execute(
            "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, byte_end) "
            "VALUES (?, ?, ?, ?, ?)",
            (mod_id, file_path, f"/fake/delta/{mod_id}.bsdiff", byte_start, byte_end),
        )
    db.connection.commit()


def test_no_conflicts_different_files(db: Database) -> None:
    _insert_mod_with_deltas(db, 1, "ModA", [("0008/0.paz", 100, 200)])
    _insert_mod_with_deltas(db, 2, "ModB", [("0010/0.paz", 100, 200)])

    detector = ConflictDetector(db)
    conflicts = detector.detect_all()

    # No byte-range or PAZ conflicts — different files entirely
    byte_conflicts = [c for c in conflicts if c.level == "byte_range"]
    paz_conflicts = [c for c in conflicts if c.level == "paz"]
    assert len(byte_conflicts) == 0
    assert len(paz_conflicts) == 0


def test_byte_range_conflict_overlapping(db: Database) -> None:
    _insert_mod_with_deltas(db, 1, "ModA", [("0008/0.paz", 100, 200)])
    _insert_mod_with_deltas(db, 2, "ModB", [("0008/0.paz", 150, 250)])

    detector = ConflictDetector(db)
    conflicts = detector.detect_all()

    byte_conflicts = [c for c in conflicts if c.level == "byte_range"]
    assert len(byte_conflicts) == 1
    assert byte_conflicts[0].byte_start == 150  # overlap start
    assert byte_conflicts[0].byte_end == 200  # overlap end
    assert "ModA" in byte_conflicts[0].explanation
    assert "ModB" in byte_conflicts[0].explanation


def test_paz_level_no_overlap(db: Database) -> None:
    _insert_mod_with_deltas(db, 1, "ModA", [("0008/0.paz", 100, 200)])
    _insert_mod_with_deltas(db, 2, "ModB", [("0008/0.paz", 300, 400)])

    detector = ConflictDetector(db)
    conflicts = detector.detect_all()

    byte_conflicts = [c for c in conflicts if c.level == "byte_range"]
    paz_conflicts = [c for c in conflicts if c.level == "paz"]
    assert len(byte_conflicts) == 0
    assert len(paz_conflicts) == 1
    assert "compatible" in paz_conflicts[0].explanation.lower()


def test_papgt_level_different_dirs(db: Database) -> None:
    _insert_mod_with_deltas(db, 1, "LootMod", [("0008/0.pamt", 0, 4)])
    _insert_mod_with_deltas(db, 2, "FontMod", [("0019/0.pamt", 0, 4)])

    detector = ConflictDetector(db)
    conflicts = detector.detect_all()

    papgt_conflicts = [c for c in conflicts if c.level == "papgt"]
    assert len(papgt_conflicts) == 1
    assert "automatically" in papgt_conflicts[0].explanation.lower()


def test_mod_status_clean(db: Database) -> None:
    _insert_mod_with_deltas(db, 1, "ModA", [("0008/0.paz", 100, 200)])

    detector = ConflictDetector(db)
    detector.detect_all()
    assert detector.get_mod_status(1) == "clean"


def test_mod_status_conflict(db: Database) -> None:
    _insert_mod_with_deltas(db, 1, "ModA", [("0008/0.paz", 100, 200)])
    _insert_mod_with_deltas(db, 2, "ModB", [("0008/0.paz", 150, 250)])

    detector = ConflictDetector(db)
    detector.detect_all()
    # Byte-range overlaps are auto-resolved via load order (winner assigned)
    assert detector.get_mod_status(1) == "resolved"
    assert detector.get_mod_status(2) == "resolved"


def test_mod_status_warning(db: Database) -> None:
    _insert_mod_with_deltas(db, 1, "ModA", [("0008/0.paz", 100, 200)])
    _insert_mod_with_deltas(db, 2, "ModB", [("0008/0.paz", 500, 600)])

    detector = ConflictDetector(db)
    detector.detect_all()
    # Same file, different byte ranges = compatible (clean)
    assert detector.get_mod_status(1) == "clean"


def test_disabled_mods_excluded(db: Database) -> None:
    _insert_mod_with_deltas(db, 1, "ModA", [("0008/0.paz", 100, 200)])
    db.connection.execute(
        "INSERT INTO mods (id, name, mod_type, enabled) VALUES (?, ?, ?, ?)",
        (2, "DisabledMod", "paz", 0),
    )
    db.connection.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, byte_end) "
        "VALUES (?, ?, ?, ?, ?)",
        (2, "0008/0.paz", "/fake.bsdiff", 150, 250),
    )
    db.connection.commit()

    detector = ConflictDetector(db)
    conflicts = detector.detect_all()
    assert len(conflicts) == 0  # Disabled mod excluded


def test_get_conflicts_for_mod(db: Database) -> None:
    _insert_mod_with_deltas(db, 1, "ModA", [("0008/0.paz", 100, 200)])
    _insert_mod_with_deltas(db, 2, "ModB", [("0008/0.paz", 150, 250)])

    detector = ConflictDetector(db)
    detector.detect_all()

    mod_conflicts = detector.get_conflicts_for_mod(1)
    assert len(mod_conflicts) >= 1
    assert all(c.mod_a_id == 1 or c.mod_b_id == 1 for c in mod_conflicts)


def test_check_new_mod(db: Database) -> None:
    _insert_mod_with_deltas(db, 1, "ExistingMod", [("0008/0.paz", 100, 200)])
    _insert_mod_with_deltas(db, 2, "NewMod", [("0008/0.paz", 150, 250)])

    detector = ConflictDetector(db)
    conflicts = detector.check_new_mod(2)
    assert len(conflicts) >= 1


def test_multiple_byte_range_conflicts(db: Database) -> None:
    _insert_mod_with_deltas(db, 1, "ModA", [
        ("0008/0.paz", 100, 200),
        ("0008/0.paz", 500, 600),
    ])
    _insert_mod_with_deltas(db, 2, "ModB", [
        ("0008/0.paz", 150, 250),
        ("0008/0.paz", 550, 650),
    ])

    detector = ConflictDetector(db)
    conflicts = detector.detect_all()
    byte_conflicts = [c for c in conflicts if c.level == "byte_range"]
    assert len(byte_conflicts) == 2
