"""Data structures for semantic changesets.

These dataclasses represent the output of semantic diffing and merging:
field-level diffs, record changesets, and merge results with conflict info.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def field_display_name(name: str) -> str:
    """Convert internal field name to human-readable display name.

    '_maxSlot' → 'Max Slot', '_cooltime' → 'Cooltime'
    """
    clean = name.lstrip("_")
    # Insert spaces before uppercase letters
    result = []
    for i, c in enumerate(clean):
        if c.isupper() and i > 0 and clean[i - 1].islower():
            result.append(" ")
        result.append(c)
    return "".join(result).replace("_", " ").strip().title()


def format_value_display(value: Any) -> str:
    """Format a field value for display."""
    if isinstance(value, float):
        import math
        if not math.isfinite(value):
            return str(value)  # "nan", "inf", "-inf"
        if value == int(value):
            return str(int(value))
        return f"{value:.4f}"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if value is None:
        return "(none)"
    return str(value)


@dataclass
class ModFieldValue:
    """A single mod's proposed value for a field."""
    mod_name: str
    value: Any
    display: str

    @classmethod
    def build(cls, mod_name: str, value: Any) -> ModFieldValue:
        return cls(mod_name=mod_name, value=value,
                   display=format_value_display(value))


@dataclass
class FieldDiff:
    """Difference in a single field between vanilla and one or more mods."""
    field_name: str
    display_name: str
    vanilla_value: Any
    vanilla_display: str
    mod_values: list[ModFieldValue]
    resolved: str | None = None  # winning mod name, None = unresolved

    @property
    def is_conflict(self) -> bool:
        """True if 2+ mods propose different values for this field."""
        if len(self.mod_values) < 2:
            return False
        first = self.mod_values[0].value
        from cdumm.semantic.differ import values_equal
        return any(not values_equal(mv.value, first) for mv in self.mod_values[1:])

    @property
    def winner_value(self) -> Any:
        """Get the resolved value, or vanilla for unresolved conflicts."""
        if self.resolved:
            for mv in self.mod_values:
                if mv.mod_name == self.resolved:
                    return mv.value
            # Resolved mod no longer exists — fall back to vanilla
            return self.vanilla_value
        # No conflict and mod(s) agree — use the mod value
        if not self.is_conflict and self.mod_values:
            return self.mod_values[0].value
        # Unresolved conflict — safe default is vanilla
        return self.vanilla_value


@dataclass
class RecordChangeset:
    """Changes to a single record (identified by key)."""
    record_key: int
    item_name: str
    string_key: str  # human-readable identifier (e.g., "DropSet_Faction_Graymane")
    field_diffs: list[FieldDiff] = field(default_factory=list)

    @property
    def conflict_count(self) -> int:
        return sum(1 for d in self.field_diffs if d.is_conflict)

    @property
    def auto_resolved_count(self) -> int:
        return sum(1 for d in self.field_diffs
                   if not d.is_conflict and d.resolved)


@dataclass
class TableChangeset:
    """All changes to a game data table across all mods."""
    table_name: str
    records: list[RecordChangeset] = field(default_factory=list)

    @property
    def total_fields_changed(self) -> int:
        return sum(len(r.field_diffs) for r in self.records)

    @property
    def total_conflicts(self) -> int:
        return sum(r.conflict_count for r in self.records)

    @property
    def conflicts(self) -> list[FieldDiff]:
        result = []
        for r in self.records:
            result.extend(d for d in r.field_diffs if d.is_conflict)
        return result


@dataclass
class SemanticConflict:
    """Flattened view of a single unresolved conflict for UI display."""
    table_name: str
    record_key: int
    item_name: str
    field_name: str
    display_name: str
    vanilla_display: str
    mod_values: list[ModFieldValue]


@dataclass
class SemanticMergeResult:
    """Complete result of a semantic merge operation."""
    table_changeset: TableChangeset
    conflicts: list[SemanticConflict] = field(default_factory=list)

    @property
    def has_conflicts(self) -> bool:
        return len(self.conflicts) > 0

    @property
    def summary(self) -> str:
        tc = self.table_changeset
        parts = [f"{tc.table_name}: {tc.total_fields_changed} field(s) changed"]
        if tc.total_conflicts:
            parts.append(f"{tc.total_conflicts} conflict(s)")
        else:
            parts.append("no conflicts")
        return ", ".join(parts)
