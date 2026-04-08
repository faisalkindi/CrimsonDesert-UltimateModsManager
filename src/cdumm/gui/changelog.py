"""Changelog data and patch notes dialog for CDUMM."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QTextBrowser, QPushButton, QLabel, QHBoxLayout,
)

# Changelog entries — newest first. Add new versions at the top.
CHANGELOG = [
    {
        "version": "2.2.4",
        "date": "2026-04-09",
        "notes": [
            "Fixed standalone PAZ mods (like Proper 3rd Person Camera) being incorrectly detected as loose-file mods. Numbered directories containing 0.paz are now correctly routed to the PAZ import path.",
        ],
    },
    {
        "version": "2.2.3",
        "date": "2026-04-08",
        "notes": [
            "DX10 multi-mip DDS textures (BC7 with mipmaps) now handled correctly. Written raw to overlay without inner LZ4 compression. Fixes Enhanced Map Icons and similar mods using DX10/BC7 format.",
            "Standard DDS textures (DXT1/DXT5 single mip) continue using inner LZ4 compression. Both formats work together in the same overlay.",
            "Vanilla backup gap closed. All game files are now backed up before modification, even if they don't match the snapshot. A potentially dirty backup is better than no backup.",
            "Revert to Vanilla button restored in the action bar.",
            "DDS decompression fallback: if all LZ4 decompression attempts fail, raw DDS data is returned instead of crashing. Handles DX10 raw passthrough entries during import.",
        ],
    },
    {
        "version": "2.2.2",
        "date": "2026-04-08",
        "notes": [
            "Localization system: language selector in Tools page. English and Arabic included. Translators can contribute by copying en.json to their language code. RTL layout support for Arabic.",
            "CDUMM minimizes to taskbar when Launch Game is clicked.",
            "Fixed text clipping on Tools page header and combo boxes (descenders cut off).",
        ],
    },
    {
        "version": "2.2.1",
        "date": "2026-04-08",
        "notes": [
            "Bare numbered directory mods now recognized. Mods that ship game files directly in NNNN/ directories without a files/ wrapper or mod.json (e.g. VAXIS Blood Mod with 0010/actionchart/...) are now detected and imported correctly.",
            "Fixed PAPGT stale hash causing game crash. The PAPGT rebuild now verifies PAMT hashes against disk for small PAMTs (<2MB), catching stale hashes from previous in-place applies or other mod managers without memory issues.",
        ],
    },
    {
        "version": "2.2.0",
        "date": "2026-04-07",
        "notes": [
            "Removed auto-update download for NexusMods TOS compliance. CDUMM no longer downloads executables from the internet. Update checks still work: when a new version is found, you are prompted to open the GitHub releases page in your browser to download manually.",
        ],
    },
    {
        "version": "2.1.9",
        "date": "2026-04-07",
        "notes": [
            "Fixed overlay PAZ entries not being found by the game. The overlay PAMT was using flattened folder paths (e.g. sequencer/) instead of the full hierarchical paths the game uses for lookups (e.g. sequencer/baseseq/gamesystemfx/ui/). Full paths are now resolved from the vanilla PAMT's folder tree.",
            "Fixed overlay PAMT format: corrected folder record structure and file record prefix to match vanilla format exactly.",
            "Fixed overlay directory collision with standalone mods. Overlay now allocates a directory number that doesn't conflict with any staged mod directories.",
            "Fixed standalone PAZ mods with mod.json being incorrectly detected as loose-file mods. Mods with numbered directories containing 0.paz are now correctly routed to the PAZ import path.",
            "Fixed PAPGT crash during rebuild when reading all PAMTs from disk in the worker thread.",
        ],
    },
    {
        "version": "2.1.8",
        "date": "2026-04-07",
        "notes": [
            "Overlay PAZ system: ENTR delta mods now write to a fresh overlay directory instead of modifying original game files. Original PAZ and PAMT files stay vanilla. The game loads modded entries from the overlay. This matches JSON Mod Manager's approach and improves stability.",
            "DDS texture mod support: fixed type 0x01 DDS split handling. The compression check no longer relies on comp_size vs orig_size (which is always equal for DDS split). DDS headers are automatically fixed with correct flags, depth, mip sizes, and format identifier. Mods like Modern Controller Icons now work correctly.",
            "DDS decompression reads inner LZ4 size from the DDS header instead of using the full padded body. Fixes decompression failures for overlay DDS entries.",
            "Removed PATHC update for DDS mods that was causing hash collisions with existing textures.",
            "PAPGT rebuild always verifies PAMT hashes against actual files on disk instead of trusting cached values from the base. Fixes stale hash mismatches after switching from in-place to overlay.",
            "CB handler uses game directory for PAZ file paths instead of vanilla backup which may be incomplete. Fixes import failures for mods in directories with partial backups.",
            "Loose file mods without a files/ directory now recognized. Mods with game paths directly next to mod.json are resolved via PAMT lookup.",
            "Automatic migration from in-place to overlay on first apply after update. PAMTs modified by previous versions are restored to vanilla before the overlay is created.",
            "Stale overlay directories from previous applies are cleaned up automatically.",
            "Mod status correctly shows active for ENTR delta mods when overlay directory exists.",
        ],
    },
    {
        "version": "2.1.7",
        "date": "2026-04-06",
        "notes": [
            "Fixed PAMT revert overwriting active mod changes. When a disabled mod shared a PAZ directory with enabled mods, the revert would restore the vanilla PAMT on top of the ENTR-updated PAMT, corrupting all active entries in that directory. PAMT revert now skips directories where enabled mods have active entry-level deltas.",
            "Fix Everything no longer requires Steam verify. Asks if you verified, takes a snapshot only if yes, proceeds either way.",
            "Contaminated ENTR deltas from pre-v2.1.6 imports are automatically cleaned on startup. Duplicate entry_paths across mods are detected and the foreign copies removed.",
        ],
    },
    {
        "version": "2.1.6",
        "date": "2026-04-06",
        "notes": [
            "Fixed disabling a mod breaking other active mods. The import handler was copying the current (modded) game PAZ instead of the vanilla backup. This caused other mods' entry changes to be attributed to the new mod. Disabling that mod would then revert all of them. Now uses vanilla backup as the base for PAZ copies and PAMT parsing.",
        ],
    },
    {
        "version": "2.1.5",
        "date": "2026-04-06",
        "notes": [
            "Loose file mods without a files/ directory now recognized. Mods that ship game file paths (gamedata/, sequencer/, ui/) directly next to mod.json are resolved via PAMT lookup. Supports mods like Skip All Loading Scene.",
        ],
    },
    {
        "version": "2.1.4",
        "date": "2026-04-06",
        "notes": [
            "Fixed removing a mod leaving game files partially modded. When a mod with entry-level deltas was removed, the PAZ was reverted but the PAMT index was not, causing corrupted game state (wrong texture sizes, missing icons, crashes). Both PAZ and PAMT are now reverted together.",
            "Fixed vanilla backups not created for PAMT and PATHC files. These files get modified during Apply by entry-level deltas and texture mods but had no backup, making revert impossible without Steam verify. Backups are now created automatically before any modification.",
        ],
    },
    {
        "version": "2.1.3",
        "date": "2026-04-06",
        "notes": [
            "Fixed false byte_range conflicts showing for mods that use entry-level deltas. Conflict panel now compares at the game file level instead of raw byte offsets. Two mods modifying different files inside the same PAZ archive correctly show as compatible.",
            "Fixed stale PAMT byte-range deltas left behind after entry-level PAZ import. PAMT files were processed before PAZ files alphabetically, creating unnecessary deltas that caused false conflict reports.",
            "Multi-variant loose file mods now detected and show a picker dialog. Mods like VAXIS Partial LOD Fix with multiple quality options (x1/x2/x3/x4/x5 folders) are recognized even when nested several folders deep inside the archive.",
            "Fixed null-byte padding crash for mods with smaller content than vanilla. XML and CSS files that are shorter than the original (common with line ending differences) no longer get padded with null bytes that crash the game's parser. PAMT orig_size is now updated to the actual content size.",
        ],
    },
    {
        "version": "2.1.2",
        "date": "2026-04-06",
        "notes": [
            "Fixed game directory not updating when Steam library is moved. CDUMM now validates the saved path has the actual game exe, and auto-detects the new location if the game was moved.",
            "Fixed loose file mods in zip archives not being detected (VAXIS LOD Fix and similar mods).",
            "Auto migration no longer triggers on every version update. Only triggers when the delta format actually changes. No more unnecessary reimports that wipe working mods.",
            "Migration never clears deltas. If a mod has no stored source, its existing deltas are kept as-is instead of being destroyed.",
            "Fix Everything button in the action bar. One click to revert, clear backups, rescan, and reimport. Recommends Steam verify first.",
            "Startup health check silently auto-fixes dirty game state (orphan directories, wrong PAPGT) when no mods are enabled.",
            "Auto migration now clears old deltas before reimport. Mods with no source get disabled instead of keeping stale wrong deltas that crash the game.",
            "Fixed configurable source leak between imports. Failed imports no longer pollute the next mod's source path or configure options.",
            "Fixed source archiving for filtered JSON mods. Original source path is preserved so reimport and Configure work correctly.",
            "Configure now shows preset picker for multi-preset mods (like Trust Me variants) and auto-applies after selection. Mod name updates to reflect current configuration.",
            "Toggle picker only shows when changes target the same game file. Mods like LET ME SLEEP that patch multiple files no longer show a confusing options dialog.",
            "Smarter configurable detection. Only mods with real options (multiple bracket groups in same file, or 10+ independent changes) show the gear icon.",
            "Bare loose file mods now detected without mod.json (files/NNNN/ structure).",
            "Notifies users on startup if mods are missing source files and need reimport.",
            "Fixed orphan mod directories not cleaned up when mods disabled via Apply.",
            "Fixed stale PAPGT backup causing modded state after revert.",
            "Bare loose file mods now detected without mod.json. Mods with files/NNNN/ structure (like Enhanced Internal Graphics) import correctly.",
            "Auto migration now runs on background thread with progress dialog instead of freezing the UI.",
            "Configure now shows preset picker for multi-preset mods (like Trust Me) and auto-applies after selection.",
            "Mod name updates to reflect current configuration after reconfiguring.",
            "JSON mods now archive source files to CDMods/sources/ for auto-reimport and Configure support.",
            "Notifies users on startup if mods are missing source files and need reimport.",
            "Configurable flag only set for mods with real configurable options (not simple labeled descriptions).",
            "Rescan now clears stale vanilla backups automatically. After Steam verify, old backups from previous modded state are wiped so reverts always use clean data.",
            "Fixed JSON mods in ZIP archives using the old FULL_COPY path instead of entry level deltas. ZIP JSON mods now compose correctly like folder and standalone JSON imports.",
            "Consolidated duplicate extraction code into single decompress_entry utility.",
            "Range backup revert now verifies result against snapshot hash and warns if reconstruction may be incomplete.",
            "Stale staging directory cleaned up on startup after crash.",
            "Database entry_path index created safely after migration for old databases.",
            "JSON patch merge metadata now scoped to specific entry instead of overwriting all entries in same directory.",
            "Empty JSON imports now show error instead of creating a mod with no data.",
            "Directory assignment ceiling raised from 200 to 9999 with proper error instead of silent collision.",
            "Assigned directory numbers cleared on startup to prevent leaks across sessions.",
            "Per patch toggle now works for ALL labeled JSON mods, not just bracket prefixed ones.",
            "Fixed mods importing with 'no data' when game files are already modded.",
            "Fixed game directory not persisting between launches. The setup dialog no longer appears every time you open CDUMM.",
            "Auto migration after update. When CDUMM updates, it offers to revert and reimport all mods automatically so they use the new internal format. Mod list, enabled state, and load order are preserved.",
            "Fixed critical PAPGT crash with standalone mods (Better Minimap, Better Trade Menu, Better Inventory UI, save editors). Mod shipped PAPGTs were being parsed incorrectly, removing all 33 vanilla directories and leaving only the mod's directory. PAPGT is now always built from vanilla base with new directories discovered from disk.",
            "Update dialog only shows once per session, no more repeated popups every 15 minutes",
            "Fixed JSON import crash — preset picker had a variable rename bug that silently killed all labeled JSON imports",
            "Detects and offers to clean up stale data from old CDUMM versions in AppData",
            "Warns when game is installed under Program Files (admin restrictions can cause mod issues)",
            "Update check interval reduced to 15 minutes (was 4 hours)",
            "Epic Games Store support — CDUMM now auto-detects Crimson Desert installed via Epic",
            "Improved Xbox Game Pass detection — scans .GamingRoot drives",
            "Partially compressed textures now supported — DDS mods with split header+body compression (type 0x01) work correctly",
            "Fixed preset picker auto-selecting first option — single-patch mods with bracket-labeled variants now show radio buttons instead of checkboxes",
            "Fixed v1.8.0 regression — encryption probe was falsely marking all large entries as encrypted, causing game crashes on every mod",
            "PAZ replacement mods now decompose into entry-level deltas — mods modifying different entries in the same PAZ no longer conflict",
            "Fixed slow/laggy UI — mod list no longer queries database on every cell paint",
            "Added database indexes for mod_deltas and conflicts tables",
            "Build spec no longer has hardcoded paths — other contributors can build from source",
            "Patches with mismatched original bytes are now skipped instead of applied blindly",
            "JSON mods no longer crash when sharing a PAZ file — Trust Me + Loot Multiplier etc. now compose correctly",
            "JSON mods produce entry-level deltas instead of copying the entire 955MB PAZ",
            "Fixed Dark Map and other CSS mods crashing the game — encrypted files were being repacked without encryption",
            "Loose file mods now supported — mods with mod.json + files/ directory (e.g. Mute Vendor Music) import correctly",
            "Fixed Revert leaving files modded — range backups now accumulate when new mods touch the same file",
            "Fixed PAPGT rebuild during Revert using stale modded hashes — now recomputes from vanilla PAMTs",
            "Revert now warns if any files couldn't be restored and advises Steam Verify",
            "Preset picker shows summary instead of hundreds of change labels",
        ],
    },
    {
        "version": "1.7.1",
        "date": "2026-04-01",
        "notes": [
            "Fixed auto-update dialog not showing — users on old versions now get prompted correctly",
            "Fixed 'too many SQL variables' crash on startup for users with many mods",
            "Persistent red update banner at the bottom — stays visible until you update",
            "Critical versions (below v1.7.0) are force-updated — no option to skip",
            "Update download applies immediately — no second confirmation dialog",
            "If automatic download fails, opens the browser to GitHub releases as fallback",
            "Re-checks for updates every 4 hours for users who leave the app open",
        ],
    },
    {
        "version": "1.7.0",
        "date": "2026-04-01",
        "notes": [
            "Mods survive game updates — auto-reimported from stored sources after rescan",
            "Database moved to CDMods/ in game directory — everything in one place",
            "File hashing 16x faster with xxh3_128",
            "Verify Game State detects in-place mods (same size, different content)",
            "Stale vanilla backups auto-cleaned after Steam verify",
            "Game-running check no longer gives false positives",
            "Duplicate mods and orphaned files cleaned up on startup",
            "About tab with update indicator and links",
            "Resizable columns, readable progress bar",
        ],
    },
    {
        "version": "1.6.3",
        "date": "2026-03-31",
        "notes": [
            "Fixed decompression error when importing JSON mods on modded game files",
            "CDUMM now retries extraction with fresh offsets when vanilla offsets don't match",
            "Mods that both use directory 0036 (like PlayStation Icons + Clean Kills) now work together",
            "Each standalone mod gets its own directory and all are added to PAPGT correctly",
            "After updating: Disable all → Apply → Re-enable all → Apply",
        ],
    },
    {
        "version": "1.6.1",
        "date": "2026-03-31",
        "notes": [
            "JSON mods no longer fail when vanilla PAZ backup doesn't exist",
            "Variant mods like Fat Stacks now show a picker to choose which option to install",
            "Mods with plain labels no longer incorrectly show the preset picker",
            "Standalone mods (Free Gliding, LET ME SLEEP, etc.) now work — new directories placed first in PAPGT",
            "New mod directories use correct flags matching what mod authors expect",
            "All columns in the mod list are now resizable by dragging",
            "After updating: Disable all → Apply → Re-enable all → Apply for changes to take effect",
        ],
    },
    {
        "version": "1.6.0",
        "date": "2026-03-31",
        "notes": [
            "Import is dramatically faster — large files use streaming comparison",
            "Apply responds instantly — removed blocking dialogs and slow process checks",
            "PAPGT integrity check only rehashes directories that changed (not all 33)",
            "Revert now guarantees ALL files return to vanilla — safety net catches orphaned files",
            "Multiple mods modifying the same PAZ compose correctly (FULL + sparse patches)",
            "Overlay mods like Helmet and Armor Hider now work (mod-shipped PAPGT preserved)",
            "JSON mods patching the same file get changes merged (e.g. Stamina + Fat Stacks)",
            ".bsdiff patches auto-detect target game file — no special naming needed",
            "PAPGT backed up before first Apply — Revert restores exact vanilla copy",
            "Xbox Game Pass game directory detection",
            "Import progress shows per-file status instead of freezing at 0%",
            "Conflicts shown in panel instead of blocking popup",
        ],
    },
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
