"""Temp-dir lifecycle for CDUMM extraction workspaces.

Every CDUMM workflow that pulls files out of an archive (variant
scans, configurable_scanner cog extracts, preset picker, batch ASI
import, mod swap staging) uses tempfile.mkdtemp under %TEMP%. These
never got cleaned up — %TEMP% accumulates indefinitely.

This module centralises temp creation so that:

* Every dir has a predictable `cdumm_*` prefix (already true in the
  call sites, enforced here).
* atexit removes directories created this session.
* A startup sweep removes stale directories from prior runs that
  crashed or were force-killed.

Call from app startup:
    from cdumm.engine.temp_workspace import sweep_stale
    sweep_stale(max_age_hours=48)
"""
from __future__ import annotations

import atexit
import logging
import shutil
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

CDUMM_PREFIXES: tuple[str, ...] = (
    "cdumm_swap_",
    "cdumm_preset_",
    "cdumm_cog_",
    "cdumm_variant_",
    "cdumm_asi_",
    "cdumm_batch_asi_",
    "cdumm_mod_",
    "cdumm_extract_",
)

_active: set[Path] = set()
_atexit_registered = False


def make_temp_dir(prefix: str) -> Path:
    """Create a tracked temp directory. `prefix` must begin with `cdumm_`."""
    if not prefix.startswith("cdumm_"):
        raise ValueError(f"temp dir prefix must start with 'cdumm_', got {prefix!r}")
    _ensure_atexit()
    p = Path(tempfile.mkdtemp(prefix=prefix))
    _active.add(p)
    return p


def release_temp_dir(path: Path | str) -> None:
    """Remove a temp directory and drop it from the cleanup set."""
    p = Path(path)
    try:
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
    finally:
        _active.discard(p)


def sweep_stale(max_age_hours: int = 48) -> int:
    """Remove stale `cdumm_*` temp directories from prior app runs.

    Only considers directories whose name starts with a known CDUMM
    prefix AND whose mtime is older than `max_age_hours`. Returns the
    count removed.
    """
    tmp_root = Path(tempfile.gettempdir())
    if not tmp_root.is_dir():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    try:
        entries = list(tmp_root.iterdir())
    except OSError as e:
        logger.warning("temp_workspace sweep: can't list %s (%s)", tmp_root, e)
        return 0
    for entry in entries:
        if not entry.is_dir():
            continue
        if not any(entry.name.startswith(pfx) for pfx in CDUMM_PREFIXES):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            try:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
            except Exception as e:  # pragma: no cover
                logger.debug("temp_workspace sweep: skip %s (%s)", entry, e)
    if removed:
        logger.info("temp_workspace: swept %d stale CDUMM temp dirs", removed)
    return removed


def _ensure_atexit() -> None:
    global _atexit_registered
    if _atexit_registered:
        return
    atexit.register(_cleanup_all)
    _atexit_registered = True


def _cleanup_all() -> None:
    for p in list(_active):
        try:
            shutil.rmtree(p, ignore_errors=True)
        except Exception:  # pragma: no cover
            pass
    _active.clear()
