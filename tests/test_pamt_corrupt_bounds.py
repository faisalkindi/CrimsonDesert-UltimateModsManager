"""B2: parse_pamt must fail FAST and LOUD on corrupt headers.

Real bug from Nexus issue #35 (UMANLE's Axiom Of Excellence Slim Lacking):
a corrupt PAMT claimed a file offset of 9,704,786,772 inside a 152-byte
buffer. struct.unpack_from eventually raised ``struct.error`` deep in
the loop, but by then apply_engine had already logged a vague DEBUG
"skip mod N" and moved on. The user saw "stuck at 2%" for 7+ minutes.

Fix: sanity-check every size field in parse_pamt BEFORE feeding it
back to struct.unpack_from. Raise a clean ``ValueError`` naming the
file and the bad field so the caller can surface it.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from cdumm.archive.paz_parse import parse_pamt


def _write(tmp_path: Path, blob: bytes) -> str:
    p = tmp_path / "0.pamt"
    p.write_bytes(blob)
    return str(p)


def test_truncated_header_raises_value_error(tmp_path):
    """PAMT shorter than the fixed 16-byte prefix cannot be parsed."""
    path = _write(tmp_path, b"\x00" * 8)
    with pytest.raises(ValueError) as exc:
        parse_pamt(path)
    msg = str(exc.value).lower()
    assert "pamt" in msg or "corrupt" in msg or "truncated" in msg


def test_folder_size_exceeds_file_size_raises_value_error(tmp_path):
    """UMANLE's shape: a folder_size that implies the file is multi-GB
    when it's actually a few hundred bytes. parse_pamt must refuse."""
    blob = bytearray()
    blob += b"\x00" * 4              # magic
    blob += struct.pack("<I", 1)     # paz_count = 1
    blob += b"\x00" * 8              # hash + zero
    blob += b"\x00" * 4              # paz[0] hash
    blob += b"\x00" * 4              # paz[0] size
    # folder_size = 9 GB. Total file ~150 bytes → obviously bogus.
    blob += struct.pack("<I", 4_000_000_000)
    blob += b"\x00" * 32             # padding so we don't EOF first
    path = _write(tmp_path, bytes(blob))
    with pytest.raises(ValueError) as exc:
        parse_pamt(path)
    assert "folder" in str(exc.value).lower() or \
           "exceeds" in str(exc.value).lower() or \
           "corrupt" in str(exc.value).lower()


def test_node_size_exceeds_file_size_raises_value_error(tmp_path):
    """Same sanity pattern for the node (filename trie) section."""
    blob = bytearray()
    blob += b"\x00" * 4              # magic
    blob += struct.pack("<I", 0)     # paz_count = 0
    blob += b"\x00" * 8              # hash + zero
    # folder_size = 0 (valid — empty folder section)
    blob += struct.pack("<I", 0)
    # node_size = 5 GB. Bogus.
    blob += struct.pack("<I", 4_000_000_000)
    blob += b"\x00" * 32
    path = _write(tmp_path, bytes(blob))
    with pytest.raises(ValueError) as exc:
        parse_pamt(path)
    msg = str(exc.value).lower()
    assert "node" in msg or "exceeds" in msg or "corrupt" in msg


def test_paz_count_absurdly_large_raises_value_error(tmp_path):
    """A sane PAMT has 1-100 paz files. Claim a billion → refuse fast
    instead of looping a billion times in the paz-table walker."""
    blob = bytearray()
    blob += b"\x00" * 4              # magic
    blob += struct.pack("<I", 1_000_000)       # paz_count insane (>4096)
    blob += b"\x00" * 8
    blob += b"\x00" * 64
    path = _write(tmp_path, bytes(blob))
    with pytest.raises(ValueError) as exc:
        parse_pamt(path)
    msg = str(exc.value).lower()
    assert "paz" in msg or "count" in msg or "corrupt" in msg


def test_error_message_names_the_file(tmp_path):
    """Caller needs to surface the filename. ValueError must include
    the pamt path or its basename so the user knows WHICH mod broke."""
    blob = b"\x00" * 4 + struct.pack("<I", 1_000_000) + b"\x00" * 20
    path = _write(tmp_path, blob)
    with pytest.raises(ValueError) as exc:
        parse_pamt(path)
    # Path or filename must appear in the message for diagnosability.
    assert "0.pamt" in str(exc.value) or str(path) in str(exc.value)


def test_valid_pamt_still_parses(tmp_path):
    """Sanity — don't break the happy path. A minimal valid PAMT with
    zero entries must still return an empty list, not raise."""
    blob = bytearray()
    blob += b"\x00" * 4              # magic
    blob += struct.pack("<I", 0)     # paz_count = 0
    blob += b"\x00" * 8              # hash + zero
    blob += struct.pack("<I", 0)     # folder_size = 0
    blob += struct.pack("<I", 0)     # node_size = 0
    blob += struct.pack("<I", 0)     # folder_count = 0
    blob += struct.pack("<I", 0)     # file_count = 0
    path = _write(tmp_path, bytes(blob))
    entries = parse_pamt(path)
    assert entries == []
