# Running CDUMM on macOS

Native macOS support, no Wine. Crimson Desert ships a real macOS build,
so CDUMM reads and writes mod files directly inside the
`Crimson Desert.app` bundle on the host filesystem. Apple Silicon
only — Crimson Desert macOS doesn't run on Intel.

## Install — recommended

Download `CDUMM-<version>-macos-arm64.dmg` from the
[Releases](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases)
page, double-click to mount, drag `CDUMM.app` into `/Applications`.

**First launch — two macOS permission steps**:

1. **Gatekeeper**: macOS will refuse to open the app with "Apple
   cannot check it for malicious software" — the build is ad-hoc
   signed (no Apple Developer ID). Right-click `CDUMM.app` → Open →
   Open. The prompt only appears once; subsequent launches are
   silent. If you prefer to skip the right-click dance entirely:

   ```bash
   xattr -dr com.apple.quarantine /Applications/CDUMM.app
   ```

2. **App Management** (macOS Sonoma 14+ / Sequoia 15+ / 26+):
   modding Crimson Desert means CDUMM has to write inside
   `Crimson Desert.app`'s bundle. Apps launched from Finder need
   *App Management* permission to do that, and the first launch
   will appear to do nothing (CDUMM exits silently because SQLite
   gets EPERM on its database file).
   - Open **System Settings → Privacy & Security → App Management**.
   - Toggle **CDUMM** on. (It only appears in the list after CDUMM
     has tried to launch at least once.)
   - Re-open CDUMM. It should now boot all the way to the welcome
     wizard.

   *Why this happens*: macOS sandboxes Finder-launched apps by
   default and blocks writes into other `.app` bundles unless the
   user explicitly grants permission. Run-from-source via
   `python -m cdumm.main` doesn't trigger this because the terminal
   is already exempt from App Management TCC.

## Install — from source

For development, or if you want to run an unreleased branch:

```bash
# 1. Toolchain (one-time)
brew install python@3.13 rust sevenzip unar

# 2. Build the native Rust extension
git clone https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager.git
cd CrimsonDesert-UltimateModsManager/native
python3 -m pip install --user maturin
python3 -m maturin build --release
python3 -m pip install --user target/wheels/cdumm_native-*.whl

# 3. Install CDUMM itself
cd ..
python3 -m pip install --user -e .

# 4. Run
python3 -m cdumm.main
```

To build a `.app` and `.dmg` from source the same way CI does:

```bash
./scripts/build-macos.sh           # produces dist/CDUMM.app + dist/CDUMM-<version>-macos-arm64.dmg
./scripts/build-macos.sh --no-dmg  # skip the .dmg if you only want the .app
```

The first launch shows the welcome wizard. Pick your
`Crimson Desert.app` (CDUMM walks into it to find the inner
`Contents/Resources/packages/` directory automatically) — or let
auto-detect locate it under `~/Games`, `~/Applications`,
`/Applications`, or your Steam library at
`~/Library/Application Support/Steam/steamapps/common/`.

## What is and isn't supported

| Feature                                 | macOS |
|-----------------------------------------|-------|
| PAZ mods (drag-drop import + apply)     | ✓ |
| `.json` byte-patch mods                 | ✓ |
| `.field.json` field-name mods           | ✓ |
| `.dds` texture mods                     | ✓ |
| `.bnk` Wwise soundbank mods             | ✓ |
| `.bsdiff` / `.xdelta` binary patches    | ✓ |
| Multi-variant pickers, configurable mods | ✓ |
| Mod conflict detection + load order     | ✓ |
| Game Update Recovery banner             | ✓ |
| Find Culprit (auto-bisect crash search) | Windows only — needs Pearl Abyss' `crashpad_handler.exe` infrastructure |
| ASI plugins                             | Not possible — ASI is a Win32-only proxy DLL format. The page is hidden on macOS. |
| `nxm://` Mod Manager Download buttons   | Windows only for now — macOS would need a packaged `.app` to register a URL scheme handler |
| RAR archive import                      | Works out-of-box on most macOS systems via the built-in `/usr/bin/bsdtar`. RAR5 archives with v6 compression need `unar` (`brew install unar`) — the open-source `sevenzip` formula doesn't ship RARLAB's codec, so it can't decode v6 RAR5 files. CDUMM tries 7z, then unar, then bsdtar in sequence. |

## Where CDUMM stores its data

| Path                                                         | What           |
|--------------------------------------------------------------|----------------|
| `~/Library/Application Support/cdumm/`                       | Per-user state (log, single-instance lock, game-dir pointer, welcome-wizard marker) |
| `<Crimson Desert.app>/Contents/Resources/packages/CDMods/`   | Mod database, vanilla backups, deltas, sources, overlay |

Putting `CDMods/` inside the `.app` bundle invalidates the bundle's
code signature. macOS does not re-verify a previously-launched
unsigned app on subsequent launches, so the game still runs — but if
you ever need to re-verify or re-sign the app, take a backup of
`CDMods/` first and copy it back afterwards.

## Game directory

Auto-detect scans:

- `~/Library/Application Support/Steam/steamapps/common/`
  (parses `libraryfolders.vdf` for additional library locations)
- `~/Games/`
- `~/Applications/`
- `/Applications/`

For each location it looks for `Crimson Desert.app` and validates the
install by checking the inner PAZ layout (`0008/0.paz` and
`meta/0.papgt` exist). The Windows-only `bin64/CrimsonDesert.exe`
check does not apply on macOS — the native Mac build doesn't ship a
Windows executable.

## Game launch

The "Launch Game" button calls `open <Crimson Desert.app>` on macOS,
which lets the system handle Steam-overlay attachment and the rest.
If the .app can't be located from your saved game directory (e.g.
you pointed CDUMM at the inner `packages/` directory of a Steam
install), CDUMM falls back to the `steam://rungameid/<APP_ID>` URI.

## Troubleshooting

**"This app cannot be opened" the first time CDUMM modifies the .app**
macOS refuses to launch newly-unsigned apps via Gatekeeper. After
the first apply, right-click `Crimson Desert.app` → Open → Open. The
prompt appears once; after that the game launches normally.

**Crimson Desert reports "missing files" after applying mods**
CDUMM's vanilla backups are atomic but Apple's launch services
sometimes cache the bundle's executable hashes. Quit Crimson Desert,
wait 30 seconds, and relaunch. If the message persists, click
**Fix Everything** in CDUMM to restore the vanilla state.

**`No module named cdumm_native` at startup**
The Rust extension didn't get built or installed. Rerun the
`maturin build` + `pip install` steps in the quick start.
`cdumm_native` is required — it's the LZ4 + ChaCha20 hot path, the
pure-Python fallback is intentionally not available.

**The .app exits silently on first launch with no error window**
You missed the App Management permission step above. Open
**System Settings → Privacy & Security → App Management** and
toggle **CDUMM** on, then re-launch. The crash log at
`~/Library/Application Support/cdumm/crash-pre-qt.log` will show
`sqlite3.OperationalError: unable to open database file` —
that's macOS denying CDUMM write access to the game's `.app`
bundle.

**Old mod source paths point at `Z:\` (Windows VM holdover)**
If you previously ran CDUMM in a Windows VM with the macOS host
mounted as `Z:\`, your existing `cdumm.db` has source paths that
look like `Z:\Games\Crimson Desert.app\...`. Native CDUMM can't
resolve those. The mod rows still load and apply (the deltas are
already on disk), but reimport-from-source and "open source folder"
won't work for those rows. Re-drop the original archive on top of
the existing card to relink.

## What's next

- **Notarisation**: would suppress the first-launch Gatekeeper prompt
  but requires a paid Apple Developer ID. Tracked but no concrete
  plan; ad-hoc signing is the realistic compromise for a free tool.
- **`nxm://` URL scheme handler** via
  `LSSetDefaultHandlerForURLScheme`. Now possible because the .app
  has a real `CFBundleIdentifier`; the runtime registration code in
  `cdumm/engine/nxm_handler.py` would grow a macOS branch parallel
  to the Windows registry path.
