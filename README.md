# Crimson Desert Ultimate Mods Manager (BETA)

A desktop mod manager for **Crimson Desert** that understands the game's PAZ/PAMT/PAPGT archive format. Install, manage, and safely combine multiple mods with automatic conflict detection and one-click revert to vanilla.

![Screenshot](https://raw.githubusercontent.com/faisalkindi/CrimsonDesert-UltimateModsManager/master/screenshots/app.png)

## Features

### Drag-and-Drop Import
Drop a mod onto the window and it's installed. Supports multiple formats:

| Format | Description |
|--------|-------------|
| `.zip` | Archives containing modified game files or installer scripts |
| Folders | Loose directories with modified PAZ/PAMT files |
| `.bat` / `.py` | Script-based installers — runs in a visible console, captures changes automatically |
| `.bsdiff` | Pre-generated binary patches |
| `.asi` | Native ASI plugins (installed to `bin64/`) |

### Delta-Based Patching
Mods are stored as binary deltas against vanilla game files, not full file copies. This means:

- **Small on disk** — only the changed bytes are saved
- **Composable** — multiple mods can modify the same PAZ file at different offsets
- **Reversible** — vanilla files are always preserved and restorable

The engine automatically selects between sparse patches (for small, scattered changes) and bsdiff4 (for larger modifications).

### 3-Level Conflict Detection

When two mods touch the same files, the manager detects the conflict and shows exactly what overlaps:

| Level | What It Means | Action |
|-------|--------------|--------|
| **PAPGT** (metadata) | Mods modify PAMT in different directories | Auto-handled, no action needed |
| **PAZ** (archive) | Same PAZ file, different byte ranges | Usually compatible — shown as info |
| **Byte-Range** (data) | Overlapping byte ranges in the same file | Resolved by load order — winner shown in UI |

Conflicts are displayed in a tree view at the bottom of the window with color-coded severity and explanations of what each mod changes.

### Load Order & Priority
- Drag mods up and down to set priority
- Higher position = applied last = wins conflicts
- Enable/disable individual mods without removing them

### One-Click Apply & Revert
- **Apply** composes all enabled mods onto vanilla files using atomic file operations (never leaves the game in a half-patched state)
- **Revert to Vanilla** restores original game files instantly
- Crash recovery via `.pre-apply` markers if something goes wrong mid-apply

### Script Mod Support
For mods distributed as installer scripts (`.bat` or `.py`):

1. Drop the zip/script onto the manager
2. A console window opens — interact with the installer normally
3. The manager automatically detects which game files changed and captures them as deltas
4. The mod is now managed like any other — can be disabled, reordered, or reverted

The manager passes `CDMM_GAME_DIR` as an environment variable so scripts can find the game directory automatically.

### Mod Health Check
Every mod is automatically validated before import:

- **Duplicate PAMT paths** — detects mods that add files already in another PAZ (crashes the game). Auto-fixed by redirecting the entry.
- **Hash mismatches** — verifies PAMT and PAPGT integrity chains
- **PAZ size errors** — catches when PAMT size fields don't match actual files
- **Version mismatches** — warns if the mod was built for a different game version
- **Bug report generator** — one-click copy of a detailed report to send to the mod author

### PAZ-Shift Aware Composition
Mods that rearrange PAZ files (like LootMultiplier's data shift) work alongside mods that patch specific bytes (like Inventory Expander). The apply engine detects file size changes and automatically adjusts patch offsets so both mods compose correctly.

### ASI Plugin Management
A dedicated **ASI Plugins** tab for managing native DLL plugins:

- Scans `bin64/` for installed `.asi` files
- **Install** — copies `.asi`, all `.ini` config files, and ASI loader DLLs from mod folder
- **Update** — replace a plugin with a newer version while preserving enabled/disabled state
- **Uninstall** — deletes the `.asi` and all companion `.ini` files
- Enable/disable plugins (renames to `.asi.disabled`)
- Detects hook conflicts between plugins
- Opens `.ini` config files in your text editor
- Shows ASI Loader status

### Test Mod (Dry Run)
Check if a mod is compatible with your current setup **before** installing:

- Analyzes which files the mod changes
- Runs conflict detection against all installed mods
- Generates a compatibility report (exportable as Markdown)

### Vanilla Snapshot
On first launch, the manager takes a SHA-256 snapshot of all game files. This snapshot is used for:

- Detecting changes made by script mods
- Generating accurate deltas
- Verifying game file integrity

### Bug Report
Built-in diagnostic report generator that collects:

- System info (OS, Python version, memory)
- Installed mods and their conflict status
- Database state and snapshot status
- Recent log entries

Copy to clipboard or save as a file for troubleshooting.

## Installation

### Option 1: Standalone Executable
Download `CrimsonDesertModManager.exe` from the [Releases](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases) page. No Python required.

### Option 2: Run from Source
Requires Python 3.10+.

```bash
git clone https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager.git
cd CrimsonDesert-UltimateModsManager
pip install -e .
python src/cdmm/main.py
```

### Building the Executable

```bash
pip install pyinstaller
python scripts/build.py
```

The exe is written to `dist/CrimsonDesertModManager.exe`.

## How It Works

Crimson Desert stores game data in PAZ archives, indexed by PAMT files, with PAPGT as a hash registry. This manager:

1. **Snapshots** vanilla game files on first run
2. **Imports** mods by diffing modified files against vanilla, storing only the binary delta
3. **Composes** all enabled mod deltas onto vanilla in priority order when you click Apply
4. **Rebuilds** the PAPGT integrity chain so the game accepts the modified files
5. **Commits** atomically — all files are staged and swapped in one operation

All data is stored in `%LOCALAPPDATA%\cdmm\`:
- `cdmm.db` — mod registry, snapshots, conflicts
- `vanilla/` — byte-range backups of modified game files (not full copies)
- `deltas/` — binary patches for each mod

## Requirements

- Windows 10/11
- Crimson Desert (Steam)
- Minimal free disk space (backups are byte-range level, not full file copies)

## Support

If you find this useful, consider supporting development:

[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/kindiboy)

## License

MIT
