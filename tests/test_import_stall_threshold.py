"""The import-stall watchdog must allow huge mods to finish.

Bug 2026-05-09 (Ny4tsuru and srimk on Nexus): the 300-second import
stall watchdog introduced in commit 235c742 (first shipped v3.2.8)
kills legitimate huge-mod imports. Ny4tsuru's 50k+ file audio
mod and srimk's "Traduction integrale des voix en Francais" both
hit the kill timer because zip extraction and entry scanning of
that many files takes longer than 5 minutes without emitting a
per-file progress event. Workaround they found: drop back to
v3.2.7 (which had no watchdog) for the install, then upgrade.

This is a regression. v3.2.7 imports never timed out. The watchdog
itself is right (genuinely hung 7z extracts and infinite-loop
scans should die), but its threshold has to be loose enough that
a real-world 50k-file mod can finish.

The fix is purely a constant bump. Pinning at 30 minutes keeps the
watchdog useful (anything legitimately stuck for 30 minutes is
hung, period) and gives huge mods enough headroom.
"""
from __future__ import annotations

from pathlib import Path


def _src() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "gui" / "fluent_window.py"
            ).read_text(encoding="utf-8")


def test_import_stall_threshold_is_at_least_30_minutes():
    """The constant must be at least 1800 seconds (30 minutes).
    Below that, real-world 50k-file mods get killed mid-extract
    on slow drives and the user sees an unrecoverable timeout."""
    src = _src()
    # Match the assignment line and pull the integer literal.
    import re
    m = re.search(r"IMPORT_STALL_THRESHOLD_S\s*=\s*(\d+)", src)
    assert m is not None, (
        "IMPORT_STALL_THRESHOLD_S assignment not found in "
        "fluent_window.py — has the constant been moved or renamed?"
    )
    threshold = int(m.group(1))
    assert threshold >= 1800, (
        f"IMPORT_STALL_THRESHOLD_S = {threshold} kills legitimate "
        f"huge-mod imports (Ny4tsuru's 50k-file audio mod, srimk's "
        f"Traduction Francais on Nexus 2026-05-09). Must be at "
        f"least 1800s (30 minutes)."
    )


def test_import_stall_threshold_is_not_absurdly_large():
    """Sanity: don't let the threshold balloon past 2 hours.
    Anything stuck that long is hung, period — at some point the
    watchdog has to fire to free the user from a wedged worker."""
    src = _src()
    import re
    m = re.search(r"IMPORT_STALL_THRESHOLD_S\s*=\s*(\d+)", src)
    assert m is not None
    threshold = int(m.group(1))
    assert threshold <= 7200, (
        f"IMPORT_STALL_THRESHOLD_S = {threshold} is over 2 hours; "
        f"the watchdog stops being useful past that point. If a "
        f"real-world mod legitimately takes longer than 2h to "
        f"import, the right answer is to add a heartbeat from the "
        f"worker, not to disable the safety net entirely."
    )
