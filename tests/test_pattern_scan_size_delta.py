"""Pattern-scan relocation of a SIZE-CHANGING replace (code-review
finding, 2026-06-10): the relocated write used an equal-length slice
``data[new_offset:new_offset + len(patched)] = patched``, so the
buffer never changed size, the bytes beyond the original region got
clobbered, and the recorded size_delta corrupted the shift tracker.

The branch is only entered when the original bytes match at the
relocated offset, so the write must replace the ORIGINAL's length,
exactly like the normal success path.
"""
from __future__ import annotations

from cdumm.engine.json_patch_handler import _apply_byte_patches

_MARKER = b"\xde\xad\xbe\xef\xca\xfe\xf0\x0d"


def _drifted_buffer() -> bytearray:
    # Marker lives at 40; the change's stale offset says 8.
    data = bytearray(64)
    data[40:48] = _MARKER
    data[48:56] = b"TAILTAIL"  # must survive the relocated write
    return data


def test_relocated_growing_replace_preserves_tail_and_grows():
    data = _drifted_buffer()
    patched = b"\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c"  # 12B
    change = {
        "offset": 8,
        "original": _MARKER.hex(),
        "patched": patched.hex(),
    }
    applied, mismatched, relocated = _apply_byte_patches(
        data, [change], skipped_out=[])
    assert applied == 1 and relocated == 1, "pattern scan did not relocate"
    assert len(data) == 64 + 4, "buffer did not grow by the size delta"
    assert bytes(data[40:52]) == patched
    assert bytes(data[52:60]) == b"TAILTAIL", (
        "bytes beyond the original region were clobbered")


def test_relocated_shrinking_replace_preserves_tail_and_shrinks():
    data = _drifted_buffer()
    patched = b"\x01\x02\x03\x04"  # 4B
    change = {
        "offset": 8,
        "original": _MARKER.hex(),
        "patched": patched.hex(),
    }
    applied, mismatched, relocated = _apply_byte_patches(
        data, [change], skipped_out=[])
    assert applied == 1 and relocated == 1, "pattern scan did not relocate"
    assert len(data) == 64 - 4, "buffer did not shrink by the size delta"
    assert bytes(data[40:44]) == patched
    assert bytes(data[44:52]) == b"TAILTAIL", (
        "tail after the shrunk region is wrong")
