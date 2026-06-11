"""Corrupt deltas must not become raw file replacements.

Audit finding 6 (2026-06-11): ``apply_delta`` used to treat ANY
unknown magic as a raw file replacement, so a truncated/corrupt stored
delta silently replaced a game file with garbage. Raw replacement is
now only allowed for ``.newfile`` deltas (the importer's full copies,
the only delta kind legitimately written without a magic header);
anything else raises ValueError, which the apply engine downgrades to
a per-file skip.
"""
from __future__ import annotations

import pytest

from cdumm.engine.delta_engine import (
    FULL_COPY_MAGIC,
    apply_delta,
    apply_delta_from_file,
    generate_delta,
)

VANILLA = b"VANILLA_BYTES" + b"\x00" * 64


def test_unknown_magic_raises_by_default():
    with pytest.raises(ValueError):
        apply_delta(VANILLA, b"GARBAGE_NOT_A_DELTA")


def test_unknown_magic_allowed_when_raw_flagged():
    raw = b"RAW_NEWFILE_CONTENT"
    assert apply_delta(VANILLA, raw, allow_raw=True) == raw


def test_known_magics_still_apply():
    assert apply_delta(VANILLA, FULL_COPY_MAGIC + b"NEW") == b"NEW"
    modified = b"X" + VANILLA[1:]
    delta = generate_delta(VANILLA, modified)
    assert apply_delta(VANILLA, delta) == modified


def test_newfile_path_passes_raw_through(tmp_path):
    p = tmp_path / "0036_0.paz.newfile"
    p.write_bytes(b"WHOLE_NEW_FILE")
    assert apply_delta_from_file(VANILLA, p) == b"WHOLE_NEW_FILE"


def test_corrupt_delta_file_raises(tmp_path):
    p = tmp_path / "0008_0.paz.bsdiff"
    p.write_bytes(b"BSDI_truncated_header_only")  # not BSDIFF40
    with pytest.raises(ValueError):
        apply_delta_from_file(VANILLA, p)
