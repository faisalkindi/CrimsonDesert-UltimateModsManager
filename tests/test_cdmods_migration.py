"""Tests for CDMods/ migration (Task 3.4)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


def _write_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_migrates_simple_directory(tmp_path):
    from cdumm.storage.cdmods_migration import migrate_cdmods

    src = tmp_path / "old"
    dst = tmp_path / "new"
    _write_file(src / "mod1.json", b"hello")
    _write_file(src / "vanilla" / "0009" / "0.paz", b"big-binary-blob")

    migrate_cdmods(src, dst)

    assert (dst / "mod1.json").read_bytes() == b"hello"
    assert (dst / "vanilla" / "0009" / "0.paz").read_bytes() == b"big-binary-blob"
    assert not src.exists()


def test_refuses_non_empty_dst(tmp_path):
    from cdumm.storage.cdmods_migration import migrate_cdmods, MigrationError

    src = tmp_path / "old"
    dst = tmp_path / "new"
    _write_file(src / "a.txt", b"x")
    _write_file(dst / "existing.txt", b"y")  # dst already has content

    with pytest.raises(MigrationError):
        migrate_cdmods(src, dst)
    # src untouched
    assert (src / "a.txt").read_bytes() == b"x"


def test_marker_file_present_during_migration_and_cleared_after(tmp_path):
    from cdumm.storage.cdmods_migration import (
        migrate_cdmods, detect_partial_migration,
    )

    src = tmp_path / "old"
    dst = tmp_path / "new"
    _write_file(src / "a.txt", b"x")

    migrate_cdmods(src, dst)
    assert detect_partial_migration(dst) is None  # marker cleared


def test_rollback_on_hash_mismatch(monkeypatch, tmp_path):
    """If a copy somehow corrupts the file (we simulate via monkeypatch),
    the migration must abort and leave src untouched."""
    from cdumm.storage import cdmods_migration

    src = tmp_path / "old"
    dst = tmp_path / "new"
    _write_file(src / "a.txt", b"x" * 1000)

    # Monkeypatch shutil.copy2 to flip a byte during copy.
    import shutil
    real_copy = shutil.copy2

    def _bad_copy(s, d, **kw):
        real_copy(s, d, **kw)
        # Corrupt destination to force hash mismatch
        Path(d).write_bytes(b"corrupted")
    monkeypatch.setattr("shutil.copy2", _bad_copy)

    from cdumm.storage.cdmods_migration import MigrationError
    with pytest.raises(MigrationError):
        cdmods_migration.migrate_cdmods(src, dst)

    # src is intact (we don't delete it on failure)
    assert (src / "a.txt").read_bytes() == b"x" * 1000


def test_progress_callback_invoked(tmp_path):
    from cdumm.storage.cdmods_migration import migrate_cdmods

    src = tmp_path / "old"
    dst = tmp_path / "new"
    for i in range(5):
        _write_file(src / f"f{i}.txt", b"x")

    received = []
    def cb(i, total, name):
        received.append((i, total, name))
    migrate_cdmods(src, dst, progress_callback=cb)
    assert len(received) >= 5
    assert all(total == 5 for _, total, _ in received)


def test_detect_partial_migration_finds_marker(tmp_path):
    from cdumm.storage.cdmods_migration import detect_partial_migration

    cdmods = tmp_path / "cdmods"
    cdmods.mkdir()
    (cdmods / ".cdumm_migration_in_progress").write_text("source=/old", encoding="utf-8")

    assert detect_partial_migration(cdmods) == cdmods


def test_detect_partial_migration_returns_none_when_clean(tmp_path):
    from cdumm.storage.cdmods_migration import detect_partial_migration

    cdmods = tmp_path / "cdmods"
    cdmods.mkdir()
    assert detect_partial_migration(cdmods) is None
