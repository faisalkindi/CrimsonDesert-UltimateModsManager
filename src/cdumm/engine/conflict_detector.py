"""Three-level conflict detection engine.

Levels:
  1. PAPGT (metadata) — two mods modify PAMT in different directories → auto-handled
  2. PAZ (archive) — two mods modify the same PAZ at different byte ranges → warning
  3. Byte-range (data) — two mods modify overlapping byte ranges → conflict
"""
import logging
from dataclasses import dataclass

from cdumm.archive.format_parsers import identify_records_for_file
from cdumm.storage.database import Database

logger = logging.getLogger(__name__)


@dataclass
class Conflict:
    mod_a_id: int
    mod_a_name: str
    mod_b_id: int
    mod_b_name: str
    file_path: str
    level: str  # "papgt", "paz", "byte_range"
    byte_start: int | None
    byte_end: int | None
    explanation: str
    winner_id: int | None = None
    winner_name: str | None = None


class ConflictDetector:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _semantic_conflict_info(
        self, entry_path: str,
        mod_a_name: str, mod_b_name: str,
        a_deltas: list[dict], b_deltas: list[dict],
    ) -> str | None:
        """Try to get field-level conflict info using the semantic system.

        Returns a human-readable explanation string, or None if semantic
        analysis is unavailable for this entry.
        """
        try:
            from cdumm.semantic.parser import identify_table_from_path
            table_name = identify_table_from_path(entry_path)
            if not table_name:
                return None

            # Check if delta metadata has semantic_fields_changed
            a_fields = set()
            b_fields = set()
            for d in a_deltas:
                if d.get("entry_path") == entry_path:
                    # Try to read from stored metadata
                    try:
                        from cdumm.engine.delta_engine import load_entry_delta
                        from pathlib import Path
                        _, meta = load_entry_delta(Path(d["delta_path"]))
                        fields = meta.get("semantic_fields_changed", [])
                        a_fields.update(fields)
                    except Exception:
                        pass
            for d in b_deltas:
                if d.get("entry_path") == entry_path:
                    try:
                        from cdumm.engine.delta_engine import load_entry_delta
                        from pathlib import Path
                        _, meta = load_entry_delta(Path(d["delta_path"]))
                        fields = meta.get("semantic_fields_changed", [])
                        b_fields.update(fields)
                    except Exception:
                        pass

            if not a_fields and not b_fields:
                return None

            shared = a_fields & b_fields
            only_a = a_fields - b_fields
            only_b = b_fields - a_fields

            parts = []
            if shared:
                parts.append(f"Both modify: {', '.join(sorted(shared))}")
            if only_a:
                parts.append(f"{mod_a_name} only: {', '.join(sorted(only_a))}")
            if only_b:
                parts.append(f"{mod_b_name} only: {', '.join(sorted(only_b))}")

            if shared:
                return f"{entry_path} — CONFLICT on {', '.join(sorted(shared))}. " + ". ".join(parts)
            else:
                return f"{entry_path} — Compatible (different fields). " + ". ".join(parts)

        except Exception:
            return None

    def detect_all(self) -> list[Conflict]:
        """Run full conflict detection across all enabled mods.

        Returns list of Conflict objects. PAPGT conflicts are informational
        (auto-handled). PAZ and byte-range conflicts require user attention.
        """
        conflicts: list[Conflict] = []

        # Get all enabled mods with their deltas
        enabled_mods = self._get_enabled_mods()
        if len(enabled_mods) < 2:
            return conflicts

        # Compare each pair of mods (cap total conflicts to prevent UI freeze)
        MAX_CONFLICTS = 200
        mod_ids = list(enabled_mods.keys())
        for i in range(len(mod_ids)):
            for j in range(i + 1, len(mod_ids)):
                pair_conflicts = self._compare_mods(
                    mod_ids[i], enabled_mods[mod_ids[i]],
                    mod_ids[j], enabled_mods[mod_ids[j]],
                )
                conflicts.extend(pair_conflicts)
                if len(conflicts) >= MAX_CONFLICTS:
                    break
            if len(conflicts) >= MAX_CONFLICTS:
                break

        # Store conflicts in database
        self._save_conflicts(conflicts)

        return conflicts

    def check_new_mod(self, mod_id: int) -> list[Conflict]:
        """Check a single mod against all other enabled mods."""
        conflicts: list[Conflict] = []

        enabled_mods = self._get_enabled_mods()
        if mod_id not in enabled_mods:
            return conflicts

        new_mod_deltas = enabled_mods[mod_id]
        for other_id, other_deltas in enabled_mods.items():
            if other_id == mod_id:
                continue
            pair_conflicts = self._compare_mods(mod_id, new_mod_deltas, other_id, other_deltas)
            conflicts.extend(pair_conflicts)

        return conflicts

    def _get_enabled_mods(self) -> dict[int, list[dict]]:
        """Get all enabled PAZ mods with their delta byte ranges and priority."""
        cursor = self._db.connection.execute(
            "SELECT m.id, m.name, m.priority, md.file_path, md.byte_start, md.byte_end, "
            "md.entry_path, md.delta_path, m.conflict_mode "
            "FROM mods m JOIN mod_deltas md ON m.id = md.mod_id "
            "WHERE m.enabled = 1 AND m.mod_type = 'paz' "
            "ORDER BY m.priority"
        )
        mods: dict[int, list[dict]] = {}
        for mod_id, mod_name, priority, file_path, byte_start, byte_end, entry_path, delta_path, conflict_mode in cursor.fetchall():
            if mod_id not in mods:
                mods[mod_id] = []
            mods[mod_id].append({
                "name": mod_name,
                "priority": priority,
                "file_path": file_path,
                "byte_start": byte_start,
                "byte_end": byte_end,
                "entry_path": entry_path,
                "delta_path": delta_path,
                "conflict_mode": conflict_mode or "normal",
            })
        return mods

    def _compare_mods(
        self,
        mod_a_id: int, mod_a_deltas: list[dict],
        mod_b_id: int, mod_b_deltas: list[dict],
    ) -> list[Conflict]:
        """Compare two mods for conflicts at all three levels."""
        conflicts: list[Conflict] = []
        mod_a_name = mod_a_deltas[0]["name"] if mod_a_deltas else f"Mod {mod_a_id}"
        mod_b_name = mod_b_deltas[0]["name"] if mod_b_deltas else f"Mod {mod_b_id}"
        mod_a_priority = mod_a_deltas[0].get("priority", 0) if mod_a_deltas else 0
        mod_b_priority = mod_b_deltas[0].get("priority", 0) if mod_b_deltas else 0
        a_override = mod_a_deltas[0].get("conflict_mode") == "override" if mod_a_deltas else False
        b_override = mod_b_deltas[0].get("conflict_mode") == "override" if mod_b_deltas else False

        double_override = a_override and b_override
        if a_override and not b_override:
            winner_id, winner_name = mod_a_id, mod_a_name
        elif b_override and not a_override:
            winner_id, winner_name = mod_b_id, mod_b_name
        elif mod_a_priority <= mod_b_priority:
            # Lower priority number = higher in list = applied last = wins
            winner_id, winner_name = mod_a_id, mod_a_name
        else:
            winner_id, winner_name = mod_b_id, mod_b_name

        # Build winner reason for explanations
        if double_override:
            _winner_reason = f"WARNING: both mods declare override — {winner_name} wins by priority"
        elif a_override or b_override:
            _winner_reason = f"{winner_name} (override mode)"
        else:
            _winner_reason = f"{winner_name} (higher load order)"

        # Group deltas by file
        a_files: dict[str, list[dict]] = {}
        for d in mod_a_deltas:
            a_files.setdefault(d["file_path"], []).append(d)

        b_files: dict[str, list[dict]] = {}
        for d in mod_b_deltas:
            b_files.setdefault(d["file_path"], []).append(d)

        # Find common files
        common_files = set(a_files.keys()) & set(b_files.keys())

        # Check for PAPGT-level: different directories modifying PAMT
        a_dirs = {f.split("/")[0] for f in a_files if "/" in f}
        b_dirs = {f.split("/")[0] for f in b_files if "/" in f}
        a_pamt = any("pamt" in f.lower() for f in a_files)
        b_pamt = any("pamt" in f.lower() for f in b_files)

        if a_pamt and b_pamt and a_dirs != b_dirs:
            conflicts.append(Conflict(
                mod_a_id=mod_a_id, mod_a_name=mod_a_name,
                mod_b_id=mod_b_id, mod_b_name=mod_b_name,
                file_path="meta/0.papgt",
                level="papgt",
                byte_start=None, byte_end=None,
                explanation=(
                    f"{mod_a_name} and {mod_b_name} modify PAMT files in different directories. "
                    "PAPGT will be rebuilt automatically — no action needed."
                ),
            ))

        if not common_files:
            return conflicts

        # For each shared file, check for conflicts
        for file_path in common_files:
            a_deltas = a_files[file_path]
            b_deltas = b_files[file_path]

            # Check if both mods use ENTR deltas for this file — compare at entry level
            a_entries = {d["entry_path"] for d in a_deltas if d.get("entry_path")}
            b_entries = {d["entry_path"] for d in b_deltas if d.get("entry_path")}

            if a_entries and b_entries:
                # Both use ENTR deltas — compare at entry level
                shared_entries = a_entries & b_entries
                if shared_entries:
                    for entry_path in sorted(shared_entries):
                        # Try semantic analysis for richer conflict info
                        sem_explanation = self._semantic_conflict_info(
                            entry_path, mod_a_name, mod_b_name,
                            a_deltas, b_deltas)
                        if sem_explanation:
                            level = "semantic"
                            explanation = sem_explanation
                        else:
                            level = "byte_range"
                            explanation = (
                                f"{mod_a_name} and {mod_b_name} both modify "
                                f"{entry_path} in {file_path}. "
                                f"Winner: {_winner_reason}.")
                        conflicts.append(Conflict(
                            mod_a_id=mod_a_id, mod_a_name=mod_a_name,
                            mod_b_id=mod_b_id, mod_b_name=mod_b_name,
                            file_path=file_path,
                            level=level,
                            byte_start=None, byte_end=None,
                            explanation=explanation,
                            winner_id=winner_id, winner_name=winner_name,
                        ))
                else:
                    # Different entries in the same PAZ — compatible
                    conflicts.append(Conflict(
                        mod_a_id=mod_a_id, mod_a_name=mod_a_name,
                        mod_b_id=mod_b_id, mod_b_name=mod_b_name,
                        file_path=file_path,
                        level="paz",
                        byte_start=None, byte_end=None,
                        explanation=(
                            f"{mod_a_name} and {mod_b_name} both modify {file_path} "
                            "but different game files inside it. Compatible."
                        ),
                    ))
                continue

            a_ranges = [(d["byte_start"], d["byte_end"]) for d in a_deltas
                        if d["byte_start"] is not None]
            b_ranges = [(d["byte_start"], d["byte_end"]) for d in b_deltas
                        if d["byte_start"] is not None]

            if not a_ranges or not b_ranges:
                # No byte-range info — PAZ-level warning
                conflicts.append(Conflict(
                    mod_a_id=mod_a_id, mod_a_name=mod_a_name,
                    mod_b_id=mod_b_id, mod_b_name=mod_b_name,
                    file_path=file_path,
                    level="paz",
                    byte_start=None, byte_end=None,
                    explanation=(
                        f"{mod_a_name} and {mod_b_name} both modify {file_path}. "
                        "They may be compatible if they change different parts of the file."
                    ),
                ))
                continue

            # Too many ranges for O(n²) — report PAZ-level and skip
            MAX_RANGE_PRODUCT = 100_000  # limit O(n*m) comparisons
            if len(a_ranges) * len(b_ranges) > MAX_RANGE_PRODUCT:
                conflicts.append(Conflict(
                    mod_a_id=mod_a_id, mod_a_name=mod_a_name,
                    mod_b_id=mod_b_id, mod_b_name=mod_b_name,
                    file_path=file_path,
                    level="paz",
                    byte_start=None, byte_end=None,
                    explanation=(
                        f"{mod_a_name} and {mod_b_name} both modify {file_path}. "
                        "Too many byte ranges for detailed comparison — "
                        f"winner: {winner_name} (higher load order)."
                    ),
                    winner_id=winner_id, winner_name=winner_name,
                ))
                continue

            # Check for byte-range overlaps
            has_overlap = False
            for a_start, a_end in a_ranges:
                for b_start, b_end in b_ranges:
                    if a_start < b_end and b_start < a_end:
                        # Overlap detected
                        has_overlap = True
                        overlap_start = max(a_start, b_start)
                        overlap_end = min(a_end, b_end)

                        # Try to get record-level explanation
                        record_info = identify_records_for_file(
                            file_path, overlap_start, overlap_end
                        )
                        if record_info:
                            explanation = (
                                f"{mod_a_name} and {mod_b_name} both modify "
                                f"{record_info} in {file_path}. "
                                f"Winner: {_winner_reason}."
                            )
                        else:
                            explanation = (
                                f"{mod_a_name} and {mod_b_name} both modify "
                                f"bytes {overlap_start}-{overlap_end} in {file_path}. "
                                f"Winner: {_winner_reason}."
                            )

                        conflicts.append(Conflict(
                            mod_a_id=mod_a_id, mod_a_name=mod_a_name,
                            mod_b_id=mod_b_id, mod_b_name=mod_b_name,
                            file_path=file_path,
                            level="byte_range",
                            byte_start=overlap_start, byte_end=overlap_end,
                            explanation=explanation,
                            winner_id=winner_id, winner_name=winner_name,
                        ))

            if not has_overlap:
                # Same file, no byte overlap → PAZ-level warning
                conflicts.append(Conflict(
                    mod_a_id=mod_a_id, mod_a_name=mod_a_name,
                    mod_b_id=mod_b_id, mod_b_name=mod_b_name,
                    file_path=file_path,
                    level="paz",
                    byte_start=None, byte_end=None,
                    explanation=(
                        f"{mod_a_name} and {mod_b_name} both modify {file_path} "
                        "but at different byte ranges. Likely compatible."
                    ),
                ))

        return conflicts

    def _save_conflicts(self, conflicts: list[Conflict]) -> None:
        """Store conflicts in database (replaces existing)."""
        self._db.connection.execute("DELETE FROM conflicts")
        for c in conflicts:
            self._db.connection.execute(
                "INSERT INTO conflicts (mod_a_id, mod_b_id, file_path, level, "
                "byte_start, byte_end, explanation, winner_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (c.mod_a_id, c.mod_b_id, c.file_path, c.level,
                 c.byte_start, c.byte_end, c.explanation, c.winner_id),
            )
        self._db.connection.commit()

    def get_mod_status(self, mod_id: int) -> str:
        """Get conflict status for a mod: 'clean', 'warning', 'resolved', or 'outdated'."""
        cursor = self._db.connection.execute(
            "SELECT level, winner_id FROM conflicts "
            "WHERE mod_a_id = ? OR mod_b_id = ?",
            (mod_id, mod_id),
        )
        rows = cursor.fetchall()
        levels = {row[0] for row in rows}
        winners = {row[1] for row in rows if row[1] is not None}

        if "byte_range" in levels:
            # All byte_range conflicts have a winner via load order
            if winners:
                return "resolved"
            return "conflict"
        if "paz" in levels:
            return "clean"  # same file, different byte ranges = compatible
        if "papgt" in levels:
            return "clean"  # PAPGT is auto-handled
        return "clean"

    def get_all_mod_statuses(self) -> dict[int, str]:
        """Batch-compute conflict status for all mods in a single query."""
        cursor = self._db.connection.execute(
            "SELECT mod_a_id, mod_b_id, level, winner_id FROM conflicts")
        mod_levels: dict[int, set[str]] = {}
        mod_has_winner: dict[int, bool] = {}
        for mod_a_id, mod_b_id, level, winner_id in cursor.fetchall():
            for mid in (mod_a_id, mod_b_id):
                mod_levels.setdefault(mid, set()).add(level)
                if winner_id is not None:
                    mod_has_winner[mid] = True

        statuses: dict[int, str] = {}
        for mid, levels in mod_levels.items():
            if "byte_range" in levels:
                statuses[mid] = "resolved" if mod_has_winner.get(mid) else "conflict"
            else:
                statuses[mid] = "clean"
        return statuses

    def get_conflicts_for_mod(self, mod_id: int) -> list[Conflict]:
        """Get all conflicts involving a specific mod."""
        cursor = self._db.connection.execute(
            "SELECT c.mod_a_id, ma.name, c.mod_b_id, mb.name, "
            "c.file_path, c.level, c.byte_start, c.byte_end, c.explanation, "
            "c.winner_id, mw.name "
            "FROM conflicts c "
            "JOIN mods ma ON c.mod_a_id = ma.id "
            "JOIN mods mb ON c.mod_b_id = mb.id "
            "LEFT JOIN mods mw ON c.winner_id = mw.id "
            "WHERE c.mod_a_id = ? OR c.mod_b_id = ?",
            (mod_id, mod_id),
        )
        return [
            Conflict(
                mod_a_id=row[0], mod_a_name=row[1],
                mod_b_id=row[2], mod_b_name=row[3],
                file_path=row[4], level=row[5],
                byte_start=row[6], byte_end=row[7],
                explanation=row[8],
                winner_id=row[9], winner_name=row[10],
            )
            for row in cursor.fetchall()
        ]
