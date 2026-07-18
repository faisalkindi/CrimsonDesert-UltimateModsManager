# Unlocking more tables: extract field order from the macOS binary

The shipped `pabgb_complete_schema.json` lists each table's fields in **memory
order**, not on-disk **read order**, so ~82 of the game's 134 data tables can't
be decoded (see `GAME_DATA_UNLOCK_ROADMAP.md`). The game itself carries the read
order, in its reflection **error strings**:

> `<Class>의 _<field>를 읽어들이는데 실패했다`  — "Failed to read _<field> of <Class>"

`tools/extract_field_order.py` scans a binary for these, groups them per class in
file order, and runs the result through `cdumm.engine.schema_verify` — so the
output is only trusted if it reproduces the 7 tables CDUMM already knows byte-exact.

## Which binary — this matters

| binary | error strings | usable for order? |
|---|---|---|
| **macOS** `CrimsonDesert_Steam-*` | in **read order** | ✅ **yes — use this** |
| Windows `CrimsonDesert.exe` | present, but string-table order ≠ read order | ❌ membership only |

Proven on 2026-07-11: the Windows exe yields correct field *membership* for **505
reflection classes**, but the extractor's own verification **fails all 7 known
tables** on it — because the order is wrong. That failure is the tool working, not
breaking. The macOS binary is specifically the one whose strings are ordered
(unstripped build; this is the method NattKh used to decode `skill.pabgb`).

## Getting the macOS executable

You do **not** need the whole macOS install — only the executable
(`CrimsonDesert_Steam-*`, a few hundred MB).

**Option A — you have a Mac:** install Crimson Desert via Steam, then copy the
executable out of the app bundle
(`.../Crimson Desert.app/Contents/MacOS/CrimsonDesert_Steam-*`).

**Option B — SteamCMD on this PC (no Mac needed):**

```
steamcmd +@sSteamCmdForcePlatformType macos +login <your_steam_account> \
         +app_update 3321460 validate +quit
```

App ID is **3321460**. Forcing the macOS platform makes SteamCMD fetch the macOS
build; once the `CrimsonDesert_Steam-*` binary has downloaded you can stop it — the
bulk `.paz` assets aren't needed. (If you'd rather grab just the binary's depot,
look up the macOS depot + manifest on SteamDB for 3321460 and use
`download_depot 3321460 <depot> <manifest>`.)

## Running it

```
python tools/extract_field_order.py  /path/to/CrimsonDesert_Steam-macos
```

- **✅ VERIFIED** → the extraction reproduced all 7 known tables. The orders for the
  other classes can be trusted enough to add to `pabgb_type_overrides.json`
  (`_ordered_fields`), each still gated to `verified_fields` after a quick in-game
  value spot-check.
- **❌ NOT VERIFIED** → it prints the first divergence per failing table and stops.
  Do not use its output; the order isn't right (wrong binary, or the format shifted).

The script never writes a schema itself — it only proposes and verifies. Turning a
verified extraction into `_ordered_fields` entries is the deliberate next step.
