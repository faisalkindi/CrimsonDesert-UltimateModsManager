## CDUMM v3.2

Bundles the v3.1.7.x hotfixes, the NexusMods API integration, and the new Game Update Recovery flow into one release.

### What's new

- **Game Update Recovery.** When Crimson Desert is patched by Steam and your mods break, CDUMM now shows a **Start Recovery** button on the banner that runs the full recovery flow in one click: verify prompt → Fix Everything → rescan → reimport → apply. Mods whose original archive is gone are automatically disabled so Apply never runs against stale patches. This replaces the five manual steps users had to remember after a Steam update. Both the startup "game was updated" path and the later "game files changed" fingerprint check now surface the same Recovery button.

- **NexusMods integration (Phase 1).** Settings → NexusMods Integration card: paste your personal API key and CDUMM checks every 30 minutes for mod updates. Outdated mods get a red "Click To Update" pill; up-to-date mods get a green check. Premium users get one-click downloads; free users get routed to the mod's Files tab on Nexus with CDUMM's application slug approved.

- **Single Sign-On with Nexus.** Click "Login with Nexus" in Settings to authorize CDUMM via your browser without pasting an API key. Uses the Nexus SSO websocket protocol; CDUMM never sees your password.

- **nxm:// protocol handler.** Register CDUMM as the default handler in Settings and "Mod Manager Download" buttons on Nexus pages send the file directly to CDUMM. No more manual drag-drop after downloading.

### Fixes rolled in

- **Import no longer rejects valid mods** (was v3.1.7.1). v3.1.7 was rejecting mods at import time with a "corrupt PAMT" error mentioning a temp-file name like `'tmp9wk9wy2h'`. Root cause: the validator was writing to a randomly-named temp file that confused the parser's filename-derived PAZ index. Fixed by using the mod's real PAMT filename for validation.

- **Apply no longer shows false "corrupt PAMT" warning** (was v3.1.7.2). v3.1.7.1 still showed an "Apply Completed with Warnings" banner after every successful Apply claiming your mods had corrupt PAMT files. The precheck producing that banner was examining the wrong file (the binary patch, not the reconstructed PAMT). The precheck is disabled; the real import and apply flows still catch truly corrupt files.

- **Recovery no longer explodes multi-preset mods into a sea of duplicates.** When a mod was originally imported from a folder with multiple preset JSONs (Glider Stamina ships 5, Infinite Horse ships 9), Recovery's reimport step was sending the FOLDER to the worker. The worker's multi-JSON branch then split each preset into its own mod row — turning one Glider into 5 cards and one Horse into 9. Reimport now resolves the source as `json_source` first (the user's chosen single preset) and only falls back to the folder for PAZ-archive mods. One mod stays one mod.

### After updating

1. Replace your old `CDUMM3.exe` with this release.
2. Optional: open Settings → NexusMods Integration. Click "Login with Nexus" (or paste your personal API key from https://next.nexusmods.com/settings/api-keys). Toggle nxm:// handler if you want Nexus download buttons to send files straight to CDUMM.
3. Launch CDUMM. If Crimson Desert was updated since your last successful apply, the Recovery InfoBar appears automatically — click Start Recovery and let it run.

### Download

`CDUMM3.exe` attached to this release.
