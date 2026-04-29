"""Format 3 field-name lookup: snake_case → camelCase fallback.

Round-5 systematic-debugging finding (2026-04-29): Matrixz's mod
ships intents with field names like ``gimmick_info`` and
``item_charge_type``. The Pearl Abyss schema stores those same
fields as ``_gimmickInfo`` and ``_itemChargeType`` (camelCase +
underscore prefix). The validator's existing prefix-fallback
(json_patch_handler.py:322-325) tries:

  1. ``field_specs.get("gimmick_info")``    → None
  2. ``field_specs.get("_gimmick_info")``   → None

Neither matches ``_gimmickInfo``. Result: the validator rejects
these PRIMITIVE intents with a "field has no schema entry"
message — even though the field is fully described in the schema
overrides.

The earlier ``cooltime`` → ``_cooltime`` fix (commit 7c9fb05)
handled the underscore-prefix mismatch. This adds the snake_case
→ camelCase normalization on top, so the four-shape lookup is:

  exact / +underscore / +camelCase / +underscore+camelCase
"""
from __future__ import annotations


def test_snake_case_to_camel_case_helper_exists():
    """A helper that normalizes ``foo_bar_baz`` → ``fooBarBaz``
    so the lookup can chain it with the underscore-prefix step."""
    from cdumm.engine.format3_handler import _snake_to_camel
    assert _snake_to_camel("gimmick_info") == "gimmickInfo"
    assert _snake_to_camel("item_charge_type") == "itemChargeType"
    assert _snake_to_camel("unk_post_cooltime_a") == "unkPostCooltimeA"
    # Already camelCase — unchanged
    assert _snake_to_camel("cooltime") == "cooltime"
    # Single underscore at start should be stripped before camelCase,
    # but the helper accepts either; caller composes prefix separately
    assert _snake_to_camel("foo_bar") == "fooBar"


def test_validator_resolves_snake_case_primitive_to_camel_schema():
    """The Matrixz case: intent.field='gimmick_info' must resolve
    against schema entry '_gimmickInfo'."""
    from cdumm.engine.format3_handler import validate_intents, Format3Intent

    intents = [
        Format3Intent(entry="Lantern", key=10026,
                      field="gimmick_info", op="set", new=1002041),
        Format3Intent(entry="Lantern", key=10026,
                      field="item_charge_type", op="set", new=0),
    ]
    result = validate_intents("iteminfo.pabgb", intents)
    skipped_fields = [i.field for i, _ in result.skipped]
    assert "gimmick_info" not in skipped_fields, (
        f"gimmick_info must resolve via snake_case→camelCase. "
        f"Skipped: {skipped_fields}")
    assert "item_charge_type" not in skipped_fields, (
        f"item_charge_type must resolve via snake_case→camelCase. "
        f"Skipped: {skipped_fields}")
    assert len(result.supported) == 2


def test_existing_underscore_prefix_fallback_still_works():
    """Regression guard: the earlier fix that resolves ``cooltime``
    → ``_cooltime`` must still work after this change."""
    from cdumm.engine.format3_handler import validate_intents, Format3Intent

    intents = [
        Format3Intent(entry="Lantern", key=10026,
                      field="cooltime", op="set", new=1),
    ]
    result = validate_intents("iteminfo.pabgb", intents)
    assert len(result.supported) == 1, (
        f"cooltime → _cooltime fallback regressed. "
        f"Skipped: {[(i.field, r) for i, r in result.skipped]}")


def test_camelcase_intent_also_resolves_directly():
    """If a mod author already uses camelCase (rare, but possible),
    the lookup must still work — the prefix-only fallback gets it."""
    from cdumm.engine.format3_handler import validate_intents, Format3Intent

    intents = [
        Format3Intent(entry="Lantern", key=10026,
                      field="gimmickInfo", op="set", new=1002041),
    ]
    result = validate_intents("iteminfo.pabgb", intents)
    assert len(result.supported) == 1
