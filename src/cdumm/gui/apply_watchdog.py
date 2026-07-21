"""Watchdog helpers for the apply QProcess.

Apply runs in a subprocess (:func:`cdumm.worker_process._run_apply`)
that streams JSON progress messages over stdout. When the subprocess
stalls — a corrupt mod archive, a silent C-level I/O hang, a deadlock
in one of the phases — the user sees a frozen progress dialog and no
way to escape it. Before v3.1.7 that state persisted indefinitely.

This module carries the pure-logic pieces of the watchdog so they
can be unit-tested without standing up a QTimer or a real QProcess.

The wiring (QTimer, kill, InfoBar) lives in
:func:`cdumm.gui.fluent_window.FluentWindow._run_qprocess`.
"""
from __future__ import annotations


APPLY_STALL_THRESHOLD_S = 180.0
"""Default stall threshold, in seconds.

180s covers the legitimate worst case we've observed: large PAMT
rewrites on very slow drives between progress emits. Anything longer
is almost always a real hang.
"""


def is_game_in_program_files(game_dir) -> bool:
    """Return True iff ``game_dir`` sits under Windows' Program Files
    or Program Files (x86).

    Windows restricts writes to these directories unless the process
    is elevated. CDUMM's mod-apply path does many writes (staging,
    backups, overlay PAZ files) and silent ACL denials are a frequent
    hidden cause of "stuck" reports. Callers surface a persistent
    banner when this returns True.

    Match is case-insensitive and segment-level, so nonsense like
    ``D:\\My Programs\\Files\\...`` does not false-positive.
    """
    if not game_dir:
        return False
    import os
    from pathlib import Path
    # Resolve the path through any junction or symlink first. Bug 2026
    # -05-09 #69 (DemonBigj781): Steam's steamapps under Program Files
    # is often a junction to a user-writable location (e.g.
    # Documents\steamapps), in which case Windows write permissions
    # apply to the *target* dir, not Program Files. We need to
    # check where the data actually lives, not where the path string
    # spells out. Path.resolve(strict=False) follows junctions on
    # Windows; falls back to the original path on resolve errors so
    # nonexistent / mid-construction paths still get a string check.
    try:
        resolved = Path(game_dir).resolve(strict=False)
        path = str(resolved)
    except (OSError, ValueError):
        path = str(game_dir)
    norm = os.path.normpath(path).lower()
    parts = norm.replace("\\", "/").split("/")
    return ("program files" in parts
            or "program files (x86)" in parts)


def is_apply_blocked_by_stale_snapshot(startup_context) -> bool:
    """Return True iff the user needs to Rescan before Apply can run.

    ``startup_context["game_updated"]`` is set by ``main.py`` when
    the game's version fingerprint (exe mtime + size + hash) changed
    since the last snapshot was taken — i.e. Steam auto-updated the
    game, or the user verified files. Running apply in that state is
    the root cause of most "stuck at 2%" reports: every vanilla-
    backup check mismatches, every patch lands on wrong bytes.

    The flag is cleared by ``_on_snapshot_finished`` after a
    successful rescan, so Apply unlocks without an app restart.

    Accepts ``None`` so callers during early startup don't need to
    guard before calling.
    """
    if not startup_context:
        return False
    return bool(startup_context.get("game_updated"))


def is_apply_blocked_by_live_game_change(
    *, current_fingerprint: str | None, snapshot_fingerprint: str | None
) -> bool:
    """Return True iff the *live* game version fingerprint no longer
    matches the one the vanilla snapshot was captured against.

    :func:`is_apply_blocked_by_stale_snapshot` only reads the one-shot
    ``game_updated`` flag that ``main.py`` computes at launch. A Steam
    auto-update (or a file verify) that lands *while CDUMM is already
    open* is therefore invisible to it: the flag stays False, Apply is
    not blocked, and the user patches onto a stale vanilla baseline —
    which is exactly the crash-on-launch reported in GitHub #307
    (game exe modified 18:57, after the 17:51 snapshot, then applied).

    Re-computing the fingerprint at apply time closes that window. The
    caller passes ``current_fingerprint`` from
    :func:`cdumm.engine.version_detector.detect_game_version` (the same
    detector the bug report and the startup path use) and
    ``snapshot_fingerprint`` from the stored ``game_version_fingerprint``
    config value.

    Returns False whenever either fingerprint is unknown, so a detection
    gap can never *block* a legitimate apply (no false positives). The
    missing-snapshot case is owned by the separate rescan-required gate,
    and applying/reverting mods never changes the fingerprint (it is
    derived only from the Steam build id + game exe, never from staged
    PAZ/PAMT files), so a clean apply is never mistaken for an update.
    """
    if not current_fingerprint or not snapshot_fingerprint:
        return False
    return current_fingerprint != snapshot_fingerprint


def is_apply_stalled(*, now: float, last_progress_ts: float,
                     threshold_s: float) -> bool:
    """Return True iff ``now - last_progress_ts`` strictly exceeds
    ``threshold_s``.

    Boundary is strict (``>``) — exactly-at-threshold is NOT stalled,
    so we don't race the next progress emit by a hair.
    """
    return (now - last_progress_ts) > threshold_s


def build_stall_message(*, phase: str, last_progress_msg: str | None,
                        threshold_s: float) -> str:
    """Return a user-facing error string explaining why CDUMM aborted.

    ``last_progress_msg`` is the most recent ``msg`` field from a
    ``{"type": "progress", ...}`` payload. May be None if the
    subprocess died before emitting any progress at all.
    """
    minutes = int(threshold_s // 60) or 1
    if last_progress_msg:
        tail = f"Last step was: {last_progress_msg}"
    else:
        tail = "No progress was reported before the stall."
    return (
        f"{phase.capitalize()} stalled with no progress for "
        f"{minutes} minute(s). This usually means a mod archive is "
        f"corrupt or a game file is locked. CDUMM has stopped the "
        f"operation so you're not stuck waiting.\n\n"
        f"{tail}\n\n"
        "Open the Bug Report panel in the left sidebar and save a "
        "report so the issue can be diagnosed.")
