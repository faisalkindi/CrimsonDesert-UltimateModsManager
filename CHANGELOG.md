# Changelog

All released versions of CDUMM, newest first. The project's first commit was **2026-03-26**.
Summaries are condensed; each version links to its full release notes. See the
[Releases](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases) page for complete notes and downloads (the in-app updater also shows them).


## v3.17

- **[v3.17.6](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.17.6)** -- _2026-09-11_ -- Three of the four call sites into the per-intent writer path dropped warnings_out, so a refusal raised there was discarded: the intent was skipped, nothing was written, and the user was told nothing. All four thread it now. Found while working #414.
- **[v3.17.5](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.17.5)** -- _2026-09-09_ -- A launch blocked by the RUNASADMIN compatibility flag (WinError 740) now names the flag instead of reporting Steam as unreachable, so all three launch methods no longer look identical. Reported by ombre03. (#415)
- **[v3.17.4](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.17.4)** -- _2026-09-08_ -- StageInfo is gated with _verified_fields to the five fields the game's own deserializer confirms. The exe reads three fields the ported schema does not contain and reads eight bytes for _stageCategory where it declares u32, so everything past _sequencerDesc was misaligned; the grid now marks those columns (unverified) and Format 3 refuses to write to them. (#409)
- **[v3.17.3](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.17.3)** -- _2026-09-08_ -- QuestGaugeInfo and AIEventTableInfo each had two fields in the wrong order, derived in August by a tool whose block ordering was missing a leader at the fall-through of a conditional branch. The pair is the same total width either way, so the byte-consumption proof could not see it. Corrected and pinned by the deserializer, the declared widths and the value domains. (#409)
- **[v3.17.2](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.17.2)** -- _2026-09-07_ -- iteminfo reads the four bytes the 4 September update added inside default_sub_item on the type_id 0 shape. 392 of 6,813 item records had stopped decoding, so mods targeting them applied nothing silently; the previous build is unaffected. Spotted by Gleb Kogtev.
- **[v3.17.1](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.17.1)** -- _2026-09-06_ -- Inspect Mod recognises multi-target Format 3 mods instead of reporting them as an unsupported format; it now routes through the importer's own parser and lists each target with its intent count.
- **[v3.17.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.17.0)** -- _2026-09-06_ -- Mods work again after the 4 September game update, which renamed every data table to .staticinfobody and grew the shop stock record by eight bytes. Both namings are now aliases, the new shop layout is derived and pinned, unknown builds are refused instead of mis-read, and switching off a no-op mod no longer wedges Apply. Fixes by Gleb Kogtev.

## v3.16

- **[v3.16.7](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.16.7)** -- _2026-09-02_ -- Rescan then Apply no longer leaves the game unable to start (stale index entry for a deleted mod folder was being restored every apply). Linux/Steam Deck: CDUMM starts under any Wine/Proton version (ICU bundled).
- **[v3.16.6](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.16.6)** -- _2026-09-01_ -- Expanded Vendor Inventory Rebuilt V3 applies, with its Dye Addon and Bran's Specialty Store: DMM's add-to-list operation is accepted, shops can gain stock records and per-slot edits, Dyers can gain colour groups and texture sets, and the dye colour group table is writable.
- **[v3.16.5](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.16.5)** -- _2026-08-31_ -- CDUMM no longer deletes installed 2.0 language packs (Steam's optional voice packs in slots 0036-0040); a scan, rescan or startup health check used to remove them.
- **[v3.16.4](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.16.4)** -- _2026-08-31_ -- Dye Hard applies (new NPC dye-list writer). Greylight Special fully applies (store purchase-currency field pinned). README, CHANGELOG and Linux guide caught up.
- **[v3.16.3](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.16.3)** -- _2026-08-29_ -- No more false "5 issues that may crash the game" after every Apply on 2.0. Rescan no longer sits on "Initiating rescan..." forever. Linux / Steam Deck / Bazzite: CDUMM now finds the game when run under Proton or Wine. Bug reports generated under Wine no longer lead with a bogus "running as administrator" warning (Wine always reports that).
- **[v3.16.2](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.16.2)** -- _2026-08-26_ -- CDUMM now cleans up after interrupted imports.
- **[v3.16.1](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.16.1)** -- _2026-08-26_ -- Mods actually load in game on 2.0.
- **[v3.16.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.16.0)** -- _2026-08-26_ -- Item mods work again on the 26 August game update (2.0). Reset and Rescan work on 2.0. Disabling all your mods now actually removes them from the game. Apply no longer sits at 100% looking hung. "Check for Mod Updates" tells you what it actually checked. Also: apply no longer logs a scary error on game versions whose item layout is unknown, and the Game Data grid reads the 2.0 item table at full depth again.

## v3.15

- **[v3.15.1](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.15.1)** -- _2026-08-24_ -- Store mods no longer apply only part of their edits.
- **[v3.15.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.15.0)** -- _2026-08-20_ -- Item price mods work again. Store mods can add stock again. Mods that identify a record by number alone now import. The Game Data grid says when it cannot read a table. Three more of the game's tables are readable, taking it to 55.

## v3.14

- **[v3.14.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.14.0)** -- _2026-08-13_ -- CDUMM reads 53 of the game's data tables, up from 29. Two table layouts shipped in 3.13.0 were wrong and have been withdrawn. One more field of the wanted-list table is read, taking it to all three.

## v3.13

- **[v3.13.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.13.0)** -- _2026-08-12_ -- CDUMM reads five more of the game's tables correctly. Twenty-two more of the game's tables can be read. Nothing that worked before changes.

## v3.12

- **[v3.12.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.12.0)** -- _2026-08-11_ -- Skill mods work again on the current game. Character Creator's Female Animations starts the game again. Status-group mods apply again.

## v3.11

- **[v3.11.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.11.0)** -- _2026-08-07_ -- A mod can no longer smuggle extra commands past you when CDUMM runs its script. A mod can no longer use an XML patch to stall CDUMM or read files off your disk. Both of these need a mod built on purpose to do it, so this is hardening rather than a fix for anything that has been reported happening. The warning after Apply no longer tells you a mod did nothing when it did.

## v3.10

- **[v3.10.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.10.0)** -- _2026-08-05_ -- CDUMM now tells you when a UI mod is too old for the current game. Status-group mods apply.

## v3.9

- **[v3.9.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.9.0)** -- _2026-08-04_ -- Item mods work again on the August 1 game update. Fixed a crash caused by a half-applied character mod. Four more kinds of mod apply instead of doing nothing. CDUMM finds Xbox and moved game installs it used to reject. Character Creator's race and gender picker appears again. Several places where CDUMM could have written to the wrong bytes now refuse instead.

## v3.8

- **[v3.8.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.8.0)** -- _2026-07-29_ -- Four more kinds of mod now apply instead of doing nothing. A wrong value can no longer be written when the game's layout shifts. A failed mod update can no longer delete the mod you already had. CDUMM reads game files it previously mistook for corrupt. When Apply is locked, it now tells you the real reason.

## v3.7

- **[v3.7.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.7.0)** -- _2026-07-27_ -- Character Creator mods apply again: equipslotinfo's record block size is read from your files instead of an older build's constant (#190), Character Creator 7.6's appearance / model fields are written (#302), and wrapper-nested race / gender packs show the picker. Ultra Hard Mode goes 0 of 5 to 5 of 5 -- nine of the eleven unknown buff-variant lengths derived, so the item walk no longer loses its place. Apply is blocked, not just flagged, after a mid-session game update (#307). Same-named nested folders import on Windows (#191). Variant packs keep the selected option across an update. macOS: window restores after closing the game (#300), Find Culprit Mod works (#299). Skipped-file notices are dark-theme readable and copyable.

## v3.6

- **[v3.6.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.6.0)** -- _2026-07-18_ -- `prefab_data_list` decoded again after the 1.13 relocation, unblocking Equip Everything and AXIOM Mask Mega Collection (#285). New `.cdmod` package format, including partial localization patches (#288, #290). Conflict detection compares record and field, not just filename (#292). Offset mods are re-anchored onto a rebuilt table, or refused with a reason, instead of corrupting it (#293). Long-path zip import fix (#191). Gear stat editor in the Game Data tab. New Format 3 operations: `clone_record`, `new_record`, `delete_record`, `array_append`, `match`. DMM Mod Builder parity: match-all, `$in`, inventory slots, character cooldowns, stat mods. Bug reports no longer report a phantom previous-session crash.

## v3.5

- **[v3.5.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.5.0)** -- _2026-07-08_ -- The Game Data tab: ~1.6M assets indexed and searchable, keyed data tables shown as decoded grids (only byte-verified fields render as values), DDS and Wwise previews. Make a mod from grid edits -- writes a shareable `.field.json` with no hex editor (#242). Item mods work again on game 1.13 via a version-adaptive `iteminfo` decoder; large iteminfo applies no longer stall the watchdog (#252, #248). Compound armor mods install fully (#241).

## v3.4

- **[v3.4.2](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.4.2)** -- _2026-06-30_ -- Text / string mods apply -- variable-length string entries are rewritten by key and the table index rebuilt (#224). The "Missing directory" error when disabling a folder-adding mod is fixed and names the mod (#225).
- **[v3.4.1](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.4.1)** -- _2026-06-23_ -- Item mods work again on game 1.12 (the June 20 item-table relayout, #219). One-click **Update All** button. Large-mod apply no longer killed early by the progress watchdog (#218).
- **[v3.4.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.4.0)** -- _2026-06-17_ -- `equipable_hash` equipment-unlock mods apply (#191). Bare ReShade `.addon64` mods install into bin64 (#202). Folder group survives a mod update (#161). Preset / toggle picker height capped (#196).

## v3.3

- **[v3.3.23](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.3.23)** -- _2026-06-16_ -- storeinfo 1.11 parser; AbyssGearUnlock equip-field resolver.
- **[v3.3.22](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.3.22)** -- _2026-06-13_ -- iteminfo 1.11 parser fix; update-status fixes.
- **[v3.3.21](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.3.21)** -- _2026-06-11_
- **[v3.3.20](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.3.20)** -- _2026-06-10_ -- Hotfix: mixing item mods no longer drops the JSON ones -- the whole-table change rebuilds against the live bytes (#191).
- **[v3.3.19](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.3.19)** -- _2026-06-10_ -- Item mods apply again after the item-table relayout (#182, #191); store `stock_data_list` mods (#183); equip-slot hash lists (#190); encrypted material mods like VAXIS Water Physics (#199); a "Game exe (skip Steam)" launch option; available updates no longer vanish from the list (#194).
- **[v3.3.18](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.3.18)** -- _2026-06-08_ -- Recovery no longer loops after a game update (#163); preset chooser keeps its Install button on screen (#200); character-creator mods ask race / gender first (#190).
- **[v3.3.17](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.3.17)** -- _2026-06-05_ -- Steam Launch works on more setups (#186); characterinfo mesh / model fields apply (#192); failed imports surfaced (#193); Crimson Browser preview textures kept (#193); Find Culprit disabled on Linux / macOS (#195); reimport keeps the original archive (#165).
- **[v3.3.16](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.3.16)** -- _2026-06-02_ -- Downloads and update checks fixed after the 1.09 patch (certifi trust store); self-update freeze properly fixed; mod version respects the manifest.
- **[v3.3.15](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.3.15)** -- _2026-05-29_ -- Self-update freeze fixes (#170/#172); Format 3 import retry for skipped mods (#167); iteminfo socket-field aliases (#171).
- **[v3.3.14](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.3.14)** -- _2026-05-27_
- **[v3.3.13](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.3.13)** -- _2026-05-26_
- **[v3.3.12](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.3.12)** -- _2026-05-23_
- **[v3.3.11](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.3.11)** -- _2026-05-22_
- **[v3.3.10](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.3.10)** -- _2026-05-20_ -- `gamedata/` wrapper import (#146); 4 GB texture-pack guard (#148).
- **[v3.3.9](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.3.9)** -- _2026-05-20_ -- DMM v3.1 missing-key handling; Format 3 macOS writer diagnostic.
- **[v3.3.8](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.3.8)** -- _2026-05-18_ -- Stale overlay-dir cleanup via a marker file (#141).
- **[v3.3.7](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.3.7)** -- _2026-05-18_ -- HAWT regression, updater hang, and macOS Format 3 diagnostic fixes.
- **[v3.3.6](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.3.6)** -- _2026-05-17_ -- Hide-on-launch toggle; HAWT protection; preset persistence; addon64 zip import; malformed-JSON line numbers; Format 3 + legacy collision fix.
- **[v3.3.5](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.3.5)** -- _2026-05-14_
- **[v3.3.4](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.3.4)** -- _2026-05-12_
- **[v3.3.3](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.3.3)** -- _2026-05-12_
- **[v3.3.2](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.3.2)** -- _2026-05-11_
- **[v3.3.1](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.3.1)** -- _2026-05-10_
- **[v3.3.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.3.0)** -- _2026-05-10_

## v3.2

- **[v3.2.16](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.2.16)** -- _2026-05-09_
- **[v3.2.15](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.2.15)** -- _2026-05-09_
- **[v3.2.14](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.2.14)** -- _2026-05-09_ -- hotfix
- **[v3.2.13](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.2.13)** -- _2026-05-09_
- **[v3.2.12](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.2.12)** -- _2026-05-08_ -- hotfix
- **[v3.2.11](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.2.11)** -- _2026-05-08_ -- hotfix
- **[v3.2.10](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.2.10)** -- _2026-05-08_
- **[v3.2.9](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.2.9)** -- _2026-05-05_
- **[v3.2.8.2](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.2.8.2)** -- _2026-05-03_ -- Diagnostic guard for stage_file absolute paths
- **[v3.2.8.1](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.2.8.1)** -- _2026-05-03_ -- Click-To-Update + revert + uninstall fixes
- **[v3.2.8](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.2.8)** -- _2026-05-03_ -- Game patch 1.05 fixes + multi-mod dir collision + launch-game CLI
- **[v3.2.7](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.2.7)** -- _2026-04-30_ -- plain XML drop + dropped-mod naming + robustness sweep
- **[v3.2.6](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.2.6)** -- _2026-04-30_ -- Format 3 list-of-dict writers, ASI/variant fixes, apply diagnostics
- **[v3.2.5](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.2.5)** -- _2026-04-29_ -- Multi-file partial-skip, stale-signature fallback, Format 3 hardening
- **[v3.2.4](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.2.4)** -- _2026-04-28_ -- Field-name JSON basename fix + BC7 texture rainbow-noise fix
- **[v3.2.3](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.2.3)** -- _2026-04-27_ -- Field-name JSON mods finally apply
- **[v3.2.2](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.2.2)** -- _2026-04-26_ -- Crimson Browser warning fix + Nexus update detection overhaul + UI polish
- **[v3.2.1](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.2.1)** -- _2026-04-26_ -- CSS mod crash fix + Format 3 routing + auto-detect improvements
- **[v3.2](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.2)** -- _2026-04-25_ -- NexusMods Integration + One-Click Game Update Recovery

## v3.1

- **[v3.1.7.1](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.1.7.1)** -- _2026-04-24_
- **[v3.1.7](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.1.7)** -- _2026-04-24_ -- Post-update recovery + silent-stuck-apply fixes
- **[v3.1.6](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.1.6)** -- _2026-04-23_
- **[v3.1.5](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.1.5)** -- _2026-04-23_
- **[v3.1.4](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.1.4)** -- _2026-04-21_
- **[v3.1.3](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.1.3)** -- _2026-04-20_
- **[v3.1.1](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.1.1)** -- _2026-04-19_

## v3.0

- **[v3.0.4](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.0.4)** -- _2026-04-17_
- **[v3.0.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v3.0.0)** -- _2026-04-16_

## v2.5

- **[v2.5.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.5.0)** -- _2026-04-13_

## v2.4

- **[v2.4.4](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.4.4)** -- _2026-04-13_
- **[v2.4.3](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.4.3)** -- _2026-04-12_
- **[v2.4.2](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.4.2)** -- _2026-04-12_
- **[v2.4.1](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.4.1)** -- _2026-04-12_
- **[v2.4.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.4.0)** -- _2026-04-12_

## v2.3

- **[v2.3.1](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.3.1)** -- _2026-04-11_
- **[v2.3.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.3.0)** -- _2026-04-10_

## v2.2

- **[v2.2.9](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.2.9)** -- _2026-04-10_
- **[v2.2.8](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.2.8)** -- _2026-04-10_
- **[v2.2.7](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.2.7)** -- _2026-04-10_
- **[v2.2.6](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.2.6)** -- _2026-04-08_
- **[v2.2.5](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.2.5)** -- _2026-04-08_
- **[v2.2.4](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.2.4)** -- _2026-04-08_
- **[v2.2.3](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.2.3)** -- _2026-04-08_
- **[v2.2.2](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.2.2)** -- _2026-04-08_
- **[v2.2.1](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.2.1)** -- _2026-04-08_
- **[v2.2.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.2.0)** -- _2026-04-07_

## v2.1

- **[v2.1.9](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.1.9)** -- _2026-04-07_
- **[v2.1.8](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.1.8)** -- _2026-04-07_
- **[v2.1.2](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.1.2)** -- _2026-04-06_
- **[v2.1.1](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.1.1)** -- _2026-04-05_
- **[v2.1.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.1.0)** -- _2026-04-05_

## v2.0

- **[v2.0.3](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.0.3)** -- _2026-04-04_
- **[v2.0.2](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.0.2)** -- _2026-04-04_
- **[v2.0.1](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.0.1)** -- _2026-04-04_
- **[v2.0.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v2.0.0)** -- _2026-04-04_

## v1.9

- **[v1.9.4](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.9.4)** -- _2026-04-04_
- **[v1.9.3](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.9.3)** -- _2026-04-03_
- **[v1.9.2](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.9.2)** -- _2026-04-03_
- **[v1.9.1](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.9.1)** -- _2026-04-03_
- **[v1.9.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.9.0)** -- _2026-04-03_

## v1.8

- **[v1.8.9](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.8.9)** -- _2026-04-03_
- **[v1.8.8](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.8.8)** -- _2026-04-03_
- **[v1.8.7](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.8.7)** -- _2026-04-03_
- **[v1.8.6](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.8.6)** -- _2026-04-03_
- **[v1.8.5](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.8.5)** -- _2026-04-03_
- **[v1.8.4](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.8.4)** -- _2026-04-03_
- **[v1.8.3](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.8.3)** -- _2026-04-03_
- **[v1.8.2](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.8.2)** -- _2026-04-03_
- **[v1.8.1](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.8.1)** -- _2026-04-03_
- **[v1.8.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.8.0)** -- _2026-04-02_

## v1.7

- **[v1.7.7](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.7.7)** -- _2026-04-02_
- **[v1.7.6](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.7.6)** -- _2026-04-02_
- **[v1.7.5](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.7.5)** -- _2026-04-02_
- **[v1.7.4](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.7.4)** -- _2026-04-02_
- **[v1.7.2](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.7.2)** -- _2026-04-01_
- **[v1.7.1](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.7.1)** -- _2026-04-01_
- **[v1.7.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.7.0)** -- _2026-03-31_

## v1.6

- **[v1.6.3](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.6.3)** -- _2026-03-31_
- **[v1.6.2](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.6.2)** -- _2026-03-31_
- **[v1.6.1](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.6.1)** -- _2026-03-31_
- **[v1.6.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.6.0)** -- _2026-03-31_

## v1.3

- **[v1.3.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.3.0)** -- _2026-03-31_

## v1.2

- **[v1.2.1](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.2.1)** -- _2026-03-31_
- **[v1.2.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.2.0)** -- _2026-03-31_

## v1.1

- **[v1.1.2](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.1.2)** -- _2026-03-31_
- **[v1.1.1](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.1.1)** -- _2026-03-31_

## v1.0

- **[v1.0.9](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.0.9)** -- _2026-03-31_
- **[v1.0.8](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.0.8)** -- _2026-03-31_
- **[v1.0.5](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.0.5)** -- _2026-03-31_
- **[v1.0.4](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.0.4)** -- _2026-03-31_
- **[v1.0.3](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.0.3)** -- _2026-03-31_
- **[v1.0.2](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.0.2)** -- _2026-03-31_
- **[v1.0.0](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v1.0.0)** -- _2026-03-31_

## v0.9

- **[v0.9.8](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v0.9.8)** -- _2026-03-31_
- **[v0.9.7](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v0.9.7)** -- _2026-03-31_
- **[v0.9.6](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v0.9.6)** -- _2026-03-31_
- **[v0.9.5](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v0.9.5)** -- _2026-03-31_
- **[v0.9.4](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v0.9.4)** -- _2026-03-31_
- **[v0.9.3](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v0.9.3)** -- _2026-03-31_

## v0.7

- **[v0.7.8](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/tag/v0.7.8)** -- _2026-03-31_
