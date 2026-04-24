## CDUMM v3.1.7.2

Hotfix for a v3.1.7 / v3.1.7.1 regression that showed a scary warning banner after every Apply even though mods were applied correctly.

### What's fixed

- **"Apply Completed with Warnings — has a corrupt 0036/0.pamt" banner on every Apply.** Reported by @LeoBodnar on issue #38 after v3.1.7.1. The apply-time precheck introduced in v3.1.7 was parsing the wrong type of file (the binary patch, not the reconstructed PAMT) and false-flagging every valid PAMT-touching mod as corrupt. Mods were actually applying fine — it was banner noise. The precheck is disabled; the import-time check from v3.1.7.1 still catches truly corrupt mods at the right step.

### After updating

1. Replace your old `CDUMM3.exe` with this release.
2. Apply your mods. The corrupt-PAMT warning banner should not appear anymore.
3. If your mods still misbehave in-game, re-import the affected mod using right-click → "Reimport from source" so it picks up any fixes shipped in v3.1.7.1.

### Download

`CDUMM3.exe` attached to this release.
