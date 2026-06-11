"""Atomic migration of CDMods/ contents from one root to another.

Used by Settings -> Mod storage location when the user picks a new
disk for sources/vanilla/deltas/cdumm.db. The job here is to move
real user data (potentially gigabytes of vanilla snapshots and
delta patches) without dropping a single byte. Wrong move and the
mod library is gone.

Safety guarantees:

  1. Refuse to overwrite an existing non-empty destination — the
     caller picked a folder that already has stuff in it, which is
     either a typo or a user assuming we'd merge. We don't merge.
  2. Drop a ``.cdumm_migration_in_progress`` marker inside dst as
     the very first write. If the process crashes mid-copy the
     marker is still there on next launch, so CDUMM can detect a
     half-finished move (handled separately by ``detect_partial_
     migration``).
  3. SHA-256 every source file BEFORE copying, copy with shutil.
     copy2 (preserves mtime), then SHA-256 the destination and
     compare. Any copy or verify failure raises ``MigrationError``
     with the source left untouched, and the partial destination
     tree (marker included) is removed so a retry can proceed.
  4. Only after every file is copied + verified do we delete the
     source tree. Marker is cleared last.

We deliberately do NOT use shutil.move — on cross-drive moves it
falls back to copy+delete with no integrity check. We also skip
symlinks: CDMods/ shouldn't contain any, and following one across
trees risks pulling in unrelated data.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

MARKER_NAME = ".cdumm_migration_in_progress"
_CHUNK = 8192


class MigrationError(RuntimeError):
    """Raised when an atomic CDMods/ migration cannot complete safely."""


def _sha256_file(path: Path) -> str:
    """Stream a file through SHA-256 in 8 KiB chunks. Used for both
    pre- and post-copy checksums; large vanilla .paz blobs would
    blow up memory if we read them whole."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _is_empty_dir(path: Path) -> bool:
    if not path.exists():
        return True
    if not path.is_dir():
        return False
    return next(path.iterdir(), None) is None


def detect_partial_migration(cdmods_root: Path) -> Path | None:
    """Return ``cdmods_root`` if a migration marker is present there,
    otherwise None. Called on CDUMM launch to surface a "previous
    migration didn't finish" warning to the user.
    """
    if not cdmods_root.exists() or not cdmods_root.is_dir():
        return None
    if (cdmods_root / MARKER_NAME).exists():
        return cdmods_root
    return None


def _enumerate_files(src: Path) -> list[Path]:
    """Walk ``src`` recursively and return every regular file as an
    absolute Path. Symlinks are skipped (followlinks=False) — CDMods/
    isn't supposed to contain any, and chasing one across trees would
    drag in unrelated data."""
    found: list[Path] = []
    for root, _dirs, files in os.walk(src, followlinks=False):
        root_path = Path(root)
        for name in files:
            p = root_path / name
            # Skip symlinks defensively even though os.walk doesn't
            # follow them — a file entry could still BE a symlink.
            if p.is_symlink():
                logger.warning(
                    "cdmods migration: skipping symlink %s", p)
                continue
            found.append(p)
    return found


def _cleanup_partial_destination(dst: Path, marker: Path) -> None:
    """Remove the partially-copied destination tree after a failed copy.

    The source is still intact at this point (src is only deleted
    AFTER every file is copied and verified), so everything under
    ``dst`` is a replaceable partial copy. Without this cleanup, a
    retry of the same migration hits the "destination already exists
    and is not empty" guard and the user is wedged until they
    manually delete the partial tree.

    Only proceeds when our migration marker is present in ``dst``:
    the marker is the very first write into a destination that the
    empty-dir guard verified was empty, so its presence proves every
    item in ``dst`` belongs to this migration attempt.
    """
    if not marker.exists():
        return
    removed: list[str] = []
    try:
        for child in dst.iterdir():
            if child == marker:
                continue
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
                removed.append(child.name)
            except OSError as e:
                logger.warning(
                    "cdmods migration: cleanup could not remove %s: %s",
                    child, e)
        marker.unlink()
        logger.info(
            "cdmods migration: copy failed; removed %d partial item(s) "
            "from %s (%s) plus the marker so a retry can proceed",
            len(removed), dst, ", ".join(removed) or "none")
    except OSError as e:
        logger.warning(
            "cdmods migration: partial-destination cleanup failed: %s", e)


def migrate_cdmods(
    src: Path,
    dst: Path,
    *,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> None:
    """Move every file under ``src`` to ``dst`` atomically.

    See module docstring for the safety contract. Raises
    ``MigrationError`` on any verification or I/O failure; on raise
    ``src`` is left intact (we delete src LAST, after every file is
    verified). A copy/verify failure also cleans up the partially-
    copied destination tree (marker included) so the user can retry
    into the same folder without hitting the non-empty guard. Only
    the post-copy "source tree refused to delete" failure leaves the
    marker in place, because at that point the verified data lives
    in ``dst`` and must not be removed; the next launch picks that
    up via :func:`detect_partial_migration`.
    """
    src = Path(src)
    dst = Path(dst)

    if not src.exists():
        raise MigrationError(
            f"source path does not exist: {src}")
    if not src.is_dir():
        raise MigrationError(
            f"source path is not a directory: {src}")

    if dst.exists() and not _is_empty_dir(dst):
        raise MigrationError(
            f"destination already exists and is not empty: {dst}. "
            "Refusing to merge — pick a fresh folder.")

    # Create dst (idempotent if it's an existing empty dir).
    dst.mkdir(parents=True, exist_ok=True)

    # Marker FIRST so a crash anywhere below leaves a recoverable
    # signal in dst. Embed the source path so a recovery flow could
    # offer to roll the half-move back.
    marker = dst / MARKER_NAME
    try:
        marker.write_text(f"source={src}\n", encoding="utf-8")
    except OSError as e:
        raise MigrationError(
            f"failed to write migration marker at {marker}: {e}") from e

    files = _enumerate_files(src)
    total = len(files)
    logger.info(
        "cdmods migration: %d files from %s -> %s", total, src, dst)

    try:
        for idx, src_file in enumerate(files, start=1):
            rel = src_file.relative_to(src)
            dst_file = dst / rel
            if progress_callback is not None:
                try:
                    progress_callback(idx, total, str(rel))
                except Exception as cb_err:
                    # A buggy callback must not abort a real migration.
                    logger.debug(
                        "cdmods migration: progress callback raised: %s",
                        cb_err)

            try:
                src_hash = _sha256_file(src_file)
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst_file)
                dst_hash = _sha256_file(dst_file)
            except OSError as e:
                raise MigrationError(
                    f"copy failed for {src_file}: {e}") from e

            if src_hash != dst_hash:
                raise MigrationError(
                    f"checksum mismatch after copy: {src_file} "
                    f"(src {src_hash[:12]} vs dst {dst_hash[:12]})")
    except MigrationError:
        # Source is untouched; clear the half-copied destination so
        # the user can retry without tripping the non-empty guard.
        _cleanup_partial_destination(dst, marker)
        raise

    # Every file copied + verified. Now delete the source tree.
    try:
        shutil.rmtree(src)
    except OSError as e:
        # Files made it to dst safely. Source tree refused to
        # delete (locked file, permission, etc). Leave the marker
        # in place so the user can retry / clean up manually, but
        # surface the failure to the caller.
        raise MigrationError(
            f"copies verified but failed to remove source tree "
            f"{src}: {e}") from e

    # Source gone, copies verified — clear the marker last.
    try:
        marker.unlink()
    except OSError as e:
        logger.warning(
            "cdmods migration: completed but failed to clear marker "
            "%s: %s", marker, e)
