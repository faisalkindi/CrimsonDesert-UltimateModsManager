"""Export/import mod list as shareable JSON."""
import json
import logging
from datetime import datetime
from pathlib import Path

from cdumm.storage.database import Database

logger = logging.getLogger(__name__)


def export_mod_list(db: Database, path: Path) -> int:
    """Export the current mod list to a JSON file. Returns mod count."""
    from cdumm import __version__
    cursor = db.connection.execute(
        "SELECT name, mod_type, enabled, priority, author, version, description, source_path "
        "FROM mods ORDER BY priority"
    )
    mods = []
    for row in cursor.fetchall():
        mods.append({
            "name": row[0],
            "mod_type": row[1],
            "enabled": bool(row[2]),
            "priority": row[3],
            "author": row[4],
            "version": row[5],
            "description": row[6],
            "source_path": row[7],
        })

    data = {
        "cdumm_version": __version__,
        "exported_at": datetime.now().isoformat(),
        "mod_count": len(mods),
        "mods": mods,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("Exported %d mods to %s", len(mods), path)
    return len(mods)


def import_mod_list(path: Path) -> list[dict]:
    """Read a mod list JSON file. Returns list of mod dicts."""
    data = json.loads(path.read_text(encoding="utf-8"))
    mods = data.get("mods", [])
    logger.info("Imported mod list: %d mods from %s (CDUMM %s)",
                len(mods), path, data.get("cdumm_version", "?"))
    return mods
