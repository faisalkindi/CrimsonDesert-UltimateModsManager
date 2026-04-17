"""Startup scanner that detects configurable mods retroactively.

Mods imported before the configurable-detection logic existed, or through
paths that skipped the check, may have ``configurable = 0`` even though
their JSON source has labels or multiple preset variants.

This scanner walks the ``mods`` table on app startup and flips the flag
for anything that qualifies. It also rescues old batch imports whose
``source_path`` points at the original archive file by extracting it into
``CDMods/sources/<mod_id>/`` (so the config panel can surface the preset
picker from the extracted directory).
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from cdumm.storage.database import Database

logger = logging.getLogger(__name__)


ARCHIVE_SUFFIXES = {".zip", ".7z", ".rar"}


def _try_load_json(path: Path) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _has_labeled_changes(data: dict) -> bool:
    """Return True if a JSON patch contains any labeled byte change (toggles)."""
    if not isinstance(data, dict):
        return False
    for patch in data.get("patches", []):
        if not isinstance(patch, dict):
            continue
        for ch in patch.get("changes", []):
            if isinstance(ch, dict) and ch.get("label"):
                return True
    return False


def _find_json_presets_dir(folder: Path) -> int:
    """Count valid JSON-patch preset files in a folder (depth 1)."""
    if not folder.is_dir():
        return 0
    candidates = sorted(folder.glob("*.json")) or sorted(folder.glob("*/*.json"))
    count = 0
    for f in candidates:
        data = _try_load_json(f)
        if (isinstance(data, dict)
                and "patches" in data
                and isinstance(data["patches"], list)
                and data["patches"]
                and isinstance(data["patches"][0], dict)
                and "game_file" in data["patches"][0]
                and "changes" in data["patches"][0]):
            count += 1
    return count


def _rescue_archive(archive: Path, dest_dir: Path) -> Path | None:
    """Extract a ZIP/7z/RAR archive into ``dest_dir`` for later preset lookup.

    Returns the extracted directory on success, or None if extraction
    failed / the format is unsupported.
    """
    suffix = archive.suffix.lower()
    if suffix not in ARCHIVE_SUFFIXES:
        return None
    if dest_dir.exists():
        # Already rescued previously — reuse it.
        return dest_dir
    try:
        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        if suffix == ".zip":
            import zipfile
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(dest_dir)
        elif suffix == ".7z":
            import py7zr
            with py7zr.SevenZipFile(archive, "r") as zf:
                zf.extractall(dest_dir)
        elif suffix == ".rar":
            import subprocess
            _no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            for tool in ("7z", "7z.exe", r"C:\Program Files\7-Zip\7z.exe"):
                try:
                    proc = subprocess.run(
                        [tool, "x", str(archive), f"-o{dest_dir}", "-y"],
                        capture_output=True, timeout=120,
                        creationflags=_no_window,
                    )
                    if proc.returncode == 0:
                        break
                except FileNotFoundError:
                    continue
            else:
                shutil.rmtree(dest_dir, ignore_errors=True)
                return None
        return dest_dir
    except Exception as e:
        logger.warning("Rescue extraction failed for %s: %s", archive.name, e)
        shutil.rmtree(dest_dir, ignore_errors=True)
        return None


def scan_configurable_mods(db: Database, sources_root: Path) -> dict[str, int]:
    """Flag configurable mods based on their JSON source and archived source.

    Args:
        db: Open Database handle.
        sources_root: ``CDMods/sources/`` — destination for archive rescues.

    Returns a stats dict: keys ``scanned``, ``flagged_a`` (toggles),
    ``flagged_b`` (multi-preset), ``rescued``, ``errors``.
    """
    stats = {"scanned": 0, "flagged_a": 0, "flagged_b": 0,
             "rescued": 0, "errors": 0}

    rows = db.connection.execute(
        "SELECT id, configurable, json_source, source_path, mod_type FROM mods"
    ).fetchall()

    for mod_id, configurable, json_source, source_path, mod_type in rows:
        if mod_type != "paz":
            continue
        stats["scanned"] += 1

        needs_flag = False
        new_source_path: str | None = None

        # Type A — labeled changes in the stored JSON source
        if json_source:
            try:
                jp = Path(json_source)
                if jp.exists() and jp.suffix.lower() == ".json":
                    data = _try_load_json(jp)
                    if data and _has_labeled_changes(data):
                        needs_flag = True
                        stats["flagged_a"] += 1
            except Exception:
                stats["errors"] += 1

        # Type B — multiple preset JSONs in the archived source folder
        if source_path:
            try:
                sp = Path(source_path)
                if sp.exists():
                    if sp.is_dir():
                        if _find_json_presets_dir(sp) > 1:
                            needs_flag = True
                            stats["flagged_b"] += 1
                    elif sp.is_file() and sp.suffix.lower() in ARCHIVE_SUFFIXES:
                        rescued = _rescue_archive(
                            sp, sources_root / str(mod_id))
                        if rescued and _find_json_presets_dir(rescued) > 1:
                            needs_flag = True
                            new_source_path = str(rescued)
                            stats["flagged_b"] += 1
                            stats["rescued"] += 1
                        elif rescued is None:
                            stats["errors"] += 1
            except Exception:
                stats["errors"] += 1

        if needs_flag and not configurable:
            db.connection.execute(
                "UPDATE mods SET configurable = 1 WHERE id = ?", (mod_id,))
        if new_source_path:
            db.connection.execute(
                "UPDATE mods SET source_path = ? WHERE id = ?",
                (new_source_path, mod_id))

    db.connection.commit()
    logger.info(
        "Configurable scan: scanned=%d flagged_a=%d flagged_b=%d rescued=%d errors=%d",
        stats["scanned"], stats["flagged_a"], stats["flagged_b"],
        stats["rescued"], stats["errors"])
    return stats
