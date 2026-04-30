"""Mixed primitive + list-of-dict intents on the same iteminfo
record must compose correctly.

Hypothesis: when a Format 3 mod has BOTH a primitive intent (e.g.
`is_blocked`) AND a list intent (e.g. `enchant_data_list`) on
iteminfo, the apply pipeline emits TWO changes:
  1. A primitive change at some offset > 0 (entry-anchored).
  2. A whole-file change at offset 0.
The whole-file change applies first (lower offset wins the sort),
overwriting the entire buffer. When the primitive change tries to
match its `original` bytes against the new buffer, it mismatches
and silently skips. Net effect: primitive intent is lost.

Test pins the FAILURE mode so the fix is verifiable.
"""
from __future__ import annotations
from pathlib import Path

import pytest


_VANILLA_ITEMINFO = Path(r"C:\Users\faisa\AppData\Local\Temp\iteminfo.pabgb")
_VANILLA_PABGH = Path(r"C:\Users\faisa\AppData\Local\Temp\iteminfo.pabgh")


def _have() -> bool:
    return _VANILLA_ITEMINFO.exists() and _VANILLA_PABGH.exists()


@pytest.mark.skipif(not _have(), reason="vanilla extracts not present")
def test_mixed_primitive_and_list_intents_compose():
    """A mod with both a primitive AND a list intent on the same
    iteminfo record must end up with both changes reflected in the
    final bytes, not silently lose the primitive."""
    from cdumm.engine.format3_handler import (
        Format3Intent, validate_intents,
    )
    from cdumm.engine.format3_apply import _intents_to_v2_changes
    from cdumm.engine.json_patch_handler import _apply_byte_patches
    from cdumm.engine.crimson_rs_loader import get_crimson_rs

    crimson_rs = get_crimson_rs()
    if crimson_rs is None:
        pytest.skip("crimson_rs not loadable")

    pabgb = _VANILLA_ITEMINFO.read_bytes()
    pabgh = _VANILLA_PABGH.read_bytes()
    items = crimson_rs.parse_iteminfo_from_bytes(pabgb)
    target = next(it for it in items if it.get("enchant_data_list"))
    target_key = target["key"]
    orig_is_blocked = target["is_blocked"]
    new_is_blocked = 1 - orig_is_blocked  # toggle 0<->1

    intents = [
        # Primitive: flip is_blocked
        Format3Intent(
            entry=target.get("string_key", ""), key=target_key,
            field="is_blocked", op="set", new=new_is_blocked,
        ),
        # List: replace enchant_data_list
        Format3Intent(
            entry=target.get("string_key", ""), key=target_key,
            field="enchant_data_list", op="set",
            new=[{
                "level": 0,
                "enchant_stat_data": {
                    "max_stat_list": [], "regen_stat_list": [],
                    "stat_list_static": [], "stat_list_static_level": [],
                },
                "buy_price_list": [], "equip_buffs": [],
            }],
        ),
    ]
    validation = validate_intents("iteminfo.pabgb", intents)
    assert len(validation.supported) == 2, (
        f"Both intents must validate, got "
        f"supported={len(validation.supported)} "
        f"skipped={len(validation.skipped)}: {validation.skipped}")

    changes = _intents_to_v2_changes(
        "iteminfo.pabgb", pabgb, pabgh, validation.supported)
    # Apply through the byte patcher
    modified = bytearray(pabgb)
    applied, mismatched, _ = _apply_byte_patches(
        modified, changes, signature=None, vanilla_data=pabgb)

    # Parse the result and verify BOTH the primitive AND the list change
    # landed on the target record.
    new_items = crimson_rs.parse_iteminfo_from_bytes(bytes(modified))
    new_by_key = {it["key"]: it for it in new_items}
    target_after = new_by_key[target_key]
    assert target_after["is_blocked"] == new_is_blocked, (
        f"Primitive intent (is_blocked: {orig_is_blocked} -> "
        f"{new_is_blocked}) did NOT land. Got: "
        f"{target_after['is_blocked']}. "
        f"applied={applied}, mismatched={mismatched}")
    assert len(target_after["enchant_data_list"]) == 1
