"""Tests for the BuffPayloadCommon decoder (28-field common prefix
that follows each present buff_data item's 5-byte header).

Validates field-by-field decoding plus offset annotations against
synthetic and real-world buffinfo data.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from cdumm._vendor.buffinfo_parser import (
    BuffPayloadCommon,
    locate_buff_field,
    parse_payload_common,
)


def _build_payload_bytes(
    *,
    tag: int = 1,
    id_val: int = 0x10000001,
    name_id: int = 0x20000002,
    flags_a: int = 1,
    flags_b: int = 2,
    qword_a: int = 0xAAAAAAAAAAAAAAAA,
    qword_b: int = 0xBBBBBBBBBBBBBBBB,
    qword_c: int = 0xCCCCCCCCCCCCCCCC,
    asset_path: str = "fx/assets/test",
    category: int = 7,
    by58: int = 8,
    lookup_a_60: int = 60,
    lookup_b_62: int = 62,
    lookup_c_64: int = 64,
    lookup_d_66: int = 66,
    by68: int = 68,
    by69: int = 69,
    lookup_88: int = 88,
    lookup_90: int = 90,
    first_array: tuple[int, ...] = (),
    u32_at128: int = 128,
    u32_at72: int = 72,
    u32_at76: int = 76,
    u32_at80: int = 80,
    u32_at84: int = 84,
    second_array: tuple[int, ...] = (),
    by132: int = 132,
    u32_at136: int = 136,
) -> bytes:
    name_bytes = asset_path.encode("utf-8")
    out = bytearray()
    out += bytes([tag])
    out += struct.pack("<I", id_val)
    out += struct.pack("<I", name_id)
    out += bytes([flags_a, flags_b])
    out += struct.pack("<Q", qword_a)
    out += struct.pack("<Q", qword_b)
    out += struct.pack("<Q", qword_c)
    out += struct.pack("<I", len(name_bytes)) + name_bytes
    out += struct.pack("<I", category)
    out += bytes([by58])
    out += struct.pack("<I", lookup_a_60)
    out += struct.pack("<I", lookup_b_62)
    out += struct.pack("<I", lookup_c_64)
    out += struct.pack("<I", lookup_d_66)
    out += bytes([by68, by69])
    out += struct.pack("<I", lookup_88)
    out += struct.pack("<I", lookup_90)
    out += struct.pack("<I", len(first_array))
    for v in first_array:
        out += struct.pack("<I", v)
    out += struct.pack("<I", u32_at128)
    out += struct.pack("<I", u32_at72)
    out += struct.pack("<I", u32_at76)
    out += struct.pack("<I", u32_at80)
    out += struct.pack("<I", u32_at84)
    out += struct.pack("<I", len(second_array))
    for v in second_array:
        out += struct.pack("<I", v)
    out += bytes([by132])
    out += struct.pack("<I", u32_at136)
    return bytes(out)


def test_payload_common_decodes_all_28_fields():
    raw = _build_payload_bytes(
        first_array=(11, 22, 33),
        second_array=(99, 88),
    )
    p = parse_payload_common(raw, 0)
    assert isinstance(p, BuffPayloadCommon)
    assert p.tag == 1
    assert p.id == 0x10000001
    assert p.name_id == 0x20000002
    assert p.flags_a == 1
    assert p.flags_b == 2
    assert p.qword_a == 0xAAAAAAAAAAAAAAAA
    assert p.qword_b == 0xBBBBBBBBBBBBBBBB
    assert p.qword_c == 0xCCCCCCCCCCCCCCCC
    assert p.asset_path == "fx/assets/test"
    assert p.category == 7
    assert p.by58 == 8
    assert p.lookup_a_60 == 60
    assert p.lookup_b_62 == 62
    assert p.lookup_c_64 == 64
    assert p.lookup_d_66 == 66
    assert p.by68 == 68
    assert p.by69 == 69
    assert p.lookup_88 == 88
    assert p.lookup_90 == 90
    assert p.first_array == (11, 22, 33)
    assert p.u32_at128 == 128
    assert p.u32_at72 == 72
    assert p.u32_at76 == 76
    assert p.u32_at80 == 80
    assert p.u32_at84 == 84
    assert p.second_array == (99, 88)
    assert p.by132 == 132
    assert p.u32_at136 == 136
    assert p.end_offset == len(raw)


def test_payload_offsets_point_at_correct_bytes():
    raw = _build_payload_bytes()
    p = parse_payload_common(raw, 0)
    assert raw[p.tag_offset] == p.tag
    assert struct.unpack_from("<I", raw, p.id_offset)[0] == p.id
    assert struct.unpack_from(
        "<I", raw, p.name_id_offset)[0] == p.name_id
    assert raw[p.flags_a_offset] == p.flags_a
    assert struct.unpack_from(
        "<Q", raw, p.qword_a_offset)[0] == p.qword_a
    assert struct.unpack_from(
        "<I", raw, p.category_offset)[0] == p.category
    assert raw[p.by58_offset] == p.by58
    assert raw[p.by132_offset] == p.by132
    assert struct.unpack_from(
        "<I", raw, p.u32_at136_offset)[0] == p.u32_at136


def test_payload_handles_empty_asset_path_and_arrays():
    raw = _build_payload_bytes(
        asset_path="", first_array=(), second_array=())
    p = parse_payload_common(raw, 0)
    assert p.asset_path == ""
    assert p.first_array == ()
    assert p.second_array == ()
    assert p.end_offset == len(raw)


def test_payload_offsets_shift_with_asset_path_length():
    """Fields after asset_path must move when its cstring grows."""
    short = _build_payload_bytes(asset_path="x")
    long_p = _build_payload_bytes(asset_path="x" * 50)
    short_p = parse_payload_common(short, 0)
    long_pp = parse_payload_common(long_p, 0)
    assert long_pp.category_offset == short_p.category_offset + 49
    assert long_pp.u32_at136_offset == short_p.u32_at136_offset + 49


def test_payload_at_nonzero_position():
    raw = b"\x99" * 7 + _build_payload_bytes()
    p = parse_payload_common(raw, 7)
    assert p.tag_offset == 7  # offset is absolute to the entry start
    assert raw[p.tag_offset] == p.tag


def test_payload_rejects_truncated_input():
    raw = _build_payload_bytes()[:30]  # missing tail
    with pytest.raises(ValueError):
        parse_payload_common(raw, 0)


def test_locate_buff_field_resolves_data_base_paths():
    """Build an entry with a single present item carrying the
    payload, then resolve every data.base.X path via
    locate_buff_field. Each path must point at the field's byte
    offset and report the right width/dtype."""
    payload = _build_payload_bytes(
        tag=42, id_val=1000, flags_a=3, by58=99, by132=77,
        qword_a=0x123456789ABCDEF0)
    name = b"X"
    raw = (
        struct.pack("<I", 1)
        + struct.pack("<I", len(name)) + name
        + bytes([0])
        + struct.pack("<I", 1)
        # item: 5-byte header (prefix_id + absent_flag=0) + payload
        + struct.pack("<I", 0xCAFEBABE) + bytes([0x00])
        + payload
        # wrapper trailer
        + struct.pack("<I", 1) + struct.pack("<I", 5)
        + struct.pack("<I", 0) + bytes([0])
        + struct.pack("<I", 0) * 3 + bytes([0, 0])
    )
    res = locate_buff_field(raw, "buff_data_list[0].data.base.tag")
    assert res is not None
    off, width, dtype = res
    assert width == 1 and dtype == "u8"
    assert raw[off] == 42

    res = locate_buff_field(raw, "buff_data_list[0].data.base.id")
    assert res is not None
    off, width, dtype = res
    assert struct.unpack_from("<I", raw, off)[0] == 1000

    res = locate_buff_field(
        raw, "buff_data_list[0].data.base.qword_a")
    assert res is not None
    off, width, dtype = res
    assert width == 8 and dtype == "u64"
    assert struct.unpack_from(
        "<Q", raw, off)[0] == 0x123456789ABCDEF0

    res = locate_buff_field(raw, "buff_data_list[0].data.base.by58")
    assert raw[res[0]] == 99

    res = locate_buff_field(raw, "buff_data_list[0].data.base.by132")
    assert raw[res[0]] == 77


def test_locate_buff_field_returns_none_for_absent_item_data_paths():
    """If the item's absent_flag is non-zero there's no payload to
    address , data.base.X paths must return None."""
    name = b"X"
    raw = (
        struct.pack("<I", 1) + struct.pack("<I", 1) + name
        + bytes([0]) + struct.pack("<I", 1)
        + struct.pack("<I", 0) + bytes([0x01])  # absent_flag=1
        + struct.pack("<I", 1) + struct.pack("<I", 5)
        + struct.pack("<I", 0) + bytes([0])
        + struct.pack("<I", 0) * 3 + bytes([0, 0])
    )
    assert locate_buff_field(
        raw, "buff_data_list[0].data.base.tag") is None
    assert locate_buff_field(
        raw, "buff_data_list[0].data.base.flags_a") is None


def test_locate_buff_field_returns_none_for_unknown_base_field():
    payload = _build_payload_bytes()
    name = b"X"
    raw = (
        struct.pack("<I", 1) + struct.pack("<I", 1) + name
        + bytes([0]) + struct.pack("<I", 1)
        + struct.pack("<I", 0) + bytes([0x00]) + payload
        + struct.pack("<I", 1) + struct.pack("<I", 5)
        + struct.pack("<I", 0) + bytes([0])
        + struct.pack("<I", 0) * 3 + bytes([0, 0])
    )
    assert locate_buff_field(
        raw, "buff_data_list[0].data.base.totally_made_up") is None


_VANILLA = Path(r"C:/temp/buffinfo.pabgb")
_VANILLA_PABGH = Path(r"C:/temp/buffinfo.pabgh")


def _vanilla_entries():
    if not (_VANILLA.exists() and _VANILLA_PABGH.exists()):
        return None
    pabgb = _VANILLA.read_bytes()
    pabgh = _VANILLA_PABGH.read_bytes()
    n_entries = struct.unpack_from("<H", pabgh, 0)[0]
    offsets = []
    pos = 2
    for _ in range(n_entries):
        key = struct.unpack_from("<I", pabgh, pos)[0]
        off = struct.unpack_from("<I", pabgh, pos + 4)[0]
        offsets.append((key, off))
        pos += 8
    offsets.sort(key=lambda x: x[1])
    out: dict[int, bytes] = {}
    for i, (_key, off) in enumerate(offsets):
        end = offsets[i + 1][1] if i + 1 < len(offsets) else len(pabgb)
        out[off] = pabgb[off:end]
    return out


def test_locate_buff_field_walks_to_index_one_via_known_variant():
    """Build an entry whose item 0 uses variant tag 17 (zero-byte
    tail per the empirical table), then resolve a path on item 1.
    The walker must skip past item 0's payload and land on item 1
    correctly."""
    payload_tag17 = _build_payload_bytes(tag=17)
    payload_item1 = _build_payload_bytes(tag=80, by58=99)
    name = b"X"
    raw = (
        struct.pack("<I", 1)
        + struct.pack("<I", len(name)) + name
        + bytes([0])
        + struct.pack("<I", 2)  # 2 items
        # item 0: header + payload (tag 17 has 0-byte tail)
        + struct.pack("<I", 0xAA) + bytes([0x00]) + payload_tag17
        # item 1: header + payload (tag 80 has 8-byte tail, but we
        # don't need to walk past it, so any tail works)
        + struct.pack("<I", 0xBB) + bytes([0x00]) + payload_item1
        + b"\x00" * 8  # tag 80 tail bytes
        # wrapper trailer
        + struct.pack("<I", 1) + struct.pack("<I", 5)
        + struct.pack("<I", 0) + bytes([0])
        + struct.pack("<I", 0) * 3 + bytes([0, 0])
    )
    res = locate_buff_field(
        raw, "buff_data_list[1].data.base.by58")
    assert res is not None, (
        "walker should have found item 1 by skipping item 0 "
        "(tag 17 has known 0-byte tail)")
    off, _, _ = res
    assert raw[off] == 99


def test_locate_buff_field_returns_none_when_intermediate_tag_unknown():
    """If item 0 has a tag NOT in the variant size table, we can't
    walk past it , item 1+ paths must return None gracefully."""
    payload_unknown = _build_payload_bytes(tag=200)  # not in table
    payload_item1 = _build_payload_bytes(tag=80)
    name = b"X"
    raw = (
        struct.pack("<I", 1)
        + struct.pack("<I", len(name)) + name
        + bytes([0]) + struct.pack("<I", 2)
        + struct.pack("<I", 0) + bytes([0x00]) + payload_unknown
        + struct.pack("<I", 0) + bytes([0x00]) + payload_item1
        + b"\x00" * 8
        + struct.pack("<I", 1) + struct.pack("<I", 5)
        + struct.pack("<I", 0) + bytes([0])
        + struct.pack("<I", 0) * 3 + bytes([0, 0])
    )
    assert locate_buff_field(
        raw, "buff_data_list[1].data.base.tag") is None


def test_real_vanilla_first_item_payload_decodes():
    """For every vanilla entry whose first item is present, decode
    the BuffPayloadCommon and confirm sane field values + offsets
    that don't fall outside the entry."""
    entries = _vanilla_entries()
    if entries is None:
        pytest.skip("local vanilla buffinfo files not present")
    n_decoded = 0
    for off, payload in entries.items():
        from cdumm._vendor.buffinfo_parser import (
            parse_entry,
            parse_item_header,
        )
        entry = parse_entry(payload)
        if entry.buff_data_count == 0:
            continue
        header = parse_item_header(
            payload, entry.buff_data_list_offset)
        if header.absent_flag != 0:
            continue
        common = parse_payload_common(payload, header.payload_offset)
        # Tag should be small (< 200, 120 known variants)
        assert 0 <= common.tag < 200, (
            f"unexpected tag {common.tag} in entry at {off}")
        # Offsets must be in-range
        assert 0 <= common.tag_offset < len(payload)
        assert 0 <= common.u32_at136_offset < len(payload)
        assert common.end_offset <= len(payload), (
            f"payload end {common.end_offset} overflows entry of "
            f"size {len(payload)} at {off}")
        n_decoded += 1
    assert n_decoded > 0, "no vanilla items had a present payload"
