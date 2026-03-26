from cdmm.engine.test_mod_checker import generate_compatibility_report, ModTestResult
from cdmm.engine.conflict_detector import Conflict


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


def test_generate_report_markdown_format() -> None:
    result = ModTestResult("TestMod")
    result.changed_files = [{"file_path": "0008/0.paz"}]
    result.compatible_mods = ["ModA"]

    report = generate_compatibility_report(result)
    assert report.startswith("# Compatibility Report:")
    assert "Generated:" in report
    assert "Crimson Desert Mod Manager" in report
