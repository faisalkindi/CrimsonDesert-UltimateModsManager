"""Semantic differ — compares vanilla record dicts against mod record dicts.

Returns FieldDiff objects for fields whose values changed. This is the
lowest-level component: it knows nothing about N mods, merge strategy,
or persistence. It just answers: "what did this one mod change?"

Design note: we deliberately do NOT use deepdiff because:
  - We need custom float epsilon logic (matching game engine behavior)
  - We need a specific output format (FieldDiff)
  - The comparison rules are simple enough in ~60 lines
"""
from __future__ import annotations

import math
from typing import Any

from cdumm.semantic.changeset import (
    FieldDiff,
    ModFieldValue,
    field_display_name,
    format_value_display,
)

_FLOAT_EPSILON = 1e-6


def values_equal(a: Any, b: Any) -> bool:
    """Deep equality with float-epsilon tolerance.

    Handles nested dicts, lists, None, bool, int, float, str, bytes.
    NaN == NaN returns False (IEEE 754 standard).
    """
    if a is b:
        return True
    if type(a) is not type(b):
        # Allow int/float cross-comparison
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            fa, fb = float(a), float(b)
            if math.isnan(fa) or math.isnan(fb):
                return False
            return abs(fa - fb) < _FLOAT_EPSILON
        return False

    if isinstance(a, float):
        if math.isnan(a) and math.isnan(b):
            return False  # IEEE 754
        if not math.isfinite(a) or not math.isfinite(b):
            return a == b  # inf == inf, -inf == -inf
        return abs(a - b) < _FLOAT_EPSILON

    if isinstance(a, dict):
        if a.keys() != b.keys():
            return False
        return all(values_equal(a[k], b[k]) for k in a)

    if isinstance(a, (list, tuple)):
        if len(a) != len(b):
            return False
        return all(values_equal(x, y) for x, y in zip(a, b))

    return a == b


def diff_record(vanilla_record: dict[str, Any],
                mod_record: dict[str, Any],
                mod_name: str) -> list[FieldDiff]:
    """Compare two record dicts (vanilla vs one mod) field by field.

    Args:
        vanilla_record: the original game record as a dict
        mod_record: the mod's version of the same record
        mod_name: display name of the mod

    Returns:
        List of FieldDiff, one per field that the mod changed.
        Empty list if mod == vanilla (no changes).

    Notes:
        - Fields present in vanilla but absent in mod are skipped
          (the mod did not touch them).
        - Fields present in mod but absent in vanilla are also skipped
          (new fields added by a mod are handled separately by the merger).
        - Comparison uses values_equal with float-epsilon tolerance.
        - Metadata fields (_key, _name, _entry_id) are skipped.
    """
    diffs: list[FieldDiff] = []
    skip_fields = {"_key", "_name", "_entry_id"}

    for field_name, vanilla_value in vanilla_record.items():
        if field_name in skip_fields:
            continue
        if field_name not in mod_record:
            continue

        mod_value = mod_record[field_name]
        if values_equal(vanilla_value, mod_value):
            continue

        diffs.append(FieldDiff(
            field_name=field_name,
            display_name=field_display_name(field_name),
            vanilla_value=vanilla_value,
            vanilla_display=format_value_display(vanilla_value),
            mod_values=[ModFieldValue.build(mod_name, mod_value)],
        ))

    return diffs


def diff_records_multi(vanilla_record: dict[str, Any],
                       mod_records: dict[str, dict[str, Any]]
                       ) -> list[FieldDiff]:
    """Compare vanilla against multiple mods' versions of the same record.

    Args:
        vanilla_record: the original game record
        mod_records: {mod_name: record_dict} for each mod

    Returns:
        List of FieldDiff with all mods' values collected per field.
        Fields where no mod changed anything are omitted.
    """
    if not mod_records:
        return []

    # Collect all changed fields across all mods
    field_changes: dict[str, list[ModFieldValue]] = {}
    skip_fields = {"_key", "_name", "_entry_id"}

    for mod_name, mod_record in mod_records.items():
        for field_name, vanilla_value in vanilla_record.items():
            if field_name in skip_fields:
                continue
            if field_name not in mod_record:
                continue

            mod_value = mod_record[field_name]
            if values_equal(vanilla_value, mod_value):
                continue

            field_changes.setdefault(field_name, []).append(
                ModFieldValue.build(mod_name, mod_value))

    # Build FieldDiff list
    diffs: list[FieldDiff] = []
    for field_name, mod_values in field_changes.items():
        vanilla_value = vanilla_record.get(field_name)
        diffs.append(FieldDiff(
            field_name=field_name,
            display_name=field_display_name(field_name),
            vanilla_value=vanilla_value,
            vanilla_display=format_value_display(vanilla_value),
            mod_values=mod_values,
        ))

    return diffs
