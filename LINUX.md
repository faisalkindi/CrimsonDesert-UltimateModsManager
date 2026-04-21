# Running CDUMM on Linux

CDUMM ships as a Windows executable. On Linux it runs through **Wine 11 or
newer** once its Wine prefix has a couple of Microsoft redistributables
installed. The smoke test (`CDUMM3.exe --worker` boots and emits the
expected JSON) was verified on Ubuntu 24.04.3 LTS with WineHQ 11.0 stable,
`vcrun2022`, and `corefonts`.

## Quick start

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
