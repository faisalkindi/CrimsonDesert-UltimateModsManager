## CDUMM v3.1.7.1

Hotfix for a v3.1.7 regression that was rejecting valid mods at import time.

### What's fixed

- **Mods were being falsely flagged as "corrupt PAMT" during import** with a message like `invalid literal for int() with base 10: 'tmp9wk9wy2h'`. The mods themselves were fine — v3.1.7's new import-time PAMT validator was writing to a randomly-named temp file, which confused the parser into rejecting valid files. Reported by @LeoBodnar (issue #38) and @Catarek (issue #37). Affected mods include several Crimson Browser mods (Better Radial Menus, Better Inventory UI, JerK's Map Icons), language packs, and UI mods.

### After updating

1. Replace your old `CDUMM3.exe` with this release.
2. Re-import any mods that were flagged as corrupt in v3.1.7 — they should import cleanly now.
3. If a mod still throws a corrupt-PAMT error after this update, the mod's archive is genuinely damaged; re-download it from Nexus.

### Download

`CDUMM3.exe` attached to this release.
