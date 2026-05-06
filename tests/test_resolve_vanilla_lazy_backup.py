"""GitHub #68 (mit999sif) follow-up: when ``resolve_vanilla_source``
falls back to a hash-verified live PAZ, it should lazily create the
vanilla backup so subsequent applies don't trip the same "backup
missing" warning forever.

Before this fix: user runs Fix Everything which clears backups, then
applies. Apply hits the live-as-vanilla self-heal path, warns, and
applies. Next apply, same warning fires again. The "Run Settings ->
Fix Everything to refresh backups" message in the warning was even
misleading because Fix Everything CLEARS backups instead of refreshing
all of them (it only refreshes the ones critical for enabled JSON
mods).

After this fix: the first apply that uses hash-verified live ALSO
copies the file to vanilla_dir. Next apply finds the backup, no
warning. The system is now self-healing without user intervention.
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def test_resolve_vanilla_source_lazily_backs_up_hash_verified_live(
    monkeypatch, tmp_path,
):
    """When live PAZ hash matches the snapshot fingerprint, the
    resolver should:
      1. Return the live entry (current behavior — no regression)
      2. Copy the live PAZ + sibling PAMT to vanilla_dir/ so next
         apply doesn't re-trigger the warning"""
    from cdumm.engine.apply_engine import resolve_vanilla_source

    vanilla_dir = tmp_path / "vanilla"
    game_dir = tmp_path / "game"
    vanilla_dir.mkdir()
    (game_dir / "0008").mkdir(parents=True)

    live_paz = game_dir / "0008" / "0.paz"
    live_paz.write_bytes(b"PRISTINE_PAZ_BYTES")
    live_pamt = game_dir / "0008" / "0.pamt"
    live_pamt.write_bytes(b"PRISTINE_PAMT_BYTES")

    expected_hash = hashlib.sha256(b"PRISTINE_PAZ_BYTES").hexdigest()

    fake_entry = MagicMock()
    fake_entry.paz_file = str(live_paz)
    fake_entry.path = "iteminfo.pabgb"

    snapshot_mgr = MagicMock()
    snapshot_mgr.get_file_hash.return_value = expected_hash

    def _fake_hash_file(p):
        return hashlib.sha256(Path(p).read_bytes()).hexdigest(), 0

    monkeypatch.setattr(
        "cdumm.engine.snapshot_manager.hash_file", _fake_hash_file)
    monkeypatch.setattr(
        "cdumm.engine.json_patch_handler._find_pamt_entry",
        lambda target, base_dir:
            fake_entry if base_dir == game_dir else None)

    warns: list[str] = []
    result = resolve_vanilla_source(
        "iteminfo.pabgb", vanilla_dir, game_dir, snapshot_mgr,
        warn_callback=warns.append)

    # Resolver returned the live entry (existing behavior)
    assert result is fake_entry

    # Lazy backup created
    backup_paz = vanilla_dir / "0008" / "0.paz"
    backup_pamt = vanilla_dir / "0008" / "0.pamt"
    assert backup_paz.exists(), (
        "live PAZ must be copied to vanilla_dir as a lazy backup")
    assert backup_pamt.exists(), (
        "sibling PAMT must also be copied so _find_pamt_entry can "
        "resolve from vanilla_dir on next apply")
    # Bytes match (real copy, not a stub)
    assert backup_paz.read_bytes() == b"PRISTINE_PAZ_BYTES"
    assert backup_pamt.read_bytes() == b"PRISTINE_PAMT_BYTES"


def test_resolve_vanilla_source_idempotent_after_backup(
    monkeypatch, tmp_path,
):
    """Subsequent apply that finds the lazy-created backup must NOT
    re-trigger the warn_callback. The backup is the source of truth
    once it exists."""
    from cdumm.engine.apply_engine import resolve_vanilla_source

    vanilla_dir = tmp_path / "vanilla"
    game_dir = tmp_path / "game"
    (vanilla_dir / "0008").mkdir(parents=True)
    (game_dir / "0008").mkdir(parents=True)

    backup_paz = vanilla_dir / "0008" / "0.paz"
    backup_paz.write_bytes(b"PRISTINE")
    (vanilla_dir / "0008" / "0.pamt").write_bytes(b"PAMT")
    (game_dir / "0008" / "0.paz").write_bytes(b"PRISTINE")

    backup_entry = MagicMock()
    backup_entry.paz_file = str(backup_paz)
    backup_entry.path = "iteminfo.pabgb"

    monkeypatch.setattr(
        "cdumm.engine.json_patch_handler._find_pamt_entry",
        lambda target, base_dir:
            backup_entry if base_dir == vanilla_dir else None)

    warns: list[str] = []
    result = resolve_vanilla_source(
        "iteminfo.pabgb", vanilla_dir, game_dir,
        snapshot_mgr=MagicMock(),  # should not be touched
        warn_callback=warns.append)

    assert result is backup_entry
    assert warns == [], (
        "no warning should fire when backup exists in vanilla_dir")


def test_resolve_vanilla_source_skips_lazy_backup_on_hash_mismatch(
    monkeypatch, tmp_path,
):
    """When live hash diverges from snapshot, the resolver raises
    VanillaSourceUnavailable AND must NOT create a backup of the
    modded bytes (which would poison future applies)."""
    from cdumm.engine.apply_engine import resolve_vanilla_source
    from cdumm.engine.json_patch_handler import VanillaSourceUnavailable

    vanilla_dir = tmp_path / "vanilla"
    game_dir = tmp_path / "game"
    vanilla_dir.mkdir()
    (game_dir / "0008").mkdir(parents=True)

    live_paz = game_dir / "0008" / "0.paz"
    live_paz.write_bytes(b"MODDED")

    fake_entry = MagicMock()
    fake_entry.paz_file = str(live_paz)
    fake_entry.path = "iteminfo.pabgb"

    snapshot_mgr = MagicMock()
    snapshot_mgr.get_file_hash.return_value = "DIFFERENT_HASH"

    monkeypatch.setattr(
        "cdumm.engine.snapshot_manager.hash_file",
        lambda p: ("LIVE_HASH", 0))
    monkeypatch.setattr(
        "cdumm.engine.json_patch_handler._find_pamt_entry",
        lambda target, base_dir:
            fake_entry if base_dir == game_dir else None)

    with pytest.raises(VanillaSourceUnavailable):
        resolve_vanilla_source(
            "iteminfo.pabgb", vanilla_dir, game_dir, snapshot_mgr,
            warn_callback=lambda _: None)

    backup_paz = vanilla_dir / "0008" / "0.paz"
    assert not backup_paz.exists(), (
        "must not back up modded bytes when hash mismatches")
