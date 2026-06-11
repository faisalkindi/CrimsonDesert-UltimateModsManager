"""Synthetic PAMT builder for archive-layer tests.

Builds a minimal but structurally valid .pamt byte blob that
``cdumm.archive.paz_parse.parse_pamt`` accepts:

    magic(4) | paz_count(4) | hash+zero(8)
    | paz table (hash+size per paz, separator between)
    | folder section (size-prefixed; one root folder entry)
    | node section (size-prefixed; one node per file entry)
    | folder record count (0)
    | file record count | 20-byte records

Each entry dict: ``name`` (filename), ``offset``, ``comp_size``,
``orig_size``, ``flags``. The flags low 16 bits are the PAZ chunk id,
bits 16-19 the compression type.
"""
from __future__ import annotations

import struct


def build_pamt(entries: list[dict], folder_prefix: str = "root",
               paz_count: int = 1) -> bytes:
    out = bytearray()
    out += b"PAMT"                                  # magic (parser skips)
    out += struct.pack("<I", paz_count)
    out += b"\x00" * 8                              # hash + zero

    for i in range(paz_count):                      # paz table
        out += struct.pack("<II", 0, 0)             # hash + size
        if i < paz_count - 1:
            out += struct.pack("<I", 0)             # separator

    fname = folder_prefix.encode("utf-8")           # folder section
    folder = struct.pack("<I", 0xFFFFFFFF) + bytes([len(fname)]) + fname
    out += struct.pack("<I", len(folder)) + folder

    node_blob = bytearray()                         # node section
    node_refs: list[int] = []
    for e in entries:
        node_refs.append(len(node_blob))
        nm = e["name"].encode("utf-8")
        node_blob += struct.pack("<I", 0xFFFFFFFF) + bytes([len(nm)]) + nm
    out += struct.pack("<I", len(node_blob)) + node_blob

    out += struct.pack("<I", 0)                     # folder record count

    out += struct.pack("<I", len(entries))          # file record count
    for ref, e in zip(node_refs, entries):
        out += struct.pack(
            "<IIIII", ref, e["offset"], e["comp_size"],
            e["orig_size"], e["flags"])
    return bytes(out)
