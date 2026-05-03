"""GitHub #62 (UnLuckyLust, 2026-05-02): Format 3 mod targeting
iteminfo.pabgb's enchant_data_list applies cleanly but produces 0
byte changes in-game. CDUMM warning misleadingly says: "could not
extract vanilla bytes for 'iteminfo.pabgb'. The target file may
not exist in your game's PAZ archives, check the spelling or run
Steam Verify if the file is missing."

Root cause: _get_vanilla_entry_content's bare `except Exception:
pass` silently swallowed the real extraction error from
_extract_from_paz (decompression failure, encryption mismatch,
PAMT/PAZ desync after game patch, etc.) and returned None. The
caller then synthesized a generic "file may not exist" warning,
hiding the actual cause.

Fix: log the underlying exception at warning level so the user can
see WHY extraction failed. The original "file may not exist"
warning still fires for the None return, but the log now contains
the real cause for support / debugging.
"""
from __future__ import annotations
from pathlib import Path

import pytest


def test_vanilla_extraction_failure_is_logged(tmp_path: Path, caplog):
    """When _extract_from_paz raises, the exception detail must be
    logged at WARNING level so users / debuggers can see the real
    failure cause instead of the generic 'file may not exist'."""
    import logging
    from cdumm.engine.apply_engine import ApplyWorker
    from cdumm.archive.paz_parse import PazEntry

    vanilla_dir = tmp_path / "vanilla"
    game_dir = tmp_path / "game"
    pamt_dir = vanilla_dir / "0009"
    pamt_dir.mkdir(parents=True)
    (pamt_dir / "0.pamt").write_bytes(b"\x00" * 256)

    worker = ApplyWorker.__new__(ApplyWorker)
    worker._vanilla_dir = vanilla_dir
    worker._game_dir = game_dir

    fake_entries = [PazEntry(
        path="gamedata/iteminfo.pabgb",
        paz_file=str(pamt_dir / "0.paz"),
        offset=0, comp_size=10, orig_size=10, flags=0, paz_index=0,
    )]

    SENTINEL_ERROR = "lz4 decompression failed: corrupt block at offset 0x42"

    def _fake_parse(pamt_path, paz_dir):
        return fake_entries

    def _fake_extract_raises(entry, paz_path=None):
        raise RuntimeError(SENTINEL_ERROR)

    import cdumm.archive.paz_parse as pp_mod
    import cdumm.engine.json_patch_handler as jph_mod
    pp_orig = pp_mod.parse_pamt
    jph_orig = jph_mod._extract_from_paz
    pp_mod.parse_pamt = _fake_parse
    jph_mod._extract_from_paz = _fake_extract_raises

    try:
        with caplog.at_level(logging.WARNING, logger="cdumm.engine.apply_engine"):
            result = worker._get_vanilla_entry_content(
                "0009/0.paz", "iteminfo.pabgb")

        assert result is None  # extraction failed → None as before
        assert any(SENTINEL_ERROR in rec.message for rec in caplog.records), (
            f"The underlying extraction error was silently swallowed. "
            f"Captured warnings: {[r.message for r in caplog.records]!r}. "
            f"Expected the warning to include the real cause "
            f"({SENTINEL_ERROR!r}) so users see why extraction failed."
        )
    finally:
        pp_mod.parse_pamt = pp_orig
        jph_mod._extract_from_paz = jph_orig
