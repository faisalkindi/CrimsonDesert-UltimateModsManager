"""GitHub #62 (UnLuckyLust) + #68 (mit999sif): Format 3 mod targeting
iteminfo.pabgb crashes the iteminfo writer with a parse error like
"CArray count 15386081 exceeds remaining bytes 5338665" because the
extractor fell back to the LIVE (already-modded) game file when the
vanilla backup PAZ was missing.

Root cause: ``_vanilla_extractor`` inside
``aggregate_json_mods_into_synthetic_patches`` looks in ``vanilla_dir``
first and silently falls back to ``game_dir`` without verifying the
live file's hash matches the snapshot. When previous mods have already
applied to the live game, those bytes are no longer vanilla, but the
extractor returns them anyway.

Format 3 mods feed those bytes to dedicated whole-table parsers
(crimson_rs / skill parser / buffinfo parser). Those parsers expect
vanilla layout; modded bytes have shifted offsets and produce garbage
counts → ValueError or worse.

This test pins: when the live PAZ's hash diverges from the snapshot
fingerprint, the extractor must return None (NOT modded bytes).
``expand_format3_into_aggregated``'s existing "could not extract
vanilla bytes" warning then surfaces a clean message to the user
instead of a cryptic CArray parse error from the writer.
"""
from __future__ import annotations

import hashlib
import struct
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def test_format3_vanilla_extractor_rejects_modded_live_paz(monkeypatch):
    """Set up a fake game dir with a live PAZ whose hash does NOT match
    the snapshot, and a missing vanilla backup. The extractor must
    return None for that target instead of returning the modded bytes.
    """
    from cdumm.engine import apply_engine

    # Stand up directories
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        vanilla_dir = tmp / "vanilla"
        game_dir = tmp / "game"
        vanilla_dir.mkdir()
        game_dir.mkdir()
        (game_dir / "0008").mkdir()

        # Live PAZ + PAMT in game_dir (modded — will mismatch snapshot)
        live_paz = game_dir / "0008" / "0.paz"
        live_paz.write_bytes(b"MODDED" * 100)
        live_pamt = game_dir / "0008" / "0.pamt"
        live_pamt.write_bytes(b"")  # we'll mock parse_pamt to bypass

        # No vanilla backup (vanilla_dir has no 0008/)

        # Snapshot says the live PAZ should hash to something else
        # (i.e., it's been modded since snapshot was taken)
        snapshot_mgr = MagicMock()
        snapshot_mgr.get_file_hash.return_value = (
            "abc123_known_vanilla_hash")

        # Mock _find_pamt_entry to return a fake live entry
        fake_entry = MagicMock()
        fake_entry.paz_file = str(live_paz)
        fake_entry.path = "iteminfo.pabgb"

        from cdumm.engine.json_patch_handler import VanillaSourceUnavailable

        def _fake_find_pamt_entry(target, base_dir):
            if base_dir == vanilla_dir:
                return None  # no backup
            return fake_entry  # live exists

        monkeypatch.setattr(
            "cdumm.engine.json_patch_handler._find_pamt_entry",
            _fake_find_pamt_entry)

        # Build the same vanilla extractor the apply path uses, with
        # the new hash-verification behavior.
        extractor = apply_engine._make_format3_vanilla_extractor(
            vanilla_dir=vanilla_dir,
            game_dir=game_dir,
            snapshot_mgr=snapshot_mgr,
            get_vanilla_entry_content=lambda fp, target: b"...some bytes...",
            extract_sibling_entry=lambda pamt_dir, hp: b"...sibling...",
        )

        result = extractor("iteminfo.pabgb")
        assert result is None, (
            "extractor must refuse to return modded live bytes when "
            "the snapshot hash check fails")


def test_format3_vanilla_extractor_returns_bytes_when_backup_present(
    monkeypatch, tmp_path,
):
    """Sanity: when the vanilla backup IS present, the extractor
    returns its bytes without consulting snapshot hashes (because
    the backup is the source of truth)."""
    from cdumm.engine import apply_engine

    vanilla_dir = tmp_path / "vanilla"
    game_dir = tmp_path / "game"
    vanilla_dir.mkdir()
    game_dir.mkdir()
    (vanilla_dir / "0008").mkdir()
    (vanilla_dir / "0008" / "0.paz").write_bytes(b"VANILLA")
    (vanilla_dir / "0008" / "0.pamt").write_bytes(b"")

    fake_entry = MagicMock()
    fake_entry.paz_file = str(vanilla_dir / "0008" / "0.paz")
    fake_entry.path = "iteminfo.pabgb"

    monkeypatch.setattr(
        "cdumm.engine.json_patch_handler._find_pamt_entry",
        lambda target, base_dir: fake_entry
        if base_dir == vanilla_dir else None)

    snapshot_mgr = MagicMock()
    # snapshot_mgr should NOT be called when backup is present.
    snapshot_mgr.get_file_hash.side_effect = AssertionError(
        "snapshot check should not run when backup is present")

    extractor = apply_engine._make_format3_vanilla_extractor(
        vanilla_dir=vanilla_dir,
        game_dir=game_dir,
        snapshot_mgr=snapshot_mgr,
        get_vanilla_entry_content=lambda fp, target: b"vanilla_body",
        extract_sibling_entry=lambda pamt_dir, hp: b"vanilla_header",
    )

    result = extractor("iteminfo.pabgb")
    assert result == (b"vanilla_body", b"vanilla_header")


def test_format3_vanilla_extractor_accepts_live_when_hash_matches(
    monkeypatch, tmp_path,
):
    """When the backup is missing but the live PAZ hashes equal to
    the snapshot value, the extractor should succeed (the live file
    IS vanilla by snapshot's definition, just hasn't been backed up
    yet). Mirrors the ``resolve_vanilla_source`` self-heal behavior."""
    from cdumm.engine import apply_engine

    vanilla_dir = tmp_path / "vanilla"
    game_dir = tmp_path / "game"
    vanilla_dir.mkdir()
    game_dir.mkdir()
    (game_dir / "0008").mkdir()

    live_paz = game_dir / "0008" / "0.paz"
    live_paz.write_bytes(b"PRISTINE")
    live_pamt = game_dir / "0008" / "0.pamt"
    live_pamt.write_bytes(b"")

    expected_hash = hashlib.sha256(b"PRISTINE").hexdigest()
    snapshot_mgr = MagicMock()
    snapshot_mgr.get_file_hash.return_value = expected_hash

    fake_entry = MagicMock()
    fake_entry.paz_file = str(live_paz)
    fake_entry.path = "iteminfo.pabgb"
    monkeypatch.setattr(
        "cdumm.engine.json_patch_handler._find_pamt_entry",
        lambda target, base_dir:
            fake_entry if base_dir == game_dir else None)

    # hash_file in snapshot_manager: stub it
    def _fake_hash_file(p):
        return hashlib.sha256(Path(p).read_bytes()).hexdigest(), 0

    monkeypatch.setattr(
        "cdumm.engine.snapshot_manager.hash_file", _fake_hash_file)

    extractor = apply_engine._make_format3_vanilla_extractor(
        vanilla_dir=vanilla_dir,
        game_dir=game_dir,
        snapshot_mgr=snapshot_mgr,
        get_vanilla_entry_content=lambda fp, target: b"live_vanilla_body",
        extract_sibling_entry=lambda pamt_dir, hp: b"live_vanilla_header",
    )

    result = extractor("iteminfo.pabgb")
    assert result == (b"live_vanilla_body", b"live_vanilla_header")

    # Lazy backup created so subsequent applies skip the warn path.
    backup_paz = vanilla_dir / "0008" / "0.paz"
    backup_pamt = vanilla_dir / "0008" / "0.pamt"
    assert backup_paz.exists(), (
        "Format 3 extractor must lazy-backup live PAZ when hash "
        "matches snapshot")
    assert backup_pamt.exists(), (
        "sibling PAMT must also be backed up so subsequent applies "
        "find the entry directly")
