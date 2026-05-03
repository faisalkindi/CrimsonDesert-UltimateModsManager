"""Overlay PAZ builder for Crimson Desert.

Builds a fresh overlay PAZ + PAMT from ENTR delta entries. The overlay
directory replaces modifying original game files in-place. The game loads
entries from the overlay directory first, leaving vanilla files untouched.

Matches JSON Mod Manager's BuildMultiPamt format exactly.
"""

import bisect
import logging
import struct
from dataclasses import dataclass, field
from typing import Optional

from cdumm.archive.hashlittle import hashlittle
from cdumm.archive.paz_crypto import lz4_compress

logger = logging.getLogger(__name__)

HASH_SEED = 0xC5EDE
PAZ_ALIGNMENT = 16
PAMT_CONSTANT = 0x610E0232  # from JSON MM: 1628308018u

# Cache for full path maps (per pamt_dir)
_path_map_cache: dict[str, dict[str, str]] = {}

# Cache for (filename, comp_type) lists per pamt_dir, used by the
# vanilla-flag-inheritance fallback below.
_ext_comp_cache: dict[str, list[tuple[str, int]]] = {}

# Extension → compression type mapping (from CRIMSON_DESERT_MODDING_BIBLE.md §4)
_EXT_COMP_TYPE: dict[str, int] = {
    ".dds": 1,   # DDS texture: 128-byte header + LZ4 body
    ".bnk": 0,   # Wwise soundbank: raw uncompressed
}

# DDS format tables — ported 1:1 from JMM V9.9.1 (MPL-2.0, ModManager.cs).
_LAST4_BY_FOURCC = {
    b"DXT1": 12,
    b"DXT2": 15, b"DXT3": 15,
    b"DXT4": 15, b"DXT5": 15,
    b"ATI1": 4, b"BC4U": 4, b"BC4S": 4,
    b"ATI2": 4, b"BC5U": 4, b"BC5S": 4,
}
_LAST4_BY_DXGI = {
    70: 12, 71: 12, 72: 12,
    73: 15, 74: 15, 75: 15,
    76: 15, 77: 15, 78: 15,
    79: 4,  80: 4,  81: 4,
    82: 4,  83: 4,  84: 4,
    94: 4,  95: 4,  96: 4,
    97: 15, 98: 15, 99: 15,
}
_BC_BLOCK_BYTES_BY_FOURCC = {
    b"DXT1": 8, b"ATI1": 8, b"BC4U": 8, b"BC4S": 8,
    b"DXT3": 16, b"DXT5": 16, b"ATI2": 16, b"BC5U": 16, b"BC5S": 16,
}
_BC_BLOCK_BYTES_BY_DXGI = {
    70: 8, 71: 8, 72: 8, 73: 16, 74: 16, 75: 16, 76: 16, 77: 16, 78: 16,
    79: 8, 80: 8, 81: 8, 82: 16, 83: 16, 84: 16, 94: 16, 95: 16, 96: 16,
    97: 16, 98: 16, 99: 16,
}


def _infer_comp_type_from_extension(filename: str) -> int:
    """Infer PAZ compression type from file extension.

    Returns 1 for DDS textures, 0 for BNK soundbanks, 2 (LZ4) for everything else.
    """
    dot = filename.rfind(".")
    if dot >= 0:
        ext = filename[dot:].lower()
        return _EXT_COMP_TYPE.get(ext, 2)
    return 2


def infer_comp_type_from_pamt(
    pamt_entries: list[tuple[str, int]], ext: str
) -> int | None:
    """Find the first PAMT entry whose filename ends with ``ext`` and
    return its compression type. Case-insensitive.

    Used as a smarter fallback than the hardcoded extension map: when
    a mod ships a NEW file (not in vanilla) whose extension isn't in
    ``_EXT_COMP_TYPE``, scanning a real PAMT for the same extension
    gives an accurate answer (the engine itself cares about flags
    matching whatever shape the loader expects for that file type).

    Args:
        pamt_entries: list of ``(filename, comp_type)`` tuples taken
            from a vanilla PAMT (typically the ``pamt_dir`` the new
            entry will land in).
        ext: extension to look up, including the leading dot
            (e.g. ``".dds"``). Empty string returns None.

    Returns:
        The first matching entry's comp_type, or None if no match.
    """
    if not ext:
        return None
    lo = ext.lower()
    for filename, comp_type in pamt_entries:
        # Filenames without an extension (no dot, or trailing dot) never
        # match a non-empty extension query.
        if "." not in filename:
            continue
        if filename.lower().endswith(lo):
            return comp_type
    return None


@dataclass
class OverlayEntry:
    dir_path: str       # folder path in PAMT (e.g. "gamedata", "ui")
    filename: str       # file basename (e.g. "inventory.pabgb")
    paz_offset: int     # offset in the overlay PAZ
    comp_size: int      # compressed size in PAZ
    decomp_size: int    # decompressed size
    flags: int          # ushort flags (compression_type, no encryption)
    # DDS metadata (populated for comp_type==1 entries) so PATHC update can
    # read the exact reserved1 / last4 values the overlay PAZ carries without
    # re-slicing the paz buffer.
    dds_m_values: Optional[tuple[int, int, int, int]] = None
    dds_last4: int = 0


def _build_dds_partial_payload(
    dds_bytes: bytes,
) -> tuple[bytes, tuple[int, int, int, int]]:
    """Port of JMM ``BuildPartialDdsPayload`` (ModManager.cs:4935-5037).

    Returns ``(payload_bytes, (m1, m2, m3, m4))``. The payload's bytes 32..47
    are already stamped with the m-values; bytes 48..75 are zeroed.

    For an unsupported format (unknown block bytes) or a non-DDS input, the
    original bytes are returned unchanged with m=(0,0,0,0).

    Layout of the returned payload:

      - ``flag3=True`` (inner-LZ4 single chunk — standard case):
          ``header + chosen_first_mip + rest_of_ddsData_after_first_mip_end``
          where ``chosen_first_mip`` is the LZ4-compressed first mip if it
          compresses smaller than raw, else the raw first mip.
          ``m1 = len(chosen), m2 = first_mip_raw, m3 = mip2 (or 0), m4 = mip3 (or 0)``

      - ``flag3=False`` (multi-chunk raw):
          ``header + mip1_raw + mip2_raw + mip3_raw + mip4_raw + rest``
          ``m1..m4 = per-mip raw sizes``
    """
    if len(dds_bytes) < 128 or dds_bytes[:4] != b"DDS ":
        return dds_bytes, (0, 0, 0, 0)

    height, width = struct.unpack_from("<II", dds_bytes, 12)
    depth = struct.unpack_from("<I", dds_bytes, 24)[0]
    mip_count = struct.unpack_from("<I", dds_bytes, 28)[0] or 1
    fourcc = dds_bytes[84:88]
    field_112 = struct.unpack_from("<I", dds_bytes, 112)[0]

    is_dx10 = fourcc == b"DX10" and len(dds_bytes) >= 148
    header_size = 148 if is_dx10 else 128
    dxgi = struct.unpack_from("<I", dds_bytes, 128)[0] if is_dx10 else None
    array_size = (
        struct.unpack_from("<I", dds_bytes, 140)[0] if is_dx10 else 1
    )

    block_bytes = _BC_BLOCK_BYTES_BY_FOURCC.get(fourcc)
    if block_bytes is None and dxgi is not None:
        block_bytes = _BC_BLOCK_BYTES_BY_DXGI.get(dxgi)
    if not block_bytes:
        # Unsupported format — JMM ships raw bytes unchanged.
        logger.debug("DDS unsupported format fourcc=%r dxgi=%r — raw payload",
                     fourcc, dxgi)
        return dds_bytes, (0, 0, 0, 0)

    # Per-mip size table (BC block math), up to max(4, mip_count) slots.
    mip_slots = max(4, mip_count)
    mip_sizes = [0] * mip_slots
    w = max(1, width)
    h = max(1, height)
    for i in range(min(mip_slots, mip_count)):
        mip_sizes[i] = (
            max(1, (w + 3) // 4)
            * max(1, (h + 3) // 4)
            * block_bytes
        )
        w = max(1, w // 2)
        h = max(1, h // 2)

    # flag3: selects inner-LZ4 single-chunk vs multi-chunk raw layout.
    not_dx10_or_array_small = (not is_dx10) or array_size < 2
    multi_chunk_rawable = (mip_count > 5) and (field_112 == 0) and (depth < 2)
    flag3 = (not not_dx10_or_array_small) or (not multi_chunk_rawable)

    header = bytearray(dds_bytes[:header_size])
    if depth == 0:
        struct.pack_into("<I", header, 24, 1)  # force depth>=1 in shipped header

    out = bytearray()
    out += header

    m = [0, 0, 0, 0]

    if flag3:
        first_mip_size = mip_sizes[0]
        first_mip_end = header_size + first_mip_size
        first_mip = bytes(dds_bytes[header_size:first_mip_end])
        compressed = lz4_compress(first_mip)
        chosen = compressed if len(compressed) < len(first_mip) else first_mip
        m[0] = len(chosen)
        m[1] = first_mip_size
        if mip_count > 1:
            m[2] = mip_sizes[1]
        if mip_count > 2:
            m[3] = mip_sizes[2]
        out += chosen
        if first_mip_end < len(dds_bytes):
            out += dds_bytes[first_mip_end:]
    else:
        cursor = header_size
        for j in range(min(4, mip_count)):
            size = mip_sizes[j]
            m[j] = size
            end = cursor + size
            out += dds_bytes[cursor:end]
            cursor = end
        if cursor < len(dds_bytes):
            out += dds_bytes[cursor:]

    # Stamp reserved1 = [m1, m2, m3, m4] and zero reserved1[4..10].
    struct.pack_into("<4I", out, 32, m[0], m[1], m[2], m[3])
    struct.pack_into("<7I", out, 48, 0, 0, 0, 0, 0, 0, 0)

    return bytes(out), (m[0], m[1], m[2], m[3])


def _get_dds_format_last4(dds_bytes: bytes) -> int:
    """Port of JMM ``GetDdsFormatLast4`` — returns the expected last4 from
    the DDS header's format, or 0 if format unrecognised."""
    if len(dds_bytes) < 92:
        return 0
    fourcc = dds_bytes[84:88]
    if fourcc == b"DX10" and len(dds_bytes) >= 132:
        dxgi = struct.unpack_from("<I", dds_bytes, 128)[0]
        return _LAST4_BY_DXGI.get(dxgi, 0)
    return _LAST4_BY_FOURCC.get(fourcc, 0)


# Cache for loaded vanilla PATHC (per game_dir) so we read it once per apply.
_pathc_cache: dict[str, object] = {}


def _get_pathc_last4_for_path(vanilla_pathc_path, virtual_path: str) -> int:
    """Look up the vanilla PATHC last4 for a given virtual path.

    Mirrors JMM's ``GetPathcDdsLast4``. Returns 0 if PATHC is missing, the
    path isn't in PATHC, or the DDS record is too short.
    """
    if vanilla_pathc_path is None:
        return 0
    cache_key = str(vanilla_pathc_path)
    pathc = _pathc_cache.get(cache_key)
    if pathc is None and vanilla_pathc_path.exists():
        try:
            from cdumm.archive.pathc_handler import read_pathc
            pathc = read_pathc(vanilla_pathc_path)
            _pathc_cache[cache_key] = pathc
        except Exception as e:
            logger.debug("PATHC read failed (%s): %s", vanilla_pathc_path, e)
            return 0
    if pathc is None:
        return 0

    from cdumm.archive.pathc_handler import get_path_hash
    vpath = "/" + virtual_path.replace("\\", "/").strip().strip("/")
    h = get_path_hash(vpath)
    idx = bisect.bisect_left(pathc.key_hashes, h)
    if idx >= len(pathc.key_hashes) or pathc.key_hashes[idx] != h:
        return 0
    me = pathc.map_entries[idx]
    dds_idx = me.selector & 0xFFFF
    if not (0 <= dds_idx < len(pathc.dds_records)):
        return 0
    rec = pathc.dds_records[dds_idx]
    if len(rec) < 128:
        return 0
    return struct.unpack_from("<I", rec, 124)[0]


def _reset_pathc_cache() -> None:
    """Clear the vanilla-PATHC cache. Called at the start of each apply to
    avoid stale data if the user swaps the vanilla backup."""
    _pathc_cache.clear()
    _ext_comp_cache.clear()


def _build_ext_comp_list(pamt_dir: str, game_dir) -> list[tuple[str, int]]:
    """Return a list of ``(filename, comp_type)`` from the vanilla PAMT
    for ``pamt_dir``. Cached per pamt_dir per apply.

    Used by the fallback path that infers a NEW file's comp_type from a
    vanilla neighbor with the same extension.
    """
    if pamt_dir in _ext_comp_cache:
        return _ext_comp_cache[pamt_dir]

    from pathlib import Path
    from cdumm.archive.paz_parse import parse_pamt

    out: list[tuple[str, int]] = []
    game_dir = Path(game_dir)
    for base in [game_dir / "CDMods" / "vanilla", game_dir]:
        pamt_path = base / pamt_dir / "0.pamt"
        if not pamt_path.exists():
            continue
        try:
            for entry in parse_pamt(str(pamt_path)):
                filename = entry.path.rsplit("/", 1)[-1] if "/" in entry.path else entry.path
                out.append((filename, entry.compression_type))
            break
        except Exception as e:
            logger.debug("ext-comp PAMT scan failed (%s): %s", pamt_path, e)
            continue

    _ext_comp_cache[pamt_dir] = out
    return out


def _build_full_path_map(pamt_dir: str, game_dir) -> dict[str, str]:
    """Build a map of flattened_path -> full_folder_path from the vanilla PAMT.

    The PAMT stores files with full hierarchical folder paths in its folder
    records (via folder tree references). parse_pamt flattens these to
    top-level-folder/filename, but the game uses the full path for lookups.

    Returns: {flattened_entry_path: full_folder_path}
    """
    from pathlib import Path
    from cdumm.archive.paz_parse import parse_pamt

    game_dir = Path(game_dir)
    result = {}

    for base in [game_dir / "CDMods" / "vanilla", game_dir]:
        pamt_path = base / pamt_dir / "0.pamt"
        if not pamt_path.exists():
            continue

        data = pamt_path.read_bytes()
        if len(data) < 24:
            continue

        try:
            # Skip header: hash(4) + paz_count(4) + hash2(4) + zero(4)
            off = 16
            # Skip PAZ table entries
            pc = struct.unpack_from('<I', data, 4)[0]
            for i in range(pc):
                off += 8  # hash + size
                if i < pc - 1:
                    off += 4  # separator

            # Folder section — build hierarchical folder tree
            folder_len = struct.unpack_from('<I', data, off)[0]; off += 4
            folders = {}
            foff = off
            while foff < off + folder_len:
                rel = foff - off
                parent = struct.unpack_from('<I', data, foff)[0]
                slen = data[foff + 4]
                name = data[foff + 5:foff + 5 + slen].decode('utf-8', errors='replace')
                folders[rel] = (parent, name)
                foff += 5 + slen
            off += folder_len

            def build_folder_path(ref):
                parts = []
                cur = ref
                while cur != 0xFFFFFFFF and len(parts) < 20:
                    if cur not in folders:
                        break
                    p, n = folders[cur]
                    parts.append(n)
                    cur = p
                return ''.join(reversed(parts))

            # Node section — trie of filenames
            node_len = struct.unpack_from('<I', data, off)[0]; off += 4
            nodes = {}
            noff = off
            while noff < off + node_len:
                rel = noff - off
                parent = struct.unpack_from('<I', data, noff)[0]
                slen = data[noff + 4]
                name = data[noff + 5:noff + 5 + slen].decode('utf-8', errors='replace')
                nodes[rel] = (parent, name)
                noff += 5 + slen
            off += node_len

            def build_node_path(ref):
                parts = []
                cur = ref
                while cur != 0xFFFFFFFF and len(parts) < 64:
                    if cur not in nodes:
                        break
                    p, n = nodes[cur]
                    parts.append(n)
                    cur = p
                return ''.join(reversed(parts))

            # Folder records — map each folder to its file range
            folder_count = struct.unpack_from('<I', data, off)[0]; off += 4
            folder_recs = []
            for i in range(folder_count):
                ph, fr, fi, fc = struct.unpack_from('<IIII', data, off)
                folder_recs.append((build_folder_path(fr), fi, fc))
                off += 16

            # File records — build the map
            file_count = struct.unpack_from('<I', data, off)[0]; off += 4
            # Find root folder name (for building flattened path)
            root_folder = ""
            for _, (p, n) in folders.items():
                if p == 0xFFFFFFFF:
                    root_folder = n
                    break

            # Build file_index → folder_path lookup (O(1) per file instead of O(folders))
            file_to_folder: dict[int, str] = {}
            for fp, fi, fc in folder_recs:
                for idx in range(fi, fi + fc):
                    file_to_folder[idx] = fp

            for i in range(file_count):
                nr = struct.unpack_from('<I', data, off)[0]
                off += 20
                filename = build_node_path(nr)
                fp = file_to_folder.get(i)
                if fp is not None:
                    flattened = f"{root_folder}/{filename}" if root_folder else filename
                    result[flattened] = fp

            return result
        except Exception as e:
            logger.debug("Failed to build path map for %s: %s", pamt_dir, e)
            continue

    return result


OVERLAY_CACHE_SCHEMA = 2  # bump whenever the overlay byte layout changes;
# v2 = JMM BuildPartialDdsPayload parity for DDS entries.


def _load_overlay_cache(game_dir) -> dict:
    """Load the overlay cache manifest from the previous Apply.

    Returns {entry_path: {offset, comp_size, decomp_size, flags, delta_hash, overlay_dir}}
    and the cached overlay PAZ bytes (or None). If the on-disk cache was
    written by an older schema, it's discarded (treated as cold).
    """
    import json
    from pathlib import Path
    cache_path = Path(game_dir) / "CDMods" / ".overlay_cache.json"
    if not cache_path.exists():
        return {}, None, None
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        if manifest.get("_schema") != OVERLAY_CACHE_SCHEMA:
            logger.info("Overlay cache schema mismatch (got %r, want %d) — "
                         "discarding and rebuilding",
                         manifest.get("_schema"), OVERLAY_CACHE_SCHEMA)
            return {}, None, None
        overlay_dir = manifest.get("_overlay_dir")
        if not overlay_dir:
            return {}, None, None
        paz_path = Path(game_dir) / overlay_dir / "0.paz"
        if not paz_path.exists():
            return {}, None, None
        paz_data = paz_path.read_bytes()
        entries = {k: v for k, v in manifest.items() if not k.startswith("_")}
        return entries, paz_data, overlay_dir
    except Exception as e:
        logger.debug("Overlay cache load failed: %s", e)
        return {}, None, None


def _save_overlay_cache(game_dir, overlay_dir, cache_entries: dict):
    """Save overlay cache manifest for incremental rebuild."""
    import json
    from pathlib import Path
    cache_path = Path(game_dir) / "CDMods" / ".overlay_cache.json"
    manifest = dict(cache_entries)
    manifest["_overlay_dir"] = overlay_dir
    manifest["_schema"] = OVERLAY_CACHE_SCHEMA
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f)
    except Exception as e:
        logger.debug("Overlay cache save failed: %s", e)


def _get_vanilla_pabgh(pamt_dir: str, pabgb_entry_path: str, game_dir) -> bytes | None:
    """Extract the vanilla PABGH file that corresponds to a PABGB entry.

    Looks up the sibling .pabgh in the same PAMT directory and extracts it
    from the vanilla PAZ file.

    Returns raw (decompressed) PABGH bytes, or None if not found.
    """
    from pathlib import Path
    from cdumm.archive.paz_parse import parse_pamt, PazEntry

    if not pamt_dir or not game_dir:
        return None

    # Derive PABGH path from PABGB path
    pabgh_path = pabgb_entry_path.replace(".pabgb", ".pabgh")
    pabgh_filename = pabgh_path.rsplit("/", 1)[-1] if "/" in pabgh_path else pabgh_path

    game_dir = Path(game_dir)

    # Search in vanilla backup first, then game dir
    for base in [game_dir / "CDMods" / "vanilla", game_dir]:
        pamt_path = base / pamt_dir / "0.pamt"
        if not pamt_path.exists():
            continue

        try:
            entries = parse_pamt(str(pamt_path), paz_dir=str(base / pamt_dir))
            for e in entries:
                if e.path.lower().endswith(pabgh_filename.lower()):
                    # Found the PABGH entry — extract from PAZ
                    paz_path = Path(e.paz_file)
                    if not paz_path.exists():
                        continue
                    with open(paz_path, "rb") as f:
                        f.seek(e.offset)
                        raw = f.read(e.comp_size)

                    if e.comp_size != e.orig_size and e.orig_size > 0:
                        # Compressed — decompress
                        from cdumm.archive.paz_crypto import lz4_decompress
                        return lz4_decompress(raw, e.orig_size)
                    else:
                        # Uncompressed
                        return raw
        except Exception as ex:
            logger.debug("PABGH extraction failed for %s: %s", pabgh_filename, ex)
            continue

    return None


def build_overlay(
    entries: list[tuple[bytes, dict]],
    game_dir=None,
    progress_cb=None,
    preloaded_cache=None,
    vanilla_pathc_path=None,
) -> tuple[bytes, bytes, list["OverlayEntry"]]:
    """Build overlay PAZ + PAMT from ENTR delta entries.

    Uses incremental rebuild: entries unchanged since last Apply are copied
    from the cached overlay PAZ (no re-compression). Only new/changed entries
    are compressed from scratch.

    Args:
        entries: list of (decompressed_content, entr_metadata) tuples.
        game_dir: game installation directory (for vanilla PAMT lookup).
        progress_cb: optional (idx, total, entry_name) callback.
        preloaded_cache: optional pre-loaded (manifest, paz_data, dir) tuple
            from _load_overlay_cache. Used when the previous overlay dir
            gets cleaned up before build runs.
        vanilla_pathc_path: path to the backed-up vanilla meta/0.pathc. Used
            for DDS ``last4`` lookup so overlay DDS records match what the
            game's texture loader expects. Falls back to format-derived last4.

    Returns:
        (paz_bytes, pamt_bytes, overlay_entries) — the first two ready to write
        to the overlay directory, the third giving per-file comp/decomp sizes
        PLUS the DDS m-values / last4 that the overlay PAZ actually carries,
        so the PATHC update step can register exactly those values.
    """
    _reset_pathc_cache()
    if preloaded_cache:
        cache_manifest, cached_paz, _cached_dir = preloaded_cache
    elif game_dir:
        cache_manifest, cached_paz, _cached_dir = _load_overlay_cache(game_dir)
    else:
        cache_manifest, cached_paz, _cached_dir = {}, None, None
    cache_hits = 0
    new_cache: dict[str, dict] = {}

    paz_buf = bytearray()
    overlay_entries: list[OverlayEntry] = []
    # Track which PABGH files we've already added so the sibling auto-include
    # step below doesn't double up. Seed with any `.pabgh` already in
    # `entries` (e.g. a JSON-insert-fixed .pabgh companion emitted explicitly
    # by process_json_patches_for_overlay). Dedupe key is the ENTRY_PATH in
    # lowercase so the auto-include's derived `.pabgh` path matches.
    _added_pabgh: set[str] = set()
    for _c, _m in entries:
        _ep = _m.get("entry_path", "")
        if _ep.lower().endswith(".pabgh"):
            _added_pabgh.add(_ep.lower())
    total_entries = len(entries)

    for entry_idx, (content, metadata) in enumerate(entries):
        if progress_cb and total_entries > 0:
            ename = metadata.get("entry_path", "").rsplit("/", 1)[-1] if metadata.get("entry_path") else ""
            progress_cb(entry_idx, total_entries, ename)
        entry_path = metadata["entry_path"]
        comp_type = metadata.get("compression_type")
        if comp_type is None:
            entry_filename = (
                entry_path.rsplit("/", 1)[-1] if "/" in entry_path else entry_path
            )
            # Try the explicit extension map first (DDS, BNK).
            dot = entry_filename.rfind(".")
            ext = entry_filename[dot:].lower() if dot >= 0 else ""
            if ext and ext in _EXT_COMP_TYPE:
                comp_type = _EXT_COMP_TYPE[ext]
            else:
                # Unknown extension: scan the vanilla PAMT for the entry's
                # pamt_dir for any neighbor sharing the extension and copy
                # its comp_type. More accurate than always defaulting to
                # LZ4 (=2). Falls back to _infer_comp_type_from_extension
                # if no neighbor exists or the PAMT can't be read.
                pamt_dir_for_ext = metadata.get("pamt_dir", "")
                inferred: int | None = None
                if game_dir and pamt_dir_for_ext and ext:
                    ext_list = _build_ext_comp_list(pamt_dir_for_ext, game_dir)
                    inferred = infer_comp_type_from_pamt(ext_list, ext)
                comp_type = (
                    inferred
                    if inferred is not None
                    else _infer_comp_type_from_extension(entry_filename)
                )
        # JMM-parity: any .dds entry must take the partial-DDS payload branch
        # (flags=1) in the overlay regardless of what metadata's
        # compression_type says. That metadata captures how VANILLA stored
        # the entry — orthogonal to how the OVERLAY should encode the
        # replacement. Without this, DDS entries whose vanilla copy happened
        # to be uncompressed (comp_size==decomp_size, metadata comp_type=0)
        # fell through to raw passthrough with flags=0 + no reserved1 stamp,
        # which the game can't render. Fixes RoninWoof / AvariceHnt minimap
        # enemy-die / dropped-item icon blank-render bug. Equivalent to
        # JMM ModManager.cs:1769 "Flags = .EndsWith('.dds') ? 1 : 2".
        if entry_path.lower().endswith(".dds"):
            comp_type = 1
        pamt_dir = metadata.get("pamt_dir", "")

        # Resolve full folder path from vanilla PAMT.
        # The game uses full hierarchical paths for VFS lookups, not the
        # flattened top-level-folder/filename from parse_pamt.
        if "/" in entry_path:
            _, filename = entry_path.rsplit("/", 1)
        else:
            filename = entry_path

        dir_path = ""
        if game_dir and pamt_dir:
            # Build path map for this PAMT directory (cached per pamt_dir)
            cache_key = pamt_dir
            if cache_key not in _path_map_cache:
                _path_map_cache[cache_key] = _build_full_path_map(pamt_dir, game_dir)
            path_map = _path_map_cache[cache_key]
            dir_path = path_map.get(entry_path, "")

        if not dir_path and "/" in entry_path:
            dir_path = entry_path.rsplit("/", 1)[0]

        paz_offset = len(paz_buf)

        # PAZ format encodes file offsets as u32. Once the overlay PAZ
        # crosses 4 GiB we can't represent the offset and `struct.pack`
        # raises an opaque error far downstream. Surface this here with
        # a clear error so the user knows the overlay needs to split.
        # Round 8 audit catch (F1.1).
        if paz_offset > 0xFFFFFFFF:
            raise ValueError(
                f"Overlay PAZ exceeded 4 GiB at entry {entry_path!r} "
                f"(current size: {paz_offset:,} bytes). The PAZ format "
                f"encodes offsets as u32; an overlay this large can't "
                f"be addressed. Reduce the number of large mods or "
                f"split the load into multiple apply passes."
            )

        # Check overlay cache: if this entry is unchanged, copy compressed
        # bytes directly from the previous overlay PAZ (skip decompression/recompression)
        delta_hash = metadata.get("delta_hash", "")
        if not delta_hash and metadata.get("delta_path"):
            # Use delta file mtime as a cheap change indicator
            import os
            try:
                delta_hash = str(os.path.getmtime(metadata["delta_path"]))
            except OSError:
                delta_hash = ""

        cached = cache_manifest.get(entry_path)
        if (cached and cached_paz and delta_hash
                and cached.get("delta_hash") == delta_hash):
            # Cache hit — copy pre-compressed bytes from previous overlay
            c_off = cached["offset"]
            c_size = cached["comp_size"]
            c_end = c_off + c_size
            # Include padding to alignment
            pad = (PAZ_ALIGNMENT - (c_end % PAZ_ALIGNMENT)) % PAZ_ALIGNMENT
            c_padded = c_end + pad
            # Safety: verify alignment and bounds
            if (c_padded <= len(cached_paz)
                    and paz_offset % PAZ_ALIGNMENT == 0  # current position aligned
                    and c_size > 0):
                paz_buf.extend(cached_paz[c_off:c_padded])
                # For DDS entries, recover m-values + last4 from the cached
                # bytes so PATHC update can proceed without recomputing.
                cached_m = None
                cached_last4 = 0
                if cached["flags"] == 1 and c_size >= 128:
                    seg = cached_paz[c_off:c_off + 128]
                    cached_m = struct.unpack_from("<4I", seg, 32)
                    cached_last4 = struct.unpack_from("<I", seg, 124)[0]
                overlay_entries.append(OverlayEntry(
                    dir_path=dir_path, filename=filename,
                    paz_offset=paz_offset, comp_size=cached["comp_size"],
                    decomp_size=cached["decomp_size"], flags=cached["flags"],
                    dds_m_values=cached_m, dds_last4=cached_last4,
                ))
                new_cache[entry_path] = {
                    "offset": paz_offset, "comp_size": cached["comp_size"],
                    "decomp_size": cached["decomp_size"], "flags": cached["flags"],
                    "delta_hash": delta_hash,
                }
                cache_hits += 1
                continue

        built_m_values: Optional[tuple[int, int, int, int]] = None
        built_last4: int = 0

        if comp_type == 1:
            # DDS type 0x01 — mirrors JMM's BuildPartialDdsPayload + last4
            # patch. The game's texture loader reads reserved1 (bytes 32-47)
            # and reserved2 (byte 124) to find the payload layout; those
            # values must match the bytes actually written to the overlay.
            partial, m_values = _build_dds_partial_payload(content)
            original_len = len(content)
            buf = bytearray(original_len)
            copy_len = min(len(partial), original_len)
            buf[:copy_len] = partial[:copy_len]

            # last4 at byte 124: vanilla PATHC first, else format lookup.
            last4 = _get_pathc_last4_for_path(vanilla_pathc_path, entry_path)
            if last4 == 0:
                last4 = _get_dds_format_last4(content)
            if last4 and len(buf) >= 128:
                struct.pack_into("<I", buf, 124, last4)

            # The payload is stored raw (no outer LZ4) with size == original.
            payload = bytes(buf)
            comp_size = len(payload)
            decomp_size = len(payload)
            flags = 1
            built_m_values = m_values
            built_last4 = last4

        elif comp_type == 2:
            compressed = lz4_compress(content)
            payload = compressed
            comp_size = len(compressed)
            decomp_size = len(content)
            flags = 2

        else:
            payload = content
            comp_size = len(content)
            decomp_size = len(content)
            flags = 0

        # ChaCha20 overlay re-encryption.
        # When the vanilla source entry was encrypted, the overlay must be
        # encrypted too. The overlay PAMT stores flags as a 16-bit ushort
        # with a different layout than the 32-bit vanilla PAMT flags:
        #   bits 0-3:  compression type (0=raw, 2=LZ4, etc.)
        #   bits 4-7:  encryption type  (0=none, 3=ChaCha20)
        # Vanilla PAMT carries paz_index in bits 0-7 of its 32-bit flags;
        # those bits collide with the overlay's compression nibble. Bug
        # report from TheUnLuckyOnes 2026-04-26: blindly copying
        # vanilla_flags & 0xFFFF for Dark Mode Map's CSS yielded 0x0004
        # (paz_index=4 in the low byte), which the game decoded as
        # comp_type=4 (unknown) and crashed. The synthesised pair is
        # what the game's VFS actually expects on overlay entries.
        if metadata.get("encrypted"):
            from cdumm.archive.paz_crypto import encrypt as _chacha_encrypt
            key_name = metadata.get("crypto_filename") or filename
            payload = _chacha_encrypt(payload, key_name)
            comp_size = len(payload)
            # Synthesise: compression nibble we just computed + ChaCha20
            # type 3 in the encryption nibble. Always — ignoring
            # vanilla_flags because vanilla and overlay have different
            # flag layouts.
            flags = (flags & 0x0F) | 0x30
            logger.info("overlay encrypt: %s (ChaCha20, flags=0x%04X)",
                        filename, flags)

        paz_buf.extend(payload)

        # Align to 16 bytes
        pad = PAZ_ALIGNMENT - (len(paz_buf) % PAZ_ALIGNMENT)
        if pad < PAZ_ALIGNMENT:
            paz_buf.extend(b'\x00' * pad)

        overlay_entries.append(OverlayEntry(
            dir_path=dir_path, filename=filename,
            paz_offset=paz_offset, comp_size=comp_size,
            decomp_size=decomp_size, flags=flags,
            dds_m_values=built_m_values, dds_last4=built_last4,
        ))

        # Cache this entry for future incremental rebuilds
        new_cache[entry_path] = {
            "offset": paz_offset, "comp_size": comp_size,
            "decomp_size": decomp_size, "flags": flags,
            "delta_hash": delta_hash,
        }

        logger.info("Overlay entry: %s/%s (comp=%d, decomp=%d, type=%d)",
                     dir_path, filename, comp_size, decomp_size, comp_type)

        # Auto-include matching PABGH alongside PABGB entries.
        # The game needs the index file to read the data file. Without it,
        # the game uses the vanilla PABGH which has stale offsets if the
        # PABGB structure changed.
        if filename.endswith(".pabgb") and game_dir:
            pabgh_name = filename.replace(".pabgb", ".pabgh")
            # Dedupe key matches the pre-seed above (entry_path, lowercase)
            # so an explicit .pabgh in `entries` wins over the vanilla copy.
            companion_entry_path = entry_path.rsplit(".", 1)[0] + ".pabgh"
            pabgh_dedupe_key = companion_entry_path.lower()
            if pabgh_dedupe_key not in _added_pabgh:
                pabgh_data = _get_vanilla_pabgh(pamt_dir, entry_path, game_dir)
                if pabgh_data:
                    _added_pabgh.add(pabgh_dedupe_key)
                    pabgh_offset = len(paz_buf)
                    paz_buf.extend(pabgh_data)
                    # Align
                    pad = PAZ_ALIGNMENT - (len(paz_buf) % PAZ_ALIGNMENT)
                    if pad < PAZ_ALIGNMENT:
                        paz_buf.extend(b'\x00' * pad)
                    overlay_entries.append(OverlayEntry(
                        dir_path=dir_path, filename=pabgh_name,
                        paz_offset=pabgh_offset,
                        comp_size=len(pabgh_data),
                        decomp_size=len(pabgh_data),
                        flags=0,  # uncompressed, matching DMM
                    ))
                    logger.info("Overlay entry (auto PABGH): %s/%s (%d bytes, uncompressed)",
                                 dir_path, pabgh_name, len(pabgh_data))

    if cache_hits > 0:
        logger.info("Overlay cache: %d/%d entries reused (skipped recompression)",
                     cache_hits, len(entries))

    paz_bytes = bytes(paz_buf)
    pamt_bytes = _build_multi_pamt(overlay_entries, len(paz_bytes))

    # Patch PAZ CRC into PAMT at offset 16, then recompute outer hash
    paz_crc = hashlittle(paz_bytes, HASH_SEED)
    pamt_buf = bytearray(pamt_bytes)
    struct.pack_into("<I", pamt_buf, 16, paz_crc)
    outer_hash = hashlittle(bytes(pamt_buf[12:]), HASH_SEED)
    struct.pack_into("<I", pamt_buf, 0, outer_hash)
    pamt_bytes = bytes(pamt_buf)

    logger.info("Overlay built: %d entries, PAZ=%d bytes, PAMT=%d bytes",
                len(overlay_entries), len(paz_bytes), len(pamt_bytes))

    # Store cache for caller to save after overlay dir is allocated
    build_overlay._last_cache = new_cache

    return paz_bytes, pamt_bytes, overlay_entries


def _build_multi_pamt(entries: list[OverlayEntry], paz_data_len: int) -> bytes:
    """Build a PAMT file matching JSON MM's BuildMultiPamt format exactly.

    Layout:
        [0:4]   outer_hash (hashlittle(pamt[12:], 0xC5EDE))
        [4:8]   paz_count (1)
        [8:12]  constant (0x610E0232)
        [12:16] zero (0)
        [16:20] PAZ CRC (filled by caller)
        [20:24] PAZ data length
        folder_section_len(4) + folder_bytes
        node_section_len(4) + node_bytes
        folder_count(4) + folder_records (16 bytes each, NO hash prefix)
        file_count(4) + file_records (20 bytes each)
    """
    unique_dirs = sorted(set(e.dir_path for e in entries))

    # ── Folder section (directory tree) ──
    folder_bytes = bytearray()
    folder_offsets: dict[str, int] = {}

    for dir_path in unique_dirs:
        parts = dir_path.split("/") if dir_path else [""]
        for depth in range(len(parts)):
            key = "/".join(parts[:depth + 1])
            if key in folder_offsets:
                continue
            offset = len(folder_bytes)
            folder_offsets[key] = offset

            if depth == 0:
                parent = 0xFFFFFFFF
                name = parts[0]
            else:
                parent_key = "/".join(parts[:depth])
                parent = folder_offsets[parent_key]
                name = "/" + parts[depth]

            name_bytes = name.encode("utf-8")
            folder_bytes += struct.pack("<I", parent)
            folder_bytes += bytes([len(name_bytes)])
            folder_bytes += name_bytes

    # ── Node section (filenames) ──
    node_bytes = bytearray()
    node_offsets: dict[int, int] = {}

    # Group and sort entries by dir
    dir_entries: dict[str, list[tuple[int, OverlayEntry]]] = {}
    for i, e in enumerate(entries):
        dir_entries.setdefault(e.dir_path, []).append((i, e))
    for d in dir_entries:
        dir_entries[d].sort(key=lambda x: x[1].filename)

    for dir_path in unique_dirs:
        for idx, entry in dir_entries.get(dir_path, []):
            node_offsets[idx] = len(node_bytes)
            name_bytes = entry.filename.encode("utf-8")
            node_bytes += struct.pack("<I", 0xFFFFFFFF)
            node_bytes += bytes([len(name_bytes)])
            node_bytes += name_bytes

    # ── Folder records (16 bytes each) ──
    folder_records = bytearray()
    file_index = 0

    for dir_path in unique_dirs:
        count = len(dir_entries.get(dir_path, []))
        path_hash = hashlittle(dir_path.encode("utf-8"), HASH_SEED)
        folder_ref = folder_offsets.get(dir_path, 0)

        folder_records += struct.pack("<IIII",
                                       path_hash, folder_ref, file_index, count)
        file_index += count

    # ── File records (20 bytes each: node_ref(4) + offset(4) + comp(4) + decomp(4) + zero(2) + flags(2)) ──
    file_records = bytearray()
    for dir_path in unique_dirs:
        for idx, entry in dir_entries.get(dir_path, []):
            node_ref = node_offsets[idx]
            file_records += struct.pack("<IIIIHH",
                                         node_ref,
                                         entry.paz_offset,
                                         entry.comp_size,
                                         entry.decomp_size,
                                         0,
                                         entry.flags)

    # ── Assemble PAMT body (without outer hash) ──
    body = bytearray()
    body += struct.pack("<I", 1)                  # paz_count
    body += struct.pack("<I", PAMT_CONSTANT)      # constant
    body += struct.pack("<I", 0)                  # zero
    body += struct.pack("<I", 0)                  # PAZ CRC placeholder
    body += struct.pack("<I", paz_data_len)       # PAZ size

    body += struct.pack("<I", len(folder_bytes))
    body += folder_bytes

    body += struct.pack("<I", len(node_bytes))
    body += node_bytes

    body += struct.pack("<I", len(unique_dirs))
    body += folder_records

    body += struct.pack("<I", file_index)  # file_count prefix
    body += file_records

    # Prepend outer hash placeholder
    pamt = bytearray(4) + body  # [0:4] = 0 (filled by caller)
    return bytes(pamt)
