# Crimson Desert Ultimate Mods Manager

A desktop mod manager for **Crimson Desert** that handles the game's PAZ/PAMT/PAPGT archive format. Install, manage, and safely combine multiple mods with automatic conflict detection, JSON patch merging, and one-click revert to vanilla.

**Works with Steam.** Xbox Game Pass installations are detected but currently limited by platform restrictions (read-only game files).

![Screenshot](https://raw.githubusercontent.com/faisalkindi/CrimsonDesert-UltimateModsManager/master/screenshots/app.png?v=2)

## Features

### Drag-and-Drop Import
Drop a mod onto the window and it's installed. Supports every mod format in the Crimson Desert modding scene:

| Format | Description |
|--------|-------------|
| `.zip` / `.7z` | Archives containing modified game files or installer scripts |
| Folders | Loose directories with modified PAZ/PAMT files |
| `.json` | JSON byte-patch mods (compatible with [JSON Mod Manager](https://www.nexusmods.com/crimsondesert/mods/113)) |
| `manifest.json` + files | Crimson Browser loose-file mods — automatically repacked into PAZ |
| `.bat` / `.py` | Script-based installers — runs in a visible console, captures changes automatically |
| `.bsdiff` | Pre-generated binary patches (auto-detects target game file) |
| `.asi` | Native ASI plugins (installed to `bin64/`) with bundled ASI Loader |

Batch import supported — drop multiple mods at once.

### JSON Patch Merging
Multiple JSON mods that patch the **same game file** (e.g., Stamina mod + Fat Stacks both editing `iteminfo.pabgb`) are automatically merged at the decompressed content level. Non-overlapping patches from different mods compose perfectly. Overlapping bytes go to the higher-priority mod.

Works for both newly imported mods and mods imported in older versions (fallback three-way merge).

### Entry-Level Script Mod Composition
Script mods (`.bat`) are captured at the PAMT entry level — the manager identifies which individual game files changed inside each PAZ archive and stores the decompressed content. This means two script mods modifying different files in the same PAZ compose correctly instead of corrupting each other.

### Delta-Based Patching
Mods are stored as binary deltas against vanilla game files, not full file copies:

- **Small on disk** — only the changed bytes are saved
- **Composable** — multiple mods can modify the same PAZ file at different offsets
- **Reversible** — vanilla files are always preserved and restorable

The engine automatically selects between sparse patches (small, scattered changes), entry-level deltas (decompressed game files), and bsdiff4 (large modifications).

### 3-Level Conflict Detection

When two mods touch the same files, the manager detects the conflict and shows exactly what overlaps:

| Level | What It Means | Action |
|-------|--------------|--------|
| **PAPGT** (metadata) | Mods modify PAMT in different directories | Auto-handled, no action needed |
| **PAZ** (archive) | Same PAZ file, different byte ranges | Usually compatible — shown as info |
| **Byte-Range** (data) | Overlapping byte ranges in the same file | Resolved by load order — winner shown in UI |

**Dangerous overlaps are shown as a blocking warning before Apply** — lists every conflict pair and which mod wins.

### Trust & Transparency
- **Apply Preview** — see exactly what files will be changed before modifying anything
- **Verify Game State** — scan all files and see what's vanilla vs modded
- **Activity Log** — persistent, color-coded history of every action across sessions
- **Post-apply verification** — confirms PAPGT/PAMT integrity after every Apply

### Load Order & Priority
- Drag mods up and down to set priority
- Higher position = applied last = wins conflicts
- Enable/disable individual mods without removing them
- **Export/Import Mod List** — save and restore your entire setup (enabled state, load order, priorities)

### One-Click Apply & Revert
- **Apply** composes all enabled mods onto vanilla files in correct dependency order (PAZ first, then PAMT, then PAPGT)
- **Revert to Vanilla** restores original game files from full vanilla backups
- Crash recovery via `.pre-apply` markers if something goes wrong mid-apply

### Game Update Detection
- Detects game updates and hotfixes automatically (via Steam build ID + exe hash)
- Warns about mods imported for a different game version
- One-time migration on major updates — guides you through verify + rescan

### Script Mod Support
For mods distributed as installer scripts (`.bat` or `.py`):

1. Drop the zip/script onto the manager
2. A console window opens — interact with the installer normally
3. The manager parses the PAMT to identify which game files changed, extracts and decompresses each entry, and stores the decompressed content
4. The mod is now managed like any other — can be disabled, reordered, or reverted

The manager passes `CDMM_GAME_DIR` as an environment variable so scripts can find the game directory automatically.

### Mod Health Check
Every mod is automatically validated before import:

- **Duplicate PAMT paths** — detects overlay mods that add files already in another PAZ directory and handles them correctly (skips PAPGT entry to avoid crashes)
- **Hash mismatches** — verifies PAMT and PAPGT integrity chains
- **PAZ size errors** — catches when PAMT size fields don't match actual files
- **Version mismatches** — warns if the mod was built for a different game version

### Find Problem Mod (Delta Debugging)
When a combination of mods crashes the game, the **Find Problem Mod** wizard uses the Delta Debugging algorithm (ddmin) to find the minimal set of mods causing the crash:

- Tests subsets of your enabled mods automatically
- You launch the game and report crash/no-crash after each test
- Finds single bad mods, conflict pairs, and multi-mod interactions
- Progress is saved — you can resume later if interrupted
- Typically finds the culprit in 10-20 rounds

### ASI Plugin Management
A dedicated **ASI Plugins** tab for managing native DLL plugins:

- Scans `bin64/` for installed `.asi` files
- **Bundled ASI Loader** — auto-installs `winmm.dll` if missing
- Install, update, uninstall, enable/disable plugins
- Opens `.ini` config files in your text editor
- Detects ASI Loader variants (winmm.dll, version.dll, dinput8.dll, dsound.dll)

### Configurable Mods
JSON mods with labeled presets (e.g., "x5 loot", "x10 loot", "x99 loot") show a toggle picker during import. Choose which variant you want. Configurable mods display a gear icon in the mod list.

### Vanilla Snapshot
On first launch, the manager takes a SHA-256 snapshot of all game files. This snapshot is used for:

- Detecting changes made by script mods
- Generating accurate deltas
- Verifying game file integrity
- Blocking snapshots on modded files (prevents dirty backups)

### Bug Report
Built-in diagnostic report generator that collects system info, installed mods, conflict status, database state, and recent log entries. Copy to clipboard or save as a file.

## Installation

### Option 1: Standalone Executable (Recommended)
Download `CDUMM.exe` from the [Releases](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases) page. No Python required. Just run it.

### Option 2: Run from Source
Requires Python 3.10+.

```bash
git clone https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager.git
cd CrimsonDesert-UltimateModsManager
pip install -e .
py -3 -m cdumm.main
```

### Building the Executable

```bash
pip install pyinstaller
pyinstaller cdumm.spec --noconfirm
```

The exe is written to `dist/CDUMM.exe`.

## How It Works

Crimson Desert stores game data in PAZ archives, indexed by PAMT files, with PAPGT as a hash registry. This manager:

1. **Snapshots** vanilla game files on first run
2. **Imports** mods by diffing modified files against vanilla, storing only the binary delta (or decompressed entry content for script/JSON mods)
3. **Merges** JSON patches from multiple mods at the decompressed content level (three-way merge against vanilla)
4. **Composes** all enabled mod deltas onto vanilla in priority order when you click Apply (PAZ first, then PAMT, then PAPGT)
5. **Rebuilds** the PAPGT integrity chain so the game accepts the modified files
6. **Commits** atomically — all files are staged and swapped in one operation

Mod data is stored in `<GameDir>/CDMods/`:
- `vanilla/` — full backups of PAMT files, byte-range backups for PAZ files
- `deltas/` — binary patches and entry-level deltas for each mod

App config is stored in `%LOCALAPPDATA%\cdumm\cdumm.db`.

## Requirements

- Windows 10/11
- Crimson Desert (Steam version recommended, Xbox Game Pass detected but limited)

## Support

If you find this useful, consider supporting development:

[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/kindiboy)

## Credits

- **Lazorr** — [Crimson Desert Unpacker](https://www.nexusmods.com/crimsondesert/mods/62) — PAZ parsing and repacking tools that CDUMM's archive pipeline is built on
- **PhorgeForge** — [JSON Mod Manager](https://www.nexusmods.com/crimsondesert/mods/113) — JSON byte-patch mod format, natively supported by CDUMM
- **993499094** — [Crimson Desert QT Mod Manager](https://www.nexusmods.com/crimsondesert/mods/218) — Hard link deployment approach and modinfo.json format
- **callmeslinkycd** — [Crimson Desert PATHC Tool](https://www.nexusmods.com/crimsondesert/mods/396) — PATHC texture index parser and repacker that CDUMM's DDS texture mod support is built on

## License

MIT
