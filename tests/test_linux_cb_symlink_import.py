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

import logging
import sys
from pathlib import Path

import pytest

from cdumm.engine.crimson_browser_handler import _rglob_follow, convert_to_paz_mod


def _symlink_or_skip(link: Path, target: Path, *,
                     target_is_directory: bool = False) -> None:
    """Create a symlink, or skip the test when the OS refuses.

    Windows needs admin rights (or Developer Mode) to create symlinks;
    plain accounts raise WinError 1314 as an OSError.
    """
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError:
        pytest.skip("symlinks require admin on Windows")


def _make_files_with_symlinked_dir(base: Path) -> Path:
    """Build files/ where the numbered PAZ dir (0008) is a symlink to
    a sibling real directory — the shape that vanished on Python 3.13+."""
    files_dir = base / "files"
    files_dir.mkdir(parents=True)
    real = base / "realdata" / "0008"
    real.mkdir(parents=True)
    (real / "texture.paz").write_bytes(b"PAZ")
    _symlink_or_skip(files_dir / "0008", real, target_is_directory=True)
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
        _symlink_or_skip(files_dir / "evil.paz", outside)

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
        _symlink_or_skip(files_dir / "alias.paz", files_dir / "real.paz")

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


class TestTrustSymlinksParameter:
    """``convert_to_paz_mod(trust_symlinks=...)`` distinguishes folder
    imports (user picked the directory — symlinks are trusted local
    references) from archive extractions (untrusted — the escape
    guard must stay active).

    Reported by RoGreat on a Nix-packaged build (PR #123): his mod's
    individual .dds files are symlinks into the Nix store. Before
    this fix, the escape guard refused every such file and the
    import fell through to the standalone-texture-overlay fallback
    instead of producing a proper CB PAZ build.
    """

    def _build_cb_mod_with_escaping_symlink(
            self, base: Path) -> tuple[dict, Path]:
        """Construct a minimal CB-format mod folder where one file is
        a symlink pointing OUTSIDE the mod's files/ tree (mirrors the
        Nix-store-into-mod-folder layout). Returns the manifest dict
        and the files/ Path."""
        mod_dir = base / "ModRoot"
        files_dir = mod_dir / "files" / "0008"
        files_dir.mkdir(parents=True)
        (files_dir / "real.dds").write_bytes(b"REAL")
        outside = base / "elsewhere" / "stash.dds"
        outside.parent.mkdir(parents=True)
        outside.write_bytes(b"STASH")
        _symlink_or_skip(files_dir / "linked.dds", outside)
        manifest = {
            "id": "test_mod",
            "files_dir": "files",
            "_base_dir": mod_dir,
        }
        return manifest, mod_dir / "files"

    def _run_convert(self, manifest: dict, tmp_path: Path,
                     *, trust_symlinks: bool) -> None:
        """Call convert_to_paz_mod with the given flag. The function's
        post-loop machinery needs a real game_dir + vanilla PAZ files
        which we don't have here, so the call typically raises after
        the symlink-handling loop runs. We only care about the
        in-loop warning, so swallow any exception."""
        game_dir = tmp_path / "junk_game_dir"
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        try:
            convert_to_paz_mod(
                manifest, game_dir, work_dir,
                trust_symlinks=trust_symlinks)
        except Exception:
            pass

    def test_default_keeps_escape_guard_active(
            self, tmp_path: Path, caplog) -> None:
        """``trust_symlinks=False`` (default — what every zip /
        extraction call site uses) still emits the symlink-escape
        warning for files whose target resolves outside files_dir.
        The 6 archive-extraction callers in import_handler.py rely
        on this default to stay protected against malicious archives
        with symlinks pointing at arbitrary filesystem locations."""
        manifest, _ = self._build_cb_mod_with_escaping_symlink(tmp_path)
        caplog.set_level(
            logging.WARNING,
            logger="cdumm.engine.crimson_browser_handler")
        self._run_convert(manifest, tmp_path, trust_symlinks=False)
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "CB import: skipping" in msgs and "linked.dds" in msgs, (
            "default trust_symlinks=False must emit the symlink-escape "
            "warning for files resolving outside files_dir; got: "
            + msgs)

    def test_trust_symlinks_true_suppresses_escape_warning(
            self, tmp_path: Path, caplog) -> None:
        """``trust_symlinks=True`` (folder-import call sites only)
        follows the symlink to its target rather than refusing it.
        The escape warning must NOT fire — Nix-store-symlinked mod
        files are legitimate user content, not a security threat."""
        manifest, _ = self._build_cb_mod_with_escaping_symlink(tmp_path)
        caplog.set_level(
            logging.WARNING,
            logger="cdumm.engine.crimson_browser_handler")
        self._run_convert(manifest, tmp_path, trust_symlinks=True)
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "CB import: skipping" not in msgs, (
            "trust_symlinks=True must NOT emit symlink-escape warnings; "
            "got: " + msgs)
