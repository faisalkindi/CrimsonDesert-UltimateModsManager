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
    """Return True if a JSON patch has genuinely configurable options.

    Delegates to `preset_picker.has_labeled_changes` so the scanner's
    verdict matches what the import dialog and the cog-side panel use.
    Without this, the scanner flags mods configurable when they only
    have labels describing a single feature (like `[FASTER VANILLA] Trim
    starting animations` + `[FASTER VANILLA] Trim ending animations` —
    two parts of one behavior, not two toggles).
    """
    if not isinstance(data, dict):
        return False
    try:
        from cdumm.gui.preset_picker import has_labeled_changes
        return has_labeled_changes(data)
    except Exception:
        return False


def _has_folder_variants(folder: Path) -> bool:
    """Return True when ``folder`` contains 2+ mutually-exclusive
    variant subfolders, matching what ``preset_picker.find_folder_variants``
    detects on import. Used so the scanner keeps the cog flag set on
    XML-only mods like Vaxis LoD after a restart.
    """
    if not folder.is_dir():
        return False
    try:
        from cdumm.gui.preset_picker import find_folder_variants
        return len(find_folder_variants(folder)) >= 2
    except Exception:
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


def _dir_has_rescue_markers(d: Path) -> bool:
    """Return True when ``d`` already holds the full archive contents
    the scanner would extract (2+ JSON presets OR 2+ folder variants).

    Used to decide whether to trust an existing rescue dir or wipe and
    re-extract. The previous heuristic ``dest_dir.exists()`` reused
    partially-populated dirs left by the import worker (which copies
    only the USER-CHOSEN variant, not the full archive). That caused
    folder-variant mods like Vaxis LoD to lose their cog on restart —
    the scanner saw only one variant folder, ``_has_folder_variants``
    returned False, and configurable got cleared to 0.
    """
    if _find_json_presets_dir(d) > 1:
        return True
    if _has_folder_variants(d):
        return True
    return False


def _rescue_archive(archive: Path, dest_dir: Path) -> Path | None:
    """Extract a ZIP/7z/RAR archive into ``dest_dir`` for later preset lookup.

    Returns the extracted directory on success, or None if extraction
    failed / the format is unsupported. If ``dest_dir`` exists but
    lacks the expected markers (2+ presets or variants), it gets wiped
    and re-extracted so stale import_handler output doesn't mask the
    archive's real contents.
    """
    suffix = archive.suffix.lower()
    if suffix not in ARCHIVE_SUFFIXES:
        return None
    if dest_dir.exists():
        if _dir_has_rescue_markers(dest_dir):
            # Full archive contents already on disk — reuse.
            return dest_dir
        # Partial/stale contents (import_handler copied one variant
        # only, or a prior rescue crashed mid-extract). Wipe and
        # re-extract so the scanner sees the real archive layout.
        logger.info(
            "rescue: dest %s lacks variant/preset markers, re-extracting",
            dest_dir)
        shutil.rmtree(dest_dir, ignore_errors=True)
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

        logger.info(
            "[scan] mod_id=%s configurable_before=%s json_source=%r source_path=%r",
            mod_id, configurable, json_source, source_path)

        # Type A — labeled changes in the stored JSON source
        if json_source:
            try:
                jp = Path(json_source)
                if jp.exists() and jp.suffix.lower() == ".json":
                    data = _try_load_json(jp)
                    if data and _has_labeled_changes(data):
                        needs_flag = True
                        stats["flagged_a"] += 1
                        logger.info(
                            "[scan] mod_id=%s Type A HIT (labeled changes)",
                            mod_id)
            except Exception as _e:
                stats["errors"] += 1
                logger.warning(
                    "[scan] mod_id=%s Type A threw: %s", mod_id, _e)

        # Type B — multiple preset JSONs OR multiple folder variants
        # in the archived source folder. Folder variants cover XML-only
        # archives like Vaxis LoD where the author ships mutually-
        # exclusive NNNN subfolders; the cog uses them to swap variants
        # post-install.
        if source_path:
            try:
                sp = Path(source_path)
                if not sp.exists():
                    logger.info(
                        "[scan] mod_id=%s source_path does not exist on disk",
                        mod_id)
                else:
                    if sp.is_dir():
                        has_jsons = _find_json_presets_dir(sp) > 1
                        has_folders = _has_folder_variants(sp)
                        logger.info(
                            "[scan] mod_id=%s source is DIR jsons>1=%s "
                            "folder_variants=%s",
                            mod_id, has_jsons, has_folders)
                        if has_jsons or has_folders:
                            needs_flag = True
                            stats["flagged_b"] += 1
                    elif sp.is_file() and sp.suffix.lower() in ARCHIVE_SUFFIXES:
                        logger.info(
                            "[scan] mod_id=%s source is ARCHIVE, rescuing to %s",
                            mod_id, sources_root / str(mod_id))
                        rescued = _rescue_archive(
                            sp, sources_root / str(mod_id))
                        if rescued is None:
                            logger.warning(
                                "[scan] mod_id=%s rescue FAILED (returned None)",
                                mod_id)
                            stats["errors"] += 1
                        else:
                            has_jsons = _find_json_presets_dir(rescued) > 1
                            has_folders = _has_folder_variants(rescued)
                            logger.info(
                                "[scan] mod_id=%s rescued to %s jsons>1=%s "
                                "folder_variants=%s",
                                mod_id, rescued, has_jsons, has_folders)
                            if has_jsons or has_folders:
                                needs_flag = True
                                new_source_path = str(rescued)
                                stats["flagged_b"] += 1
                                stats["rescued"] += 1
                    else:
                        logger.info(
                            "[scan] mod_id=%s source is neither dir nor "
                            "supported archive (suffix=%s)",
                            mod_id, sp.suffix)
            except Exception as _e:
                stats["errors"] += 1
                logger.warning(
                    "[scan] mod_id=%s Type B threw: %s", mod_id, _e)

        logger.info(
            "[scan] mod_id=%s needs_flag=%s -> action=%s",
            mod_id, needs_flag,
            "SET 1" if (needs_flag and not configurable)
            else "CLEAR 0" if (configurable and not needs_flag)
            else "no-op")

        if needs_flag and not configurable:
            db.connection.execute(
                "UPDATE mods SET configurable = 1 WHERE id = ?", (mod_id,))
        elif configurable and not needs_flag:
            # Clean up stale configurable=1 set by the old dumb heuristic
            # (any-label-at-all). The stricter logic now says this mod has
            # nothing to configure, so clear the flag and the cog goes
            # away on the next list refresh.
            db.connection.execute(
                "UPDATE mods SET configurable = 0 WHERE id = ?", (mod_id,))
            stats["unflagged"] = stats.get("unflagged", 0) + 1
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
