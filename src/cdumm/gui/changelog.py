"""Changelog data and patch notes dialog for CDUMM."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QTextBrowser, QPushButton, QLabel, QHBoxLayout,
)

# Changelog entries — newest first. Add new versions at the top.
CHANGELOG = [
    {
        "version": "1.4.0",
        "date": "2026-03-30",
        "notes": [
            # ── Mod Composition Engine (NEW) ──
            "Script mods now captured at PAMT entry level — mods that change different files in the same PAZ compose correctly",
            "Multiple script mods modifying the same PAZ no longer corrupt each other",
            "PAMT index rebuilt from entry-level changes during Apply instead of raw byte diffs",
            "Apply now processes PAZ files first, then rebuilds PAMT, then PAPGT — correct dependency order",
            # ── Conflict Detection & Safety ──
            "Dangerous byte-range overlaps shown as a blocking warning before Apply — lists every conflict and winner",
            "Apply preview — shows exactly what files will be changed before modifying anything",
            "Post-apply integrity verification — checks PAPGT hash, PAMT entries, and PAZ bounds",
            "Safety net catches orphaned modded files left by removed mods and restores them",
            # ── Game Update Detection ──
            "Game update/hotfix detection now shows Steam build ID in the notification",
            "Automatic reset and rescan when game files change (update, hotfix, or Steam verify)",
            "Mod version mismatch warnings — flags mods imported for a different game version",
            # ── Reliability Overhaul ──
            "PAPGT always rebuilt from scratch — never restored from stale backup",
            "PAPGT rebuild removes entries for deleted mod directories (fixes reinstall errors)",
            "Vanilla backups validated against snapshot before creation (rejects modded files)",
            "Snapshot refuses to run on modded files — blocks with clear error message",
            "Orphan mod directories (0036+) cleaned up automatically",
            "Corrupted vanilla backups detected and purged on startup",
            "PAMT hash always recomputed after composing multiple mod deltas",
            # ── Trust & Transparency ──
            "Verify Game State tool — scan all files and see what's vanilla vs modded",
            "Activity Log tab — persistent, color-coded history of every action across sessions",
            "No more silent snapshot refresh — always asks before rescanning",
            # ── New Formats & Import ──
            "JSON preset picker — choose which variant when a mod has labeled presets",
            "7z archive support",
            "Batch import — drop multiple mods at once, imported sequentially",
            "New mods import as disabled — must enable and Apply explicitly",
            # ── ASI Mods ──
            "ASI Loader detection recognizes version.dll, dinput8.dll, dsound.dll",
            "Bundled ASI Loader auto-install when missing",
            # ── UX Improvements ──
            "Script capture progress now shows per-file scanning status instead of freezing at 0%",
            "Configurable mods show gear icon in mod list",
            "Import date shows local time instead of UTC",
            "Leftover .bak files from mod scripts detected and offered for cleanup",
            # ── Bug Fixes ──
            "Fixed script mods leaving game files modded after capture — vanilla restored automatically",
            "Fixed CB mod content truncation — mod files are never modified or stripped",
            "Fixed FULL_COPY delta ordering — applied before SPRS patches from other mods",
            "Fixed ASI panel not showing installed plugins after loader install",
            "Fixed binary search wizard crash on round 10 (NameError in result display)",
            "Fixed uninstall not reverting game files (now disables, applies, then deletes)",
        ],
    },
    {
        "version": "1.2.0",
        "date": "2026-03-29",
        "notes": [
            "Added DDS texture mod support (PATHC format) — install texture replacement mods",
            "Added Crimson Browser mod support for game update directories (prefers latest PAZ)",
            "Fixed Hair Physics mod crash — CB handler now resolves to correct PAZ directory",
            "Added patch notes dialog — see what changed after each update",
            "Drop zone now shows hints about updating mods and right-click options",
            "Snapshot now tracks meta/0.pathc for texture mod revert support",
        ],
    },
    {
        "version": "1.1.2",
        "date": "2026-03-28",
        "notes": [
            "Fixed stale snapshot detection causing repeated reset prompts",
            "Improved game update detection using Steam build ID",
            "Silent snapshot refresh when files are stale but game version unchanged",
        ],
    },
    {
        "version": "1.1.1",
        "date": "2026-03-27",
        "notes": [
            "Fixed app freeze when importing large mods (LootMultiplier 954MB PAZ)",
            "Added FULL_COPY delta format for files >500MB with different sizes",
            "Fixed mod update detection for concatenated names",
        ],
    },
    {
        "version": "1.1.0",
        "date": "2026-03-26",
        "notes": [
            "Added game update auto-detection and reset flow",
            "Added one-time reset for users upgrading from pre-1.0.7",
            "Improved snapshot integrity — prevents dirty snapshots from modded files",
            "Fixed conflict detector capped at 200 to prevent UI freeze",
        ],
    },
    {
        "version": "1.0.9",
        "date": "2026-03-25",
        "notes": [
            "Fixed PAMT hash conflict when multiple mods modify the same PAMT",
            "Health check now uses vanilla backup for accurate validation",
            "Bug report version now reads from __version__ instead of hardcoded",
        ],
    },
    {
        "version": "1.0.0",
        "date": "2026-03-22",
        "notes": [
            "First stable release",
            "PAZ mod import from zip, folder, .bat, .py scripts",
            "JSON byte-patch mod format support",
            "Crimson Browser mod format support",
            "ASI plugin management",
            "Drag-and-drop import with auto-update detection",
            "Mod conflict detection and resolution",
            "Vanilla backup and restore system",
            "Health check with auto-fix for common mod issues",
        ],
    },
]


def get_changelog_html(versions: list[dict] | None = None) -> str:
    """Generate HTML changelog from version data."""
    entries = versions or CHANGELOG
    lines = ['<div style="font-family: Segoe UI, sans-serif; color: #D8DEE9;">']
    for entry in entries:
        lines.append(
            f'<h3 style="color: #D4A43C; margin-bottom: 4px;">'
            f'v{entry["version"]} &mdash; {entry["date"]}</h3>'
        )
        lines.append('<ul style="margin-top: 2px; margin-bottom: 16px;">')
        for note in entry["notes"]:
            lines.append(f'<li style="margin-bottom: 3px;">{note}</li>')
        lines.append('</ul>')
    lines.append('</div>')
    return "\n".join(lines)


def get_latest_notes_html() -> str:
    """Get HTML for just the latest version's notes."""
    if not CHANGELOG:
        return ""
    return get_changelog_html([CHANGELOG[0]])


class PatchNotesDialog(QDialog):
    """Dialog showing patch notes — either latest or full history."""

    def __init__(self, parent=None, latest_only: bool = False):
        super().__init__(parent)
        version = CHANGELOG[0]["version"] if CHANGELOG else "?"
        if latest_only:
            self.setWindowTitle(f"What's New in v{version}")
        else:
            self.setWindowTitle("CDUMM Patch Notes")
        self.setMinimumSize(520, 420)
        self.resize(560, 500)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        if latest_only:
            header = QLabel(f"CDUMM has been updated to v{version}")
            header.setStyleSheet(
                "font-size: 15px; font-weight: bold; color: #ECEFF4;"
            )
            layout.addWidget(header)

        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setStyleSheet(
            "QTextBrowser { background: #1A1D23; border: 1px solid #2E3440; "
            "border-radius: 6px; padding: 8px; }"
        )
        if latest_only:
            browser.setHtml(get_latest_notes_html())
        else:
            browser.setHtml(get_changelog_html())
        layout.addWidget(browser)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setFixedWidth(100)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)
