"""Name-less-record Format 3 apply: record_key + record_rel_offset.

wantedinfo (and other name-less tables) have empty entry names, so the
entry-anchored resolver can't place a write and the record_key path is the
only anchor. The change must carry the numeric ``record_key`` plus a
record-START relative offset (``record_rel_offset``); the apply resolver adds
that to the pabgh index offset (which is the record start) to land exactly on
the field.

Regression for the in-app mod maker producing "unresolvable offset" /
"produced no game changes" on a wantedinfo `_increasePrice` edit: the change
built by ``_intents_to_v2_changes`` only carried a name_end-relative
``rel_offset`` and an empty ``entry``, so neither the entry-name nor the
record_key path could resolve it. Verified end-to-end against the real
wantedinfo.pabgb (record 1030, field at record_start+7).
"""
from __future__ import annotations

from cdumm.engine.json_patch_handler import _apply_byte_patches


def test_record_key_resolves_via_record_rel_offset():
    # record 1030 starts at byte 48; the field sits 7 bytes in (offset 55).
    data = bytearray(b"\x11" * 55 + b"\xdc\x05" + b"\x22" * 3)  # len 60
    changes = [{
        "entry": "",                 # name-less: entry anchor can't help
        "record_key": 1030,
        "record_rel_offset": 7,      # record-START relative
        "original": "dc05",
        "patched": "f049",
    }]
    applied, mismatched, _rel = _apply_byte_patches(
        data, changes, record_offsets={1030: 48}, name_offsets={})
    assert applied == 1
    assert mismatched == 0
    # wrote at record_start(48) + record_rel_offset(7) == 55
    assert bytes(data[55:57]) == b"\xf0\x49"
    # nothing else moved
    assert bytes(data[48:55]) == b"\x11" * 7
    assert bytes(data[57:60]) == b"\x22" * 3


def test_missing_record_offset_skips_not_corrupts():
    # If the key isn't in the index (drift / wrong table), the write must be
    # skipped, never applied to a guessed byte.
    data = bytearray(b"\x11" * 55 + b"\xdc\x05" + b"\x22" * 3)
    changes = [{
        "entry": "", "record_key": 9999,   # not in index
        "record_rel_offset": 7, "original": "dc05", "patched": "f049",
    }]
    applied, mismatched, _rel = _apply_byte_patches(
        data, changes, record_offsets={1030: 48}, name_offsets={})
    assert applied == 0
    assert bytes(data[55:57]) == b"\xdc\x05"   # untouched
