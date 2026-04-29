"""Format 3 intent with field=None must not crash the validator.

Round-14 systematic-debugging finding: a malformed Format 3 mod
JSON with ``"field": null`` (or missing field key) constructs a
Format3Intent with ``field=None``. The validator at
format3_handler.py:397 then runs ``"_" in intent.field`` which
raises TypeError on None.

Real-world scenario: an author starts editing a Format 3 mod and
forgets to fill in a field name before saving. Or an automated
generator outputs ``null`` instead of skipping the intent. Either
way, dropping the mod into CDUMM should produce a clean per-intent
skip message, not crash the importer.

Fix: treat None / non-string field names as a per-intent skip
("intent has no `field` name set") instead of crashing.
"""
from __future__ import annotations


def test_intent_with_none_field_skips_cleanly():
    """A Format3Intent with field=None must produce a per-intent
    skip reason, not crash."""
    from cdumm.engine.format3_handler import validate_intents, Format3Intent

    intent = Format3Intent(entry="Foo", key=1,
                            field=None, op="set", new=1)
    # Must not raise.
    result = validate_intents("iteminfo.pabgb", [intent])
    assert len(result.supported) == 0
    assert len(result.skipped) == 1
    _, reason = result.skipped[0]
    assert "field" in reason.lower(), (
        f"skip reason should mention the missing field name. "
        f"Got: {reason}")


def test_intent_with_empty_string_field_skips_cleanly():
    """field='' is also malformed — same behavior."""
    from cdumm.engine.format3_handler import validate_intents, Format3Intent

    intent = Format3Intent(entry="Foo", key=1,
                            field="", op="set", new=1)
    result = validate_intents("iteminfo.pabgb", [intent])
    assert len(result.skipped) == 1


def test_intent_with_int_field_skips_cleanly():
    """Defensive: a non-string field type (mod author wrote a
    number by accident) must not crash."""
    from cdumm.engine.format3_handler import validate_intents, Format3Intent

    intent = Format3Intent(entry="Foo", key=1,
                            field=42, op="set", new=1)
    result = validate_intents("iteminfo.pabgb", [intent])
    assert len(result.skipped) == 1


def test_valid_field_still_works():
    """Regression guard: real string field still validates."""
    from cdumm.engine.format3_handler import validate_intents, Format3Intent

    intent = Format3Intent(entry="ThiefGloves", key=1001250,
                            field="cooltime", op="set", new=1)
    result = validate_intents("iteminfo.pabgb", [intent])
    assert len(result.supported) == 1
