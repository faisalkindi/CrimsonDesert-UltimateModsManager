"""Wrapper-level BuffInfo decode + round-trip tests.

The wrapper has 13 fields surrounding the variable-length
``buff_data_list`` items region. This test pins each field and proves
the wrapper round-trips byte-perfectly across all 280 entries of the
local CD v1.05 vanilla buffinfo.pabgb when present.

Items themselves are still opaque bytes at this stage; future passes
add per-item decoding without breaking these tests.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from cdumm._vendor.buffinfo_parser import (
    BuffinfoEntry,
    locate_buff_field,
    parse_entry,
    serialize_entry,
)


def _build_synthetic_entry(
    *,
    key: int = 1000062,
    name: str = "BuffLevel_Test",
    is_blocked: int = 0,
    buff_data_count: int = 0,
    items_bytes: bytes = b"",
    min_level: int = 1,
    max_level: int = 10,
    sequencer_name: str = "fx/test",
    blct: int = 0,
    uit: int = 0,
    uic: int = 0,
    esi: int = 0,
    iuspd: int = 0,
    ucbgt: int = 0,
) -> bytes:
    name_bytes = name.encode("utf-8")
    seq_bytes = sequencer_name.encode("utf-8")
    out = bytearray()
    out += struct.pack("<I", key)
    out += struct.pack("<I", len(name_bytes))
    out += name_bytes
    out += bytes([is_blocked])
    out += struct.pack("<I", buff_data_count)
    out += items_bytes
    out += struct.pack("<I", min_level)
    out += struct.pack("<I", max_level)
    out += struct.pack("<I", len(seq_bytes))
    out += seq_bytes
    out += bytes([blct])
    out += struct.pack("<I", uit)
    out += struct.pack("<I", uic)
    out += struct.pack("<I", esi)
    out += bytes([iuspd])
    out += bytes([ucbgt])
    return bytes(out)


# ── Synthetic-entry tests (no game files needed) ──────────────────


def test_parse_entry_decodes_all_wrapper_fields():
    raw = _build_synthetic_entry(
        key=42, name="X",
        is_blocked=1, buff_data_count=3,
        items_bytes=b"\xaa" * 60,  # opaque
        min_level=5, max_level=99,
        sequencer_name="fx/path",
        blct=2, uit=11, uic=22, esi=33,
        iuspd=1, ucbgt=1,
    )
    e = parse_entry(raw)
    assert e.entry_key == 42
    assert e.name == "X"
    assert e.is_blocked == 1
    assert e.buff_data_count == 3
    assert e.buff_data_list_bytes == b"\xaa" * 60
    assert e.min_level == 5
    assert e.max_level == 99
    assert e.sequencer_file_name == "fx/path"
    assert e.buff_level_calculate_type == 2
    assert e.ui_template_name == 11
    assert e.ui_component_name == 22
    assert e.elemental_status_info == 33
    assert e.is_use_skill_info_pattern_description == 1
    assert e.use_counting_by_global_timer == 1


def test_serialize_entry_round_trips_synthetic():
    raw = _build_synthetic_entry(
        items_bytes=b"\x00\x01\x02\x03\x04\x05\x06\x07")
    e = parse_entry(raw)
    assert serialize_entry(e) == raw


def test_serialize_entry_handles_empty_items_region():
    raw = _build_synthetic_entry(
        buff_data_count=0, items_bytes=b"")
    e = parse_entry(raw)
    assert e.buff_data_list_bytes == b""
    assert serialize_entry(e) == raw


def test_serialize_entry_handles_empty_sequencer_name():
    raw = _build_synthetic_entry(sequencer_name="")
    e = parse_entry(raw)
    assert e.sequencer_file_name == ""
    assert serialize_entry(e) == raw


def test_offsets_point_at_correct_bytes():
    """Each ``_offset`` field must map to the byte position whose
    value matches the decoded field. CDUMM's intent expander relies
    on these offsets to write byte patches at the right location."""
    raw = _build_synthetic_entry(
        key=42, name="X", min_level=5, max_level=99,
        uit=0xDEADBEEF, esi=0xCAFEBABE,
    )
    e = parse_entry(raw)
    assert struct.unpack_from(
        "<I", raw, e.min_level_offset)[0] == 5
    assert struct.unpack_from(
        "<I", raw, e.max_level_offset)[0] == 99
    assert struct.unpack_from(
        "<I", raw, e.ui_template_name_offset)[0] == 0xDEADBEEF
    assert struct.unpack_from(
        "<I", raw, e.elemental_status_info_offset)[0] == 0xCAFEBABE


def test_locate_buff_field_resolves_wrapper_paths():
    raw = _build_synthetic_entry(min_level=5, max_level=99, esi=33)
    res = locate_buff_field(raw, "min_level")
    assert res is not None
    off, width, dtype = res
    assert width == 4
    assert dtype == "u32"
    assert struct.unpack_from("<I", raw, off)[0] == 5

    res = locate_buff_field(raw, "elemental_status_info")
    assert res is not None
    assert struct.unpack_from("<I", raw, res[0])[0] == 33

    res = locate_buff_field(raw, "use_counting_by_global_timer")
    assert res is not None
    assert res[1] == 1
    assert res[2] == "u8"


def test_locate_buff_field_returns_none_for_unresolved_paths():
    """Paths into BuffDataBase substructure or into items beyond
    index 0 still require decoders not yet built."""
    raw = _build_synthetic_entry(
        buff_data_count=1,
        items_bytes=struct.pack("<I", 0) + bytes([0x01]),
    )
    assert locate_buff_field(
        raw, "buff_data_list[0].data.base.absent_flag") is None
    assert locate_buff_field(
        raw, "buff_data_list[1].absent_flag") is None
    assert locate_buff_field(raw, "data.base.flags_a") is None


def test_locate_buff_field_returns_none_for_unknown_field():
    raw = _build_synthetic_entry()
    assert locate_buff_field(raw, "totally_made_up_field") is None


def test_parse_entry_rejects_truncated_trailer():
    """An entry whose trailing 15 bytes don't fit must raise rather
    than reading garbage from neighbouring memory."""
    raw = _build_synthetic_entry()[:5]  # truncate hard
    with pytest.raises(ValueError):
        parse_entry(raw)


# ── Real-world tests (skip if local vanilla file missing) ─────────


_VANILLA = Path(r"C:/temp/buffinfo.pabgb")
_VANILLA_PABGH = Path(r"C:/temp/buffinfo.pabgh")


def _vanilla_entries():
    """Return a dict {offset: entry_bytes} computed directly from the
    .pabgh offset table. NOT using pycrimson's BinaryGameBlob ,
    that splits entries by reading ``next_absolute_offset`` bytes
    instead of ``next_offset - this_offset``, which appends garbage
    from subsequent entries onto each payload (bug in pycrimson
    1.0.x as of 2026-05-05)."""
    if not (_VANILLA.exists() and _VANILLA_PABGH.exists()):
        return None
    pabgb = _VANILLA.read_bytes()
    pabgh = _VANILLA_PABGH.read_bytes()
    count = struct.unpack_from("<H", pabgh, 0)[0]
    offsets = []
    pos = 2
    for _ in range(count):
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


def test_real_vanilla_round_trips_all_entries():
    """Parse and re-serialize every vanilla entry; bytes must match
    the source verbatim. This is the strongest correctness signal:
    if any wrapper field is mis-located, the round-trip diverges."""
    entries = _vanilla_entries()
    if entries is None:
        pytest.skip(
            "Local vanilla buffinfo.pabgb / .pabgh not present at "
            f"{_VANILLA}; skipping real-world round-trip")
    n_entries = 0
    for offset, payload in entries.items():
        n_entries += 1
        try:
            entry = parse_entry(payload)
        except ValueError as e:
            pytest.fail(
                f"parse_entry failed on vanilla entry at offset "
                f"{offset}: {e}")
        out = serialize_entry(entry)
        assert out == payload, (
            f"round-trip diverged for vanilla entry at offset "
            f"{offset} ({entry.name!r}): "
            f"{len(out)} bytes vs {len(payload)} bytes original"
        )
    assert n_entries == 280, (
        f"expected 280 vanilla entries, got {n_entries}")


def test_real_vanilla_decodes_known_entry_fields():
    """Pin the decoded values for one well-known entry. If the layout
    drifts (e.g. min/max swapped), this regresses loudly."""
    entries = _vanilla_entries()
    if entries is None:
        pytest.skip("Local vanilla buffinfo.pabgb not present")
    target = None
    for offset, payload in entries.items():
        head = parse_entry(payload)
        if head.name == "BuffLevel_Comma_Symptom":
            target = head
            break
    assert target is not None, (
        "BuffLevel_Comma_Symptom not found in vanilla buffinfo")
    assert target.entry_key == 1000062
    assert target.is_blocked == 0
    assert target.buff_data_count == 3
