"""v3.1.7 Feature #1: PAZ source layout in ``game_files/`` subfolder.

A mod author publishes a folder mod with this layout:

    MyMod/
        mod.json
        game_files/
            0036/
                0.paz
                0.pamt

CDUMM should detect this exactly the same as the canonical layout
``MyMod/0036/0.paz``. Today the pattern check at
``import_handler.py:_detect_mod_structure`` only looks at *immediate*
children of the candidate dir for the numbered ``NNNN/0.paz`` shape,
so the ``game_files/`` wrapper makes ``is_standalone_paz`` False and
the mod gets miscategorised.

These tests pin the corrected behavior.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _build_layout(root: Path, *, wrap_in_game_files: bool) -> Path:
    """Stage a minimal compiled mod tree.

    ``wrap_in_game_files=False`` produces the canonical layout (numbered
    dir as a direct child of the mod root). ``True`` nests the same
    layout under ``game_files/`` — what 9.9.2 added support for.
    """
    mod_dir = root / "MyMod"
    mod_dir.mkdir()
    (mod_dir / "mod.json").write_text(
        '{"modinfo": {"title": "MyMod", "version": "1.0"}}', encoding="utf-8"
    )
    base = mod_dir if not wrap_in_game_files else (mod_dir / "game_files")
    paz_dir = base / "0036"
    paz_dir.mkdir(parents=True)
    # Minimal stub bytes — detection only needs the files to exist.
    (paz_dir / "0.paz").write_bytes(b"PAZ_STUB")
    (paz_dir / "0.pamt").write_bytes(b"PAMT_STUB")
    return mod_dir


def test_canonical_layout_detected_as_standalone_paz(tmp_path):
    """Sanity check: the standard ``MyMod/0036/0.paz`` shape is NOT
    flagged as a loose-file mod (so the standalone-PAZ codepath gets
    to handle it later)."""
    from cdumm.engine.import_handler import detect_loose_file_mod
    mod_dir = _build_layout(tmp_path, wrap_in_game_files=False)
    detected = detect_loose_file_mod(mod_dir)
    assert detected is None or detected.get("format") != "loose_file_mod", (
        f"canonical layout should bypass loose_file_mod tag; got {detected}")


def test_game_files_subfolder_detected_as_standalone_paz(tmp_path):
    """The new 9.9.2 layout: ``MyMod/game_files/0036/0.paz`` should
    be treated identically to the canonical layout. Today the
    ``is_standalone_paz = any(... for d in candidate.iterdir())``
    only looks one level deep, so the wrapper folder hides it and
    detection falls through to the loose-file path — which then
    treats the PAZ files as loose-file payload and mis-routes them.
    """
    from cdumm.engine.import_handler import detect_loose_file_mod
    mod_dir = _build_layout(tmp_path, wrap_in_game_files=True)
    detected = detect_loose_file_mod(mod_dir)
    assert detected is None or detected.get("format") != "loose_file_mod", (
        f"game_files/ wrapper layout should be detected as standalone "
        f"PAZ, not loose_file_mod; got {detected}")
