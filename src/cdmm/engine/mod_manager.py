"""Core mod state management — CRUD for mod registry."""
import logging
import shutil
from pathlib import Path

from cdmm.storage.database import Database

logger = logging.getLogger(__name__)


class ModManager:
    """Manages the mod registry: list, enable/disable, remove, metadata."""

    def __init__(self, db: Database, deltas_dir: Path) -> None:
        self._db = db
        self._deltas_dir = deltas_dir

    def list_mods(self, mod_type: str | None = None) -> list[dict]:
        """List all mods ordered by priority (load order), optionally filtered by type."""
        if mod_type:
            cursor = self._db.connection.execute(
                "SELECT id, name, mod_type, enabled, priority, import_date, game_version_hash, source_path "
                "FROM mods WHERE mod_type = ? ORDER BY priority",
                (mod_type,),
            )
        else:
            cursor = self._db.connection.execute(
                "SELECT id, name, mod_type, enabled, priority, import_date, game_version_hash, source_path "
                "FROM mods ORDER BY priority"
            )
        return [
            {
                "id": row[0], "name": row[1], "mod_type": row[2],
                "enabled": bool(row[3]), "priority": row[4], "import_date": row[5],
                "game_version_hash": row[6], "source_path": row[7],
            }
            for row in cursor.fetchall()
        ]

    def set_enabled(self, mod_id: int, enabled: bool) -> None:
        """Enable or disable a mod."""
        self._db.connection.execute(
            "UPDATE mods SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, mod_id),
        )
        self._db.connection.commit()
        logger.info("Mod %d %s", mod_id, "enabled" if enabled else "disabled")

    def remove_mod(self, mod_id: int) -> None:
        """Remove a mod and its deltas from the manager."""
        # Get mod name for logging
        cursor = self._db.connection.execute("SELECT name FROM mods WHERE id = ?", (mod_id,))
        row = cursor.fetchone()
        mod_name = row[0] if row else f"Mod {mod_id}"

        # Delete delta files from disk
        delta_dir = self._deltas_dir / str(mod_id)
        if delta_dir.exists():
            shutil.rmtree(delta_dir)

        # Database cascade handles mod_deltas and conflicts
        self._db.connection.execute("DELETE FROM mods WHERE id = ?", (mod_id,))
        self._db.connection.commit()
        logger.info("Removed mod: %s (id=%d)", mod_name, mod_id)

    def get_mod_details(self, mod_id: int) -> dict | None:
        """Get full mod details including delta information."""
        cursor = self._db.connection.execute(
            "SELECT id, name, mod_type, enabled, priority, import_date, game_version_hash, source_path "
            "FROM mods WHERE id = ?",
            (mod_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None

        mod = {
            "id": row[0], "name": row[1], "mod_type": row[2],
            "enabled": bool(row[3]), "priority": row[4], "import_date": row[5],
            "game_version_hash": row[6], "source_path": row[7],
            "changed_files": [],
        }

        # Get delta details
        delta_cursor = self._db.connection.execute(
            "SELECT file_path, byte_start, byte_end FROM mod_deltas WHERE mod_id = ? "
            "ORDER BY file_path, byte_start",
            (mod_id,),
        )
        for file_path, byte_start, byte_end in delta_cursor.fetchall():
            mod["changed_files"].append({
                "file_path": file_path,
                "byte_start": byte_start,
                "byte_end": byte_end,
            })

        return mod

    def clear_deltas(self, mod_id: int) -> None:
        """Remove all deltas for a mod (keeps the mod entry intact)."""
        delta_dir = self._deltas_dir / str(mod_id)
        if delta_dir.exists():
            shutil.rmtree(delta_dir)
        self._db.connection.execute("DELETE FROM mod_deltas WHERE mod_id = ?", (mod_id,))
        self._db.connection.execute("DELETE FROM conflicts WHERE mod_a_id = ? OR mod_b_id = ?",
                                    (mod_id, mod_id))
        self._db.connection.commit()
        logger.info("Cleared deltas for mod %d", mod_id)

    def cleanup_orphaned_deltas(self) -> None:
        """Remove delta folders on disk that have no matching mod in the DB."""
        if not self._deltas_dir.exists():
            return
        cursor = self._db.connection.execute("SELECT id FROM mods")
        valid_ids = {str(row[0]) for row in cursor.fetchall()}
        for entry in self._deltas_dir.iterdir():
            if entry.is_dir() and entry.name not in valid_ids:
                shutil.rmtree(entry)
                logger.info("Cleaned up orphaned delta folder: %s", entry.name)

    def rename_mod(self, mod_id: int, new_name: str) -> None:
        """Rename a mod."""
        self._db.connection.execute(
            "UPDATE mods SET name = ? WHERE id = ?", (new_name, mod_id))
        self._db.connection.commit()
        logger.info("Renamed mod %d to '%s'", mod_id, new_name)

    def get_mod_count(self) -> int:
        cursor = self._db.connection.execute("SELECT COUNT(*) FROM mods")
        return cursor.fetchone()[0]

    def get_next_priority(self) -> int:
        """Get the next available priority value (for new mods)."""
        cursor = self._db.connection.execute("SELECT COALESCE(MAX(priority), 0) + 1 FROM mods")
        return cursor.fetchone()[0]

    def move_up(self, mod_id: int) -> None:
        """Move a mod higher in load order (lower priority number = loaded earlier = loses conflicts)."""
        mods = self.list_mods()
        idx = next((i for i, m in enumerate(mods) if m["id"] == mod_id), None)
        if idx is None or idx == 0:
            return
        self._swap_priority(mods[idx]["id"], mods[idx - 1]["id"])
        logger.info("Moved mod %d up in load order", mod_id)

    def move_down(self, mod_id: int) -> None:
        """Move a mod lower in load order (higher priority number = loaded later = wins conflicts)."""
        mods = self.list_mods()
        idx = next((i for i, m in enumerate(mods) if m["id"] == mod_id), None)
        if idx is None or idx >= len(mods) - 1:
            return
        self._swap_priority(mods[idx]["id"], mods[idx + 1]["id"])
        logger.info("Moved mod %d down in load order", mod_id)

    def _swap_priority(self, mod_a_id: int, mod_b_id: int) -> None:
        """Swap priority values between two mods."""
        cursor = self._db.connection.execute(
            "SELECT id, priority FROM mods WHERE id IN (?, ?)", (mod_a_id, mod_b_id))
        rows = {r[0]: r[1] for r in cursor.fetchall()}
        if len(rows) != 2:
            return
        self._db.connection.execute(
            "UPDATE mods SET priority = ? WHERE id = ?", (rows[mod_b_id], mod_a_id))
        self._db.connection.execute(
            "UPDATE mods SET priority = ? WHERE id = ?", (rows[mod_a_id], mod_b_id))
        self._db.connection.commit()

    def set_winner(self, mod_id: int) -> None:
        """Set a mod as #1 priority (wins all conflicts)."""
        cursor = self._db.connection.execute("SELECT COALESCE(MIN(priority), 1) - 1 FROM mods")
        min_priority = cursor.fetchone()[0]
        self._db.connection.execute(
            "UPDATE mods SET priority = ? WHERE id = ?", (min_priority, mod_id))
        self._db.connection.commit()
        logger.info("Set mod %d as winner (priority=%d)", mod_id, min_priority)
