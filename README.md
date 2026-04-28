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

## New in v3.2.4

- **Field-name JSON mods finally apply (v3.2.3).** The new format that says "change `cooltime` to 0" instead of "change byte 1632 to `00 00 00 00`" now applies for six game tables: items, mounts, terrain, stages, regions, and mount character data. Previously imports worked but apply produced no changes; now they actually take effect in-game.
- **One broken mod can't kill your whole loadout (v3.2.3).** A single mod with a corrupt change used to abort the entire Apply. Now CDUMM skips just the broken change, names which mod and entry was wrong, and applies everything else cleanly.
- **BC7 texture mods no longer show as rainbow noise (v3.2.4).** Single-mip BC7 textures (UI elements, HUD bars, icons) had a 20-byte header layout mismatch that scrambled pixel decoding. Fixed.
- **Multi-version mod packs now show a picker (v3.2.3).** Mods like CrimsonWings ship five strength levels in one zip; drop the zip, pick a level, done.
- **NexusMods built-in (v3.2).** Sign in once, CDUMM watches every mod you've installed and lights up a red badge when an update lands. Click "Mod Manager Download" on a Nexus page and the file goes straight in.
- **One-click Game Update Recovery (v3.2).** Steam patches Crimson Desert, your mods break? CDUMM catches it on launch and a single button runs the whole repair: verify, refresh every mod against the new game files, reapply.

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
| `.field.json` (field-name) | Field-name JSON mods — six tables covered (items, mounts, terrain, stages, regions, mount character) |
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

- Windows 10/11 (or Linux via Wine 11 — see [LINUX.md](LINUX.md))
- Crimson Desert from Steam, Epic Games Store, or Xbox Game Pass

> **No native macOS build.** CDUMM is a Windows executable. Linux users can
> run it under Wine via the bundled launcher; macOS isn't currently
> supported.

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

CDUMM supports the new field-name JSON format for these tables: items (`iteminfo.pabgb`), mounts (`vehicleinfo.pabgb`), terrain (`fieldinfo.pabgb`), stages (`stageinfo.pabgb`), regions (`regioninfo.pabgb`), and mount character data (`characterinfo.pabgb`). Other tables show a clean "no schema for this table yet" message naming the missing schema. See `field_schema/README.md` to author a schema for an unsupported table.

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