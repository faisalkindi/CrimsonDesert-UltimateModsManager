"""Resolve which PAZ archives enabled JSON mods target.

Used by :func:`cdumm.gui.fluent_window.CdummWindow._refresh_vanilla_backups`
to always keep a clean vanilla copy of the archives that mount-time JSON
patching will extract from, even when those archives exceed the default
size threshold. Without this, large archives like ``0008/0.paz`` (~100MB)
never get backed up and JSON mods targeting them silently no-op at
apply-time.

JSON mods are stored with ``mods.mod_type = 'paz'`` but have a non-null
``mods.json_source``. Each patched archive is recorded in
``mod_deltas.file_path`` in the canonical ``"NNNN/N.paz"`` form, so the
scanner is a single SQL query — no PAMT lookup needed.
"""
from __future__ import annotations

from cdumm.storage.database import Database


def enabled_json_target_archives(db: Database) -> set[str]:
    """Return the set of ``"NNNN/N.paz"`` paths enabled JSON mods patch.

    Ignores disabled mods, non-JSON paz mods (zip/folder imports), and
    empty ``file_path`` rows (edge-case from older imports).
    """
    rows = db.connection.execute(
        "SELECT DISTINCT d.file_path "
        "FROM mod_deltas d JOIN mods m ON d.mod_id = m.id "
        "WHERE m.enabled = 1 "
        "AND m.json_source IS NOT NULL "
        "AND m.json_source != '' "
        "AND d.file_path IS NOT NULL "
        "AND d.file_path != ''"
    ).fetchall()
    return {row[0].replace("\\", "/") for row in rows}
