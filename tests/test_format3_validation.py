"""Format 3 — parse + validate.

NattKh's "field-names" JSON format (#41, #208 in CDUMM tracker).
The file has::

    {"format": 3, "target": "dropsetinfo.pabgb",
     "intents": [{"entry":..., "key":..., "field":..., "op":..., "new":...}]}

Phase 1 covers parsing the file into ``Format3Intent`` objects and
classifying each intent as **supported** (we know how to write it)
or **skipped** (we don't, with a reason).

The kori228 example mod (issue #41) targets the ``drops`` array
which is a variable-length record list — we cannot apply that in
Phase 1. Tests pin that we surface every skipped intent with a
clear reason instead of silently dropping them.

Empirical: every one of the 695 intents in the example uses
``field: "drops"`` and ``op: "set"`` — no flat-field intents in
the headline mod, so Phase 1 won't apply this specific mod, only
later phases will. Tests cover that explicitly so the limitation
is documented.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cdumm.engine.format3_handler import (
    Format3Intent,
    Format3Validation,
    parse_format3_mod,
    validate_intents,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "format3"


# ── parse_format3_mod ───────────────────────────────────────────────


def test_parse_kori228_mod_returns_target_and_695_intents():
    target, intents = parse_format3_mod(
        FIXTURE_DIR / "dropsetinfo_5x_drops.json")
    assert target == "dropsetinfo.pabgb"
    assert len(intents) == 695
    assert all(isinstance(i, Format3Intent) for i in intents)


def test_parse_first_intent_round_trips():
    """First intent in kori228's mod is DropSet_Faction_Graymane."""
    _, intents = parse_format3_mod(
        FIXTURE_DIR / "dropsetinfo_5x_drops.json")
    first = intents[0]
    assert first.entry == "DropSet_Faction_Graymane"
    assert first.key == 175001
    assert first.field == "drops"
    assert first.op == "set"
    assert isinstance(first.new, list)
    assert len(first.new) == 4


def test_parse_rejects_format_2_files(tmp_path):
    """Format 3 detector and parser must agree — only ``format: 3``
    files parse here. A v2 byte-patch file should raise."""
    import json
    p = tmp_path / "v2.json"
    p.write_text(json.dumps({"target": "x.pabgb", "patches": []}),
                 encoding="utf-8")
    with pytest.raises(ValueError, match="Format 3"):
        parse_format3_mod(p)


def test_parse_rejects_missing_target(tmp_path):
    import json
    p = tmp_path / "noTarget.json"
    p.write_text(json.dumps({"format": 3, "intents": []}),
                 encoding="utf-8")
    with pytest.raises(ValueError, match="target"):
        parse_format3_mod(p)


def test_parse_rejects_intents_missing_required_keys(tmp_path):
    """Each intent needs entry, field, op, new at minimum."""
    import json
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({
        "format": 3,
        "target": "x.pabgb",
        "intents": [{"field": "x"}],   # no entry, no op, no new
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="intent"):
        parse_format3_mod(p)


def test_parse_rejects_intent_missing_key(tmp_path):
    """``key`` is the record id — silently defaulting it to 0
    silently nukes the wrong record. Treat it like every other
    required field and raise, so the mod author sees the bad
    intent and fixes it."""
    import json
    p = tmp_path / "no_key.json"
    p.write_text(json.dumps({
        "format": 3,
        "target": "x.pabgb",
        "intents": [{"entry": "X", "field": "y",
                     "op": "set", "new": 42}],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="key"):
        parse_format3_mod(p)


def test_parse_rejects_intent_with_non_int_key(tmp_path):
    """A string key like ``"175001"`` is a common authoring mistake.
    Silently truncating or crashing with int(...) ValueError is bad
    UX — surface a clear ValueError naming the intent."""
    import json
    p = tmp_path / "str_key.json"
    p.write_text(json.dumps({
        "format": 3,
        "target": "x.pabgb",
        "intents": [{"entry": "X", "key": "175001",
                     "field": "y", "op": "set", "new": 42}],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="key"):
        parse_format3_mod(p)


def test_parse_rejects_intent_with_float_key(tmp_path):
    """Floats silently truncate via int() — and a ``175000.5`` would
    map to record 175000, the wrong record. Refuse explicitly."""
    import json
    p = tmp_path / "float_key.json"
    p.write_text(json.dumps({
        "format": 3,
        "target": "x.pabgb",
        "intents": [{"entry": "X", "key": 175001.5,
                     "field": "y", "op": "set", "new": 42}],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="key"):
        parse_format3_mod(p)


# ── validate_intents — supported vs skipped split ───────────────────


def test_validate_kori228_drops_intents_all_supported():
    """All 695 intents in the example target `drops` on
    `dropsetinfo.pabgb`. With the dropset_writer module registered
    in `LIST_WRITERS`, the validator must now classify these as
    supported (not skipped). The apply-time expander dispatches each
    intent to `build_drops_replacement_change` and emits a
    record-replacement byte change.

    Previous behavior: all 695 skipped with a "list-of-dicts coming
    in v3.3" message (because writer-side support was missing).
    """
    target, intents = parse_format3_mod(
        FIXTURE_DIR / "dropsetinfo_5x_drops.json")
    result = validate_intents(target, intents)
    assert isinstance(result, Format3Validation)
    assert len(result.supported) == 695, (
        f"All 695 drops intents on dropsetinfo should be supported, "
        f"got {len(result.supported)} supported / "
        f"{len(result.skipped)} skipped")
    assert len(result.skipped) == 0


def test_validate_unknown_target_table_skips_everything():
    """If the target .pabgb name doesn't match any of the 434
    known schemas, every intent must be skipped — we have no way
    to validate field names without a schema."""
    intents = [Format3Intent(
        entry="X", key=1, field="anything", op="set", new=42)]
    result = validate_intents("notarealtable.pabgb", intents)
    assert len(result.supported) == 0
    assert len(result.skipped) == 1
    _, reason = result.skipped[0]
    assert "schema" in reason.lower()


def test_validate_supported_flat_field_intent_is_kept():
    """An intent whose ``field`` exactly matches a schema field
    name with a known direct_* type and stream size > 0 is
    classified as supported."""
    # dropsetinfo._dropRollCount is direct_u32, stream=4
    intents = [Format3Intent(
        entry="DropSet_Faction_Graymane", key=175001,
        field="_dropRollCount", op="set", new=3)]
    result = validate_intents("dropsetinfo.pabgb", intents)
    assert len(result.supported) == 1
    assert len(result.skipped) == 0


def test_validate_unknown_field_name_skipped_with_helpful_reason():
    """When the intent's ``field`` doesn't match any schema field,
    skip and say so — and include a hint that this is the friendly-
    name → schema-name mapping gap."""
    intents = [Format3Intent(
        entry="DropSet_Faction_Graymane", key=175001,
        field="madeupField", op="set", new=42)]
    result = validate_intents("dropsetinfo.pabgb", intents)
    assert len(result.supported) == 0
    assert len(result.skipped) == 1
    _, reason = result.skipped[0]
    assert "madeupField" in reason or "field" in reason.lower()


def test_validate_unsupported_op_is_skipped():
    """Phase 1 only supports op='set'. add_entry, remove, append,
    etc. must skip with a reason naming the unsupported op."""
    intents = [Format3Intent(
        entry="DropSet_Faction_Graymane", key=175001,
        field="_dropRollCount", op="add_entry", new=3)]
    result = validate_intents("dropsetinfo.pabgb", intents)
    assert len(result.supported) == 0
    assert len(result.skipped) == 1
    _, reason = result.skipped[0]
    assert "add_entry" in reason or "op" in reason.lower()


def test_validate_variable_length_field_skipped_with_phase_note():
    """The schema has _list (variable-length array, stream=None).
    Even if a future translation layer maps 'drops' → '_list',
    Phase 1 can't write variable-length data. Skip with a reason
    that mentions the limitation, not just 'unsupported'."""
    intents = [Format3Intent(
        entry="DropSet_Faction_Graymane", key=175001,
        field="_list", op="set", new=[])]
    result = validate_intents("dropsetinfo.pabgb", intents)
    assert len(result.supported) == 0
    assert len(result.skipped) == 1
    _, reason = result.skipped[0]
    # The reason should make clear it's not a generic "unsupported"
    # but specifically about variable-length / array fields
    assert ("array" in reason.lower()
            or "variable" in reason.lower()
            or "length" in reason.lower())


def test_validate_mixed_supported_and_skipped_partitions_correctly():
    """A mod with one applicable + one inapplicable intent must
    return both lists populated. We use a field with NO writer to
    force a skip (`madeupField` for the unsupported case;
    `dropsetinfo.drops` is now supported via dropset_writer)."""
    intents = [
        Format3Intent(entry="X", key=1,
                      field="_dropRollCount", op="set", new=5),
        Format3Intent(entry="X", key=1,
                      field="madeupField", op="set", new=42),
    ]
    result = validate_intents("dropsetinfo.pabgb", intents)
    assert len(result.supported) == 1
    assert len(result.skipped) == 1
    assert result.supported[0].field == "_dropRollCount"
    assert result.skipped[0][0].field == "madeupField"


# ── Format3Validation summary ───────────────────────────────────────


def test_validator_skips_intent_when_writer_cannot_resolve_offset(
        monkeypatch):
    """The validator and writer share field-resolution logic; if
    a field is reachable in the PABGB schema by name but its byte
    offset can't be computed (because a preceding field has
    stream_size=None / variable-length), the validator MUST mark
    it as skipped — otherwise the writer silently no-ops at apply
    time and the user sees 'intent ready' followed by no change.

    Synthesize a table where the second flat field is preceded by
    a variable-length field. Both fields are in the loaded schema,
    but only the first is reachable for writing.
    """
    from cdumm.semantic import parser as parser_mod
    from cdumm.semantic.parser import FieldSpec, TableSchema

    schema = TableSchema(table_name="resolvetest", fields=[
        FieldSpec(name="_first", stream_size=4,
                  field_type="direct_u32", struct_fmt="I"),
        # Variable-length blocks the offset walk
        FieldSpec(name="_blocker", stream_size=0,
                  field_type="CString", struct_fmt=None),
        FieldSpec(name="_after", stream_size=4,
                  field_type="direct_u32", struct_fmt="I"),
    ])
    parser_mod._load_schemas()
    cache = dict(parser_mod._loaded_schemas or {})
    cache["resolvetest"] = schema
    monkeypatch.setattr(parser_mod, "_loaded_schemas", cache)

    # _first is reachable
    result_first = validate_intents("resolvetest.pabgb", [
        Format3Intent(entry="X", key=1, field="_first",
                      op="set", new=1)])
    assert len(result_first.supported) == 1, (
        "first flat field at payload offset 0 is reachable")

    # _after is NOT reachable — preceded by a variable-length field
    result_after = validate_intents("resolvetest.pabgb", [
        Format3Intent(entry="X", key=1, field="_after",
                      op="set", new=1)])
    assert len(result_after.skipped) == 1, (
        "field after a variable-length field can't have its offset "
        "computed at write time, so validator must skip it too")
    _, reason = result_after.skipped[0]
    assert "variable" in reason.lower() or "preceding" in reason.lower()


def test_validation_summary_text_lists_skip_reasons():
    """`Format3Validation.summary()` must produce a human-readable,
    deterministic block listing the skip count and the distinct
    reasons. UI uses this directly. Use fields that ARE skipped
    (no writer registered) to drive the summary path."""
    intents = [
        Format3Intent(entry="X", key=1, field="madeupFieldA",
                      op="set", new=0),
        Format3Intent(entry="X", key=2, field="madeupFieldA",
                      op="set", new=0),
        Format3Intent(entry="X", key=3, field="madeupFieldB",
                      op="set", new=0),
    ]
    result = validate_intents("dropsetinfo.pabgb", intents)
    summary = result.summary()
    assert "3" in summary  # the count
    assert "skipped" in summary.lower()
    assert "madeupField" in summary  # mentions an offending field
