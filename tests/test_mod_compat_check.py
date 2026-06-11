"""Tests for mod_compat_check (renamed from test_mod_checker, audit
finding C2 2026-06-10: the old module matched pytest's collection
pattern, and its failure-path cleanup deleted MAX(id) from the mods
table, destroying the user's newest real mod when the trial import
failed before inserting anything)."""
from cdumm.engine.mod_compat_check import (
    ModTestResult,
    _cleanup_trial_rows,
    generate_compatibility_report,
)
from cdumm.engine.conflict_detector import Conflict
from cdumm.storage.database import Database


def test_generate_report_no_conflicts() -> None:
    result = ModTestResult("MyTestMod")
    result.changed_files = [{"file_path": "0008/0.paz"}]
    result.compatible_mods = ["CDLootMultiplier", "CDInventoryExpander"]
    result.conflicts = []

    report = generate_compatibility_report(result)
    assert "MyTestMod" in report
    assert "0008/0.paz" in report
    assert "CDLootMultiplier" in report
    assert "CDInventoryExpander" in report
    assert "Conflicts" not in report or "**Conflicts with:**" not in report


def test_generate_report_with_conflicts() -> None:
    result = ModTestResult("CombatMod")
    result.changed_files = [{"file_path": "0010/0.paz"}]
    result.compatible_mods = ["CDInventoryExpander"]
    result.conflicts = [
        Conflict(
            mod_a_id=1, mod_a_name="CombatMod",
            mod_b_id=2, mod_b_name="OtherCombatMod",
            file_path="0010/0.paz", level="byte_range",
            byte_start=100, byte_end=200,
            explanation="Both modify sword_upper.paac combat states",
        )
    ]

    report = generate_compatibility_report(result)
    assert "CombatMod" in report
    assert "OtherCombatMod" in report
    assert "sword_upper" in report
    assert "CDInventoryExpander" in report


def test_generate_report_no_changed_files() -> None:
    result = ModTestResult("EmptyMod")
    result.changed_files = []
    result.compatible_mods = []

    report = generate_compatibility_report(result)
    assert "No file changes" in report


def test_generate_report_tolerates_plain_string_entries() -> None:
    """changed_files mixes dicts and plain strings depending on the
    import pass that produced the entry (audit minor M7); the report
    must not crash on the string form."""
    result = ModTestResult("MixedMod")
    result.changed_files = [{"file_path": "0008/0.paz"}, "0010/0.paz"]
    report = generate_compatibility_report(result)
    assert "0008/0.paz" in report
    assert "0010/0.paz" in report


def test_generate_report_markdown_format() -> None:
    result = ModTestResult("TestMod")
    result.changed_files = [{"file_path": "0008/0.paz"}]
    result.compatible_mods = ["ModA"]

    report = generate_compatibility_report(result)
    assert report.startswith("# Compatibility Report:")
    assert "Generated:" in report
    assert "Crimson Desert Ultimate Mods Manager" in report


def test_cleanup_never_deletes_below_watermark(tmp_path) -> None:
    """The C2 regression pin: cleanup with a watermark equal to the
    newest REAL mod's id must delete nothing, even when the trial
    import inserted no rows at all (the exact failure mode that used
    to destroy the user's newest mod via MAX(id))."""
    db = Database(tmp_path / "t.db")
    db.initialize()
    db.connection.execute(
        "INSERT INTO mods (name, mod_type, enabled, priority) "
        "VALUES ('RealMod', 'paz', 1, 1)")
    db.connection.commit()
    real_id = db.connection.execute(
        "SELECT MAX(id) FROM mods").fetchone()[0]

    # Trial import failed before inserting anything: watermark == max.
    _cleanup_trial_rows(db, real_id)
    assert db.connection.execute(
        "SELECT COUNT(*) FROM mods").fetchone()[0] == 1, (
        "cleanup deleted a pre-existing mod")

    # Trial rows above the watermark ARE deleted.
    db.connection.execute(
        "INSERT INTO mods (name, mod_type, enabled, priority) "
        "VALUES ('TrialMod', 'paz', 1, 2)")
    db.connection.commit()
    _cleanup_trial_rows(db, real_id)
    names = [r[0] for r in db.connection.execute(
        "SELECT name FROM mods").fetchall()]
    assert names == ["RealMod"]
    db.close()
