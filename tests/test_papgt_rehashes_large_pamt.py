"""Bug from Nexus reports (michael2k + timelesscjing, 2026-05-03):
after the Crimson Desert 1.05 patch, Post-Apply Verification flags
'0009 PAMT hash mismatch' and '0015 PAMT hash mismatch'. Both are
large PAZ groups whose 0.pamt files exceed the 2MB threshold added
in v2.2.1.

Root cause traced to ``papgt_manager.py`` rebuild loop. For the
vanilla-base branch (most users), large PAMTs short-circuit and trust
the hash cached in the PAPGT base instead of recomputing from live
disk bytes. After a game update, the live PAMT differs but the rebuilt
PAPGT carries the OLD hash , Post-Apply Verification then correctly
flags the inconsistency.

The mod-base branch (``is_mod_base=True``) right beside it has no
size cap and always reads live bytes. Same hash function, same I/O
shape. The 2MB cap on the vanilla branch was a precaution against
worker-thread memory pressure that the mod branch demonstrates is
unnecessary.

Fix: drop the size cap so the vanilla branch always recomputes the
live PAMT hash.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest


def _build_papgt_with_one_entry(dir_name: str,
                                cached_hash: int) -> bytes:
    """Stage a minimal PAPGT containing one entry with a deliberately
    stale ``cached_hash``."""
    string_table = bytearray()
    name_offset = 0
    string_table += dir_name.encode("ascii") + b"\x00"

    body = bytearray()
    body += struct.pack("<III", 0x003FFF00, name_offset, cached_hash)
    body += struct.pack("<I", len(string_table))
    body += string_table

    out = bytearray()
    out += b"\x01\x02\x03\x04"
    out += b"\x00\x00\x00\x00"
    out += bytes([1, 0xFF, 0xFF, 0xFF])
    out += body
    return bytes(out)


def _read_pamt_hash_for(papgt: bytes, dir_name: str) -> int:
    """Pull the stored PAMT hash for ``dir_name`` from a rebuilt PAPGT."""
    entry_count = papgt[8]
    entry_start = 12
    string_table_off = entry_start + entry_count * 12 + 4
    for i in range(entry_count):
        pos = entry_start + i * 12
        name_off = struct.unpack_from('<I', papgt, pos + 4)[0]
        h = struct.unpack_from('<I', papgt, pos + 8)[0]
        abs_off = string_table_off + name_off
        end = papgt.index(0, abs_off)
        name = papgt[abs_off:end].decode('ascii')
        if name == dir_name:
            return h
    raise AssertionError(f"{dir_name} not found in rebuilt PAPGT")


def test_rebuild_recomputes_hash_for_large_pamt(tmp_path: Path):
    """A PAMT >2MB on disk whose content differs from the PAPGT base's
    cached hash must be rehashed during rebuild. Pre-fix this trusts
    the cached hash for files at or above 2MB and ships a stale value
    that Post-Apply Verification then flags."""
    from cdumm.archive.papgt_manager import PapgtManager
    from cdumm.archive.hashlittle import compute_pamt_hash

    game_dir = tmp_path / "game"
    (game_dir / "meta").mkdir(parents=True)
    (game_dir / "0009").mkdir()

    # PAMT body: > 2MB so the bug branch fires. First 12 bytes need to
    # decode as something compute_pamt_hash accepts (just non-empty
    # past the 12-byte length check). Fill the rest with a recognizable
    # repeating pattern so the live hash is deterministic.
    pamt_data = b"PAMT" + b"\x00" * 8 + b"\xAB" * (2_500_000 - 12)
    assert len(pamt_data) > 2_000_000, "PAMT must exceed the 2MB cap"
    pamt_path = game_dir / "0009" / "0.pamt"
    pamt_path.write_bytes(pamt_data)

    live_hash = compute_pamt_hash(pamt_data)

    # PAPGT base claims a stale cached hash for 0009 (one bit flipped
    # off the live value so the values are guaranteed to differ).
    stale_hash = live_hash ^ 0x1
    papgt_base = _build_papgt_with_one_entry("0009", stale_hash)
    (game_dir / "meta" / "0.papgt").write_bytes(papgt_base)

    mgr = PapgtManager(game_dir)
    rebuilt = mgr.rebuild()

    stored = _read_pamt_hash_for(rebuilt, "0009")
    assert stored == live_hash, (
        f"Rebuild must rehash large PAMTs against live disk. PAPGT base "
        f"had stale hash {stale_hash:#010x}; live PAMT hashes to "
        f"{live_hash:#010x}; rebuilt PAPGT shipped {stored:#010x}. "
        f"Pre-fix this returns the stale value because the 2MB cap "
        f"short-circuits before the read+hash. After a game update "
        f"that touches a large PAMT, this is what makes Post-Apply "
        f"Verification flag '0009 PAMT hash mismatch' for users like "
        f"michael2k / timelesscjing on Nexus 2026-05-03."
    )


def test_rebuild_still_recomputes_small_pamt(tmp_path: Path):
    """Regression guard for the existing < 2MB path , small PAMTs
    must continue to be rehashed against live disk after the cap is
    removed. Otherwise the fix has narrowed the verification scope."""
    from cdumm.archive.papgt_manager import PapgtManager
    from cdumm.archive.hashlittle import compute_pamt_hash

    game_dir = tmp_path / "game"
    (game_dir / "meta").mkdir(parents=True)
    (game_dir / "0001").mkdir()

    pamt_data = b"PAMT" + b"\x00" * 8 + b"\xCD" * 100
    pamt_path = game_dir / "0001" / "0.pamt"
    pamt_path.write_bytes(pamt_data)

    live_hash = compute_pamt_hash(pamt_data)
    stale_hash = live_hash ^ 0x1
    papgt_base = _build_papgt_with_one_entry("0001", stale_hash)
    (game_dir / "meta" / "0.papgt").write_bytes(papgt_base)

    rebuilt = PapgtManager(game_dir).rebuild()
    stored = _read_pamt_hash_for(rebuilt, "0001")
    assert stored == live_hash
