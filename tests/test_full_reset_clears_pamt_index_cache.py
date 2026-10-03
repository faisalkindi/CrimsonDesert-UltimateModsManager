"""Regression (#439, baramjeoung): Full Reset + Rescan (the Steam-verified
branch of ``_run_fix``) wiped ``vanilla_dir`` but left the on-disk PAMT
index cache behind, because that cache file is a sibling of
``vanilla_dir`` (``<cdmods>/.pamt_index_<ver>_vanilla.cache``), not inside
it, so ``shutil.rmtree(vanilla_dir)`` never touched it.

``_get_pamt_index``'s staleness check only compares the cached PAMTs'
mtimes against the cache file's mtime. A freshly rebuilt vanilla backup
can carry over the source file's mtime (copy preserves timestamps), so
that check can pass even though the cache's offsets no longer match the
rebuilt backups, silently serving stale archive offsets and breaking
Apply with an LZ4 decompress error on perfectly valid mods.

After the fix, ``_run_fix`` deletes any ``.pamt_index*_vanilla.cache``
sibling of ``vanilla_dir`` when it clears backups on the Steam-verified
path, forcing a clean rebuild from the fresh backups.
"""
from __future__ import annotations

import cdumm.worker_process as wp


def test_full_reset_deletes_stale_vanilla_pamt_index_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(wp, "_emit", lambda obj: None)

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    cdmods = tmp_path / "CDMods"
    vanilla_dir = cdmods / "vanilla"
    vanilla_dir.mkdir(parents=True)

    # Simulate a disk-cached PAMT index left over from before the reset,
    # referencing archive offsets that no longer match once backups are
    # rebuilt.
    stale_cache = cdmods / ".pamt_index_v4_vanilla.cache"
    stale_cache.write_bytes(b"stale-pickled-index")

    db_path = tmp_path / "cdumm.db"

    wp._run_fix(str(game_dir), str(vanilla_dir), str(db_path),
                steam_verified="1")

    assert not stale_cache.exists(), (
        "Full Reset + Rescan must delete the stale vanilla PAMT index "
        "cache when it clears backups, otherwise a cache built against "
        "the old backups survives the reset and keeps serving offsets "
        "that no longer match the freshly rebuilt backups (#439)")
    assert vanilla_dir.exists(), "vanilla_dir must be recreated empty"


def test_full_reset_leaves_other_cache_versions_alone_if_matching_pattern(
        tmp_path, monkeypatch):
    """Older superseded cache versions matching the vanilla suffix must
    also go -- a leftover v3 cache is just as stale as a v4 one once the
    backups it was built from are wiped."""
    monkeypatch.setattr(wp, "_emit", lambda obj: None)

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    cdmods = tmp_path / "CDMods"
    vanilla_dir = cdmods / "vanilla"
    vanilla_dir.mkdir(parents=True)

    old_cache = cdmods / ".pamt_index_v3_vanilla.cache"
    old_cache.write_bytes(b"old-version-pickled-index")

    db_path = tmp_path / "cdumm.db"

    wp._run_fix(str(game_dir), str(vanilla_dir), str(db_path),
                steam_verified="1")

    assert not old_cache.exists()


def test_non_verified_path_does_not_need_cache_clear(tmp_path, monkeypatch):
    """Quick Fix (steam_verified=False) reverts from the existing
    backups instead of wiping them, so there's nothing stale to clear
    here; this just documents that the cache-clear is scoped to the
    Steam-verified branch and doesn't blow up on the other path."""
    monkeypatch.setattr(wp, "_emit", lambda obj: None)

    from cdumm.engine import apply_engine

    class _FakeRevertWorker:
        def __init__(self, **kwargs):
            self.progress_updated = _FakeSignal()
            self.warning = _FakeSignal()
            self.error_occurred = _FakeSignal()

        def run(self):
            pass

    class _FakeSignal:
        def connect(self, *_a, **_k):
            pass

    monkeypatch.setattr(apply_engine, "RevertWorker", _FakeRevertWorker)

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    cdmods = tmp_path / "CDMods"
    vanilla_dir = cdmods / "vanilla"
    vanilla_dir.mkdir(parents=True)
    cache = cdmods / ".pamt_index_v4_vanilla.cache"
    cache.write_bytes(b"current-cache")

    db_path = tmp_path / "cdumm.db"

    wp._run_fix(str(game_dir), str(vanilla_dir), str(db_path),
                steam_verified="0")

    # Backups aren't wiped on this path, so the cache built from them
    # is still valid and must be left alone.
    assert cache.exists()
