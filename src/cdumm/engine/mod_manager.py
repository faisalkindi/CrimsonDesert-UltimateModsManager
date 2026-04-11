"""Core mod state management — CRUD for mod registry."""
import logging
import shutil
from pathlib import Path

from cdumm.storage.database import Database

logger = logging.getLogger(__name__)


class ModManager:
    """Manages the mod registry: list, enable/disable, remove, metadata."""

    def __init__(self, db: Database, deltas_dir: Path) -> None:
        self._db = db
        self._deltas_dir = deltas_dir

    def list_mods(self, mod_type: str | None = None) -> list[dict]:
        """List all mods ordered by priority (load order), optionally filtered by type."""
        query = (
            "SELECT id, name, mod_type, enabled, priority, import_date, "
            "game_version_hash, source_path, author, version, description, configurable, "
            "force_inplace, notes "
            "FROM mods"
        )
        if mod_type:
            cursor = self._db.connection.execute(
                query + " WHERE mod_type = ? ORDER BY priority", (mod_type,))
        else:
            cursor = self._db.connection.execute(query + " ORDER BY priority")
        return [
            {
                "id": row[0], "name": row[1], "mod_type": row[2],
                "enabled": bool(row[3]), "priority": row[4], "import_date": row[5],
                "game_version_hash": row[6], "source_path": row[7],
                "author": row[8], "version": row[9], "description": row[10],
                "configurable": bool(row[11]) if len(row) > 11 else False,
                "force_inplace": bool(row[12]) if len(row) > 12 else False,
                "notes": row[13] if len(row) > 13 else None,
            }
            for row in cursor.fetchall()
        ]

    def set_notes(self, mod_id: int, notes: str) -> None:
        """Set user notes for a mod."""
        self._db.connection.execute(
            "UPDATE mods SET notes = ? WHERE id = ?",
            (notes or None, mod_id),
        )
        self._db.connection.commit()

    def set_enabled(self, mod_id: int, enabled: bool) -> None:
        """Enable or disable a mod."""
        self._db.connection.execute(
            "UPDATE mods SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, mod_id),
        )
        self._db.connection.commit()
        logger.info("Mod %d %s", mod_id, "enabled" if enabled else "disabled")

    def remove_mod(self, mod_id: int) -> None:
        """Remove a mod and its deltas from the manager.

        Files are NOT reverted here — the caller must Apply after removing
        to revert game files. We disable the mod first and keep its delta
        entries until after the next Apply reverts them, then clean up.
        """
        cursor = self._db.connection.execute("SELECT name, enabled FROM mods WHERE id = ?", (mod_id,))
        row = cursor.fetchone()
        mod_name = row[0] if row else f"Mod {mod_id}"
        was_enabled = bool(row[1]) if row else False

        if was_enabled:
            # Disable first — next Apply will revert its files
            self._db.connection.execute(
                "UPDATE mods SET enabled = 0 WHERE id = ?", (mod_id,))
            self._db.connection.commit()
            logger.info("Disabled for removal: %s (id=%d) — Apply needed to revert files",
                        mod_name, mod_id)

        # Delete delta files from disk
        delta_dir = self._deltas_dir / str(mod_id)
        if delta_dir.exists():
            shutil.rmtree(delta_dir)

        # Delete from DB (cascade removes mod_deltas and conflicts)
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

    def validate_mods_post_update(self, game_dir: Path) -> dict[int, str]:
        """Validate all mods against current game files after a game update.

        Checks each mod's delta metadata against current PAMT entries to
        detect if the game update broke any mods. Does NOT modify mod state —
        just returns a dict of {mod_id: reason} for broken mods.

        Checks:
        1. ENTR deltas: does the target entry still exist in PAMT? Is orig_size the same?
        2. Byte-range deltas: is the vanilla file size still the same?
        3. New files: always valid (standalone PAZ mods aren't affected by updates)
        """
        from cdumm.archive.paz_parse import parse_pamt

        broken: dict[int, str] = {}

        # Cache PAMT entries per directory to avoid re-parsing
        pamt_cache: dict[str, dict[str, tuple[int, int]]] = {}  # {dir: {entry_path: (comp_size, orig_size)}}

        def get_pamt_entries(pamt_dir: str) -> dict[str, tuple[int, int]]:
            if pamt_dir in pamt_cache:
                return pamt_cache[pamt_dir]
            pamt_path = game_dir / pamt_dir / "0.pamt"
            if not pamt_path.exists():
                pamt_cache[pamt_dir] = {}
                return {}
            try:
                entries = parse_pamt(str(pamt_path), paz_dir=str(game_dir / pamt_dir))
                result = {e.path: (e.comp_size, e.orig_size) for e in entries}
                pamt_cache[pamt_dir] = result
                return result
            except Exception as e:
                logger.warning("Failed to parse PAMT %s: %s", pamt_dir, e)
                pamt_cache[pamt_dir] = {}
                return {}

        # Get all mods with deltas
        mods = self._db.connection.execute(
            "SELECT id, name FROM mods WHERE id IN "
            "(SELECT DISTINCT mod_id FROM mod_deltas)").fetchall()

        for mod_id, mod_name in mods:
            reasons = []

            # Check ENTR deltas
            entr_deltas = self._db.connection.execute(
                "SELECT delta_path, entry_path, file_path FROM mod_deltas "
                "WHERE mod_id = ? AND entry_path IS NOT NULL",
                (mod_id,)).fetchall()

            for delta_path, entry_path, file_path in entr_deltas:
                pamt_dir = file_path.split("/")[0]
                pamt_entries = get_pamt_entries(pamt_dir)

                if not pamt_entries:
                    reasons.append(f"PAZ directory {pamt_dir} no longer has a valid PAMT")
                    break

                if entry_path not in pamt_entries:
                    reasons.append(f"{entry_path} no longer exists in {pamt_dir}")
                    continue

                # Compare orig_size from ENTR metadata against current PAMT
                try:
                    from cdumm.engine.delta_engine import load_entry_delta
                    _, meta = load_entry_delta(Path(delta_path))
                    old_orig = meta.get("vanilla_orig_size", 0)
                    new_comp, new_orig = pamt_entries[entry_path]
                    if old_orig and new_orig != old_orig:
                        reasons.append(
                            f"{entry_path} size changed ({old_orig} -> {new_orig})")
                except Exception:
                    pass

            # Check byte-range deltas via mod_vanilla_sizes
            size_rows = self._db.connection.execute(
                "SELECT file_path, vanilla_size FROM mod_vanilla_sizes "
                "WHERE mod_id = ?", (mod_id,)).fetchall()

            for file_path, stored_size in size_rows:
                game_file = game_dir / file_path.replace("/", "\\")
                if game_file.exists():
                    current_size = game_file.stat().st_size
                    if current_size != stored_size:
                        reasons.append(
                            f"{file_path} size changed ({stored_size} -> {current_size})")

            if reasons:
                broken[mod_id] = "; ".join(reasons[:3])
                logger.info("Mod '%s' (id=%d) broken by game update: %s",
                            mod_name, mod_id, broken[mod_id])

        return broken

    def get_mod_game_status(self, mod_id: int, game_dir: Path) -> str:
        """Check if a mod is actually active in the game files.

        Returns:
            'active'      — mod's files differ from vanilla (mod is working)
            'not applied' — mod is enabled but game files are still vanilla
            'no data'     — mod has 0 deltas (broken import, needs re-import)
            'outdated'    — mod was imported for a different game version
            'disabled'    — mod is not enabled
        """
        # Check if enabled
        row = self._db.connection.execute(
            "SELECT enabled, game_version_hash FROM mods WHERE id = ?", (mod_id,)).fetchone()
        if not row or not row[0]:
            # Check if also outdated
            is_outdated = False
            if row and row[1]:
                if not hasattr(self, '_cached_game_version'):
                    try:
                        from cdumm.engine.version_detector import detect_game_version
                        self._cached_game_version = detect_game_version(game_dir)
                    except Exception:
                        self._cached_game_version = None
                if self._cached_game_version and row[1] != self._cached_game_version:
                    is_outdated = True
            if not is_outdated and row:
                bad_copy = self._db.connection.execute(
                    "SELECT COUNT(*) FROM mod_deltas "
                    "WHERE mod_id = ? AND is_new = 1 AND file_path LIKE '%.paz' AND byte_end > 100000000",
                    (mod_id,)).fetchone()[0]
                if bad_copy > 0:
                    is_outdated = True
            return "disabled (outdated)" if is_outdated else "disabled"

        # Check if mod is outdated (version mismatch or old format)
        is_outdated = False
        if row[1]:
            if not hasattr(self, '_cached_game_version'):
                try:
                    from cdumm.engine.version_detector import detect_game_version
                    self._cached_game_version = detect_game_version(game_dir)
                except Exception:
                    self._cached_game_version = None
            if self._cached_game_version and row[1] != self._cached_game_version:
                is_outdated = True

        if not is_outdated:
            bad_copy = self._db.connection.execute(
                "SELECT COUNT(*) FROM mod_deltas "
                "WHERE mod_id = ? AND is_new = 1 AND file_path LIKE '%.paz' AND byte_end > 100000000",
                (mod_id,)).fetchone()[0]
            if bad_copy > 0:
                is_outdated = True

        # Helper to append "(outdated)" suffix when applicable
        def _status(base: str) -> str:
            return f"{base} (outdated)" if is_outdated else base

        # If outdated with no further checks needed, return early
        if is_outdated:
            delta_count = self._db.connection.execute(
                "SELECT COUNT(*) FROM mod_deltas WHERE mod_id = ?", (mod_id,)).fetchone()[0]
            if delta_count == 0:
                return "outdated"

        # Check if mod has any deltas
        delta_count = self._db.connection.execute(
            "SELECT COUNT(*) FROM mod_deltas WHERE mod_id = ?", (mod_id,)).fetchone()[0]
        if delta_count == 0:
            return "no data"

        # Get the mod's target files (excluding meta/0.papgt which is always rebuilt)
        files = self._db.connection.execute(
            "SELECT DISTINCT file_path FROM mod_deltas WHERE mod_id = ? AND file_path != 'meta/0.papgt'",
            (mod_id,)).fetchall()
        if not files:
            return "no data"

        # Check if any target file differs from vanilla snapshot
        import os
        from cdumm.engine.snapshot_manager import hash_file

        # Check if mod has ENTR deltas — these go to overlay PAZ, not in-place.
        # If an overlay directory exists, the mod is active via overlay.
        has_entr = self._db.connection.execute(
            "SELECT COUNT(*) FROM mod_deltas WHERE mod_id = ? AND entry_path IS NOT NULL",
            (mod_id,)).fetchone()[0]
        if has_entr > 0:
            for d in sorted(game_dir.iterdir()):
                if (d.is_dir() and d.name.isdigit() and len(d.name) == 4
                        and int(d.name) >= 37
                        and (d / "0.paz").exists() and (d / "0.pamt").exists()):
                    return _status("active")

        for (file_path,) in files:
            is_new = self._db.connection.execute(
                "SELECT is_new FROM mod_deltas WHERE mod_id = ? AND file_path = ? LIMIT 1",
                (mod_id, file_path)).fetchone()
            game_file = game_dir / file_path.replace("/", os.sep)

            if is_new and is_new[0]:
                if game_file.exists():
                    return _status("active")
                continue

            if not game_file.exists():
                continue

            snap = self._db.connection.execute(
                "SELECT file_hash FROM snapshots WHERE file_path = ?", (file_path,)).fetchone()
            if snap is None:
                vanilla_dir = game_dir / "CDMods" / "vanilla"
                full_backup = vanilla_dir / file_path.replace("/", os.sep)
                range_backup = vanilla_dir / (file_path.replace("/", "_") + ".vranges")
                if full_backup.exists() or range_backup.exists():
                    return _status("active")
                continue
            from cdumm.engine.snapshot_manager import hash_matches
            if not hash_matches(game_file, snap[0]):
                return _status("active")

        return _status("not applied")

    def cleanup_orphaned_deltas(self) -> None:
        """Remove delta folders on disk that have no matching mod in the DB.
        Also clean up DB entries pointing to missing delta files."""
        import os

        if self._deltas_dir.exists():
            cursor = self._db.connection.execute("SELECT id FROM mods")
            valid_ids = {str(row[0]) for row in cursor.fetchall()}
            for entry in self._deltas_dir.iterdir():
                if entry.is_dir() and entry.name not in valid_ids:
                    shutil.rmtree(entry)
                    logger.info("Cleaned up orphaned delta folder: %s", entry.name)

        # Clean up DB entries pointing to missing delta files (zombie entries
        # from old game update resets that deleted files but kept DB rows)
        rows = self._db.connection.execute(
            "SELECT md.id, md.delta_path, m.name FROM mod_deltas md "
            "JOIN mods m ON m.id = md.mod_id").fetchall()
        missing_ids = []
        for md_id, dp, name in rows:
            if not os.path.exists(dp):
                missing_ids.append(md_id)
        if missing_ids:
            # Batch deletes to avoid SQLite's variable limit (~999)
            for i in range(0, len(missing_ids), 500):
                batch = missing_ids[i:i + 500]
                placeholders = ",".join("?" * len(batch))
                self._db.connection.execute(
                    f"DELETE FROM mod_deltas WHERE id IN ({placeholders})",
                    batch)
            self._db.connection.commit()
            logger.info("Cleaned up %d orphaned delta DB entries", len(missing_ids))

        # Clean up orphaned source folders
        sources_dir = self._deltas_dir.parent / "sources"
        if sources_dir.exists():
            valid_ids = {str(row[0]) for row in
                         self._db.connection.execute("SELECT id FROM mods").fetchall()}
            for entry in sources_dir.iterdir():
                if entry.is_dir() and entry.name not in valid_ids:
                    shutil.rmtree(entry, ignore_errors=True)
                    logger.info("Cleaned up orphaned source folder: %s", entry.name)

        # Remove zombie mods (0 deltas, disabled — from failed imports)
        zombies = self._db.connection.execute(
            "SELECT m.id, m.name FROM mods m "
            "WHERE m.enabled = 0 AND NOT EXISTS "
            "(SELECT 1 FROM mod_deltas md WHERE md.mod_id = m.id)"
        ).fetchall()
        for mod_id, name in zombies:
            self._db.connection.execute("DELETE FROM mods WHERE id = ?", (mod_id,))
            logger.info("Removed zombie mod (0 deltas, disabled): %s (id=%d)", name, mod_id)
        if zombies:
            self._db.connection.commit()
            logger.info("Cleaned up %d zombie mod(s)", len(zombies))

        # Remove duplicate mods (same name, keep highest priority / newest)
        dupes = self._db.connection.execute(
            "SELECT name, COUNT(*) as cnt FROM mods "
            "GROUP BY name HAVING cnt > 1").fetchall()
        for name, cnt in dupes:
            rows = self._db.connection.execute(
                "SELECT id, priority FROM mods WHERE name = ? ORDER BY priority ASC",
                (name,)).fetchall()
            # Keep the first (highest priority = lowest number), remove the rest
            keep_id = rows[0][0]
            for mod_id, _ in rows[1:]:
                self.remove_mod(mod_id)
                logger.info("Removed duplicate mod: %s (id=%d, kept id=%d)",
                            name, mod_id, keep_id)

    def rename_mod(self, mod_id: int, new_name: str) -> None:
        """Rename a mod."""
        self._db.connection.execute(
            "UPDATE mods SET name = ? WHERE id = ?", (new_name, mod_id))
        self._db.connection.commit()
        logger.info("Renamed mod %d to '%s'", mod_id, new_name)

    def get_file_counts(self) -> dict[int, int]:
        """Get delta file counts for all mods in a single query."""
        cursor = self._db.connection.execute(
            "SELECT mod_id, COUNT(*) FROM mod_deltas GROUP BY mod_id")
        return dict(cursor.fetchall())

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

    def reorder_mods(self, ordered_ids: list[int]) -> None:
        """Reassign priorities based on a new ordering."""
        for priority, mod_id in enumerate(ordered_ids):
            self._db.connection.execute(
                "UPDATE mods SET priority = ? WHERE id = ?", (priority, mod_id))
        self._db.connection.commit()
        logger.info("Reordered %d mods", len(ordered_ids))

    def set_winner(self, mod_id: int) -> None:
        """Set a mod as #1 priority (wins all conflicts)."""
        cursor = self._db.connection.execute("SELECT COALESCE(MIN(priority), 1) - 1 FROM mods")
        min_priority = cursor.fetchone()[0]
        self._db.connection.execute(
            "UPDATE mods SET priority = ? WHERE id = ?", (min_priority, mod_id))
        self._db.connection.commit()
        logger.info("Set mod %d as winner (priority=%d)", mod_id, min_priority)
