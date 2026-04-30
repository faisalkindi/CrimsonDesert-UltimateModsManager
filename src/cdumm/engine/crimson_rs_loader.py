"""Lazy loader for the vendored crimson_rs Rust extension.

crimson_rs is NattKh's full PABGB parser/serializer for several
tables (iteminfo, skill, etc.) shipped as a compiled .pyd in
`src/cdumm/_vendor/crimson_rs/`. License: MPL-2.0
(see _vendor/crimson_rs/LICENSE_MPL2).

Wrapped here so callers don't have to muck with sys.path. Returns
None if the binary fails to load (older Python, missing VCRedist,
non-Windows host) so callers can fall back gracefully.
"""
from __future__ import annotations
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEV_VENDOR_DIR = Path(__file__).resolve().parent.parent / "_vendor"
_cached_module: Any | None = None
_load_attempted = False


def _candidate_dirs() -> list[Path]:
    """Return possible locations of the vendored crimson_rs package.

    Dev: `<src>/cdumm/_vendor/crimson_rs/` directly.
    Frozen: PyInstaller flattens the binary alongside the wrapper
    files into `<exe>/_internal/cdumm/_vendor/crimson_rs/`. The
    parent dir (`<exe>/_internal/cdumm/_vendor`) needs to be on
    sys.path so `import crimson_rs` resolves the package.
    """
    out: list[Path] = []
    out.append(_DEV_VENDOR_DIR)
    if hasattr(sys, "_MEIPASS"):
        meipass = Path(sys._MEIPASS)
        out.append(meipass / "cdumm" / "_vendor")
        out.append(meipass / "_vendor")
    return out


def get_crimson_rs():
    """Return the crimson_rs module, or None if unavailable.

    Idempotent: caches the result of the first load attempt.
    """
    global _cached_module, _load_attempted
    if _load_attempted:
        return _cached_module
    _load_attempted = True
    for candidate in _candidate_dirs():
        if not candidate.exists():
            continue
        try:
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            import crimson_rs as _mod
            _cached_module = _mod
            logger.info("crimson_rs loaded from %s", candidate)
            return _cached_module
        except Exception as e:
            logger.debug(
                "crimson_rs load attempt at %s failed: %s", candidate, e)
            continue
    logger.warning(
        "crimson_rs vendored extension not loadable; "
        "iteminfo / skill list-of-dict writers will be unavailable")
    return None
