""".paloc localization tables (#290).

CDUMM treated `.paloc` as an opaque blob, so `.cdmod` `localization-patch`
components couldn't be applied. This is the codec.

Two things these tests exist to defend, both learned the hard way:

1. **The trailer is a witness.** The file ends with its own record count.
   A parse that disagrees with it is wrong, and we refuse. This is the
   independent check that a byte-exact round-trip CANNOT give you -- see
   #285, where iteminfo round-tripped perfectly for months while carrying
   76-139 undecoded bytes per record.

2. **`tag` must be preserved, never recomputed.** Measured on the live
   187,526-record Ukrainian table, it is NOT the value length (1,049
   matches), the char length (2,721), or the key length (370). It's a
   category enum. Recomputing it would corrupt every string touched, and
   the file would still parse clean.
"""
from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest

from cdumm.engine.paloc_handler import (
    PalocEntry, PalocError, apply_changes, parse_paloc, serialize_paloc,
)


def _table(entries):
    return serialize_paloc([PalocEntry(*e) for e in entries])


SAMPLE = [
    (47, 0, "262897", "Недоступно під час бою."),
    (3, 0, "4294967344", "Кліфф"),
    (9, 0, "42597485641824", "Sword"),
]


# ── format ──────────────────────────────────────────────────────────────

def test_round_trip_is_byte_identical():
    raw = _table(SAMPLE)
    assert serialize_paloc(parse_paloc(raw)) == raw


def test_the_trailer_is_the_record_count():
    raw = _table(SAMPLE)
    assert struct.unpack_from("<I", raw, len(raw) - 4)[0] == len(SAMPLE)


def test_a_lying_trailer_is_refused():
    """The format hands us a witness; a parse that disagrees with it is
    wrong. Refuse rather than write a mis-framed table over the game's."""
    raw = bytearray(_table(SAMPLE))
    struct.pack_into("<I", raw, len(raw) - 4, 999)
    with pytest.raises(PalocError, match="record count mismatch"):
        parse_paloc(bytes(raw))


def test_keys_are_decimal_strings_and_values_are_utf8():
    e = parse_paloc(_table(SAMPLE))
    assert [x.key for x in e] == ["262897", "4294967344", "42597485641824"]
    assert e[0].value.endswith("бою.")


def test_value_len_is_bytes_not_characters():
    """Cyrillic is 2 bytes/char in UTF-8. If the field were a char count,
    every multi-byte string would desync the walk."""
    one = _table([(1, 0, "5", "Кліфф")])       # 5 chars, 10 bytes
    vlen = struct.unpack_from("<I", one, 12 + 1)[0]
    assert vlen == 10


def test_garbage_is_refused_not_best_efforted():
    with pytest.raises(PalocError):
        parse_paloc(b"\xff" * 64)


# ── the edit the mod actually performs ──────────────────────────────────

def test_append_edits_one_record_and_leaves_the_rest_alone():
    raw = _table(SAMPLE)
    out, applied, missing = apply_changes(
        raw, [{"key": "4294967344", "op": "append", "suffix": " ({price})"}])

    assert (applied, missing) == (1, [])
    after = parse_paloc(out)
    before = parse_paloc(raw)
    assert after[1].value == "Кліфф ({price})"
    assert [a.value for a in after if a.key != "4294967344"] == \
           [b.value for b in before if b.key != "4294967344"]


def test_the_tag_is_preserved_not_recomputed():
    """The one that would corrupt everything silently."""
    raw = _table(SAMPLE)
    out, _n, _m = apply_changes(
        raw, [{"key": "262897", "op": "append", "suffix": " X"}])
    assert parse_paloc(out)[0].tag == 47      # unchanged
    assert parse_paloc(out)[0].reserved == 0


def test_a_change_whose_key_is_absent_is_reported_not_swallowed():
    """A patch that matches nothing would install clean and change nothing
    -- the exact silent no-op of #259 / #275 / #278 / #285."""
    out, applied, missing = apply_changes(
        _table(SAMPLE),
        [{"key": "does-not-exist", "op": "append", "suffix": "!"}])
    assert applied == 0
    assert missing == ["does-not-exist"]


def test_an_unknown_op_is_refused():
    with pytest.raises(PalocError, match="unknown localization op"):
        apply_changes(_table(SAMPLE),
                      [{"key": "262897", "op": "reverse-the-polarity"}])


# ── the real 24 MB table ────────────────────────────────────────────────

_REAL = os.environ.get("CDUMM_PALOC")


@pytest.mark.skipif(
    not (_REAL and Path(_REAL).exists()),
    reason="set $CDUMM_PALOC to a real localizationstring_*.paloc "
           "(extract one from the Ukrainian Localization mod's PAZ -- see "
           "the #290 write-up; the file is 24 MB so it isn't committed)")
def test_the_live_table_parses_and_round_trips():
    raw = Path(_REAL).read_bytes()
    entries = parse_paloc(raw)
    assert len(entries) > 100_000
    assert serialize_paloc(entries) == raw          # byte-identical
    # and the trailer agreed, or parse_paloc would have refused.
