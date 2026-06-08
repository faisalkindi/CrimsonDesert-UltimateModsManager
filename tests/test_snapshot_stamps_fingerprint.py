"""GitHub #163 (xenoi60 Self-Reimporting) and Faisal's local recovery
loop: clicking Start Recovery takes a fresh snapshot, but on the next
launch CDUMM says the snapshot does not match and prompts recovery
again, forever.

Root cause (confirmed from Faisal's cdumm.log on CD v1.10): the snapshot
runs in a worker SUBPROCESS, then the main thread tried to stamp the
game-version fingerprint in _on_snapshot_finished. At that moment the
SQLite DB was still write-locked (the same lock that made the WAL
checkpoint log "database table is locked"), so the
``Config.set("game_version_fingerprint", fp)`` threw and was swallowed
by a bare ``except Exception: pass``. The stale fingerprint survived
every snapshot, so main.py's startup check (stored_fp != current_fp ->
game_updated=True) re-prompted recovery on every launch.

Fix: stamp the fingerprint inside the snapshot worker's own transaction,
on the same connection that writes the snapshot, so it is atomic with
the snapshot and can never lose to the cross-process lock race. This
test pins that a completed snapshot leaves the fingerprint stamped.
"""
from __future__ import annotations

from pathlib import Path

from cdumm.engine.snapshot_manager import SnapshotWorker
from cdumm.engine.version_detector import detect_game_version
from cdumm.storage.config import Config
from cdumm.storage.database import Database


def _make_game(tmp_path: Path) -> Path:
    game_dir = tmp_path / "game"
    d = game_dir / "0008"
    d.mkdir(parents=True)
    (d / "0.pamt").write_bytes(b"PAMT" + b"\x00" * 50)
    (d / "0.paz").write_bytes(b"PAZ" + b"\x00" * 80)
    (game_dir / "bin64").mkdir()
    # detect_game_version fingerprints this exe; give it real bytes.
    (game_dir / "bin64" / "CrimsonDesert.exe").write_bytes(b"MZ" + b"\x90" * 4096)
    return game_dir


def test_snapshot_stamps_game_version_fingerprint(tmp_path: Path):
    game_dir = _make_game(tmp_path)
    db = Database(tmp_path / "cdumm.db")
    db.initialize()

    # Precondition: no fingerprint stored yet.
    assert Config(db).get("game_version_fingerprint") is None

    SnapshotWorker(game_dir=game_dir, db_path=db.db_path).run()

    stored = Config(db).get("game_version_fingerprint")
    expected = detect_game_version(game_dir)
    assert expected, "test setup: detect_game_version should return a value"
    assert stored == expected, (
        "the snapshot worker must stamp the current game-version "
        "fingerprint so the startup game-updated check does not loop "
        f"(stored={stored!r}, expected={expected!r})")
    db.close()


def test_snapshot_refreshes_a_stale_fingerprint(tmp_path: Path):
    """The loop case: a stale fingerprint from an older game version
    must be overwritten by the snapshot, not left in place."""
    game_dir = _make_game(tmp_path)
    db = Database(tmp_path / "cdumm.db")
    db.initialize()
    # Simulate the stale value (old game version) that caused the loop.
    Config(db).set("game_version_fingerprint", "6791f383b57e0c37_stale")

    SnapshotWorker(game_dir=game_dir, db_path=db.db_path).run()

    stored = Config(db).get("game_version_fingerprint")
    assert stored == detect_game_version(game_dir)
    assert "stale" not in (stored or ""), "stale fingerprint must be replaced"
    db.close()
