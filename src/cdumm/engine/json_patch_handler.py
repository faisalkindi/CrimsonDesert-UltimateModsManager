"""JSON byte-patch mod format handler.

Detects mods distributed as JSON files containing byte-level patches
against specific game files inside PAZ archives.

Format:
    {
        "name": "...",
        "version": "...",
        "description": "...",
        "author": "...",
        "patches": [
            {
                "game_file": "gamedata/iteminfo.pabgb",
                "changes": [
                    {"offset": 24, "label": "...", "original": "64000000", "patched": "3f420f00"},
                    ...
                ]
            }
        ]
    }

Signature-based dynamic offsets (optional):
    If a patch entry has a "signature" field, the handler searches the
    decompressed file for that hex byte pattern. Change offsets are then
    relative to the END of the signature match instead of absolute.
    This survives game updates that shift data around.

    {
        "game_file": "gamedata/inventory.pabgb",
        "signature": "090000004368617261637465720001",
        "changes": [
            {"offset": 0, "label": "...", "original": "3200", "patched": "b400"},
            {"offset": 2, "label": "...", "original": "f000", "patched": "bc02"}
        ]
    }

Offsets are into the DECOMPRESSED file content. The handler:
1. Finds each target file in the game's PAMT index
2. Extracts and decompresses it from the PAZ
3. Applies all byte patches (absolute or signature-relative)
4. Recompresses and repacks into a PAZ copy
5. Returns modified PAZ files for standard CDUMM delta import
"""

import json
import logging
import os
import shutil
import struct
from pathlib import Path

from cdumm.archive.paz_parse import parse_pamt, PazEntry
from cdumm.archive.paz_crypto import decrypt, encrypt, lz4_decompress, lz4_compress
from cdumm.archive.paz_repack import repack_entry_bytes, _save_timestamps

logger = logging.getLogger(__name__)


def _prettify(name: str) -> str:
    """Wrapper to avoid circular import with import_handler."""
    from cdumm.engine.import_handler import prettify_mod_name
    return prettify_mod_name(name)


# ── Inline value editing helpers ──────────────────────────────────────

_VALUE_FORMATS = {
    "int32_le": ("<i", 4),
    "float32_le": ("<f", 4),
    "int16_le": ("<h", 2),
    "uint8": ("<B", 1),
}


def encode_value(value: int | float, type_str: str) -> str:
    """Convert a Python number to a hex byte string for a PAZ patch.

    >>> encode_value(5, "int32_le")
    '05000000'
    """
    fmt, _ = _VALUE_FORMATS.get(type_str, (None, None))
    if fmt is None:
        raise ValueError(f"Unknown editable_value type: {type_str}")
    return struct.pack(fmt, value).hex()


def decode_value(hex_str: str, type_str: str) -> int | float:
    """Convert a hex byte string back to a Python number.

    >>> decode_value('05000000', 'int32_le')
    5
    """
    fmt, size = _VALUE_FORMATS.get(type_str, (None, None))
    if fmt is None:
        raise ValueError(f"Unknown editable_value type: {type_str}")
    raw = bytes.fromhex(hex_str[:size * 2])
    return struct.unpack(fmt, raw)[0]


def apply_custom_values(changes: list[dict], custom_values: dict) -> list[dict]:
    """Return a copy of changes with 'patched' fields updated for custom values.

    custom_values maps change index (as string) to the user's chosen value.
    """
    if not custom_values:
        return changes
    result = []
    for i, change in enumerate(changes):
        idx_key = str(i)
        if idx_key in custom_values and "editable_value" in change:
            ev = change["editable_value"]
            try:
                new_hex = encode_value(custom_values[idx_key], ev["type"])
                change = {**change, "patched": new_hex}
            except (ValueError, KeyError, struct.error) as e:
                logger.warning("Failed to encode custom value for change %d: %s", i, e)
        result.append(change)
    return result


def detect_json_patch(path: Path) -> dict | None:
    """Check if path contains a JSON byte-patch mod.

    Checks the path itself (if a .json file) or searches one level deep
    in a directory.

    Returns parsed JSON dict if valid, None otherwise. For folders with
    multiple valid JSONs (e.g. Trust Me + Pet Abyss Gear shipped as one
    zip), returns the first — callers that want all of them should use
    :func:`detect_json_patches_all` and import each separately.
    """
    results = detect_json_patches_all(path)
    return results[0] if results else None


def detect_json_patches_all(path: Path) -> list[dict]:
    """Return every valid JSON byte-patch under ``path``.

    Folders that ship multi-part JSON mods (Trust Me's main + Pet Abyss
    Gear, bundled texture packs, etc.) put several independent patch
    files in the same zip. Previously CDUMM only imported the first and
    silently dropped the rest. Callers now iterate this list to create
    one mod row per JSON so the user ends up with everything enabled,
    toggleable per-part, and properly version-tracked.
    """
    candidates: list[Path] = []
    if path.is_file() and path.suffix.lower() == ".json":
        candidates = [path]
    elif path.is_dir():
        # Walk the whole tree so mods like Gild's Gear that bury batch
        # JSONs in subfolders (Weapons/Sword1/patch.json etc.) get picked
        # up. Skip directories that look like extracted vanilla PAZ
        # content (NNNN-numbered dirs + 'meta') so we don't false-positive
        # on random bytes that happen to parse as JSON.
        import re as _re_nnnn
        _NNNN = _re_nnnn.compile(r"^\d{4}$")
        for p in path.rglob("*.json"):
            rel = p.relative_to(path)
            if any(_NNNN.match(part) or part.lower() == "meta"
                   for part in rel.parts[:-1]):
                continue
            candidates.append(p)

    from cdumm.engine.json_repair import load_json_tolerant

    valid: list[dict] = []
    for candidate in candidates:
        try:
            data = load_json_tolerant(candidate)
            if (isinstance(data, dict)
                    and "patches" in data
                    and isinstance(data["patches"], list)
                    and len(data["patches"]) > 0
                    and "game_file" in data["patches"][0]
                    and "changes" in data["patches"][0]):
                data["_json_path"] = candidate
                valid.append(data)
        except Exception:
            continue
    return valid


def decompress_entry(raw: bytes, entry: PazEntry) -> bytes:
    """Decompress raw PAZ entry bytes based on the entry's compression type.

    Handles type 0x01 (DDS split: 128-byte header + LZ4 body),
    type 0x02 (fully LZ4 compressed), and uncompressed entries.
    Detects encryption automatically and corrects entry._encrypted_override.
    """
    basename = os.path.basename(entry.path)

    if entry.compression_type == 1:
        DDS_HEADER_SIZE = 128
        header = raw[:DDS_HEADER_SIZE]
        compressed_body = raw[DDS_HEADER_SIZE:]
        body_orig_size = entry.orig_size - DDS_HEADER_SIZE

        # The DDS header may store the inner LZ4 compressed size at offset 32.
        # When comp_size == orig_size (padded DDS), the full body includes
        # LZ4 data + zero padding. Use the header field to read only the
        # actual LZ4 bytes, falling back to the full body if not available.
        inner_comp_size = 0
        if len(header) >= 36:
            inner_comp_size = struct.unpack_from("<I", header, 32)[0]
        if inner_comp_size > 0 and inner_comp_size < len(compressed_body):
            lz4_input = compressed_body[:inner_comp_size]
        else:
            lz4_input = compressed_body

        try:
            body = lz4_decompress(lz4_input, body_orig_size)
        except Exception:
            # Retry with full body (vanilla entries without header field)
            if lz4_input is not compressed_body:
                try:
                    body = lz4_decompress(compressed_body, body_orig_size)
                except Exception:
                    try:
                        decrypted = decrypt(compressed_body, basename)
                        body = lz4_decompress(decrypted, body_orig_size)
                        if not entry._encrypted_override:
                            logger.info("Corrected encrypted flag for %s (DDS split, actually encrypted)",
                                        entry.path)
                            entry._encrypted_override = True
                    except Exception:
                        # All decompression failed — DX10 multi-mip raw passthrough
                        logger.info("DDS %s: returning raw (DX10 multi-mip)", entry.path)
                        return raw
            else:
                try:
                    decrypted = decrypt(compressed_body, basename)
                    body = lz4_decompress(decrypted, body_orig_size)
                    if not entry._encrypted_override:
                        logger.info("Corrected encrypted flag for %s (DDS split, actually encrypted)",
                                    entry.path)
                        entry._encrypted_override = True
                except Exception:
                    # All decompression failed — DX10 multi-mip raw passthrough
                    logger.info("DDS %s: returning raw (DX10 multi-mip)", entry.path)
                    return raw
        return header + body

    if entry.compressed and entry.compression_type == 2:
        try:
            return lz4_decompress(raw, entry.orig_size)
        except Exception:
            decrypted = decrypt(raw, basename)
            result = lz4_decompress(decrypted, entry.orig_size)
            if not entry._encrypted_override:
                logger.info("Corrected encrypted flag for %s (was False, actually encrypted)",
                            entry.path)
                entry._encrypted_override = True
            return result

    if entry.encrypted:
        return decrypt(raw, basename)

    return raw


def _extract_from_paz(entry: PazEntry, paz_path: str | None = None) -> bytes:
    """Read a file entry from its PAZ archive and return decompressed plaintext.

    Args:
        entry: PAMT entry describing the file location and format
        paz_path: override PAZ file path (default: entry.paz_file).
                  Use when reading from a mod's PAZ copy instead of the game file.

    If the PAMT encrypted flag is wrong (file is actually encrypted),
    corrects entry.encrypted so repack_entry_bytes will re-encrypt.

    Handles compression type 0x01 (128-byte DDS header + LZ4 body)
    and type 0x02 (fully LZ4 compressed).
    """
    with open(paz_path or entry.paz_file, "rb") as f:
        f.seek(entry.offset)
        raw = f.read(entry.comp_size)
    return decompress_entry(raw, entry)


def _pattern_scan(
    data: bytearray,
    original_offset: int,
    original_bytes: bytes,
    vanilla_data: bytes | None = None,
) -> int | None:
    """Find the relocated position of original_bytes in data.

    Delegates to Rust cdumm_native.pattern_scan when available.
    Two-tier approach (matching DMM's pattern scan engine):
    1. Contextual scan: grab a context window from vanilla around the
       original offset, search data for that unique fingerprint.
    2. Simple scan: search for original_bytes directly. Short patterns
       (<4 bytes) limited to ±512 bytes to prevent false matches.

    Returns new offset or None if not found/ambiguous.
    """
    try:
        import cdumm_native
        result = cdumm_native.pattern_scan(
            bytes(data), original_offset, original_bytes, vanilla_data)
        if result is not None:
            logger.info("Pattern scan (native): offset 0x%X → 0x%X (delta %+d)",
                        original_offset, result, result - original_offset)
        return result
    except ImportError:
        pass

    # ── Python fallback ──
    data_bytes = bytes(data)

    if vanilla_data and original_offset < len(vanilla_data):
        for ctx_size in (24, 16, 12, 8):
            ctx_start = max(0, original_offset - ctx_size)
            ctx_end = min(len(vanilla_data),
                          original_offset + len(original_bytes) + ctx_size)
            if ctx_end - ctx_start < ctx_size:
                continue
            context = vanilla_data[ctx_start:ctx_end]
            patch_rel = original_offset - ctx_start
            matches = []
            pos = 0
            while True:
                idx = data_bytes.find(context, pos)
                if idx == -1:
                    break
                matches.append(idx)
                pos = idx + 1
            if len(matches) == 1:
                new_offset = matches[0] + patch_rel
                if new_offset + len(original_bytes) <= len(data):
                    logger.info("Pattern scan (contextual, %dB): offset 0x%X → 0x%X (delta %+d)",
                                len(context), original_offset, new_offset,
                                new_offset - original_offset)
                    return new_offset

    pattern = original_bytes
    if not pattern:
        return None  # empty pattern = no relocation possible
    if len(pattern) < 4:
        window = 512
        scan_start = max(0, original_offset - window)
        scan_end = min(len(data_bytes), original_offset + window)
    else:
        scan_start = 0
        scan_end = len(data_bytes)

    best_match = None
    best_dist = float('inf')
    pos = scan_start
    while True:
        idx = data_bytes.find(pattern, pos, scan_end)
        if idx == -1:
            break
        dist = abs(idx - original_offset)
        if dist < best_dist:
            best_dist = dist
            best_match = idx
        pos = idx + 1

    if best_match is not None and best_match != original_offset:
        logger.info("Pattern scan (simple): offset 0x%X → 0x%X (delta %+d)",
                     original_offset, best_match, best_match - original_offset)
        return best_match

    return None


def _build_name_offsets_generic(pabgb_bytes: bytes,
                                 pabgh_bytes: bytes) -> dict[str, int] | None:
    """Generic name→body-offset resolver for any .pabgb where each entry
    begins with ``u32 entry_key`` + ``u32 name_len`` + UTF-8 ``name``.

    This is the standard PABGB record layout — characterinfo, iteminfo,
    and most other Crimson Desert data tables follow it. Returns a dict
    mapping entry name to the absolute body offset the patch's
    ``rel_offset`` is anchored against (which is the same offset the
    .pabgh index stores, i.e. the start of the record before the key).

    Returns None if the formats don't match (caller falls back to
    absolute-offset mode).
    """
    try:
        if len(pabgh_bytes) < 2:
            return None
        count = struct.unpack_from("<H", pabgh_bytes, 0)[0]
        if count == 0 or 2 + count * 8 > len(pabgh_bytes) + 16:
            return None
        name_to_offset: dict[str, int] = {}
        for i in range(count):
            pos = 2 + i * 8
            if pos + 8 > len(pabgh_bytes):
                break
            # .pabgh stores (u32 hash, u32 offset). Offset points at
            # the entry's start in .pabgb. The entry layout:
            #   u32 entry_key
            #   u32 name_len
            #   char[name_len] name
            #   ... (record-specific fields)
            offset = struct.unpack_from("<I", pabgh_bytes, pos + 4)[0]
            if offset + 8 > len(pabgb_bytes):
                continue
            name_len = struct.unpack_from("<I", pabgb_bytes, offset + 4)[0]
            if name_len == 0 or name_len > 100_000:
                continue
            name_end = offset + 8 + name_len
            if name_end > len(pabgb_bytes):
                continue
            try:
                name = pabgb_bytes[offset + 8:name_end].decode(
                    "utf-8", errors="strict")
            except (UnicodeDecodeError, ValueError):
                continue
            if not name or any(c == "\x00" for c in name[:1]):
                continue
            # JMM/SWISS Knife export convention: `rel_offset` is anchored
            # AFTER the name string so mods don't have to track per-record
            # name-length shifts. Verified against ExtraSockets: for
            # Scalaphynion_Fabric_Armor with rel=357, expected bytes
            # 03000000 live at record_start+8+name_len+357, not at
            # record_start+357.
            name_to_offset[name] = name_end
        return name_to_offset if name_to_offset else None
    except Exception as e:
        logger.debug("generic name-offset build failed: %s", e)
        return None


def _build_name_offsets_for_v2(game_file: str, pabgb_bytes: bytes,
                               pabgh_bytes: bytes) -> dict[str, int] | None:
    """Build a name→body-offset map for v2 entry-anchored patches.

    characterinfo.pabgb uses a dedicated SWISS Knife parser (we extract
    localization metadata + boolean blocks there). For every other
    .pabgb we fall back to the generic resolver, which handles the
    standard ``u32 key + u32 name_len + name`` entry header.

    Returns None when the formats don't match so the caller can log
    a clear error instead of silently applying at offset 0 — which was
    the root cause of ExtraSockets and RingEarringGearSockets crashing
    the game: mod patches use ``entry`` + ``rel_offset`` anchored
    against iteminfo.pabgb's body layout, but without a name-offset
    map the apply path treated ``rel_offset`` as absolute and wrote
    to random positions in the file, leaving 217/884 patches failing
    and the rest writing to wrong bytes.
    """
    base = os.path.basename(game_file).lower()
    if base.startswith("characterinfo."):
        try:
            from cdumm.archive.format_parsers.characterinfo_full_parser import (
                build_name_to_body_offset,
            )
            return build_name_to_body_offset(pabgb_bytes, pabgh_bytes)
        except Exception as e:
            logger.warning("v2 name-offset build failed for %s: %s", game_file, e)
            return None
    result = _build_name_offsets_generic(pabgb_bytes, pabgh_bytes)
    if result is None:
        logger.warning("v2 generic name-offset build failed for %s", game_file)
    return result


def fixup_pabgh_after_inserts(pabgh: bytes,
                              inserts: list[tuple[int, int]]) -> bytes:
    """Shift entry pointers in a .pabgh by the total insert size that falls
    before each entry.

    Ports JMM V9.9.1 ``FixupPabghAfterInserts`` (ModManager.cs:855). Handles
    the two 8-byte-entry pabgh variants (2-byte ushort header and 4-byte uint
    header). The 6-byte-entry variant is left alone — JMM skips it too.

    ``inserts`` is a list of ``(original_offset, size)`` tuples referring to
    absolute byte positions in the PRE-insert .pabgb. Any pabgh pointer at
    or past an insert offset is shifted by that insert's size.

    Returns a new bytes object with the fixups applied (or the input bytes
    unchanged if there's nothing to do or the format doesn't match).
    """
    if not inserts or len(pabgh) < 2:
        return pabgh

    arr = bytearray(pabgh)
    # Format detection mirrors JMM: if ushort header and arr length fits a
    # tail of 8*count plus up to 16 padding bytes, treat as format 2.
    ushort_count = struct.unpack_from("<H", arr, 0)[0]
    fmt2 = (ushort_count > 0
            and 2 + ushort_count * 8 <= len(arr)
            and 2 + ushort_count * 8 >= len(arr) - 16)
    if fmt2:
        entry_count = ushort_count
        header_prefix = 2
    else:
        if len(arr) < 4:
            return pabgh
        entry_count = struct.unpack_from("<I", arr, 0)[0]
        header_prefix = 4

    entry_stride = 8
    max_by_len = (len(arr) - header_prefix) // entry_stride
    if entry_count > max_by_len:
        entry_count = max_by_len

    # Pre-sort inserts ascending so we can early-break.
    sorted_inserts = sorted(inserts, key=lambda x: x[0])
    shifted = 0
    for i in range(entry_count):
        pos = header_prefix + i * entry_stride
        if pos + entry_stride > len(arr):
            break
        ptr = struct.unpack_from("<I", arr, pos + 4)[0]
        delta = 0
        for ins_off, ins_size in sorted_inserts:
            if ins_off <= ptr:
                delta += ins_size
            else:
                break
        if delta:
            struct.pack_into("<I", arr, pos + 4, ptr + delta)
            shifted += 1
    logger.info("PABGH fixup: %d/%d entry offsets shifted (+%d bytes total)",
                shifted, entry_count, sum(sz for _, sz in inserts))
    return bytes(arr)


def _apply_byte_patches(data: bytearray, changes: list[dict],
                        signature: str | None = None,
                        vanilla_data: bytes | None = None,
                        record_offsets: dict[int, int] | None = None,
                        name_offsets: dict[str, int] | None = None,
                        inserts_out: list | None = None) -> tuple[int, int, int]:
    """Apply byte patches to decompressed file data.

    If signature is provided, find it in data and treat change offsets
    as relative to the end of the signature match. Otherwise offsets
    are absolute.

    If vanilla_data is provided, enables contextual pattern scan for
    patches whose original bytes don't match at the expected offset
    (game update shifted the data).

    If record_offsets is provided (from pabgh index), changes with
    "record_key" + "relative_offset" resolve their offset via the
    record index instead of using the absolute "offset" field.

    If name_offsets is provided (v2 entry-anchored format — JMM V8+ /
    SWISS Knife style), changes with "entry" name + "rel_offset" resolve
    their offset via the name→body-offset map. This lets mods survive
    game updates that shuffle record keys but keep names stable.

    Returns (applied_count, mismatched_count, relocated_count).
    """
    mismatched = 0
    relocated = 0
    base_offset = 0
    if signature:
        sig_bytes = bytes.fromhex(signature)
        idx = bytes(data).find(sig_bytes)
        if idx < 0:
            logger.error("Signature %s not found in data (%d bytes)",
                         signature[:40] + "..." if len(signature) > 40 else signature,
                         len(data))
            return 0, 0, 0
        base_offset = idx + len(sig_bytes)
        logger.info("Signature found at offset %d, patches relative to %d",
                     idx, base_offset)

    applied = 0

    # Parse and sort all changes by offset (ascending) for single-pass
    # with cumulative delta tracking. This correctly handles interleaved
    # insert+replace ops where inserts shift subsequent offsets.
    def _resolve_all_offsets(change):
        """Return (primary, fallbacks) — all resolvable offsets for a change.

        Priority matters for the offset-drift scenario: anchored offsets
        (record_key via pabgh index, entry name via current-game pabgb)
        are computed against the CURRENT game's structure, so they
        survive updates that shift bytes. Literal `offset` values are
        baked in at mod-author time and go stale. Preferring the
        literal would silently patch the wrong record whenever the mod
        ships both and the game has drifted (Codex 2026-04 regression
        report). BUT: if anchored resolves to bytes that don't match
        vanilla (generic name-resolver gets specific formats like
        multichangeinfo.pabgb wrong — the BRCC case), the apply loop
        falls through to the literal fallback. Both paths are always
        returned; the loop picks whichever one verifies.
        """
        resolved: list[int] = []

        # 1. record_key + relative_offset via pabgh index (anchored).
        record_key = change.get("record_key")
        if record_key is not None and record_offsets:
            try:
                key = int(record_key)
            except (ValueError, TypeError):
                key = None
                logger.debug("resolve_offsets: bad record_key %r (entry=%r)",
                             record_key, change.get("entry"))
            if key is not None:
                if key in record_offsets:
                    rel = change.get("relative_offset")
                    if rel is None:
                        rel = change.get("rel_offset", 0)
                    try:
                        rel = int(rel, 0) if isinstance(rel, str) else int(rel)
                        resolved.append(record_offsets[key] + rel)
                    except (ValueError, TypeError):
                        logger.debug("resolve_offsets: bad relative_offset %r for key %d",
                                     rel, key)
                else:
                    logger.debug("resolve_offsets: record_key %d not in index (entry=%r)",
                                 key, change.get("entry"))

        # 2. v2 entry-anchored: resolve via name→body-offset map.
        entry_name = change.get("entry")
        if entry_name and name_offsets:
            body_offset = name_offsets.get(str(entry_name))
            if body_offset is not None:
                rel = change.get("rel_offset")
                if rel is None:
                    rel = change.get("relative_offset", 0)
                try:
                    rel = int(rel, 0) if isinstance(rel, str) else int(rel)
                    resolved.append(body_offset + rel)
                except (ValueError, TypeError):
                    logger.debug("resolve_offsets: bad rel_offset %r for entry %r",
                                 rel, entry_name)
            else:
                logger.debug("resolve_offsets: entry %r not in name_offsets "
                             "(%d names available)",
                             entry_name, len(name_offsets))

        # 3. Literal numeric `offset` — the stale/stable absolute.
        raw = change.get("offset")
        if raw is not None:
            try:
                val = int(raw, 0) if isinstance(raw, str) else int(raw)
                resolved.append(base_offset + val)
            except (ValueError, TypeError):
                try:
                    resolved.append(base_offset + int(str(raw), 16))
                except (ValueError, TypeError):
                    logger.debug("resolve_offsets: bad literal offset %r (entry=%r)",
                                 raw, change.get("entry"))

        # Deduplicate while preserving order.
        seen: set[int] = set()
        uniq: list[int] = []
        for off in resolved:
            if off not in seen:
                seen.add(off)
                uniq.append(off)
        if not uniq:
            return None, []
        return uniq[0], uniq[1:]

    def _parse_offset(change):
        primary, _fallbacks = _resolve_all_offsets(change)
        return primary

    all_changes = []
    for change in changes:
        primary, fallbacks = _resolve_all_offsets(change)
        if primary is None:
            # Unresolvable offset (bad record_key, entry name missing, or
            # malformed offset). Count as mismatched so the import layer
            # reports this as a compatibility failure, not "already applied".
            mismatched += 1
            logger.warning("Unresolvable offset for change: entry=%r record_key=%r offset=%r",
                           change.get("entry"), change.get("record_key"),
                           change.get("offset"))
            continue
        all_changes.append((primary, change, fallbacks))
    all_changes.sort(key=lambda x: x[0])

    # Track writes as (original-coord position, size_delta) tuples. A
    # single cumulative counter silently over-shifts later patches when
    # an earlier patch's fallback/relocation lands above some subsequent
    # primaries. For each patch, shift is the sum of deltas from writes
    # whose position is strictly BELOW this patch's primary.
    writes: list[tuple[int, int]] = []

    def _shift_for(pos: int) -> int:
        return sum(d for w_pos, d in writes if w_pos < pos)

    for original_offset, change, fallback_offsets in all_changes:
        offset = original_offset + _shift_for(original_offset)
        ct = change.get("type", "replace")

        if ct == "insert":
            insert_hex = change.get("bytes", "")
            if not insert_hex:
                continue
            try:
                insert_bytes = bytes.fromhex(insert_hex)
            except ValueError:
                continue
            if offset <= len(data):
                data[offset:offset] = insert_bytes
                if inserts_out is not None:
                    inserts_out.append((original_offset, len(insert_bytes)))
                writes.append((original_offset, len(insert_bytes)))
                applied += 1
        else:
            # Replace
            patched_hex = change.get("patched")
            if not patched_hex:
                logger.warning("Change at offset %d has no 'patched' field, skipping", offset)
                continue
            patched_bytes = bytes.fromhex(patched_hex)

            if offset + len(patched_bytes) > len(data):
                logger.warning("Patch at offset %d exceeds file size %d, skipping",
                               offset, len(data))
                continue

            if "original" in change:
                original_bytes = bytes.fromhex(change["original"])
                size_delta = len(patched_bytes) - len(original_bytes)
                actual = data[offset:offset + len(original_bytes)]
                if actual != original_bytes:
                    # Idempotent re-apply.
                    actual_at_patch = data[offset:offset + len(patched_bytes)]
                    if actual_at_patch == patched_bytes:
                        logger.debug("Already patched at %d, keeping as-is", offset)
                        writes.append((original_offset, size_delta))
                        applied += 1
                        continue

                    # Vanilla-remnant check. The mod's 'original' bytes
                    # appear in vanilla — either at original_offset or at
                    # a fallback offset (in case primary was anchored
                    # against current-game, which drifted from vanilla).
                    # If any location matches vanilla, the buffer
                    # divergence is from a prior overlapping write in
                    # this same run; keep going and write patched bytes.
                    remnant_matched = False
                    if vanilla_data is not None:
                        check_positions = [original_offset] + list(fallback_offsets)
                        for van_pos in check_positions:
                            van_end = van_pos + len(original_bytes)
                            if van_end <= len(vanilla_data) and \
                                    vanilla_data[van_pos:van_end] == original_bytes:
                                remnant_matched = True
                                break
                    if remnant_matched:
                        logger.debug(
                            "Overlap at %d: vanilla matches original, writing "
                            "patched bytes over earlier-patch remnant", offset)
                        data[offset:offset + len(original_bytes)] = patched_bytes
                        writes.append((original_offset, size_delta))
                        applied += 1
                        continue

                    # Fallback-offset resolution. Uniqueness guard: require
                    # exactly one fallback to match (avoid silently patching
                    # the wrong record when short `original` byte strings
                    # recur).
                    viable_fbs: list[int] = []
                    for fb_orig in fallback_offsets:
                        fb_off = fb_orig + _shift_for(fb_orig)
                        if fb_off + len(original_bytes) > len(data):
                            continue
                        if data[fb_off:fb_off + len(original_bytes)] == original_bytes:
                            viable_fbs.append(fb_orig)
                    if len(viable_fbs) == 1:
                        fb_orig = viable_fbs[0]
                        fb_off = fb_orig + _shift_for(fb_orig)
                        data[fb_off:fb_off + len(original_bytes)] = patched_bytes
                        logger.info(
                            "Fallback offset 0x%X matched (primary 0x%X missed) "
                            "for entry=%r", fb_orig, original_offset,
                            change.get("entry"))
                        writes.append((fb_orig, size_delta))
                        applied += 1
                        continue
                    elif len(viable_fbs) > 1:
                        logger.warning(
                            "Fallback offset ambiguous for entry=%r: %d "
                            "candidates match (0x%s) — skipping rather than "
                            "patching wrong bytes",
                            change.get("entry"), len(viable_fbs),
                            ", 0x".join(f"{o:X}" for o in viable_fbs))
                        mismatched += 1
                        continue

                    # Pattern scan drift recovery.
                    new_offset = _pattern_scan(data, offset, original_bytes,
                                               vanilla_data=vanilla_data)
                    if new_offset is not None:
                        if data[new_offset:new_offset + len(original_bytes)] == original_bytes:
                            data[new_offset:new_offset + len(patched_bytes)] = patched_bytes
                            # Record the write at the patch's ORIGINAL
                            # sort-key primary, not a reconstructed
                            # `new_offset - _shift_for(new_offset)`. The
                            # approximation could point into the middle
                            # of a later patch's primary and double-
                            # shift it. The sort was done by
                            # original_offset; the shift tracker needs
                            # to agree with that sort order.
                            writes.append((original_offset, size_delta))
                            applied += 1
                            relocated += 1
                            continue

                    logger.warning("Original mismatch at %d: expected %s, got %s — skipping patch",
                                   offset, change["original"], actual.hex())
                    mismatched += 1
                    continue

            # Track size delta for replace ops that change size
            old_len = len(bytes.fromhex(change["original"])) if "original" in change else len(patched_bytes)
            data[offset:offset + old_len] = patched_bytes
            writes.append((original_offset, len(patched_bytes) - old_len))
            applied += 1

    return applied, mismatched, relocated


def convert_json_patch_to_paz(patch_data: dict, game_dir: Path, work_dir: Path) -> Path | None:
    """Convert a JSON patch mod to modified PAZ files.

    IMPORTANT: Always uses VANILLA files as the base, not the current game
    files which may have other mods applied (shifted offsets, changed sizes).

    For each patched game_file:
    1. Find it in vanilla PAMT, extract from vanilla PAZ
    2. Apply byte patches to decompressed content
    3. Recompress/encrypt and write to vanilla PAZ copy in work_dir

    Returns work_dir containing modified PAZ files, or None on failure.
    """
    patches = patch_data["patches"]
    # Prefer modinfo.title (JMM spec) over top-level name so log messages
    # and abort errors reference the mod by its real title.
    _mi = patch_data.get("modinfo") if isinstance(
        patch_data.get("modinfo"), dict) else {}
    mod_name = (_mi.get("title") or _mi.get("name")
                or patch_data.get("title")
                or patch_data.get("name") or "unknown")

    # Use vanilla backups if available, fall back to game dir
    vanilla_dir = game_dir / "CDMods" / "vanilla"
    if not vanilla_dir.exists():
        vanilla_dir = game_dir
        logger.warning("No vanilla backup dir, using game dir (may have shifted offsets)")
    else:
        logger.info("Using vanilla backups for JSON patch base")

    logger.info("JSON patch mod '%s': %d file(s) to patch", mod_name, len(patches))

    entry_cache: dict[str, PazEntry] = {}

    # Same AIO performance fix as import_json_as_entr — collapse patches
    # that target the same game_file so we don't extract+recompress the
    # same .pabgb 4000 times for a 4000-offset stamina mod.
    grouped: dict[str, dict] = {}
    for _p in patches:
        gf = _p.get("game_file")
        if not gf:
            continue
        if gf not in grouped:
            grouped[gf] = {"game_file": gf, "changes": list(_p.get("changes", [])),
                           "signature": _p.get("signature")}
        else:
            grouped[gf]["changes"].extend(_p.get("changes", []))
            if grouped[gf].get("signature") is None and _p.get("signature"):
                grouped[gf]["signature"] = _p.get("signature")

    for patch in grouped.values():
        game_file = patch["game_file"]
        changes = patch["changes"]

        if not changes:
            continue

        # Find the PAMT entry using VANILLA PAMT (correct offsets)
        if game_file.lower() not in entry_cache:
            entry = _find_pamt_entry(game_file, vanilla_dir)
            if entry is None:
                # Fallback to game dir if vanilla doesn't have this directory
                entry = _find_pamt_entry(game_file, game_dir)
            if entry:
                entry_cache[game_file.lower()] = entry

        entry = entry_cache.get(game_file.lower())
        if entry is None:
            logger.error("Could not find '%s' in any PAMT index", game_file)
            return None

        logger.info("Patching %s: %d changes (paz=%s, comp=%d, orig=%d)",
                     game_file, len(changes),
                     os.path.basename(entry.paz_file),
                     entry.comp_size, entry.orig_size)

        # Extract and decompress the file.
        # If the vanilla PAZ backup doesn't exist, fall back to game dir
        # AND re-lookup the entry using the game PAMT (correct offsets for
        # the current game PAZ state, which may have other mods applied).
        try:
            if not os.path.exists(entry.paz_file):
                game_entry = _find_pamt_entry(game_file, game_dir)
                if game_entry:
                    logger.info("Vanilla PAZ not found, using game dir for %s", game_file)
                    entry = game_entry
                    entry_cache[game_file.lower()] = entry
            plaintext = _extract_from_paz(entry)
        except Exception as e:
            # If extraction fails (e.g., offsets wrong from modded PAZ),
            # try game dir with fresh PAMT lookup as last resort
            try:
                game_entry = _find_pamt_entry(game_file, game_dir)
                if game_entry:
                    logger.info("Retrying extraction from game dir for %s", game_file)
                    plaintext = _extract_from_paz(game_entry)
                    entry = game_entry
                    entry_cache[game_file.lower()] = entry
                else:
                    raise
            except Exception:
                logger.error("Failed to extract %s: %s", game_file, e, exc_info=True)
                raise RuntimeError(f"Failed to extract {game_file}: {e}") from e

        # Resolve pabgh record offsets if any change uses record_key OR entry (v2)
        record_offsets = None
        name_offsets = None
        needs_pabgh = any(
            c.get("record_key") is not None or c.get("entry") for c in changes)
        if needs_pabgh:
            pabgh_file = game_file.rsplit(".", 1)[0] + ".pabgh"
            pabgh_entry = entry_cache.get(pabgh_file.lower())
            if pabgh_entry is None:
                pabgh_entry = _find_pamt_entry(pabgh_file, vanilla_dir)
                if pabgh_entry is None:
                    pabgh_entry = _find_pamt_entry(pabgh_file, game_dir)
                if pabgh_entry:
                    entry_cache[pabgh_file.lower()] = pabgh_entry
            if pabgh_entry:
                try:
                    pabgh_plain = _extract_from_paz(pabgh_entry)
                    table_name = os.path.basename(game_file).rsplit(".", 1)[0]
                    from cdumm.semantic.parser import parse_pabgh_index
                    _key_size, record_offsets = parse_pabgh_index(pabgh_plain, table_name)
                    logger.info("Loaded pabgh index for %s: %d records",
                                pabgh_file, len(record_offsets))
                    # Also build name→offset map if any change uses the v2 entry form
                    if any(c.get("entry") for c in changes):
                        name_offsets = _build_name_offsets_for_v2(
                            game_file, bytes(plaintext), pabgh_plain)
                        if name_offsets is not None:
                            logger.info("Built v2 name index for %s: %d names",
                                        game_file, len(name_offsets))
                except Exception as e_pabgh:
                    logger.warning("Failed to parse pabgh for %s: %s", pabgh_file, e_pabgh)

        # Apply byte patches
        modified = bytearray(plaintext)
        signature = patch.get("signature")
        applied, mismatched, relocated_count = _apply_byte_patches(
            modified, changes, signature=signature, vanilla_data=bytes(plaintext),
            record_offsets=record_offsets, name_offsets=name_offsets)
        if relocated_count:
            logger.info("Applied %d/%d patches to %s (mismatched=%d, relocated=%d)",
                         applied, len(changes), game_file, mismatched, relocated_count)
        else:
            logger.info("Applied %d/%d patches to %s (mismatched=%d)",
                         applied, len(changes), game_file, mismatched)

        # Strict-abort for data-table files (.pabgb / .pabgh / .pamt) at
        # IMPORT time too: if any patch mismatches, refuse to store a
        # half-patched delta. Kliff Wears Damiane V2 and similar mods
        # ship absolute offsets that drift between game versions — the
        # mount-time guard catches a json_source-driven apply but this
        # path pre-computes deltas during import and the mount guard
        # never fires. Raising makes the import fail cleanly with a
        # user-visible error instead of shipping a crash-causing file.
        gf_lower = game_file.lower()
        is_data_table = (gf_lower.endswith(".pabgb")
                         or gf_lower.endswith(".pabgh")
                         or gf_lower.endswith(".pamt"))
        if mismatched > 0 and is_data_table:
            logger.error(
                "JSON import: aborting — %d of %d patches mismatched "
                "against vanilla %s. Data tables cannot be partially "
                "applied (causes game crashes). Mod likely built for a "
                "different game version.",
                mismatched, applied + mismatched, game_file)
            raise ValueError(
                f"Mod '{mod_name}' has {mismatched} of "
                f"{applied + mismatched} patches that do not match "
                f"vanilla {game_file}. Shipping a partial data table "
                f"would crash the game on startup. This mod was likely "
                f"built for a different game version, check the mod "
                f"page for an updated release.")

        if bytes(modified) == plaintext:
            logger.info("No actual changes after patching %s, skipping", game_file)
            continue

        # Repack: compress + encrypt back to PAZ format
        # Use allow_size_change=True because byte patches change the LZ4
        # compression ratio slightly — we'll update PAMT to match.
        try:
            payload, actual_comp, actual_orig = repack_entry_bytes(
                bytes(modified), entry, allow_size_change=True)
        except Exception as e:
            logger.error("Failed to repack %s: %s", game_file, e, exc_info=True)
            return None

        # Copy the PAZ file and write the patched payload
        paz_src = Path(entry.paz_file)
        dir_name = paz_src.parent.name
        paz_dst = work_dir / dir_name / paz_src.name
        if not paz_dst.exists():
            paz_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(paz_src, paz_dst)
            logger.info("Copied PAZ: %s -> %s", paz_src.name, paz_dst)

        new_offset = entry.offset
        if actual_comp > entry.comp_size:
            # Data doesn't fit in the original slot — append to end of PAZ
            # and update offset in PAMT
            restore_ts = _save_timestamps(str(paz_dst))
            with open(paz_dst, "r+b") as fh:
                fh.seek(0, 2)  # seek to end
                new_offset = fh.tell()
                fh.write(payload)
            restore_ts()
            logger.info("Appended %s to end of PAZ at offset %d (was %d, grew %d->%d)",
                        game_file, new_offset, entry.offset, entry.comp_size, actual_comp)
        else:
            # Write patched payload at the original offset
            restore_ts = _save_timestamps(str(paz_dst))
            with open(paz_dst, "r+b") as fh:
                fh.seek(entry.offset)
                fh.write(payload)
            restore_ts()

        # Copy PAMT and update comp_size/offset if they changed
        pamt_src = paz_src.parent / "0.pamt"
        pamt_dst = work_dir / dir_name / "0.pamt"
        if pamt_src.exists() and not pamt_dst.exists():
            pamt_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pamt_src, pamt_dst)

        if (actual_comp != entry.comp_size or new_offset != entry.offset
                or actual_orig != entry.orig_size) and pamt_dst.exists():
            # If we appended to PAZ, pass the new file size so PAMT PAZ table is updated
            new_paz_size = None
            if new_offset != entry.offset:
                new_paz_size = new_offset + actual_comp  # end of appended data = new PAZ size
            _update_pamt_record(pamt_dst, entry, actual_comp, new_offset,
                                new_paz_size=new_paz_size)
            logger.info("Updated PAMT for %s: comp %d->%d, offset %d->%d%s",
                        game_file, entry.comp_size, actual_comp,
                        entry.offset, new_offset,
                        f", paz_size={new_paz_size}" if new_paz_size else "")

    return work_dir


def _update_pamt_record(pamt_path: Path, entry: PazEntry,
                        new_comp_size: int, new_offset: int,
                        new_paz_size: int | None = None) -> None:
    """Update a file record's comp_size and/or offset in a PAMT binary file.

    PAMT file records are 20 bytes: node_ref(4) + offset(4) + comp_size(4) + orig_size(4) + flags(4).
    Also updates the PAZ size table if new_paz_size is provided.
    """
    data = bytearray(pamt_path.read_bytes())

    # Update PAZ size table if the PAZ file grew (data appended to end)
    if new_paz_size is not None:
        paz_index = entry.paz_index
        paz_count = struct.unpack_from('<I', data, 4)[0]
        if paz_index < paz_count:
            # PAZ table starts at offset 16: [hash(4) + size(4)] per entry,
            # with 4-byte separator between entries (except after the last)
            table_off = 16
            for i in range(paz_index):
                table_off += 8  # hash + size
                if i < paz_count - 1:
                    table_off += 4  # separator
            # table_off now points to hash(4) + size(4) for this PAZ
            size_off = table_off + 4  # skip hash, point to size
            old_size = struct.unpack_from('<I', data, size_off)[0]
            struct.pack_into('<I', data, size_off, new_paz_size)
            logger.debug("Updated PAMT PAZ[%d] size: %d -> %d",
                         paz_index, old_size, new_paz_size)

    # Search for the 16-byte pattern: offset + comp_size + orig_size + flags
    search = struct.pack('<IIII', entry.offset, entry.comp_size, entry.orig_size, entry.flags)

    pos = 0
    found = False
    while pos <= len(data) - 20:
        idx = data.find(search, pos)
        if idx < 0:
            break
        record_start = idx - 4
        if record_start >= 0:
            struct.pack_into('<I', data, idx, new_offset)
            struct.pack_into('<I', data, idx + 4, new_comp_size)
            found = True
            logger.debug("Patched PAMT record at byte %d: offset %d->%d, comp %d->%d",
                         record_start, entry.offset, new_offset,
                         entry.comp_size, new_comp_size)
            break
        pos = idx + 1

    if not found:
        logger.warning("Could not find PAMT record for %s (offset=0x%X, comp=%d)",
                       entry.path, entry.offset, entry.comp_size)
        return

    # Recompute PAMT hash
    from cdumm.archive.hashlittle import compute_pamt_hash
    new_hash = compute_pamt_hash(bytes(data))
    struct.pack_into('<I', data, 0, new_hash)

    pamt_path.write_bytes(bytes(data))


# Global PAMT index cache: {game_dir_str: {path_lower: PazEntry, basename_lower: PazEntry}}
_pamt_index_cache: dict[str, dict[str, PazEntry]] = {}


def _get_pamt_index(game_dir: Path) -> dict[str, PazEntry]:
    """Build or retrieve a cached index of all PAMT entries for a game directory.

    Uses a disk cache (pickle) to avoid rebuilding on every subprocess.
    Cache is self-generated data (not user input), safe to unpickle.
    """
    key = str(game_dir)
    if key in _pamt_index_cache:
        return _pamt_index_cache[key]

    # Try loading from disk cache (self-generated, safe to unpickle)
    import pickle as _pickle
    import time as _time
    # Cache lives in CDMods if it exists, otherwise next to the PAMTs
    cdmods = game_dir / "CDMods"
    if not cdmods.exists():
        cdmods = game_dir.parent / "CDMods" if game_dir.name == "vanilla" else game_dir
    cdmods.mkdir(parents=True, exist_ok=True)
    cache_path = cdmods / ".pamt_index.cache"
    if cache_path.exists():
        try:
            cache_mtime = cache_path.stat().st_mtime
            # Only check vanilla PAMTs (< 0036) for staleness — mod PAMTs
            # change on every Apply and would always invalidate the cache
            stale = False
            for d in game_dir.iterdir():
                if not d.is_dir() or not d.name.isdigit() or len(d.name) != 4:
                    continue
                if int(d.name) >= 36:
                    continue  # skip mod directories
                pamt = d / "0.pamt"
                if pamt.exists() and pamt.stat().st_mtime > cache_mtime:
                    stale = True
                    break
            if not stale:
                t0 = _time.perf_counter()
                with open(cache_path, "rb") as f:
                    index = _pickle.load(f)  # noqa: S301 — self-generated cache
                dt = _time.perf_counter() - t0
                logger.info("Loaded PAMT index from cache: %d keys in %.2fs", len(index), dt)
                _pamt_index_cache[key] = index
                return index
        except Exception as e:
            logger.debug("PAMT cache load failed: %s", e)

    # Build fresh index
    t0 = _time.perf_counter()
    index: dict[str, PazEntry] = {}
    for d in sorted(game_dir.iterdir()):
        if not d.is_dir() or not d.name.isdigit():
            continue
        pamt = d / "0.pamt"
        if not pamt.exists():
            continue
        try:
            entries = parse_pamt(str(pamt), paz_dir=str(d))
            for e in entries:
                ep = e.path.lower().replace("\\", "/")
                index[ep] = e
                bname = ep.rsplit("/", 1)[-1]
                index[bname] = e
        except Exception:
            continue

    dt = _time.perf_counter() - t0
    logger.info("Built PAMT index for %s: %d keys in %.2fs", game_dir, len(index), dt)

    # Persist to disk for next subprocess invocation
    try:
        with open(cache_path, "wb") as f:
            _pickle.dump(index, f, protocol=5)
    except Exception:
        pass

    _pamt_index_cache[key] = index
    return index


def _derive_pamt_dir(paz_file: str | Path) -> str:
    """Return the PAMT directory ('0009', '0002', …) for a PAZ file path.

    When the caller passes a bare filename with no parent (rare, usually
    a bug upstream), Path.parent.name is ''. Overlay entries keyed on
    an empty pamt_dir collide in the overlay builder and misroute at
    write-time, so log a warning rather than returning it silently.
    """
    name = Path(paz_file).parent.name
    if not name:
        logger.warning(
            "_derive_pamt_dir: empty pamt_dir for paz_file=%r — "
            "overlay metadata may be invalid", str(paz_file))
    return name


def _find_pamt_entry(game_file: str, game_dir: Path) -> PazEntry | None:
    """Search all PAMT indices for a specific game file path.

    Uses a cached global index for O(1) lookup instead of scanning
    all directories on every call.
    """
    index = _get_pamt_index(game_dir)
    game_file_lower = game_file.lower().replace("\\", "/")

    # Exact match
    e = index.get(game_file_lower)
    if e:
        return e

    # Basename match
    game_basename = game_file_lower.rsplit("/", 1)[-1]
    e = index.get(game_basename)
    if e:
        logger.info("Matched '%s' to '%s' by basename", game_file, e.path)
        return e
    return None


def import_json_as_entr(patch_data: dict, game_dir: Path, db, deltas_dir: Path,
                        mod_name: str, existing_mod_id: int | None = None,
                        modinfo: dict | None = None) -> dict | None:
    """Import a JSON patch mod as ENTR deltas instead of FULL_COPY PAZ deltas.

    This produces entry-level deltas that compose correctly when multiple
    mods modify different entries in the same PAZ file.

    Returns a result dict with mod_id and changed_files, or None on failure.
    """
    from cdumm.engine.delta_engine import save_entry_delta

    patches = patch_data["patches"]
    logger.info("import_json_as_entr: starting '%s' (%d patches)", mod_name, len(patches))

    vanilla_dir = game_dir / "CDMods" / "vanilla"
    if not vanilla_dir.exists():
        vanilla_dir = game_dir
        logger.info("import_json_as_entr: no vanilla dir, using game dir")

    # Create mod entry in DB
    priority = db.connection.execute(
        "SELECT COALESCE(MAX(priority), 0) + 1 FROM mods").fetchone()[0]
    author = modinfo.get("author") if modinfo else patch_data.get("author")
    version = modinfo.get("version") if modinfo else patch_data.get("version")
    description = modinfo.get("description") if modinfo else patch_data.get("description")

    # Stamp with current game version
    game_ver_hash = None
    try:
        from cdumm.engine.version_detector import detect_game_version
        game_ver_hash = detect_game_version(game_dir)
    except Exception:
        pass

    if existing_mod_id:
        mod_id = existing_mod_id
        # Clear existing deltas for re-import
        db.connection.execute("DELETE FROM mod_deltas WHERE mod_id = ?", (mod_id,))
        if game_ver_hash:
            db.connection.execute(
                "UPDATE mods SET game_version_hash = ? WHERE id = ?",
                (game_ver_hash, mod_id))
        import shutil
        old_delta_dir = deltas_dir / str(mod_id)
        if old_delta_dir.exists():
            shutil.rmtree(old_delta_dir)
    else:
        cursor = db.connection.execute(
            "INSERT INTO mods (name, mod_type, priority, author, version, description, game_version_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_prettify(mod_name), "paz", priority, author, version, description, game_ver_hash))
        mod_id = cursor.lastrowid

    changed_files = []
    entry_cache: dict[str, PazEntry] = {}

    # ── AIO performance fix ──────────────────────────────────────────
    # Group patches by game_file BEFORE the per-file extract loop. AIO
    # mods (e.g. 0xNobody's stamina + spirit pack) ship multiple patch
    # entries that all target the same .pabgb. The original per-patch
    # loop re-extracted the same PAZ file once per patch, which on a
    # 4000-offset mod meant decompressing the same file 4000 times and
    # locking the import worker for over a minute. Grouping collapses
    # that to one extract + one delta save per unique game_file.
    plaintext_cache: dict[str, bytes] = {}
    grouped: dict[str, dict] = {}
    for _p in patches:
        gf = _p.get("game_file")
        if not gf:
            continue
        if gf not in grouped:
            grouped[gf] = {"game_file": gf, "changes": list(_p.get("changes", [])),
                           "signature": _p.get("signature")}
        else:
            grouped[gf]["changes"].extend(_p.get("changes", []))
            # Inherit signature from first patch that declares one.
            if grouped[gf].get("signature") is None and _p.get("signature"):
                grouped[gf]["signature"] = _p.get("signature")
    if len(grouped) < len(patches):
        logger.info(
            "import_json_as_entr: collapsed %d patches into %d unique "
            "game_files (saved %d redundant extracts)",
            len(patches), len(grouped), len(patches) - len(grouped))

    for patch in grouped.values():
        game_file = patch["game_file"]
        changes = patch["changes"]
        if not changes:
            continue

        # Find PAMT entry
        logger.info("import_json_as_entr: looking up '%s' in PAMTs", game_file)
        if game_file.lower() not in entry_cache:
            entry = _find_pamt_entry(game_file, vanilla_dir)
            if entry is None:
                entry = _find_pamt_entry(game_file, game_dir)
            if entry:
                entry_cache[game_file.lower()] = entry
                logger.info("import_json_as_entr: found '%s' in %s (offset=%d, comp=%d)",
                            game_file, Path(entry.paz_file).parent.name,
                            entry.offset, entry.comp_size)

        entry = entry_cache.get(game_file.lower())
        if entry is None:
            logger.error("Could not find '%s' in any PAMT index", game_file)
            # Rollback
            db.connection.execute("DELETE FROM mods WHERE id = ?", (mod_id,))
            db.connection.commit()
            return None

        # Extract and decompress
        logger.info("import_json_as_entr: extracting '%s' from %s", game_file, entry.paz_file)
        try:
            if not os.path.exists(entry.paz_file):
                logger.info("import_json_as_entr: PAZ not found at %s, trying game dir", entry.paz_file)
                game_entry = _find_pamt_entry(game_file, game_dir)
                if game_entry:
                    entry = game_entry
                    entry_cache[game_file.lower()] = entry
            plaintext = _extract_from_paz(entry)
            logger.info("import_json_as_entr: extracted %d bytes", len(plaintext))
        except Exception as e:
            try:
                game_entry = _find_pamt_entry(game_file, game_dir)
                if game_entry:
                    plaintext = _extract_from_paz(game_entry)
                    entry = game_entry
                    entry_cache[game_file.lower()] = entry
                else:
                    raise
            except Exception:
                logger.error("Failed to extract %s: %s", game_file, e)
                db.connection.execute("DELETE FROM mods WHERE id = ?", (mod_id,))
                db.connection.commit()
                return None

        # v2 entry-anchored: resolve name→offset map for characterinfo.pabgb etc.
        name_offsets = None
        if any(c.get("entry") for c in changes):
            pabgh_file = game_file.rsplit(".", 1)[0] + ".pabgh"
            pabgh_entry = entry_cache.get(pabgh_file.lower())
            if pabgh_entry is None:
                pabgh_entry = _find_pamt_entry(pabgh_file, game_dir)
                if pabgh_entry:
                    entry_cache[pabgh_file.lower()] = pabgh_entry
            if pabgh_entry:
                try:
                    pabgh_plain = _extract_from_paz(pabgh_entry)
                    name_offsets = _build_name_offsets_for_v2(
                        game_file, bytes(plaintext), pabgh_plain)
                    if name_offsets is not None:
                        logger.info("Built v2 name index for %s: %d names",
                                    game_file, len(name_offsets))
                except Exception as e_pabgh:
                    logger.warning("v2 index build failed for %s: %s", pabgh_file, e_pabgh)

        # Apply byte patches
        modified = bytearray(plaintext)
        signature = patch.get("signature")
        applied, mismatched, relocated_count = _apply_byte_patches(
            modified, changes, signature=signature, vanilla_data=bytes(plaintext),
            name_offsets=name_offsets)
        if relocated_count:
            logger.info("Applied %d/%d patches to %s (mismatched=%d, relocated=%d)",
                         applied, len(changes), game_file, mismatched, relocated_count)
        else:
            logger.info("Applied %d/%d patches to %s (mismatched=%d)",
                         applied, len(changes), game_file, mismatched)

        # All patches failed due to byte mismatch → game version incompatibility
        if mismatched > 0 and applied == 0 and bytes(modified) == plaintext:
            game_ver = patch_data.get("game_version", "unknown")
            logger.error("All %d patches mismatched for %s — mod targets game version %s",
                         mismatched, game_file, game_ver)
            db.connection.execute("DELETE FROM mods WHERE id = ?", (mod_id,))
            db.connection.commit()
            return {"changed_files": [], "version_mismatch": True,
                    "game_file": game_file, "game_version": game_ver,
                    "mismatched": mismatched}

        # PARTIAL mismatch on a data-table (.pabgb / .pabgh / .pamt):
        # Shipping half-patched data crashes the game (socket counts vs
        # cost tables drift, entry counts vs entries drift, etc.).
        # Kliff Wears Damiane V2 is the reference case: 458/464 patches
        # apply, 6 miss, a 4.6 MB delta gets stored, user enables it,
        # game crashes on splash. Abort the import with a clear error.
        gf_lower = game_file.lower()
        is_data_table = (gf_lower.endswith(".pabgb")
                         or gf_lower.endswith(".pabgh")
                         or gf_lower.endswith(".pamt"))
        if mismatched > 0 and is_data_table:
            game_ver = patch_data.get("game_version", "unknown")
            logger.error(
                "JSON import: aborting — %d of %d patches mismatched on "
                "data table %s. Shipping a partial data table crashes "
                "the game. Mod was built for game version %s.",
                mismatched, applied + mismatched, game_file, game_ver)
            db.connection.execute("DELETE FROM mods WHERE id = ?", (mod_id,))
            db.connection.commit()
            return {"changed_files": [], "version_mismatch": True,
                    "game_file": game_file, "game_version": game_ver,
                    "mismatched": mismatched,
                    "partial_abort": True,
                    "patches_applied": applied,
                    "patches_total": applied + mismatched}

        if bytes(modified) == plaintext:
            # Content unchanged. Could mean: (a) patches had no effect, or
            # (b) game file already has the patched values (mod already applied).
            # For case (b), try to get vanilla content to compare against.
            # If modified differs from vanilla, the mod IS doing something.
            vanilla_content = None
            if applied > 0:
                try:
                    van_entry = _find_pamt_entry(game_file, vanilla_dir)
                    if van_entry and os.path.exists(van_entry.paz_file):
                        vanilla_content = _extract_from_paz(van_entry)
                except Exception:
                    pass
            if vanilla_content is not None and bytes(modified) != vanilla_content:
                logger.info("Mod already applied to %s, using current content as delta", game_file)
            elif vanilla_content is None and applied > 0:
                logger.info("Mod likely already applied to %s (no vanilla to verify), creating delta", game_file)
            else:
                logger.info("No changes after patching %s, skipping", game_file)
                continue

        # Determine PAZ file path for this entry
        pamt_dir = _derive_pamt_dir(entry.paz_file)
        paz_file_path = f"{pamt_dir}/{entry.paz_index}.paz"

        # Save as ENTR delta
        metadata = {
            "pamt_dir": pamt_dir,
            "entry_path": entry.path,
            "paz_index": entry.paz_index,
            "compression_type": entry.compression_type,
            "flags": entry.flags,
            "vanilla_offset": entry.offset,
            "vanilla_comp_size": entry.comp_size,
            "vanilla_orig_size": entry.orig_size,
            "encrypted": entry.encrypted,
        }

        # Semantic annotation: mark entry path as semantically parseable
        # Full field-level diff requires both .pabgb body + .pabgh header,
        # which are only available at the PAZ level (not individual entries).
        # The semantic engine handles this during Apply/conflict detection.
        try:
            from cdumm.semantic.parser import identify_table_from_path
            sem_table = identify_table_from_path(entry.path)
            if sem_table:
                metadata["semantic_table"] = sem_table
                logger.info("Semantic: %s is parseable table '%s'",
                            entry.path, sem_table)
        except Exception:
            pass

        safe_name = entry.path.replace("/", "_") + ".entr"
        delta_path = deltas_dir / str(mod_id) / safe_name
        save_entry_delta(bytes(modified), metadata, delta_path)

        # DB entry
        db.connection.execute(
            "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
            "byte_start, byte_end, entry_path) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mod_id, paz_file_path, str(delta_path),
             entry.offset, entry.offset + entry.comp_size, entry.path))

        changed_files.append({
            "file_path": paz_file_path,
            "entry_path": entry.path,
            "delta_path": str(delta_path),
        })

        logger.info("ENTR delta: %s in %s (comp=%d, orig=%d)",
                     entry.path, paz_file_path, entry.comp_size, entry.orig_size)

    # Archive JSON source for auto-reimport after game updates
    sources_dir = deltas_dir.parent / "sources" / str(mod_id)
    try:
        import shutil
        if sources_dir.exists():
            shutil.rmtree(sources_dir)
        sources_dir.mkdir(parents=True, exist_ok=True)
        # Copy the original JSON file and any sibling JSONs (for multi-preset mods)
        # Prefer _original_source (set by toggle picker) over _json_path (may be temp file)
        json_path = patch_data.get("_original_source") or patch_data.get("_json_path")
        if json_path and Path(json_path).exists():
            src = Path(json_path)
            if src.is_file():
                # Copy all sibling JSON files so Configure can show all presets
                parent = src.parent
                copied = False
                for sibling in parent.glob("*.json"):
                    shutil.copy2(sibling, sources_dir / sibling.name)
                    copied = True
                if not copied:
                    shutil.copy2(src, sources_dir / src.name)
            elif src.is_dir():
                shutil.copytree(src, sources_dir, dirs_exist_ok=True)
        db.connection.execute(
            "UPDATE mods SET source_path = ? WHERE id = ?",
            (str(sources_dir), mod_id))
        logger.info("Archived JSON source: %s -> %s", mod_name, sources_dir)
    except Exception as e:
        logger.warning("Failed to archive JSON source: %s", e)

    if not changed_files:
        # No changes produced — clean up the mod entry instead of leaving a zombie
        db.connection.execute("DELETE FROM mods WHERE id = ?", (mod_id,))
        db.connection.commit()
        logger.info("Removed empty mod entry %d (no changes)", mod_id)
        return {"mod_id": None, "changed_files": [], "name": mod_name}

    db.connection.commit()
    return {"mod_id": mod_id, "changed_files": changed_files, "name": mod_name}


# ── Mount-time patching (Phase 3) ──────────────────────────────────

def import_json_fast(
    patch_data: dict, game_dir: Path, db, mods_dir: Path,
    mod_name: str, existing_mod_id: int | None = None,
    modinfo: dict | None = None,
) -> dict | None:
    """Fast-import a JSON mod: store the file + lightweight DB entries only.

    No PAZ extraction, no delta generation, no compression.
    Patches are applied from vanilla at Apply time (mount-time patching).

    Returns result dict with mod_id and entry_paths, or None on failure.
    """
    patches = patch_data["patches"]
    logger.info("import_json_fast: '%s' (%d patches)", mod_name, len(patches))

    # Validate: check all game_files exist in PAMTs
    vanilla_dir = game_dir / "CDMods" / "vanilla"
    if not vanilla_dir.exists():
        vanilla_dir = game_dir

    entry_paths = []
    pamt_dirs = {}
    for patch in patches:
        game_file = patch["game_file"]
        entry = _find_pamt_entry(game_file, vanilla_dir)
        if entry is None:
            entry = _find_pamt_entry(game_file, game_dir)
        if entry is None:
            logger.error("import_json_fast: game file '%s' not found in PAMTs", game_file)
            return None
        pamt_dir = _derive_pamt_dir(entry.paz_file)
        paz_file_path = f"{pamt_dir}/{entry.paz_index}.paz"
        entry_paths.append({
            "game_file": game_file,
            "entry_path": entry.path,
            "paz_file_path": paz_file_path,
            "pamt_dir": pamt_dir,
            "offset": entry.offset,
            "comp_size": entry.comp_size,
        })
        pamt_dirs[game_file] = pamt_dir

    # Store JSON file in CDMods/mods/
    mods_dir.mkdir(parents=True, exist_ok=True)
    # JMM titles can contain `:` `/` `\` `<` `>` `"` `|` `?` `*`, any of
    # which raise OSError on Windows filesystems. Replace the reserved
    # set with an underscore. Two different titles — `Foo:Bar` and
    # `Foo?Bar` — both sanitize to `Foo_Bar`; append a short hash of
    # the ORIGINAL title to keep them distinct and prevent one import
    # from silently overwriting another mod's stored JSON.
    import re as _re_fn
    import hashlib as _hash
    _safe_name = _re_fn.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", mod_name).strip()
    if not _safe_name:
        _safe_name = "mod"
    if _safe_name != mod_name:
        _suffix = _hash.sha1(
            mod_name.encode("utf-8", errors="replace")).hexdigest()[:8]
        _safe_name = f"{_safe_name}_{_suffix}"
    json_dest = mods_dir / f"{_safe_name}.json"
    import json
    json_source_path = patch_data.get("_original_source") or patch_data.get("_json_path")
    if json_source_path and Path(json_source_path).exists():
        import shutil
        shutil.copy2(json_source_path, json_dest)
    else:
        # Write from parsed data
        export_data = {k: v for k, v in patch_data.items() if not k.startswith("_")}
        json_dest.write_text(json.dumps(export_data, indent=2), encoding="utf-8")

    # Create/update DB entry
    priority = db.connection.execute(
        "SELECT COALESCE(MAX(priority), 0) + 1 FROM mods").fetchone()[0]
    author = (modinfo or {}).get("author") or patch_data.get("author")
    version = (modinfo or {}).get("version") or patch_data.get("version")
    description = (modinfo or {}).get("description") or patch_data.get("description")

    game_ver_hash = None
    try:
        from cdumm.engine.version_detector import detect_game_version
        game_ver_hash = detect_game_version(game_dir)
    except Exception:
        pass

    if existing_mod_id:
        mod_id = existing_mod_id
        db.connection.execute("DELETE FROM mod_deltas WHERE mod_id = ?", (mod_id,))
        # Clear disabled_patches on reimport — indices may not match new version
        db.connection.execute(
            "UPDATE mods SET json_source = ?, game_version_hash = ?, disabled_patches = NULL WHERE id = ?",
            (str(json_dest), game_ver_hash, mod_id))
    else:
        cursor = db.connection.execute(
            "INSERT INTO mods (name, mod_type, priority, author, version, "
            "description, game_version_hash, json_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_prettify(mod_name), "paz", priority, author, version,
             description, game_ver_hash, str(json_dest)))
        mod_id = cursor.lastrowid

    # Create lightweight mod_deltas rows (for conflict detection + Apply)
    # No actual delta files — just entry_path references
    changed_files = []
    for ep in entry_paths:
        db.connection.execute(
            "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
            "byte_start, byte_end, entry_path) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mod_id, ep["paz_file_path"], "",
             ep["offset"], ep["offset"] + ep["comp_size"], ep["entry_path"]))
        changed_files.append({
            "file_path": ep["paz_file_path"],
            "entry_path": ep["entry_path"],
            "delta_path": "",
        })

    # Also archive JSON source for Configure/preset picker
    sources_dir = mods_dir.parent / "sources" / str(mod_id)
    try:
        import shutil
        if sources_dir.exists():
            shutil.rmtree(sources_dir)
        sources_dir.mkdir(parents=True, exist_ok=True)
        if json_source_path and Path(json_source_path).exists():
            src = Path(json_source_path)
            if src.is_file():
                # Copy source + siblings only if in a mod-specific folder
                # (not a crowded downloads dir). Limit to 20 files max.
                siblings = list(src.parent.glob("*.json"))
                if len(siblings) <= 20:
                    for sibling in siblings:
                        shutil.copy2(sibling, sources_dir / sibling.name)
                else:
                    shutil.copy2(src, sources_dir / src.name)
            elif src.is_dir():
                shutil.copytree(src, sources_dir, dirs_exist_ok=True)
        db.connection.execute(
            "UPDATE mods SET source_path = ? WHERE id = ?",
            (str(sources_dir), mod_id))
    except Exception as e:
        logger.warning("Failed to archive JSON source: %s", e)

    db.connection.commit()
    logger.info("import_json_fast: stored '%s' (mod_id=%d, %d entries)",
                mod_name, mod_id, len(entry_paths))
    return {"mod_id": mod_id, "changed_files": changed_files, "name": mod_name}


class VanillaSourceUnavailable(Exception):
    """Raised by a vanilla-source resolver when no safe source exists.

    ``safe`` means either the vanilla backup is present, or the live
    PAZ has been hash-verified against the snapshot fingerprint. When
    neither holds, the patch must be skipped — applying against modded
    bytes would produce a corrupt overlay.
    """


def process_json_patches_for_overlay(
    mod_id: int, json_source: str, game_dir: Path,
    disabled_indices: list[int] | None = None,
    custom_values: dict | None = None,
    vanilla_source_resolver=None,
    errors_out: list[str] | None = None,
) -> list[tuple[bytes, dict]]:
    """Process a JSON mod's patches at Apply time (mount-time patching).

    Reads the stored JSON, extracts each target from vanilla PAZ,
    applies byte patches with pattern scan, and returns overlay entries.

    If disabled_indices is provided, individual changes at those flat
    indices are skipped (per-patch toggle feature).

    ``vanilla_source_resolver`` is an optional callable that takes a
    game-relative file path and returns a :class:`PazEntry` pointing at
    a known-clean (vanilla or hash-verified live) PAZ. Raising
    :class:`VanillaSourceUnavailable` causes the patch to be skipped
    with a logged error. When not supplied, the legacy inline lookup
    is used — the caller in apply_engine.py normally provides a
    resolver so the live-PAZ fallback can self-heal after a missing
    vanilla backup.

    ``errors_out`` is an optional mutable list the function will append
    user-facing error strings to — used to surface partial-apply aborts
    (Kliff Wears Damiane style mods that mismatch against the current
    game version) via InfoBar without crashing the game on a half-
    patched data table.

    Returns list of (decompressed_content, metadata) tuples ready for
    the overlay builder.
    """
    import json
    json_path = Path(json_source)
    if not json_path.exists():
        logger.error("JSON source not found: %s", json_source)
        return []

    patch_data = json.loads(json_path.read_text(encoding="utf-8"))
    patches = patch_data.get("patches", [])
    if not patches:
        return []

    vanilla_dir = game_dir / "CDMods" / "vanilla"
    if not vanilla_dir.exists():
        vanilla_dir = game_dir

    overlay_entries = []
    disabled = set(disabled_indices) if disabled_indices else set()
    flat_idx = 0  # global index across all patches' changes

    for patch in patches:
        game_file = patch["game_file"]
        all_changes = patch.get("changes", [])
        if not all_changes:
            continue

        # Filter out disabled changes (per-patch toggle)
        if disabled:
            changes = []
            for c in all_changes:
                if flat_idx not in disabled:
                    changes.append(c)
                flat_idx += 1
        else:
            changes = all_changes
            flat_idx += len(all_changes)

        # Apply custom values (inline editing) to patched bytes
        if custom_values:
            changes = apply_custom_values(changes, custom_values)

        if not changes:
            continue

        # Find entry in PAMT — prefer vanilla backup over game dir.
        # When the caller supplied a resolver, it's responsible for
        # deciding vanilla vs live (with hash verification) so the
        # bytes we get back are always safe to treat as vanilla.
        if vanilla_source_resolver is not None:
            try:
                entry = vanilla_source_resolver(game_file)
            except VanillaSourceUnavailable as e:
                logger.error("mount-time: %s", e)
                continue
            try:
                plaintext = _extract_from_paz(entry)
            except Exception as e:
                logger.error("mount-time: failed to extract '%s': %s",
                             game_file, e)
                continue
            vanilla_ref = bytes(plaintext)
        else:
            from_vanilla = True
            entry = _find_pamt_entry(game_file, vanilla_dir)
            if entry is None:
                entry = _find_pamt_entry(game_file, game_dir)
                from_vanilla = False
            if entry is None:
                logger.error("mount-time: game file '%s' not found",
                             game_file)
                continue
            try:
                plaintext = _extract_from_paz(entry)
            except Exception as e:
                logger.error("mount-time: failed to extract '%s': %s",
                             game_file, e)
                continue
            # For pattern scan: only trust as vanilla if actually vanilla
            vanilla_ref = bytes(plaintext) if from_vanilla else None

        # v2 entry-anchored: build name→offset map when any change uses `entry`
        name_offsets = None
        if any(c.get("entry") for c in changes):
            pabgh_file = game_file.rsplit(".", 1)[0] + ".pabgh"
            pabgh_entry = None
            if vanilla_source_resolver is not None:
                try:
                    pabgh_entry = vanilla_source_resolver(pabgh_file)
                except VanillaSourceUnavailable:
                    pabgh_entry = None
            else:
                pabgh_entry = _find_pamt_entry(pabgh_file, vanilla_dir)
                if pabgh_entry is None:
                    pabgh_entry = _find_pamt_entry(pabgh_file, game_dir)
            if pabgh_entry:
                try:
                    pabgh_plain = _extract_from_paz(pabgh_entry)
                    name_offsets = _build_name_offsets_for_v2(
                        game_file, bytes(plaintext), pabgh_plain)
                except Exception as e_pabgh:
                    logger.warning("mount-time: v2 index build failed for %s: %s",
                                   pabgh_file, e_pabgh)

        # Apply byte patches with pattern scan against vanilla. Also capture
        # inserts so we can shift a companion .pabgh (JMM parity).
        modified = bytearray(plaintext)
        signature = patch.get("signature")
        inserts_out: list[tuple[int, int]] = []
        applied, mismatched, relocated = _apply_byte_patches(
            modified, changes, signature=signature, vanilla_data=vanilla_ref,
            name_offsets=name_offsets, inserts_out=inserts_out)

        if applied == 0 and mismatched > 0:
            logger.warning("mount-time: all patches mismatched for '%s' — game update?",
                          game_file)
            if errors_out is not None:
                errors_out.append(
                    f"{Path(json_source).stem}: all {mismatched} patches "
                    f"mismatched against vanilla {game_file} — the mod "
                    f"was built for a different game version.")
            continue

        # Strict-abort for data-table files (.pabgb / .pabgh / .pamt):
        # these formats mix inserts with cumulative-offset tracking and
        # replaces that encode counts/sizes elsewhere. Shipping a
        # partially-applied data table is how Kliff Wears Damiane style
        # mods crash the game before the main menu. Better to refuse
        # the apply and tell the user the mod is incompatible than to
        # ship a half-patched iteminfo.pabgb.
        gf_lower = game_file.lower()
        is_data_table = (gf_lower.endswith(".pabgb")
                         or gf_lower.endswith(".pabgh")
                         or gf_lower.endswith(".pamt"))
        if mismatched > 0 and is_data_table:
            logger.error(
                "mount-time: aborting overlay for '%s' — %d of %d patches "
                "mismatched against vanilla. Data tables cannot be partially "
                "applied (causes game crashes). Mod likely built for a "
                "different game version.",
                game_file, mismatched, applied + mismatched)
            if errors_out is not None:
                mod_name = Path(json_source).stem
                errors_out.append(
                    f"{mod_name} skipped: {mismatched} of "
                    f"{applied + mismatched} patches don't match vanilla "
                    f"{game_file}. Shipping a partial data table would "
                    f"crash the game. This mod was likely built for a "
                    f"different game version — check the mod page for an "
                    f"updated release.")
            continue

        if bytes(modified) == plaintext:
            logger.debug("mount-time: no changes for '%s', skipping", game_file)
            continue

        pamt_dir = _derive_pamt_dir(entry.paz_file)
        metadata = {
            "entry_path": entry.path,
            "pamt_dir": pamt_dir,
            "compression_type": entry.compression_type,
        }
        # JMM parity: preserve the vanilla's encryption state on the overlay
        # so the game's VFS decoder treats the bytes the same way it would
        # have treated the original PAZ entry. Pass the vanilla flags ushort
        # verbatim — JMM writes it into the overlay PAMT unchanged.
        if getattr(entry, "encrypted", False):
            metadata["encrypted"] = True
            metadata["crypto_filename"] = entry.path.rsplit("/", 1)[-1]
            metadata["vanilla_flags"] = entry.flags & 0xFFFF

        overlay_entries.append((bytes(modified), metadata))
        logger.info("mount-time: patched '%s' (%d applied, %d relocated, %d inserts)",
                    game_file, applied, relocated, len(inserts_out))

        # JMM FixupPabghAfterInserts: if we inserted bytes into a .pabgb,
        # the companion .pabgh must have its entry pointers shifted so the
        # game can still find each blob. Emit the fixed .pabgh as an extra
        # overlay entry so it overrides vanilla at load time.
        if inserts_out and game_file.lower().endswith(".pabgb"):
            pabgh_file = game_file.rsplit(".", 1)[0] + ".pabgh"
            pabgh_entry_for_fixup = None
            if vanilla_source_resolver is not None:
                try:
                    pabgh_entry_for_fixup = vanilla_source_resolver(pabgh_file)
                except VanillaSourceUnavailable:
                    pabgh_entry_for_fixup = None
            else:
                pabgh_entry_for_fixup = _find_pamt_entry(pabgh_file, vanilla_dir)
                if pabgh_entry_for_fixup is None:
                    pabgh_entry_for_fixup = _find_pamt_entry(pabgh_file, game_dir)
            if pabgh_entry_for_fixup is None:
                logger.warning(
                    "mount-time: inserts into %s but no companion .pabgh found — "
                    "overlay will ship vanilla .pabgh, game may read stale offsets",
                    game_file,
                )
            else:
                try:
                    pabgh_plain = _extract_from_paz(pabgh_entry_for_fixup)
                    fixed_pabgh = fixup_pabgh_after_inserts(
                        bytes(pabgh_plain), inserts_out)
                    pabgh_pamt_dir = _derive_pamt_dir(
                        pabgh_entry_for_fixup.paz_file)
                    overlay_entries.append((fixed_pabgh, {
                        "entry_path": pabgh_entry_for_fixup.path,
                        "pamt_dir": pabgh_pamt_dir,
                        "compression_type": pabgh_entry_for_fixup.compression_type,
                    }))
                    logger.info(
                        "mount-time: emitted fixed .pabgh companion for %s (%d inserts)",
                        pabgh_file, len(inserts_out),
                    )
                except Exception as e_fix:
                    logger.error(
                        "mount-time: PABGH fixup failed for %s: %s",
                        pabgh_file, e_fix, exc_info=True,
                    )

    return overlay_entries
