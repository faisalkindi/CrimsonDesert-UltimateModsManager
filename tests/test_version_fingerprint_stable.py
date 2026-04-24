"""F1+F2: game version fingerprint must be stable across mod apply
and revert cycles.

Root cause of the "Found 21 issues that may crash the game" false
positive: _compute_game_version included PAMT file sizes for dirs
0000, 0001, 0002. Those change whenever mods are applied or reverted,
not when the game itself updates. Each mod imported during a
different mod state ended up with a different stored game_version_hash
— even though the game version was identical.

Fix: the fingerprint must derive from GAME-LEVEL state only
(Steam build ID + game exe hash). Nothing inside the game directory
that CDUMM writes to (PAMT, PAZ, PAPGT) may contribute.

This file covers both the detector change (F1) and the one-time
backfill migration (F2).
"""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path


# ── F1: detector no longer depends on PAMT sizes ─────────────────────

def test_fingerprint_excludes_pamt_sizes():
    """The combined string _compute_game_version hashes must not
    contain any '0000:' / '0001:' / '0002:' PAMT-size segments."""
    src = (Path(__file__).resolve().parents[1]
           / "src" / "cdumm" / "engine" / "version_detector.py"
           ).read_text(encoding="utf-8")
    # The old implementation iterated ["0000", "0001", "0002"] and
    # appended `{d}:{pamt.stat().st_size}`. The new one must not do
    # that — PAMT sizes change on apply/revert, which conflates game
    # version with mod state.
    assert "\"0000\"" not in src or "pamt.stat().st_size" not in src, (
        "_compute_game_version must not append PAMT file sizes — "
        "they change on apply/revert and break mod version tracking")


def test_fingerprint_stable_across_pamt_size_change(tmp_path, monkeypatch):
    """Concrete test: same exe + same Steam buildid, different PAMT
    sizes → MUST produce the same fingerprint."""
    from cdumm.engine import version_detector as vd

    # Set up a fake game_dir
    game_dir = tmp_path / "Crimson Desert"
    (game_dir / "bin64").mkdir(parents=True)
    exe = game_dir / "bin64" / "CrimsonDesert.exe"
    exe.write_bytes(b"X" * (64 * 1024 + 100))  # fixed content

    # Stub Steam build ID lookup to a constant.
    monkeypatch.setattr(vd, "_get_steam_build_id",
                        lambda _gd: "22924815")

    # Clear the lru_cache so each call re-computes.
    vd._cached_version.cache_clear()

    # Scenario 1: vanilla PAMT sizes
    for name, size in [("0000", 6794283), ("0001", 373482),
                       ("0002", 8760)]:
        d = game_dir / name
        d.mkdir(exist_ok=True)
        (d / "0.pamt").write_bytes(b"\x00" * size)
    fp_vanilla = vd.detect_game_version(game_dir)

    # Scenario 2: PAMTs modified (apply changed their sizes)
    vd._cached_version.cache_clear()
    for name, size in [("0000", 6900000), ("0001", 400000),
                       ("0002", 9000)]:
        (game_dir / name / "0.pamt").write_bytes(b"\x00" * size)
    fp_applied = vd.detect_game_version(game_dir)

    assert fp_vanilla is not None
    assert fp_applied is not None
    assert fp_vanilla == fp_applied, (
        f"fingerprint must be stable across apply/revert. "
        f"vanilla={fp_vanilla!r} applied={fp_applied!r}")


def test_fingerprint_still_changes_on_real_game_update(tmp_path, monkeypatch):
    """Regression: the fingerprint MUST still change when the game
    is actually updated (different exe contents or different Steam
    build ID)."""
    from cdumm.engine import version_detector as vd

    game_dir = tmp_path / "Crimson Desert"
    (game_dir / "bin64").mkdir(parents=True)
    exe = game_dir / "bin64" / "CrimsonDesert.exe"

    monkeypatch.setattr(vd, "_get_steam_build_id",
                        lambda _gd: "22924815")

    exe.write_bytes(b"A" * 200_000)
    vd._cached_version.cache_clear()
    fp_v1 = vd.detect_game_version(game_dir)

    # Simulate a real game update: exe content changes.
    exe.write_bytes(b"B" * 200_000)
    vd._cached_version.cache_clear()
    fp_v2 = vd.detect_game_version(game_dir)

    assert fp_v1 != fp_v2, (
        "fingerprint must change when the game exe actually changes "
        "(real patch / Steam update)")

    # And when Steam build ID changes.
    monkeypatch.setattr(vd, "_get_steam_build_id",
                        lambda _gd: "22999999")
    vd._cached_version.cache_clear()
    fp_v3 = vd.detect_game_version(game_dir)
    assert fp_v2 != fp_v3, "fingerprint must change on buildid bump"


# ── F2: one-time backfill migration ──────────────────────────────────

def test_migration_helper_exists():
    from cdumm.engine import version_detector as vd
    assert hasattr(vd, "backfill_stored_fingerprints"), (
        "need a public helper that backfills config + every mod's "
        "game_version_hash to the current detector output")


def test_migration_updates_mod_hashes(tmp_path, monkeypatch):
    """All mods' game_version_hash must be overwritten with the new
    fingerprint when the migration runs."""
    from cdumm.engine import version_detector as vd
    from cdumm.storage.database import Database
    from cdumm.storage.config import Config

    game_dir = tmp_path / "Crimson Desert"
    (game_dir / "bin64").mkdir(parents=True)
    (game_dir / "bin64" / "CrimsonDesert.exe").write_bytes(b"X" * 200_000)

    monkeypatch.setattr(vd, "_get_steam_build_id",
                        lambda _gd: "22924815")
    vd._cached_version.cache_clear()

    db = Database(tmp_path / "cdumm.db")
    db.initialize()
    # Stash two mods with old/mismatched hashes.
    db.connection.execute(
        "INSERT INTO mods (name, mod_type, enabled, priority, "
        "game_version_hash) VALUES ('A', 'paz', 1, 1, 'old_hash_1')")
    db.connection.execute(
        "INSERT INTO mods (name, mod_type, enabled, priority, "
        "game_version_hash) VALUES ('B', 'paz', 1, 2, 'old_hash_2')")
    db.connection.commit()

    new_fp = vd.detect_game_version(game_dir)
    assert new_fp

    vd.backfill_stored_fingerprints(db, game_dir)

    # Every mod now has the new fingerprint.
    rows = db.connection.execute(
        "SELECT name, game_version_hash FROM mods").fetchall()
    for name, h in rows:
        assert h == new_fp, (
            f"mod {name!r} still has old hash {h!r}, expected {new_fp!r}")

    # Config fingerprint also updated.
    assert Config(db).get("game_version_fingerprint") == new_fp

    # Migration flag is set so we don't run it again.
    assert Config(db).get("version_detector_v2") == "1"
    db.close()


def test_migration_is_idempotent(tmp_path, monkeypatch):
    """Running the migration twice must not corrupt anything — the
    second run is a no-op."""
    from cdumm.engine import version_detector as vd
    from cdumm.storage.database import Database
    from cdumm.storage.config import Config

    game_dir = tmp_path / "Crimson Desert"
    (game_dir / "bin64").mkdir(parents=True)
    (game_dir / "bin64" / "CrimsonDesert.exe").write_bytes(b"X" * 200_000)

    monkeypatch.setattr(vd, "_get_steam_build_id",
                        lambda _gd: "22924815")
    vd._cached_version.cache_clear()

    db = Database(tmp_path / "cdumm.db")
    db.initialize()
    db.connection.execute(
        "INSERT INTO mods (name, mod_type, enabled, priority, "
        "game_version_hash) VALUES ('A', 'paz', 1, 1, 'old_hash_1')")
    db.connection.commit()

    new_fp = vd.detect_game_version(game_dir)

    vd.backfill_stored_fingerprints(db, game_dir)
    # Simulate user importing a new mod with a stale value AFTER
    # the first migration (should NOT be clobbered).
    db.connection.execute(
        "INSERT INTO mods (name, mod_type, enabled, priority, "
        "game_version_hash) VALUES ('C', 'paz', 1, 3, 'fresh_value')")
    db.connection.commit()

    vd.backfill_stored_fingerprints(db, game_dir)

    rows = dict(db.connection.execute(
        "SELECT name, game_version_hash FROM mods").fetchall())
    assert rows["A"] == new_fp
    # C was inserted AFTER the flag was set — second call is a no-op,
    # so C keeps its own value.
    assert rows["C"] == "fresh_value", (
        "second migration run must not overwrite values inserted "
        "after the first migration")
    db.close()


def test_migration_called_from_startup():
    """main.py must invoke backfill_stored_fingerprints during
    startup so existing installs get fixed on next launch without
    user action."""
    src = (Path(__file__).resolve().parents[1]
           / "src" / "cdumm" / "main.py").read_text(encoding="utf-8")
    assert "backfill_stored_fingerprints" in src, (
        "main.py must call backfill_stored_fingerprints during "
        "startup — otherwise existing installs keep showing the "
        "Post-Apply Verification false positives forever")
