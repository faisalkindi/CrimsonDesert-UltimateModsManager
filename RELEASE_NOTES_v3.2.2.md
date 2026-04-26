## CDUMM v3.2.2

The follow-up that turns v3.2.1's "we recognize this format" into "we actually apply this format." Plus a Find Culprit fix and a quiet engine improvement that two-mod conflicts will benefit from.

### What's new

- **NattKh-style field-name mods now actually apply.** v3.2.1 started recognizing them and showing a clear message; v3.2.2 ships the engine that runs them. Drop your mod in, click Apply, bytes change. Provided someone in the community has authored a mapping file for the table the mod targets — CDUMM ships the engine, the maps are community-curated. Look in `field_schema/README.md` next to the exe for the format spec and how to author one.

- **Apply tells you when a field-name mod did nothing.** If the mod's intents can't be applied (no matching mapping, missing target file, value out of range), the post-Apply banner names the mod and explains what to do. No more "Apply succeeded but the game looks the same" mystery.

- **Find Culprit is back.** The auto-bisect tool that finds which mod is breaking your game was crashing on click in v3.2 because of a leftover from the v3.0 rewrite. Fixed.

- **Engine fix that quietly improves your two-mod merges.** CDUMM was reading the wrong header width on certain data tables (storeinfo, inventory). Two-mod conflict merges on those tables had been silently no-op'ing since v3.0. Now they produce real merges — you may notice cleaner results when two mods touch the same shop or inventory file.

### For mod authors

If you're authoring or maintaining a field-name JSON mod for Crimson Desert: CDUMM v3.2.2 will run it, but only for tables where there's a `field_schema/<table>.json` mapping shipped (or community-authored). Drop your own mapping file into the `field_schema/` directory next to your CDUMM install and CDUMM picks it up — see `field_schema/README.md` for the format. Schemas authored for JMM v9.9.3 work as-is.

### Upgrade

Replace your `CDUMM3.exe` with the one attached to this release. Your mods, settings, and Nexus login carry over.

**One thing to do after upgrading:** if you imported any field-name JSON mods on v3.2.1 (when CDUMM only recognized them), right-click each → Reimport from source so the stored state reflects the new engine. New imports after the upgrade Just Work.

### Download

`CDUMM3.exe` attached to this release.
