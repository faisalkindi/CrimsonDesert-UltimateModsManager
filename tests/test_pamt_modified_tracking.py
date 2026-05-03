"""Bug A (michael2k + timelesscjing on Nexus, 2026-05-03):
Post-apply verification reports `[PAPGT] 0009 PAMT hash mismatch`
and `[PAPGT] 0015 PAMT hash mismatch` after Apply, especially on
mods that ship complete .pamt files as is_new entries.

Root cause: three stage_file call sites in apply_engine.py
(is_new path ~1294, FULL_COPY fast-track ~1311, compose path
~1322) write .pamt bytes to the staging dir but DO NOT update
the modified_pamts dict. PapgtManager.rebuild() then reads the
PAMT from game_dir BEFORE txn.commit() finalizes the staged
files, so it hashes the OLD vanilla PAMT bytes. After commit
the disk has NEW bytes from staging, the PAPGT entry holds the
OLD hash, post-apply verification reports mismatch.

Other stage_file sites (1825/1834 from _compose_pamt, 1885 +
1928 from revert/safety-net paths) DO update modified_pamts so
those paths are correct.

Fix: ensure every stage_file call that targets a .pamt path
also updates modified_pamts with the same bytes.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


_APPLY_ENGINE_PATH = (
    Path(__file__).resolve().parent.parent
    / "src" / "cdumm" / "engine" / "apply_engine.py"
)


def test_apply_engine_has_pamt_aware_stage_helper():
    """Drive the refactor: there should be a single helper that does
    both stage_file AND modified_pamts bookkeeping in one call. Three
    inline sites diverged on this — having one helper means the next
    new stage site can't accidentally forget the bookkeeping."""
    src = _APPLY_ENGINE_PATH.read_text(encoding="utf-8")
    # Helper name documents intent. Any of these acceptable.
    candidates = [
        r"def _stage_with_pamt_tracking\(",
        r"def _stage_pamt_aware\(",
        r"def _stage_file_track_pamt\(",
    ]
    found = any(re.search(p, src) for p in candidates)
    assert found, (
        "apply_engine.py needs a single helper that wraps "
        "txn.stage_file + modified_pamts bookkeeping. The current "
        "pattern of inline updates at multiple sites caused Bug A: "
        "stage_file sites at line ~1294 (is_new), ~1311 (FULL_COPY), "
        "and ~1322 (compose) staged .pamt bytes without updating "
        "modified_pamts, so PAPGT rebuild hashed the stale on-disk "
        "PAMT instead of the staged bytes. Result: post-apply "
        "verification reported PAMT hash mismatch."
    )


def test_pamt_aware_stage_helper_updates_dict():
    """Direct unit test: helper must update modified_pamts when the
    file_path ends in .pamt, and must leave it unchanged for other
    file types."""
    from cdumm.engine.apply_engine import ApplyWorker

    worker = ApplyWorker.__new__(ApplyWorker)

    class _RecordingTxn:
        def __init__(self):
            self.staged: list[tuple[str, bytes]] = []

        def stage_file(self, path, data):
            self.staged.append((path, bytes(data)))

    txn = _RecordingTxn()
    modified_pamts: dict[str, bytes] = {}

    helper = (
        getattr(worker, "_stage_with_pamt_tracking", None)
        or getattr(worker, "_stage_pamt_aware", None)
        or getattr(worker, "_stage_file_track_pamt", None)
    )
    assert helper is not None, (
        "Helper missing — see test_apply_engine_has_pamt_aware_stage_helper"
    )

    # PAMT path → updates dict
    helper(txn, "0009/0.pamt", b"PAMT_BYTES_AAA", modified_pamts)
    assert modified_pamts.get("0009") == b"PAMT_BYTES_AAA", (
        f"Helper did not update modified_pamts for .pamt stage. "
        f"Dict: {modified_pamts!r}"
    )
    assert ("0009/0.pamt", b"PAMT_BYTES_AAA") in txn.staged

    # Non-PAMT path → leaves dict alone
    before = dict(modified_pamts)
    helper(txn, "0009/0.paz", b"PAZ_BYTES", modified_pamts)
    assert modified_pamts == before, (
        f"Helper updated modified_pamts for non-.pamt path. "
        f"Before: {before!r} After: {modified_pamts!r}"
    )
    assert ("0009/0.paz", b"PAZ_BYTES") in txn.staged
