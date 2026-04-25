## CDUMM v3.2.1

A small follow-up to v3.2 with three quality-of-life fixes from the v3.2 ship-day reports. Same v3.2 features (NexusMods integration, one-click Game Update Recovery, faster Apply) plus these.

### Fixes

- **The "update available" banner is now closable.** When CDUMM detected a newer release, the strip at the top of the window had a Download button but no X — and once shown, it stayed all session and reappeared every relaunch. Now there's an X close button on the right. Click it to dismiss the banner. CDUMM remembers your choice per-version, so it won't reappear next launch — but a NEWER release will show fresh. The small badge in the sidebar still appears so you can find the update later from Settings → About if you change your mind.

- **Conflict warnings now name the dropped mod.** When two mods modify the same game file in incompatible ways (one shifts the file size, the other doesn't) CDUMM has to drop one. The old warning just said "1 mod was dropped" — leaving you guessing which. Now it names both sides: "Active: 'Fat Stacks'. Dropped: 'Accessory1Socket'." Plus tells you the exact UI action to change the winner (drag a different mod to the top of the load order).

- **NattKh's Format 3 mods get a clear message.** NattKh's GameMods recently switched to a new field-names JSON format (semantic intents instead of byte offsets). v3.2 rejected those files with a generic "unsupported format" error. v3.2.1 detects Format 3 specifically and shows a "this format is on the roadmap, here's the workaround" message instead. Full support is coming in a future update.

### Upgrade

Replace your `CDUMM3.exe` with the one attached to this release. Your mods, settings, and Nexus login carry over.

### Download

`CDUMM3.exe` attached to this release.
