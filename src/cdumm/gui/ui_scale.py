"""Interface-zoom scale factor persistence.

Qt only honours ``QT_SCALE_FACTOR`` when it is set *before* the
``QApplication`` object is constructed, which happens before CDUMM's
SQLite config is opened. So the interface-zoom preference is mirrored to
a tiny sidecar file in the app-data dir that ``main`` can read at the
very top of startup without touching the database.

The settings page writes both the SQLite config (for the settings UI to
display) and this sidecar (for the next launch to apply).
"""
from __future__ import annotations

from cdumm.platform import app_data_dir

# Allowed scale factors (strings, to match QT_SCALE_FACTOR's format and
# the settings combo). "1.0" means no scaling.
ALLOWED_SCALES = ("1.0", "1.1", "1.25", "1.5", "1.75", "2.0")
DEFAULT_SCALE = "1.0"


def _scale_path():
    return app_data_dir() / "ui_scale"


def read_ui_scale() -> str:
    """Return the saved UI scale factor, or ``"1.0"`` if unset/invalid.

    Best-effort and dependency-free so it is safe to call before the
    QApplication exists.
    """
    try:
        value = _scale_path().read_text(encoding="utf-8").strip()
    except Exception:
        return DEFAULT_SCALE
    return value if value in ALLOWED_SCALES else DEFAULT_SCALE


def write_ui_scale(factor: str) -> None:
    """Persist the UI scale factor. Invalid values fall back to 1.0.

    Best-effort: a locked or unwritable app-data dir must not raise into
    the settings handler.
    """
    if factor not in ALLOWED_SCALES:
        factor = DEFAULT_SCALE
    try:
        path = _scale_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(factor, encoding="utf-8")
    except Exception:
        pass
