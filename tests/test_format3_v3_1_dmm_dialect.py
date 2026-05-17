"""Tests for the DMM Field JSON v3.1 dialect.

Reference: CrimsonGameMods/FIELD_JSON_V3_1_SPEC.md in NattKh's repo.

The v3.1 dialect:
  - format: 3
  - format_minor: 1 (recommended)
  - targets: [{file, intents}, ...] (replaces singular target/intents)

CDUMM already supported targets[] (commit 2b48aa3, v3.2.13). These tests
guard the dialect end-to-end so a future refactor cannot regress
RichmondS1337's #125 mod and similar DMM-targeted exports.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from cdumm.engine.format3_handler import (
    parse_format3_mod,
    parse_format3_mod_targets,
)
from cdumm.engine.json_patch_handler import is_natt_format_3


FIXTURE = Path(__file__).parent / "fixtures" / "format3" / \
    "dmm_v3_1_multi_target.json"


def test_v3_1_fixture_passes_detector():
    """The is_natt_format_3 detector classifies a real v3.1 export as
    Format 3, even with format_minor: 1 present.
    """
    assert FIXTURE.exists(), "v3.1 fixture missing"
    assert is_natt_format_3(FIXTURE) is True


def test_v3_1_fixture_parses_both_targets():
    """parse_format3_mod_targets returns one pair per target in the
    plural shape. The example ships two targets (iteminfo + gimmick_info).
    """
    pairs = parse_format3_mod_targets(FIXTURE)
    assert len(pairs) == 2
    files = sorted(t for t, _ in pairs)
    assert files == ["gimmick_info.pabgb", "iteminfo.pabgb"]
    # Each target carries exactly one intent in this fixture.
    by_file = {f: ints for f, ints in pairs}
    assert len(by_file["iteminfo.pabgb"]) == 1
    assert len(by_file["gimmick_info.pabgb"]) == 1
    iteminfo_intent = by_file["iteminfo.pabgb"][0]
    assert iteminfo_intent.entry == "Oath_Of_Darkness"
    assert iteminfo_intent.key == 391518535
    assert iteminfo_intent.field == "cooltime"
    assert iteminfo_intent.op == "set"
    assert iteminfo_intent.new == 1


def test_v3_1_legacy_single_target_caller_rejects_multi_target():
    """The legacy parse_format3_mod entry point still raises on
    multi-target files so callers that haven't migrated cannot silently
    drop intents past the first target.
    """
    with pytest.raises(ValueError, match="Multi-target Format 3 file"):
        parse_format3_mod(FIXTURE)


def test_v3_1_format_minor_emits_log_line(tmp_path, caplog):
    """When format_minor >= 1, the parser logs an INFO line naming the
    dialect so bug-report bundles make it obvious which shape the mod
    used. The log line is informational; the document is still parsed.
    """
    doc = {
        "format": 3,
        "format_minor": 1,
        "modinfo": {"title": "log-line probe", "version": "1.0"},
        "targets": [{
            "file": "iteminfo.pabgb",
            "intents": [{
                "entry": "Test_Item", "key": 1, "field": "cooltime",
                "op": "set", "new": 0,
            }],
        }],
    }
    p = tmp_path / "probe.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    with caplog.at_level(logging.INFO, logger="cdumm.engine.format3_handler"):
        pairs = parse_format3_mod_targets(p)
    assert len(pairs) == 1
    assert any(
        "Format 3.1" in rec.message and rec.levelno == logging.INFO
        for rec in caplog.records
    ), "expected an INFO line announcing the v3.1 dialect"


def test_v3_1_unsupported_op_raises_clear_error(tmp_path):
    """v3.1 spec defers list_set / list_append / list_remove / list_merge
    to v3.2. CDUMM rejects them up front with a clear message so the mod
    author sees 'not yet supported' instead of having the intent silently
    drop through to a writer that ignores op and just overwrites.
    """
    doc = {
        "format": 3,
        "format_minor": 1,
        "modinfo": {"title": "unsupported-op probe", "version": "1.0"},
        "targets": [{
            "file": "iteminfo.pabgb",
            "intents": [{
                "entry": "Test_Item", "key": 1, "field": "tags",
                "op": "list_append", "new": ["new_tag"],
            }],
        }],
    }
    p = tmp_path / "list_append.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="list_append.*v3.2.*not yet"):
        parse_format3_mod_targets(p)


def test_v3_0_singular_shape_still_accepted(tmp_path):
    """v3.0 documents (singular target + intents, no format_minor) still
    parse without the log line firing. v3.1 acceptance must not regress
    v3.0 inputs.
    """
    doc = {
        "format": 3,
        "modinfo": {"title": "v3.0 probe", "version": "1.0"},
        "target": "iteminfo.pabgb",
        "intents": [{
            "entry": "Test_Item", "key": 1, "field": "cooltime",
            "op": "set", "new": 0,
        }],
    }
    p = tmp_path / "v3_0.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    pairs = parse_format3_mod_targets(p)
    assert len(pairs) == 1
    target, intents = pairs[0]
    assert target == "iteminfo.pabgb"
    assert len(intents) == 1
