"""Regression: v3.1.7 B1 PAMT validator false-positive on valid mods.

Issues #37 (Catarek) and #38 (LeoBodnar) — v3.1.7 started rejecting
mods that imported fine on v3.1.6 with:

    Mod ships a corrupt 0012/0.pamt: invalid literal for int() with
    base 10: 'tmp9wk9wy2h'

Root cause: B1 writes the modified PAMT bytes to
``NamedTemporaryFile(suffix=".pamt")``, which produces a path like
``/tmp/tmp9wk9wy2h.pamt``. ``parse_pamt`` then does
``int(pamt_stem)`` where stem is ``tmp9wk9wy2h``, blowing up.

Fix: B1 must write to a file whose basename stem is numeric — the
real PAMT basename from rel_path (e.g. ``0.pamt``).
"""
from __future__ import annotations

import struct


def _build_minimal_pamt_with_one_entry() -> bytes:
    """Minimal valid PAMT with 1 file entry so parse_pamt actually
    reaches the ``int(pamt_stem)`` line. The zero-entry fixture used by
    test_pamt_corrupt_bounds skips that code path entirely."""
    blob = bytearray()
    blob += b"\x00" * 4              # magic
    blob += struct.pack("<I", 1)     # paz_count = 1
    blob += b"\x00" * 8              # hash + zero
    blob += b"\x00" * 4              # paz[0] hash
    blob += b"\x00" * 4              # paz[0] size
    blob += struct.pack("<I", 0)     # folder_size = 0
    blob += struct.pack("<I", 0)     # node_size = 0
    blob += struct.pack("<I", 0)     # folder_count = 0
    blob += struct.pack("<I", 1)     # file_count = 1
    # One file entry: node_ref=0, paz_offset=0, comp_size=0,
    # orig_size=0, flags=0 (paz_index=0). 5 uint32s = 20 bytes.
    blob += struct.pack("<IIIII", 0, 0, 0, 0, 0)
    return bytes(blob)


def test_validate_modified_pamt_tolerates_any_rel_path():
    """B1 validator must not false-positive on a valid PAMT regardless
    of how rel_path is formed. This is the direct regression test for
    issues #37 and #38."""
    from cdumm.engine.import_handler import _validate_modified_pamt
    blob = _build_minimal_pamt_with_one_entry()
    # rel_path with the numeric basename seen in production. This MUST
    # succeed — it's a valid PAMT.
    _validate_modified_pamt(blob, "0012/0.pamt")
    # Also a backslash variant — Windows paths.
    _validate_modified_pamt(blob, "0010\\0.pamt")


def test_validate_modified_pamt_still_catches_truly_corrupt():
    """Fix must not regress the whole point of B1 — a corrupt PAMT
    still has to raise so the import is rejected."""
    import pytest
    from cdumm.engine.import_handler import _validate_modified_pamt
    # 8 bytes = truncated header, parse_pamt rejects immediately.
    with pytest.raises(ValueError) as exc:
        _validate_modified_pamt(b"\x00" * 8, "0012/0.pamt")
    assert "0012/0.pamt" in str(exc.value), (
        "error message must name the mod's pamt file so the user "
        "knows WHICH file is bad — not the tempfile path")


def test_b1_callsite_uses_rel_path_basename_not_tempfile_stem():
    """Wiring guard: the B1 call path must NOT use
    NamedTemporaryFile(suffix=".pamt") because that produces a
    non-numeric stem which breaks parse_pamt. It must use the real
    basename from rel_path so stem = '0' (or similar).
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1]
           / "src" / "cdumm" / "engine" / "import_handler.py").read_text(
               encoding="utf-8")
    # The old broken pattern must not appear in executable code. We
    # allow the docstring reference inside _validate_modified_pamt
    # which explains the historical fix; reject any ACTUAL tempfile
    # call with that suffix.
    import re
    # Match `tempfile.NamedTemporaryFile(... suffix=".pamt" ...)` or
    # the imported-alias form `_b1_tempfile.NamedTemporaryFile(...)`.
    bad_call = re.search(
        r"NamedTemporaryFile\s*\([^)]*suffix\s*=\s*[\"']\.pamt",
        src)
    # Scrub docstrings before checking so the rationale text inside
    # _validate_modified_pamt's docstring doesn't false-flag.
    scrubbed = re.sub(r"\"\"\".*?\"\"\"", "", src, flags=re.DOTALL)
    assert re.search(
        r"NamedTemporaryFile\s*\([^)]*suffix\s*=\s*[\"']\.pamt",
        scrubbed) is None, (
        "NamedTemporaryFile with suffix='.pamt' produces a stem like "
        "'tmpXXXXXXXX' which breaks parse_pamt's int(pamt_stem). Use "
        "a tempdir + the real rel_path basename instead.")
