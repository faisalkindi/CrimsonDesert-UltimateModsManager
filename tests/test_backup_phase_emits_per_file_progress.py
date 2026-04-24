"""A3: the backup phase must emit progress per file so the UI doesn't
freeze at 2% for the full duration of _ensure_backups.

Before v3.1.7 the flow was:

  progress_updated.emit(2, "Backing up vanilla files...")
  _ensure_backups(...)   # silent loop over potentially hundreds of files
  progress_updated.emit(55, "Phase 1: Compose PAZ files")

With many mods enabled the silent window could run for minutes. This
is what several users in issue #30 called "stuck at 2%". The fix is a
per-file progress emit inside _ensure_backups so the UI advances.
"""
from __future__ import annotations

import re
from pathlib import Path


def _apply_engine_src() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "engine" / "apply_engine.py").read_text(
                encoding="utf-8")


def test_ensure_backups_emits_progress_inside_its_loop():
    """_ensure_backups must call self.progress_updated.emit(...) from
    inside its per-file loop. A single emit before the loop (the
    existing 'Backing up vanilla files...' at 2%) is not enough."""
    src = _apply_engine_src()
    # Anchor: the method definition.
    anchor = src.find("def _ensure_backups(self")
    assert anchor != -1, "_ensure_backups not found"
    # Scope: body of the method (next ~6000 chars is plenty).
    body = src[anchor:anchor + 6000]
    # Must contain at least one progress_updated.emit — the old
    # version's only emit was the pre-loop 2% call in _apply(), NOT
    # inside _ensure_backups.
    emits = re.findall(r"self\.progress_updated\.emit\(", body)
    assert emits, (
        "_ensure_backups must emit progress_updated at least once "
        "from inside its loop (not just the 2% pre-loop tick in "
        "_apply). Users were seeing 'stuck at 2%' because the whole "
        "backup phase was silent.")


def test_ensure_backups_progress_uses_file_counter():
    """The emit inside _ensure_backups must reference a per-file
    counter so percent advances as work completes. The message must
    also name the file so bug reports pinpoint the stuck one."""
    src = _apply_engine_src()
    anchor = src.find("def _ensure_backups(self")
    assert anchor != -1
    body = src[anchor:anchor + 6000]
    # Simple heuristic: some form of counter tracking plus a file_path
    # in the emitted string.
    assert re.search(r"(backup_idx|i\s*\+\s*=\s*1|enumerate)", body), (
        "expected a per-file counter (enumerate() or backup_idx++) "
        "so percent progresses across the loop")
    # The emit should mention the file being worked on so when the
    # watchdog (A1) fires it can surface the last file.
    assert "file_path" in body
