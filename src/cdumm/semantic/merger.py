"""Semantic merger — combines multiple mods' changes with conflict detection.

Takes vanilla records + N mods' records, diffs each mod against vanilla,
then merges the diffs. When two mods change the same field in the same
record, it's a conflict. Single-mod changes are auto-resolved.
"""
from __future__ import annotations

import logging
from typing import Any

from cdumm.semantic.changeset import (
    FieldDiff,
    ModFieldValue,
    RecordChangeset,
    SemanticConflict,
    TableChangeset,
    field_display_name,
    format_value_display,
)
from cdumm.semantic.differ import diff_records_multi

logger = logging.getLogger(__name__)


def merge_table(
    table_name: str,
    vanilla_records: dict[int, dict[str, Any]],
    mod_records: dict[str, dict[int, dict[str, Any]]],
    resolutions: dict[str, str] | None = None,
) -> TableChangeset:
    """Merge multiple mods' changes to a game data table.

    Args:
        table_name: e.g., "inventory", "skill", "iteminfo"
        vanilla_records: {record_key: {field: value}} from vanilla game
        mod_records: {mod_name: {record_key: {field: value}}} per mod
        resolutions: {resolution_key: winning_mod_name} for pre-resolved conflicts
            resolution_key format: "{record_key}:{field_name}"

    Returns:
        TableChangeset with all field diffs and conflict info.
    """
    if resolutions is None:
        resolutions = {}

    changeset = TableChangeset(table_name=table_name)

    # Collect all record keys across vanilla and all mods
    all_keys: set[int] = set(vanilla_records.keys())
    for mod_recs in mod_records.values():
        all_keys.update(mod_recs.keys())

    for record_key in sorted(all_keys):
        vanilla_rec = vanilla_records.get(record_key)
        if vanilla_rec is None:
            continue  # new records added by mods — skip for now

        # Collect mod versions of this record
        mods_for_record: dict[str, dict[str, Any]] = {}
        for mod_name, mod_recs in mod_records.items():
            if record_key in mod_recs:
                mods_for_record[mod_name] = mod_recs[record_key]

        if not mods_for_record:
            continue

        # Diff all mods against vanilla for this record
        field_diffs = diff_records_multi(vanilla_rec, mods_for_record)
        if not field_diffs:
            continue

        # Apply pre-resolved conflicts (validate mod still exists)
        for diff in field_diffs:
            res_key = f"{record_key}:{diff.field_name}"
            if res_key in resolutions:
                winner = resolutions[res_key]
                if any(mv.mod_name == winner for mv in diff.mod_values):
                    diff.resolved = winner
                else:
                    logger.debug("Stale resolution for %s: mod '%s' no longer present",
                                 res_key, winner)

        # Auto-resolve non-conflicting changes
        for diff in field_diffs:
            if not diff.is_conflict and not diff.resolved:
                diff.resolved = diff.mod_values[0].mod_name

        # Build record changeset
        item_name = vanilla_rec.get("_name", "")
        string_key = vanilla_rec.get("_name", str(record_key))

        changeset.records.append(RecordChangeset(
            record_key=record_key,
            item_name=item_name,
            string_key=string_key,
            field_diffs=field_diffs,
        ))

    return changeset


def build_resolved_record(
    vanilla_record: dict[str, Any],
    record_changeset: RecordChangeset,
) -> dict[str, Any]:
    """Apply resolved diffs to a vanilla record to produce the merged output.

    For each field diff:
    - If resolved: use the winning mod's value
    - If unresolved conflict: use vanilla (safe default)
    - If single mod, no conflict: use the mod's value

    Returns a new dict with merged values.
    """
    result = dict(vanilla_record)

    for diff in record_changeset.field_diffs:
        result[diff.field_name] = diff.winner_value

    return result


def extract_flat_conflicts(table_changeset: TableChangeset
                           ) -> list[SemanticConflict]:
    """Extract all unresolved conflicts as flat SemanticConflict objects.

    Used by the UI to display a list of conflicts that need user resolution.
    """
    conflicts: list[SemanticConflict] = []

    for record in table_changeset.records:
        for diff in record.field_diffs:
            if diff.is_conflict and not diff.resolved:
                conflicts.append(SemanticConflict(
                    table_name=table_changeset.table_name,
                    record_key=record.record_key,
                    item_name=record.item_name,
                    field_name=diff.field_name,
                    display_name=diff.display_name,
                    vanilla_display=diff.vanilla_display,
                    mod_values=list(diff.mod_values),
                ))

    return conflicts
