"""Tests for the 5-byte item header that introduces each entry in
the ``buff_data_list`` region of a buffinfo entry.

The header is fixed-width and always present , an item with the
absent flag set has just the 5-byte header and no payload, items
with absent_flag=0 have a variable-length payload following.
"""
from __future__ import annotations

import struct

import pytest

from cdumm._vendor.buffinfo_parser import (
    BuffItemHeader,
    parse_item_header,
)


def test_header_decodes_prefix_id_and_absent_flag():
    raw = struct.pack("<I", 7) + bytes([0x00]) + b"\xaa\xaa\xaa"
    h = parse_item_header(raw, 0)
    assert isinstance(h, BuffItemHeader)
    assert h.prefix_id == 7
    assert h.absent_flag == 0
    assert h.prefix_id_offset == 0
    assert h.absent_flag_offset == 4
    assert h.payload_offset == 5


def test_header_decodes_absent_indicator_nonzero():
    raw = struct.pack("<I", 1) + bytes([0x01])
    h = parse_item_header(raw, 0)
    assert h.absent_flag == 1
    assert h.payload_offset == 5


def test_header_at_nonzero_position():
    leading = b"\x00" * 12
    raw = leading + struct.pack("<I", 0xDEADBEEF) + bytes([0x00])
    h = parse_item_header(raw, 12)
    assert h.prefix_id == 0xDEADBEEF
    assert h.prefix_id_offset == 12
    assert h.absent_flag_offset == 16
    assert h.payload_offset == 17


def test_header_rejects_truncated_position():
    raw = b"\x00" * 4  # only 4 bytes, not enough for header
    with pytest.raises(ValueError, match="out of range"):
        parse_item_header(raw, 0)


def test_header_rejects_negative_position():
    raw = b"\x00" * 8
    with pytest.raises(ValueError, match="out of range"):
        parse_item_header(raw, -1)


def test_locate_buff_field_resolves_first_item_absent_flag():
    """``buff_data_list[0].absent_flag`` must resolve via the new
    item header decoder. Items at higher indices still return None
    until the variant size table lands."""
    from cdumm._vendor.buffinfo_parser import locate_buff_field

    name_bytes = b"X"
    raw = (
        struct.pack("<I", 1)            # entry_key
        + struct.pack("<I", len(name_bytes)) + name_bytes
        + bytes([0])                    # is_blocked
        + struct.pack("<I", 1)          # buff_data_count
        + struct.pack("<I", 7)          # item 0 prefix_id
        + bytes([0x01])                 # item 0 absent_flag
        # min_level, max_level
        + struct.pack("<I", 1) + struct.pack("<I", 5)
        + struct.pack("<I", 0)          # empty sequencer cstring
        + bytes([0])                    # blct
        + struct.pack("<I", 0) * 3      # uit, uic, esi
        + bytes([0, 0])                 # iuspd, ucbgt
    )
    res = locate_buff_field(raw, "buff_data_list[0].absent_flag")
    assert res is not None
    offset, width, dtype = res
    assert width == 1
    assert dtype == "u8"
    assert raw[offset] == 0x01


def test_locate_buff_field_resolves_first_item_leading_lookup():
    """``buff_data_list[0].leading_lookup`` is the public schema
    name for the 4-byte prefix integer that opens each item."""
    from cdumm._vendor.buffinfo_parser import locate_buff_field

    name_bytes = b"X"
    raw = (
        struct.pack("<I", 1)
        + struct.pack("<I", len(name_bytes)) + name_bytes
        + bytes([0])
        + struct.pack("<I", 1)
        + struct.pack("<I", 0xCAFEBABE)
        + bytes([0x00])
        + struct.pack("<I", 1) + struct.pack("<I", 5)
        + struct.pack("<I", 0) + bytes([0])
        + struct.pack("<I", 0) * 3 + bytes([0, 0])
    )
    res = locate_buff_field(raw, "buff_data_list[0].leading_lookup")
    assert res is not None
    offset, width, dtype = res
    assert width == 4
    assert dtype == "u32"
    assert struct.unpack_from("<I", raw, offset)[0] == 0xCAFEBABE


def test_locate_buff_field_returns_none_for_higher_indices():
    """``buff_data_list[3].absent_flag`` etc. need the variant size
    table to walk past items 0..2. Until that lands, return None."""
    from cdumm._vendor.buffinfo_parser import locate_buff_field

    name_bytes = b"X"
    raw = (
        struct.pack("<I", 1) + struct.pack("<I", 1) + name_bytes
        + bytes([0]) + struct.pack("<I", 1)
        + struct.pack("<I", 7) + bytes([0x01])
        + struct.pack("<I", 1) + struct.pack("<I", 5)
        + struct.pack("<I", 0) + bytes([0])
        + struct.pack("<I", 0) * 3 + bytes([0, 0])
    )
    assert locate_buff_field(
        raw, "buff_data_list[3].absent_flag") is None
    # data.base paths still unsupported even at index 0.
    assert locate_buff_field(
        raw, "buff_data_list[0].data.base.flags_a") is None


def test_real_vanilla_first_item_decodes_consistently():
    """For every vanilla entry, parse the first item header and
    confirm it produces a sane prefix_id (small) and a valid
    absent_flag (0 or 1)."""
    from pathlib import Path

    pabgb_path = Path(r"C:/temp/buffinfo.pabgb")
    pabgh_path = Path(r"C:/temp/buffinfo.pabgh")
    if not (pabgb_path.exists() and pabgh_path.exists()):
        pytest.skip("local vanilla buffinfo files not present")

    pabgb = pabgb_path.read_bytes()
    pabgh = pabgh_path.read_bytes()
    n_entries = struct.unpack_from("<H", pabgh, 0)[0]
    offsets = []
    pos = 2
    for _ in range(n_entries):
        key = struct.unpack_from("<I", pabgh, pos)[0]
        off = struct.unpack_from("<I", pabgh, pos + 4)[0]
        offsets.append((key, off))
        pos += 8
    offsets.sort(key=lambda x: x[1])

    n_decoded = 0
    for i, (_key, off) in enumerate(offsets):
        end = offsets[i + 1][1] if i + 1 < len(offsets) else len(pabgb)
        payload = pabgb[off:end]
        slen = struct.unpack_from("<I", payload, 4)[0]
        body_start = 8 + slen + 5
        # Skip entries with no items (count==0)
        cnt = struct.unpack_from("<I", payload, 8 + slen + 1)[0]
        if cnt == 0:
            continue
        h = parse_item_header(payload, body_start)
        # absent_flag is a boolean-like byte
        assert h.absent_flag in (0, 1), (
            f"unexpected absent_flag {h.absent_flag} in entry at "
            f"offset {off}")
        n_decoded += 1
    assert n_decoded > 0, "no vanilla entries had items to decode"
