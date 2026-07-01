# Changelog

All released versions of CDUMM, newest first. The project's first commit was **2026-03-26**.
Summaries are condensed; each version links to its full release notes. See the
[Releases](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases) page for complete notes and downloads (the in-app updater also shows them).


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
