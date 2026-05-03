"""When a Format 3 mod sets `field=drops` on a DropSet that has
tagged-extra trailing bytes (`unk4=7` → 28-byte `friendly_data`,
`unk4=10` → u32 extra, `unk4=13` → u8 extra), the dropset writer's
intent-to-ItemDrop conversion was defaulting `unk4` to 0 and
`friendly_data` to None instead of copying from the template
record. The serialized record was the wrong byte length and the
next record's bytes shifted into the wrong slot, so DropSet_Friendly_Talk
mods (Trust Me workalike) produced 0 friendship gain in-game
instead of the modded amount.

Source: kori228's GitHub #58 report 2026-05-01.

Fix: `_drop_dict_to_item_drop` must fall back to template.unk4,
template.extra_u8, template.extra_u32, and template.friendly_data
when those keys aren't in the JSON dict, the same way it already
does for unk3, unk_cond_flag, unk_post_cond, etc.
"""
from __future__ import annotations
import pytest


def _make_friendly_template():
    from cdumm.engine.dropset_writer import ItemDrop
    return ItemDrop(
        flag=1,
        item_key=12345,
        unk3=0,
        unk4=7,                                  # the tagged-extras flag
        unk1_flag=b"\x01\x02\x03\x04\x05",
        unk_cond_flag=0xFFFFFFFF,
        unk_post_cond=0,
        rates=1000000,
        rates_100=100,
        unk2=0,
        max_amt=5,
        min_amt=5,
        unk3_flags=0xFFFF,
        item_key_dup=12345,
        extra_u8=None,
        extra_u32=None,
        friendly_data=b"FRIENDSHIP_28_BYTE_DATA__OK!",  # exactly 28 bytes
    )


def test_drop_dict_preserves_unk4_from_template_when_absent():
    """When the JSON intent omits `unk4`, the ItemDrop must inherit
    template.unk4 (= 7 for friendship entries), not default to 0."""
    from cdumm.engine.dropset_writer import _drop_dict_to_item_drop

    template = _make_friendly_template()
    intent_dict = {
        "item_key": 0,
        "rates": 1000000,
        "rates_100": 100,
        "min_amt": 100,
        "max_amt": 100,
    }
    drop = _drop_dict_to_item_drop(intent_dict, template=template)
    assert drop.unk4 == 7, (
        f"unk4 = {drop.unk4}, expected 7 from template. The writer "
        f"was defaulting to 0, which loses the tagged-extras trailer "
        f"on serialize and corrupts every subsequent record."
    )


def test_drop_dict_preserves_friendly_data_from_template_when_absent():
    """JSON intent omits `friendly_data` — must inherit from template."""
    from cdumm.engine.dropset_writer import _drop_dict_to_item_drop

    template = _make_friendly_template()
    intent_dict = {
        "item_key": 0,
        "rates": 1000000,
        "rates_100": 100,
        "min_amt": 100,
        "max_amt": 100,
    }
    drop = _drop_dict_to_item_drop(intent_dict, template=template)
    assert drop.friendly_data == template.friendly_data, (
        f"friendly_data lost. The 28-byte friendship trailer must be "
        f"preserved when not specified in the intent."
    )


def test_drop_dict_preserves_extra_u8_and_u32_from_template():
    """unk4=13 (extra_u8) and unk4=10 (extra_u32) tagged extras must
    also fall back to template values."""
    from cdumm.engine.dropset_writer import ItemDrop, _drop_dict_to_item_drop

    template_u8 = ItemDrop(
        flag=1, item_key=99, unk4=13,
        unk1_flag=b"\x00" * 5,
        extra_u8=42,
    )
    drop = _drop_dict_to_item_drop({"item_key": 0}, template=template_u8)
    assert drop.unk4 == 13
    assert drop.extra_u8 == 42

    template_u32 = ItemDrop(
        flag=1, item_key=99, unk4=10,
        unk1_flag=b"\x00" * 5,
        extra_u32=0xDEADBEEF,
    )
    drop = _drop_dict_to_item_drop({"item_key": 0}, template=template_u32)
    assert drop.unk4 == 10
    assert drop.extra_u32 == 0xDEADBEEF


def test_drop_dict_explicit_value_wins_over_template():
    """If the JSON dict explicitly sets a tagged-extra, that value
    wins; the template is only the fallback."""
    from cdumm.engine.dropset_writer import _drop_dict_to_item_drop

    template = _make_friendly_template()  # unk4=7
    intent_dict = {"item_key": 0, "unk4": 13, "extra_u8": 99}
    drop = _drop_dict_to_item_drop(intent_dict, template=template)
    assert drop.unk4 == 13
    assert drop.extra_u8 == 99
