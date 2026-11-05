"""PAPGT authority — single point of control for meta/0.papgt.

The mod manager ALWAYS rebuilds PAPGT from scratch on every apply.
No individual mod ever writes to PAPGT directly.

PAPGT format:
  [0:4]  = metadata (DO NOT modify)
  [4:8]  = file integrity hash: hashlittle(papgt[12:], 0xC5EDE)
  [8:12] = metadata (DO NOT modify)
  [12:]  = N x 12-byte entries + 4-byte string table size + string table

Each 12-byte entry:
  [0:4]  = flags (e.g., 00 FF 3F 00)
  [4:8]  = offset into string table (null-terminated ASCII dir name)
  [8:12] = PAMT hash for this directory: hashlittle(pamt[12:], 0xC5EDE)

After entries: 4-byte LE uint = string table size in bytes
Then: string table (null-terminated ASCII strings)
"""
import logging
import struct
from pathlib import Path

from cdumm.archive.hashlittle import compute_pamt_hash, compute_papgt_hash

logger = logging.getLogger(__name__)

ENTRY_SIZE = 12  # bytes per directory entry

# Default LangType: 0x3FFF means "all languages" — matches the value the
# game ships in vanilla PAPGT entries and JMM 9.9.2's BuildPapgtWithMod
# default (ModManager.cs:2902, langType=16383).
DEFAULT_LANG_TYPE = 0x3FFF


def _compact_papgt_layout(data: bytes) -> tuple[int, int] | None:
    """Recognise 2.04's four-byte prefix and opaque 64-byte trailer.

    This only frames existing entries. It deliberately makes no claim about
    regenerating the trailer or about the engine accepting a modified index.
    """
    for count in range(1, max(1, (len(data) - 72) // ENTRY_SIZE + 1)):
        size_pos = 4 + count * ENTRY_SIZE
        size = struct.unpack_from('<I', data, size_pos)[0]
        start = size_pos + 4
        end = start + size
        if size == 0 or end + 64 != len(data):
            continue
        names = data[start:end]
        seen = set()
        for i in range(count):
            offset = struct.unpack_from('<I', data, 4 + i * ENTRY_SIZE + 4)[0]
            if offset >= size or (offset and names[offset - 1] != 0):
                break
            name_end = names.find(b'\0', offset)
            name = names[offset:name_end] if name_end >= 0 else b''
            if len(name) != 4 or not name.isdigit() or name in seen:
                break
            seen.add(name)
        else:
            return count, start
    return None


def decode_papgt_entry_flags(flags: int) -> tuple[int, int, int]:
    """Split a PAPGT entry's first 4 bytes into the named fields.

    The vanilla game and JMM 9.9.2 (ModManager.cs:2911-2919) treat these
    bytes as ``IsOptional`` (byte 0), ``LangType`` (uint16 little-endian,
    bytes 1-2) and ``Zero`` (byte 3). Returns ``(is_optional, lang_type,
    zero)``.
    """
    is_optional = flags & 0xFF
    lang_type = (flags >> 8) & 0xFFFF
    zero = (flags >> 24) & 0xFF
    return is_optional, lang_type, zero


def encode_papgt_entry_flags(
    is_optional: int = 0,
    lang_type: int = DEFAULT_LANG_TYPE,
    zero: int = 0,
) -> int:
    """Inverse of :func:`decode_papgt_entry_flags` — pack three fields
    into the historical 4-byte ``flags`` uint32 the game expects."""
    return (
        (is_optional & 0xFF)
        | ((lang_type & 0xFFFF) << 8)
        | ((zero & 0xFF) << 24)
    )


def read_papgt_entries(path: Path) -> "list[tuple[str, int, int]] | None":
    """Parse a PAPGT file into ``[(dir_name, flags, pamt_hash), ...]``.

    Returns ``None`` when the file is missing or malformed — callers use
    this for *advisory* data (vanilla entry names, reserved dir numbers)
    and must degrade gracefully rather than crash mid-apply.
    """
    try:
        data = path.read_bytes()
        if len(data) < 12:
            return None
        compact = _compact_papgt_layout(data)
        entry_start = 4 if compact else 12
        entry_count = compact[0] if compact else _find_entry_count(bytearray(data), entry_start)
        string_table_start = compact[1] if compact else entry_start + entry_count * ENTRY_SIZE + 4
        out: list[tuple[str, int, int]] = []
        for i in range(entry_count):
            pos = entry_start + i * ENTRY_SIZE
            flags, name_offset, pamt_hash = struct.unpack_from("<III", data, pos)
            name = _read_string(bytearray(data), string_table_start, name_offset)
            if name:
                out.append((name, flags, pamt_hash))
        return out
    except (OSError, ValueError, struct.error):
        return None


def reserved_papgt_dir_numbers(game_dir: Path,
                               vanilla_dir: "Path | None" = None) -> "set[int]":
    """Numeric dir names claimed by the live and vanilla PAPGT indexes.

    Crimson Desert 2.0 (build 24934353) ships five PLACEHOLDER entries
    0036-0040 in vanilla meta/0.papgt — optional language-pack slots
    (is_optional=1, single-bit lang_type) whose directories do not exist
    on disk for most installs. Any dir number CDUMM allocates for an
    overlay or standalone mod must not collide with these reserved
    names, or the game applies the placeholder's optional/language
    gating to the mod content and silently never mounts it (GitHub
    #383, EnefFlow).
    """
    reserved: set[int] = set()
    for papgt in (game_dir / "meta" / "0.papgt",
                  (vanilla_dir / "meta" / "0.papgt") if vanilla_dir else None):
        if papgt is None:
            continue
        entries = read_papgt_entries(papgt)
        if not entries:
            continue
        for name, _flags, _h in entries:
            if name.isdigit() and len(name) == 4:
                reserved.add(int(name))
    return reserved


def language_pack_dirs(game_dir: Path,
                       vanilla_dir: "Path | None" = None) -> "set[str]":
    """Numbered directories on disk that hold Steam-delivered OPTIONAL
    content, not mod content.

    Crimson Desert 2.0 ("Enhanced") added voice packs for German, French,
    Spanish, Brazilian Portuguese and Japanese. Steam downloads the
    selected pack into one of the five reserved slots 0036-0040 -- the
    PAPGT entries flagged ``is_optional=1`` with a single language bit.
    Every "0036+ means a mod put it there" rule in CDUMM therefore has
    to exempt these, or a rescan / health check deletes the user's
    language pack and the game silently drops back to English (Nexus,
    Dante1963, 27 Aug 2026).

    A directory qualifies when it exists on disk, the live (or vanilla)
    PAPGT lists it as optional, and it carries no
    ``_cdumm_overlay.marker`` -- a CDUMM overlay that squatted one of
    these slots (pre-3.16.1) is still CDUMM's to clean up.
    """
    optional: set[str] = set()
    for papgt in (game_dir / "meta" / "0.papgt",
                  (vanilla_dir / "meta" / "0.papgt") if vanilla_dir else None):
        if papgt is None:
            continue
        for name, flags, _h in (read_papgt_entries(papgt) or []):
            if not (name.isdigit() and len(name) == 4):
                continue
            is_optional, _lang, _zero = decode_papgt_entry_flags(flags)
            if is_optional:
                optional.add(name)
    out: set[str] = set()
    for name in optional:
        d = game_dir / name
        try:
            if d.is_dir() and not (d / "_cdumm_overlay.marker").exists():
                out.add(name)
        except OSError:
            continue
    return out


def strip_dangling_entries(game_dir: Path) -> int:
    """Remove non-optional PAPGT entries whose directory is gone.

    Called right before a vanilla snapshot, after the rescan path has
    deleted leftover mod dirs (#393). Uses the normal rebuild with no
    snapshot as base, so optional language-pack placeholders survive
    and every kept entry is rehashed from disk. Returns the number of
    entries dropped; 0 when the file needed no change.
    """
    papgt = game_dir / "meta" / "0.papgt"
    entries = read_papgt_entries(papgt)
    if not entries:
        return 0
    dangling = [
        n for n, f, _h in entries
        if n.isdigit() and len(n) == 4 and int(n) >= 36
        and not (game_dir / n).exists()
        and not decode_papgt_entry_flags(f)[0]
    ]
    if not dangling:
        return 0
    rebuilt = PapgtManager(game_dir, None).rebuild()
    papgt.write_bytes(rebuilt)
    logger.info("PAPGT: stripped dangling entries before snapshot: %s", dangling)
    return len(dangling)


class PapgtManager:
    """Manages PAPGT rebuild from scratch."""

    def __init__(self, game_dir: Path, vanilla_dir: Path | None = None) -> None:
        self._game_dir = game_dir
        self._papgt_path = game_dir / "meta" / "0.papgt"
        self._vanilla_papgt = vanilla_dir / "meta" / "0.papgt" if vanilla_dir else None

    def rebuild(self, modified_pamts: dict[str, bytes] | None = None,
                mod_papgt: bytes | None = None,
                mod_entry_options: dict[str, dict] | None = None,
                exclude_dirs: "set[str] | None" = None) -> bytes:
        """Rebuild PAPGT with correct hashes for all directories.

        Starts from the vanilla PAPGT structure (or mod-shipped PAPGT if
        provided), then:
        1. Removes entries for directories that don't exist on disk
           (cleaned-up mod directories)
        2. Adds entries for new directories in modified_pamts
        3. Updates all PAMT hashes

        Args:
            modified_pamts: dict of {dir_name: pamt_bytes} for directories
                           that have been modified by mods.
            mod_papgt: if a mod ships its own PAPGT (e.g., overlay mods that
                      add new directories), use it as the base instead of
                      vanilla. The mod's PAPGT includes the correct entries
                      and ordering for the new directories.

        Returns:
            The rebuilt PAPGT bytes.
        """
        # Inspect the live index even when an older vanilla snapshot or a
        # mod-supplied index would otherwise become the rebuild base (#458).
        # The legacy writer cannot represent 2.04's prefix/trailer. Refuse
        # before the apply transaction can replace the game's entry list.
        candidates = [mod_papgt] if mod_papgt else []
        for path in (self._papgt_path, self._vanilla_papgt):
            if path is not None and path.exists():
                candidates.append(path.read_bytes())
        for data in candidates:
            if _compact_papgt_layout(data) is not None:
                raise ValueError(
                    'Game 2.04 archive index detected. Rebuilding this PAPGT format '
                    'is not supported yet; the original index has been preserved.')
            _find_entry_count(bytearray(data), 12)
        # Use mod-shipped PAPGT as base if available (it includes new dirs
        # with correct flags and ordering). Otherwise use vanilla.
        if mod_papgt and len(mod_papgt) >= 12:
            papgt = bytearray(mod_papgt)
            logger.info("PAPGT rebuild using mod-shipped base (%d bytes)", len(mod_papgt))
        else:
            base_path = self._vanilla_papgt if self._vanilla_papgt and self._vanilla_papgt.exists() else self._papgt_path
            if not base_path.exists():
                # GitHub #65 (tbyk101, 2026-05-03): use the 3-arg form
                # so e.filename is populated. Bare
                # `raise FileNotFoundError(message)` leaves .filename
                # as None, and the top-level apply error then renders
                # '(unknown path)' instead of the actual missing
                # PAPGT path.
                raise FileNotFoundError(
                    2, "PAPGT not found", str(base_path))
            papgt = bytearray(base_path.read_bytes())

        if len(papgt) < 12:
            raise ValueError("PAPGT file too small")

        # Preserve header metadata (bytes 0:4 and 8:12 are NOT hashes)
        header_meta0 = papgt[0:4]
        header_meta8 = papgt[8:12]

        entry_start = 12
        entry_count = _find_entry_count(papgt, entry_start)
        string_table_start = entry_start + entry_count * ENTRY_SIZE + 4

        base_name = "mod-shipped" if mod_papgt else base_path.name
        logger.info("PAPGT base: %d entries from %s", entry_count, base_name)

        # Parse existing entries with their directory names
        parsed_entries: list[tuple[str, int, int]] = []  # (dir_name, flags, pamt_hash)
        for i in range(entry_count):
            pos = entry_start + i * ENTRY_SIZE
            flags = struct.unpack_from("<I", papgt, pos)[0]
            name_offset = struct.unpack_from("<I", papgt, pos + 4)[0]
            pamt_hash = struct.unpack_from("<I", papgt, pos + 8)[0]
            dir_name = _read_string(papgt, string_table_start, name_offset)
            if dir_name:
                parsed_entries.append((dir_name, flags, pamt_hash))

        # Determine which directories should be in the PAPGT:
        # - Keep entries where the PAMT exists on disk OR is in modified_pamts
        # - Remove entries for directories that no longer exist (mod cleanup)
        # - exclude_dirs: dirs the caller has PLANNED to delete but is
        #   deferring until after the transaction commits (audit
        #   finding C1, 2026-06-10: deleting overlay dirs before
        #   commit meant a failed commit rolled back to a PAPGT that
        #   referenced already-deleted dirs, which crashes the game at
        #   boot). Treat them as already gone so the new PAPGT never
        #   references them.
        excluded = exclude_dirs or set()

        # Vanilla entry set from the pristine snapshot PAPGT. Crimson
        # Desert 2.0 (build 24934353) ships PLACEHOLDER entries 0036-0040
        # — optional language-pack slots with no directory on disk — so
        # "digit < 36" is no longer a valid vanilla test (GitHub #383,
        # EnefFlow: the old test stripped 4 vanilla entries and let the
        # overlay squat slot 0037, inheriting is_optional=1 flags the
        # game uses to skip it). The snapshot is re-taken on every game
        # update (fingerprint-stamped), so its PAPGT tracks the current
        # build's entry set.
        vanilla_entries: dict[str, tuple[int, int]] = {}
        if self._vanilla_papgt is not None:
            for _n, _f, _h in (read_papgt_entries(self._vanilla_papgt) or []):
                vanilla_entries[_n] = (_f, _h)

        live_entries: list[tuple[str, int, int]] = []
        removed = []
        for dir_name, flags, pamt_hash in parsed_entries:
            if dir_name in excluded:
                removed.append(dir_name)
                continue
            pamt_on_disk = (self._game_dir / dir_name / "0.pamt").exists()
            in_modified = modified_pamts and dir_name in modified_pamts
            # Keep vanilla entries even if not on disk — 2.0 ships
            # placeholder entries whose dirs don't exist. Fall back to
            # the historical "digit < 36" floor when no snapshot PAPGT
            # is available. Only remove mod-added dirs.
            is_vanilla_dir = dir_name in vanilla_entries
            if not is_vanilla_dir:
                try:
                    is_vanilla_dir = int(dir_name) < 36
                except (ValueError, TypeError):
                    is_vanilla_dir = True
            # DANGLING entries (no dir on disk) are kept ONLY when the
            # entry itself says it is optional content — that is how
            # 2.0's five language-pack placeholders (0036-0040) look, and
            # it holds even when the vanilla snapshot is stale. A dangling
            # NON-optional entry is a leftover: the rescan path deletes
            # mod dirs before snapshotting without rebuilding the PAPGT,
            # so the snapshot itself can carry one (#393, delichandelarosse:
            # 'Missing directory 0052' after every apply, game refused to
            # boot). Such an entry must be dropped, never kept or restored.
            is_optional, _lt, _z = decode_papgt_entry_flags(flags)
            dir_missing = not (self._game_dir / dir_name).exists()
            try:
                _mod_range = int(dir_name) >= 36
            except (ValueError, TypeError):
                _mod_range = False
            if dir_missing and _mod_range and not is_optional and not in_modified:
                removed.append(dir_name)
                continue
            if pamt_on_disk or in_modified or is_vanilla_dir or dir_missing:
                live_entries.append((dir_name, flags, pamt_hash))
            else:
                removed.append(dir_name)

        # Restore vanilla entries missing from the base (heals installs
        # mutilated by the pre-fix "digit < 36" removal, and installs
        # whose squatted placeholder dir is being deleted this apply —
        # the ENTRY must survive with its vanilla flags+hash even though
        # the dir goes away, because that is exactly how vanilla ships).
        restored: list[str] = []
        _present = {e[0] for e in live_entries}
        for _n, (_f, _h) in vanilla_entries.items():
            if _n in _present:
                continue
            if modified_pamts and _n in modified_pamts:
                continue  # actively rewritten this apply; added below
            # Only optional placeholders, or entries whose dir really
            # exists, are worth restoring. A non-optional snapshot entry
            # with no dir on disk is a mod leftover the snapshot captured
            # (see above); restoring it references a dir the game cannot
            # find and it refuses to start (#393).
            _opt, _lt2, _z2 = decode_papgt_entry_flags(_f)
            try:
                _mod_range2 = int(_n) >= 36
            except (ValueError, TypeError):
                _mod_range2 = False
            if _mod_range2 and not _opt and not (self._game_dir / _n).exists():
                logger.info("PAPGT: not restoring dangling non-optional "
                            "snapshot entry %s (mod leftover)", _n)
                continue
            live_entries.append((_n, _f, _h))
            restored.append(_n)
        if restored:
            logger.info("PAPGT: restored %d vanilla entries: %s",
                        len(restored), restored)

        if removed:
            logger.info("PAPGT: removing %d stale entries: %s", len(removed), removed)

        # Add new directories not already in PAPGT.
        # Sources: modified_pamts AND directories on disk with a PAMT file
        # (standalone mods create 0036+ directories with their own PAZ/PAMT).
        existing_names = {e[0] for e in live_entries}
        new_dirs = []
        if modified_pamts:
            for dir_name in sorted(modified_pamts.keys()):
                if dir_name not in existing_names:
                    new_dirs.append(dir_name)
                    existing_names.add(dir_name)

        # Scan for new directories on disk (0036+) not in PAPGT
        try:
            for d in sorted(self._game_dir.iterdir()):
                if not d.is_dir() or not d.name.isdigit() or len(d.name) != 4:
                    continue
                if int(d.name) < 36:
                    continue  # vanilla directories, already in PAPGT
                if d.name in existing_names:
                    continue  # already covered
                if d.name in excluded:
                    continue  # deletion deferred until post-commit
                if (d / "0.pamt").exists():
                    new_dirs.append(d.name)
                    existing_names.add(d.name)
                    logger.info("PAPGT: discovered new directory on disk: %s", d.name)
        except OSError:
            pass

        # New mod directories default to is_optional=0, lang_type=0x3FFF
        # (= 0x003FFF00 packed) — same as vanilla data dirs 0000-0017.
        # Callers can override per-dir via ``mod_entry_options`` to mark
        # an entry optional or target a specific language group, matching
        # JMM 9.9.2's BuildPapgtWithMod isOptional / langType parameters.
        default_new_flags = encode_papgt_entry_flags()
        opts = mod_entry_options or {}

        if new_dirs:
            logger.info("PAPGT: adding %d new entries: %s", len(new_dirs), new_dirs)

        # Build the complete entry list: NEW dirs first (so they override
        # existing entries — game loads PAPGT in order, first match wins),
        # then existing dirs.
        all_entries: list[tuple[str, int]] = []  # (dir_name, flags)
        for dir_name in new_dirs:
            override = opts.get(dir_name)
            if override:
                flags_u32 = encode_papgt_entry_flags(
                    is_optional=int(override.get("is_optional", 0)),
                    lang_type=int(override.get("lang_type", DEFAULT_LANG_TYPE)),
                    zero=int(override.get("zero", 0)),
                )
            else:
                flags_u32 = default_new_flags
            all_entries.append((dir_name, flags_u32))
        for dir_name, flags, _ in live_entries:
            all_entries.append((dir_name, flags))

        # Build string table
        string_table = bytearray()
        name_offsets: dict[str, int] = {}
        for dir_name, _ in all_entries:
            if dir_name not in name_offsets:
                name_offsets[dir_name] = len(string_table)
                string_table += dir_name.encode("ascii") + b"\x00"

        # Construct new PAPGT
        result = bytearray()
        result += header_meta0          # [0:4] metadata
        result += b"\x00\x00\x00\x00"   # [4:8] hash placeholder
        result += header_meta8          # [8:12] metadata

        # Update entry count in header byte 8
        new_count = len(all_entries)
        result[8] = new_count & 0xFF

        # Write entries — verify and rehash as needed.
        # When using a mod-shipped PAPGT as base, its hashes for vanilla
        # directories may be stale (mod built on older game version).
        # Always verify existing hashes against the actual PAMT on disk.
        existing_hashes = {d: h for d, _, h in parsed_entries}
        # Restored vanilla entries aren't in the parsed base — carry
        # their vanilla hashes so the hash loop below doesn't zero them.
        for _n in restored:
            existing_hashes.setdefault(_n, vanilla_entries[_n][1])
        modified_set = set(modified_pamts.keys()) if modified_pamts else set()
        is_mod_base = mod_papgt is not None
        rehashed = 0

        restored_set = set(restored)
        for dir_name, flags in all_entries:
            if dir_name in restored_set and dir_name in excluded:
                # Vanilla entry restored over a squatted placeholder dir
                # that is deleted right after commit — its pamt is still
                # on disk NOW, so the rehash branches below would pin the
                # hash of a file about to vanish. Use the vanilla hash:
                # that is the exact end-state the game ships.
                pamt_hash = vanilla_entries[dir_name][1]
            elif dir_name in modified_set:
                # PAMT was modified — recompute hash from new data
                pamt_data = modified_pamts[dir_name]
                pamt_hash = compute_pamt_hash(pamt_data) if len(pamt_data) >= 12 else 0
                rehashed += 1
            elif dir_name in existing_hashes and not is_mod_base:
                # Verify hash against actual PAMT on disk. The PAPGT
                # base may have stale hashes from previous in-place
                # applies, other mod managers, OR a game patch that
                # rewrote the PAMT (Nexus reports 2026-05-03: 0009
                # and 0015 mismatch after Crimson Desert 1.05). Always
                # rehash from live bytes , the parallel mod-base
                # branch below has done this with no cap since v2.2.1
                # without crashing, so the old "memory pressure"
                # 2MB short-circuit was an over-cautious guard that
                # was actively producing the user-visible bug.
                pamt_path = self._game_dir / dir_name / "0.pamt"
                if pamt_path.exists():
                    try:
                        pamt_data = pamt_path.read_bytes()
                        actual_hash = (
                            compute_pamt_hash(pamt_data)
                            if len(pamt_data) >= 12 else 0)
                        if actual_hash != existing_hashes[dir_name]:
                            pamt_hash = actual_hash
                            rehashed += 1
                        else:
                            pamt_hash = existing_hashes[dir_name]
                    except OSError:
                        pamt_hash = existing_hashes[dir_name]
                else:
                    pamt_hash = existing_hashes[dir_name]
            elif dir_name in existing_hashes and is_mod_base:
                # Mod-shipped base — verify hash against actual PAMT on disk
                pamt_path = self._game_dir / dir_name / "0.pamt"
                if pamt_path.exists():
                    pamt_data = pamt_path.read_bytes()
                    actual_hash = compute_pamt_hash(pamt_data) if len(pamt_data) >= 12 else 0
                    if actual_hash != existing_hashes[dir_name]:
                        pamt_hash = actual_hash
                        rehashed += 1
                    else:
                        pamt_hash = existing_hashes[dir_name]
                else:
                    pamt_hash = existing_hashes[dir_name]
            elif dir_name in new_dirs:
                # New directory — read and hash its PAMT
                pamt_path = self._game_dir / dir_name / "0.pamt"
                if pamt_path.exists():
                    pamt_data = pamt_path.read_bytes()
                    pamt_hash = compute_pamt_hash(pamt_data) if len(pamt_data) >= 12 else 0
                else:
                    pamt_hash = 0
                rehashed += 1
            else:
                pamt_hash = 0

            result += struct.pack("<III", flags, name_offsets[dir_name], pamt_hash)

        if rehashed:
            logger.info("PAPGT: rehashed %d/%d directories", rehashed, len(all_entries))

        # Write string table size + string table
        result += struct.pack("<I", len(string_table))
        result += string_table

        # Compute and write PAPGT file hash at [4:8]
        papgt_hash = compute_papgt_hash(bytes(result))
        struct.pack_into("<I", result, 4, papgt_hash)

        logger.info("PAPGT rebuilt: %d entries (%d removed, %d added), hash=0x%08X",
                     new_count, len(removed), len(new_dirs), papgt_hash)

        return bytes(result)


def _find_entry_count(papgt: bytearray, entry_start: int) -> int:
    """Determine entry count by finding the string table size field.

    The string table size field is at entry_start + N*12, and its value
    satisfies: entry_start + N*12 + 4 + string_size == len(papgt).
    """
    file_size = len(papgt)
    for n in range(max(0, (file_size - entry_start - 4) // ENTRY_SIZE + 1)):
        size_field_pos = entry_start + n * ENTRY_SIZE
        if size_field_pos + 4 > file_size:
            break
        string_size = struct.unpack_from("<I", papgt, size_field_pos)[0]
        if size_field_pos + 4 + string_size == file_size:
            return n
    raise ValueError('Unsupported or corrupt PAPGT layout; refusing to estimate its entry count')


def _read_string(papgt: bytearray, string_table_start: int,
                 name_offset: int) -> str | None:
    """Read a null-terminated string from the PAPGT string table."""
    abs_offset = string_table_start + name_offset
    if abs_offset >= len(papgt):
        return None
    end = papgt.index(0, abs_offset) if 0 in papgt[abs_offset:] else len(papgt)
    name = papgt[abs_offset:end].decode("ascii", errors="replace")
    return name if name else None
