"""H1: Fix Everything must NOT run the revert step when Steam just
verified. Reverting with stale/Frankenstein backups overwrites the
clean Steam-verified game files, producing the "40 files reacquired"
Steam loop users kept hitting post-game-update.

Before this fix: ``_run_fix`` at worker_process.py unconditionally
reverted using whatever's in ``vanilla/``, even when the Steam-
Verified checkbox was set — directly undoing the user's recovery.

After: Steam-verified branch skips revert entirely. Game is already
clean per Steam. Clean orphans + clear backups + done.
"""
from __future__ import annotations

import re
from pathlib import Path


def _worker_src() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "worker_process.py").read_text(
                encoding="utf-8")


def test_run_fix_skips_revert_when_steam_verified():
    """When steam_verified=True, the revert step must be gated off
    so stale backups don't overwrite clean Steam-repaired files."""
    src = _worker_src()
    anchor = src.find("def _run_fix")
    assert anchor != -1
    next_def = src.find("\ndef ", anchor + 20)
    body = src[anchor:next_def if next_def != -1 else anchor + 6000]

    # The revert step must now live behind a guard that checks the
    # steam_verified flag (steam variable is set from steam_verified
    # == "1"). Simplest form: `if not steam:` around the revert.
    # Accept either an explicit `if not steam:` wrapping the revert,
    # or the revert block being reachable only on that branch.
    revert_idx = body.find("RevertWorker")
    assert revert_idx != -1, "revert still needs to exist for the non-steam branch"

    # Between the start of the body and the revert call, the code
    # must check `steam`. Heuristic: there's an `if not steam` OR
    # `if steam:` branch before the revert call.
    prelude = body[:revert_idx]
    assert re.search(r"if\s+not\s+steam\b|if\s+steam:", prelude), (
        "_run_fix must gate the revert step on steam_verified. "
        "Running revert after a Steam Verify re-writes the clean "
        "game files with stale backups, breaking recovery.")


def test_run_fix_still_clears_backups_on_verified_path():
    """Regression: Steam-verified path must still clear backups and
    clean orphans. That's the whole point of the flow."""
    src = _worker_src()
    anchor = src.find("def _run_fix")
    next_def = src.find("\ndef ", anchor + 20)
    body = src[anchor:next_def if next_def != -1 else anchor + 6000]
    assert "shutil.rmtree(vanilla_dir" in body, (
        "must still wipe vanilla backups on the steam-verified path")
    assert "orphan" in body.lower(), (
        "orphan-dir cleanup must still run")


def test_non_verified_path_still_reverts():
    """Quick Fix (steam_verified=False) must keep reverting — that's
    how users roll back mod changes WITHOUT a Steam reset."""
    src = _worker_src()
    anchor = src.find("def _run_fix")
    next_def = src.find("\ndef ", anchor + 20)
    body = src[anchor:next_def if next_def != -1 else anchor + 6000]
    # RevertWorker instantiation must still be in the source —
    # gated on the non-verified branch.
    assert "RevertWorker(" in body


def test_result_message_differs_by_path():
    """UI must tell the user what ACTUALLY happened. If we skipped
    revert, don't say 'Revert Complete' — that's misleading."""
    src = _worker_src()
    anchor = src.find("def _run_fix")
    next_def = src.find("\ndef ", anchor + 20)
    body = src[anchor:next_def if next_def != -1 else anchor + 6000]
    # On the verified branch we should emit a specific marker like
    # "Revert Skipped" or "Steam-verified" in a result card.
    assert (re.search(r"[Ss]kipped", body)
            or re.search(r"[Ss]team.?verified", body)), (
        "when revert is skipped, the result card must say so "
        "instead of silently claiming 'Revert Complete'")
