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

import os
import re
import sqlite3
import struct
import time
from typing import Any, Callable, Iterable

# Data-table blob (.pabgb) + schema/header (.pabgh) extensions.
TABLE_EXTS = (".pabgb", ".pabgh")

# The game's own verbose reflection-serialized formats — these embed a real
# field / type / object name schema as text (readable via decode_reflection /
# extract_strings). Packed value-only formats (.pabgh/.paatt/.paac/…) and
# third-party binaries (.hkx, .roadsector, …) embed NO name schema, so the
# flat "names" outline must never be mined from them.
REFLECTION_EXTS = (".pae", ".paseq", ".prefab", ".meshinfo", ".paproj")


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
    # One data table == one .pabgb blob (+ its .pabgh key index). Count the
    # blobs only, so the paired header file isn't tallied as a second table.
    distinct = con.execute(
        "SELECT COUNT(DISTINCT name) FROM data_tables "
        "WHERE name LIKE '%.pabgb'").fetchone()[0]
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


def _identify_binary_format(data: bytes) -> str | None:
    """Best-effort identification of a packed binary from its magic — the one
    thing that IS verifiable in these schema-less formats. Returns a human
    label or None. Does NOT name individual fields (those aren't in the file).
    """
    if data[:4] == b"PAR ":            # constant across all .paa in the corpus
        return "Pearl Abyss animation (PAR container)"
    if data[:2] == b"\xff\xff" and b"AnimationMetaData" in data[:64]:
        return "Animation metadata (AnimationMetaData)"
    return None


def decode_struct(data: bytes, max_words: int = 512) -> dict | None:
    """Interpret a small, string-free binary as a table of 32-bit words.

    The game stores fixed-layout structs with no embedded field names for
    per-record attribute blocks (``.paatt``) and table key indexes
    (``.pabgh``). There's no schema to name the fields, but showing each
    4-byte word as uint32 / int32 / float32 is far more legible than a raw
    hex wall: record keys (1,000,000+n), byte offsets, flags and float
    attributes all become readable. Returns ``None`` when there aren't at
    least one whole 4-byte word to show — nothing a struct view adds over hex.

    Each row is ``(offset, hex, uint32, int32, float, ascii, is_key)`` and
    every column is always populated; the float is the exact IEEE-754 reading
    of the same 4 bytes, and ``is_key`` flags values in the
    1,000,000–9,999,999 game-data record-key range.
    """
    nwords = len(data) // 4
    if nwords < 1:                    # need at least one whole 4-byte word
        return None
    shown = min(nwords, max_words)
    rows: list = []
    for k in range(shown):
        off = k * 4
        chunk = data[off:off + 4]
        u = int.from_bytes(chunk, "little")
        i = u - 0x1_0000_0000 if u & 0x8000_0000 else u
        f = struct.unpack_from("<f", data, off)[0]
        # Every column is always populated: the float is the exact IEEE-754
        # reading of the same 4 bytes (a word that's really an int just reads
        # as a tiny/denormal number) — no blank cells.
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        rows.append((f"{off:04X}", chunk.hex(), str(u), str(i), f"{f:.6g}",
                     ascii_, 1_000_000 <= u <= 9_999_999))
    return {"rows": rows, "total_words": nwords, "shown": shown,
            "trailing": len(data) - nwords * 4,
            "format": _identify_binary_format(data)}


# Per-table record payload offset holding a world (X, Y, Z) float triplet,
# VALIDATED individually before being added here (factionnodespawninfo's nodes
# cluster correctly by region at +4). Never guess an offset — it differs per
# table (the +4 that fits factionnodespawninfo yields nothing on
# actionpointinfo / gimmick / sequencer tables). Extend as tables are RE'd.
_TABLE_POSITION_OFFSET = {"factionnodespawninfo": 4}


def decode_table_positions(table: str, body: bytes, header: bytes) -> dict:
    """Best-effort per-record world position (X, Y, Z) for the tables whose
    position offset has been reverse-engineered and validated (see
    ``_TABLE_POSITION_OFFSET``). Returns ``{record_key: (x, y, z)}`` for records
    that have a plausible triplet there, or ``{}`` for tables with no known
    offset. Candidate data for map-makers — accurate for listed tables, absent
    (never guessed) for the rest."""
    import math
    off = _TABLE_POSITION_OFFSET.get(table)
    if off is None:
        return {}
    from cdumm.semantic import parser as sem
    try:
        key_size, offsets = sem.parse_pabgh_index(header, table)
    except Exception:  # noqa: BLE001
        return {}
    if not offsets:
        return {}
    ordered = sorted(offsets.items(), key=lambda kv: kv[1])
    out: dict = {}
    n = len(body)
    for i, (key, start) in enumerate(ordered):
        end = ordered[i + 1][1] if i + 1 < len(ordered) else n
        if start >= n:
            continue
        rec = body[start:end]
        try:
            _eid, _name, pstart = sem._parse_entry_header(rec, 0, key_size)
        except Exception:  # noqa: BLE001
            pstart = key_size
        payload = rec[pstart:]
        if len(payload) < off + 12:
            continue
        x, y, z = struct.unpack_from("<fff", payload, off)
        if (all(math.isfinite(v) for v in (x, y, z))
                and any(0.5 < abs(v) < 1_000_000 for v in (x, y, z))
                and not (abs(abs(x) - 1) < 1e-2 and abs(abs(z) - 1) < 1e-2)):
            out[key] = (round(x, 1), round(y, 1), round(z, 1))
    return out


# ── Wwise audio (.wem / .bnk) ─────────────────────────────────────────
# The game ships ALL sound through Wwise: .wem are the encoded media streams
# (almost always Wwise Vorbis) and .bnk are SoundBanks. Windows can't play a
# raw .wem, and Wwise Vorbis can't be decoded in pure Python — so this is a
# header parse for metadata, paired with vgmstream for actual WAV playback.
WWISE_EXTS = (".wem", ".bnk")
_WEM_CODECS = {0x0001: "PCM", 0x0002: "ADPCM", 0x0011: "IMA ADPCM",
               0x0069: "Wwise IMA", 0x0166: "XMA2",
               0xFFFE: "WAVE extensible", 0xFFFF: "Wwise Vorbis"}


def _parse_wem_header(data: bytes) -> dict | None:
    if data[:4] not in (b"RIFF", b"RIFX") or data[8:12] != b"WAVE":
        return None
    end = ">" if data[:4] == b"RIFX" else "<"
    pos, chunks = 12, []
    codec = channels = sample_rate = bits = data_bytes = None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        csz = struct.unpack_from(end + "I", data, pos + 4)[0]
        chunks.append(cid.decode("ascii", "replace").strip())
        if cid == b"fmt " and csz >= 16 and pos + 8 + 16 <= len(data):
            tag, channels, sample_rate, _br, _ba, bits = \
                struct.unpack_from(end + "HHIIHH", data, pos + 8)
            codec = _WEM_CODECS.get(tag, f"0x{tag:04X}")
        elif cid == b"data":
            data_bytes = csz
        pos += 8 + csz + (csz & 1)
    dur = None
    if codec == "PCM" and channels and sample_rate and bits and data_bytes:
        dur = data_bytes / (channels * (bits // 8) * sample_rate)
    return {"kind": "wem", "codec": codec, "channels": channels,
            "sample_rate": sample_rate, "bits": bits or None,
            "data_bytes": data_bytes, "chunks": chunks,
            "endian": "big" if end == ">" else "little", "duration": dur}


def _parse_bnk_header(data: bytes) -> dict | None:
    if data[:4] != b"BKHD":
        return None
    version = struct.unpack_from("<I", data, 8)[0] if len(data) >= 12 else None
    pos, sections, n_streams = 0, [], 0
    while pos + 8 <= len(data):
        sid = data[pos:pos + 4]
        ssz = struct.unpack_from("<I", data, pos + 4)[0]
        if not (sid.isalpha() and sid.isupper()):
            break
        sections.append(sid.decode("ascii", "replace"))
        if sid == b"DIDX":            # data index: 12 bytes per embedded stream
            n_streams = ssz // 12
        pos += 8 + ssz
    return {"kind": "bnk", "bank_version": version, "sections": sections,
            "embedded_streams": n_streams}


def decode_audio(data: bytes, path: str = "") -> dict | None:
    """Parse a Wwise audio container header into readable metadata, or None if
    it isn't one. Header parse only — the encoded audio needs vgmstream (see
    ``find_vgmstream`` / ``convert_to_wav``) to become a playable WAV."""
    if data[:4] in (b"RIFF", b"RIFX"):
        return _parse_wem_header(data)
    if data[:4] == b"BKHD":
        return _parse_bnk_header(data)
    return None


def find_vgmstream() -> str | None:
    """Locate the vgmstream CLI used to decode Wwise audio to WAV: the copy
    under ``cdumm/tools/vgmstream/`` (searched recursively, since release
    archives may nest it in a subfolder), then anything on PATH."""
    import shutil
    names = ("vgmstream-cli.exe", "vgmstream-cli", "test.exe")
    pkg = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # …/cdumm
    base = os.path.join(pkg, "tools", "vgmstream")
    for name in names:
        cand = os.path.join(base, name)
        if os.path.exists(cand):
            return cand
    if os.path.isdir(base):
        for root, _dirs, files in os.walk(base):
            for name in names:
                if name in files:
                    return os.path.join(root, name)
    return shutil.which("vgmstream-cli") or shutil.which("vgmstream_cli")


VGMSTREAM_RELEASES_API = \
    "https://api.github.com/repos/vgmstream/vgmstream/releases/latest"


def _pick_vgmstream_asset(assets: list, system: str,
                          machine: str = "") -> dict | None:
    """Pick the right release archive for this OS from a GitHub 'assets' list
    (each a dict with 'name' / 'browser_download_url'). Pure and testable."""
    system = (system or "").lower()
    pairs = [(a, str(a.get("name", "")).lower()) for a in assets]

    def find(*must):
        for a, n in pairs:
            if (n.endswith((".zip", ".tar.gz", ".tgz"))
                    and all(m in n for m in must)):
                return a
        return None
    if system.startswith("win"):
        return find("win64") or find("win")
    if system == "linux":
        return find("linux", "cli") or find("linux")
    if system in ("darwin", "mac"):
        return find("mac", "cli") or find("mac")
    return None


def download_vgmstream(dest_dir: str, *, timeout: int = 90) -> tuple:
    """Download the latest vgmstream CLI for this OS from the OFFICIAL
    vgmstream GitHub releases and extract it into ``dest_dir``. Returns
    ``(ok, message)`` — message is the version tag on success, else an error.
    Network + archive extraction only; nothing is executed here."""
    import io
    import json
    import platform
    import tarfile
    import urllib.request
    import zipfile
    os.makedirs(dest_dir, exist_ok=True)

    def _get(url, as_json=False):
        req = urllib.request.Request(
            url, headers={"User-Agent": "CDUMM",
                          "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r) if as_json else r.read()

    try:
        rel = _get(VGMSTREAM_RELEASES_API, as_json=True)
    except Exception as ex:  # noqa: BLE001
        return False, f"Could not reach GitHub: {ex}"
    asset = _pick_vgmstream_asset(
        rel.get("assets", []), platform.system(), platform.machine())
    if not asset:
        return False, f"No vgmstream build published for {platform.system()}."
    url = str(asset.get("browser_download_url", ""))
    if "github.com/vgmstream/vgmstream" not in url:
        return False, "Refusing a non-official download URL."
    try:
        blob = _get(url)
    except Exception as ex:  # noqa: BLE001
        return False, f"Download failed: {ex}"
    name = str(asset.get("name", "")).lower()
    try:
        if name.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                z.extractall(dest_dir)
        elif name.endswith((".tar.gz", ".tgz")):
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
                t.extractall(dest_dir, filter="data")
        else:
            return False, f"Unknown archive type: {asset.get('name')}"
    except Exception as ex:  # noqa: BLE001
        return False, f"Extract failed: {ex}"
    exe = find_vgmstream()
    if not exe:
        return False, "Extracted, but vgmstream-cli wasn't in the archive."
    if not exe.endswith(".exe"):
        try:
            os.chmod(exe, 0o755)
        except OSError:
            pass
    return True, str(rel.get("tag_name", "latest"))


def convert_to_wav(data: bytes, out_wav: str) -> bool:
    """Decode Wwise ``.wem`` bytes to a standard WAV at ``out_wav`` via
    vgmstream. Returns True on success, False if vgmstream is unavailable or
    the decode fails."""
    exe = find_vgmstream()
    if not exe:
        return False
    import subprocess
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".wem", delete=False)
    try:
        tmp.write(data)
        tmp.close()
        subprocess.run([exe, "-o", out_wav, tmp.name],
                       capture_output=True, timeout=60, check=False)
        return os.path.exists(out_wav) and os.path.getsize(out_wav) > 44
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


_NAME_LIKE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")   # engine identifier


def _is_name_like(s: str) -> bool:
    """True only for strings shaped like an engine reflection name — a plain
    identifier (``Sequence``, ``_isAccessLock``, ``bool``) or an asset-path
    reference (contains ``/``).

    Packed value-only formats (.paatt / .pabgh / .paac / …) embed no names —
    their printable-ASCII runs are just random byte noise (``&N$0``, ``;@g#``,
    ``JJ<sR``). Those must NOT be surfaced as a "names" outline, so anything
    carrying punctuation or otherwise not identifier/path-shaped is rejected.
    """
    if _NAME_LIKE.match(s):
        return True
    return "/" in s and any(c.isalpha() for c in s)


def extract_strings(data: bytes, min_len: int = 4, limit: int = 600) -> list:
    """Printable-ASCII runs of at least ``min_len`` chars that look like
    engine names — the embedded field / type / object names in the game's
    reflection-serialized binaries (.paseq, .prefab, .meshinfo, ...).
    De-duplicated in encounter order and capped to ``limit`` — a readable
    structure outline instead of raw hex.

    Only ``_is_name_like`` runs are kept, so packed value-only files don't
    masquerade their random ASCII noise as a structure outline.
    """
    out: list = []
    seen: set = set()
    cur = bytearray()
    for b in data:
        if 32 <= b < 127:
            cur.append(b)
        else:
            if len(cur) >= min_len:
                s = cur.decode("ascii")
                if s not in seen and _is_name_like(s):
                    seen.add(s)
                    out.append(s)
                    if len(out) >= limit:
                        return out
            cur = bytearray()
    if len(cur) >= min_len and len(out) < limit:
        s = cur.decode("ascii")
        if s not in seen and _is_name_like(s):
            out.append(s)
    return out


_REFLECT_ID = re.compile(r"[A-Za-z][A-Za-z0-9]*$")   # class / type identifier


def _raw_ascii_runs(data: bytes, min_len: int = 4, cap: int = 12000) -> list:
    """Ordered ASCII runs WITHOUT de-duplication (unlike ``extract_strings``),
    so the field→type alternation of reflection binaries is preserved."""
    out: list = []
    cur = bytearray()
    for b in data:
        if 32 <= b < 127:
            cur.append(b)
        else:
            if len(cur) >= min_len:
                out.append(cur.decode("ascii"))
                if len(out) >= cap:
                    return out
            cur = bytearray()
    if len(cur) >= min_len and len(out) < cap:
        out.append(cur.decode("ascii"))
    return out


def decode_reflection(data: bytes, limit: int = 4000) -> dict | None:
    """Parse a verbose reflection-serialized binary (.pae / .paseq / .prefab /
    .meshinfo / .paproj / …) into the schema it embeds: the object/class names
    and their ``(field, type)`` pairs, plus any asset references.

    These formats store their own reflection metadata inline as an alternating
    stream — a class name, then ``_field`` followed by its type, repeating, with
    nested objects starting a new class section. Returns ``None`` when it isn't
    one (fewer than 3 field/type pairs), so callers can fall back to the plain
    string outline / hex. Everything here is the engine's OWN names, read
    straight from the file — nothing inferred.
    """
    ss = _raw_ascii_runs(data, 4, cap=limit * 3)
    fields: list = []
    objects: list = []
    refs: list = []
    cur = ""
    i, n = 0, len(ss)
    while i < n and len(fields) < limit:
        s = ss[i]
        if s.startswith("_"):
            fields.append((cur, s, ss[i + 1] if i + 1 < n else ""))
            i += 2
        else:
            nxt = ss[i + 1] if i + 1 < n else ""
            if nxt.startswith("_") and _REFLECT_ID.match(s):
                cur = s
                objects.append(s)
                i += 1
            else:
                if "/" in s:                  # a real asset-path reference
                    refs.append(s)
                i += 1
    if len(fields) < 3:
        return None
    seen: set = set()
    urefs: list = []
    for r in refs:
        if r not in seen:
            seen.add(r)
            urefs.append(r)
    return {"objects": objects, "fields": fields,
            "refs": urefs[:400], "nfields": len(fields)}


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
