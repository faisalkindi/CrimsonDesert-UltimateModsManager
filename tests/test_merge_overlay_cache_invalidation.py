"""#447 (Vi-gilo): merging two mods onto the same PAMT entry can leave
the overlay cache serving the previous version.

``_merge_same_target_overlay_entries`` collapses N mods that each patch
the same (pamt_dir, entry_path) into one overlay entry, but used to keep
the metadata of ONLY the first mod in the merge (including its
delta_hash / delta_path). ``overlay_builder.build_overlay`` skips
recompressing an entry — and reuses the previous apply's compressed
bytes verbatim — whenever delta_hash matches the cached value. So if a
LATER mod in the merge was the one that actually changed between two
Apply runs, the first mod's unchanged delta_hash hid that change from
the cache, and the overlay kept shipping the stale pre-change merge
with no warning.

This reproduces the two-mod merge + re-apply sequence end to end
(``_merge_same_target_overlay_entries`` -> ``build_overlay`` twice,
feeding the second build the first build's cache) using only in-memory
fixtures, no game install required.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import cdumm.archive.overlay_builder as ob
from cdumm.archive.paz_crypto import lz4_decompress
from cdumm.engine.apply_engine import ApplyWorker


def _make_worker(vanilla_bytes: bytes):
    w = ApplyWorker.__new__(ApplyWorker)
    w._db = MagicMock()
    w._game_dir = MagicMock()
    w._vanilla_dir = MagicMock()
    w._get_vanilla_entry_content = MagicMock(return_value=vanilla_bytes)
    return w


def _decompressed(paz: bytes, overlay_entries, filename: str) -> bytes:
    entry = next(e for e in overlay_entries if e.filename == filename)
    raw = paz[entry.paz_offset:entry.paz_offset + entry.comp_size]
    if entry.flags & 0x0F == 2:
        return lz4_decompress(raw, entry.decomp_size)
    return raw


def test_merged_entry_cache_invalidates_when_second_mod_changes(
        tmp_path: Path):
    vanilla = b"\x00" * 64
    meta = {
        "pamt_dir": "0008",
        "entry_path": "gamedata/skill.staticinfobody",
        "compression_type": 2,
    }

    # Mod A ("Dragon Flying"): patches bytes 0x00-0x04, never changes
    # between the two applies below — this is the mod whose metadata
    # the merge keeps.
    dragon_body = bytearray(vanilla)
    dragon_body[0x00:0x04] = b"\xAA\xAA\xAA\xAA"
    dragon_meta = {
        **meta, "priority": 1, "mod_name": "Dragon Flying",
        "delta_hash": "dragon-stable",
    }

    # Mod B ("Stamina Tweak"): patches bytes 0x20-0x24. ITS content
    # changes between apply 1 and apply 2 (e.g. the user edited its
    # config or re-imported an updated version), while mod A above is
    # untouched.
    stamina_v1 = bytearray(vanilla)
    stamina_v1[0x20:0x24] = b"\xBB\xBB\xBB\xBB"
    stamina_v2 = bytearray(vanilla)
    stamina_v2[0x20:0x24] = b"\xCC\xCC\xCC\xCC"

    w = _make_worker(vanilla)

    # --- Apply 1 ---
    entries_v1 = [
        (bytes(dragon_body), dict(dragon_meta)),
        (bytes(stamina_v1), {**meta, "priority": 2,
                              "mod_name": "Stamina Tweak",
                              "delta_hash": "stamina-v1"}),
    ]
    merged_v1 = w._merge_same_target_overlay_entries(entries_v1)
    assert len(merged_v1) == 1
    merged_body_v1, merged_meta_v1 = merged_v1[0]
    assert merged_body_v1[0x00:0x04] == b"\xAA\xAA\xAA\xAA"
    assert merged_body_v1[0x20:0x24] == b"\xBB\xBB\xBB\xBB"

    paz1, _pamt1, _entries1 = ob.build_overlay(
        [(merged_body_v1, merged_meta_v1)],
        game_dir=None, preloaded_cache=None, vanilla_pathc_path=None,
    )
    cache1 = ob.build_overlay._last_cache

    # --- Apply 2: mod A (Dragon Flying) is unchanged; mod B (Stamina
    # Tweak) now patches different bytes. ---
    entries_v2 = [
        (bytes(dragon_body), dict(dragon_meta)),
        (bytes(stamina_v2), {**meta, "priority": 2,
                              "mod_name": "Stamina Tweak",
                              "delta_hash": "stamina-v2"}),
    ]
    merged_v2 = w._merge_same_target_overlay_entries(entries_v2)
    assert len(merged_v2) == 1
    merged_body_v2, merged_meta_v2 = merged_v2[0]
    assert merged_body_v2[0x20:0x24] == b"\xCC\xCC\xCC\xCC"
    assert merged_body_v2 != merged_body_v1, (
        "test fixture bug: v2 must actually differ from v1")

    paz2, _pamt2, entries2 = ob.build_overlay(
        [(merged_body_v2, merged_meta_v2)],
        game_dir=None,
        preloaded_cache=(cache1, paz1, None),
        vanilla_pathc_path=None,
    )

    rebuilt = _decompressed(paz2, entries2, "skill.staticinfobody")
    assert rebuilt == merged_body_v2, (
        "overlay cache served the pre-change merge: Stamina Tweak's "
        "new bytes (0x20-0x24 == CC) are missing from the rebuilt "
        "overlay, which still reads "
        f"{rebuilt[0x20:0x24]!r} instead of b'\\xCC\\xCC\\xCC\\xCC' "
        "(GitHub #447)")


def _apply_twice(w, entries_v1, entries_v2):
    merged_v1 = w._merge_same_target_overlay_entries(entries_v1)
    paz1, _p1, _e1 = ob.build_overlay(
        merged_v1, game_dir=None, preloaded_cache=None,
        vanilla_pathc_path=None)
    cache1 = ob.build_overlay._last_cache
    merged_v2 = w._merge_same_target_overlay_entries(entries_v2)
    paz2, _p2, entries2 = ob.build_overlay(
        merged_v2, game_dir=None, preloaded_cache=(cache1, paz1, None),
        vanilla_pathc_path=None)
    return merged_v1, merged_v2, _decompressed(
        paz2, entries2, "skill.staticinfobody")


def test_merge_of_entries_with_no_hash_or_path_still_rebuilds():
    """Components built in memory carry neither delta_hash nor
    delta_path. Before #447 that left the merged entry's delta_hash
    empty, which disables caching for it. A fingerprint made only from
    component metadata is then a constant, so the first merge would be
    cached and every later change to it ignored."""
    vanilla = b"\x00" * 64
    meta = {"pamt_dir": "0008",
            "entry_path": "gamedata/skill.staticinfobody",
            "compression_type": 2}
    a = bytearray(vanilla)
    a[0x00:0x04] = b"\xAA" * 4
    b1 = bytearray(vanilla)
    b1[0x20:0x24] = b"\xBB" * 4
    b2 = bytearray(vanilla)
    b2[0x20:0x24] = b"\xCC" * 4
    w = _make_worker(vanilla)
    _m1, merged_v2, rebuilt = _apply_twice(
        w,
        [(bytes(a), {**meta, "priority": 1}),
         (bytes(b1), {**meta, "priority": 2})],
        [(bytes(a), {**meta, "priority": 1}),
         (bytes(b2), {**meta, "priority": 2})])
    assert rebuilt == merged_v2[0][0]
    assert rebuilt[0x20:0x24] == b"\xCC" * 4


def test_an_unchanged_merge_keeps_its_fingerprint():
    """The cache must still be usable: the same inputs give the same
    delta_hash, so an Apply with nothing changed reuses the bytes."""
    vanilla = b"\x00" * 64
    meta = {"pamt_dir": "0008",
            "entry_path": "gamedata/skill.staticinfobody",
            "compression_type": 2}
    a = bytearray(vanilla)
    a[0x00:0x04] = b"\xAA" * 4
    b = bytearray(vanilla)
    b[0x20:0x24] = b"\xBB" * 4
    entries = [(bytes(a), {**meta, "priority": 1}),
               (bytes(b), {**meta, "priority": 2})]
    w = _make_worker(vanilla)
    m1, m2, rebuilt = _apply_twice(w, list(entries), list(entries))
    assert m1[0][1]["delta_hash"]
    assert m1[0][1]["delta_hash"] == m2[0][1]["delta_hash"]
    assert rebuilt == m1[0][0]
