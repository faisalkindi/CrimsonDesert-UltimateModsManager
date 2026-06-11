"""Shift tracking with many same-size replaces (audit finding 7).

``_apply_byte_patches`` now records only NONZERO size deltas in its
``writes`` list (zero-delta entries contribute nothing to the
``_shift_for`` sum, they only made the lookup O(n^2)). These tests pin
the observable behavior: interleaved same-size replaces and a size-
changing insert/replace still land all writes at the right offsets.
"""
from __future__ import annotations

from cdumm.engine.json_patch_handler import _apply_byte_patches


def test_same_size_replaces_around_an_insert_still_shift_correctly():
    data = bytearray(b"\x00" * 0x40)
    data[0x08:0x0A] = b"\xaa\xaa"   # same-size replace below the insert
    data[0x20:0x22] = b"\xbb\xbb"   # same-size replace above the insert
    vanilla = bytes(data)

    changes = [
        {"offset": 0x08, "original": "aaaa", "patched": "1111"},
        {"type": "insert", "offset": 0x10, "bytes": "deadbeef"},
        {"offset": 0x20, "original": "bbbb", "patched": "2222"},
    ]
    applied, mismatched, _r = _apply_byte_patches(
        data, changes, vanilla_data=vanilla)

    assert applied == 3 and mismatched == 0
    assert data[0x08:0x0A] == b"\x11\x11"
    assert data[0x10:0x14] == b"\xde\xad\xbe\xef"
    # The 0x20 replace sits past the 4-byte insert: shifted to 0x24.
    assert data[0x24:0x26] == b"\x22\x22"
    assert len(data) == 0x44


def test_many_same_size_replaces_do_not_disturb_growing_replace():
    data = bytearray(b"\x00" * 0x100)
    changes = []
    # 16 same-size (zero-delta) replaces.
    for i in range(16):
        off = 0x04 * i
        data[off] = 0x55
        changes.append(
            {"offset": off, "original": "55", "patched": "66"})
    # One growing replace at the top; its offset must shift by ZERO
    # because every earlier write was size-preserving.
    data[0x80:0x82] = b"\xcc\xcc"
    changes.append(
        {"offset": 0x80, "original": "cccc", "patched": "dddddd"})
    vanilla = bytes(data)

    applied, mismatched, _r = _apply_byte_patches(
        data, changes, vanilla_data=vanilla)

    assert applied == 17 and mismatched == 0
    for i in range(16):
        assert data[0x04 * i] == 0x66
    assert data[0x80:0x83] == b"\xdd\xdd\xdd"
    assert len(data) == 0x101
