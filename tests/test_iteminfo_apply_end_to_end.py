"""End-to-end iteminfo Format 3: synthesize a multi-intent mod
that sets enchant_data_list on multiple records, run the full
expander, apply via _apply_byte_patches, verify the resulting
bytes parse with the target items having the new lists.

Mirrors test_dropset_apply_end_to_end but for iteminfo's whole-
table writer path.
"""
from __future__ import annotations
from pathlib import Path

import pytest

from tests.fixture_loaders import vanilla110_file


_VANILLA_ITEMINFO = vanilla110_file("iteminfo.pabgb")
_VANILLA_PABGH = vanilla110_file("iteminfo.pabgh")



def _crimson_rs_can_parse_fixture() -> bool:
    """The vendored crimson_rs Rust parser predates the committed
    fixtures: it fails on BOTH CD 1.10 and CD 1.13 at offset 0x71
    ("CArray count 15386081 exceeds remaining bytes"). It only ever
    parsed one maintainer's 1.11-era extract, which is why this module
    used to point at C:/Users/faisa/... and skip everywhere else.

    Guard on the real capability rather than a machine-local path, so
    this test re-enables itself the moment crimson_rs is refreshed --
    and states the actual reason until then. CDUMM's own native parser
    handles these tables (6508 records, 0 opaque on 1.13); the Rust
    extension is the stale component here.
    """
    from cdumm.engine.crimson_rs_loader import get_crimson_rs
    rs = get_crimson_rs()
    if rs is None:
        return False
    try:
        rs.parse_iteminfo_from_bytes(_VANILLA_ITEMINFO.read_bytes())
        return True
    except Exception:
        return False


_RS_REASON = ("vendored crimson_rs parser predates the committed fixtures "
              "(fails CD 1.10 and 1.13 alike); CDUMM's native parser is "
              "used everywhere else and is covered by its own tests")

def _have_inputs() -> bool:
    return _VANILLA_ITEMINFO.exists()


@pytest.mark.skipif(not _crimson_rs_can_parse_fixture(),
                    reason=_RS_REASON)
def test_synthetic_iteminfo_mod_applies_and_persists():
    """Apply a synthetic Format 3 mod to vanilla iteminfo, parse the
    output, verify changes round-trip through crimson_rs."""
    from cdumm.engine.crimson_rs_loader import get_crimson_rs
    from cdumm.engine.format3_handler import (
        Format3Intent, validate_intents,
    )
    from cdumm.engine.format3_apply import _intents_to_v2_changes
    from cdumm.engine.json_patch_handler import _apply_byte_patches

    crimson_rs = get_crimson_rs()
    if crimson_rs is None:
        pytest.skip("crimson_rs not loadable")

    vanilla = _VANILLA_ITEMINFO.read_bytes()
    pabgh_bytes = (_VANILLA_PABGH.read_bytes()
                   if _VANILLA_PABGH.exists() else b"")

    items = crimson_rs.parse_iteminfo_from_bytes(vanilla)
    candidates = [it for it in items if it.get("enchant_data_list")][:5]
    assert len(candidates) >= 3, "need at least 3 enchant items in vanilla"

    new_edl = [{
        "level": 0,
        "enchant_stat_data": {
            "max_stat_list": [],
            "regen_stat_list": [],
            "stat_list_static": [],
            "stat_list_static_level": [
                {"stat": 1000011, "change_mb": 99}
            ],
        },
        "buy_price_list": [
            {"key": 1, "price": {"price": 1, "sym_no": 0,
                                  "item_info_wrapper": 1}},
        ],
        "equip_buffs": [],
    }]
    intents = [
        Format3Intent(
            entry=c.get("string_key", ""), key=c["key"],
            field="enchant_data_list", op="set", new=new_edl,
        )
        for c in candidates
    ]

    validation = validate_intents("iteminfo.pabgb", intents)
    assert len(validation.supported) == len(intents), (
        f"All intents must validate, got {len(validation.skipped)} skipped")

    changes = _intents_to_v2_changes(
        "iteminfo.pabgb", vanilla, pabgh_bytes, validation.supported)
    assert len(changes) == 1, (
        f"iteminfo writer must emit ONE whole-table change for "
        f"{len(intents)} intents, got {len(changes)}")
    c = changes[0]
    assert c["offset"] == 0
    assert bytes.fromhex(c["original"]) == vanilla
    new_bytes = bytes.fromhex(c["patched"])
    assert new_bytes != vanilla

    # Verify the new bytes parse back, and the targeted items now have
    # the new enchant_data_list.
    new_items = crimson_rs.parse_iteminfo_from_bytes(new_bytes)
    new_by_key = {it["key"]: it for it in new_items}
    for c_orig in candidates:
        new_item = new_by_key[c_orig["key"]]
        assert len(new_item["enchant_data_list"]) == 1
        edl = new_item["enchant_data_list"][0]
        assert edl["level"] == 0
        assert edl["enchant_stat_data"]["stat_list_static_level"][0]["change_mb"] == 99

    # Apply the change via the byte patcher (signature=None, offset=0
    # path); verify the buffer matches the patched bytes.
    modified = bytearray(vanilla)
    applied, mismatched, _ = _apply_byte_patches(
        modified, changes, signature=None, vanilla_data=vanilla)
    assert mismatched == 0, f"Expected 0 mismatched, got {mismatched}"
    assert applied == 1
    assert bytes(modified) == new_bytes
