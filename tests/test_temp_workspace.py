"""CRITICAL #3: temp-dir leak cleanup.

mkdtemp() calls across fluent_window.py + mods_page.py with prefixes
cdumm_swap_, cdumm_preset_, cdumm_cog_, cdumm_variant_, cdumm_asi_,
cdumm_batch_asi_ accumulate forever in %TEMP%. This module tracks
them for atexit cleanup and sweeps stale ones from prior runs.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from cdumm.engine.temp_workspace import (
    make_temp_dir,
    release_temp_dir,
    sweep_stale,
    CDUMM_PREFIXES,
)


def test_make_temp_dir_creates_with_cdumm_prefix(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    p = make_temp_dir("cdumm_swap_42_")
    assert p.exists() and p.is_dir()
    assert p.name.startswith("cdumm_swap_42_")
    release_temp_dir(p)


def test_release_removes_directory(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    p = make_temp_dir("cdumm_preset_1_")
    (p / "file.txt").write_text("x")
    release_temp_dir(p)
    assert not p.exists()


def test_sweep_stale_removes_old_cdumm_dirs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    old_stale = tmp_path / "cdumm_swap_99_stale"
    old_stale.mkdir()
    recent = tmp_path / "cdumm_preset_1_fresh"
    recent.mkdir()
    unrelated = tmp_path / "some_other_dir"
    unrelated.mkdir()

    # Backdate the stale dir to 10 days ago.
    ten_days_ago = time.time() - 10 * 24 * 3600
    import os
    os.utime(old_stale, (ten_days_ago, ten_days_ago))

    removed = sweep_stale(max_age_hours=48)
    assert old_stale.exists() is False, "old cdumm temp dir should be swept"
    assert recent.exists(), "recent cdumm dir must be kept"
    assert unrelated.exists(), "non-cdumm dirs must never be touched"
    assert removed == 1


def test_sweep_never_touches_non_cdumm_dirs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    other = tmp_path / "pip-build-abc"   # pip temp pattern
    other.mkdir()
    import os
    ten_days_ago = time.time() - 10 * 24 * 3600
    os.utime(other, (ten_days_ago, ten_days_ago))

    removed = sweep_stale(max_age_hours=48)
    assert other.exists(), "sweep must be prefix-scoped to cdumm_*"
    assert removed == 0


def test_prefixes_list_covers_all_known_cdumm_temp_patterns():
    required = {
        "cdumm_swap_", "cdumm_preset_", "cdumm_cog_",
        "cdumm_variant_", "cdumm_asi_", "cdumm_batch_asi_",
    }
    missing = required - set(CDUMM_PREFIXES)
    assert not missing, f"CDUMM_PREFIXES missing {missing}"
