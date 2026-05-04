"""Phase 1 tests for the buffinfo.pabgb prefix decoder.

Verified against Crimson Desert v1.05 vanilla (280 entries). The
prefix layout pins:
  [0..3]  entry_key (u32)
  [4..7]  string length (u32)
  [8..N]  UTF-8 name string

Future phases will extend with body decoding, list-item decoding,
and the data.base substructure.
"""
from __future__ import annotations

import struct

import pytest

from cdumm._vendor.buffinfo_parser import (
    BuffinfoEntryHeader,
    locate_buff_field,
    parse_entry_prefix,
)


def _build_entry_with_prefix(key: int, name: str, body: bytes = b"") -> bytes:
    name_bytes = name.encode("utf-8")
    return (
        struct.pack("<I", key)
        + struct.pack("<I", len(name_bytes))
        + name_bytes
        + body
    )


def test_prefix_decodes_key_and_name():
    raw = _build_entry_with_prefix(
        1000062, "BuffLevel_Comma_Symptom", b"\x00" + b"\x03\x00\x00\x00")
    h = parse_entry_prefix(raw)
    assert h.entry_key == 1000062
    assert h.name == "BuffLevel_Comma_Symptom"
    assert h.prefix_end == 8 + len("BuffLevel_Comma_Symptom")


def test_prefix_decodes_isBlocked_and_buff_data_count():
    """Verified across all 280 v1.05 vanilla entries: body+0 is
    _isBlocked (1 byte, always 0) and body+1..+4 is the
    _buffDataList count (u32, observed 1..200)."""
    body = bytes([0x00]) + struct.pack("<I", 7)  # _isBlocked=0, count=7
    raw = _build_entry_with_prefix(42, "Test", body)
    h = parse_entry_prefix(raw)
    assert h.is_blocked == 0
    assert h.buff_data_count == 7
    assert h.is_blocked_offset == 12  # 8 + len("Test")
    assert h.buff_data_count_offset == 13
    assert h.body_start == 17  # 12 + 5


def test_prefix_rejects_implausible_buff_data_count():
    """A misread pointer would land us reading huge values from
    arbitrary bytes. Cap at 10k well above the observed max of 200."""
    body = bytes([0x00]) + struct.pack("<I", 99_999_999)
    raw = _build_entry_with_prefix(1, "X", body)
    with pytest.raises(ValueError, match="implausible buff_data_list"):
        parse_entry_prefix(raw)


def test_prefix_rejects_truncated_body_header():
    raw = _build_entry_with_prefix(1, "X")  # no body bytes at all
    with pytest.raises(ValueError, match="truncated at body header"):
        parse_entry_prefix(raw)


def test_prefix_handles_real_world_first_entry_bytes():
    """Pinned bytes from the actual first entry of vanilla v1.05
    buffinfo.pabgb (extracted via pycrimson's BinaryGameBlob).
    BuffLevel_Comma_Symptom has 3 buff_data items in its list."""
    # entry_key 0x000F427E = 1000062, slen 0x17 = 23, name follows.
    raw = bytes.fromhex(
        "7e420f00"   # entry_key
        "17000000"   # slen=23
        "427566664c6576656c5f436f6d6d615f53796d70746f6d"  # name
        "00"         # _isBlocked = 0
        "03000000"   # _buffDataList count = 3
    )
    h = parse_entry_prefix(raw)
    assert h.entry_key == 1000062
    assert h.name == "BuffLevel_Comma_Symptom"
    assert h.prefix_end == 31
    assert h.is_blocked == 0
    assert h.buff_data_count == 3
    assert h.body_start == 36  # 31 + 1 + 4


def test_prefix_rejects_truncated_entry():
    with pytest.raises(ValueError, match="too short"):
        parse_entry_prefix(b"\x00" * 4)


def test_prefix_rejects_implausible_string_length():
    raw = struct.pack("<I", 1) + struct.pack("<I", 99_999_999) + b"x"
    with pytest.raises(ValueError, match="implausible"):
        parse_entry_prefix(raw)


def test_locate_buff_field_returns_none_for_phase1():
    """Phase 1 has no body walker yet , every field path returns
    None so callers can surface a clear 'not yet applied' skip."""
    raw = _build_entry_with_prefix(1, "X", b"\x00" * 100)
    assert locate_buff_field(raw, "buff_data_list[0].absent_flag") is None
    assert locate_buff_field(raw, "buff_data_list[0].data.base.flags_a") is None
