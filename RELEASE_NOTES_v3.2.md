## CDUMM v3.2

**CDUMM and Nexus Mods are now best friends.** Sign in once and CDUMM keeps an eye on every mod you've installed. When an author pushes an update you see a red badge on the card. Click "Mod Manager Download" on a Nexus page and it lands straight in CDUMM. Game update broke your mods? One button puts everything back together.

This is a big release. Take a minute to read it.

### Nexus Mods, built in (the headline)

**One-click sign-in.** Open Settings, hit **Login with Nexus**, your browser pops up, you confirm it's you, done. CDUMM is connected to your Nexus account. No API keys to copy and paste. No accounts to make. CDUMM never sees your password.

**CDUMM tells you when your mods need updating.** Every 30 minutes CDUMM quietly checks Nexus for new versions of the mods you have installed. Outdated mods get a bright red **Click To Update** badge. Up-to-date mods get a quiet green check. The first check runs right after you sign in so you can see immediately what's stale.

**Download from Nexus with one click.** On any mod's Nexus page, the "Mod Manager Download" button now sends the file straight to CDUMM. No more saving the zip to your Downloads folder and dragging it in.

- **Premium Nexus members** get the full one-click experience — file lands in CDUMM, imports automatically.
- **Free Nexus members** get sent to the mod's Files tab so you can grab the right file fast — still saves you several clicks.

**Cleaner Settings page.** Login is the recommended path and it leads. The old manual API key paste is still there for power users — tucked behind an "Advanced" toggle so it doesn't clutter the page.

### Game updates can't break your day anymore

Steam patches Crimson Desert overnight, you launch the game tomorrow, mods are broken. CDUMM now catches that on startup and offers you a **Start Recovery** button.

One click runs the whole repair: verify your game files, regenerate every mod against the new game version, and reapply them. The five-step manual recipe you used to memorize is gone.

- **Catches both kinds of break.** A normal Steam patch — or any other change to your game files (antivirus rewriting things, half-finished Steam Verify, third-party tool drops) — both trigger the same Recovery banner.
- **You see exactly where it is.** Step 1 of 4 → Step 4 of 4 with a progress bar. Cancel any time.
- **Mods that can't be auto-recovered get safely disabled** instead of corrupting your save. CDUMM tells you which ones, and you can drop their original archive back in to fix them.

### Apply is way faster

Apply used to crawl on big mod sets — sometimes sitting at 0% on one file forever. Two engine rewrites:

- **Conflict detection** is near-instant even with a hundred mods touching the same files.
- **Merging mod changes** runs hundreds of times faster. Files that took minutes now take seconds.
- **The progress bar tells the truth.** Real progress every step of the way, no more frozen "95%" mystery.

### Other things you'll notice

- **No more false "corrupt PAMT" warning** popping up after every Apply when nothing was actually wrong.
- **Mods stop multiplying during Recovery.** Some mods ship multiple presets in one archive (Glider Stamina with 5 options, Infinite Horse with 9). Recovery used to clone each preset into its own card. One mod is one mod again.
- **No more black command-prompt windows flashing** while CDUMM is working.
- **Recovery doesn't get stuck at Step 3** anymore.
- **Recovery doesn't false-alarm.** A bug was making the Recovery banner appear on every launch right after a successful Apply. Fixed.
- **Cleaner error messages.** When a mod fails you see *which* mod failed instead of a generic placeholder.

### Nexus update-checking polish

A bunch of small fixes to the new update-checking system:

- The outdated count in the bottom corner matches what you see on the cards.
- Updating an outdated mod clears the red badge instantly — no waiting for the next check.
- Importing the same mod twice from Nexus stops creating duplicate cards.
- "Mod Manager Download" no longer crashes if you click it before a previous download finishes.
- Mod files with `.zip` or `.7z` baked into their name parse correctly now.
- Browsers that fire the download link twice (some do) get the second click ignored if it lands within 10 seconds.

### ASI plugin fixes

For DLL-based mods (Free Camera, Better Controller Remap, etc.):

- Uninstalling a plugin now cleans up its `.ini` and other sidecar files too — no leftover ghosts.
- If a plugin author renames their `.asi` file in a new version, CDUMM removes the old one for you.
- Plugin `.ini` files match correctly even when authors put the version number in the file name.
- The outdated badge on a plugin clears the moment you update it.

### After updating

1. **Replace your old `CDUMM3.exe`** with this one.
2. **Connect to Nexus.** Settings → NexusMods Integration → click **Login with Nexus** (takes about 5 seconds in your browser). While you're there, flip on the "Mod Manager Download" handler so Nexus pages send downloads straight to CDUMM.
3. **Launch CDUMM.** If your game files have changed since your last apply, the yellow Recovery banner appears — click Start Recovery and let it do its thing.

### Download

`CDUMM3.exe` attached to this release.
