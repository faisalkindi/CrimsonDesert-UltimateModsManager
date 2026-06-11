"""Key-width-aware name-offset resolution (audit finding 10).

``_build_name_offsets_generic`` hardcoded the u32-key entry layout
(name_len at offset+4), so u16-keyed tables (storeinfo) always failed
name resolution and every scalar change on them died "unresolvable
offset". The resolver now accepts a key_size hint from callers that
ran parse_pabgh_index, and auto-retries the u16 layout when the u32
pass yields nothing.
"""
from __future__ import annotations

import struct

from cdumm.engine.json_patch_handler import (
    _build_name_offsets_for_v2,
    _build_name_offsets_generic,
    _pabgh_key_size_hint,
)


def _table(key_size: int, entries):
    """entries: list of (key, name, payload_bytes). Returns
    (body, header). header: u16 count + (key + u32 offset) per entry."""
    kfmt = "<H" if key_size == 2 else "<I"
    body = bytearray()
    offs = {}
    for key, name, payload in entries:
        offs[key] = len(body)
        nb = name.encode("utf-8")
        body += struct.pack(kfmt, key)
        body += struct.pack("<I", len(nb)) + nb
        body += payload
    header = struct.pack("<H", len(entries))
    for key, _n, _p in entries:
        header += struct.pack(kfmt, key) + struct.pack("<I", offs[key])
    return bytes(body), bytes(header)


_ENTRIES = [
    (3101, "Store_Foo", b"\x01" * 24),
    (3102, "Store_Bar", b"\x02" * 16),
]


def test_u16_table_resolves_with_hint():
    body, header = _table(2, _ENTRIES)
    result = _build_name_offsets_generic(body, header, key_size=2)
    assert result is not None
    # name_end anchor = entry_off + 2 (key) + 4 (len) + len(name)
    assert result["Store_Foo"] == 0 + 6 + len("Store_Foo")
    second_off = 6 + len("Store_Foo") + 24
    assert result["Store_Bar"] == second_off + 6 + len("Store_Bar")


def test_u16_table_resolves_without_hint_via_retry():
    body, header = _table(2, _ENTRIES)
    result = _build_name_offsets_generic(body, header)
    assert result is not None
    assert result["Store_Foo"] == 6 + len("Store_Foo")


def test_u32_table_still_resolves():
    body, header = _table(4, _ENTRIES)
    result = _build_name_offsets_generic(body, header)
    assert result is not None
    assert result["Store_Foo"] == 8 + len("Store_Foo")


def test_key_size_hint_derived_from_pabgh():
    body16, header16 = _table(2, _ENTRIES)
    assert _pabgh_key_size_hint("storeinfo.pabgb", header16) == 2
    body32, header32 = _table(4, _ENTRIES)
    assert _pabgh_key_size_hint("dropsetinfo.pabgb", header32) == 4


def test_for_v2_wrapper_threads_key_size():
    body, header = _table(2, _ENTRIES)
    result = _build_name_offsets_for_v2(
        "storeinfo.pabgb", body, header, key_size=2)
    assert result is not None
    assert "Store_Foo" in result and "Store_Bar" in result
