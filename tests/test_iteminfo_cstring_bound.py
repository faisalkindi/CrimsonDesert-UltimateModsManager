"""#49: cstring must reject an impossible length instead of decoding megabytes.

A speculative/optional parse trial on a garbage length prefix (lengths up to
2 GB were seen during real 1.13 layout detection) used to slice and decode up to
the whole 5.9 MB buffer with errors="replace" BEFORE the trial backtracked --
~2.6k of those cost ~20s of the ~26s whole-table parse, the bulk of falobos76's
hang. Bounding the length by the record (the same guard carray already carries)
makes the doomed trial fail instantly: parse of the real 1.13 table dropped from
~26s to ~1.1s.

Valid strings always fit their record, so real parses are byte-identical -- the
1.13 round-trip tests prove the output is unchanged; these pin the fast-fail.
"""
import struct

import pytest

from cdumm.engine.iteminfo_native_parser import _Reader


def test_cstring_refuses_length_past_record_end():
    # A u32 length prefix claiming 2 GB with only a few bytes behind it: the
    # exact shape a mis-aligned trial produces. Must raise, not decode 5.9 MB.
    buf = struct.pack("<I", 2_000_000_000) + b"junkbytes"
    with pytest.raises(ValueError):
        _Reader(buf, 0, rec_end=len(buf)).cstring()
    with pytest.raises(ValueError):
        _Reader(buf, 0, rec_end=len(buf)).cstring_raw()


def test_cstring_bound_falls_back_to_buffer_when_no_record_end():
    buf = struct.pack("<I", 10_000) + b"short"
    with pytest.raises(ValueError):
        _Reader(buf, 0).cstring()


def test_cstring_still_reads_a_valid_string():
    buf = struct.pack("<I", 3) + b"abc" + b"tail"
    r = _Reader(buf, 0, rec_end=len(buf))
    assert r.cstring() == "abc"
    assert r.pos == 7                    # 4 (length) + 3 (string)


def test_cstring_exact_fit_is_allowed():
    # n == remaining is valid: the string fills the record to its boundary.
    buf = struct.pack("<I", 4) + b"data"
    r = _Reader(buf, 0, rec_end=len(buf))
    assert r.cstring() == "data"
    assert r.pos == len(buf)
