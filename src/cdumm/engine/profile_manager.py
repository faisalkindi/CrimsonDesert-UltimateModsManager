"""Mod profile management — save/restore mod enabled states."""
import logging

from cdumm.storage.database import Database

logger = logging.getLogger(__name__)


class ProfileManager:
    def __init__(self, db: Database) -> None:
        self._db = db

    def list_profiles(self) -> list[dict]:
        cursor = self._db.connection.execute(
            "SELECT id, name, created_at FROM profiles ORDER BY name")
        return [{"id": r[0], "name": r[1], "created_at": r[2]} for r in cursor.fetchall()]

    def save_profile(self, name: str) -> int:
        """Snapshot current mod enabled/priority state. Returns profile id.

        Saving under an existing profile name REPLACES that profile.
        The ``profiles.name`` column is UNIQUE; before this contract
        existed, a duplicate name raised an uncaught
        ``sqlite3.IntegrityError`` straight through the GUI's save
        handler (which treats the name as the profile's identity and
        ignores the returned id). All statements run in one
        transaction: a failure rolls everything back so a previously
        saved profile is never half-deleted.
        """
        conn = self._db.connection
        try:
            row = conn.execute(
                "SELECT id FROM profiles WHERE name = ?", (name,)).fetchone()
            replaced = row is not None
            if replaced:
                # Explicit child delete first: ON DELETE CASCADE covers
                # this when foreign_keys is ON, but don't rely on the
                # pragma being set on every connection.
                conn.execute(
                    "DELETE FROM profile_mods WHERE profile_id = ?",
                    (row[0],))
                conn.execute(
                    "DELETE FROM profiles WHERE id = ?", (row[0],))
            cursor = conn.execute(
                "INSERT INTO profiles (name) VALUES (?)", (name,))
            profile_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO profile_mods (profile_id, mod_id, enabled, priority) "
                "SELECT ?, id, enabled, priority FROM mods",
                (profile_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        logger.info("Saved profile '%s' (id=%d)%s", name, profile_id,
                    " replacing previous profile with same name"
                    if replaced else "")
        return profile_id

    def load_profile(self, profile_id: int) -> int:
        """Restore mod states from a profile. Returns number of mods updated."""
        rows = self._db.connection.execute(
            "SELECT mod_id, enabled, priority FROM profile_mods WHERE profile_id = ?",
            (profile_id,)).fetchall()
        for mod_id, enabled, priority in rows:
            self._db.connection.execute(
                "UPDATE mods SET enabled = ?, priority = ? WHERE id = ?",
                (enabled, priority, mod_id))
        self._db.connection.commit()
        logger.info("Loaded profile id=%d (%d mods)", profile_id, len(rows))
        return len(rows)

    def delete_profile(self, profile_id: int) -> None:
        self._db.connection.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        self._db.connection.commit()

    def rename_profile(self, profile_id: int, name: str) -> None:
        self._db.connection.execute(
            "UPDATE profiles SET name = ? WHERE id = ?", (name, profile_id))
        self._db.connection.commit()

    def get_profile_mods(self, profile_id: int) -> list[dict]:
        """Get mod states in a profile for preview."""
        cursor = self._db.connection.execute(
            "SELECT pm.mod_id, m.name, pm.enabled, pm.priority "
            "FROM profile_mods pm JOIN mods m ON pm.mod_id = m.id "
            "WHERE pm.profile_id = ? ORDER BY pm.priority",
            (profile_id,))
        return [{"mod_id": r[0], "name": r[1], "enabled": bool(r[2]), "priority": r[3]}
                for r in cursor.fetchall()]
