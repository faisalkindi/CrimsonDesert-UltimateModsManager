"""Display-only decoder for the Game Data grid.

parse_records_display / decode_record_display honor the override flags
(no_entry_header / no_null_skip) and drive byte consumption through the
format3 walker, so richly-overridden tables (iteminfo, regioninfo, ...) show
their real fields. These are display-only and must never affect the shared
apply/diff parse_records path.
"""
from __future__ import annotations

import struct

from cdumm.semantic.parser import (
    FieldSpec, TableSchema,
    _display_value, _display_payload_start, decode_record_display,
)


def _f(name, td=None, fmt=None, size=0, ftype=""):
    return FieldSpec(name=name, stream_size=size, field_type=ftype,
                     struct_fmt=fmt, type_descriptor=td)


def test_display_value_scalars_strings_arrays():
    # primitive
    assert _display_value("u16", struct.pack("<H", 1500), 0, 2) == 1500
    # CString: u32 len + bytes
    cs = struct.pack("<I", 5) + b"hello"
    assert _display_value("CString", cs, 0, len(cs)) == "hello"
    # CArray<T>: leading u32 count → shown as "[N items]"
    assert _display_value("CArray<u32>", struct.pack("<I", 3), 0, 4) == "[3 items]"
    # COptional present/absent
    assert _display_value("COptional<u32>", b"\x00", 0, 1) == "—"
    assert _display_value("COptional<u32>", b"\x01", 0, 1) == "present"
    # LocalizableString with no inline text → loc#index
    ls = struct.pack("<B", 0) + struct.pack("<Q", 42) + struct.pack("<I", 0)
    assert _display_value("LocalizableString", ls, 0, len(ls)) == "loc#42"


def test_display_payload_start_honors_flags():
    body = b"\x06\x01" + b"\x00\x00\x00\x00" + b"\x00" + b"rest"
    # normal: entry header (key2 + nlen4 + null1) = 7
    normal = TableSchema("T", [], no_entry_header=False, no_null_skip=False)
    assert _display_payload_start(body, normal, key_size=2) == 7
    # no_null_skip: stop before the null terminator → 6
    nns = TableSchema("T", [], no_entry_header=False, no_null_skip=True)
    assert _display_payload_start(body, nns, key_size=2) == 6
    # no_entry_header: first field at byte 0
    neh = TableSchema("T", [], no_entry_header=True)
    assert _display_payload_start(body, neh, key_size=2) == 0


def test_decode_record_display_no_entry_header_walks_fields():
    # mirrors regioninfo: no header, _key(u16) then _stringKey(CString)
    entry = struct.pack("<H", 7) + struct.pack("<I", 11) + b"Region_Test" + b"\x03"
    schema = TableSchema(
        "regionlike",
        [_f("_key", td="u16"), _f("_stringKey", td="CString"),
         _f("_regionType", td="u8")],
        no_entry_header=True)
    got = decode_record_display(entry, schema, key_size=2)
    assert got["_key"] == 7
    assert got["_stringKey"] == "Region_Test"
    assert got["_regionType"] == 3


def test_decode_record_display_stops_cleanly_when_walker_blocks():
    # a field whose bytes run past the entry → decode keeps earlier fields,
    # never guesses the rest
    entry = struct.pack("<H", 9)                      # only 2 bytes
    schema = TableSchema(
        "t", [_f("_a", td="u16"), _f("_b", td="u32")], no_entry_header=True)
    got = decode_record_display(entry, schema, key_size=2)
    assert got == {"_a": 9}                            # _b absent, not guessed
