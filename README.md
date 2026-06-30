<p align="center">
  <img src="assets/cdumm-banner.png" alt="CDUMM Banner" width="100%">
</p>

<p align="center">
  <b>The only mod manager you need for Crimson Desert.</b><br>
  Every mod format. Every store (Steam, Epic, Xbox). One click.
</p>

<p align="center">
  <a href="https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/latest"><img src="https://img.shields.io/github/v/release/faisalkindi/CrimsonDesert-UltimateModsManager?style=flat-square&color=2878D0&label=Download" alt="Download"></a>
  <a href="https://ko-fi.com/kindiboy"><img src="https://img.shields.io/badge/Support-Ko--fi-FF5E5B?style=flat-square&logo=ko-fi&logoColor=white" alt="Ko-fi"></a>
  <img src="https://img.shields.io/github/downloads/faisalkindi/CrimsonDesert-UltimateModsManager/total?style=flat-square&color=16A34A&label=Downloads" alt="Downloads">
</p>

---

## What's New

CDUMM ships frequent updates. For the complete per-version history, see the [Releases](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases) page (the in-app updater also shows release notes after each update). Recent progress, newest first:

### v3.4 series — game 1.12 support & one-click updates

- **v3.4.2** — _June 30, 2026_ — **Text / string mods apply.** Mods like the Female Armor Module edit variable-length string entries that were silently getting skipped; CDUMM now rewrites the string in place by its key and rebuilds the table index (checked byte-for-byte against the whole vanilla string table). The "Missing directory" error when disabling a folder-adding mod is fixed and now names the mod responsible. (#224, #225)
- **v3.4.1** — _June 23, 2026_ — **Item mods work again on game 1.12** (the June 20 patch changed the item-table layout). A new **Update All** button reimports every outdated mod in one go, keeping each mod's enabled state, load order and folder group. A very large mod no longer has its apply killed early by the progress watchdog. (#219, #218)
- **v3.4.0** — _June 17, 2026_ — `equipable_hash` equipment-unlock mods apply now (the importer was skipping them before they reached the writer; verified on AbyssGearUnlock). Bare ReShade `.addon64` mods install into `bin64`, a mod's folder group survives an update, and the preset / toggle picker no longer pushes Apply / Cancel off-screen. (#191, #202, #161, #196)

### v3.3 series — item-table overhaul & robustness

- **v3.3.19 – v3.3.23** — _June 10–16, 2026_ — The big item-data campaign. After successive game patches reshuffled the item-table layout, Format 3 item mods (stack sizes, durability, cooldowns, buffs, sockets, **store** stock lists, **equip-slot** hash lists, storeinfo) went from importing with zero changes to applying completely again — verified byte-for-byte. Encrypted material mods (e.g. VAXIS Water Physics) write correctly again, a third "Game exe (skip Steam)" launch option was added, and available updates no longer vanish from the list after a follow-up check. (#182, #191, #183, #190, #199, #186, #194)
- **v3.3.15 – v3.3.18** — _May 29–June 8, 2026_ — The self-update download-complete freeze is properly fixed (work moved back to the main thread, plus a `.old` exe swap so Windows can replace the running app). Downloads and update checks work again after the 1.09 patch (certifi trust store). Character-creator mods now ask which race / gender to install first, recovery no longer loops after a game update, characterinfo mesh / model fields apply, and silent import failures are surfaced instead of counting as success. (#170/#172, #190, #163, #192, #193, #165)
- **v3.3.0 – v3.3.14** — _May 10–27, 2026_ — Stale-overlay cleanup (#141), `gamedata/` wrapper imports (#146), a 4 GB texture-pack guard (#148), the hide-on-launch toggle, preset persistence, and a steady run of apply-correctness fixes.

Older releases — **v3.1–v3.2** (NexusMods integration, one-click game-update recovery, and the first Format 3 field-name mod support) — are on the [Releases](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases) page.

---

## How It Works

Your original game files are **never modified**. Mods are applied through an overlay directory. Reverting is instant.

1. Download **CDUMM3.exe** and run it — no install needed
2. Welcome wizard guides you through language, theme, and game folder setup
3. Drop mods onto the window OR sign in to Nexus and use "Mod Manager Download" buttons
4. Click **Apply**

> If something goes wrong, click **Fix Everything** to restore clean state. After a Steam patch, click **Start Recovery** on the yellow banner.

---

## Supported Formats

| Format | Description |
|--------|-------------|
| `.zip` / `.7z` / `.rar` | Archives — auto-extracted, including nested zips for multi-language packs |
| Folders | Loose directories with PAZ/PAMT files or Crimson Browser mods |
| `.json` (byte-patch) | Offset-based JSON mods (`offset`, `original`, `patched`) |
| `.field.json` (field-name) | Field-name JSON mods — items, mounts, terrain, stages, regions, mount character, buffs, drop sets, and skills. Supports both singular `target` and multi-target `targets: [...]` shapes. |
| `.dds` | DDS texture mods with full PATHC index registration (BC1/BC3/BC4/BC5/BC7) |
| `OG_*.xml` | XML full replacement mods |
| `.asi` | ASI plugins — auto-detected, installed to `bin64/` with clean uninstall tracking |
| `.bnk` | Wwise soundbank mods |
| `.bat` / `.py` | Script installers — runs in console, captures changes |
| `.bsdiff` / `.xdelta` | Binary patches |
| Mixed archives | ZIPs with ASI + PAZ content — auto-separated |
| Multi-variant packs | Mods that ship multiple versions in one zip — variant picker appears |

---

## Key Features

### NexusMods Integration (v3.2)
- **One-click sign-in** — Login with Nexus opens your browser, you confirm, done. No API keys to copy and paste. CDUMM never sees your password.
- **Auto-check for mod updates** — every 30 minutes CDUMM checks Nexus for new versions of the mods you have installed. Outdated mods get a red "Click To Update" badge; current mods get a quiet green check.
- **Mod Manager Download buttons work** — toggle the handler in Settings and any "Mod Manager Download" button on a Nexus page sends the file straight to CDUMM. Premium users get one-click downloads; free users get sent to the right Files tab.
- **Manual API key still supported** — tucked behind an Advanced toggle in Settings if you'd rather paste your own key.

### Game Update Recovery (v3.2)
- **One-click recovery after Steam patches.** Yellow banner appears on launch, click Start Recovery, watch a 4-step progress bar repair everything: verify your game files, regenerate every mod against the new game version, reapply.
- **Two triggers, one banner.** Catches normal Steam patches AND any other change to your game files (antivirus rewrites, manual edits, half-finished Steam Verify).
- **Mods that can't be auto-recovered get safely disabled** instead of corrupting your save. CDUMM tells you which ones so you can drop their original archive back in.

### Performance
- **Apply is hundreds of times faster on big mod sets** (v3.2). Conflict detection is near-instant; cross-mod byte merging runs hundreds of times faster than the v3.1 line.
- **Batch import** — drop dozens of mods at once, single-process import
- **Fast apply** — overlay cache + Rust native engine, applies in seconds
- **~50 MB exe** — single standalone binary, no install needed

### Resilience
- **One bad mod can't kill the apply** (v3.2.3) — broken changes are skipped with a clear log naming the mod, the rest of your stack still applies
- **Fix Everything** — one click restores clean vanilla state if anything goes sideways
- **Atomic apply** — partial failures roll back; no half-applied state on disk

### Mod Management
- **Entry-level composition** — multiple mods safely modify the same PAZ file
- **Semantic merging** — field-level diffing for PABGB data tables
- **Conflict detection** — see exactly what overlaps and why
- **Override mode** — mod authors can declare conflict winners in `modinfo.json`
- **Partial apply opt-in** (v3.2.3) — authors can mark a mod as "apply what fits" for cost-only / scalar tweaks
- **Load order** — drag-and-drop reordering with folder groups
- **Configurable mods** — preset picker for multi-variant mods, per-patch toggle, multi-version pack picker (v3.2.3)

### Game Integration
- **Auto-detection** — finds your game on Steam, Epic Games, or Xbox Game Pass
- **Game update detection** — surfaces the Recovery banner the moment Crimson Desert patches
- **ASI management** — full plugin page with version tracking, enable/disable, config editing
- **Launch game** — start Crimson Desert directly from the manager

### Interface
- **Card-based UI** — Fluent Design with drag-reorder and folder groups
- **Welcome wizard** — guided first-time setup with store logos
- **In-app Patch Notes** — Settings → About → View Patch Notes opens the full version history any time
- **Light & Dark themes** — choose during setup or switch anytime
- **16 languages** — English, Deutsch, Español, Français, 한국어, 日本語, 简体中文, 繁體中文, العربية, Italiano, Polski, Русский, Türkçe, Українська, Bahasa Indonesia, Português

### Safety
- **Apply preview** — see what changes before modifying anything
- **Verify game state** — scan all files, see vanilla vs modded
- **One-click revert** — restores all files including PATHC and PAMTs
- **Crash recovery** — atomic commits with `.pre-apply` markers
- **Find Culprit** — auto-bisect tool that finds which mod crashes the game by toggling halves on and off until stable

---

## Installation

### Standalone Executable (Recommended)

Download `CDUMM3.exe` from the [Releases](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases) page. No Python required. Just run it.

### Run from Source

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
# Output: dist/CDUMM.exe — rename to CDUMM3.exe for distribution
```

---

## Requirements

- Windows 10/11 (or Linux via Wine 11 — see [LINUX.md](LINUX.md), or
  macOS — see [MACOS.md](MACOS.md))
- Crimson Desert from Steam, Epic Games Store, Xbox Game Pass, or
  the native macOS build

> **macOS:** native port (no Wine). Run-from-source today via
> `pip install -e . && python -m cdumm.main`; signed `.app` bundle is
> tracked separately. ASI plugins, the Find-Culprit auto-bisect tool,
> and `nxm://` Mod Manager Download buttons remain Windows-only.

---

## Storage

By default, CDUMM keeps all of its working data (imported mods, vanilla snapshots, overlays, caches) in a `CDMods/` folder next to the game install, so for a Steam install that lands at `E:\SteamLibrary\steamapps\common\Crimson Desert\CDMods\`. This keeps everything next to the game it belongs to and survives moves of the game folder.

### Changing the location

If you want CDMods on a different drive (smaller SSD, dedicated mods drive, network share), open **Settings**, scroll to **Mod storage location**, and click **Change...**. Pick any folder on a writable drive. CDUMM updates the override and migrates the existing `CDMods/` contents to the new path before the next apply.

### Migration safety

The migration is atomic with checksum verification. CDUMM copies every file from the old location to the new one, verifies each copy by hash, and only then removes the source. While this is in progress, a `.cdumm_migration_in_progress` marker file lives at the destination.

If a migration is interrupted partway through (network drive drops out, drive runs out of space, power loss, anything), the marker file stays behind. On the next launch CDUMM sees the marker and surfaces a recovery prompt instead of treating the half-copied destination as the live data. The original source is left intact until every byte at the destination has been verified, so an interrupted migration never loses data.

### Junction workaround (advanced)

If you would rather keep the path stable at the default `<game>\CDMods\` while the actual data lives on another drive, you can use a directory junction:

```
mklink /J "E:\SteamLibrary\steamapps\common\Crimson Desert\CDMods" "D:\CDMods"
```

This is supported but not the recommended path for most users. The Settings override is simpler, has explicit migration with checksums, and survives game folder moves better than a junction does. Use the junction only if you have a specific reason (for example, sharing one CDMods folder across multiple game installs).

---

## For Mod Authors

CDUMM supports these fields in `modinfo.json`:

```json
{
  "name": "My Mod",
  "version": "1.0",
  "author": "You",
  "description": "What it does",
  "conflict_mode": "override",
  "target_language": "ko"
}
```

- `conflict_mode: "override"` — your mod always wins conflicts regardless of load order
- `target_language` — marks the mod as a language/localization mod, shows a badge

### JSON byte-patch flags

```json
{
  "patches": [...],
  "allow_partial_apply": true
}
```

- `allow_partial_apply: true` (v3.2.3) — when some bytes drift after a game patch, CDUMM will apply the verified changes and skip the mismatched ones with a clear log instead of rejecting the whole mod. Useful for cost-only / scalar mods like Refinement Cost Reforged. Default is `false` — mismatches still reject so a half-broken mod can't crash structural data tables.

JSON patches also support `editable_value` metadata for inline value editing in the config panel.

### Field-name JSON mods (Format 3)

CDUMM supports the field-name JSON format (`.field.json`) for these tables: items (`iteminfo.pabgb`), mounts (`vehicleinfo.pabgb`), terrain (`fieldinfo.pabgb`), stages (`stageinfo.pabgb`), regions (`regioninfo.pabgb`), mount character data (`characterinfo.pabgb`), buffs (`buffinfo.pabgb`), drop sets (`dropsetinfo.pabgb`), and skills (`skill.pabgb`). Other tables show a clean "no schema for this table yet" message naming the missing schema. See `field_schema/README.md` to author a schema for an unsupported table.

Both file shapes work: the original singular `{"format": 3, "target": "iteminfo.pabgb", "intents": [...]}` and the newer multi-target `{"format": 3, "targets": [{"file": "...", "intents": [...]}, ...]}` form. The `op` key is optional and defaults to `"set"`.

---

## Credits

- **Lazorr** — PAZ parsing and repacking tools
- **PhorgeForge** — JSON byte-patch mod format
- **993499094** — PATHC texture format reference
- **callmeslinkycd** — Crimson Desert PATHC Tool
- **p1xel8ted** — Performance analysis
- **NattKh** — Field-name JSON mod format reference
- **Potter420 (corin)** — `crimson-rs` ItemInfo schema port (MIT)
- **HaZt** — German translation
- **Kyo-70** — Brazilian Portuguese translation

---

## Support

If CDUMM saves you time, consider supporting development:

[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/kindiboy)

## License

MIT