"""v3.1.7 Feature #3: infer comp_type from adjacent vanilla file by extension.

When a mod ships a NEW file (one that doesn't exist in vanilla)
whose extension isn't in CDUMM's hardcoded ``_EXT_COMP_TYPE`` map,
overlay_builder falls back to ``2`` (LZ4) — which is fine for some
formats but wrong for others (e.g. game-specific binary blobs the
extension map doesn't know about).

JMM 9.9.2 (``ModManager.cs:1947-1969``) handles this by scanning the
GLOBAL set of known game files for any entry with the same extension
and inheriting its flags. CDUMM doesn't have a global PAMT index, but
it knows the target ``pamt_dir`` per overlay entry and can scan that
PAMT for an extension neighbor — strictly more accurate than picking
LZ4 by default.

Pure-logic helper test (no Qt). Vanilla PAMT staged directly so we
don't depend on a real game install.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest


def test_helper_returns_comp_type_of_adjacent_extension(tmp_path):
    """Given a fake vanilla PAMT carrying entries with various
    extensions, ``infer_comp_type_from_pamt`` should return the
    comp_type of the first entry whose filename ends with the
    requested extension.

    PamtEntry.compression_type is ``(flags >> 16) & 0x0F``. We stage
    one entry whose flags carry comp_type=4 (zlib) for ``.foo`` and
    comp_type=2 (LZ4) for ``.bar`` so the resolver has to pick the
    right one.
    """
    from cdumm.archive.overlay_builder import infer_comp_type_from_pamt
    # Mock a parsed entries list (not a real PAMT byte stream). The
    # helper signature is intentionally narrow: take a list of
    # (filename, comp_type) tuples (or a callable that returns one).
    pamt_entries = [
        ("vanilla1.foo", 4),
        ("vanilla2.bar", 2),
        ("vanilla3.FOO", 4),  # case-insensitive match for .foo
    ]
    assert infer_comp_type_from_pamt(pamt_entries, ".foo") == 4
    assert infer_comp_type_from_pamt(pamt_entries, ".bar") == 2
    # Case-insensitive — ``.FOO`` matches ``.foo``.
    assert infer_comp_type_from_pamt(pamt_entries, ".FOO") == 4


def test_helper_returns_none_when_no_extension_matches(tmp_path):
    """When no vanilla entry shares the extension, the helper must
    return None so the caller can fall back to its hardcoded map."""
    from cdumm.archive.overlay_builder import infer_comp_type_from_pamt
    pamt_entries = [("vanilla1.foo", 4)]
    assert infer_comp_type_from_pamt(pamt_entries, ".unknown") is None


def test_helper_handles_files_without_extension(tmp_path):
    """Filenames without an extension should never match a
    requested extension lookup."""
    from cdumm.archive.overlay_builder import infer_comp_type_from_pamt
    pamt_entries = [("noext", 4), ("with.dat", 2)]
    assert infer_comp_type_from_pamt(pamt_entries, ".dat") == 2
    assert infer_comp_type_from_pamt(pamt_entries, "") is None
