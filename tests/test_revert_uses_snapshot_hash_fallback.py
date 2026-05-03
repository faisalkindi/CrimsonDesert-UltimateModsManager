"""GitHub #67 (Doleun on v3.2.8, 2026-05-03) — '2 file(s) could not
be reverted (no backup found): 0042/0.pamt, 0042/0.paz' after game
patch 1.05.01 added a new PAZ group.

Sequence: user has stale mod_deltas rows referencing 0042 (e.g. a
prior standalone mod was assigned to that dir number before the
game patch added 0042 as vanilla). Game patch + Steam Verify +
Rescan rebuilds the snapshot to include the new vanilla 0042
files. The vanilla backup directory does NOT have backups for
large PAZ files (skipped by _refresh_vanilla_backups since they're
backed up lazily). Revert iterates mod_deltas, hits 0042, calls
_get_vanilla_bytes which only looks at the backup dir + range
backup — neither exists for 0042 — returns None — Revert errors.

ApplyWorker has a hash-verified-live fallback (resolve_vanilla_source
returns the live file when its hash matches the snapshot, since
that's already vanilla bytes by definition). RevertWorker's
_get_vanilla_bytes does not. Mirror the fallback here so revert
treats already-vanilla files as no-ops instead of errors.
"""
from __future__ import annotations
import hashlib
from pathlib import Path

import pytest


def test_revert_get_vanilla_bytes_falls_back_to_snapshot_match(tmp_path: Path):
    """When the vanilla backup is missing but the live file's hash
    matches the snapshot, _get_vanilla_bytes must return the live
    bytes (the file is already vanilla — restoring it is a no-op,
    but the path must succeed instead of failing)."""
    from cdumm.engine.apply_engine import RevertWorker
    from cdumm.engine.snapshot_manager import hash_file
    from cdumm.storage.database import Database

    game_dir = tmp_path / "game"
    vanilla_dir = tmp_path / "vanilla"
    cdmods = game_dir / "CDMods"
    cdmods.mkdir(parents=True)
    vanilla_dir.mkdir()
    # Game has 0042/0.paz with vanilla bytes; vanilla_dir has NO backup
    paz_dir = game_dir / "0042"
    paz_dir.mkdir()
    paz_bytes = b"VANILLA_PAZ_BYTES" * 100
    (paz_dir / "0.paz").write_bytes(paz_bytes)

    db_path = tmp_path / "test.db"
    db = Database(db_path)
    db.initialize()
    # Store snapshot row with the live file's hash + size.
    # hash_file returns (hash_str, size_int).
    live_hash, _ = hash_file(paz_dir / "0.paz")
    db.connection.execute(
        "INSERT INTO snapshots (file_path, file_hash, file_size) "
        "VALUES (?, ?, ?)",
        ("0042/0.paz", live_hash, len(paz_bytes)))
    db.connection.commit()
    db.close()

    worker = RevertWorker.__new__(RevertWorker)
    worker._game_dir = game_dir
    worker._vanilla_dir = vanilla_dir
    worker._db = Database(db_path)
    worker._db.initialize()

    result = worker._get_vanilla_bytes("0042/0.paz")
    assert result == paz_bytes, (
        f"Revert _get_vanilla_bytes returned {result!r} for a file "
        f"whose live hash matches the snapshot. Should return the "
        f"live bytes so revert treats it as already-vanilla instead "
        f"of failing with 'no backup found'. Doleun's #67 case."
    )
    worker._db.close()


def test_revert_get_vanilla_bytes_returns_none_when_live_diverges(tmp_path: Path):
    """Safety check: if the live file does NOT match the snapshot
    (it's actually modded), _get_vanilla_bytes still returns None.
    The fallback only activates when live IS verified vanilla."""
    from cdumm.engine.apply_engine import RevertWorker
    from cdumm.engine.snapshot_manager import hash_file
    from cdumm.storage.database import Database

    game_dir = tmp_path / "game"
    vanilla_dir = tmp_path / "vanilla"
    (game_dir / "CDMods").mkdir(parents=True)
    vanilla_dir.mkdir()
    paz_dir = game_dir / "0042"
    paz_dir.mkdir()

    # Snapshot says vanilla content, but live is modded
    vanilla_bytes = b"VANILLA" * 50
    modded_bytes = b"MODDED__" * 50
    (paz_dir / "0.paz").write_bytes(modded_bytes)

    # Compute hash of vanilla bytes (what snapshot recorded)
    h = hashlib.sha256(vanilla_bytes).hexdigest()

    db_path = tmp_path / "test.db"
    db = Database(db_path)
    db.initialize()
    db.connection.execute(
        "INSERT INTO snapshots (file_path, file_hash, file_size) "
        "VALUES (?, ?, ?)",
        ("0042/0.paz", h, len(vanilla_bytes)))
    db.connection.commit()
    db.close()

    worker = RevertWorker.__new__(RevertWorker)
    worker._game_dir = game_dir
    worker._vanilla_dir = vanilla_dir
    worker._db = Database(db_path)
    worker._db.initialize()

    result = worker._get_vanilla_bytes("0042/0.paz")
    # Live file is actually modded (size differs from snapshot, hash
    # won't match) — fallback must NOT trust it.
    assert result is None, (
        f"_get_vanilla_bytes returned {result!r} but the live file "
        f"differs from the snapshot. The hash-verified fallback must "
        f"reject divergent live files to avoid baking modded bytes "
        f"into a 'restore' operation."
    )
    worker._db.close()
