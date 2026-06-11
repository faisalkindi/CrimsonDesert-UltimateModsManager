"""Test Mod mode: conflict analysis without permanently installing.

Imports a mod temporarily, runs full conflict detection against all
installed mods, generates an exportable compatibility report, then
removes exactly the rows the trial import created.

Renamed from ``test_mod_checker.py`` (audit finding C2, 2026-06-10):
the old name matched pytest's ``test_*.py`` collection pattern and
its ``test_mod()`` function would be collected as a test with
unresolvable fixtures the moment anyone ran ``pytest src``. Worse,
its failure-path cleanup deleted ``MAX(id)`` from the mods table
unconditionally; many import failures never insert a row, so the
user's newest REAL mod (and, via ON DELETE CASCADE, all its deltas)
was destroyed by a feature documented as read-only. Cleanup is now
watermark-based: only rows created after the trial import started
are ever deleted.
"""
import logging
from datetime import datetime
from pathlib import Path

from cdumm.engine.conflict_detector import Conflict, ConflictDetector
from cdumm.engine.import_handler import detect_format, import_from_folder, import_from_zip
from cdumm.engine.snapshot_manager import SnapshotManager
from cdumm.storage.database import Database

logger = logging.getLogger(__name__)


class ModTestResult:
    def __init__(self, mod_name: str) -> None:
        self.mod_name = mod_name
        self.changed_files: list[dict] = []
        self.conflicts: list[Conflict] = []
        self.compatible_mods: list[str] = []
        self.error: str | None = None


def analyze_mod(
    mod_path: Path, game_dir: Path, db: Database, snapshot: SnapshotManager
) -> ModTestResult:
    """Analyze a mod without installing it permanently.

    Temporarily imports the mod into the real database, runs conflict
    detection, then removes only the rows the trial import created
    (watermark on the pre-import MAX(id))."""
    fmt = detect_format(mod_path)
    mod_name = mod_path.stem
    result = ModTestResult(mod_name)

    # Watermark BEFORE the trial import: every row above this id was
    # created by the trial and is safe to delete; nothing at or below
    # it is ever touched.
    row = db.connection.execute("SELECT MAX(id) FROM mods").fetchone()
    pre_import_max = row[0] if row and row[0] is not None else 0

    # R2 follow-up to Bug E: route staging under game_dir/CDMods so
    # large mods being analyzed don't fill C:/ when %TEMP% is on a
    # different drive than the game install.
    from cdumm.engine.import_handler import import_staging_dir
    with import_staging_dir(game_dir) as tmp:
        tmp_path = Path(tmp)
        deltas_dir = tmp_path / "deltas"

        # Import to get change analysis (uses the REAL database temporarily)
        if fmt == "zip":
            import_result = import_from_zip(mod_path, game_dir, db, snapshot, deltas_dir)
        elif fmt == "folder":
            import_result = import_from_folder(mod_path, game_dir, db, snapshot, deltas_dir)
        else:
            result.error = f"Test Mod only supports zip and folder formats (got: {fmt})"
            return result

        if import_result.error:
            result.error = import_result.error
            _cleanup_trial_rows(db, pre_import_max)
            return result

        try:
            result.changed_files = import_result.changed_files

            # Run conflict detection
            detector = ConflictDetector(db)
            all_conflicts = detector.detect_all()

            # The trial import's rows are exactly those above the
            # watermark.
            trial_ids = {
                r[0] for r in db.connection.execute(
                    "SELECT id FROM mods WHERE id > ?", (pre_import_max,)
                ).fetchall()
            }

            # Filter conflicts involving the test mod
            for c in all_conflicts:
                if c.mod_a_id in trial_ids or c.mod_b_id in trial_ids:
                    result.conflicts.append(c)

            # Determine compatible mods (no conflicts with test mod)
            all_mods = db.connection.execute(
                "SELECT id, name FROM mods WHERE id <= ?",
                (pre_import_max,)
            ).fetchall()

            conflicting_ids = set()
            for c in result.conflicts:
                if c.level in ("byte_range", "paz"):
                    conflicting_ids.add(c.mod_a_id)
                    conflicting_ids.add(c.mod_b_id)
            conflicting_ids -= trial_ids

            for mod_id, mod_name_db in all_mods:
                if mod_id not in conflicting_ids:
                    result.compatible_mods.append(mod_name_db)
        finally:
            # An exception mid-analysis must not strand trial rows in
            # the DB (release-review minor, 2026-06-11).
            _cleanup_trial_rows(db, pre_import_max)

    return result


def _cleanup_trial_rows(db: Database, pre_import_max: int) -> None:
    """Remove every mods row the trial import created (id above the
    pre-import watermark). Deleting zero rows is fine; deleting a
    pre-existing mod is impossible by construction."""
    db.connection.execute(
        "DELETE FROM mods WHERE id > ?", (pre_import_max,))
    db.connection.commit()


def generate_compatibility_report(result: ModTestResult) -> str:
    """Generate a markdown compatibility report for Nexus Mods."""
    lines = [
        f"# Compatibility Report: {result.mod_name}",
        f"",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"",
        f"## Files Modified",
        f"",
    ]

    if result.changed_files:
        for cf in result.changed_files:
            # changed_files mixes dicts and plain path strings
            # depending on which import pass produced the entry.
            path = cf["file_path"] if isinstance(cf, dict) else cf
            lines.append(f"- `{path}`")
    else:
        lines.append("- No file changes detected")

    lines.append("")
    lines.append("## Compatibility")
    lines.append("")

    if result.compatible_mods:
        lines.append("**Compatible with:**")
        for name in sorted(result.compatible_mods):
            lines.append(f"- {name}")
    else:
        lines.append("**No installed mods to test against.**")

    if result.conflicts:
        lines.append("")
        lines.append("**Conflicts with:**")
        for c in result.conflicts:
            other_name = c.mod_b_name if c.mod_a_name == result.mod_name else c.mod_a_name
            lines.append(f"- **{other_name}**: {c.explanation}")

    lines.append("")
    lines.append("---")
    lines.append("*Generated by Crimson Desert Ultimate Mods Manager*")

    return "\n".join(lines)
