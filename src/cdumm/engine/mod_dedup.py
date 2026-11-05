"""Detect and resolve duplicate mod rows.

The single-import path at ``fluent_window._import_with_prechecks``
calls ``_find_existing_mod`` and skips silently when an exact
name+version match is found. The batch import path
(``_launch_batch_import``) historically did NOT call into that flow,
which let users accumulate two rows for every re-imported mod when
they dragged a folder of all their mods back in.

This module supplies:

- :func:`find_duplicate_groups` — pure SQL read; returns groups of
  rows that share a name (the engine's identity key).
- :func:`pick_canonical_row` — applies the keep-rule: prefer the row
  the engine considers ``applied=1``, then ``enabled=1``, then the
  one with the most non-NULL Nexus metadata, then highest priority,
  then most-recent ``import_date``. Deterministic.
- :func:`merge_into_canonical` — copies missing-but-known fields
  (version, drop_name, nexus_real_file_id) from soon-to-be-deleted
  rows into the kept row so the cleanup never *loses* data.
- :func:`cleanup_duplicates` — runs the full plan via the existing
  ``ModManager.remove_mod`` so delta + source folders + cascade rows
  go too. Returns the list of (kept, deleted) tuples for reporting.

All public helpers are unit-testable without Qt.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class _Row:
    id: int
    name: str
    enabled: int
    applied: int
    priority: int
    import_date: str
    version: str | None
    drop_name: str | None
    nexus_mod_id: int | None
    nexus_real_file_id: int | None


_FIELDS = (
    "id, name, enabled, applied, priority, import_date, "
    "version, drop_name, nexus_mod_id, nexus_real_file_id"
)


def _fetch_rows(conn) -> list[_Row]:
    out = []
    for r in conn.execute(f"SELECT {_FIELDS} FROM mods").fetchall():
        out.append(_Row(*r))
    return out


def _file_identity(path: str) -> str:
    """SHA-256 of ``path``'s content when it's readable, else the
    path string itself. The string fallback still lets two distinct
    (if unreadable or not-yet-written) paths read as different rather
    than colliding on a read error."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return path


def _delta_fingerprint(conn, mod_id: int) -> tuple:
    """Content identity for a mod: two rows are true duplicates only
    when the bytes they'd actually apply match, not merely which
    file/offset they target. Position alone is not enough: two
    multiplier variants of the same mod ("Loot 2x" and "Loot 10x")
    routinely patch the identical byte range of the identical file,
    so a position-only fingerprint made them look like duplicates and
    one got silently deleted (#476).

    Mount-time JSON and Format 3 mods store their whole patch in
    ``mods.json_source`` with no delta file on disk (``delta_path``
    is empty by design), so their identity is that file's content.
    Everything else is identified by each delta file's content.
    """
    row = conn.execute(
        "SELECT json_source FROM mods WHERE id = ?", (mod_id,)).fetchone()
    json_source = row[0] if row else None
    if json_source:
        return ("json", _file_identity(json_source))

    rows = conn.execute(
        "SELECT file_path, COALESCE(entry_path, ''), delta_path, is_new "
        "FROM mod_deltas WHERE mod_id = ? "
        "ORDER BY file_path, entry_path",
        (mod_id,)).fetchall()
    return tuple(
        (file_path, entry_path,
         _file_identity(delta_path) if delta_path else "", is_new)
        for file_path, entry_path, delta_path, is_new in rows)


def find_duplicate_groups(conn) -> dict[str, list[_Row]]:
    """Return ``{group_key: [rows]}`` for every TRUE duplicate group.

    Display name alone is NOT identity: prettify_mod_name strips
    versions and trailing multipliers, so "Trust Me 2x" and
    "Trust Me 5x" both land as "Trust Me", and name-only dedup
    deleted one of two intentionally different variants (audit
    finding, 2026-06-10). Rows must share name AND version AND the
    full delta fingerprint to be considered duplicates.
    """
    groups: dict[tuple, list[_Row]] = {}
    by_name: dict[str, list[_Row]] = {}
    for row in _fetch_rows(conn):
        by_name.setdefault(row.name, []).append(row)
    for name, rows in by_name.items():
        if len(rows) < 2:
            continue
        for row in rows:
            key = (name, row.version or "",
                   _delta_fingerprint(conn, row.id))
            groups.setdefault(key, []).append(row)
    # Key must stay unique per (name, version, fingerprint) group:
    # two DIFFERENT fingerprint groups can share name+version, and a
    # display-string key silently dropped one of them per run
    # (release-review minor, 2026-06-11). Append a stable fingerprint
    # tag; callers ignore the key or use it only for logging.
    return {
        f"{key[0]} (v{key[1] or '?'}) [{abs(hash(key[2])) % 0xFFFF:04x}]":
            rows
        for key, rows in groups.items() if len(rows) > 1
    }


def _row_score(row: _Row) -> tuple:
    """Sort key for picking the canonical row in a group.

    Higher tuple = better. The first non-zero comparison wins so the
    rules cascade in priority order:

    1. ``applied=1`` rows beat ``applied=0`` (engine still considers
       the old row's deltas active, so deleting it would leave game
       files in a stale state).
    2. ``enabled=1`` rows beat ``enabled=0``.
    3. More non-NULL Nexus metadata is better (file_id chain walk
       depends on it; we don't want to lose it).
    4. Higher ``priority`` wins (the user's hand-set load order).
    5. Newer ``import_date`` wins (most recent fields).
    """
    nexus_score = (
        (1 if row.nexus_real_file_id else 0)
        + (1 if row.nexus_mod_id else 0)
        + (1 if row.version else 0)
        + (1 if row.drop_name else 0)
    )
    return (
        int(row.applied or 0),
        int(row.enabled or 0),
        nexus_score,
        int(row.priority or 0),
        row.import_date or "",
    )


def pick_canonical_row(rows: list[_Row]) -> _Row:
    """Pick the row to KEEP from a duplicate group."""
    return max(rows, key=_row_score)


def merge_into_canonical(canonical: _Row, others: list[_Row]) -> dict:
    """Fill any NULL field on the canonical row from the soon-to-be-
    deleted siblings. Returns ``{column: new_value}`` for the SQL
    UPDATE so the caller can persist (or test the plan in isolation).
    """
    update: dict = {}
    if not canonical.version:
        for o in others:
            if o.version:
                update["version"] = o.version
                break
    if not canonical.drop_name:
        for o in others:
            if o.drop_name:
                update["drop_name"] = o.drop_name
                break
    if not canonical.nexus_real_file_id:
        for o in others:
            if o.nexus_real_file_id:
                update["nexus_real_file_id"] = int(o.nexus_real_file_id)
                break
    if not canonical.nexus_mod_id:
        for o in others:
            if o.nexus_mod_id:
                update["nexus_mod_id"] = int(o.nexus_mod_id)
                break
    return update


def plan_cleanup(conn) -> list[tuple[_Row, list[_Row], dict]]:
    """Return ``[(canonical, deleted_rows, merge_update_dict), ...]``
    without touching the DB. Useful for dry-run previews and for the
    CLI output."""
    plan = []
    for _name, rows in find_duplicate_groups(conn).items():
        canon = pick_canonical_row(rows)
        deleted = [r for r in rows if r.id != canon.id]
        update = merge_into_canonical(canon, deleted)
        plan.append((canon, deleted, update))
    return plan


def apply_cleanup(mod_manager) -> list[tuple[int, list[int]]]:
    """Execute the dedup plan via ``mod_manager.remove_mod`` so delta
    folders, source folders, and cascade rows are removed in lockstep
    with the DB row.

    Returns ``[(kept_id, [deleted_ids]), ...]``. The caller logs.
    """
    conn = mod_manager._db.connection
    plan = plan_cleanup(conn)
    results: list[tuple[int, list[int]]] = []
    for canon, deleted, update in plan:
        # Persist any merged fields on the canonical row first so the
        # data survives the deletes.
        if update:
            cols = list(update.keys())
            placeholders = ", ".join(f"{c} = ?" for c in cols)
            conn.execute(
                f"UPDATE mods SET {placeholders} WHERE id = ?",
                [*update.values(), canon.id],
            )
            conn.commit()
            logger.info(
                "dedup: filled %s on kept row %d (%s) from siblings",
                ", ".join(cols), canon.id, canon.name,
            )
        for d in deleted:
            try:
                mod_manager.remove_mod(d.id)
            except Exception as e:
                logger.warning(
                    "dedup: failed to remove duplicate row %d (%s): %s",
                    d.id, d.name, e,
                )
        results.append((canon.id, [d.id for d in deleted]))
    return results
