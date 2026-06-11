"""PAMT auto-fix must refuse to patch an ambiguous record pattern.

``_patch_file_record`` locates a file record by searching the PAMT
bytes for the vanilla entry's 12-byte (offset, comp_size, orig_size)
triple. With no uniqueness check, a second record that happens to
share the same triple meant the first match got patched blind,
potentially corrupting an unrelated record. The fix searches again
from pos+1 and returns unfixable when a second match exists.
"""
from __future__ import annotations

import struct
from types import SimpleNamespace

from cdumm.engine.mod_health_check import _patch_file_record


def _record(node_ref: int, offset: int, comp: int, orig: int,
            flags: int) -> bytes:
    """A 20-byte PAMT file record:
    node_ref(4) + offset(4) + comp(4) + orig(4) + flags(4)."""
    return struct.pack("<IIIII", node_ref, offset, comp, orig, flags)


def test_unique_pattern_is_patched() -> None:
    van = SimpleNamespace(offset=100, comp_size=50, orig_size=80)
    data = bytearray(
        _record(1, 100, 50, 80, 0x00020003)
        + _record(2, 200, 60, 90, 0x00020001)
    )

    patched = _patch_file_record(data, van, 999, 55, 88, 7)

    assert patched is True
    new_off, new_comp, new_orig = struct.unpack_from("<III", data, 4)
    assert (new_off, new_comp, new_orig) == (999, 55, 88)
    flags = struct.unpack_from("<I", data, 16)[0]
    assert flags & 0xFF == 7
    assert flags & 0xFFFFFF00 == 0x00020000  # upper bytes preserved
    # Second record untouched.
    assert struct.unpack_from("<III", data, 24) == (200, 60, 90)


def test_ambiguous_pattern_is_refused() -> None:
    van = SimpleNamespace(offset=100, comp_size=50, orig_size=80)
    data = bytearray(
        _record(1, 100, 50, 80, 0x00020003)
        + _record(2, 100, 50, 80, 0x00020001)  # same triple again
    )
    before = bytes(data)

    patched = _patch_file_record(data, van, 999, 55, 88, 7)

    assert patched is False, (
        "pattern matches two records; patching the first blind can "
        "corrupt an unrelated record")
    assert bytes(data) == before, "data must be left untouched"


def test_missing_pattern_returns_false() -> None:
    van = SimpleNamespace(offset=12345, comp_size=1, orig_size=2)
    data = bytearray(_record(1, 100, 50, 80, 0))
    assert _patch_file_record(data, van, 9, 9, 9, 9) is False
