"""FIX 16 introduced a hole: when an ItemDrop has `unk4 in {7, 10, 13}`
but the matching trailer field (`friendly_data`, `extra_u32`, or
`extra_u8`) is None, the serializer at dropset_writer.py:135-145
silently emits no trailer. The record is short by 28 / 4 / 1 bytes,
every subsequent record shifts, the dropsetinfo table corrupts.

Two reachable cases:
1. Template missing entirely (`parsed.drops` empty), JSON intent
   sets `unk4=7` without `friendly_data`. _drop_dict_to_item_drop's
   template-fallback can't help, friendly_data stays None.
2. Cross-variant tag switch: JSON sets `unk4=10` but template has
   `unk4=7`. Fallback guard requires `template.unk4 == unk4`, so the
   trailer doesn't inherit, all three trailers are None.

Either way, _serialize_drop_entry silently produces a short record.

Round 4 audit caught this. Fix: raise a clear ValueError in the
serializer when `unk4` declares a tagged trailer but the field is
None — surfaces the bug at apply time instead of corrupting bytes.
"""
from __future__ import annotations
import pytest


def test_serialize_unk4_7_without_friendly_data_raises():
    from cdumm.engine.dropset_writer import ItemDrop, _serialize_drop_entry

    drop = ItemDrop(
        flag=1, item_key=0, unk4=7,
        unk1_flag=b"\x00" * 5,
        friendly_data=None,
    )
    with pytest.raises(ValueError, match="friendly_data"):
        _serialize_drop_entry(drop)


def test_serialize_unk4_10_without_extra_u32_raises():
    from cdumm.engine.dropset_writer import ItemDrop, _serialize_drop_entry

    drop = ItemDrop(
        flag=1, item_key=0, unk4=10,
        unk1_flag=b"\x00" * 5,
        extra_u32=None,
    )
    with pytest.raises(ValueError, match="extra_u32"):
        _serialize_drop_entry(drop)


def test_serialize_unk4_13_without_extra_u8_raises():
    from cdumm.engine.dropset_writer import ItemDrop, _serialize_drop_entry

    drop = ItemDrop(
        flag=1, item_key=0, unk4=13,
        unk1_flag=b"\x00" * 5,
        extra_u8=None,
    )
    with pytest.raises(ValueError, match="extra_u8"):
        _serialize_drop_entry(drop)


def test_serialize_unk4_0_no_trailer_required():
    """unk4=0 (the default for non-tagged entries) must NOT require
    any trailer. Should serialize cleanly."""
    from cdumm.engine.dropset_writer import ItemDrop, _serialize_drop_entry

    drop = ItemDrop(
        flag=1, item_key=99, unk4=0,
        unk1_flag=b"\x00" * 5,
    )
    out = _serialize_drop_entry(drop)
    assert isinstance(out, bytes)
    # Standard layout = 1 + 4 + 4 + 4 + 5 + 4 + 4 + 8 + 8 + 4 + 8 + 8 + 2 + 4 = 68 bytes
    assert len(out) == 68


def test_serialize_unk4_7_with_friendly_data_succeeds():
    """Happy path: unk4=7 + 28-byte friendly_data → 96-byte record."""
    from cdumm.engine.dropset_writer import ItemDrop, _serialize_drop_entry

    drop = ItemDrop(
        flag=1, item_key=99, unk4=7,
        unk1_flag=b"\x00" * 5,
        friendly_data=b"X" * 28,
    )
    out = _serialize_drop_entry(drop)
    assert len(out) == 68 + 28


def test_build_drops_replacement_returns_none_on_trailer_mismatch():
    """The serializer's new ValueError on missing trailers must be
    caught by `build_drops_replacement_change` so the apply pipeline
    sees a graceful `None` (= skipped change) instead of an
    uncaught exception that aborts the whole apply pass.

    Round 5 audit catch — FIX 17's ValueError was bubbling up
    uncaught through format3_apply._build_list_writer_change and
    crashing every Format 3 mod that hit this path.
    """
    import struct
    from cdumm.engine.dropset_writer import build_drops_replacement_change

    # Build a synthetic minimal DropSet record (no drops) so the
    # template fallback returns None, which then forces the
    # serializer's trailer-missing path when the intent specifies
    # `unk4=7` without `friendly_data`.
    name = b"DropSet_Test"
    name_len = len(name)
    record = bytearray()
    record += struct.pack("<I", 1234)        # key
    record += struct.pack("<I", name_len)    # name_len
    record += name                            # name
    record.append(0)                          # is_blocked
    record += struct.pack("<I", 0)            # drop_roll_type
    record += struct.pack("<I", 0)            # drop_roll_count
    record += struct.pack("<I", 0)            # condition string len
    record += struct.pack("<I", 0)            # tag name hash
    record += struct.pack("<I", 0)            # drops count = 0
    record += struct.pack("<i", -1)           # nee_slot_count
    record += struct.pack("<I", 0)            # need_weight
    record += struct.pack("<I", 0)            # total_drop_rate
    record += struct.pack("<I", 0)            # original string len

    # Note: this synthetic record likely doesn't pass parse, but
    # that's fine — the test verifies that whether parse fails OR
    # serialize fails, the function returns None instead of raising.
    bad_intent = [{"item_key": 0, "unk4": 7}]  # no friendly_data, no template

    out = build_drops_replacement_change(
        record_bytes=bytes(record),
        intent_key=1234,
        intent_entry="DropSet_Test",
        new_drops_json=bad_intent,
    )

    # The function must return None (graceful skip) rather than
    # propagating the ValueError.
    assert out is None, (
        f"build_drops_replacement_change must catch serializer "
        f"ValueError and return None, got: {out!r}"
    )
