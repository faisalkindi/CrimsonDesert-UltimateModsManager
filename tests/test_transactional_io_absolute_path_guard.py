"""GitHub #65 followup (tbyk101 still failing on v3.2.8.1):

    File "shutil.py", line 918, in move
    FileNotFoundError: [WinError 2] ... '<game>\0012\4.paz' -> '<game>\0012\4.paz'

The src and dst in the os.rename error are IDENTICAL strings.
That can only happen when the rel_path passed to stage_file is
an absolute path: `Path(staging_dir) / abspath_str` returns the
absolute path verbatim (Windows Path semantics), so both staged
and target collapse to the same absolute path. shutil.move then
calls os.rename with src == dst and the source doesn't exist.

The actual caller bug lives somewhere upstream that constructs an
absolute file_path. Until we trace it, transactional_io must
defensively reject absolute paths so the failure mode is a clear
ValueError that names the caller, not a baffling WinError 2 with
identical src and dst.
"""
from __future__ import annotations
from pathlib import Path

import pytest


def test_stage_file_rejects_absolute_path(tmp_path: Path):
    """Absolute paths OUTSIDE game_dir must still hard-raise. Faisal
    2026-05-12 #103 softened the in-game_dir case to accept-and-
    normalise (legacy DB rows with absolute file_path can still
    revert), but a path that does NOT live under game_dir is a
    genuine importer bug and the guard must surface it."""
    from cdumm.archive.transactional_io import TransactionalIO

    game_dir = tmp_path / "game"
    staging_dir = game_dir / ".cdumm_staging"
    game_dir.mkdir()
    staging_dir.mkdir()

    txn = TransactionalIO(game_dir, staging_dir)

    outside_abs = str(tmp_path / "outside" / "0012" / "4.paz")
    with pytest.raises(ValueError) as exc_info:
        txn.stage_file(outside_abs, b"PAZ_BYTES")

    msg = str(exc_info.value)
    assert "absolute" in msg.lower() or "relative" in msg.lower(), (
        f"Error must explain that rel_path is absolute. Got: {msg!r}"
    )


def test_stage_file_normalises_absolute_path_under_game_dir(
        tmp_path: Path):
    """Legacy DB rows that stored an absolute file_path UNDER game_dir
    must be stripped and staged successfully (Faisal 2026-05-12 #103
    OneManErmey). The guard logs a warning with the caller stack but
    keeps the apply moving so users with stale buggy rows can still
    revert without forcing a re-import."""
    from cdumm.archive.transactional_io import TransactionalIO

    game_dir = tmp_path / "game"
    staging_dir = game_dir / ".cdumm_staging"
    game_dir.mkdir()
    staging_dir.mkdir()

    txn = TransactionalIO(game_dir, staging_dir)
    abs_under = str(game_dir / "0012" / "4.paz")
    txn.stage_file(abs_under, b"PAZ_BYTES")

    staged = staging_dir / "0012" / "4.paz"
    assert staged.exists()
    assert staged.read_bytes() == b"PAZ_BYTES"


def test_stage_file_accepts_relative_path(tmp_path: Path):
    from cdumm.archive.transactional_io import TransactionalIO

    game_dir = tmp_path / "game"
    staging_dir = game_dir / ".cdumm_staging"
    game_dir.mkdir()
    staging_dir.mkdir()

    txn = TransactionalIO(game_dir, staging_dir)
    txn.stage_file("0012/4.paz", b"PAZ_BYTES")

    staged = staging_dir / "0012" / "4.paz"
    assert staged.exists()
    assert staged.read_bytes() == b"PAZ_BYTES"


def test_commit_detects_same_src_dst_explicitly(tmp_path: Path):
    """Defense in depth: even if an absolute path slips past the
    stage_file guard somehow, commit must NOT call shutil.move with
    src == dst. It should fail with a clear error."""
    from cdumm.archive.transactional_io import TransactionalIO

    game_dir = tmp_path / "game"
    staging_dir = game_dir / ".cdumm_staging"
    game_dir.mkdir()
    staging_dir.mkdir()

    txn = TransactionalIO(game_dir, staging_dir)
    # Forcibly inject an absolute path bypassing stage_file (simulating
    # the buggy upstream caller).
    abs_str = str(game_dir / "0012" / "4.paz")
    (game_dir / "0012").mkdir()
    (game_dir / "0012" / "4.paz").write_bytes(b"existing")
    txn._staged_files.append(abs_str)

    with pytest.raises(Exception) as exc_info:
        txn.commit()

    # Must NOT be a confusing "[WinError 2] '<x>' -> '<x>'" — must be
    # a recognizable error that names the path collision.
    msg = str(exc_info.value).lower()
    assert "same path" in msg or "absolute" in msg or "identical" in msg, (
        f"Commit must surface a clear error when src == dst. Got: "
        f"{exc_info.value!r}"
    )
