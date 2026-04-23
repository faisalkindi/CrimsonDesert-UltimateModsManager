"""v3.1.7 Feature #2: PAPGT entry per-field decode of IsOptional / LangType.

Background. The PAPGT entry's first 4 bytes were treated as a single
``flags`` uint32 in CDUMM. Verified against decompiled JMM 9.9.2
(``ModManager.cs:2902-2940``) those 4 bytes are actually:

    byte 0  = IsOptional   (0 = required)
    bytes 1-2 = LangType   (uint16, 0x3FFF = 16383 = "all languages")
    byte 3  = Zero         (always 0)

Vanilla CDUMM hardcodes ``new_dir_flags = 0x003FFF00`` for new entries.
Little-endian that's bytes ``00 FF 3F 00`` — exactly IsOptional=0,
LangType=0x3FFF, Zero=0. So existing byte output is correct; what's
missing is the *named field* surface so callers can override.

These tests pin (a) the per-field decode, (b) byte-identical default
output, (c) per-entry override carrying through to the rebuilt PAPGT.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest


def _build_minimal_papgt(entries: list[tuple[str, int]]) -> bytes:
    """Stage a tiny PAPGT byte stream with the entries given.

    Each entry tuple is ``(dir_name, flags_u32)``. PAMT hash is set to
    a deterministic per-entry value so we can read it back later.
    """
    string_table = bytearray()
    name_offsets = {}
    for name, _ in entries:
        if name not in name_offsets:
            name_offsets[name] = len(string_table)
            string_table += name.encode("ascii") + b"\x00"

    body = bytearray()
    for idx, (name, flags) in enumerate(entries):
        pamt_hash = 0xAAAA0000 | idx  # any deterministic non-zero
        body += struct.pack("<III", flags, name_offsets[name], pamt_hash)
    body += struct.pack("<I", len(string_table))
    body += string_table

    out = bytearray()
    out += b"\x01\x02\x03\x04"      # [0:4] header meta
    out += b"\x00\x00\x00\x00"      # [4:8] hash placeholder
    # [8:12] = entry count byte + 3 meta bytes (4 bytes total).
    out += bytes([len(entries), 0xFF, 0xFF, 0xFF])
    out += body
    return bytes(out)


def test_decode_papgt_entry_splits_first_four_bytes(tmp_path):
    """Reading a vanilla-shaped entry must decompose flags into
    is_optional + lang_type + zero. The flags ``0x003FFF00`` decodes
    to (0, 0x3FFF, 0)."""
    from cdumm.archive.papgt_manager import decode_papgt_entry_flags
    is_optional, lang_type, zero = decode_papgt_entry_flags(0x003FFF00)
    assert is_optional == 0
    assert lang_type == 0x3FFF  # 16383
    assert zero == 0


def test_encode_papgt_entry_round_trips_default(tmp_path):
    """Encoding the JMM 9.9.2 default fields back to a uint32 must
    return the historical ``0x003FFF00`` so vanilla output stays
    byte-identical."""
    from cdumm.archive.papgt_manager import encode_papgt_entry_flags
    assert encode_papgt_entry_flags(is_optional=0, lang_type=0x3FFF, zero=0) == 0x003FFF00


def test_encode_papgt_entry_carries_overrides(tmp_path):
    """A non-default override (mark optional, language-targeted) must
    surface as the matching little-endian byte pattern."""
    from cdumm.archive.papgt_manager import encode_papgt_entry_flags
    # is_optional=1, lang_type=2 ("English" per JMM lang table — value
    # is opaque to us; we just pin it round-trips).
    val = encode_papgt_entry_flags(is_optional=1, lang_type=2, zero=0)
    # Bytes (LE): 01 02 00 00
    assert val == 0x00000201


def test_rebuild_with_default_options_is_byte_identical(tmp_path):
    """Calling ``rebuild`` without overrides must produce the same
    bytes the previous implementation produced for the same input —
    regression guard for live users who don't touch the new fields.
    """
    from cdumm.archive.papgt_manager import PapgtManager
    game_dir = tmp_path / "game"
    (game_dir / "meta").mkdir(parents=True)
    pre = _build_minimal_papgt([("0036", 0x003FFF00)])
    (game_dir / "meta" / "0.papgt").write_bytes(pre)
    # Fake a 0036 PAMT so the entry survives the live-entry filter.
    (game_dir / "0036").mkdir()
    (game_dir / "0036" / "0.pamt").write_bytes(b"\x00" * 24)

    mgr = PapgtManager(game_dir, vanilla_dir=None)
    rebuilt = mgr.rebuild()
    # First entry's flags bytes (entry_start=12, flags=[12:16]) must be
    # the historical 00 FF 3F 00 little-endian pattern.
    assert rebuilt[12:16] == bytes([0x00, 0xFF, 0x3F, 0x00])


def test_rebuild_accepts_per_entry_options_override(tmp_path):
    """``rebuild`` must accept a ``mod_entry_options`` mapping that
    overrides is_optional / lang_type for specific (new) directories.
    """
    from cdumm.archive.papgt_manager import PapgtManager
    game_dir = tmp_path / "game"
    (game_dir / "meta").mkdir(parents=True)
    pre = _build_minimal_papgt([("0036", 0x003FFF00)])
    (game_dir / "meta" / "0.papgt").write_bytes(pre)
    (game_dir / "0036").mkdir()
    (game_dir / "0036" / "0.pamt").write_bytes(b"\x00" * 24)
    # New mod-added dir 0099 with a stub PAMT so PAPGT picks it up.
    (game_dir / "0099").mkdir()
    (game_dir / "0099" / "0.pamt").write_bytes(b"\x00" * 24)

    mgr = PapgtManager(game_dir, vanilla_dir=None)
    rebuilt = mgr.rebuild(
        mod_entry_options={"0099": {"is_optional": 1, "lang_type": 2}},
    )
    # New dirs are inserted at the front (first match wins). Entry 0
    # is at byte 12. Its first 4 bytes should reflect the override:
    # is_optional=1, lang_type=2, zero=0  ->  01 02 00 00 LE
    assert rebuilt[12:16] == bytes([0x01, 0x02, 0x00, 0x00]), (
        f"override didn't reach the entry; got {rebuilt[12:16].hex()}")
