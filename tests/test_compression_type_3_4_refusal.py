"""Compression types 3 (zlib) and 4 (QuickLZ) must refuse loudly
(audit finding 2).

CDUMM only implements type 1 (DDS split) and type 2 (LZ4). Before the
fix, a type 3/4 entry silently took the uncompressed path in both the
extraction and repack pipelines: the payload round-tripped as raw
bytes while the PAMT flags still claimed compression, corrupting the
slot. Both paths now raise a clear ValueError that the callers turn
into a per-file error.
"""
from __future__ import annotations

import pytest

from cdumm.archive.paz_crypto import lz4_compress
from cdumm.archive.paz_parse import PazEntry
from cdumm.archive.paz_repack import repack_entry_bytes
from cdumm.engine.json_patch_handler import decompress_entry


def _entry(comp_type: int, comp_size: int = 10,
           orig_size: int = 40) -> PazEntry:
    return PazEntry(
        path=f"data/type{comp_type}.bin", paz_file="x", offset=0,
        comp_size=comp_size, orig_size=orig_size,
        flags=comp_type << 16, paz_index=0,
    )


@pytest.mark.parametrize("comp_type", [3, 4])
def test_decompress_entry_refuses_types_3_and_4(comp_type: int):
    entry = _entry(comp_type)
    with pytest.raises(ValueError) as exc:
        decompress_entry(b"\x00" * 10, entry)
    assert f"unsupported compression type {comp_type}" in str(exc.value)
    assert entry.path in str(exc.value)


@pytest.mark.parametrize("comp_type", [3, 4])
def test_repack_entry_bytes_refuses_types_3_and_4(comp_type: int):
    entry = _entry(comp_type)
    with pytest.raises(ValueError) as exc:
        repack_entry_bytes(b"\x00" * 8, entry, allow_size_change=True)
    assert f"unsupported compression type {comp_type}" in str(exc.value)
    assert entry.path in str(exc.value)


def test_type_2_lz4_still_round_trips():
    plain = b"<root>" + b"payload " * 50 + b"</root>"
    comp = lz4_compress(plain)
    entry = PazEntry(
        path="data/ok.bin", paz_file="x", offset=0,
        comp_size=len(comp), orig_size=len(plain),
        flags=2 << 16, paz_index=0,
    )
    assert decompress_entry(comp, entry) == plain


def test_uncompressed_type_0_still_passes_through():
    entry = PazEntry(
        path="data/raw.bin", paz_file="x", offset=0,
        comp_size=8, orig_size=8, flags=0, paz_index=0,
    )
    assert decompress_entry(b"ABCDEFGH", entry) == b"ABCDEFGH"
