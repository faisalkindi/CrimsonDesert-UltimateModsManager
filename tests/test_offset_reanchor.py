"""Re-anchoring byte offsets onto a Format 3 rebuild (#293, falobos76).

#294 made the unsafe combination REFUSE. This is the proper fix: make it
WORK -- his socket mods (Format 3) and his offset mods should apply together,
which is what they already do in DMM.

These tests use synthetic tables ON PURPOSE. The whole point of the design is
that it needs NO schema, NO record index and NO per-table knowledge: it
locates a patch by the bytes AROUND it, because a record the Format 3 mod
didn't touch is byte-identical in both tables. If the algorithm needed the
real iteminfo layout to be testable, it would be the wrong algorithm.

(The real 24 MB table is exercised anyway wherever the fixture exists -- see
the optional test at the bottom.)
"""
from __future__ import annotations

import pytest

from cdumm.engine.offset_reanchor import (
    ReanchorRefused, reanchor_changes, reanchor_offset,
)

# A stand-in table: distinct 32-byte "records" so context windows are unique.
REC = [bytes([i]) * 4 + bytes(range(i, i + 28)) for i in range(1, 40)]
VANILLA = b"".join(REC)

# A "Format 3 rebuild": record 3 GROWS by 16 bytes, so everything after it
# shifts -- exactly what broke falobos76's game.
REBUILT = b"".join(REC[:3] + [REC[3] + b"\xAB" * 16] + REC[4:])

R_START = [sum(len(r) for r in REC[:i]) for i in range(len(REC))]
SHIFT = 16


def test_the_rebuild_actually_shifts_things():
    assert len(REBUILT) == len(VANILLA) + SHIFT


# ── the fix ─────────────────────────────────────────────────────────────

def test_an_offset_after_the_edit_is_remapped():
    """The core of #293: every offset after the first edited record MOVED."""
    off = R_START[20] + 6
    original = VANILLA[off:off + 4]

    new_off = reanchor_offset(VANILLA, REBUILT, off, original)

    assert new_off == off + SHIFT
    assert REBUILT[new_off:new_off + 4] == original, (
        "a remap that doesn't land on the bytes the author measured is not "
        "a remap")


def test_an_offset_before_the_edit_is_unchanged():
    off = R_START[1] + 6
    original = VANILLA[off:off + 4]
    assert reanchor_offset(VANILLA, REBUILT, off, original) == off


def test_an_unrebuilt_table_is_a_no_op():
    off = R_START[10] + 2
    assert reanchor_offset(
        VANILLA, VANILLA, off, VANILLA[off:off + 4]) == off


# ── refuse, don't guess ─────────────────────────────────────────────────

def test_a_patch_that_never_matched_vanilla_is_refused():
    """It was built for a different game version -- broken before any
    Format 3 mod touched the table. Say so; don't remap it."""
    with pytest.raises(ReanchorRefused, match="different game version"):
        reanchor_offset(VANILLA, REBUILT, R_START[20], b"\xde\xad\xbe\xef")


def test_a_record_whose_BYTES_the_format3_mod_changed_is_refused():
    """The two mods genuinely disagree about these bytes. Silently picking
    one is the exact class of bug this project keeps fixing.

    Note what is NOT refused: a record that merely GREW. If the bytes a
    patch reads are untouched, re-anchoring it is correct and refusing it
    would be a false refusal -- a guard that over-fires is its own bug.
    """
    edited = bytearray(REC[3])
    edited[8:12] = b"\xEE\xEE\xEE\xEE"        # F3 mod rewrites these bytes
    reb = b"".join(REC[:3] + [bytes(edited)] + REC[4:])

    off = R_START[3] + 8
    original = VANILLA[off:off + 4]           # the bytes it rewrote
    with pytest.raises(ReanchorRefused):
        reanchor_offset(VANILLA, reb, off, original)


def test_a_record_that_merely_GREW_still_re_anchors():
    """The counterpart. REC[3] grows by 16 bytes but its existing bytes are
    untouched, so a patch into them is still valid and must apply."""
    off = R_START[3] + 8
    original = VANILLA[off:off + 4]
    assert reanchor_offset(VANILLA, REBUILT, off, original) == off
    assert REBUILT[off:off + 4] == original


def test_an_ambiguous_anchor_widens_rather_than_guessing():
    """Two identical records: a short window matches twice. It must widen
    the context to disambiguate, never 'take the first match'."""
    rec = bytes(range(32))
    tail = b"\x99" * 32
    van = rec + b"\x01" * 32 + rec + tail
    reb = rec + b"\x01" * 32 + rec + b"\x77" * 8 + tail   # grows at the end

    off = 64 + 4                     # inside the SECOND copy of `rec`
    original = van[off:off + 4]
    got = reanchor_offset(van, reb, off, original)
    assert got == off                # unchanged: the edit is after it
    assert reb[got:got + 4] == original


def test_an_offset_past_the_end_is_refused():
    with pytest.raises(ReanchorRefused, match="past the end"):
        reanchor_offset(VANILLA, REBUILT, len(VANILLA) - 2, b"\x00" * 8)


# ── the change-list level (falobos76's exact shape) ─────────────────────

def test_reanchor_changes_rewrites_the_offsets_in_place():
    """A Format 3 whole-table change plus a byte-offset patch, in one
    aggregated list for the same file -- which is how apply_engine holds
    them."""
    off = R_START[20] + 6
    original = VANILLA[off:off + 4]
    changes = [
        {"offset": 0, "original": VANILLA.hex(), "patched": REBUILT.hex()},
        {"offset": off, "original": original.hex(), "patched": "ffffffff",
         "label": "mission_efficiency_x20"},
    ]

    kept, refused = reanchor_changes(changes)

    assert refused == []
    patch = kept[1]
    assert patch["offset"] == off + SHIFT     # remapped
    assert patch["_reanchored_from"] == off   # and it says so
    assert REBUILT[patch["offset"]:patch["offset"] + 4] == original


def test_an_unremappable_patch_is_dropped_with_a_reason():
    changes = [
        {"offset": 0, "original": VANILLA.hex(), "patched": REBUILT.hex()},
        {"offset": R_START[20], "original": "deadbeef", "patched": "ffffffff",
         "label": "stale mod"},
    ]
    kept, refused = reanchor_changes(changes)

    assert len(kept) == 1                     # only the whole-table change
    assert len(refused) == 1
    assert "different game version" in refused[0]["_refuse_reason"]


def test_with_no_format3_rebuild_nothing_is_touched():
    """Offset mods on their own must behave exactly as they always have.
    A guard that over-fires is its own bug."""
    kept, refused = reanchor_changes(
        [{"offset": 2265877, "original": "6400", "patched": "ffff"}])
    assert refused == []
    assert kept[0]["offset"] == 2265877
    assert "_reanchored_from" not in kept[0]


# ── the real table, when it's available ─────────────────────────────────

def test_on_the_real_cd113_iteminfo_table():
    """Same thing against 6 MB of real game data and a real Format 3 rebuild.
    Skipped where the committed fixture isn't present (it arrives with #280)."""
    try:
        from tests.fixture_loaders import load_vanilla113
    except ImportError:
        pytest.skip("vanilla113 fixture loader not on this branch (#280)")

    from cdumm.engine import iteminfo_native_parser as P
    from cdumm.semantic.parser import parse_pabgh_index

    body = load_vanilla113("iteminfo.pabgb")
    header = load_vanilla113("iteminfo.pabgh")
    _k, offs = parse_pabgh_index(header, "iteminfo")
    starts = sorted(offs.values())
    fields = P.detect_iteminfo_layout(body, starts)

    items = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(body)
        r = P._Reader(body, s, rec_end=e)
        items.append(P._read_item(r, fields=fields))

    items[5]["item_tag_list"] = list(items[5]["item_tag_list"]) + [12345]

    out = bytearray()
    for it in items:
        w = P._Writer()
        P._write_item(w, it, fields=fields)
        out += bytes(w.buf)
    rebuilt = bytes(out)
    assert len(rebuilt) != len(body)

    off = starts[400] + 8
    original = body[off:off + 4]
    new_off = reanchor_offset(body, rebuilt, off, original)

    assert new_off != off
    assert rebuilt[new_off:new_off + 4] == original
