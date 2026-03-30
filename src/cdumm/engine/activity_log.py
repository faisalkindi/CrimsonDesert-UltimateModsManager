"""Persistent activity log for CDUMM.

Records every action that modifies game files, organized by session.
Stored in SQLite for persistence across launches.
"""

import logging
from datetime import datetime
from pathlib import Path

from cdumm.storage.database import Database

logger = logging.getLogger(__name__)

# Action categories with display colors
CATEGORY_COLORS = {
    "apply":    "#A3BE8C",  # green — mods applied to game
    "revert":   "#81A1C1",  # blue — files restored to vanilla
    "import":   "#D4A43C",  # gold — mod imported
    "remove":   "#BF616A",  # red — mod removed
    "snapshot": "#B48EAD",  # purple — snapshot taken
    "verify":   "#88C0D0",  # cyan — verification ran
    "cleanup":  "#D08770",  # orange — cleanup/maintenance
    "warning":  "#EBCB8B",  # yellow — something unexpected
    "error":    "#BF616A",  # red — error occurred
}


class ActivityLog:
    """Records and retrieves activity log entries."""

    def __init__(self, db: Database):
        self._db = db
        self._ensure_table()
        self._session_id = self._start_session()

    def _ensure_table(self) -> None:
        self._db.connection.executescript("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                timestamp TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                category TEXT NOT NULL,
                message TEXT NOT NULL,
                detail TEXT
            );
            CREATE TABLE IF NOT EXISTS activity_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                app_version TEXT
            );
        """)
        self._db.connection.commit()

    def _start_session(self) -> int:
        from cdumm import __version__
        cursor = self._db.connection.execute(
            "INSERT INTO activity_sessions (app_version) VALUES (?)",
            (__version__,))
        self._db.connection.commit()
        return cursor.lastrowid

    def log(self, category: str, message: str, detail: str = None) -> None:
        """Record an activity log entry."""
        self._db.connection.execute(
            "INSERT INTO activity_log (session_id, category, message, detail) "
            "VALUES (?, ?, ?, ?)",
            (self._session_id, category, message, detail))
        self._db.connection.commit()
        logger.info("[%s] %s%s", category, message,
                    f" — {detail}" if detail else "")

    def get_sessions(self, limit: int = 20) -> list[dict]:
        """Get recent sessions with their entry counts."""
        rows = self._db.connection.execute("""
            SELECT s.id, s.started_at, s.app_version,
                   COUNT(a.id) as entry_count
            FROM activity_sessions s
            LEFT JOIN activity_log a ON a.session_id = s.id
            GROUP BY s.id
            ORDER BY s.id DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [{"id": r[0], "started_at": r[1], "version": r[2],
                 "count": r[3]} for r in rows]

    def get_entries(self, session_id: int = None) -> list[dict]:
        """Get log entries, optionally filtered by session."""
        if session_id:
            rows = self._db.connection.execute(
                "SELECT timestamp, category, message, detail "
                "FROM activity_log WHERE session_id = ? ORDER BY id",
                (session_id,)).fetchall()
        else:
            rows = self._db.connection.execute(
                "SELECT timestamp, category, message, detail "
                "FROM activity_log ORDER BY id DESC LIMIT 500").fetchall()
        return [{"timestamp": r[0], "category": r[1], "message": r[2],
                 "detail": r[3]} for r in rows]

    def search(self, query: str) -> list[dict]:
        """Search log entries by message or detail text."""
        rows = self._db.connection.execute(
            "SELECT a.timestamp, a.category, a.message, a.detail, "
            "       s.started_at as session_start "
            "FROM activity_log a JOIN activity_sessions s ON a.session_id = s.id "
            "WHERE a.message LIKE ? OR a.detail LIKE ? "
            "ORDER BY a.id DESC LIMIT 200",
            (f"%{query}%", f"%{query}%")).fetchall()
        return [{"timestamp": r[0], "category": r[1], "message": r[2],
                 "detail": r[3], "session": r[4]} for r in rows]
