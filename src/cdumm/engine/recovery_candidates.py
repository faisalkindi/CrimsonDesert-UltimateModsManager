"""Recovery-flow helpers — who can be reimported, who must be disabled.

v3.1.9 plan, Task 1. Two pure-logic functions backing the Game Update
Recovery flow. Pulled out of the orchestrator so they are testable
without Qt and without the full import pipeline.

Addresses Codex review findings 1-3:
  1. Skipped reimports stay enabled → Apply runs with stale deltas.
     Fix: orchestrator calls ``disable_mods`` on the skipped set BEFORE
     running Apply.
  2. ``enabled=1 AND source_path IS NOT NULL`` is the wrong
     reimportability predicate. It ignores both disk existence and the
     ``CDMods/sources/<mod_id>/`` fallback. Fix: partition via
     :func:`cdumm.engine.mod_source_path.resolve_mod_source_path`, which
     already encodes the correct resolution rules.
  3. "zero reimportable mods" does not equal "zero enabled PAZ mods".
     Fix: ``reimport_candidates`` returns ``(reimportable, skipped)``
     so callers can distinguish "nothing to do, done" from "everything
     was broken, enter all_skipped terminal state".
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from cdumm.engine.mod_source_path import resolve_mod_source_path

logger = logging.getLogger(__name__)


def reimport_candidates(db, game_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition every enabled PAZ mod into (reimportable, skipped).

    Reimportable: ``resolve_mod_source_path`` returns an existing Path.
    Skipped: the resolver returns ``None`` — both the stored
    ``source_path`` and the ``CDMods/sources/<id>/`` fallback are gone.

    Disabled mods and ASI plugins are excluded from both lists.

    Logs a diagnostic line per skipped mod so a 'Recovery halted -- no
    reimportable mods' false positive can be traced post-mortem from
    cdumm.log instead of guessing at why the resolver said None.
    """
    rows = db.connection.execute(
        "SELECT id, name, source_path FROM mods "
        "WHERE enabled = 1 AND mod_type = 'paz' "
        "ORDER BY id"
    ).fetchall()

    reimportable: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in rows:
        mod = {"id": row[0], "name": row[1], "source_path": row[2]}
        resolved = resolve_mod_source_path(mod, game_dir)
        if resolved is not None:
            reimportable.append(mod)
        else:
            skipped.append(mod)
            # Diagnostic: WHY did this skip? source_path missing on
            # disk? Fallback dir missing? Both? Recovery flow's
            # 'all_skipped' terminal state is destructive (disables
            # the mods) so we want hard evidence per row.
            sp = row[2]
            sp_exists = bool(sp) and Path(sp).exists()
            fallback = (Path(game_dir) / "CDMods" / "sources" / str(row[0])
                        if game_dir is not None else None)
            fb_exists = (fallback is not None and fallback.exists()
                         and fallback.is_dir())
            logger.info(
                "reimport_candidates: SKIP id=%d name=%r "
                "source_path=%r sp_exists=%s fallback=%s fb_exists=%s",
                row[0], row[1], sp, sp_exists,
                str(fallback) if fallback else None, fb_exists)
    logger.info(
        "reimport_candidates: %d enabled PAZ rows -> %d reimportable, "
        "%d skipped (game_dir=%r)",
        len(rows), len(reimportable), len(skipped), str(game_dir))
    return reimportable, skipped


def disable_mods(db, mod_ids: list[int]) -> None:
    """Flip ``enabled = 0`` on every mod in ``mod_ids`` and commit.

    Called by the orchestrator after reimport completes, on the
    ``skipped`` set. This is what prevents Apply from touching stale
    deltas for mods whose source files are gone.
    """
    if not mod_ids:
        return
    placeholders = ",".join("?" * len(mod_ids))
    db.connection.execute(
        f"UPDATE mods SET enabled = 0 WHERE id IN ({placeholders})",
        mod_ids)
    db.connection.commit()
