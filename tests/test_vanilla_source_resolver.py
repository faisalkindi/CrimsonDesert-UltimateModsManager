"""resolve_vanilla_source branch coverage.

Covers all five paths from the plan coverage diagram:

1. Vanilla backup present             -> returns vanilla entry, no warn
2. Vanilla PAZ missing + hash match   -> returns live entry, warn_callback called
3. Vanilla PAZ missing + hash mismatch -> raises VanillaSourceUnavailable
4. Vanilla PAZ missing + no snapshot  -> raises VanillaSourceUnavailable
5. Vanilla & live PAMT both missing   -> raises VanillaSourceUnavailable

Rather than build real PAZ + PAMT fixtures (expensive), the test mocks
_find_pamt_entry and hash_file at the apply_engine module level.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cdumm.engine import apply_engine as ae
from cdumm.engine.json_patch_handler import VanillaSourceUnavailable


# Fake PazEntry stand-in — only .paz_file is inspected by the resolver.
def _entry(paz_file: Path):
    return SimpleNamespace(paz_file=str(paz_file))


class _FakeSnapshot:
    def __init__(self, hashes: dict[str, str] | None = None):
        self._hashes = hashes or {}

    def get_file_hash(self, rel_path: str) -> str | None:
        return self._hashes.get(rel_path.replace("\\", "/"))


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def dirs(tmp_path: Path):
    vanilla = tmp_path / "CDMods" / "vanilla"
    game = tmp_path / "game"
    vanilla.mkdir(parents=True)
    game.mkdir(parents=True)
    (game / "0008").mkdir()
    # Real file so hash_file succeeds
    (game / "0008" / "0.paz").write_bytes(b"live-bytes-123")
    return SimpleNamespace(vanilla=vanilla, game=game)


# ── Tests ────────────────────────────────────────────────────────────


def test_vanilla_backup_present_returns_vanilla(dirs, monkeypatch):
    van_paz = dirs.vanilla / "0008" / "0.paz"
    van_paz.parent.mkdir(parents=True)
    van_paz.write_bytes(b"vanilla")

    def fake_find(game_file, base):
        if base == dirs.vanilla:
            return _entry(van_paz)
        return _entry(dirs.game / "0008" / "0.paz")

    monkeypatch.setattr(ae, "_find_pamt_entry", lambda gf, base: fake_find(gf, base),
                        raising=False)
    # Patch in json_patch_handler too since apply_engine imports it lazily
    monkeypatch.setattr("cdumm.engine.json_patch_handler._find_pamt_entry",
                        fake_find)

    warned: list[str] = []
    result = ae.resolve_vanilla_source(
        "gamedata/x.pabgb", dirs.vanilla, dirs.game,
        _FakeSnapshot(), warn_callback=warned.append,
    )
    assert result.paz_file == str(van_paz)
    assert warned == []


def test_vanilla_missing_live_hash_match_falls_back_and_warns(dirs, monkeypatch):
    # Vanilla PAMT points at a file that does NOT exist
    van_paz = dirs.vanilla / "0008" / "0.paz"  # no parent, no file
    live_paz = dirs.game / "0008" / "0.paz"

    def fake_find(game_file, base):
        if base == dirs.vanilla:
            return _entry(van_paz)
        return _entry(live_paz)

    monkeypatch.setattr("cdumm.engine.json_patch_handler._find_pamt_entry",
                        fake_find)
    monkeypatch.setattr(
        "cdumm.engine.snapshot_manager.hash_file",
        lambda p, progress_callback=None, algo="auto":
            ("match-hash", p.stat().st_size))

    # Snapshot has matching hash for 0008/0.paz
    snapshot = _FakeSnapshot({"0008/0.paz": "match-hash"})
    warned: list[str] = []
    result = ae.resolve_vanilla_source(
        "gamedata/x.pabgb", dirs.vanilla, dirs.game,
        snapshot, warn_callback=warned.append,
    )
    assert result.paz_file == str(live_paz)
    assert warned == ["0008/0.paz"]


def test_vanilla_missing_live_hash_mismatch_raises(dirs, monkeypatch):
    van_paz = dirs.vanilla / "0008" / "0.paz"
    live_paz = dirs.game / "0008" / "0.paz"

    def fake_find(game_file, base):
        if base == dirs.vanilla:
            return _entry(van_paz)
        return _entry(live_paz)

    monkeypatch.setattr("cdumm.engine.json_patch_handler._find_pamt_entry",
                        fake_find)
    monkeypatch.setattr(
        "cdumm.engine.snapshot_manager.hash_file",
        lambda p, progress_callback=None, algo="auto":
            ("different-hash", p.stat().st_size))

    snapshot = _FakeSnapshot({"0008/0.paz": "snapshot-hash"})
    with pytest.raises(VanillaSourceUnavailable, match="diverged from snapshot"):
        ae.resolve_vanilla_source(
            "gamedata/x.pabgb", dirs.vanilla, dirs.game, snapshot,
        )


def test_vanilla_missing_no_snapshot_hash_raises(dirs, monkeypatch):
    van_paz = dirs.vanilla / "0008" / "0.paz"
    live_paz = dirs.game / "0008" / "0.paz"

    def fake_find(game_file, base):
        if base == dirs.vanilla:
            return _entry(van_paz)
        return _entry(live_paz)

    monkeypatch.setattr("cdumm.engine.json_patch_handler._find_pamt_entry",
                        fake_find)

    snapshot = _FakeSnapshot()  # empty
    with pytest.raises(VanillaSourceUnavailable, match="no snapshot hash"):
        ae.resolve_vanilla_source(
            "gamedata/x.pabgb", dirs.vanilla, dirs.game, snapshot,
        )


def test_both_pamts_missing_raises(dirs, monkeypatch):
    monkeypatch.setattr("cdumm.engine.json_patch_handler._find_pamt_entry",
                        lambda gf, base: None)

    with pytest.raises(VanillaSourceUnavailable, match="no PAMT entry"):
        ae.resolve_vanilla_source(
            "nonexistent/thing.pabgb", dirs.vanilla, dirs.game,
            _FakeSnapshot(),
        )
