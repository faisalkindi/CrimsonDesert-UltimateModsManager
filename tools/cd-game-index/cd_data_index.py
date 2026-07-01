#!/usr/bin/env python3
"""cd_data_index — build a searchable catalog of a Crimson Desert install.

Turns the game's PAZ archives into a SQLite database you can query for:
  * every asset/file the game ships (path, category, type, which archive,
    offset, sizes, compression/encryption) — ~1.6M entries, and
  * the keyed "game data" tables (iteminfo, characterinfo, questinfo,
    skill, dropsetinfo, ...) that hold item/NPC/quest/skill IDs.

It reads only the archive indexes (PAMT) + optionally one data-table's bytes
for the item sample. It never extracts or redistributes game assets — you run
it against your own legally-owned install and get a local index.

Usage:
    # auto-detects your install (Steam / Epic / Xbox / macOS / Linux):
    python cd_data_index.py --out cd_gamedata.sqlite [--items]
    # or point it at a specific install folder:
    python cd_data_index.py "<path to Crimson Desert>" --out cd_gamedata.sqlite

Reuses CDUMM's PAMT parser (src/cdumm/archive/paz_parse.py). When this script
lives at <repo>/tools/cd-game-index/ the parser is found automatically; from a
copy elsewhere, pass --cdumm-src pointing at a CDUMM checkout's ``src`` dir.
``--items`` also needs the vendored ``crimson_rs`` extension + the ``lz4``
package (``pip install lz4``).
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sqlite3
import sys
import time

# This file lives at <repo>/tools/cd-game-index/, so CDUMM's src is two levels
# up. Override with --cdumm-src when running a standalone copy.
DEFAULT_CDUMM_SRC = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))

# Data-table blob (.pabgb) + schema/header (.pabgh) extensions.
TABLE_EXTS = (".pabgb", ".pabgh")


def load_paz_parse(cdumm_src: str):
    """Load CDUMM's stdlib-only PAMT parser as a standalone module."""
    path = os.path.join(cdumm_src, "cdumm", "archive", "paz_parse.py")
    if not os.path.exists(path):
        sys.exit(f"paz_parse.py not found under {cdumm_src} "
                 f"(pass --cdumm-src PATH_TO/cdumm/src)")
    spec = importlib.util.spec_from_file_location("paz_parse", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def archive_dirs(game_dir: str) -> list[str]:
    """NNNN subdirs of the install that carry a 0.pamt index."""
    out = []
    for name in sorted(os.listdir(game_dir)):
        d = os.path.join(game_dir, name)
        if os.path.isdir(d) and os.path.exists(os.path.join(d, "0.pamt")):
            out.append(name)
    return out


def detect_game_dir(cdumm_src: str):
    """Auto-detect the Crimson Desert install using CDUMM's own finder
    (Steam / Epic / Xbox / macOS / Linux). Returns ``(path, note)`` on success
    or ``(None, reason)``. Requires ``cdumm`` to be importable from
    ``cdumm_src`` (the same checkout that provides paz_parse)."""
    if cdumm_src not in sys.path:
        sys.path.insert(0, cdumm_src)
    try:
        from cdumm.storage.game_finder import find_game_directories
    except Exception as ex:  # noqa: BLE001
        return None, f"auto-detect unavailable ({ex})"
    try:
        found = find_game_directories()
    except Exception as ex:  # noqa: BLE001
        return None, f"auto-detect failed ({ex})"
    if not found:
        return None, "no Crimson Desert install found automatically"
    note = f"  (+{len(found) - 1} more found)" if len(found) > 1 else ""
    return str(found[0]), note


def category_of(path: str) -> str:
    return path.split("/", 1)[0] if "/" in path else "(root)"


def build_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        DROP TABLE IF EXISTS assets;
        DROP TABLE IF EXISTS data_tables;
        DROP TABLE IF EXISTS stats;
        DROP TABLE IF EXISTS items;
        CREATE TABLE assets (
            path       TEXT NOT NULL,
            archive    TEXT NOT NULL,   -- NNNN dir
            category   TEXT NOT NULL,   -- first path segment
            ext        TEXT NOT NULL,
            paz_file   TEXT NOT NULL,   -- resolved .paz containing the bytes
            offset     INTEGER NOT NULL,
            comp_size  INTEGER NOT NULL,
            orig_size  INTEGER NOT NULL,
            compressed INTEGER NOT NULL,
            encrypted  INTEGER NOT NULL
        );
        CREATE TABLE data_tables (
            name      TEXT NOT NULL,    -- basename, e.g. iteminfo.pabgb
            path      TEXT NOT NULL,
            archive   TEXT NOT NULL,
            orig_size INTEGER NOT NULL
        );
        CREATE TABLE stats (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )


def index_assets(con, parse_pamt, game_dir, dirs):
    ins = ("INSERT INTO assets(path,archive,category,ext,paz_file,offset,"
           "comp_size,orig_size,compressed,encrypted) "
           "VALUES(?,?,?,?,?,?,?,?,?,?)")
    tab = "INSERT INTO data_tables(name,path,archive,orig_size) VALUES(?,?,?,?)"
    total = 0
    for d in dirs:
        base = os.path.join(game_dir, d)
        try:
            entries = parse_pamt(os.path.join(base, "0.pamt"), paz_dir=base)
        except Exception as e:  # noqa: BLE001
            print(f"  !! {d}: {e}")
            continue
        rows = []
        trows = []
        for e in entries:
            ext = (os.path.splitext(e.path)[1] or "(none)").lower()
            rows.append((e.path, d, category_of(e.path), ext, e.paz_file,
                         e.offset, e.comp_size, e.orig_size,
                         int(e.compressed), int(e.encrypted)))
            if ext in TABLE_EXTS:
                trows.append((os.path.basename(e.path), e.path, d, e.orig_size))
        con.executemany(ins, rows)
        if trows:
            con.executemany(tab, trows)
        total += len(entries)
        print(f"  {d}: {len(entries):,}")
    con.commit()
    return total


def add_indexes(con):
    con.executescript(
        """
        CREATE INDEX ix_assets_path ON assets(path);
        CREATE INDEX ix_assets_ext ON assets(ext);
        CREATE INDEX ix_assets_cat ON assets(category);
        CREATE INDEX ix_assets_archive ON assets(archive);
        CREATE INDEX ix_tables_name ON data_tables(name);
        """
    )
    con.commit()


def try_items(con, parse_pamt, game_dir, dirs, cdumm_src):
    """Best-effort: extract iteminfo.pabgb + its .pabgh companion and parse
    the item records via crimson_rs. Returns (ok, message).

    NOTE: as of writing, the vendored crimson_rs item parser expects a schema
    that does not match every shipped game version, so this may report a parse
    error. The asset + data-table catalog above does not depend on it.
    """
    want = {"iteminfo.pabgb": None, "iteminfo.pabgh": None}
    for d in dirs:
        base = os.path.join(game_dir, d)
        try:
            entries = parse_pamt(os.path.join(base, "0.pamt"), paz_dir=base)
        except Exception:  # noqa: BLE001
            continue
        for e in entries:
            bn = os.path.basename(e.path).lower()
            if bn in want and want[bn] is None:
                want[bn] = e
        if all(v is not None for v in want.values()):
            break
    if want["iteminfo.pabgb"] is None:
        return False, "iteminfo.pabgb not found"

    sys.path.insert(0, os.path.join(cdumm_src, "cdumm", "_vendor", "crimson_rs"))
    try:
        import crimson_rs  # type: ignore
    except Exception as ex:  # noqa: BLE001
        return False, f"crimson_rs import failed: {ex}"
    try:
        import lz4.block  # noqa: F401
    except Exception as ex:  # noqa: BLE001
        return False, f"lz4 missing (pip install lz4): {ex}"

    import tempfile

    def extract(entry, dest):
        with open(entry.paz_file, "rb") as f:
            f.seek(entry.offset)
            raw = f.read(entry.comp_size)
        if entry.comp_size != entry.orig_size and entry.orig_size > 0:
            import lz4.block
            raw = lz4.block.decompress(raw, uncompressed_size=entry.orig_size)
        with open(dest, "wb") as f:
            f.write(raw)

    tmp = tempfile.mkdtemp(prefix="cd_iteminfo_")
    pabgb = os.path.join(tmp, "iteminfo.pabgb")
    extract(want["iteminfo.pabgb"], pabgb)
    if want["iteminfo.pabgh"] is not None:
        extract(want["iteminfo.pabgh"], os.path.join(tmp, "iteminfo.pabgh"))

    items = None
    err = None
    if hasattr(crimson_rs, "parse_iteminfo_from_file"):
        try:
            items = crimson_rs.parse_iteminfo_from_file(pabgb)
        except Exception as ex:  # noqa: BLE001
            err = f"from_file: {ex}"
    if items is None and hasattr(crimson_rs, "parse_iteminfo_from_bytes"):
        try:
            with open(pabgb, "rb") as f:
                items = crimson_rs.parse_iteminfo_from_bytes(f.read())
        except Exception as ex:  # noqa: BLE001
            err = (err + " | " if err else "") + f"from_bytes: {ex}"
    if items is None:
        return False, err or "parse returned None"

    import json
    con.execute("DROP TABLE IF EXISTS items")
    con.execute("CREATE TABLE items (key INTEGER, string_key TEXT, data TEXT)")
    rows = [(it.get("key"), it.get("string_key"), json.dumps(it)[:4000])
            for it in items]
    con.executemany("INSERT INTO items(key,string_key,data) VALUES(?,?,?)", rows)
    con.execute("CREATE INDEX ix_items_key ON items(key)")
    con.execute("CREATE INDEX ix_items_sk ON items(string_key)")
    con.commit()
    return True, f"{len(items):,} item records"


def main():
    ap = argparse.ArgumentParser(description="Index a Crimson Desert install.")
    ap.add_argument("game_dir", nargs="?", default=None,
                    help="path to the Crimson Desert install dir "
                         "(auto-detected if omitted)")
    ap.add_argument("--out", default="cd_gamedata.sqlite")
    ap.add_argument("--cdumm-src", default=DEFAULT_CDUMM_SRC,
                    help="path to a CDUMM checkout's src/ (for paz_parse)")
    ap.add_argument("--items", action="store_true",
                    help="also try to extract iteminfo records (crimson_rs+lz4)")
    args = ap.parse_args()

    pp = load_paz_parse(args.cdumm_src)

    game_dir = args.game_dir
    if not game_dir:
        game_dir, note = detect_game_dir(args.cdumm_src)
        if not game_dir:
            sys.exit(
                f"Could not find a Crimson Desert install automatically "
                f"({note}).\nPass the install folder explicitly, e.g.:\n"
                f'  python cd_data_index.py "D:/SteamLibrary/steamapps/'
                f'common/Crimson Desert"')
        print(f"Auto-detected install: {game_dir}{note}")

    dirs = archive_dirs(game_dir)
    if not dirs:
        sys.exit(f"No NNNN/0.pamt archives under {game_dir}")
    print(f"Archives: {len(dirs)}  ->  {args.out}")

    if os.path.exists(args.out):
        os.remove(args.out)
    con = sqlite3.connect(args.out)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    build_schema(con)

    t0 = time.monotonic()
    total = index_assets(con, pp.parse_pamt, game_dir, dirs)
    add_indexes(con)

    n_tables = con.execute("SELECT COUNT(*) FROM data_tables").fetchone()[0]
    n_distinct = con.execute(
        "SELECT COUNT(DISTINCT name) FROM data_tables").fetchone()[0]
    stats = {
        "assets_total": total,
        "archives": len(dirs),
        "data_table_entries": n_tables,
        "data_table_distinct": n_distinct,
        "generated_epoch": int(time.time()),
    }

    items_msg = "not requested (use --items)"
    if args.items:
        ok, msg = try_items(con, pp.parse_pamt, game_dir, dirs,
                            args.cdumm_src)
        items_msg = ("OK: " + msg) if ok else ("skipped: " + msg)
        stats["items"] = items_msg
    con.executemany("INSERT INTO stats(key,value) VALUES(?,?)",
                    [(k, str(v)) for k, v in stats.items()])
    con.commit()
    con.close()

    dt = time.monotonic() - t0
    size_mb = os.path.getsize(args.out) / 1024 / 1024
    print(f"\nDone in {dt:.0f}s  |  {total:,} assets  |  "
          f"{n_distinct} distinct tables  |  items: {items_msg}")
    print(f"SQLite: {args.out} ({size_mb:.0f} MB)")


if __name__ == "__main__":
    main()
