"""Transactional file I/O using stage + atomic rename pattern.

Ensures game files are never left in a corrupted state. If any step fails,
the previous valid state is preserved. On crash, .pre-apply files serve as
recovery markers.
"""
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

PRE_APPLY_SUFFIX = ".pre-apply"


class TransactionalIO:
    """Manages atomic file swaps for apply/revert operations."""

    def __init__(self, game_dir: Path, staging_dir: Path) -> None:
        self._game_dir = game_dir
        self._staging_dir = staging_dir
        self._staged_files: list[str] = []  # relative POSIX paths

    def stage_file(self, rel_path: str, data: bytes) -> None:
        """Write a modified file to the staging directory.

        If the same file is staged again, the data is overwritten (last write wins).

        GitHub #65 followup (tbyk101 v3.2.8.1, 2026-05-03): rel_path
        MUST be relative. Path arithmetic on Windows treats an
        absolute right-operand as REPLACING the base, so
        ``Path(staging_dir) / abspath_str`` returns abspath verbatim.
        That collapses staged and target to the same path and the
        commit shutil.move fails with a confusing
        ``[WinError 2] '<path>' -> '<path>'`` (identical src and dst).
        Reject absolute paths at the boundary so the caller bug
        surfaces clearly.
        """
        if Path(rel_path).is_absolute():
            # Log the caller stack so the apply error tells us which
            # call site is constructing absolute paths.
            import traceback
            stack = "".join(traceback.format_stack()[-5:-1])
            logger.error(
                "stage_file got absolute path %r — caller stack:\n%s",
                rel_path, stack)
            raise ValueError(
                f"stage_file requires a relative path; got absolute "
                f"{rel_path!r}. Pass the path relative to game_dir "
                f"(e.g. '0012/4.paz')."
            )
        staged_path = self._staging_dir / rel_path.replace("/", os.sep)
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        staged_path.write_bytes(data)
        if rel_path not in self._staged_files:
            self._staged_files.append(rel_path)
        logger.debug("Staged: %s (%d bytes)", rel_path, len(data))

    def stage_file_if_changed(self, rel_path: str, data: bytes) -> bool:
        """Stage only if the target differs from ``data``.

        Returns True if a stage was performed, False if skipped.

        Fast path for Phase 3 reverts and PATHC writes: when the live
        game file already contains these exact bytes (e.g. a prior apply
        left it in the desired state), skip the read+write+rename cycle
        of stage_file + commit entirely. Only the size+byte comparison
        cost is paid.
        """
        target = self._game_dir / rel_path.replace("/", os.sep)
        if target.exists():
            try:
                if target.stat().st_size == len(data):
                    if target.read_bytes() == data:
                        logger.debug(
                            "Skipped stage (target already matches): %s "
                            "(%d bytes)", rel_path, len(data))
                        return False
            except OSError as e:
                logger.debug("Identity check for %s failed (%s) — "
                             "falling through to stage", rel_path, e)
        self.stage_file(rel_path, data)
        return True

    def commit(self) -> None:
        """Atomically swap staged files into the game directory.

        Phase 1: Rename originals to .pre-apply
        Phase 2: Rename staged files to originals
        If phase 2 fails, rollback phase 1.
        """
        renamed: list[str] = []  # tracks which files completed phase 1

        try:
            # Phase 1: rename originals to .pre-apply
            for rel_path in self._staged_files:
                original = self._game_dir / rel_path.replace("/", os.sep)
                backup = original.with_suffix(original.suffix + PRE_APPLY_SUFFIX)

                if original.exists():
                    original.rename(backup)
                    renamed.append(rel_path)
                    logger.debug("Backed up: %s -> %s", original, backup)

            # Phase 2: move staged files to game directory
            for rel_path in self._staged_files:
                staged = self._staging_dir / rel_path.replace("/", os.sep)
                target = self._game_dir / rel_path.replace("/", os.sep)
                # Defense in depth (GitHub #65 followup): if some caller
                # bypasses stage_file's guard and injects an absolute
                # path into _staged_files, Path / abspath returns
                # abspath, collapsing staged and target. Surface a clear
                # error instead of letting shutil.move fail with a
                # confusing same-path WinError 2.
                if str(staged) == str(target):
                    raise ValueError(
                        f"transactional_io: staged path equals target "
                        f"path for {rel_path!r}, refusing to call "
                        f"shutil.move on identical paths. This means "
                        f"{rel_path!r} was added as an absolute path "
                        f"despite stage_file's guard, or _staged_files "
                        f"was mutated externally. "
                        f"staged={staged!s}, target={target!s}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(staged), str(target))
                logger.debug("Committed: %s", rel_path)

        except Exception as e:
            logger.error("Commit failed at file swap, rolling back: %s", e)
            self._rollback(renamed)
            raise

        # Phase 3: cleanup .pre-apply files
        for rel_path in renamed:
            original = self._game_dir / rel_path.replace("/", os.sep)
            backup = original.with_suffix(original.suffix + PRE_APPLY_SUFFIX)
            if backup.exists():
                backup.unlink()

        logger.info("Transaction committed: %d files", len(self._staged_files))

    def _rollback(self, renamed: list[str]) -> None:
        """Restore .pre-apply files back to originals."""
        for rel_path in renamed:
            original = self._game_dir / rel_path.replace("/", os.sep)
            backup = original.with_suffix(original.suffix + PRE_APPLY_SUFFIX)

            # Remove any partially-committed staged file
            if original.exists():
                original.unlink()

            # Restore backup
            if backup.exists():
                backup.rename(original)
                logger.debug("Rolled back: %s", rel_path)

        logger.info("Rollback complete: %d files restored", len(renamed))

    def cleanup_staging(self) -> None:
        """Remove staging directory."""
        if self._staging_dir.exists():
            shutil.rmtree(self._staging_dir)

    @staticmethod
    def detect_interrupted_apply(game_dir: Path) -> list[Path]:
        """Detect .pre-apply files indicating a crashed apply operation."""
        return list(game_dir.rglob(f"*{PRE_APPLY_SUFFIX}"))

    @staticmethod
    def recover_from_interrupted(game_dir: Path) -> int:
        """Restore .pre-apply files to originals. Returns count of recovered files."""
        pre_apply_files = TransactionalIO.detect_interrupted_apply(game_dir)
        count = 0
        for backup in pre_apply_files:
            # Strip the .pre-apply suffix (handles multi-dotted names like 0.pamt.pre-apply)
            original = backup.with_name(backup.name.removesuffix(".pre-apply"))
            if original.exists():
                original.unlink()
            backup.rename(original)
            count += 1
            logger.info("Recovered: %s", original)
        return count
