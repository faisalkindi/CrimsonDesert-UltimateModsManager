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
        # Short-circuit when dest already matches src — same file set,
        # same sizes. Destroy-and-recopy on every swap was a perf
        # hit (10s+ GUI stall on multi-GB mods) and the rmtree→copytree
        # pair had no rollback on mid-copy failure (disk-full would
        # leave dest partially populated and the persisted source_path
        # pointing at it). B2.
        if dest.exists() and _manifest_matches(src, dest):
            logger.debug("swap_cache: dest already current, skipping clone")
            return str(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Copy into a temp name first, then atomically rename so a
        # mid-copy failure can't leave `dest` half-populated.
        staging = dest.parent / f".{dest.name}.swap_cache_tmp"
        try:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            shutil.copytree(src, staging)
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            staging.rename(dest)
            logger.info(
                "swap_cache: cloned %s -> %s (permanent, survives TEMP cleanup)",
                src, dest,
            )
            return str(dest)
        except Exception as e:
            logger.warning(
                "swap_cache: clone failed (%s -> %s): %s", src, dest, e)
            # Best-effort cleanup of a half-copy.
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    return None


def _manifest_matches(src: Path, dest: Path) -> bool:
    """Cheap equality check — same relative file set, same file sizes.

    Avoids re-cloning on every swap click when the user just re-opens
    the cog without changing anything. A byte-level hash would be
    more correct but prohibitive for multi-GB mod sources.
    """
    try:
        def _manifest(root: Path) -> dict[str, int]:
            # Files contribute their size; directories contribute -1 so
            # adding an empty dir shows up as a difference.
            m: dict[str, int] = {}
            for p in root.rglob("*"):
                rel = p.relative_to(root).as_posix()
                if p.is_dir():
                    m[rel] = -1
                elif p.is_file():
                    m[rel] = p.stat().st_size
            return m
        return _manifest(src) == _manifest(dest)
    except OSError:
        return False
