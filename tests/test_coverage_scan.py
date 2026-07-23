"""scripts/coverage_scan.py surfaces the Format-3 intents CDUMM can't apply.

It runs every intent through the real ``validate_intents`` classifier, so a
covered field reports no gap while an uncovered one does -- and it needs no
game install (coverage is decided from the shipped schema + field_schema +
registered LIST_WRITERS). These tests pin both directions, using two kinds of
real gap the classifier skips today:

* an **unsupported op** (``scale``) on a real, covered field (#71); and
* an **unmodelled table** (``gimmickinfo`` -- no schema, no writer).

and a covered field (``buffinfo.min_level``) that must stay silent. This keeps
the scanner from silently starting to mis-report either way. Note: the #190
``equipslotinfo.entries[].etl_hashes`` writer means equipslotinfo is *covered*,
so it deliberately is not used here as an "uncovered" example.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

# The scanner lives in scripts/, not the installed package, so load it by path.
_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "coverage_scan.py"
_spec = importlib.util.spec_from_file_location("coverage_scan", _SCRIPT)
coverage_scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(coverage_scan)


def _write(p: Path, target: str, field, new, op: str = "set") -> None:
    p.write_text(json.dumps({
        "format": 3, "target": target,
        "intents": [{"entry": "", "key": 1, "field": field,
                     "op": op, "new": new}],
    }))


def test_unsupported_op_is_reported(tmp_path):
    # op=scale isn't applied yet (#71), so validate_intents skips it -> a
    # genuine surfaced gap. The intent carries BOTH 'new' and 'factor' so the
    # file parses regardless of the scale-parsing rules: plain master requires
    # 'new', while the DMM scale-tolerance change (#304) requires 'factor'.
    # With both present it's well-formed either way, keeping this test green on
    # master and on any branch that also merges #304.
    (tmp_path / "scale.field.json").write_text(json.dumps({
        "format": 3, "target": "buffinfo.pabgb",
        "intents": [{"entry": "", "key": 1, "field": "min_level",
                     "op": "scale", "new": 2, "factor": 2}],
    }))
    gaps = coverage_scan.scan([str(tmp_path)])
    assert ("buffinfo", "min_level") in {(g.table, g.field) for g in gaps}
    assert any("scale" in g.reason for g in gaps), gaps


def test_unmodelled_table_is_reported(tmp_path):
    # gimmickinfo has no schema, no field_schema, and no LIST_WRITER -> gap.
    _write(tmp_path / "gim.field.json", "gimmickinfo.pabgb", "some_field", 1)
    gaps = coverage_scan.scan([str(tmp_path)])
    assert ("gimmickinfo", "some_field") in {(g.table, g.field) for g in gaps}


def test_covered_field_is_not_reported(tmp_path):
    # buffinfo.min_level with op=set is a plain schema field -> supported.
    _write(tmp_path / "buff.field.json", "buffinfo.pabgb", "min_level", 1)
    gaps = coverage_scan.scan([str(tmp_path)])
    assert all(g.table != "buffinfo" for g in gaps), (
        f"a covered field was wrongly reported as a gap: {gaps}")


def test_mixed_corpus_reports_only_the_gap(tmp_path):
    _write(tmp_path / "buff.field.json", "buffinfo.pabgb", "min_level", 1)
    _write(tmp_path / "gim.field.json", "gimmickinfo.pabgb", "some_field", 1)
    tables = {g.table for g in coverage_scan.scan([str(tmp_path)])}
    assert "gimmickinfo" in tables and "buffinfo" not in tables


def test_non_format3_files_are_skipped(tmp_path):
    # A PAZ modinfo.json (or any non-Format-3 JSON) must not crash or count.
    (tmp_path / "modinfo.json").write_text('{"name": "x", "version": "1"}')
    assert coverage_scan.scan([str(tmp_path)]) == []


def test_base_field_strips_indices_and_paths():
    assert coverage_scan._base_field("entries[0].etl_hashes") == "entries"
    assert coverage_scan._base_field("prefab_data_list") == "prefab_data_list"
    assert coverage_scan._base_field("upper_chart.group_lookup") == "upper_chart"
