# Running CDUMM on Linux

CDUMM can run on Linux in two modes:

| Mode | Manager runs as | Pros | Cons |
|------|-----------------|------|------|
| **Native** (recommended) | Python on Linux directly | No Wine prefix; no `vcrun2022` / `corefonts`; no Wine version churn; avoids the Python-3.13-on-Wine hangs reported on some setups | No ASI plugin support |
| **Wine** | `CDUMM3.exe` under Wine 11+ | Full feature parity with the Windows build (incl. ASI) | Wine prefix management; needs Wine 11+ |

Crimson Desert itself runs under Proton/Steam in both modes — only the
manager itself changes. The game's `bin64/CrimsonDesert.exe` is the
same Windows binary either way.

## Quick start — native Linux build (recommended)

```bash
git clone https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager.git
cd CrimsonDesert-UltimateModsManager
./scripts/cdumm-linux-native.sh
```

The launcher provisions a Python virtualenv at `.venv/`, `pip install -e .`s
CDUMM and its dependencies (PySide6, lxml, cryptography, bsdiff4, etc.),
optionally builds the Rust native component for the hashing / batch-import
speedup, then launches the GUI. First run takes a couple of minutes;
subsequent runs are near-instant — the launcher marker-skips the
provisioning steps.

Requirements:

- **Python 3.10 or newer** (3.10–3.14 all work; PySide6 6.11 ships
  stable-ABI wheels via `cp310-abi3`).
- **Rust toolchain** (optional, for the `cdumm_native` extension module
  — ~260× faster hashing, ~60% faster batch imports). Install via
  [rustup](https://rustup.rs/), or pass `--no-native` to the launcher
  to skip and use the pure-Python fallback.

What you give up vs. Wine mode: **ASI plugin support**. ASI mods inject
a Windows DLL (`winmm.dll`) into `CrimsonDesert.exe`, which requires
Wine DLL-override plumbing on Steam's launch options that the native
build deliberately doesn't manage. Texture, XML, JSON, PAZ overlay,
and ReShade mods all work normally. The ASI page renders a "not
supported on Linux" placeholder instead of the scan + card list.

Launcher flags:

```
./scripts/cdumm-linux-native.sh              # provision (first run) + launch
./scripts/cdumm-linux-native.sh --reinstall  # wipe venv, rebuild from scratch
./scripts/cdumm-linux-native.sh --no-native  # skip the Rust build
./scripts/cdumm-linux-native.sh -- --nxm URL # pass args through to CDUMM
```

The NexusMods `nxm://` URL handler is registered from the Settings page
(Settings → Handle nxm:// links → Register). It writes a `.desktop` file
to `~/.local/share/applications/cdumm-nxm.desktop` and runs
`xdg-mime default cdumm-nxm.desktop x-scheme-handler/nxm`. The same page
handles Unregister, and refuses to displace an existing Vortex / MO2
registration without explicit user confirmation.

Steam install paths probed by the auto-detect: `~/.local/share/Steam`,
the `~/.steam/steam` and `~/.steam/root` symlinks (deduped by
`Path.resolve()`), the Flatpak install under
`~/.var/app/com.valvesoftware.Steam/...`, and the Snap install under
`~/snap/steam/...`. The detector parses each install's
`steamapps/libraryfolders.vdf` and locates Crimson Desert in any
listed library — same behaviour as the Windows VDF scan.

## Quick start — Wine mode (legacy)

CDUMM also ships as a Windows executable that can run through **Wine 11
or newer** once its Wine prefix has a couple of Microsoft redistributables
installed. The smoke test (`CDUMM3.exe --worker` boots and emits the
expected JSON) was verified on Ubuntu 24.04.3 LTS with WineHQ 11.0 stable,
`vcrun2022`, and `corefonts`. Choose this path if you need ASI plugin
support.

### Wine quick start

Use the bundled launcher in `scripts/cdumm-linux.sh`:

```bash
# Place CDUMM3.exe next to the script, then:
chmod +x cdumm-linux.sh
./cdumm-linux.sh
```

First launch provisions a dedicated Wine prefix under
`~/.local/share/CDUMM/wineprefix` and installs `vcrun2022` + `corefonts`.
That takes ~3-5 minutes depending on your network. Subsequent launches
skip the provisioning and go straight to Wine.

### Modes

| Command | What it does |
|---------|--------------|
| `./cdumm-linux.sh` | Default. Uses the standalone prefix. |
| `./cdumm-linux.sh --proton` | Runs inside Crimson Desert's own Proton prefix via `protontricks-launch` (AppID 3321460). Requires Protontricks. |
| `./cdumm-linux.sh --reset` | Wipes the standalone prefix so the next run re-provisions. |

### Environment

- `CDUMM_EXE=/path/to/CDUMM3.exe` — override executable location.
- `WINEPREFIX` is set by the script, not read from the environment.

## Required packages

| Distro | Wine | Winetricks | cabextract |
|--------|------|------------|------------|
| Ubuntu 24.04 / Debian | [WineHQ repo](https://wiki.winehq.org/Ubuntu) `winehq-stable` | `apt install winetricks` | `apt install cabextract` |
| Fedora 40+ | [WineHQ repo](https://wiki.winehq.org/Fedora) `winehq-stable` | `dnf install winetricks` | `dnf install cabextract` |
| Arch / SteamOS / Bazzite Desktop | `pacman -S wine` or system wine | `pacman -S winetricks` | `pacman -S cabextract` |
| Bazzite gaming mode | Protontricks is pre-installed, use `--proton` mode | included | included |

> **Ubuntu 24.04 note:** the universe `wine-stable` package is 3.0.1
> and will not work. Install via the official WineHQ repo to get 11.0
> or later.

## Manual setup (if the launcher fails)

Airanath on Nexus posted the original walkthrough that this script
automates. The manual version:

```bash
# 1. One-time prefix setup
export WINEPREFIX=~/.local/share/CDUMM/wineprefix
export WINEDEBUG=-all
wine wineboot --init
winetricks -q vcrun2022 corefonts

# 2. Every launch
export WINEPREFIX=~/.local/share/CDUMM/wineprefix
wine /path/to/CDUMM3.exe
```

## Troubleshooting

**"DLL load failed while importing QtWidgets"**
The prefix is missing the Visual C++ 2015-2022 runtime. Re-run the
launcher (it will no-op if already installed) or manually:
`winetricks -q vcrun2022`. The underlying `ucrtbase.dll` in Wine 10 and
older has unimplemented stubs that Python 3.13 hits — upgrade to Wine
11.0+.

**"winehq-stable has no installation candidate" on Ubuntu**
Your apt sources don't include WineHQ's repo. Follow the repo setup at
<https://wiki.winehq.org/Ubuntu> before retrying.

**CDUMM reports "No worker command specified" and exits**
That's the CLI subprocess mode — it means the binary booted correctly
and needs to be launched without `--worker`. Just run the launcher
without extra arguments.

**Proton prefix mode hangs during vcrun2022 install on NixOS**
Known upstream issue — Protontricks #461. Workaround: install
`d3dcompiler_47` first, then add `--unattended` to the vcrun call.

**File paths are case-sensitive and mods don't import**
Not seen so far, but Linux filesystems are case-sensitive where NTFS is
not. Report on Nexus if a mod works on Windows but not Linux so it can
be reproduced.

## What's not yet supported on Linux

- **ASI loader auto-install.** CDUMM's ASI page writes `winmm.dll` into
  the game's `bin64/`. On Linux this only matters if you're running the
  game itself through Wine/Proton and have ASI plugins — which is an
  edge case. The toggle added in v3.1.4 (Settings → Manage ASI loader)
  can disable the auto-install if it causes friction.
- **Steam launch buttons** open `steam://rungameid/...` URIs via
  `os.startfile`, which under Wine delegates to Wine's own URL handler.
  This works for Steam but not for the Epic and Xbox entry points.

A native Linux build (no Wine) is tracked in the project roadmap but
hasn't started — the Wine-based path above is the supported route today.
