# Format 3 field_schema — mod author guide

CDUMM ships the engine that applies NattKh-style Format 3 mods. The schema
that maps friendly field names (like `attack`, `price`, `drops`) to byte
positions inside game data files lives **here** — community-authored,
shipped alongside CDUMM.

If you have a Format 3 mod that's reporting "0 byte changes" at apply
time, you (or someone in the community) needs to author or extend the
schema for the table that mod targets.

## File layout

One JSON file per game data table:

```
field_schema/
  iteminfo.json
  storeinfo.json
  dropsetinfo.json
  ... (one per .pabgb table mods target)
```

The filename matches the table name (without the `.pabgb` extension).

## Format

```json
{
  "_note": "underscore-prefixed keys are comments and ignored",

  "attack": {
    "tid": "0xAABBCCDD",
    "value_offset": 5,
    "type": "i32"
  },

  "price": {
    "rel_offset": 12,
    "type": "u32"
  }
}
```

Each entry maps a **friendly name** (the one mod authors put in their
Format 3 intent's `field`) to a write location and value type.

### Two location strategies

**`tid` (recommended for tagged primitives)** — CDUMM searches the entry's
payload for the 4-byte type-id marker, then writes at `tid_position +
value_offset`. Use this when the field's byte position can shift between
records (e.g., when records have variable-length string prefixes).
`value_offset` defaults to 5 (the byte right after the TID + 1-byte type
tag). The TID can be either a JSON string `"0xAABBCCDD"` or an integer.

**`rel_offset` (recommended for fixed-position fields)** — CDUMM writes
at `entry_payload_start + rel_offset`. Use this when the field always
sits at the same byte offset inside every entry of the table.

### Supported `type` values

`i8`, `u8`, `i16`, `u16`, `i32`, `u32`, `f32`, `i64`, `u64`, `f64`.
Match the binary width the field occupies in the game data.

## Validation rules CDUMM applies at load

- Underscore-prefixed keys are **skipped** (treat them as comments).
- Negative `rel_offset` or `value_offset` are **rejected** at load time
  with a warning naming the bad entry.
- Unknown `type` strings are **rejected** at load time.
- Entries with neither `tid` nor `rel_offset` are **dropped** (no way to
  locate the field).
- TID matches that appear more than once inside a single entry's payload
  cause CDUMM to **refuse the write** for that entry — pick a more
  unique TID or switch to `rel_offset`.

## Where CDUMM looks for these files

In order:

1. The path set in the `CDUMM_FIELD_SCHEMA_ROOT` environment variable
   (for power users with hand-edited schemas)
2. Inside CDUMM's bundled exe at `field_schema/`
3. The repo root `field_schema/` directory (for development)

## How to find TIDs

NattKh's `CrimsonDesertModdingTools` repo (github.com/NattKh) ships
parsers and schemas for the major game data tables. The TID values can
be derived from those parsers, or from a memory dump of the game's
field-reader functions. CDUMM doesn't ship a TID extraction tool —
that's NattKh's specialty.

If you author a useful schema, please share it back via a CDUMM GitHub
issue so other users can benefit.

## Example: a minimal storeinfo.json

```json
{
  "_note": "covers store reset hour, the only commonly-modded field",
  "resetHour": {
    "rel_offset": 0,
    "type": "u32"
  }
}
```

A Format 3 mod with `{"field": "resetHour", "op": "set", "new": 6}` on
a `storeinfo.pabgb` target would now apply.

## Troubleshooting

**"0 byte changes" warning after Apply** — The mod's intents reference
field names not in the schema for that table. Either add entries here,
or use the mod's offset-based JSON variant if available.

**Schema entries silently dropped** — Check the CDUMM log file for
`field_schema entry '<name>' in '<table>' …` warning lines. Each
rejected entry logs why (negative offset, unknown type, etc.).

**TID found but bytes don't change in-game** — The TID may be matching
inside another field by coincidence. Run with debug logging to see
what offset the writer landed on, and consider switching to `rel_offset`
for that field.

## Compatibility

CDUMM's field_schema format is intentionally compatible with JMM v9.9.3's
`field_schema/<table>.json` — schemas authored for JMM should drop into
CDUMM's `field_schema/` directory and work unchanged.
