import hashlib
import time

try:
    import xxhash
    _USE_XXHASH = True
except ImportError:
    _USE_XXHASH = False
import logging
import os
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from cdumm.storage.database import Database

logger = logging.getLogger(__name__)

# PAZ directory pattern: 0000, 0001, ..., 0099 (covers current and future directories)
PAZ_DIRS = [f"{i:04d}" for i in range(100)]
PAZ_PATTERN = "*.paz"
PAMT_FILE = "0.pamt"
PAPGT_FILE = "meta/0.papgt"
PATHC_FILE = "meta/0.pathc"

HASH_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB chunks for hashing


def verify_live_disk_matches_backups(
        game_dir: Path, vanilla_dir: Path,
        max_checked: int = 5) -> tuple[bool, list[str]]:
    """Sanity-check: do the live game files still match the vanilla
    backups CDUMM has on disk?

    Rationale: Rescan hashes whatever is on disk into the snapshot.
    If disk has been modified since the last apply (a silent Steam
    update, a stale-backup revert, a mod that bypassed CDUMM), the
    snapshot captures modded bytes as "vanilla." Every future
    revert restores to the wrong state.

    This helper reads up to ``max_checked`` full-file backups from
    ``vanilla_dir`` and compares each against its live counterpart
    in ``game_dir``. If any differ, the disk is NOT vanilla — abort
    rescan and tell the user to Revert (if backups are valid) or run
    Steam Verify (if the game was externally updated).

    Skipped:
    - Range backups (``*.vranges``) — they're not raw copies.
    - Missing live counterparts — CDUMM may have renamed files.

    Returns ``(is_clean, problem_files)``.
    """
    import os
    problems: list[str] = []
    if not vanilla_dir.exists():
        return True, []
    checked = 0
    for root, _dirs, files in os.walk(vanilla_dir):
        for name in files:
            if name.endswith(".vranges"):
                continue
            bp = Path(root) / name
            rel = bp.relative_to(vanilla_dir)
            live = game_dir / rel
            if not live.exists():
                continue
            try:
                if live.stat().st_size != bp.stat().st_size:
                    problems.append(str(rel).replace("\\", "/"))
                    continue
                if live.read_bytes() != bp.read_bytes():
                    problems.append(str(rel).replace("\\", "/"))
            except OSError as e:
                logger.debug(
                    "verify_live_disk_matches_backups: skip %s (%s)",
                    rel, e)
                continue
            checked += 1
            if checked >= max_checked and not problems:
                return True, []
    return (len(problems) == 0), problems


# Files Apply rewrites unconditionally as part of CDUMM's PAZ
# integrity-chain housekeeping. Whenever any PAZ mod is applied,
# `meta/0.papgt` (PAPGT — group hashes for every PAMT) gets
# rebuilt and `meta/0.pathc` (PATHC — texture hash chain) may be
# updated. Their post-apply size differs from the vanilla snapshot
# by design, so they would otherwise generate a false-positive
# drift signal on every launch after a successful Apply. They're
# only excluded when there's at least one applied mod — with zero
# applied mods these files are still vanilla and SHOULD be checked.
APPLY_REWRITTEN_META_FILES = frozenset({
    "meta/0.papgt",
    "meta/0.pathc",
})


def detect_snapshot_drift(
        db,
        game_dir: Path,
        max_reported: int = 20) -> tuple[bool, list[str]]:
    """Quick size-only drift check for files not touched by any
    currently-applied mod.

    Complements the Steam-buildid / exe-hash fingerprint trigger:
    that catches *game patches*, this catches *file tampering* when
    the buildid hasn't changed (manual edits, antivirus rewrites,
    third-party tool drops, half-finished Steam Verify runs that
    didn't bump the buildid).

    Strategy:
    1. Build the set of `file_path` values referenced by mod_deltas
       where the mod is `applied=1` — these files are EXPECTED to
       differ from the snapshot.
    2. Add the apply-rewritten meta integrity files (PAPGT, PATHC)
       to the same exclusion set whenever any mod is applied —
       Apply rebuilds them, so their post-apply size differs from
       the vanilla snapshot by design. Without this, every launch
       after Apply would false-flag those two files as drift.
    3. For every other file in the `snapshots` table, stat the live
       counterpart in `game_dir` and compare `st_size` against the
       snapshot's stored `file_size`.
    4. If any size mismatch shows up, the disk has drifted from the
       state CDUMM thinks it's in.

    Cost: O(N) `os.stat` calls on PAZ files (~200 in current installs).
    No hashing, no full reads. Designed to add < 100 ms to startup.

    Returns ``(drift_detected, sample_mismatches)``. ``False, []`` when
    the snapshots table is empty (first-time install — nothing to
    drift from).
    """
    try:
        snap_rows = db.connection.execute(
            "SELECT file_path, file_size FROM snapshots"
        ).fetchall()
    except Exception as e:
        logger.debug("detect_snapshot_drift: snapshots query failed: %s", e)
        return False, []
    if not snap_rows:
        return False, []

    try:
        touched_rows = db.connection.execute(
            "SELECT DISTINCT md.file_path FROM mod_deltas md "
            "JOIN mods m ON m.id = md.mod_id WHERE m.applied = 1"
        ).fetchall()
    except Exception as e:
        logger.debug("detect_snapshot_drift: mod_deltas query failed: %s", e)
        return False, []
    touched = {row[0] for row in touched_rows}

    # If anything is applied, exclude the meta integrity files Apply
    # rewrites. With zero applied mods these files should still be
    # vanilla; leaving them in the check covers the no-mods drift
    # case (someone tampered with meta/* on a clean install).
    try:
        applied_count_row = db.connection.execute(
            "SELECT COUNT(*) FROM mods WHERE applied = 1"
        ).fetchone()
        applied_count = applied_count_row[0] if applied_count_row else 0
    except Exception as e:
        logger.debug(
            "detect_snapshot_drift: applied-count query failed: %s", e)
        applied_count = 0
    if applied_count > 0:
        touched |= APPLY_REWRITTEN_META_FILES

    mismatches: list[str] = []
    for path_str, expected_size in snap_rows:
        if path_str in touched:
            continue
        live = game_dir / path_str
        try:
            live_size = live.stat().st_size
        except OSError:
            # File missing on disk. Could be a renamed PAZ from an
            # older game version; not safe to flag as drift here.
            continue
        if live_size != expected_size:
            mismatches.append(path_str)
            if len(mismatches) >= max_reported:
                break
    return (len(mismatches) > 0), mismatches


def hash_matches(path: Path, stored_hash: str) -> bool:
    """Check if a file matches a stored hash, auto-detecting the algorithm.

    xxh3_128 digests are 32 chars, SHA-256 are 64 chars.
    """
    algo = "sha256" if len(stored_hash) == 64 else "xxh3"
    current, _ = hash_file(path, algo=algo)
    return current == stored_hash


def hash_file(path: Path, progress_callback=None, algo: str = "auto") -> tuple[str, int]:
    """Hash a file using xxh3_128 (fast) or SHA-256 (fallback).

    Args:
        path: File to hash.
        progress_callback: Optional callable(bytes_read, total_bytes) per chunk.
        algo: "auto" (xxhash if available), "sha256", or "xxh3".

    Returns:
        (hex_digest, file_size)
    """
    file_size = path.stat().st_size
    if algo == "sha256" or (algo == "auto" and not _USE_XXHASH):
        h = hashlib.sha256()
    else:
        h = xxhash.xxh3_128()
    bytes_read = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
            bytes_read += len(chunk)
            if progress_callback:
                progress_callback(bytes_read, file_size)
    return h.hexdigest(), file_size


class SnapshotWorker(QObject):
    """Background worker for creating vanilla snapshots."""

    progress_updated = Signal(int, str)  # percent, message
    finished = Signal(int)  # total files hashed
    error_occurred = Signal(str)
    activity = Signal(str, str, str)  # category, message, detail — for activity log

    def __init__(self, game_dir: Path, db_path: Path) -> None:
        super().__init__()
        self._game_dir = game_dir
        self._db_path = db_path  # Store path, create connection on worker thread

    def run(self) -> None:
        try:
            # Create a NEW SQLite connection on this thread
            # (SQLite connections can't cross threads)
            self._thread_db = Database(self._db_path)
            self._thread_db.initialize()
            self._create_snapshot()
            self._thread_db.close()
        except Exception as e:
            logger.error("Snapshot creation failed: %s", e, exc_info=True)
            self.error_occurred.emit(f"Snapshot creation failed: {e}")

    def _create_snapshot(self) -> None:
        self.progress_updated.emit(0, "Checking for mod artifacts...")

        # Check for signs of modding BEFORE snapshotting.
        problems = self._check_pre_snapshot()

        # Mod directories (0036+) are never part of vanilla — Steam verify
        # doesn't remove them. Clean these up automatically (safe) but
        # block on actual file modifications (not safe to auto-fix).
        import shutil
        real_problems = []
        for p in problems:
            if p.startswith("Mod directory"):
                dir_name = p.split("/")[0].replace("Mod directory ", "")
                d = self._game_dir / dir_name
                if d.exists():
                    shutil.rmtree(d, ignore_errors=True)
                    logger.info("Removed mod directory before snapshot: %s", dir_name)
                    self.progress_updated.emit(1, f"Removed mod directory {dir_name}/")
                    self.activity.emit("cleanup",
                                       f"Removed mod directory {dir_name}/",
                                       "Not part of vanilla game — created by mods")
            else:
                real_problems.append(p)

        if real_problems:
            problem_list = "\n".join(f"  - {p}" for p in real_problems)
            self.error_occurred.emit(
                f"Cannot create snapshot — game files appear to be modded:\n\n"
                f"{problem_list}\n\n"
                f"Please verify game files through Steam first, then try again."
            )
            return

        self.progress_updated.emit(2, "Scanning game directories...")

        # Collect all files to hash
        files_to_hash: list[tuple[Path, str]] = []  # (abs_path, relative_posix_path)

        # PAZ and PAMT files
        for dir_name in PAZ_DIRS:
            dir_path = self._game_dir / dir_name
            if not dir_path.exists():
                continue

            # PAMT file
            pamt = dir_path / PAMT_FILE
            if pamt.exists():
                files_to_hash.append((pamt, f"{dir_name}/{PAMT_FILE}"))

            # PAZ files
            for paz in sorted(dir_path.glob(PAZ_PATTERN)):
                files_to_hash.append((paz, f"{dir_name}/{paz.name}"))

        # PAPGT file
        papgt = self._game_dir / PAPGT_FILE
        if papgt.exists():
            files_to_hash.append((papgt, PAPGT_FILE))

        # PATHC file (texture index)
        pathc = self._game_dir / PATHC_FILE
        if pathc.exists():
            files_to_hash.append((pathc, PATHC_FILE))

        total = len(files_to_hash)
        if total == 0:
            self.error_occurred.emit(
                "No PAZ/PAMT/PAPGT files found in game directory.\n\n"
                f"Searched: {self._game_dir}\n"
                "Expected directories: 0000-0032 with .paz and .pamt files."
            )
            return

        # Calculate total bytes for accurate progress
        total_bytes = sum(f.stat().st_size for f, _ in files_to_hash)
        total_gb = total_bytes / (1024 ** 3)
        logger.info("Snapshot: %d files, %.1f GB to hash", total, total_gb)
        self.progress_updated.emit(3, f"Found {total} files ({total_gb:.1f} GB). Hashing...")

        # Clear existing snapshot
        self._thread_db.connection.execute("DELETE FROM snapshots")

        bytes_hashed = 0
        last_pct = -1

        for i, (abs_path, rel_path) in enumerate(files_to_hash):
            file_size_bytes = abs_path.stat().st_size
            file_size_mb = file_size_bytes / (1024 * 1024)
            logger.debug("Hashing [%d/%d]: %s (%.0f MB)", i + 1, total, rel_path, file_size_mb)

            def on_chunk(chunk_bytes_read, chunk_total, _rel=rel_path, _i=i,
                         _base=bytes_hashed, _fmb=file_size_mb):
                nonlocal last_pct
                overall = _base + chunk_bytes_read
                pct = int(overall / total_bytes * 100) if total_bytes > 0 else 0
                if pct != last_pct:
                    last_pct = pct
                    chunk_pct = int(chunk_bytes_read / chunk_total * 100) if chunk_total > 0 else 100
                    self.progress_updated.emit(
                        pct,
                        f"[{_i + 1}/{total}] {_rel} ({_fmb:.0f} MB) — {chunk_pct}%"
                    )

            file_hash, file_size = hash_file(abs_path, progress_callback=on_chunk)
            bytes_hashed += file_size

            self._thread_db.connection.execute(
                "INSERT OR REPLACE INTO snapshots (file_path, file_hash, file_size) "
                "VALUES (?, ?, ?)",
                (rel_path, file_hash, file_size),
            )

            pct = int(bytes_hashed / total_bytes * 100) if total_bytes > 0 else 0
            self.progress_updated.emit(pct, f"[{i + 1}/{total}] {rel_path} — done")
            logger.debug("Hashed: %s -> %s", rel_path, file_hash[:16])
            time.sleep(0)  # yield GIL so GUI stays responsive

        self._thread_db.connection.commit()
        logger.info("Snapshot complete: %d files hashed", total)
        self.finished.emit(total)

    def _check_pre_snapshot(self) -> list[str]:
        """Check for signs that game files are modded.

        Returns a list of problems found. Empty list = safe to snapshot.
        Never modifies game files — only reports.
        """
        problems = []

        # 1. Check for mod-created directories (0036+)
        for d in sorted(self._game_dir.iterdir()):
            if not d.is_dir() or not d.name.isdigit() or len(d.name) != 4:
                continue
            if int(d.name) >= 36:
                files = list(d.iterdir())
                if files:
                    problems.append(
                        f"Mod directory {d.name}/ exists ({len(files)} files)")

        # 2. Check PAPGT for mod entries (vanilla has 33 entries, ~577 bytes)
        game_papgt = self._game_dir / "meta" / "0.papgt"
        if game_papgt.exists():
            papgt_data = game_papgt.read_bytes()
            if len(papgt_data) >= 12:
                entry_count = papgt_data[8]
                if entry_count > 35:  # vanilla has 33 entries
                    problems.append(
                        f"PAPGT has {entry_count} entries (vanilla has ~33)")

        # 3. Check if CDMods/vanilla backup exists (means mods were applied before).
        # If backups have different sizes from game files, the backups are
        # stale (from a previous game version). Delete them — the user just
        # verified through Steam so the game files ARE vanilla now.
        import shutil as _shutil
        vanilla_dir = self._game_dir / "CDMods" / "vanilla"
        if vanilla_dir.exists() and any(vanilla_dir.rglob("*")):
            stale_backups = []
            for backup in vanilla_dir.rglob("*"):
                if not backup.is_file() or backup.name.endswith(".vranges"):
                    continue
                rel = str(backup.relative_to(vanilla_dir)).replace("\\", "/")
                game_file = self._game_dir / rel.replace("/", os.sep)
                if game_file.exists():
                    if game_file.stat().st_size != backup.stat().st_size:
                        stale_backups.append(backup)
            if stale_backups:
                # Backups are from an old game version — delete them
                for b in stale_backups:
                    b.unlink(missing_ok=True)
                    logger.info("Deleted stale backup: %s", b)
                # Also delete range backups which are version-specific
                for vr in vanilla_dir.rglob("*.vranges"):
                    vr.unlink(missing_ok=True)
                logger.info("Cleared %d stale vanilla backups (game was updated)",
                            len(stale_backups))

        return problems


class SnapshotManager:
    """High-level snapshot operations."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def has_snapshot(self) -> bool:
        cursor = self._db.connection.execute("SELECT COUNT(*) FROM snapshots")
        return cursor.fetchone()[0] > 0

    def get_file_hash(self, rel_path: str) -> str | None:
        cursor = self._db.connection.execute(
            "SELECT file_hash FROM snapshots WHERE file_path = ?", (rel_path,)
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def get_snapshot_count(self) -> int:
        cursor = self._db.connection.execute("SELECT COUNT(*) FROM snapshots")
        return cursor.fetchone()[0]

    def detect_changes(self, game_dir: Path) -> list[tuple[str, str]]:
        """Compare current game files against snapshot. Returns list of (file_path, change_type)."""
        changes: list[tuple[str, str]] = []
        cursor = self._db.connection.execute("SELECT file_path, file_hash FROM snapshots")
        for rel_path, stored_hash in cursor.fetchall():
            abs_path = game_dir / rel_path.replace("/", "\\")
            if not abs_path.exists():
                changes.append((rel_path, "deleted"))
            else:
                if not hash_matches(abs_path, stored_hash):
                    changes.append((rel_path, "modified"))
        return changes
