"""A failed CDMods migration must leave the destination retryable.

``migrate_cdmods`` refuses a non-empty destination (safety guard
against accidental merges). Before this fix, a mid-copy failure left
the partial tree + marker in dst, so every retry into the same folder
hit that guard and the user was wedged. The source is always intact
at copy time (src is deleted LAST), so the partial destination is
safe to remove: the marker, written first into a verified-empty dir,
proves everything in dst belongs to the failed attempt.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cdumm.storage import cdmods_migration
from cdumm.storage.cdmods_migration import (
    MARKER_NAME,
    MigrationError,
    migrate_cdmods,
)


def _make_src(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "vanilla").mkdir(parents=True)
    (src / "cdumm.db").write_bytes(b"db-bytes")
    (src / "vanilla" / "0.paz").write_bytes(b"paz-bytes")
    return src


def test_failed_copy_cleans_destination_and_allows_retry(
        tmp_path: Path, monkeypatch) -> None:
    src = _make_src(tmp_path)
    dst = tmp_path / "dst"

    real_copy2 = shutil.copy2
    calls = {"n": 0}

    def failing_copy2(s, d, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated disk full")
        return real_copy2(s, d, *args, **kwargs)

    monkeypatch.setattr(cdmods_migration.shutil, "copy2", failing_copy2)

    with pytest.raises(MigrationError):
        migrate_cdmods(src, dst)

    # Source untouched.
    assert (src / "cdumm.db").exists()
    assert (src / "vanilla" / "0.paz").exists()

    # Destination cleaned: no marker, no partial files.
    assert not (dst / MARKER_NAME).exists(), (
        "marker left behind; next launch would flag a partial "
        "migration that no longer exists")
    leftovers = list(dst.iterdir()) if dst.exists() else []
    assert leftovers == [], (
        f"partial files left in dst wedge the retry against the "
        f"non-empty guard: {leftovers}")

    # Retry into the SAME folder must now succeed (copy2 fails only
    # on its second call ever; the retry makes calls 3 and 4).
    migrate_cdmods(src, dst)
    assert (dst / "cdumm.db").read_bytes() == b"db-bytes"
    assert (dst / "vanilla" / "0.paz").read_bytes() == b"paz-bytes"
    assert not (dst / MARKER_NAME).exists()
    assert not src.exists(), "source is removed after a verified move"


def test_checksum_mismatch_also_cleans_destination(
        tmp_path: Path, monkeypatch) -> None:
    src = _make_src(tmp_path)
    dst = tmp_path / "dst"

    real_sha = cdmods_migration._sha256_file
    state = {"n": 0}

    def corrupting_sha(path: Path) -> str:
        state["n"] += 1
        digest = real_sha(path)
        # Corrupt the DESTINATION hash of the second file
        # (call order per file: src hash, then dst hash).
        if state["n"] == 4:
            return "0" * 64
        return digest

    monkeypatch.setattr(cdmods_migration, "_sha256_file", corrupting_sha)

    with pytest.raises(MigrationError, match="checksum mismatch"):
        migrate_cdmods(src, dst)

    assert (src / "cdumm.db").exists()
    assert not (dst / MARKER_NAME).exists()
    assert (list(dst.iterdir()) if dst.exists() else []) == []
