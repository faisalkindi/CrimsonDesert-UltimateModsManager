"""Transactional file I/O using stage + atomic rename pattern.

Ensures game files are never left in a corrupted state. If any step fails,
the previous valid state is preserved. On crash, .pre-apply files serve as
recovery markers.
"""
import logging
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
        """
        staged_path = self._staging_dir / rel_path.replace("/", "\\")
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        staged_path.write_bytes(data)
        if rel_path not in self._staged_files:
            self._staged_files.append(rel_path)
        logger.debug("Staged: %s (%d bytes)", rel_path, len(data))

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
                original = self._game_dir / rel_path.replace("/", "\\")
                backup = original.with_suffix(original.suffix + PRE_APPLY_SUFFIX)

                if original.exists():
                    original.rename(backup)
                    renamed.append(rel_path)
                    logger.debug("Backed up: %s -> %s", original, backup)

            # Phase 2: move staged files to game directory
            for rel_path in self._staged_files:
                staged = self._staging_dir / rel_path.replace("/", "\\")
                target = self._game_dir / rel_path.replace("/", "\\")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(staged), str(target))
                logger.debug("Committed: %s", rel_path)

        except Exception as e:
            logger.error("Commit failed at file swap, rolling back: %s", e)
            self._rollback(renamed)
            raise

        # Phase 3: cleanup .pre-apply files
        for rel_path in renamed:
            original = self._game_dir / rel_path.replace("/", "\\")
            backup = original.with_suffix(original.suffix + PRE_APPLY_SUFFIX)
            if backup.exists():
                backup.unlink()

        logger.info("Transaction committed: %d files", len(self._staged_files))

    def _rollback(self, renamed: list[str]) -> None:
        """Restore .pre-apply files back to originals."""
        for rel_path in renamed:
            original = self._game_dir / rel_path.replace("/", "\\")
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
            original = backup.with_suffix("")  # strip .pre-apply
            if original.exists():
                original.unlink()
            backup.rename(original)
            count += 1
            logger.info("Recovered: %s", original)
        return count
