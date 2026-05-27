"""Regression tests for symlinked-directory traversal in
``crimson_browser_handler`` on Linux.

Background (PR #123, reported by RoGreat on a Nix-packaged build):
folder-based Crimson Browser mod imports silently dropped files that
lived behind a symlinked directory, so no PAZ was built and the mod
came in as a stray PATHC. Root cause: Python 3.13 changed
``Path.rglob`` to default ``recurse_symlinks=False`` — it no longer
descends into symlinked subdirectories. The frozen Windows build and
the pre-3.13 behaviour this module was written for DID follow them.
A zip import dodges the bug because extraction materialises plain
files; a folder import reads the user's real tree, symlinks and all.

``_rglob_follow`` restores the follow-symlinks behaviour on all Python
versions. These tests pin that, and confirm the symlink-escape guard
that the file-reading callers apply still rejects a symlink pointing
outside the mod tree.
"""
from __future__ import annotations

import sys
from pathlib import Path

from cdumm.engine.crimson_browser_handler import _rglob_follow


def _make_files_with_symlinked_dir(base: Path) -> Path:
    """Build files/ where the numbered PAZ dir (0008) is a symlink to
    a sibling real directory — the shape that vanished on Python 3.13+."""
    files_dir = base / "files"
    files_dir.mkdir(parents=True)
    real = base / "realdata" / "0008"
    real.mkdir(parents=True)
    (real / "texture.paz").write_bytes(b"PAZ")
    (files_dir / "0008").symlink_to(real, target_is_directory=True)
    return files_dir


class TestRglobFollow:
    def test_finds_file_behind_symlinked_directory(self, tmp_path: Path) -> None:
        """The core regression: a file inside a symlinked subdir must be
        discovered. Plain rglob on 3.13+ returns it as zero results."""
        files_dir = _make_files_with_symlinked_dir(tmp_path)
        found = [f for f in _rglob_follow(files_dir) if f.is_file()]
        names = {f.name for f in found}
        assert "texture.paz" in names, (
            "file behind a symlinked directory must be discovered — "
            "this is the PR #123 folder-import regression")

    def test_documents_the_stdlib_default_that_caused_the_bug(
            self, tmp_path: Path) -> None:
        """Guard rail: if a future stdlib/refactor makes plain rglob find
        these again, _rglob_follow is still correct — but this test
        documents *why* the helper exists. On 3.13+ the plain default
        misses the file; on <=3.12 it would find it. Either way
        _rglob_follow must find it (covered above)."""
        files_dir = _make_files_with_symlinked_dir(tmp_path)
        plain = [f for f in files_dir.rglob("*") if f.is_file()]
        if sys.version_info >= (3, 13):
            assert plain == [], (
                "expected the 3.13+ rglob default to miss files behind "
                "symlinked dirs — if this fails the stdlib behaviour "
                "changed and the helper's version guard can be revisited")
        else:
            assert any(f.name == "texture.paz" for f in plain)

    def test_plain_folder_unaffected(self, tmp_path: Path) -> None:
        """No symlinks: helper behaves like ordinary rglob."""
        files_dir = tmp_path / "files" / "0008"
        files_dir.mkdir(parents=True)
        (files_dir / "a.paz").write_bytes(b"x")
        found = [f.name for f in _rglob_follow(tmp_path / "files") if f.is_file()]
        assert found == ["a.paz"]


class TestSymlinkEscapeGuardStillHolds:
    """The file-reading caller (convert_to_paz_mod) resolves each file
    and refuses any that escape files_dir. Following symlinks must not
    weaken that — a symlink pointing outside the tree is still caught."""

    def test_escaping_symlink_file_is_detected(self, tmp_path: Path) -> None:
        files_dir = tmp_path / "files" / "0008"
        files_dir.mkdir(parents=True)
        outside = tmp_path / "outside_secret.paz"
        outside.write_bytes(b"SECRET")
        (files_dir / "evil.paz").symlink_to(outside)

        files_root = tmp_path / "files"
        files_root_resolved = files_root.resolve()
        escaped, safe = [], []
        for f in _rglob_follow(files_root):
            if not f.is_file():
                continue
            try:
                f.resolve().relative_to(files_root_resolved)
                safe.append(f.name)
            except ValueError:
                escaped.append(f.name)
        assert "evil.paz" in escaped, (
            "a symlink pointing outside files_dir must still be flagged "
            "as escaping so the caller can skip it")

    def test_internal_symlink_alias_is_allowed(self, tmp_path: Path) -> None:
        """A symlink that stays *within* files_dir (a legit alias) is not
        flagged as an escape."""
        files_dir = tmp_path / "files" / "0008"
        files_dir.mkdir(parents=True)
        (files_dir / "real.paz").write_bytes(b"x")
        (files_dir / "alias.paz").symlink_to(files_dir / "real.paz")

        files_root = tmp_path / "files"
        files_root_resolved = files_root.resolve()
        safe = []
        for f in _rglob_follow(files_root):
            if not f.is_file():
                continue
            try:
                f.resolve().relative_to(files_root_resolved)
                safe.append(f.name)
            except ValueError:
                pass
        assert "real.paz" in safe and "alias.paz" in safe
