"""Pattern-scan uniqueness guard (audit finding 5, 2026-06-11).

For patterns shorter than 12 bytes the simple scan tier used to pick
the match NEAREST the stale offset with no uniqueness requirement, so
a recurring 4-byte original (a float, a count) silently patched the
wrong record. Short patterns must now be UNIQUE in the searched range;
long patterns (>= 12 bytes) keep nearest-match behavior.

Both the native (cdumm_native) path and the pure-Python fallback are
covered; the fallback is forced by masking the native module in
sys.modules.
"""
from __future__ import annotations

import sys

import pytest

from cdumm.engine.json_patch_handler import _pattern_scan

_SHORT = b"\xDE\xAD\xBE\xEF\x01\x02"          # 6 bytes (< 12)
_LONG = bytes(range(1, 17))                     # 16 bytes (>= 12)


def _unique_short_buffer() -> bytearray:
    return bytearray(b"\x00" * 100 + _SHORT + b"\x00" * 100)


def _ambiguous_short_buffer() -> bytearray:
    return bytearray(
        b"\x00" * 50 + _SHORT + b"\x00" * 50 + _SHORT + b"\x00" * 50)


@pytest.fixture(params=["installed", "python_fallback"])
def scan_mode(request, monkeypatch):
    """Run each test against the installed module set AND with the
    native extension masked (sys.modules[name] = None makes the import
    raise ImportError, forcing the Python fallback)."""
    if request.param == "python_fallback":
        monkeypatch.setitem(sys.modules, "cdumm_native", None)
    return request.param


def test_short_ambiguous_pattern_returns_none(scan_mode):
    data = _ambiguous_short_buffer()
    assert _pattern_scan(data, 40, _SHORT) is None


def test_short_unique_pattern_relocates(scan_mode):
    data = _unique_short_buffer()
    assert _pattern_scan(data, 80, _SHORT) == 100


def test_long_pattern_still_relocates(scan_mode):
    data = bytearray(b"\x00" * 200 + _LONG + b"\x00" * 50)
    assert _pattern_scan(data, 150, _LONG) == 200


def test_long_duplicate_pattern_keeps_nearest_match(scan_mode):
    data = bytearray(
        b"\x00" * 10 + _LONG + b"\x00" * 100 + _LONG + b"\x00" * 50)
    # Two long matches at 10 and 126; stale offset 100 is nearer 126.
    assert _pattern_scan(data, 100, _LONG) == 126
