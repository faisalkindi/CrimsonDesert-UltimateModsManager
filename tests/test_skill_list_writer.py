"""Skill list-of-dict field writer (uses vendored NattKh
skillinfo_parser).

Bug from timuela on GitHub #41 (focus_aerial_roll skill mod):
Format 3 mods targeting skill.pabgb with `_useResourceStatList`
were skipped at validation time. Now writable.
"""
from __future__ import annotations
from pathlib import Path

import pytest


_VANILLA_PABGB = Path(r"C:\Users\faisa\AppData\Local\Temp\skill.pabgb")
_VANILLA_PABGH = Path(r"C:\Users\faisa\AppData\Local\Temp\skill.pabgh")


def _have_inputs() -> bool:
    return _VANILLA_PABGB.exists() and _VANILLA_PABGH.exists()


@pytest.mark.skipif(not _have_inputs(),
                    reason="vanilla skill extract not present")
def test_skill_parser_loads_and_roundtrips():
    """Vendored skillinfo_parser must round-trip vanilla skill.pabgb
    byte-perfect (the trust anchor for the writer)."""
    from cdumm.engine.skill_writer import _get_parser
    parser = _get_parser()
    if parser is None:
        pytest.skip("skill parser not loadable")
    pabgb = _VANILLA_PABGB.read_bytes()
    pabgh = _VANILLA_PABGH.read_bytes()
    entries = parser.parse_all(pabgh, pabgb)
    assert len(entries) > 0
    re_pabgh, re_pabgb = parser.serialize_all(entries)
    assert re_pabgb == pabgb
    assert re_pabgh == pabgh


@pytest.mark.skipif(not _have_inputs(),
                    reason="vanilla skill extract not present")
def test_skill_writer_applies_useResourceStatList_intent():
    """timuela's mod target: replace `_useResourceStatList` on
    Skill_CrowSuperDash (key 15045)."""
    from cdumm.engine.skill_writer import (
        build_skill_intent_change, _get_parser,
    )
    from cdumm.engine.format3_handler import Format3Intent

    parser = _get_parser()
    if parser is None:
        pytest.skip("skill parser not loadable")

    pabgb = _VANILLA_PABGB.read_bytes()
    pabgh = _VANILLA_PABGH.read_bytes()
    # Pick any skill that has _useResourceStatList
    entries = parser.parse_all(pabgh, pabgb)
    candidate = next(e for e in entries
                     if e.get("_useResourceStatList"))
    target_key = candidate["key"]
    new_value = [{
        "stat_type": 3, "stat_hash": 1000027, "flag": 0,
        "value": 0, "hash2": 1000063, "hash3": 1000046,
    }]

    intent = Format3Intent(
        entry=candidate.get("name", ""),
        key=target_key,
        field="_useResourceStatList",
        op="set",
        new=new_value,
    )

    change = build_skill_intent_change(pabgb, pabgh, [intent])
    assert change is not None
    assert change["offset"] == 0
    assert bytes.fromhex(change["original"]) == pabgb

    new_pabgb = bytes.fromhex(change["patched"])
    assert new_pabgb != pabgb

    new_entries = parser.parse_all(pabgh, new_pabgb)
    new_by_key = {e["key"]: e for e in new_entries}
    rsl = new_by_key[target_key]["_useResourceStatList"]
    assert len(rsl) == 1
    assert rsl[0]["stat_type"] == 3
    assert rsl[0]["stat_hash"] == 1000027


@pytest.mark.skipif(not _have_inputs(),
                    reason="vanilla skill extract not present")
def test_skill_format3_end_to_end():
    """Full pipeline on skill.pabgb: validator + expander + apply."""
    from cdumm.engine.format3_handler import (
        Format3Intent, validate_intents,
    )
    from cdumm.engine.format3_apply import _intents_to_v2_changes
    from cdumm.engine.json_patch_handler import _apply_byte_patches
    from cdumm.engine.skill_writer import _get_parser

    parser = _get_parser()
    if parser is None:
        pytest.skip("skill parser not loadable")

    pabgb = _VANILLA_PABGB.read_bytes()
    pabgh = _VANILLA_PABGH.read_bytes()
    entries = parser.parse_all(pabgh, pabgb)
    candidates = [e for e in entries if e.get("_useResourceStatList")][:3]

    intents = [
        Format3Intent(
            entry=c.get("name", ""), key=c["key"],
            field="_useResourceStatList", op="set",
            new=[{"stat_type": 3, "stat_hash": 1000027, "flag": 0,
                  "value": 999, "hash2": 1000063, "hash3": 1000046}],
        )
        for c in candidates
    ]
    validation = validate_intents("skill.pabgb", intents)
    assert len(validation.supported) == len(intents)

    changes = _intents_to_v2_changes(
        "skill.pabgb", pabgb, pabgh, validation.supported)
    assert len(changes) == 1, (
        f"skill writer must emit ONE whole-table change, got {len(changes)}")

    modified = bytearray(pabgb)
    applied, mismatched, _ = _apply_byte_patches(
        modified, changes, signature=None, vanilla_data=pabgb)
    assert mismatched == 0
    assert applied == 1
