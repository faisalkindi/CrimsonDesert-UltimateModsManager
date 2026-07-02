"""Game-data index engine.

Turns a Crimson Desert install into a searchable SQLite catalog of every
archive asset (path / category / type / location / size) plus the keyed
"game data" tables (iteminfo, characterinfo, questinfo, skill, ...).

This is a pure library — no GUI, no argparse — so it can back an in-app
"Game Data" page and be unit-tested headless. The heavy enumeration reuses
the same PAMT parser CDUMM uses everywhere else
(``cdumm.archive.paz_parse.parse_pamt``); it is injected in ``build_index`` so
the DB-building and query helpers can be exercised without a real install.

Building the index stores only metadata (paths, sizes, IDs, and where the
bytes live) — never asset bytes. ``extract_asset`` can then read one asset's
real bytes back on demand (decrypt + LZ4-decompress) so a caller can preview
it; ``decode_text`` / ``hexdump`` turn those bytes into something viewable.
"""
from __future__ import annotations

import math
import os
import sqlite3
import struct
import time
from typing import Any, Callable, Iterable

# Data-table blob (.pabgb) + schema/header (.pabgh) extensions.
TABLE_EXTS = (".pabgb", ".pabgh")


def category_of(path: str) -> str:
    """First path segment, e.g. 'gamedata' for 'gamedata/iteminfo.pabgb'."""
    return path.split("/", 1)[0] if "/" in path else "(root)"


def ext_of(path: str) -> str:
    """Lower-cased extension, or '(none)' when the path has none."""
    return (os.path.splitext(path)[1] or "(none)").lower()


def archive_dirs(game_dir: str) -> list[str]:
    """NNNN subdirs of an install that carry a 0.pamt index."""
    out = []
    for name in sorted(os.listdir(game_dir)):
        d = os.path.join(game_dir, name)
        if os.path.isdir(d) and os.path.exists(os.path.join(d, "0.pamt")):
            out.append(name)
    return out


# ── schema / build ───────────────────────────────────────────────────

def create_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        DROP TABLE IF EXISTS assets;
        DROP TABLE IF EXISTS data_tables;
        DROP TABLE IF EXISTS stats;
        CREATE TABLE assets (
            path       TEXT NOT NULL,
            archive    TEXT NOT NULL,
            category   TEXT NOT NULL,
            ext        TEXT NOT NULL,
            paz_file   TEXT NOT NULL,
            offset     INTEGER NOT NULL,
            comp_size  INTEGER NOT NULL,
            orig_size  INTEGER NOT NULL,
            compressed INTEGER NOT NULL,
            encrypted  INTEGER NOT NULL
        );
        CREATE TABLE data_tables (
            name      TEXT NOT NULL,
            path      TEXT NOT NULL,
            archive   TEXT NOT NULL,
            orig_size INTEGER NOT NULL
        );
        CREATE TABLE stats (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )


def insert_archive(con: sqlite3.Connection, archive: str,
                   entries: Iterable[Any]) -> int:
    """Insert one archive's entries.

    ``entries`` is any iterable of objects exposing ``path``, ``paz_file``,
    ``offset``, ``comp_size``, ``orig_size``, ``compressed`` and ``encrypted``
    (i.e. ``cdumm.archive.paz_parse.PazEntry``). Returns the number inserted.
    """
    rows = []
    trows = []
    n = 0
    for e in entries:
        ext = ext_of(e.path)
        rows.append((e.path, archive, category_of(e.path), ext, e.paz_file,
                     e.offset, e.comp_size, e.orig_size,
                     int(bool(e.compressed)), int(bool(e.encrypted))))
        if ext in TABLE_EXTS:
            trows.append((os.path.basename(e.path), e.path, archive,
                          e.orig_size))
        n += 1
    con.executemany(
        "INSERT INTO assets(path,archive,category,ext,paz_file,offset,"
        "comp_size,orig_size,compressed,encrypted) VALUES(?,?,?,?,?,?,?,?,?,?)",
        rows)
    if trows:
        con.executemany(
            "INSERT INTO data_tables(name,path,archive,orig_size) "
            "VALUES(?,?,?,?)", trows)
    return n


def finalize(con: sqlite3.Connection) -> None:
    """Add lookup indexes. Call once after all archives are inserted."""
    con.executescript(
        """
        CREATE INDEX IF NOT EXISTS ix_assets_path ON assets(path);
        CREATE INDEX IF NOT EXISTS ix_assets_ext ON assets(ext);
        CREATE INDEX IF NOT EXISTS ix_assets_cat ON assets(category);
        CREATE INDEX IF NOT EXISTS ix_assets_archive ON assets(archive);
        CREATE INDEX IF NOT EXISTS ix_tables_name ON data_tables(name);
        """
    )


def write_stats(con: sqlite3.Connection, **extra: Any) -> dict:
    """Compute + persist summary stats; return them as a dict."""
    total = con.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    archives = con.execute(
        "SELECT COUNT(DISTINCT archive) FROM assets").fetchone()[0]
    distinct = con.execute(
        "SELECT COUNT(DISTINCT name) FROM data_tables").fetchone()[0]
    stats: dict[str, Any] = {
        "assets_total": total,
        "archives": archives,
        "data_table_distinct": distinct,
        "generated_epoch": int(time.time()),
    }
    stats.update(extra)
    con.execute("DELETE FROM stats")
    con.executemany("INSERT INTO stats(key,value) VALUES(?,?)",
                    [(k, str(v)) for k, v in stats.items()])
    con.commit()
    return stats


# ── query helpers (for the GUI page / callers) ───────────────────────

def _dicts(cur) -> list[dict]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def search_assets(con: sqlite3.Connection, query: str = "", *,
                  ext: str | None = None, category: str | None = None,
                  archive: str | None = None, limit: int = 200) -> list[dict]:
    """Search assets by path substring, optionally filtered by ext/category/
    archive. Returns up to ``limit`` rows as dicts, path-ordered."""
    where = []
    args: list[Any] = []
    if query:
        where.append("path LIKE ? ESCAPE '\\'")
        esc = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        args.append(f"%{esc}%")
    if ext:
        where.append("ext = ?"); args.append(ext.lower())
    if category:
        where.append("category = ?"); args.append(category)
    if archive:
        where.append("archive = ?"); args.append(archive)
    sql = "SELECT path,archive,category,ext,paz_file,offset,orig_size FROM assets"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY path LIMIT ?"
    args.append(int(limit))
    return _dicts(con.execute(sql, args))


def list_data_tables(con: sqlite3.Connection) -> list[dict]:
    """The keyed game-data tables, largest first, de-duplicated by name."""
    return _dicts(con.execute(
        "SELECT name, archive, MAX(orig_size) AS orig_size "
        "FROM data_tables GROUP BY name ORDER BY orig_size DESC"))


def category_counts(con: sqlite3.Connection) -> list[dict]:
    return _dicts(con.execute(
        "SELECT category, COUNT(*) AS n FROM assets "
        "GROUP BY category ORDER BY n DESC"))


def get_stats(con: sqlite3.Connection) -> dict:
    return {k: v for k, v in con.execute("SELECT key,value FROM stats")}


def get_asset(con: sqlite3.Connection, path: str) -> dict | None:
    """Full stored row for one asset path (or None). Includes the fields
    ``extract_asset`` needs that ``search_assets`` omits (comp_size,
    compressed, encrypted)."""
    rows = _dicts(con.execute(
        "SELECT path,archive,category,ext,paz_file,offset,comp_size,"
        "orig_size,compressed,encrypted FROM assets WHERE path=? LIMIT 1",
        (path,)))
    return rows[0] if rows else None


# ── on-demand extraction + preview (read one asset's real bytes) ──────

def _dds_split_decompress(raw: bytes, orig: int) -> bytes | None:
    """Recover a 'DDS-split' texture: a plaintext DDS header + one LZ4 block
    for the pixel body. Returns header + decompressed body, or None if this
    isn't a single-block DDS-split. The header is 128 bytes, or 148 for DX10
    (the extra 20-byte DXGI header)."""
    if raw[:4] != b"DDS " or len(raw) < 128:
        return None
    hdr = 148 if raw[84:88] == b"DX10" else 128
    if orig <= hdr:
        return None
    from cdumm.archive import paz_crypto
    try:
        body = paz_crypto.lz4_decompress(raw[hdr:], orig - hdr)
    except Exception:  # noqa: BLE001 — chunked / unsupported codec
        return None
    return raw[:hdr] + body


def extract_asset(con: sqlite3.Connection, path: str, game_dir: str) -> bytes:
    """Read one asset's real bytes back out of its PAZ.

    Reverses the repack pipeline (plaintext -> LZ4 -> ChaCha20): read the
    stored slice, decrypt if the entry is an encrypted text format, then
    LZ4-decompress if it was compressed. The ``.paz`` is re-resolved under
    ``game_dir`` so an index built before the install moved still works.

    Raises KeyError if the path isn't indexed, FileNotFoundError if the
    archive is gone. A decompress failure (e.g. DDS-split / unsupported
    codec) falls back to the raw stored bytes rather than raising, so the
    caller can still show a hex view.
    """
    row = get_asset(con, path)
    if row is None:
        raise KeyError(path)

    paz = row["paz_file"]
    if not os.path.exists(paz):
        # Index may have been built on another machine / before a move.
        paz = os.path.join(game_dir, row["archive"], os.path.basename(paz))
    if not os.path.exists(paz):
        raise FileNotFoundError(paz)

    with open(paz, "rb") as f:
        f.seek(int(row["offset"]))
        raw = f.read(int(row["comp_size"]))

    from cdumm.archive import paz_crypto
    if row["encrypted"]:
        raw = paz_crypto.decrypt(raw, os.path.basename(path))

    comp, orig = int(row["comp_size"]), int(row["orig_size"])
    if comp != orig and orig > 0:
        try:
            raw = paz_crypto.lz4_decompress(raw, orig)
        except Exception:  # noqa: BLE001
            # Whole-buffer LZ4 failed — try DDS-split (plaintext header + one
            # LZ4 body). If that's not it either (chunked / type 3-4), keep
            # the raw bytes so the caller can still show a hex view.
            split = _dds_split_decompress(raw, orig)
            if split is not None:
                raw = split
    return raw


def decode_text(data: bytes, limit: int = 200_000) -> str | None:
    """Return decoded text if the bytes look like a text file, else None.

    Tries UTF-8 then UTF-16-LE over a leading sample and only accepts the
    result when it's overwhelmingly printable — so XML/CSS/JSON/JS assets
    render as text while binary blobs fall through to a hex view.
    """
    sample = data[:limit]
    if not sample:
        return ""
    for enc in ("utf-8", "utf-16-le"):
        try:
            s = sample.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
        if not s:
            continue
        printable = sum((c.isprintable() or c in "\r\n\t") for c in s)
        if printable / len(s) >= 0.90:
            return s
    return None


def hexdump(data: bytes, limit: int = 4096) -> str:
    """Classic ``offset  hex  ascii`` dump of the first ``limit`` bytes."""
    out = []
    chunk = data[:limit]
    for i in range(0, len(chunk), 16):
        row = chunk[i:i + 16]
        hexs = " ".join(f"{b:02X}" for b in row)
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        out.append(f"{i:08X}  {hexs:<47}  {ascii_}")
    if len(data) > limit:
        out.append(f"… ({len(data):,} bytes total, showing first {limit:,})")
    return "\n".join(out)


def decode_struct(data: bytes, max_words: int = 512) -> dict | None:
    """Interpret a small, string-free binary as a table of 32-bit words.

    The game stores fixed-layout structs with no embedded field names for
    per-record attribute blocks (``.paatt``) and table key indexes
    (``.pabgh``). There's no schema to name the fields, but showing each
    4-byte word as uint32 / int32 / float32 is far more legible than a raw
    hex wall: record keys (1,000,000+n), byte offsets, flags and float
    attributes all become readable. Returns ``None`` when there aren't at
    least two whole words to show — nothing a struct view adds over hex.

    Each row is ``(offset, hex, uint32, int32, float, ascii, is_key)``; the
    float cell is blank when the bit-pattern isn't a sane finite number
    (i.e. it's really an int/flag), and ``is_key`` flags values in the
    1,000,000–9,999,999 game-data record-key range.
    """
    nwords = len(data) // 4
    if nwords < 2:
        return None
    shown = min(nwords, max_words)
    rows: list = []
    for k in range(shown):
        off = k * 4
        chunk = data[off:off + 4]
        u = int.from_bytes(chunk, "little")
        i = u - 0x1_0000_0000 if u & 0x8000_0000 else u
        f = struct.unpack_from("<f", data, off)[0]
        if f == 0.0 or (math.isfinite(f) and 1e-4 <= abs(f) < 1e9):
            fstr = f"{f:.4g}"
        else:  # an int/flag reinterpreted as float is just noise
            fstr = ""
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        rows.append((f"{off:04X}", chunk.hex(), str(u), str(i), fstr,
                     ascii_, 1_000_000 <= u <= 9_999_999))
    return {"rows": rows, "total_words": nwords, "shown": shown,
            "trailing": len(data) - nwords * 4}


def extract_strings(data: bytes, min_len: int = 4, limit: int = 600) -> list:
    """Printable-ASCII runs of at least ``min_len`` chars — the embedded
    field / type / object names in the game's reflection-serialized binaries
    (.paseq, .prefab, .meshinfo, ...). De-duplicated in encounter order and
    capped to ``limit`` — a readable structure outline instead of raw hex."""
    out: list = []
    seen: set = set()
    cur = bytearray()
    for b in data:
        if 32 <= b < 127:
            cur.append(b)
        else:
            if len(cur) >= min_len:
                s = cur.decode("ascii")
                if s not in seen:
                    seen.add(s)
                    out.append(s)
                    if len(out) >= limit:
                        return out
            cur = bytearray()
    if len(cur) >= min_len and len(out) < limit:
        s = cur.decode("ascii")
        if s not in seen:
            out.append(s)
    return out


def _lz4_stream_decode(src: bytes, pos: int, want: int) -> bytes:
    """Decode a continuous LZ4-block byte stream from ``src[pos:]`` until
    ``want`` output bytes are produced. Raises on malformed / truncated
    input. Used to recover the top mip of a compressed multi-mip DDS."""
    out = bytearray()
    while len(out) < want:
        tok = src[pos]; pos += 1
        lit = tok >> 4
        if lit == 15:
            while True:
                bb = src[pos]; pos += 1; lit += bb
                if bb != 255:
                    break
        out += src[pos:pos + lit]; pos += lit
        if len(out) >= want:
            break
        off = src[pos] | (src[pos + 1] << 8); pos += 2
        ml = tok & 15
        if ml == 15:
            while True:
                bb = src[pos]; pos += 1; ml += bb
                if bb != 255:
                    break
        ml += 4
        st = len(out) - off
        for i in range(ml):
            out.append(out[st + i])
    return bytes(out[:want])


def _dds_top_mip_dds(data: bytes) -> bytes | None:
    """Recover a previewable DDS from a compressed multi-mip texture whose
    body is a continuous LZ4 stream (the game's type-1 layout that whole-
    buffer / single-block LZ4 can't open). Decodes just the top mip
    (``dwPitchOrLinearSize`` bytes) and returns a single-mip DDS a standard
    decoder opens, or None if it doesn't apply / decode."""
    if data[:4] != b"DDS " or len(data) < 128:
        return None
    hdr_size = 148 if data[84:88] == b"DX10" else 128
    linear = int.from_bytes(data[20:24], "little")   # mip-0 size in bytes
    if linear <= 0 or linear > 64 * 1024 * 1024:
        return None
    try:
        mip0 = _lz4_stream_decode(data, hdr_size, linear)
    except Exception:  # noqa: BLE001 — not a continuous-LZ4 body
        return None
    hdr = bytearray(data[:hdr_size])
    hdr[28:32] = (1).to_bytes(4, "little")           # dwMipMapCount = 1
    return bytes(hdr) + mip0


_IMAGE_EXTS = (".dds", ".png", ".jpg", ".jpeg", ".bmp", ".tga")


def decode_image(data: bytes, path: str = "", max_dim: int = 1024):
    """Decode an image asset (chiefly the game's standard DDS textures) to a
    small PNG for previewing.

    Returns ``{png, width, height, orig_w, orig_h, mode}`` or None when the
    bytes aren't a decodable image (not an image, an unsupported DDS codec
    such as BC7, or Pillow missing). The output is downscaled so its long
    edge is at most ``max_dim`` — a cheap PNG the GUI can show. Pillow does
    the decode and is called off the UI thread by the preview worker.
    """
    if not (data[:4] == b"DDS " or path.lower().endswith(_IMAGE_EXTS)):
        return None
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 — Pillow is optional
        return None
    import io
    im = None
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:  # noqa: BLE001 — unsupported codec / not an image
        im = None
    if im is None:
        # Compressed multi-mip DDS: the body is a continuous LZ4 stream a
        # standard decoder can't open. Recover just the top mip (full-res)
        # and retry — all a preview needs.
        rebuilt = _dds_top_mip_dds(data)
        if rebuilt is None:
            return None
        try:
            im = Image.open(io.BytesIO(rebuilt))
            im.load()
        except Exception:  # noqa: BLE001
            return None
    ow, oh = im.size
    if im.mode not in ("RGB", "RGBA", "L", "LA"):
        try:
            im = im.convert("RGBA")
        except Exception:  # noqa: BLE001
            return None
    w, h = im.size
    if w and h and max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return {"png": buf.getvalue(), "width": im.size[0], "height": im.size[1],
            "orig_w": ow, "orig_h": oh, "mode": im.mode}


# ── full build (integration entry point) ─────────────────────────────

def build_index(game_dir: str, out_path: str, *,
                parse_pamt: Callable[..., Iterable[Any]] | None = None,
                progress: Callable[[str, int], None] | None = None) -> dict:
    """Build the full catalog for ``game_dir`` into the SQLite at ``out_path``.

    ``parse_pamt`` defaults to CDUMM's real parser but can be injected for
    testing. ``progress(archive, count)`` is called after each archive.
    Returns the stats dict.
    """
    if parse_pamt is None:
        from cdumm.archive.paz_parse import parse_pamt as parse_pamt  # noqa

    dirs = archive_dirs(game_dir)
    if not dirs:
        raise ValueError(f"No NNNN/0.pamt archives under {game_dir}")

    if os.path.exists(out_path):
        os.remove(out_path)
    con = sqlite3.connect(out_path)
    try:
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        create_schema(con)
        for d in dirs:
            base = os.path.join(game_dir, d)
            entries = parse_pamt(os.path.join(base, "0.pamt"), paz_dir=base)
            n = insert_archive(con, d, entries)
            if progress is not None:
                progress(d, n)
        finalize(con)
        stats = write_stats(con)
    finally:
        con.commit()
        con.close()
    return stats
