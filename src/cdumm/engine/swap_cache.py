"""Permanent swap cache for configurable-scanner source paths.

Problem: variant-swap fallbacks used to clone sources/<id>/ to %TEMP% via
mkdtemp. The temp path was persisted as mods.source_path. Windows Storage
Sense / Disk Cleanup cleans %TEMP% periodically, silently invalidating the
path and breaking the Configure cog on the next app launch.

Fix: clone under CDMods/sources/_swap_cache/<mod_id>/ which lives on the
user's data drive and isn't touched by OS cleanup jobs.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_ARCHIVE_EXTS = (".rar", ".zip", ".7z")


def cache_root_for(game_dir: Path, mod_id: int) -> Path:
    """Return CDMods/sources/_swap_cache/<mod_id>/ under game_dir."""
    return Path(game_dir) / "CDMods" / "sources" / "_swap_cache" / str(mod_id)


def resolve_cfg_src(
    source_path: str | Path | None,
    sources_dir: Path | None,
    cache_root: Path,
) -> str | None:
    """Pick a stable source_path for the configurable_scanner to re-read.

    Priority:
    1. If source_path is an existing archive file (.rar/.zip/.7z), return
       it verbatim — the scanner rescues variants from the archive on next
       launch.
    2. Otherwise, if sources_dir exists, mirror it to the permanent cache
       root and return the clone path. The clone survives %TEMP% cleanup.
    3. Otherwise return None (caller can fall back to a drop_name).
    """
    if source_path:
        sp = Path(source_path)
        if sp.is_file() and sp.suffix.lower() in _ARCHIVE_EXTS and sp.exists():
            return str(sp)

    if sources_dir and Path(sources_dir).is_dir():
        src = Path(sources_dir)
        dest = Path(cache_root) / src.name
        try:
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dest)
            logger.info(
                "swap_cache: cloned %s -> %s (permanent, survives TEMP cleanup)",
                src, dest,
            )
            return str(dest)
        except Exception as e:  # pragma: no cover
            logger.warning("swap_cache: clone failed (%s -> %s): %s", src, dest, e)

    return None
