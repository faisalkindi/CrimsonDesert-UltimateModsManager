"""I1: Rescan must refuse to capture a modded disk as "vanilla".

Root cause of the hours-long Frankenstein loop we hit tonight:
``_on_refresh_snapshot`` calls the SnapshotWorker unconditionally,
and the worker hashes whatever bytes are on disk and stores them
as vanilla. If the disk is in a modded (or post-game-update-without-
verify) state, the snapshot baked in wrong hashes and every future
revert restored to that wrong state.

Fix: before clearing vanilla backups and rescanning, sample several
existing backup files in ``vanilla/`` against the live game files.
If any live file differs from its backup (and the backup passes
its own sanity checks), the disk has been modified since the last
apply — rescan would capture modded bytes as vanilla. Refuse.

This file tests the pure-logic helper. Wiring into
``_on_refresh_snapshot`` is covered by a separate grep-based guard.
"""
from __future__ import annotations

import re
from pathlib import Path


# ── Pure-logic helper ────────────────────────────────────────────────

def test_helper_exists():
    from cdumm.engine import snapshot_manager as sm
    assert hasattr(sm, "verify_live_disk_matches_backups")


def test_returns_clean_when_no_backups(tmp_path):
    from cdumm.engine.snapshot_manager import verify_live_disk_matches_backups
    game = tmp_path / "game"
    game.mkdir()
    vanilla = tmp_path / "vanilla"
    vanilla.mkdir()
    ok, problems = verify_live_disk_matches_backups(game, vanilla)
    assert ok is True
    assert problems == []


def test_returns_clean_when_backups_match_live(tmp_path):
    from cdumm.engine.snapshot_manager import verify_live_disk_matches_backups
    game = tmp_path / "game"
    (game / "0008").mkdir(parents=True)
    (game / "0008" / "0.paz").write_bytes(b"vanilla-bytes")
    vanilla = tmp_path / "vanilla"
    (vanilla / "0008").mkdir(parents=True)
    (vanilla / "0008" / "0.paz").write_bytes(b"vanilla-bytes")
    ok, problems = verify_live_disk_matches_backups(game, vanilla)
    assert ok is True
    assert problems == []


def test_returns_dirty_when_live_differs_from_backup(tmp_path):
    from cdumm.engine.snapshot_manager import verify_live_disk_matches_backups
    game = tmp_path / "game"
    (game / "0008").mkdir(parents=True)
    (game / "0008" / "0.paz").write_bytes(b"MODDED-bytes")  # different
    vanilla = tmp_path / "vanilla"
    (vanilla / "0008").mkdir(parents=True)
    (vanilla / "0008" / "0.paz").write_bytes(b"vanilla-bytes")
    ok, problems = verify_live_disk_matches_backups(game, vanilla)
    assert ok is False
    assert any("0.paz" in p for p in problems), (
        "problem list must name the offending file")


def test_live_file_missing_on_disk_is_fine(tmp_path):
    """A backup whose live counterpart doesn't exist is not proof of
    modding — CDUMM may have renamed/moved the file. Skip these."""
    from cdumm.engine.snapshot_manager import verify_live_disk_matches_backups
    game = tmp_path / "game"
    game.mkdir()  # live dir exists but no files
    vanilla = tmp_path / "vanilla"
    (vanilla / "0008").mkdir(parents=True)
    (vanilla / "0008" / "0.paz").write_bytes(b"whatever")
    ok, problems = verify_live_disk_matches_backups(game, vanilla)
    assert ok is True


def test_range_backup_extension_is_ignored(tmp_path):
    """Range backups (``*.vranges``) aren't raw file backups — they
    store byte-range diffs. The helper must skip them."""
    from cdumm.engine.snapshot_manager import verify_live_disk_matches_backups
    game = tmp_path / "game"
    game.mkdir()
    vanilla = tmp_path / "vanilla"
    vanilla.mkdir()
    # A range backup with no matching full backup.
    (vanilla / "0008_0.paz.vranges").write_bytes(b"range-backup-data")
    ok, _ = verify_live_disk_matches_backups(game, vanilla)
    assert ok is True, (
        "range-backup files should be skipped — they're not "
        "byte-for-byte vanilla copies")


# ── Wiring guard ─────────────────────────────────────────────────────

def _fluent_src() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "gui" / "fluent_window.py").read_text(
                encoding="utf-8")


def test_on_refresh_snapshot_gates_on_backup_check():
    """_on_refresh_snapshot must call the helper BEFORE clearing
    ``vanilla_dir``. If the check fails, abort with a clear message."""
    src = _fluent_src()
    anchor = src.find("def _on_refresh_snapshot")
    assert anchor != -1
    next_def = src.find("\n    def ", anchor + 20)
    body = src[anchor:next_def if next_def != -1 else anchor + 4000]
    assert "verify_live_disk_matches_backups" in body, (
        "_on_refresh_snapshot must call verify_live_disk_matches_backups "
        "so rescan can't capture a modded disk as vanilla")
    # Guard must be BEFORE the rmtree of vanilla_dir — otherwise
    # backups are gone and the check is meaningless.
    check_idx = body.find("verify_live_disk_matches_backups")
    rmtree_idx = body.find("shutil.rmtree")
    if rmtree_idx != -1:
        assert check_idx < rmtree_idx, (
            "verify_live_disk_matches_backups must run BEFORE the "
            "vanilla backups are cleared")
