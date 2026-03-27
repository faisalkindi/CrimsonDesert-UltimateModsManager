import hashlib
import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from cdmm.storage.database import Database

logger = logging.getLogger(__name__)

# PAZ directory pattern: 0000, 0001, ..., 0099 (covers current and future directories)
PAZ_DIRS = [f"{i:04d}" for i in range(100)]
PAZ_PATTERN = "*.paz"
PAMT_FILE = "0.pamt"
PAPGT_FILE = "meta/0.papgt"

HASH_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB chunks for hashing


def hash_file(path: Path, progress_callback=None) -> tuple[str, int]:
    """Compute SHA-256 hash of a file using chunked reads.

    Args:
        path: File to hash.
        progress_callback: Optional callable(bytes_read, total_bytes) called per chunk.

    Returns:
        (hex_digest, file_size)
    """
    file_size = path.stat().st_size
    h = hashlib.sha256()
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
        self.progress_updated.emit(0, "Scanning game directories...")

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
        self.progress_updated.emit(0, f"Found {total} files ({total_gb:.1f} GB). Hashing...")

        # Clear existing snapshot
        self._thread_db.connection.execute("DELETE FROM snapshots")

        bytes_hashed = 0
        last_pct = -1  # throttle: only emit when percentage changes

        # Hash each file and store
        for i, (abs_path, rel_path) in enumerate(files_to_hash):
            file_size_bytes = abs_path.stat().st_size
            file_size_mb = file_size_bytes / (1024 * 1024)
            logger.debug("Hashing [%d/%d]: %s (%.0f MB)", i + 1, total, rel_path, file_size_mb)

            # Progress callback — throttled to only emit when overall % changes
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

        self._thread_db.connection.commit()
        logger.info("Snapshot complete: %d files hashed", total)
        self.finished.emit(total)


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
                current_hash, _ = hash_file(abs_path)
                if current_hash != stored_hash:
                    changes.append((rel_path, "modified"))
        return changes
