# cd-game-index

A small tool that turns a **Crimson Desert install** into a **searchable SQLite
catalog** of everything the game ships — so modders (and CDUMM itself) can look
up *what* is in the game and *where* it lives, instead of guessing at paths.

On a current install it indexes **~1.66 million assets** across **33 archives**
and catalogs the **266 keyed game-data tables** in about **15 seconds**, into a
~400 MB SQLite file you can query with any SQLite browser or `sqlite3`.

It reads **only the archive indexes** (and, with `--items`, one table's bytes).
It never unpacks or redistributes game assets — you run it against your own
legally-owned install and get a local index of metadata (paths, IDs, sizes).

## How it works

Crimson Desert ships its content in numbered archive folders (`0000/`, `0001/`,
… each with a `0.pamt` index + one or more `N.paz` blobs). Every file the game
loads is an entry in a `.pamt` index that records its path, which `.paz` holds
it, the byte offset, the stored/original size, and compression/encryption flags.

`cd_data_index.py`:

1. Finds every `NNNN/` folder in the install that has a `0.pamt`.
2. Parses each `0.pamt` with CDUMM's own `paz_parse.parse_pamt`
   (`src/cdumm/archive/paz_parse.py`) — the same parser the mod manager uses, so
   the index matches what CDUMM sees.
3. Writes one row per entry into a SQLite `assets` table, and additionally
   records every `.pabgb`/`.pabgh` "game data" table in a `data_tables` table.
4. Builds indexes so lookups by path, extension, category, or archive are fast.

With `--items` it also tries to decode the **item records** themselves: it
extracts `gamedata/iteminfo.pabgb` (+ its `.pabgh` schema companion) and runs it
through the vendored `crimson_rs` parser. This is best-effort — see
[Item records](#item-records-known-limitation) below.

## Requirements

- Python 3.9+
- A CDUMM checkout (for `paz_parse`). When this script sits at
  `tools/cd-game-index/` inside the repo it's found automatically; from a copy
  elsewhere pass `--cdumm-src PATH/TO/cdumm/src`.
- Only for `--items`: `pip install lz4`, plus the vendored `crimson_rs`
  extension that ships in the CDUMM repo.

## Usage

The install folder is **auto-detected** (Steam / Epic / Xbox / macOS / Linux) via
CDUMM's own game-finder, so you don't need to know your path — but you can pass
one to override (e.g. a second copy, or if detection misses it).

```sh
# From inside the repo — auto-detects your install:
python tools/cd-game-index/cd_data_index.py --out cd_gamedata.sqlite

# Point it at a specific install folder instead:
python tools/cd-game-index/cd_data_index.py "D:/SteamLibrary/steamapps/common/Crimson Desert" --out cd_gamedata.sqlite

# Also attempt item-record extraction:
python tools/cd-game-index/cd_data_index.py --out cd_gamedata.sqlite --items

# Running a standalone copy of the script (point --cdumm-src at a CDUMM checkout):
python cd_data_index.py --out cd_gamedata.sqlite --cdumm-src "C:/path/to/CrimsonDesert-UltimateModsManager/src"
```

## What you get — schema

| table | columns | holds |
|---|---|---|
| `assets` | `path, archive, category, ext, paz_file, offset, comp_size, orig_size, compressed, encrypted` | one row per file in the game (~1.66M) |
| `data_tables` | `name, path, archive, orig_size` | the 266 keyed `.pabgb`/`.pabgh` game-data tables |
| `stats` | `key, value` | totals + generation timestamp |
| `items` | `key, string_key, data` | item records — only present if `--items` succeeds |

`category` is the first path segment (`character`, `sound`, `leveldata`,
`object`, `gamedata`, `sequencer`, `ui`, `effect`, …); `paz_file` + `offset` +
`comp_size` tell you exactly where the bytes live if you need to extract them.

## Example queries

```sql
-- Where does a file live?
SELECT path, archive, paz_file, offset, orig_size
FROM assets WHERE path LIKE '%iteminfo%';

-- Every sequencer file (the loading-scene animation scripts mods edit):
SELECT path, archive, orig_size FROM assets WHERE ext = '.paseq';

-- What kinds of files, and how many?
SELECT ext, COUNT(*) n FROM assets GROUP BY ext ORDER BY n DESC LIMIT 20;

-- The biggest game-data tables (item/NPC/quest/skill databases):
SELECT name, orig_size FROM data_tables ORDER BY orig_size DESC;

-- Everything a given archive contains:
SELECT category, COUNT(*) FROM assets WHERE archive = '0008' GROUP BY category;
```

## The game-data tables

All 266 keyed tables live under `gamedata/` in archive `0008` — the ID databases
for the game: `iteminfo` (items), `characterinfo` (NPCs), `questinfo`,
`missioninfo`, `skill`, `dropsetinfo` (loot), `storeinfo` (shops), `buffinfo`,
`faction*`, `knowledgeinfo`, `effectinfo`, `stringinfo`/`localstringinfo` (the
display-name lookups), and ~250 more. `data_tables` catalogs all of them so
they can be targeted next.

## Item records (known limitation)

Decoding the *contents* of those tables (e.g. `item id → name → stats`) needs a
parser whose schema matches the shipped game version. The vendored `crimson_rs`
item parser currently raises a schema-mismatch error on some versions
(`CArray count … exceeds remaining bytes`), so `--items` may report
`skipped: …`. The asset + data-table catalog does **not** depend on it and is
always produced. A version-aware / `.pabgh`-schema-driven record decoder is the
natural next step to turn this into a full item/skill/quest ID browser.

## Notes

- **Version-specific.** A game patch shifts offsets and can change table
  layouts. Regenerate the index after the game updates; `stats` records when it
  was generated.
- **Metadata only.** The catalog stores paths, IDs, sizes and locations — not
  asset bytes. Point it at your own install.
