"""Format 3 — apply supported intents to records.

Phase 2 step 1: given vanilla records (parsed by
``semantic.parser.parse_records``) and a list of supported intents,
produce the synthesized mod records the existing
``SemanticEngine.analyze_bytes`` pipeline expects. The engine
diffs vanilla vs synth, merges with other mods, and rewrites the
PABGB body via ``build_merged_body``.

These tests work at the records-dict level — no binary fixtures
needed yet. End-to-end binary roundtrip lands in step 2.
"""
from __future__ import annotations

import pytest

from cdumm.engine.format3_handler import (
    Format3Intent,
    apply_intents_to_records,
)


# ── apply_intents_to_records ────────────────────────────────────────


def test_apply_set_intent_changes_field_in_matching_record():
    vanilla = {
        175001: {"_key": 175001, "_name": "DropSet_Faction_Graymane",
                 "_dropRollCount": 1, "_dropRollType": 0},
        175002: {"_key": 175002, "_name": "DropSet_Other",
                 "_dropRollCount": 1, "_dropRollType": 0},
    }
    intents = [Format3Intent(
        entry="DropSet_Faction_Graymane", key=175001,
        field="_dropRollCount", op="set", new=99)]
    out = apply_intents_to_records(vanilla, intents)
    # Only the touched record is in the output (matches what the
    # existing differ expects from a "mod" — only changed records).
    assert set(out.keys()) == {175001}
    assert out[175001]["_dropRollCount"] == 99
    # Untouched fields preserved
    assert out[175001]["_dropRollType"] == 0
    assert out[175001]["_name"] == "DropSet_Faction_Graymane"


def test_apply_does_not_mutate_vanilla_input():
    """Caller must be able to reuse vanilla_records after this
    call without seeing intent-applied values."""
    vanilla = {1: {"_key": 1, "_dropRollCount": 7}}
    intents = [Format3Intent(
        entry="x", key=1, field="_dropRollCount",
        op="set", new=42)]
    apply_intents_to_records(vanilla, intents)
    assert vanilla[1]["_dropRollCount"] == 7  # untouched


def test_intent_keyed_to_missing_record_is_silently_dropped():
    """If the intent's key doesn't match any vanilla record, we
    can't synthesize a mod record for it — drop silently here so
    the apply pipeline doesn't see a phantom record. The classifier
    upstream already warns on unknown entries during validation."""
    vanilla = {1: {"_key": 1, "_dropRollCount": 7}}
    intents = [Format3Intent(
        entry="missing", key=99999,
        field="_dropRollCount", op="set", new=42)]
    out = apply_intents_to_records(vanilla, intents)
    assert out == {}


def test_multiple_intents_on_same_record_merge_into_one_synth_record():
    vanilla = {1: {"_key": 1, "_dropRollCount": 0,
                   "_dropRollType": 0,
                   "_dropTagNameHash": 0}}
    intents = [
        Format3Intent(entry="x", key=1,
                      field="_dropRollCount", op="set", new=5),
        Format3Intent(entry="x", key=1,
                      field="_dropRollType", op="set", new=2),
    ]
    out = apply_intents_to_records(vanilla, intents)
    assert out[1]["_dropRollCount"] == 5
    assert out[1]["_dropRollType"] == 2
    assert out[1]["_dropTagNameHash"] == 0  # untouched


def test_intents_on_different_records_yield_two_synth_records():
    vanilla = {
        1: {"_key": 1, "_dropRollCount": 0},
        2: {"_key": 2, "_dropRollCount": 0},
    }
    intents = [
        Format3Intent(entry="a", key=1,
                      field="_dropRollCount", op="set", new=10),
        Format3Intent(entry="b", key=2,
                      field="_dropRollCount", op="set", new=20),
    ]
    out = apply_intents_to_records(vanilla, intents)
    assert set(out.keys()) == {1, 2}
    assert out[1]["_dropRollCount"] == 10
    assert out[2]["_dropRollCount"] == 20


def test_unsupported_op_intent_does_not_modify_record():
    """Phase 1 only supports 'set'. Anything else slipping through
    here (caller didn't validate first) must be a no-op rather
    than corrupting the synth record with a partial state."""
    vanilla = {1: {"_key": 1, "_dropRollCount": 0}}
    intents = [Format3Intent(
        entry="x", key=1, field="_dropRollCount",
        op="add_entry", new=5)]
    out = apply_intents_to_records(vanilla, intents)
    # No matching record produced because no 'set' was applied
    assert out == {}


def test_field_not_in_vanilla_record_does_not_create_phantom_field():
    """If the intent's field somehow isn't on the vanilla record,
    don't add it — it would corrupt the differ which expects fields
    to either match a schema field or be absent."""
    vanilla = {1: {"_key": 1, "_dropRollCount": 0}}
    intents = [Format3Intent(
        entry="x", key=1, field="bogusField",
        op="set", new=99)]
    out = apply_intents_to_records(vanilla, intents)
    # No record produced — bogusField wasn't on vanilla so we
    # have nothing meaningful to write.
    assert out == {}
