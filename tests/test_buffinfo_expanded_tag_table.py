"""Phase 3d round 2: tag tail sizes derived via homogeneous-entry
cross-validation against the full vanilla buffinfo dataset.

The first round of the systematic-debugging sweep ran an iterative
last-position back-solve and only recovered 9 sizes. The new round
uses a stronger constraint: an entry whose ``buff_data_count`` items
all have the same tag and same common-prefix size must satisfy::

    items_total = N * (5 + csize + tail)

So ``tail = items_total/N - 5 - csize``. If multiple homogeneous
entries with the same tag agree on tail, it's confirmed. If they
disagree, the tag has a variable tail and stays unknown (e.g. tag 17
hits {0, 41, 42}, tag 95 hits {5, 12}).

Cross-validating the expanded table against ALL 280 vanilla entries
walks 198 to completion (vs 9 before) with zero contradictions ,
i.e. zero entries where a known-tag walk overshoots/undershoots
``min_level_offset``. That's 22x coverage at no correctness cost.

This test is the regression guard: if a future change to
``_VARIANT_TAIL_SIZES`` breaks any of the confirmed sizes, the
cross-walk count drops and the test fails.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest


_VANILLA = Path(r"C:/temp/buffinfo.pabgb")
_VANILLA_PABGH = Path(r"C:/temp/buffinfo.pabgh")


def _vanilla_entries():
    if not (_VANILLA.exists() and _VANILLA_PABGH.exists()):
        return None
    pabgb = _VANILLA.read_bytes()
    pabgh = _VANILLA_PABGH.read_bytes()
    n = struct.unpack_from("<H", pabgh, 0)[0]
    offsets = []
    pos = 2
    for _ in range(n):
        key = struct.unpack_from("<I", pabgh, pos)[0]
        off = struct.unpack_from("<I", pabgh, pos + 4)[0]
        offsets.append((key, off))
        pos += 8
    offsets.sort(key=lambda x: x[1])
    out: list[tuple[int, bytes]] = []
    for i, (key, off) in enumerate(offsets):
        end = offsets[i + 1][1] if i + 1 < len(offsets) else len(pabgb)
        out.append((key, pabgb[off:end]))
    return out


def test_expanded_tag_table_walks_at_least_198_entries():
    """At least 198 of the 280 vanilla entries must walk to completion
    using the current ``_VARIANT_TAIL_SIZES``. If this drops, someone
    accidentally broke a tag size."""
    from cdumm._vendor.buffinfo_parser import (
        parse_entry, parse_item_header, parse_payload_common,
        _VARIANT_TAIL_SIZES, _ITEM_HEADER_BYTES,
    )

    entries = _vanilla_entries()
    if entries is None:
        pytest.skip("local vanilla buffinfo files not present")

    n_walked = 0
    contradictions = []
    for _key, raw in entries:
        try:
            e = parse_entry(raw)
        except Exception:
            continue
        pos = e.buff_data_list_offset
        target_end = e.min_level_offset
        walked = True
        try:
            for _ in range(e.buff_data_count):
                h = parse_item_header(raw, pos)
                if h.absent_flag != 0:
                    pos += _ITEM_HEADER_BYTES
                    continue
                cc = parse_payload_common(raw, h.payload_offset)
                tail = _VARIANT_TAIL_SIZES.get(cc.tag)
                if tail is None:
                    walked = False
                    break
                csize = cc.end_offset - h.payload_offset
                pos += _ITEM_HEADER_BYTES + csize + tail
        except Exception:
            walked = False
        if walked:
            if pos == target_end:
                n_walked += 1
            else:
                contradictions.append(
                    (e.name, e.buff_data_count, pos, target_end))

    assert not contradictions, (
        f"{len(contradictions)} entries had a known-tag walk that "
        f"overshot/undershot min_level_offset , a tag size in the "
        f"table is wrong. Examples: {contradictions[:5]}")
    assert n_walked >= 198, (
        f"expected at least 198 walkable entries, got {n_walked}. "
        f"A confirmed tag size was likely removed.")


def test_specific_confirmed_tag_sizes_present():
    """Pin the new sizes derived in round 2 so they don't silently
    disappear in a future refactor."""
    from cdumm._vendor.buffinfo_parser import _VARIANT_TAIL_SIZES

    # (tag, expected size, # of homogeneous entries that confirmed it)
    confirmed = [
        (1, 29), (2, 12), (5, 33), (6, 30),
        (12, 28), (14, 12), (19, 13), (24, 13), (30, 5),
        (59, 17), (74, 0), (89, 4), (90, 12),
        (104, 9), (105, 5), (106, 12), (107, 2), (109, 4),
        (116, 12),
    ]
    missing = [
        (t, s) for t, s in confirmed
        if _VARIANT_TAIL_SIZES.get(t) != s
    ]
    assert not missing, (
        f"confirmed tag sizes missing or altered: {missing}")
