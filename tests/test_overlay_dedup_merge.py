"""Tests for overlay_dedup.merge_duplicate_overlay_entries.

Pins the cross-layer merge contract Faith Armor Without Sleeve +
Damian Custom would have needed.
"""
from __future__ import annotations


def test_pass_through_single_entry_group():
    """One contributor per entry_path: list returns unchanged."""
    from cdumm.engine.overlay_dedup import merge_duplicate_overlay_entries
    entries = [
        (b"body-a",
         {"pamt_dir": "0009", "entry_path": "file.prefab",
          "mod_name": "A", "priority": 1}),
    ]
    merged, warnings = merge_duplicate_overlay_entries(
        entries, vanilla_resolver=lambda d, p: b"vanilla")
    assert merged == entries
    assert warnings == []


def test_different_entry_paths_are_not_merged():
    """Two entries in the same pamt_dir but different entry_path stay
    separate."""
    from cdumm.engine.overlay_dedup import merge_duplicate_overlay_entries
    entries = [
        (b"a",
         {"pamt_dir": "0009", "entry_path": "file1.prefab",
          "mod_name": "A", "priority": 1}),
        (b"b",
         {"pamt_dir": "0009", "entry_path": "file2.prefab",
          "mod_name": "B", "priority": 2}),
    ]
    merged, _ = merge_duplicate_overlay_entries(
        entries, vanilla_resolver=lambda d, p: b"vanilla")
    assert len(merged) == 2


def test_non_overlapping_edits_both_survive():
    """The Faith+ENTR case: mod A changes bytes 0-3, mod B changes
    bytes 8-11, no overlap. Merged result carries BOTH mods' edits —
    exactly what the user wanted but doesn't happen today because
    priority-pick drops one."""
    from cdumm.engine.overlay_dedup import merge_duplicate_overlay_entries
    vanilla = b"AAAABBBBCCCC"
    mod_a = b"XXXXBBBBCCCC"   # changes bytes 0-3
    mod_b = b"AAAABBBBYYYY"   # changes bytes 8-11
    entries = [
        (mod_a, {"pamt_dir": "0009", "entry_path": "f",
                  "mod_name": "Faith", "priority": 3}),
        (mod_b, {"pamt_dir": "0009", "entry_path": "f",
                  "mod_name": "ENTR", "priority": 5}),
    ]
    merged, _ = merge_duplicate_overlay_entries(
        entries, vanilla_resolver=lambda d, p: vanilla)
    assert len(merged) == 1
    body, meta = merged[0]
    # Both mods' deltas made it into the final buffer.
    assert body == b"XXXXBBBBYYYY"
    assert set(meta["_merged_from"]) == {"Faith", "ENTR"}


def test_overlapping_edits_priority_winner_takes_region():
    """When two mods edit the same bytes, the lower CDUMM priority
    number wins (higher precedence in CDUMM terms), matching the
    existing priority contract."""
    from cdumm.engine.overlay_dedup import merge_duplicate_overlay_entries
    vanilla = b"AAAABBBB"
    mod_a = b"XXXXBBBB"   # changes 0-3, priority=1 (winner)
    mod_b = b"YYYYBBBB"   # also changes 0-3, priority=5
    entries = [
        (mod_a, {"pamt_dir": "d", "entry_path": "f",
                  "mod_name": "High", "priority": 1}),
        (mod_b, {"pamt_dir": "d", "entry_path": "f",
                  "mod_name": "Low", "priority": 5}),
    ]
    merged, warnings = merge_duplicate_overlay_entries(
        entries, vanilla_resolver=lambda d, p: vanilla)
    assert len(merged) == 1
    body, meta = merged[0]
    # Priority-1 ("High") wins the overlap.
    assert body == b"XXXXBBBB"
    # compiled_merge logs the overlap as a warning. We surface it.
    assert len(warnings) >= 1


def test_vanilla_unavailable_falls_back_to_priority_pick():
    """When the resolver returns None, emit a warning and use the
    priority winner. No silent data loss."""
    from cdumm.engine.overlay_dedup import merge_duplicate_overlay_entries
    entries = [
        (b"aaa", {"pamt_dir": "d", "entry_path": "f",
                   "mod_name": "A", "priority": 1}),
        (b"bbb", {"pamt_dir": "d", "entry_path": "f",
                   "mod_name": "B", "priority": 5}),
    ]
    merged, warnings = merge_duplicate_overlay_entries(
        entries, vanilla_resolver=lambda d, p: None)
    assert len(merged) == 1
    body, _ = merged[0]
    assert body == b"aaa"   # priority=1 wins
    assert any("vanilla unavailable" in w.lower() for w in warnings)


def test_vanilla_resolver_exception_is_swallowed():
    """A crashing resolver must not crash apply. Fall back to
    priority-pick plus a warning."""
    from cdumm.engine.overlay_dedup import merge_duplicate_overlay_entries
    entries = [
        (b"aaa", {"pamt_dir": "d", "entry_path": "f",
                   "mod_name": "A", "priority": 1}),
        (b"bbb", {"pamt_dir": "d", "entry_path": "f",
                   "mod_name": "B", "priority": 5}),
    ]

    def _boom(d, p):
        raise RuntimeError("disk error")

    merged, warnings = merge_duplicate_overlay_entries(
        entries, vanilla_resolver=_boom)
    assert len(merged) == 1
    assert merged[0][0] == b"aaa"
    assert warnings   # fallback warning emitted


def test_merge_producing_vanilla_is_dropped():
    """If all contributors' deltas cancel out to exactly vanilla,
    no overlay entry is emitted — round-tripping to vanilla would
    be wasted bytes in the overlay PAZ."""
    from cdumm.engine.overlay_dedup import merge_duplicate_overlay_entries
    vanilla = b"AAAA"
    entries = [
        (vanilla, {"pamt_dir": "d", "entry_path": "f",
                    "mod_name": "A", "priority": 1}),
        (vanilla, {"pamt_dir": "d", "entry_path": "f",
                    "mod_name": "B", "priority": 5}),
    ]
    merged, _ = merge_duplicate_overlay_entries(
        entries, vanilla_resolver=lambda d, p: vanilla)
    assert merged == []


def test_entry_without_priority_sorts_to_lowest_precedence():
    """A contributor whose metadata lacks ``priority`` is treated
    as the weakest — it loses overlap to any priority-bearing
    contributor."""
    from cdumm.engine.overlay_dedup import merge_duplicate_overlay_entries
    vanilla = b"AAAA"
    mod_a = b"XXXA"   # priority=5
    mod_b = b"YYYA"   # no priority — should lose overlap
    entries = [
        (mod_a, {"pamt_dir": "d", "entry_path": "f",
                  "mod_name": "Priced", "priority": 5}),
        (mod_b, {"pamt_dir": "d", "entry_path": "f",
                  "mod_name": "Unpriced"}),
    ]
    merged, _ = merge_duplicate_overlay_entries(
        entries, vanilla_resolver=lambda d, p: vanilla)
    assert len(merged) == 1
    assert merged[0][0] == b"XXXA"  # priced mod wins


def test_regression_leobodnar_scenario_shape():
    """Integration-shaped regression test for the reported case:
    one ENTR rewrite + one JSON-origin body targeting the same
    entry_path collapse to one merged entry when they edit
    different ranges of the same file."""
    from cdumm.engine.overlay_dedup import merge_duplicate_overlay_entries
    # Stand-in for cd_phw_00_ub_00_0166.prefab — just bytes.
    vanilla = b"header-vanilla-body-tail"
    # JSON-origin body: Faith's surgical patch at offset 15-18.
    faith = b"header-vanilla-FACE-tail"
    # ENTR-origin body: another mod rewrites the header.
    entr = b"HEADR1-vanilla-body-tail"
    entries = [
        (faith, {
            "pamt_dir": "0009",
            "entry_path":
            "character/bin__/prefab/1_pc/02_phw/armor/9_upperbody/"
            "cd_phw_00_ub_00_0166.prefab",
            "mod_name": "aggregated JSON",
            "priority": 6,
        }),
        (entr, {
            "pamt_dir": "0009",
            "entry_path":
            "character/bin__/prefab/1_pc/02_phw/armor/9_upperbody/"
            "cd_phw_00_ub_00_0166.prefab",
            "mod_name": "ENTR Mod",
            "priority": 3,
        }),
    ]
    merged, _ = merge_duplicate_overlay_entries(
        entries, vanilla_resolver=lambda d, p: vanilla)
    # Collapsed to one entry.
    assert len(merged) == 1
    body, meta = merged[0]
    # Both mods' non-overlapping edits are in the merged body.
    assert b"FACE" in body, "Faith's delta was dropped"
    assert b"HEADR1" in body, "ENTR's delta was dropped"
    # Metadata from the highest-precedence (priority=3) contributor.
    assert meta.get("mod_name") == "ENTR Mod"
