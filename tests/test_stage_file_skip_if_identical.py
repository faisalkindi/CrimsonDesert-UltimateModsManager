"""Perf optimization: ``TransactionalIO.stage_file_if_changed`` must skip
the stage write when the target file already has byte-identical content.

Context — v3.1.5 Phase 3 revert tuning. The apply pipeline currently
stages every reverted file (read vanilla backup + write to staging + atomic
rename), even when the live game file already matches vanilla from a
prior revert. For a 35s apply observed at 2026-04-22 04:18, Phase 3 alone
ate most of the wallclock; many files were already vanilla and only needed
a hash-level confirmation, not a full ~100 MB read+write cycle.

Contract:
- If target exists with identical bytes → no stage, no tracking, return False.
- If target differs or doesn't exist → delegate to stage_file, return True.
- Staged files list must reflect only real stages (skip doesn't appear).
"""
from __future__ import annotations

from pathlib import Path

from cdumm.archive.transactional_io import TransactionalIO


def test_skip_when_target_bytes_match(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    staging = tmp_path / "staging"
    game_dir.mkdir()
    staging.mkdir()

    target = game_dir / "meta" / "0.pathc"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"same-content")

    txn = TransactionalIO(game_dir, staging)
    staged = txn.stage_file_if_changed("meta/0.pathc", b"same-content")

    assert staged is False, "should skip — target already matches"
    assert "meta/0.pathc" not in txn._staged_files
    assert not (staging / "meta" / "0.pathc").exists()


def test_stage_when_target_bytes_differ(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    staging = tmp_path / "staging"
    game_dir.mkdir()
    staging.mkdir()

    target = game_dir / "meta" / "0.pathc"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old-content")

    txn = TransactionalIO(game_dir, staging)
    staged = txn.stage_file_if_changed("meta/0.pathc", b"new-content")

    assert staged is True
    assert "meta/0.pathc" in txn._staged_files
    assert (staging / "meta" / "0.pathc").read_bytes() == b"new-content"


def test_stage_when_target_missing(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    staging = tmp_path / "staging"
    game_dir.mkdir()
    staging.mkdir()

    txn = TransactionalIO(game_dir, staging)
    staged = txn.stage_file_if_changed("meta/0.pathc", b"fresh")

    assert staged is True
    assert "meta/0.pathc" in txn._staged_files
    assert (staging / "meta" / "0.pathc").read_bytes() == b"fresh"


def test_skip_is_fast_path_does_not_create_staging_file(tmp_path: Path) -> None:
    """Skip path must not write to staging at all (proves we didn't do
    a hidden write that would cost IO)."""
    game_dir = tmp_path / "game"
    staging = tmp_path / "staging"
    game_dir.mkdir()
    staging.mkdir()

    target = game_dir / "0012" / "2.paz"
    target.parent.mkdir(parents=True)
    payload = b"x" * (1024 * 1024)
    target.write_bytes(payload)

    txn = TransactionalIO(game_dir, staging)
    txn.stage_file_if_changed("0012/2.paz", payload)

    staging_file = staging / "0012" / "2.paz"
    assert not staging_file.exists(), (
        "stage_file_if_changed must not create staging file on skip — "
        "we're optimizing for avoided IO")
