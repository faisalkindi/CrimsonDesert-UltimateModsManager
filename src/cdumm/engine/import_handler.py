import logging
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from cdumm.engine.delta_engine import generate_delta, get_changed_byte_ranges, save_delta
from cdumm.engine.snapshot_manager import SnapshotManager
from cdumm.storage.database import Database

logger = logging.getLogger(__name__)

SCRIPT_TIMEOUT = 60  # seconds

import time

# Thread-local progress callback for import operations.
# Set by ImportWorker before calling import functions.
import threading
_progress_local = threading.local()

def set_import_progress_cb(cb):
    _progress_local.cb = cb

def _emit_progress(pct, msg):
    cb = getattr(_progress_local, 'cb', None)
    if cb:
        cb(pct, msg)


def _next_priority(db: Database) -> int:
    """Get the next available priority value for a new mod."""
    cursor = db.connection.execute("SELECT COALESCE(MAX(priority), 0) + 1 FROM mods")
    return cursor.fetchone()[0]


def _json_mod_display_name(jp_data: dict, fallback: str) -> str:
    """Pick the best human display name for a JSON-patch mod.

    JMM spec nests title under 'modinfo.title'; older/simpler mods put
    'name' or 'title' at the top level. Read all four in priority order
    so Dark Mode Map doesn't get stuck with its zip filename.
    """
    mi = jp_data.get("modinfo")
    mi = mi if isinstance(mi, dict) else {}
    return (mi.get("title")
            or mi.get("name")
            or jp_data.get("title")
            or jp_data.get("name")
            or fallback)


def _json_mod_modinfo(jp_data: dict) -> dict:
    """Build the canonical modinfo dict for a JSON-patch mod.

    Merges modinfo.* with top-level equivalents so the DB row carries
    title/version/author/description regardless of which style the mod
    author used.
    """
    mi = jp_data.get("modinfo")
    mi = mi if isinstance(mi, dict) else {}
    return {
        "name": (mi.get("title") or mi.get("name")
                 or jp_data.get("title") or jp_data.get("name")),
        "version": mi.get("version") or jp_data.get("version"),
        "author": mi.get("author") or jp_data.get("author"),
        "description": (mi.get("description")
                        or jp_data.get("description")),
    }


def prettify_mod_name(raw: str) -> str:
    """Clean up a raw mod name for display.

    - Strip NexusMods IDs and timestamps (e.g. '-934-2-1775958271')
    - Strip version suffixes (e.g. 'v1.2', 'v0.3.1', '1.03.00')
    - Split CamelCase (e.g. 'CDLootMultiplier' -> 'CD Loot Multiplier')
    - Replace underscores, hyphens, dots with spaces
    - Title case
    - Collapse whitespace
    """
    import re
    name = raw.strip()

    # Strip file extensions
    for ext in ('.zip', '.7z', '.rar', '.json', '.bsdiff'):
        if name.lower().endswith(ext):
            name = name[:-len(ext)]

    # Strip NexusMods suffix: -{digits}-{version}-{10-digit timestamp}
    name = re.sub(r'-\d+-[\w.-]+-\d{10,}$', '', name)

    # Strip embedded version attached to word: 'Flightv2.5' -> 'Flight', 'Mod_v2' -> 'Mod_'
    name = re.sub(r'[_\s.-]*v\d+[\.\d]*(?=[\s_.\-]|$)', '', name, flags=re.IGNORECASE)
    # Strip '.v.N' patterns: 'Ragdoll.v.2' -> 'Ragdoll'
    name = re.sub(r'\.v\.\d+', '', name)

    # Strip trailing version patterns: v1.2, v1.2.3, (1.03.00), _v2
    name = re.sub(r'[\s_.\-]*[(\[]?v?\d+\.\d+[\.\d]*[)\]]?\s*$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'[\s_.\-]*\(\d+[\.\d]*\)\s*$', '', name)
    # Strip trailing 'vN.N.N' or 'vN': 'Wing v4' -> 'Wing', 'Rush v0.2.2' -> 'Rush'
    name = re.sub(r'\s+v\d+[\.\d]*\s*$', '', name, flags=re.IGNORECASE)
    # Strip trailing pure numbers: 'MEGA STACKS 999999' -> 'MEGA STACKS'
    name = re.sub(r'\s+\d{3,}\s*$', '', name)
    # Strip trailing single/double digit glued to word: 'Wing4' -> 'Wing', 'ModV2' -> 'Mod'
    name = re.sub(r'(?<=[a-zA-Z])\d+\s*$', '', name)
    # Strip trailing multiplier suffixes: '20x', 'x10', '10x'
    name = re.sub(r'\s+\d+x\s*$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s+x\d+\s*$', '', name, flags=re.IGNORECASE)

    # Strip middle version-like segments: '1.03.00' in 'glider-stamina-1.03.00-100pct'
    name = re.sub(r'[\s_-]+\d+\.\d+\.\d+[\s_-]*', ' ', name)
    # Strip trailing version segment: 'Trimmer 1.03.00' -> 'Trimmer'
    name = re.sub(r'\s+\d+\.\d+[\.\d]*\s*$', '', name)

    # Split CamelCase: 'CDLootMultiplier' -> 'CD Loot Multiplier'
    name = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)
    name = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', name)

    # Replace separators with spaces
    name = name.replace('_', ' ').replace('-', ' ')
    # Replace dots with spaces only if not between digits
    name = re.sub(r'\.(?!\d)', ' ', name)
    name = re.sub(r'(?<!\d)\.', ' ', name)

    # Collapse whitespace
    name = re.sub(r'\s+', ' ', name).strip()

    # Title case everything. Only keep 2-3 letter acronyms uppercase (CD, UI, QTE, NPC, LOD)
    _ACRONYMS = {'cd', 'ui', 'qte', 'npc', 'lod', 'hud', 'dds', 'asi', 'dll', 'fps', 'fov', 'dps'}
    words = []
    for word in name.split():
        wl = word.lower()
        if wl in _ACRONYMS:
            words.append(word.upper())
        elif wl in ('x2', 'x3', 'x5', 'x10', 'x20', 'x50', 'x100'):
            words.append(wl)
        else:
            words.append(word.capitalize())

    return ' '.join(words)


class ModImportResult:
    """Result of importing a mod."""

    def __init__(self, name: str, mod_type: str = "paz") -> None:
        self.name = prettify_mod_name(name)
        self.mod_type = mod_type
        self.changed_files: list[dict] = []  # [{file_path, delta_path, byte_start, byte_end}]
        self.error: str | None = None
        self.health_issues: list = []  # list[HealthIssue] from mod_health_check
        self.mod_id: int | None = None
        self.asi_staged: list[str] = []  # ASI file paths staged for GUI-side install


def detect_format(path: Path) -> str:
    """Detect import format: 'zip', '7z', 'folder', 'script', 'json_patch', 'bsdiff', or 'unknown'."""
    if path.is_dir():
        return "folder"
    suffix = path.suffix.lower()
    if suffix == ".zip":
        return "zip"
    if suffix == ".7z":
        return "7z"
    if suffix in (".bat", ".py"):
        return "script"
    if suffix == ".json":
        if detect_json_patch(path) is not None:
            return "json_patch"
    if suffix in (".bsdiff", ".xdelta"):
        return "bsdiff"
    # Check if it's a zip without extension
    if path.is_file():
        try:
            with zipfile.ZipFile(path) as _:
                return "zip"
        except zipfile.BadZipFile:
            pass
    # RAR archives — extracted via 7-Zip
    if suffix == ".rar":
        return "rar"
    return "unknown"


import json
import re

from cdumm.engine.crimson_browser_handler import detect_crimson_browser, convert_to_paz_mod
from cdumm.engine.json_patch_handler import detect_json_patch, convert_json_patch_to_paz, import_json_fast
from cdumm.engine.texture_mod_handler import detect_texture_mod, convert_texture_mod

# Pattern for valid game file paths: NNNN/N.paz, NNNN/N.pamt, meta/0.papgt, meta/0.pathc
_GAME_FILE_RE = re.compile(r'^(\d{4}/\d+\.(?:paz|pamt)|meta/\d+\.(?:papgt|pathc))$')


def _verify_and_fix_pamt_crc(pamt_bytes: bytes, rel_path: str) -> bytes:
    """Verify PAMT CRC and fix it if wrong.

    PAMT header: first 4 bytes = hashlittle(data[12:], 0xC5EDE).
    If the stored hash doesn't match, recompute and return fixed bytes.
    """
    import struct
    from cdumm.archive.hashlittle import compute_pamt_hash
    stored_hash = struct.unpack_from("<I", pamt_bytes, 0)[0]
    actual_hash = compute_pamt_hash(pamt_bytes)
    if stored_hash != actual_hash:
        logger.info("Auto-fixed PAMT CRC for %s (stored=%08X, actual=%08X)",
                     rel_path, stored_hash, actual_hash)
        fixed = bytearray(pamt_bytes)
        struct.pack_into("<I", fixed, 0, actual_hash)
        return bytes(fixed)
    return pamt_bytes


def _read_modinfo(extracted_dir: Path) -> dict | None:
    """Read modinfo.json from extracted mod directory if present.

    Searches the root and one level deep (for nested zips).
    Returns dict with keys: name, version, author, description (all optional).
    """
    from cdumm.engine.json_repair import load_json_tolerant

    for candidate in [extracted_dir / "modinfo.json",
                      *extracted_dir.glob("*/modinfo.json")]:
        if candidate.exists():
            try:
                data = load_json_tolerant(candidate)
                if isinstance(data, dict):
                    logger.info("Found modinfo.json: %s", {k: data.get(k) for k in ("name", "version", "author")})
                    return data
            except Exception as e:
                logger.warning("Failed to parse modinfo.json: %s", e)
    return None


def _try_convert_crimson_browser(
    extracted_dir: Path, game_dir: Path, work_dir: Path
) -> Path | None:
    """If extracted_dir is a Crimson Browser mod, convert to standard PAZ format.

    Returns the converted directory (inside work_dir), or None if not CB format.
    """
    manifest = detect_crimson_browser(extracted_dir)
    if manifest is None:
        return None

    mod_id = manifest.get("id", "unknown")
    logger.info("Detected Crimson Browser mod: %s", mod_id)
    converted = convert_to_paz_mod(manifest, game_dir, work_dir)
    if converted:
        logger.info("CB mod converted to standard PAZ format in %s", converted)
    else:
        logger.error("CB mod conversion failed for %s", mod_id)
    return converted


def _register_xml_patches(
    extracted_dir: Path, mod_id: int, mod_name: str,
    db: Database, deltas_dir: Path,
) -> list[Path]:
    """Scan extracted mod content for XML XPath patch / merge files.

    For each detected patch / merge file:
      1. Copy it into ``deltas_dir`` with a unique name.
      2. Derive the target game-file path from the file's position within
         ``extracted_dir`` (JMM convention: strip ``.patch`` / ``.merge``
         suffix, keep the rest of the relative path).
      3. Insert a ``mod_deltas`` row with ``kind=xml_patch|xml_merge``,
         ``delta_path`` pointing to the copy, ``file_path`` = target.

    Returns the list of source paths we claimed, so the caller can ignore
    them during regular import (prevents the CB / loose-file passes from
    re-processing patch bodies as full-file replacements).
    """
    from cdumm.engine.xml_patch_handler import (
        detect_patch_file, derive_target_from_patch_path,
    )
    claimed: list[Path] = []
    for f in extracted_dir.rglob("*"):
        if not f.is_file():
            continue
        kind = detect_patch_file(f)
        if kind not in ("xml_patch", "xml_merge"):
            continue
        target = derive_target_from_patch_path(f, extracted_dir)
        if not target:
            logger.warning("xml_patch: could not derive target for %s", f)
            continue

        # Copy the patch file into deltas_dir. Unique filename: mod_id + hash
        # of the target path so two mods targeting the same file don't clash.
        import hashlib
        tag = hashlib.md5(target.encode("utf-8")).hexdigest()[:8]
        dest_name = f"{mod_id}_{kind}_{tag}_{f.name}"
        dest = deltas_dir / dest_name
        try:
            shutil.copy2(f, dest)
        except Exception as e:
            logger.error("xml_patch: copy failed (%s → %s): %s", f, dest, e)
            continue

        db.connection.execute(
            "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
            "byte_start, byte_end, is_new, kind) "
            "VALUES (?, ?, ?, 0, 0, 0, ?)",
            (mod_id, target, str(dest), kind),
        )
        claimed.append(f)
        logger.info("xml_patch: registered %s → target=%s (kind=%s)",
                    f.name, target, kind)
    if claimed:
        db.connection.commit()
    return claimed


def _detect_xml_replacements(extracted_dir: Path) -> list[dict]:
    """Detect OG_<name>__<suffix>.xml replacement files.

    Returns list of dicts with keys: source_path, target_name.
    The OG_ naming convention means: replace <name>.xml with this file's content.
    """
    results = []
    for f in extracted_dir.rglob("*.xml"):
        stem = f.stem  # e.g. "OG_inventory__mymod"
        if not stem.startswith("OG_"):
            continue
        # Split on __ (double underscore) to separate target name from suffix
        rest = stem[3:]  # remove "OG_" prefix
        parts = rest.rsplit("__", 1)
        if len(parts) < 2:
            logger.warning("OG_ XML file missing __suffix: %s", f.name)
            continue
        target_name = parts[0] + ".xml"
        results.append({"source_path": f, "target_name": target_name})
        logger.info("Detected OG_ XML replacement: %s -> %s", f.name, target_name)
    return results


def _try_paz_entry_import(
    mod_paz_path: Path, vanilla_paz_path: Path, rel_path: str,
    extracted_dir: Path, game_dir: Path, mod_id: int, db,
    deltas_dir: Path, result,
) -> bool:
    """Decompose a modified PAZ file into ENTR deltas per PAMT entry.

    Instead of storing a FULL_COPY or SPRS delta of the entire PAZ,
    this compares each PAMT entry's decompressed content against vanilla
    and stores only the changed entries as ENTR deltas. This way two mods
    modifying different entries in the same PAZ compose correctly.

    Returns True if successful, False to fall back to byte-level deltas.
    """
    from cdumm.archive.paz_parse import parse_pamt
    from cdumm.archive.paz_crypto import lz4_decompress, decrypt
    from cdumm.engine.delta_engine import save_entry_delta
    from cdumm.engine.json_patch_handler import _extract_from_paz
    import os
    import sys as _sys

    _entr_start = time.perf_counter()
    _entr_timings = {
        "parse_pamts": 0.0,
        "raw_bytes_skip": 0.0,    # seek+read+compare raw bytes (cheap path)
        "extract_compare": 0.0,   # _extract_from_paz (LZ4) + content compare
        "encryption_detect": 0.0, # speculative LZ4 decompress to detect encryption
        "save_delta": 0.0,        # save_entry_delta disk write
        "db_insert": 0.0,         # mod_deltas row inserts
        "other": 0.0,
    }
    _entr_counters = {
        "total_entries": 0,
        "skipped_identical": 0,
        "changed": 0,
        "raw_bytes_matched": 0,
    }

    dir_name = rel_path.split("/")[0]  # e.g. "0008"
    paz_index = int(rel_path.split("/")[1].split(".")[0])  # e.g. 0 from "0.paz"

    # Find PAMTs — mod's PAMT (if shipped) or vanilla PAMT
    vanilla_pamt = game_dir / "CDMods" / "vanilla" / dir_name / "0.pamt"
    if not vanilla_pamt.exists():
        vanilla_pamt = game_dir / dir_name / "0.pamt"
    if not vanilla_pamt.exists():
        logger.debug("No PAMT found for %s, skipping entry-level import", rel_path)
        return False

    mod_pamt = extracted_dir / dir_name / "0.pamt"
    if not mod_pamt.exists():
        # Mod doesn't ship a PAMT — use vanilla PAMT for both
        mod_pamt = vanilla_pamt

    # Reuse the vanilla PAMT LRU cache (shared with health_check). For a
    # batch where 6+ mods touch dir 0009, this saves ~3s per mod after
    # the first cache miss. Mod PAMTs are cached too — within one mod's
    # lifecycle health_check parses it first, then we hit the cache here.
    from cdumm.engine.mod_health_check import _load_vanilla_pamt

    _t = time.perf_counter()
    try:
        _van_paz_dir = str(
            (game_dir / "CDMods" / "vanilla" / dir_name)
            if (game_dir / "CDMods" / "vanilla" / dir_name / "0.pamt").exists()
            else (game_dir / dir_name)
        )
        van_entries = _load_vanilla_pamt(str(vanilla_pamt), _van_paz_dir)

        _mod_paz_dir = (
            str(extracted_dir / dir_name)
            if mod_pamt != vanilla_pamt
            else str(game_dir / dir_name)
        )
        mod_entries = _load_vanilla_pamt(str(mod_pamt), _mod_paz_dir)
    except Exception as e:
        logger.debug("Failed to parse PAMTs for %s: %s", rel_path, e)
        return False
    _entr_timings["parse_pamts"] = time.perf_counter() - _t

    # Filter to entries in this specific PAZ file
    van_by_path = {e.path: e for e in van_entries if e.paz_index == paz_index}
    mod_by_path = {e.path: e for e in mod_entries if e.paz_index == paz_index}

    if not van_by_path or not mod_by_path:
        logger.debug("No entries for PAZ index %d in %s", paz_index, rel_path)
        return False

    def _extract_entry(entry, paz_path):
        """Extract and decompress a single entry from a PAZ file.

        `_extract_from_paz` mutates `entry._encrypted_override` when it
        detects encryption. VanillaPamtEntry (the cache's NamedTuple) is
        immutable, so we convert to a mutable PazEntry dataclass first.
        Only pays the conversion cost for entries that actually need
        decompression (raw-bytes fast-path skips this entirely).
        """
        from cdumm.archive.paz_parse import PazEntry as _PazEntry
        if not isinstance(entry, _PazEntry):
            entry = _PazEntry(
                path=entry.path,
                paz_file=str(paz_path),
                offset=entry.offset,
                comp_size=entry.comp_size,
                orig_size=entry.orig_size,
                flags=entry.flags,
                paz_index=entry.paz_index,
            )
        return _extract_from_paz(entry, paz_path=str(paz_path))

    changed = 0
    paz_file_path = f"{dir_name}/{paz_index}.paz"

    # ── Fast-path: bulk raw-bytes filter via Rust ─────────────────
    # For entries with identical offset+comp_size in mod and vanilla,
    # read the raw bytes from both PAZs and compare. Native code keeps
    # this near disk-I/O speed even for 100K+ entries. Entries whose
    # offset or size differ in mod vs vanilla are marked "changed"
    # unconditionally (the raw-bytes compare can't apply).
    _raw_candidates: list[tuple[str, object, object]] = []
    _raw_changed: list[tuple[str, object, object]] = []

    for entry_path, mod_entry in mod_by_path.items():
        van_entry = van_by_path.get(entry_path)
        if van_entry is None:
            continue  # New entry — handled separately
        _entr_counters["total_entries"] += 1
        if (mod_entry.offset == van_entry.offset
                and mod_entry.comp_size == van_entry.comp_size):
            _raw_candidates.append((entry_path, mod_entry, van_entry))
        else:
            _raw_changed.append((entry_path, mod_entry, van_entry))

    if _raw_candidates:
        try:
            from cdumm_native import filter_changed_entries as _native_filter
        except ImportError:
            _native_filter = None

        _t = time.perf_counter()
        if _native_filter is not None:
            pairs = [
                (e[1].paz_index, e[1].offset, e[1].comp_size,
                 e[2].offset, e[2].comp_size)
                for e in _raw_candidates
            ]
            changed_idxs = _native_filter(
                str(mod_paz_path), str(vanilla_paz_path), pairs)
            changed_set = set(changed_idxs)
            for i, tup in enumerate(_raw_candidates):
                if i in changed_set:
                    _raw_changed.append(tup)
                else:
                    _entr_counters["raw_bytes_matched"] += 1
                    _entr_counters["skipped_identical"] += 1
        else:
            # Python fallback — original per-entry loop.
            for entry_path, mod_entry, van_entry in _raw_candidates:
                try:
                    with open(mod_paz_path, "rb") as f:
                        f.seek(mod_entry.offset)
                        mod_raw = f.read(mod_entry.comp_size)
                    with open(vanilla_paz_path, "rb") as f:
                        f.seek(van_entry.offset)
                        van_raw = f.read(van_entry.comp_size)
                    if mod_raw == van_raw:
                        _entr_counters["raw_bytes_matched"] += 1
                        _entr_counters["skipped_identical"] += 1
                    else:
                        _raw_changed.append((entry_path, mod_entry, van_entry))
                except Exception as e:
                    logger.warning("Raw-bytes compare failed for %s: %s",
                                   entry_path, e)
        _entr_timings["raw_bytes_skip"] += time.perf_counter() - _t

    # Now iterate only the entries that genuinely differ
    for entry_path, mod_entry, van_entry in _raw_changed:
        try:
            # Entry differs — decompress both and store mod's content
            _t = time.perf_counter()
            van_content = _extract_entry(van_entry, vanilla_paz_path)
            mod_content = _extract_entry(mod_entry, mod_paz_path)
            _same_decomp = van_content == mod_content
            _entr_timings["extract_compare"] += time.perf_counter() - _t

            if _same_decomp:
                _entr_counters["skipped_identical"] += 1
                continue  # Decompressed content is the same

            # Detect encryption: try decompressing the vanilla entry.
            # If decompress fails, the entry is encrypted.
            _t = time.perf_counter()
            encrypted = van_entry.encrypted
            if not encrypted and van_entry.compressed and van_entry.compression_type == 2:
                try:
                    with open(vanilla_paz_path, "rb") as f:
                        f.seek(van_entry.offset)
                        raw = f.read(van_entry.comp_size)
                    lz4_decompress(raw, van_entry.orig_size)
                except Exception:
                    encrypted = True
            _entr_timings["encryption_detect"] += time.perf_counter() - _t

            metadata = {
                "pamt_dir": dir_name,
                "entry_path": van_entry.path,
                "paz_index": van_entry.paz_index,
                "compression_type": van_entry.compression_type,
                "flags": van_entry.flags,
                "vanilla_offset": van_entry.offset,
                "vanilla_comp_size": van_entry.comp_size,
                "vanilla_orig_size": van_entry.orig_size,
                "encrypted": encrypted,
            }

            _t = time.perf_counter()
            safe_name = van_entry.path.replace("/", "_") + ".entr"
            delta_path = deltas_dir / str(mod_id) / safe_name
            save_entry_delta(mod_content, metadata, delta_path)
            _entr_timings["save_delta"] += time.perf_counter() - _t

            _t = time.perf_counter()
            db.connection.execute(
                "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
                "byte_start, byte_end, entry_path) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (mod_id, paz_file_path, str(delta_path),
                 van_entry.offset, van_entry.offset + van_entry.comp_size,
                 van_entry.path))
            _entr_timings["db_insert"] += time.perf_counter() - _t

            result.changed_files.append({
                "file_path": paz_file_path,
                "entry_path": van_entry.path,
                "delta_path": str(delta_path),
            })
            changed += 1
            _entr_counters["changed"] += 1

        except Exception as e:
            logger.warning("Entry comparison failed for %s: %s", entry_path, e)
            continue

    if changed == 0:
        logger.warning("No changed entries in %s (%d mod entries vs %d vanilla entries). "
                        "Mod content may match current game version or decomposition failed.",
                        rel_path, len(mod_by_path), len(van_by_path))
        return False

    _t = time.perf_counter()
    db.connection.commit()
    _entr_timings["db_insert"] += time.perf_counter() - _t

    _entr_total = time.perf_counter() - _entr_start
    _accounted = sum(_entr_timings.values())
    _entr_timings["other"] = max(0.0, _entr_total - _accounted)

    if _entr_total >= 1.0:
        _hot = sorted(_entr_timings.items(), key=lambda kv: kv[1], reverse=True)
        _parts = " ".join(
            f"{n}={int(dt * 1000)}ms" for n, dt in _hot if dt >= 0.05
        )
        print(
            f"[ENTR-TIMING] {rel_path}: total={int(_entr_total * 1000)}ms "
            f"entries={_entr_counters['total_entries']} "
            f"changed={_entr_counters['changed']} "
            f"raw_same={_entr_counters['raw_bytes_matched']} "
            f"{_parts}",
            file=_sys.stderr,
        )

    logger.info("Entry-level PAZ import: %s — %d/%d entries changed",
                rel_path, changed, len(mod_by_path))
    return True


def _find_loose_file_candidates(path: Path, max_depth: int = 5) -> list[dict]:
    """Recursively search for all loose-file mod roots (files/NNNN/ pattern).

    Returns a list of manifest dicts, one per found variant.
    """
    results: list[dict] = []
    seen_bases: set[str] = set()

    def _check_candidate(candidate: Path) -> dict | None:
        base_key = str(candidate)
        if base_key in seen_bases:
            return None
        # Pattern 1: mod.json + files/
        mod_json = candidate / "mod.json"
        files_dir = candidate / "files"
        if mod_json.exists() and files_dir.exists():
            try:
                with open(mod_json, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and "modinfo" in data:
                    modinfo = data["modinfo"]
                    seen_bases.add(base_key)
                    return {
                        "format": "loose_file_mod",
                        "id": modinfo.get("title", candidate.name),
                        "files_dir": "files",
                        "_manifest_path": mod_json,
                        "_base_dir": candidate,
                        "_modinfo": modinfo,
                    }
            except Exception:
                pass
        # Pattern 2: mod.json + game files at root (no files/ directory)
        # Game file paths like gamedata/, sequencer/, ui/ sit next to mod.json.
        # These get resolved to PAZ directories via PAMT lookup.
        # Skip if the directory contains numbered game dirs with PAZ/PAMT files
        # (that's a standalone PAZ mod, not a loose-file mod).
        if mod_json.exists() and not files_dir.exists():
            try:
                with open(mod_json, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and "modinfo" in data:
                    # Check if this is a standalone PAZ mod (has NNNN/0.paz)
                    is_standalone_paz = any(
                        d.is_dir() and d.name.isdigit() and len(d.name) == 4
                        and (d / "0.paz").exists()
                        for d in candidate.iterdir()
                    )
                    if is_standalone_paz:
                        pass  # Let _match_game_files handle it as a PAZ mod
                    else:
                        # Check if there are actual game files alongside mod.json
                        has_game_files = any(
                            f.is_file() and f.name != "mod.json"
                            for f in candidate.iterdir()
                        ) or any(
                            d.is_dir() and d.name != "files"
                            for d in candidate.iterdir()
                        )
                    if not is_standalone_paz and has_game_files:
                        modinfo = data["modinfo"]
                        seen_bases.add(base_key)
                        # Use "." as files_dir so convert_to_paz_mod scans
                        # the candidate root for game files
                        return {
                            "format": "loose_file_mod",
                            "id": modinfo.get("title", candidate.name),
                            "files_dir": ".",
                            "_manifest_path": mod_json,
                            "_base_dir": candidate,
                            "_modinfo": modinfo,
                        }
            except Exception:
                pass
        # Pattern 3: bare files/NNNN/
        if files_dir.exists() and files_dir.is_dir():
            try:
                has_numbered = any(
                    d.is_dir() and d.name.isdigit() and len(d.name) == 4
                    for d in files_dir.iterdir()
                )
            except OSError:
                has_numbered = False
            if has_numbered:
                seen_bases.add(base_key)
                return {
                    "format": "loose_file_mod",
                    "id": candidate.name,
                    "files_dir": "files",
                    "_manifest_path": None,
                    "_base_dir": candidate,
                    "_modinfo": {"title": candidate.name},
                }
        # Pattern 4: bare NNNN/ directly in the candidate (no files/ wrapper)
        # Mods that ship e.g. 0010/actionchart/xml/file.xml at the root.
        # Skip if the numbered directory contains 0.paz (that's a standalone
        # PAZ mod, handled by _match_game_files).
        try:
            has_direct_numbered = any(
                d.is_dir() and d.name.isdigit() and len(d.name) == 4
                and not (d / "0.paz").exists()
                and any(f.is_file() for f in d.rglob("*"))
                for d in candidate.iterdir()
            )
        except OSError:
            has_direct_numbered = False
        if has_direct_numbered:
            seen_bases.add(base_key)
            return {
                "format": "loose_file_mod",
                "id": candidate.name,
                "files_dir": ".",
                "_manifest_path": None,
                "_base_dir": candidate,
                "_modinfo": {"title": candidate.name},
            }
        return None

    def _walk(directory: Path, depth: int) -> None:
        if depth > max_depth:
            return
        hit = _check_candidate(directory)
        if hit:
            results.append(hit)
            return  # don't recurse into a found mod root
        try:
            children = [d for d in directory.iterdir() if d.is_dir()
                        and not d.name.startswith((".", "_"))]
        except OSError:
            return
        for child in children:
            _walk(child, depth + 1)

    _walk(path, 0)
    return results


def find_loose_file_variants(path: Path) -> list[dict]:
    """Public API: find all loose-file mod variants in a directory tree.

    Used by the GUI to detect multi-variant mods and show a picker.
    """
    return _find_loose_file_candidates(path, max_depth=5)


def detect_loose_file_mod(path: Path) -> dict | None:
    """Detect mods that ship loose game files with a mod.json metadata file.

    Format: mod.json (with "modinfo" key) + files/ directory containing
    replacement files at their PAMT paths (e.g., files/0004/sound/.../file.wem).

    Returns a CB-compatible manifest dict for convert_to_paz_mod, or None.
    """
    candidates = _find_loose_file_candidates(path, max_depth=5)
    if len(candidates) == 1:
        logger.info("Detected loose file mod: %s", candidates[0]["id"])
        return candidates[0]
    if len(candidates) > 1:
        # Multiple variants found — caller should use find_loose_file_variants()
        # and show a picker. Return None so the import doesn't silently pick one.
        logger.info("Found %d loose file variants, picker needed", len(candidates))
        return None
    return None


def _match_game_files(
    extracted_dir: Path, game_dir: Path, snapshot: SnapshotManager
) -> list[tuple[str, Path, bool]]:
    """Find files in extracted_dir that match known game file paths.

    Returns list of (relative_posix_path, absolute_extracted_path, is_new).
    is_new=True means the file doesn't exist in vanilla (mod adds it).

    Detects standalone PAZ mods that ship their own directory (e.g., 0036/)
    with completely different content from vanilla. These get assigned a new
    directory number instead of being treated as modifications to vanilla.
    """
    matches: list[tuple[str, Path, bool]] = []

    # First: detect if this is a standalone directory mod
    # (ships 0.paz + 0.pamt in a numbered dir but content is unrelated to vanilla)
    standalone_remap = _detect_standalone_mod(extracted_dir, game_dir, snapshot)

    for f in extracted_dir.rglob("*"):
        if not f.is_file():
            continue

        parts = f.relative_to(extracted_dir).parts
        matched = False

        # Build candidate paths
        for i in range(len(parts)):
            candidate = "/".join(parts[i:])

            # Skip meta/0.papgt from standalone mods (CDUMM rebuilds it)
            if candidate == "meta/0.papgt" and standalone_remap:
                matched = True
                break

            # Remap standalone mod directories to their assigned number
            if standalone_remap:
                for old_dir, new_dir in standalone_remap.items():
                    if candidate.startswith(old_dir + "/"):
                        candidate = new_dir + candidate[len(old_dir):]
                        matches.append((candidate, f, True))
                        matched = True
                        break
                if matched:
                    break

            # Try exact match against snapshot (existing vanilla files)
            if snapshot.get_file_hash(candidate) is not None:
                matches.append((candidate, f, False))
                matched = True
                break

            # Check if it looks like a game file by pattern
            if _GAME_FILE_RE.match(candidate):
                game_file = game_dir / candidate.replace("/", "\\")
                is_new = not game_file.exists()
                matches.append((candidate, f, is_new))
                matched = True
                break

        if matched:
            continue

    # If no matches found, check for unnumbered PAZ/PAMT mods
    # (e.g., mod ships "modname/0.paz" + "modname/0.pamt" without a numbered dir)
    if not matches:
        paz_files = list(extracted_dir.rglob("*.paz"))
        pamt_files = list(extracted_dir.rglob("*.pamt"))
        papgt_files = list(extracted_dir.rglob("0.papgt"))

        if paz_files and pamt_files:
            # Group by parent directory — each paz+pamt pair gets its own dir
            dirs_with_mods: dict[Path, tuple[list, list]] = {}
            for pf in paz_files:
                dirs_with_mods.setdefault(pf.parent, ([], []))[0].append(pf)
            for pf in pamt_files:
                dirs_with_mods.setdefault(pf.parent, ([], []))[1].append(pf)

            for mod_dir, (pazs, pamts) in dirs_with_mods.items():
                if pazs and pamts:
                    next_dir = _next_paz_directory(game_dir)
                    logger.info("Unnumbered PAZ mod in %s -> assigning %s",
                                mod_dir.name, next_dir)
                    for f in pazs + pamts:
                        matches.append((f"{next_dir}/{f.name}", f, True))

        elif paz_files and not pamt_files:
            # Some mods only ship PAZ without PAMT — still try to import
            next_dir = _next_paz_directory(game_dir)
            logger.info("PAZ-only mod detected (no PAMT), assigning directory %s", next_dir)
            for f in paz_files:
                matches.append((f"{next_dir}/{f.name}", f, True))

    return matches


def _detect_standalone_mod(
    extracted_dir: Path, game_dir: Path, snapshot: SnapshotManager
) -> dict[str, str] | None:
    """Detect if a mod ships standalone PAZ/PAMT in a numbered directory.

    A standalone mod has its own 0.paz + 0.pamt that are completely different
    from vanilla (different PAMT or PAZ size). These should get their own
    directory number instead of being treated as modifications.

    Returns {old_dir_prefix: new_dir} remap dict, or None if not standalone.
    The old_dir_prefix is relative to extracted_dir (e.g., "FatStacks10x/0036").
    """
    remap: dict[str, str] = {}

    # Search recursively for numbered directories containing 0.paz + 0.pamt
    for d in extracted_dir.rglob("*"):
        if not d.is_dir() or not d.name.isdigit() or len(d.name) != 4:
            continue
        dir_name = d.name
        mod_pamt = d / "0.pamt"
        mod_paz = d / "0.paz"
        if not mod_pamt.exists() or not mod_paz.exists():
            continue

        # Compare mod's files against vanilla (check both game dir and vanilla backup)
        vanilla_backup_dir = game_dir / "CDMods" / "vanilla"
        game_pamt = game_dir / dir_name / "0.pamt"
        game_paz = game_dir / dir_name / "0.paz"
        backup_pamt = vanilla_backup_dir / dir_name / "0.pamt"
        backup_paz = vanilla_backup_dir / dir_name / "0.paz"

        if not game_pamt.exists():
            continue  # no vanilla dir = truly new, handled elsewhere

        mod_pamt_size = mod_pamt.stat().st_size
        mod_paz_size = mod_paz.stat().st_size

        # Check against vanilla backup first (accurate), then game dir (may be modded)
        vanilla_pamt_size = backup_pamt.stat().st_size if backup_pamt.exists() else game_pamt.stat().st_size
        vanilla_paz_size = backup_paz.stat().st_size if backup_paz.exists() else (game_paz.stat().st_size if game_paz.exists() else 0)

        # A standalone mod has completely different content — different PAMT size
        # indicates entirely different file entries (truly a new directory).
        # Same PAMT size means same file entries, just modified content —
        # this is a regular patch even if PAZ size changed (file appending).
        if mod_pamt_size == vanilla_pamt_size:
            is_standalone = False
            logger.info("Modified vanilla: %s (same PAMT size, treating as patch, "
                         "PAZ %d vs %d)", dir_name, mod_paz_size, vanilla_paz_size)
        elif int(dir_name) < 36:
            # Vanilla directories (0000-0035): always treat as patch, even if
            # PAMT sizes differ (game update may have added/removed entries).
            # ENTR decomposition handles different PAMTs by matching entries
            # by path, so it works regardless of PAMT size differences.
            is_standalone = False
            logger.info("Modified vanilla: %s (PAMT size differs %d vs %d, but "
                         "vanilla dir — treating as patch for ENTR decomposition)",
                         dir_name, mod_pamt_size, vanilla_pamt_size)
        else:
            is_standalone = True
            logger.info("Standalone: %s has different PAMT (mod=%d vs vanilla=%d, "
                         "PAZ %d vs %d)",
                         dir_name, mod_pamt_size, vanilla_pamt_size,
                         mod_paz_size, vanilla_paz_size)

        if is_standalone:
            # Each standalone mod gets its own directory number so multiple
            # mods targeting the same directory can coexist.
            rel_parts = d.relative_to(extracted_dir).parts
            old_prefix = "/".join(rel_parts)
            new_dir = _next_paz_directory(game_dir)
            remap[old_prefix] = new_dir
            logger.info("Standalone mod: remapping %s -> %s", old_prefix, new_dir)

    return remap if remap else None


_assigned_dirs: set[int] = set()  # track dirs assigned in current import batch


def clear_assigned_dirs() -> None:
    """Clear the assigned directory tracker. Call after Apply completes."""
    _assigned_dirs.clear()


def _next_paz_directory(game_dir: Path) -> str:
    """Find the next available PAZ directory number (0036+)."""
    existing = set()
    for d in game_dir.iterdir():
        if d.is_dir() and d.name.isdigit() and len(d.name) == 4:
            existing.add(int(d.name))
    existing |= _assigned_dirs
    # Start from 36 (base game uses 0000-0035)
    for n in range(36, 9999):
        if n not in existing:
            _assigned_dirs.add(n)
            return f"{n:04d}"
    raise RuntimeError("No available PAZ directory numbers (36-9999 all used)")


def import_from_7z(
    archive_path: Path, game_dir: Path, db: Database, snapshot: SnapshotManager, deltas_dir: Path,
    existing_mod_id: int | None = None,
) -> ModImportResult:
    """Import a mod from a 7z archive by extracting and treating as folder."""
    mod_name = archive_path.stem
    result = ModImportResult(mod_name)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        try:
            import py7zr
            with py7zr.SevenZipFile(archive_path, 'r') as z:
                z.extractall(tmp_path)
        except Exception as e:
            result.error = f"Failed to extract 7z: {e}"
            return result

        # Delegate to import_from_zip's internal logic (same flow)
        return _import_from_extracted(tmp_path, game_dir, db, snapshot, deltas_dir,
                                      mod_name, existing_mod_id)


def _find_7z() -> str | None:
    """Locate the 7-Zip executable on Windows."""
    for candidate in [
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ]:
        if Path(candidate).exists():
            return candidate
    return shutil.which("7z")


def import_from_rar(
    archive_path: Path, game_dir: Path, db: Database, snapshot: SnapshotManager, deltas_dir: Path,
    existing_mod_id: int | None = None,
) -> ModImportResult:
    """Import a mod from a RAR archive by extracting via 7-Zip."""
    mod_name = archive_path.stem
    result = ModImportResult(mod_name)

    seven_z = _find_7z()
    if not seven_z:
        result.error = (
            "RAR import requires 7-Zip.\n"
            "Install from https://7-zip.org or extract manually and drop the folder."
        )
        return result

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        try:
            _no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.run(
                [seven_z, "x", str(archive_path), f"-o{tmp_path}", "-y"],
                capture_output=True, timeout=120,
                creationflags=_no_window,
            )
            if proc.returncode != 0:
                stderr_msg = proc.stderr.decode(errors="replace").strip()
                result.error = (
                    f"7-Zip extraction failed (code {proc.returncode})"
                    + (f"\n{stderr_msg}" if stderr_msg else "")
                )
                return result
        except Exception as e:
            result.error = f"Failed to extract RAR: {e}"
            return result

        return _import_from_extracted(tmp_path, game_dir, db, snapshot, deltas_dir,
                                      mod_name, existing_mod_id)


_LOOSE_GAME_EXTENSIONS = {".json", ".xml", ".css", ".html", ".thtml",
                          ".dds", ".ttf", ".otf", ".wem", ".bnk", ".mp4"}
_SKIP_LOOSE_FILES = {"mod.json", "manifest.json", "modinfo.json"}


def _import_remaining_loose_files(
    extracted_dir: Path, game_dir: Path, db: Database,
    snapshot: SnapshotManager, deltas_dir: Path,
    result: ModImportResult,
) -> None:
    """Import loose game files that weren't matched by _match_game_files.

    For mixed-format mods (standalone PAZ + loose .json/.xml files), the
    main import handles the PAZ but drops the loose files. This function
    collects those files and routes them through the CB handler which can
    resolve filenames to PAZ directories via PAMT lookup, then imports the
    converted output into the same mod entry.
    """
    from cdumm.engine.crimson_browser_handler import convert_to_paz_mod

    # Collect loose files not inside numbered directories
    loose_files: list[Path] = []
    for f in extracted_dir.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(extracted_dir)
        parts = rel.parts
        # Skip files inside numbered directories (already handled as PAZ)
        if parts and parts[0].isdigit() and len(parts[0]) == 4:
            continue
        # Skip meta/ directory (PAPGT handled by CDUMM)
        if parts and parts[0].lower() == "meta":
            continue
        # Skip known non-game files
        if f.name.lower() in _SKIP_LOOSE_FILES:
            continue
        # Only include known game data extensions
        if f.suffix.lower() in _LOOSE_GAME_EXTENSIONS:
            loose_files.append(f)

    if not loose_files:
        return

    logger.info("Mixed-format mod: found %d loose files after PAZ import: %s",
                len(loose_files), [f.name for f in loose_files])

    with tempfile.TemporaryDirectory() as tmp:
        work_dir = Path(tmp)
        files_dir = work_dir / "_loose_files"
        files_dir.mkdir(parents=True, exist_ok=True)
        for f in loose_files:
            rel = f.relative_to(extracted_dir)
            dst = files_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)

        # Create synthetic CB manifest for the loose files
        manifest = {
            "id": result.name,
            "files_dir": ".",
            "_base_dir": files_dir,
        }

        cb_output = work_dir / "_cb_output"
        converted = convert_to_paz_mod(manifest, game_dir, cb_output)
        if converted is None:
            logger.warning("CB handler could not resolve loose files for %s", result.name)
            return

        # Import the converted files into the same mod entry
        loose_result = _process_extracted_files(
            converted, game_dir, db, snapshot, deltas_dir, result.name,
            existing_mod_id=result.mod_id)

        if loose_result.changed_files:
            result.changed_files.extend(loose_result.changed_files)
            logger.info("Mixed-format mod: imported %d additional files from loose pass",
                        len(loose_result.changed_files))
        else:
            logger.warning("Mixed-format mod: loose pass matched no files for %s", result.name)


def _import_from_extracted(
    tmp_path: Path, game_dir: Path, db: Database, snapshot: SnapshotManager, deltas_dir: Path,
    mod_name: str, existing_mod_id: int | None = None,
) -> ModImportResult:
    """Common import logic for extracted archives (zip/7z)."""
    result = ModImportResult(mod_name)

    # Check for Crimson Browser format and convert if needed
    cb_manifest = detect_crimson_browser(tmp_path)
    if cb_manifest is not None:
        cb_work = tmp_path.parent / "_cb_converted"
        converted = convert_to_paz_mod(cb_manifest, game_dir, cb_work)
        if converted is not None:
            cb_name = cb_manifest.get("id", mod_name)
            modinfo = _read_modinfo(tmp_path)
            if modinfo and modinfo.get("name"):
                cb_name = modinfo["name"]
            cb_result = _process_extracted_files(
                converted, game_dir, db, snapshot, deltas_dir, cb_name,
                existing_mod_id=existing_mod_id, modinfo=modinfo)
            # Compound archive support — see import_from_folder for context.
            cb_base_raw = cb_manifest.get("_base_dir")
            cb_base = Path(cb_base_raw) if cb_base_raw is not None else None
            if (cb_base and cb_base != tmp_path
                    and cb_result and not cb_result.error
                    and cb_result.changed_files):
                try:
                    _import_sibling_json_patches(
                        tmp_path, cb_base, game_dir, db, deltas_dir)
                except Exception as e:
                    logger.debug("Sibling JSON scan after CB failed: %s", e)
            return cb_result

    # Check for loose file mod (mod.json + files/ directory)
    lfm = detect_loose_file_mod(tmp_path)
    if lfm is not None:
        lfm_work = tmp_path.parent / "_lfm_converted"
        converted = convert_to_paz_mod(lfm, game_dir, lfm_work)
        if converted is not None:
            mi = lfm.get("_modinfo", {})
            lfm_name = mi.get("title", mod_name)
            lfm_modinfo = {
                "name": mi.get("title"), "version": mi.get("version"),
                "author": mi.get("author"), "description": mi.get("description"),
                "force_inplace": mi.get("force_inplace"),
            }
            return _process_extracted_files(
                converted, game_dir, db, snapshot, deltas_dir, lfm_name,
                existing_mod_id=existing_mod_id, modinfo=lfm_modinfo)

    # Check for JSON byte-patch format — use ENTR deltas for proper composition
    jp_data = detect_json_patch(tmp_path)
    if jp_data is not None:
        from cdumm.engine.json_patch_handler import import_json_as_entr
        jp_name = _json_mod_display_name(jp_data, mod_name)
        jp_modinfo = _json_mod_modinfo(jp_data)
        entr_result = import_json_as_entr(
            jp_data, game_dir, db, deltas_dir, jp_name,
            existing_mod_id=existing_mod_id, modinfo=jp_modinfo)
        if entr_result is not None:
            if entr_result.get("version_mismatch"):
                result = ModImportResult(jp_name)
                game_ver = entr_result.get("game_version", "unknown")
                mismatched = entr_result.get("mismatched", 0)
                result.error = (
                    f"This mod is incompatible with the current game version. "
                    f"{mismatched} byte patches don't match — the game data has "
                    f"changed since this mod was created (mod targets version "
                    f"{game_ver}). The mod author needs to update it.")
                return result
            if not entr_result["changed_files"]:
                result = ModImportResult(jp_name)
                result.error = (
                    "This mod's changes are already present in your game files. "
                    "Nothing to apply.")
                return result
            result = ModImportResult(jp_name)
            result.changed_files = entr_result["changed_files"]
            if jp_data.get("patches"):
                _store_json_patches(db, result, jp_data, game_dir)
            return result
        # Fall through if ENTR import failed

    # Check for DDS texture mod
    tex_info = detect_texture_mod(tmp_path)
    if tex_info is not None:
        tex_work = tmp_path.parent / "_tex_converted"
        converted = convert_texture_mod(tex_info, game_dir, tex_work)
        if converted is not None:
            tex_name = tex_info.get("name", mod_name)
            modinfo = _read_modinfo(tmp_path)
            if modinfo and modinfo.get("name"):
                tex_name = modinfo["name"]
            return _process_extracted_files(
                converted, game_dir, db, snapshot, deltas_dir, tex_name,
                existing_mod_id=existing_mod_id, modinfo=modinfo)

    # Check for scripts: when the archive contains ONLY a .bat or .py
    # and no PAZ/PAMT files, treat it as a script mod (e.g. Glider
    # Stamina, which ships as glider-stamina.bat inside a zip). Copy
    # the script out of tmp_path to a standalone file and run it
    # through import_from_script, so users don't have to extract zips
    # manually.
    scripts = list(tmp_path.glob("*.bat")) + list(tmp_path.glob("*.py"))
    if scripts and not _match_game_files(tmp_path, game_dir, snapshot):
        if len(scripts) == 1:
            # Single-script archive → route to the script-mod flow.
            logger.info(
                "Archive contains a single script mod (%s) — routing to "
                "import_from_script", scripts[0].name)
            return import_from_script(
                scripts[0], game_dir, db, snapshot, deltas_dir)
        # Multiple scripts are ambiguous; keep the explicit error so
        # the user can pick one and drop it directly.
        names = ", ".join(s.name for s in scripts)
        result.error = (
            f"Archive contains {len(scripts)} scripts ({names}). "
            "Extract and drop the one you want to run as a script mod."
        )
        return result

    # Detect multi-variant
    variant = _find_best_variant(tmp_path)
    if variant:
        logger.info("Multi-variant archive, using: %s", variant.name)
        tmp_path = variant
        mod_name = f"{mod_name} ({variant.name})"

    modinfo = _read_modinfo(tmp_path)
    if modinfo and modinfo.get("name"):
        mod_name = modinfo["name"]

    result = _process_extracted_files(
        tmp_path, game_dir, db, snapshot, deltas_dir, mod_name,
        existing_mod_id=existing_mod_id, modinfo=modinfo)

    # Second pass: import loose game files not matched by _match_game_files.
    # Only runs for mixed-format mods (standalone PAZ dirs + loose files).
    if result.changed_files and not result.error and result.mod_id is not None:
        has_standalone_paz = any(
            d.is_dir() and d.name.isdigit() and len(d.name) == 4
            and (d / "0.paz").exists()
            for d in tmp_path.iterdir() if d.is_dir()
        )
        if has_standalone_paz:
            _import_remaining_loose_files(
                tmp_path, game_dir, db, snapshot, deltas_dir, result)

    # Third pass: register XML XPath patch / merge files (JMM parity).
    # Runs regardless of whether the main pass found other content — some
    # mods are purely XML patches and have no other game files.
    if result.mod_id is None and _scan_xml_patches(tmp_path):
        # No prior pass created a mod row; create one now for this
        # XML-patches-only mod so the deltas have somewhere to anchor.
        author = (modinfo or {}).get("author")
        version = (modinfo or {}).get("version")
        description = (modinfo or {}).get("description")
        # Reuse 'paz' as the umbrella mod_type; the presence of deltas with
        # kind='xml_patch'/'xml_merge' is what tags this mod as XML-patch
        # style. Keeps the existing schema CHECK constraint happy.
        cur = db.connection.execute(
            "INSERT INTO mods (name, mod_type, priority, author, version, "
            "description) VALUES (?, 'paz', 0, ?, ?, ?)",
            (mod_name, author, version, description),
        )
        db.connection.commit()
        result.mod_id = cur.lastrowid
        result.error = None
        logger.info("xml_patch-only mod created: id=%d name=%s",
                    result.mod_id, mod_name)
    if result.mod_id is not None:
        claimed = _register_xml_patches(
            tmp_path, result.mod_id, mod_name, db, deltas_dir)
        if claimed:
            for p in claimed:
                rel = str(p.relative_to(tmp_path)).replace("\\", "/")
                if rel not in result.changed_files:
                    result.changed_files.append(rel)

    return result


def _scan_xml_patches(root: Path) -> list[Path]:
    """Lightweight detector — returns paths to XML patch / merge files
    under ``root`` without touching the DB. Used to decide whether an
    otherwise-empty archive actually contains patch content."""
    from cdumm.engine.xml_patch_handler import detect_patch_file
    hits: list[Path] = []
    for f in root.rglob("*"):
        if f.is_file() and detect_patch_file(f):
            hits.append(f)
    return hits


def import_from_zip(
    zip_path: Path, game_dir: Path, db: Database, snapshot: SnapshotManager, deltas_dir: Path,
    existing_mod_id: int | None = None,
) -> ModImportResult:
    """Import a mod from a zip archive."""
    mod_name = zip_path.stem
    result = ModImportResult(mod_name)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(tmp_path)
        except zipfile.BadZipFile as e:
            result.error = f"Invalid zip file: {e}"
            return result
        except Exception as e:
            result.error = f"Failed to extract zip: {e}"
            logger.error("Zip extraction failed: %s", e, exc_info=True)
            return result

        # Stage ASI files separately for GUI-side install (mixed ZIP support)
        asi_staging = tmp_path / "_asi_staging"
        for f in list(tmp_path.rglob("*")):
            if not f.is_file():
                continue
            if f.suffix.lower() in (".asi", ".ini"):
                # Skip files inside staging dir itself
                if "_asi_staging" in f.parts:
                    continue
                asi_staging.mkdir(exist_ok=True)
                import shutil
                shutil.move(str(f), str(asi_staging / f.name))
                result.asi_staged.append(str(asi_staging / f.name))
                logger.info("Staged ASI file for GUI install: %s", f.name)

        # Check for OG_ XML replacement files and convert to standard deltas
        og_xml = _detect_xml_replacements(tmp_path)
        if og_xml:
            from cdumm.engine.json_patch_handler import _find_pamt_entry
            from cdumm.engine.crimson_browser_handler import fix_xml_format
            og_work = Path(tmp) / "_og_xml_converted"
            og_work.mkdir(exist_ok=True)
            for og in og_xml:
                entry = _find_pamt_entry(og["target_name"], game_dir)
                if entry is None:
                    logger.warning("OG_ XML target not found in game: %s", og["target_name"])
                    continue
                xml_bytes = og["source_path"].read_bytes()
                # Apply BOM/CRLF fixup for game compatibility
                try:
                    xml_bytes = fix_xml_format(xml_bytes)
                except Exception:
                    pass  # use raw bytes if fixup fails
                # Save as full-file ENTR delta
                safe_name = og["target_name"].replace("/", "_") + ".entr"
                delta_path = deltas_dir / safe_name
                delta_path.parent.mkdir(parents=True, exist_ok=True)
                delta_path.write_bytes(xml_bytes)
                logger.info("OG_ XML replacement delta created: %s -> %s", og["source_path"].name, og["target_name"])

        # Check for Crimson Browser format and convert if needed
        cb_manifest = detect_crimson_browser(tmp_path)
        if cb_manifest is not None:
            cb_work = Path(tmp) / "_cb_converted"
            converted = convert_to_paz_mod(cb_manifest, game_dir, cb_work)
            if converted is not None:
                cb_name = cb_manifest.get("id", mod_name)
                modinfo = _read_modinfo(tmp_path)
                if modinfo and modinfo.get("name"):
                    cb_name = modinfo["name"]
                result = _process_extracted_files(
                    converted, game_dir, db, snapshot, deltas_dir, cb_name,
                    existing_mod_id=existing_mod_id, modinfo=modinfo)
                # Compound-mod support (Lightsaber: CB mod in Lightsaber_v1_3/
                # plus a sibling Lightsaber_shop.json at archive root). Import
                # any JSON patches outside the CB subfolder as their own mods.
                cb_base = cb_manifest.get("_base_dir")
                if (cb_base and cb_base != tmp_path
                        and result and not result.error):
                    try:
                        _import_sibling_json_patches(
                            tmp_path, cb_base, game_dir, db, deltas_dir)
                    except Exception as e:
                        logger.debug("Sibling JSON scan after CB failed: %s", e)
                return result

        # Check for loose file mod (files/NNNN/ structure)
        lfm = detect_loose_file_mod(tmp_path)
        if lfm is not None:
            lfm_work = Path(tmp) / "_lfm_converted"
            converted = convert_to_paz_mod(lfm, game_dir, lfm_work)
            if converted is not None:
                mi = lfm.get("_modinfo", {})
                lfm_name = mi.get("title", mod_name)
                lfm_modinfo = {
                    "name": mi.get("title"), "version": mi.get("version"),
                    "author": mi.get("author"), "description": mi.get("description"),
                    "force_inplace": mi.get("force_inplace"),
                }
                return _process_extracted_files(
                    converted, game_dir, db, snapshot, deltas_dir, lfm_name,
                    existing_mod_id=existing_mod_id, modinfo=lfm_modinfo)

        # Check for JSON byte-patch format — use ENTR deltas for proper composition
        jp_data = detect_json_patch(tmp_path)
        if jp_data is not None:
            from cdumm.engine.json_patch_handler import import_json_as_entr
            jp_name = _json_mod_display_name(jp_data, mod_name)
            jp_modinfo = _json_mod_modinfo(jp_data)
            entr_result = import_json_as_entr(
                jp_data, game_dir, db, deltas_dir, jp_name,
                existing_mod_id=existing_mod_id, modinfo=jp_modinfo)
            if entr_result is None:
                # JSON patch detected but failed — don't fall through to slow PAZ scan
                result = ModImportResult(jp_name)
                target_files = [p.get("game_file", "?") for p in jp_data.get("patches", [])]
                result.error = (
                    f"JSON patch mod detected but failed to process. "
                    f"Target game file(s) not found: {', '.join(target_files)}. "
                    f"Use Inspect Mod for a detailed diagnostic report.")
                return result
            if entr_result is not None:
                if entr_result.get("version_mismatch"):
                    result = ModImportResult(jp_name)
                    game_ver = entr_result.get("game_version", "unknown")
                    mismatched = entr_result.get("mismatched", 0)
                    result.error = (
                        f"This mod is incompatible with the current game version. "
                        f"{mismatched} byte patches don't match — the game data has "
                        f"changed since this mod was created (mod targets version "
                        f"{game_ver}). The mod author needs to update it.")
                    return result
                if not entr_result["changed_files"]:
                    result.error = (
                        "This mod's changes are already present in your game files. "
                        "Nothing to apply.")
                    return result
                result = ModImportResult(jp_name)
                result.changed_files = entr_result["changed_files"]
                if jp_data.get("patches"):
                    _store_json_patches(db, result, jp_data, game_dir)
                return result

        # Check for DDS texture mod (folder of .dds files, no PAZ/PAMT)
        tex_info = detect_texture_mod(tmp_path)
        if tex_info is not None:
            tex_work = Path(tmp) / "_tex_converted"
            converted = convert_texture_mod(tex_info, game_dir, tex_work)
            if converted is not None:
                tex_name = tex_info.get("name", mod_name)
                modinfo = _read_modinfo(tmp_path)
                if modinfo and modinfo.get("name"):
                    tex_name = modinfo["name"]
                result = _process_extracted_files(
                    converted, game_dir, db, snapshot, deltas_dir, tex_name,
                    existing_mod_id=existing_mod_id, modinfo=modinfo)
                return result

        # Check if zip contains a script. When it's a single-script
        # zip (like Glider Stamina's glider-stamina.bat), route it to
        # the script-mod flow so the user doesn't have to unzip first.
        scripts = list(tmp_path.glob("*.bat")) + list(tmp_path.glob("*.py"))
        if scripts and not _match_game_files(tmp_path, game_dir, snapshot):
            if len(scripts) == 1:
                logger.info(
                    "Zip '%s' is a single-script mod (%s) — routing to "
                    "import_from_script", archive_path.name, scripts[0].name)
                return import_from_script(
                    scripts[0], game_dir, db, snapshot, deltas_dir)
            names = ", ".join(s.name for s in scripts)
            result.error = (
                f"'{archive_path.name}' contains {len(scripts)} scripts "
                f"({names}). Extract and drop the specific script you "
                "want to run."
            )
            return result

        # Detect multi-variant zips
        variant = _find_best_variant(tmp_path)
        if variant:
            logger.info("Multi-variant zip, using: %s", variant.name)
            tmp_path = variant
            mod_name = f"{mod_name} ({variant.name})"
        else:
            wrapped = _first_numbered_parent(tmp_path)
            if wrapped is not None and wrapped != tmp_path:
                logger.info("Descending wrapper: %s -> %s",
                            tmp_path.name, wrapped.relative_to(tmp_path))
                tmp_path = wrapped

        # Read mod metadata from modinfo.json if present
        modinfo = _read_modinfo(tmp_path)
        if modinfo and modinfo.get("name"):
            mod_name = modinfo["name"]

        result = _process_extracted_files(
            tmp_path, game_dir, db, snapshot, deltas_dir, mod_name,
            existing_mod_id=existing_mod_id, modinfo=modinfo)

    return result


def _import_sibling_json_patches(
    root: Path, exclude_subdir: Path, game_dir: Path,
    db: Database, deltas_dir: Path,
    failures_out: list[str] | None = None,
) -> None:
    """Import JSON byte-patch siblings that live alongside a CB/loose-file mod.

    Used when a compound archive contains multiple independent mod
    components: e.g. a CB mod in Lightsaber_v1_3/ plus a standalone
    Lightsaber_shop.json at the archive root. The JSONs outside
    ``exclude_subdir`` are imported as their own separate mods so the
    user can toggle stat changes vs shop changes independently.
    """
    from cdumm.engine.json_patch_handler import (
        detect_json_patches_all, import_json_as_entr,
    )
    import re as _re_sib
    _NNNN = _re_sib.compile(r"^\d{4}$")
    try:
        exclude_resolved = exclude_subdir.resolve()
    except OSError:
        exclude_resolved = exclude_subdir

    candidates: list[Path] = []
    for p in root.rglob("*.json"):
        try:
            presolved = p.resolve()
            if exclude_resolved in presolved.parents or presolved == exclude_resolved:
                continue
        except OSError:
            continue
        # Mirror the NNNN/meta guard from detect_json_patches_all so we
        # don't try to parse extracted-vanilla JSON-looking blobs as mod
        # patches when the archive happens to ship alongside those dirs.
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if any(_NNNN.match(part) or part.lower() == "meta"
               for part in rel.parts[:-1]):
            continue
        candidates.append(p)

    for cand in candidates:
        jp_list = detect_json_patches_all(cand)
        for jp_data in jp_list:
            jp_name = _json_mod_display_name(
                jp_data, jp_data["_json_path"].stem)
            jp_modinfo = _json_mod_modinfo(jp_data)
            # NOTE: we intentionally do NOT reuse an existing_mod_id by
            # name here. A prior attempt matched `SELECT id FROM mods
            # WHERE name = ?`, but that's too broad — an unrelated mod
            # with the same display name ("Shop", "Dark Mode" etc.)
            # would get its deltas/source silently overwritten. The
            # better trade-off is to accept a potential duplicate row
            # on compound-archive re-import and let the user remove the
            # stale sibling manually; we will address this properly
            # with a compound-archive ID once the import worker can
            # thread that lineage through.
            # Snapshot the current max mod_id BEFORE calling the
            # importer. If it raises mid-insert, the only orphans to
            # clean up are rows with id > this watermark. Matching
            # strictly by name was too broad — any pre-existing mod
            # with the same display name and zero deltas (a legitimate
            # in-progress import, or an earlier failed one) got
            # silently wiped too. GDS + BMAD finding C-H5.
            try:
                pre_max_row = db.connection.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM mods").fetchone()
                pre_max_id = int(pre_max_row[0]) if pre_max_row else 0
            except Exception:
                pre_max_id = 0
            try:
                entr_result = import_json_as_entr(
                    jp_data, game_dir, db, deltas_dir, jp_name,
                    existing_mod_id=None, modinfo=jp_modinfo)
            except Exception as e:
                logger.warning(
                    "Sibling JSON '%s' import failed: %s",
                    cand.name, e)
                # Surface to caller so the GUI can show an InfoBar
                # instead of the user seeing the primary succeed with
                # no visible indication a sibling failed. GDS #7.
                if failures_out is not None:
                    failures_out.append(
                        f"sibling '{cand.name}' failed: {e}")
                # Scope the orphan scan to mods inserted AFTER the
                # watermark, with name=jp_name and zero deltas.
                try:
                    from cdumm.engine.mod_manager import ModManager
                    rows = db.connection.execute(
                        "SELECT m.id FROM mods m "
                        "LEFT JOIN mod_deltas d ON d.mod_id = m.id "
                        "WHERE m.name = ? AND m.id > ? AND d.id IS NULL",
                        (jp_name, pre_max_id)).fetchall()
                    mm = ModManager(db, deltas_dir)
                    for (gid,) in rows:
                        try:
                            mm.remove_mod(gid)
                        except Exception as e_rm:
                            logger.debug(
                                "orphan sibling cleanup remove_mod(%d) "
                                "failed: %s", gid, e_rm)
                except Exception as e_scan:
                    logger.debug(
                        "orphan sibling cleanup scan failed: %s", e_scan)
                continue
            # Surface silent failures explicitly. import_json_as_entr
            # may create a ghost DB row before deciding a mod is
            # incompatible (version_mismatch) or empty (no changed
            # files). Roll back so the user doesn't see an orphan
            # "Lightsaber_shop" row with zero deltas and no toggle.
            if entr_result is None:
                logger.warning(
                    "Sibling JSON '%s' (%s): importer returned None",
                    cand.name, jp_name)
                continue
            if entr_result.get("version_mismatch"):
                game_ver = entr_result.get("game_version", "unknown")
                mismatched = entr_result.get("mismatched", 0)
                logger.warning(
                    "Sibling JSON '%s' (%s): version mismatch, "
                    "%d patches don't match vanilla (mod targets %s). "
                    "Not importing.",
                    cand.name, jp_name, mismatched, game_ver)
                # Roll back any ghost mod row the importer may have
                # created before failing. Use ModManager so mod_deltas /
                # conflicts / sources folder get cleaned up too — the
                # raw DELETE here was orphaning delta rows and the
                # archived sources dir on every rejection.
                gid = entr_result.get("mod_id")
                if gid is not None:
                    try:
                        from cdumm.engine.mod_manager import ModManager
                        ModManager(db, deltas_dir).remove_mod(gid)
                    except Exception as e_rb:
                        logger.debug(
                            "Sibling rollback via ModManager failed (%s), "
                            "falling back to raw DELETE", e_rb)
                        try:
                            db.connection.execute(
                                "DELETE FROM mods WHERE id = ?", (gid,))
                            db.connection.commit()
                        except Exception:
                            pass
                continue
            if not entr_result.get("changed_files"):
                logger.info(
                    "Sibling JSON '%s' (%s): no file changes, "
                    "already applied or noop — rolling back",
                    cand.name, jp_name)
                # Symmetry with the version_mismatch branch: the importer
                # may have created a ghost mod row before realising no
                # bytes needed changing. Remove it so the user doesn't see
                # an orphan sibling entry with zero deltas.
                gid = entr_result.get("mod_id")
                if gid is not None:
                    try:
                        from cdumm.engine.mod_manager import ModManager
                        ModManager(db, deltas_dir).remove_mod(gid)
                    except Exception as e_rb:
                        logger.debug(
                            "Sibling no-changes rollback failed (%s)", e_rb)
                        try:
                            db.connection.execute(
                                "DELETE FROM mods WHERE id = ?", (gid,))
                            db.connection.commit()
                        except Exception:
                            pass
                continue
            logger.info(
                "Compound mod: sibling JSON '%s' imported as separate mod "
                "'%s' (%d files changed)",
                cand.name, jp_name, len(entr_result["changed_files"]))


def import_from_folder(
    folder_path: Path, game_dir: Path, db: Database, snapshot: SnapshotManager, deltas_dir: Path,
    existing_mod_id: int | None = None,
) -> ModImportResult:
    """Import a mod from a folder of modified files."""
    mod_name = folder_path.name

    # Check for Crimson Browser format and convert if needed
    manifest = detect_crimson_browser(folder_path)
    if manifest is not None:
        with tempfile.TemporaryDirectory() as cb_tmp:
            cb_work = Path(cb_tmp) / "_cb_converted"
            converted = convert_to_paz_mod(manifest, game_dir, cb_work)
            if converted is not None:
                cb_name = manifest.get("id", mod_name)
                modinfo = _read_modinfo(folder_path)
                if modinfo and modinfo.get("name"):
                    cb_name = modinfo["name"]
                cb_result = _process_extracted_files(
                    converted, game_dir, db, snapshot, deltas_dir, cb_name,
                    existing_mod_id=existing_mod_id, modinfo=modinfo)
                # Compound-mod support: when the CB manifest came from a
                # SUBFOLDER (Lightsaber_v1_3/ inside a zip that also has a
                # sibling Lightsaber_shop.json at the root), the sibling
                # JSON patches are their own independent mod components and
                # should be imported as separate mods so the user can toggle
                # stat-buff vs shop-addition independently.
                cb_base_raw = manifest.get("_base_dir")
                cb_base = Path(cb_base_raw) if cb_base_raw is not None else None
                if (cb_base and cb_base != folder_path
                        and cb_result and not cb_result.error
                        and cb_result.changed_files):
                    try:
                        _import_sibling_json_patches(
                            folder_path, cb_base, game_dir, db, deltas_dir)
                    except Exception as e:
                        logger.debug("Sibling JSON scan after CB failed: %s", e)
                return cb_result

    # Check for loose file mod (mod.json + files/ directory)
    lfm = detect_loose_file_mod(folder_path)
    if lfm is not None:
        with tempfile.TemporaryDirectory() as lfm_tmp:
            lfm_work = Path(lfm_tmp) / "_lfm_converted"
            converted = convert_to_paz_mod(lfm, game_dir, lfm_work)
            if converted is not None:
                mi = lfm.get("_modinfo", {})
                lfm_name = mi.get("title", mod_name)
                lfm_modinfo = {
                    "name": mi.get("title"), "version": mi.get("version"),
                    "author": mi.get("author"), "description": mi.get("description"),
                    "force_inplace": mi.get("force_inplace"),
                }
                return _process_extracted_files(
                    converted, game_dir, db, snapshot, deltas_dir, lfm_name,
                    existing_mod_id=existing_mod_id, modinfo=lfm_modinfo)

    # Check for JSON byte-patch format in folder — use ENTR deltas.
    # When the folder has MULTIPLE valid JSONs (Trust Me main + Pet
    # Abyss Gear, bundled packs), import each as its own mod so the
    # user can toggle them independently. Only the first is returned
    # to the caller as the primary result; the rest are logged and
    # appear in the mod list after this function returns.
    from cdumm.engine.json_patch_handler import (
        detect_json_patches_all, import_json_as_entr,
    )
    jp_list = detect_json_patches_all(folder_path)
    if jp_list:
        if len(jp_list) > 1:
            logger.info(
                "Folder '%s' contains %d JSON patches — importing each "
                "as a separate mod", folder_path.name, len(jp_list))
        # Import the first as the primary result we return
        primary_result = None
        for idx, jp_data in enumerate(jp_list):
            jp_name = _json_mod_display_name(
                jp_data, jp_data["_json_path"].stem)
            jp_modinfo = _json_mod_modinfo(jp_data)
            # Only the first JSON reuses the existing_mod_id (when
            # re-importing). Extras always create new mod rows.
            target_id = existing_mod_id if idx == 0 else None
            entr_result = import_json_as_entr(
                jp_data, game_dir, db, deltas_dir, jp_name,
                existing_mod_id=target_id, modinfo=jp_modinfo)
            if entr_result is None:
                continue
            if entr_result.get("version_mismatch"):
                if idx == 0:
                    result = ModImportResult(jp_name)
                    game_ver = entr_result.get("game_version", "unknown")
                    mismatched = entr_result.get("mismatched", 0)
                    result.error = (
                        f"This mod is incompatible with the current game version. "
                        f"{mismatched} byte patches don't match — the game data has "
                        f"changed since this mod was created (mod targets version "
                        f"{game_ver}). The mod author needs to update it.")
                    primary_result = result
                else:
                    logger.warning(
                        "Skipping bundled JSON '%s' — version mismatch",
                        jp_data["_json_path"].name)
                continue
            if not entr_result["changed_files"]:
                if idx == 0:
                    result = ModImportResult(jp_name)
                    result.error = (
                        "This mod's changes are already present in your game files. "
                        "Nothing to apply.")
                    primary_result = result
                continue
            result = ModImportResult(jp_name)
            result.changed_files = entr_result["changed_files"]
            if jp_data.get("patches"):
                _store_json_patches(db, result, jp_data, game_dir)
            if idx == 0:
                primary_result = result
            else:
                logger.info(
                    "Bundled JSON '%s' imported as separate mod '%s' "
                    "(%d files changed)",
                    jp_data["_json_path"].name, jp_name,
                    len(result.changed_files))
        if primary_result is not None:
            return primary_result

    # Check for DDS texture mod (folder of .dds files, no PAZ/PAMT)
    tex_info = detect_texture_mod(folder_path)
    if tex_info is not None:
        with tempfile.TemporaryDirectory() as tex_tmp:
            tex_work = Path(tex_tmp) / "_tex_converted"
            converted = convert_texture_mod(tex_info, game_dir, tex_work)
            if converted is not None:
                tex_name = tex_info.get("name", mod_name)
                modinfo = _read_modinfo(folder_path)
                if modinfo and modinfo.get("name"):
                    tex_name = modinfo["name"]
                return _process_extracted_files(
                    converted, game_dir, db, snapshot, deltas_dir, tex_name,
                    existing_mod_id=existing_mod_id, modinfo=modinfo)

    # Check if folder contains scripts instead of game files
    scripts = list(folder_path.glob("*.bat")) + list(folder_path.glob("*.py"))
    if scripts and not _match_game_files(folder_path, game_dir, snapshot):
        result = ModImportResult(folder_path.name)
        result.error = (
            f"'{folder_path.name}' is a standalone script/tool, not a CDUMM mod. "
            "Run it directly if you need the tool, or drop the .py/.bat file "
            "individually if it's a CDUMM script mod."
        )
        return result

    # Detect variant folders: parent folder has multiple subdirectories each
    # containing their own 0.paz + 0.pamt (e.g., FatStacks2x/, FatStacks10x/).
    # Find the best single variant to import.
    variant = _find_best_variant(folder_path)
    if variant:
        logger.info("Multi-variant mod detected, using variant: %s", variant.name)
        folder_path = variant
        mod_name = f"{folder_path.parent.name} ({variant.name})"
    else:
        # Single-variant mods may wrap content in '<ModName>/files/NNNN/' or
        # similar. Descend through unambiguous single-subdir wrappers until we
        # find the folder that directly contains the NNNN game directories.
        wrapped = _first_numbered_parent(folder_path)
        if wrapped is not None and wrapped != folder_path:
            logger.info("Descending wrapper folder: %s -> %s",
                        folder_path.name, wrapped.relative_to(folder_path))
            folder_path = wrapped

    # Read mod metadata from modinfo.json if present
    modinfo = _read_modinfo(folder_path)
    if modinfo and modinfo.get("name"):
        mod_name = modinfo["name"]

    result = _process_extracted_files(
        folder_path, game_dir, db, snapshot, deltas_dir, mod_name,
        existing_mod_id=existing_mod_id, modinfo=modinfo)

    # Second pass: import loose game files not matched by _match_game_files.
    # Only runs for mixed-format mods (standalone PAZ dirs + loose files).
    if result.changed_files and not result.error and result.mod_id is not None:
        has_standalone_paz = any(
            d.is_dir() and d.name.isdigit() and len(d.name) == 4
            and (d / "0.paz").exists()
            for d in folder_path.iterdir() if d.is_dir()
        )
        if has_standalone_paz:
            _import_remaining_loose_files(
                folder_path, game_dir, db, snapshot, deltas_dir, result)

    return result


def _first_numbered_parent(root: Path, max_depth: int = 4) -> Path | None:
    """Return the directory that directly contains an NNNN 4-digit subfolder.

    Walks down from ``root`` through single-subdir wrappers (e.g. ``files/``)
    up to ``max_depth`` levels. Stops as soon as any NNNN child is found.
    Returns None if no numbered dir is reachable without branching.
    """
    import re as _re
    current = root
    for _ in range(max_depth):
        if not current.is_dir():
            return None
        subdirs = [c for c in current.iterdir() if c.is_dir()]
        for c in subdirs:
            if _re.match(r"^\d{4}$", c.name):
                return current
        # Only descend through unambiguous single-subdir wrappers
        if len(subdirs) == 1:
            current = subdirs[0]
            continue
        return None
    return None


def _find_best_variant(folder_path: Path) -> Path | None:
    """Detect if a folder contains multiple mod variants.

    Returns the best variant root, or None if not a multi-variant mod.
    Variants are either:
      - subdirectories each containing 0.paz + 0.pamt
      - subdirectories each containing at least one NNNN/ game directory
        (may be nested under e.g. ``<variant>/files/0000/``)

    When the variant wraps its content under an extra directory, the returned
    path is the inner folder that directly contains the NNNN/ subdirs — so the
    caller can treat it as a mod root without additional descent.
    """
    # Tier 1: variants with 0.paz + 0.pamt (original JSON MM style bundles)
    variants: list[Path] = []
    for sub in folder_path.iterdir():
        if not sub.is_dir():
            continue
        has_paz = list(sub.rglob("0.paz"))
        has_pamt = list(sub.rglob("0.pamt"))
        if has_paz and has_pamt:
            variants.append(sub)

    if len(variants) >= 2:
        variants.sort(key=lambda p: p.name)
        chosen = variants[-1]
        logger.info("Found %d PAZ variants: %s. Picking: %s",
                    len(variants), [v.name for v in variants], chosen.name)
        return chosen

    # Tier 2: variants by NNNN directory presence (Crimson Browser-style nests)
    nnnn_variants: list[tuple[Path, Path]] = []  # (variant_subdir, content_root)
    for sub in folder_path.iterdir():
        if not sub.is_dir():
            continue
        content_root = _first_numbered_parent(sub)
        if content_root is not None:
            nnnn_variants.append((sub, content_root))

    if len(nnnn_variants) >= 2:
        nnnn_variants.sort(key=lambda t: t[0].name)
        chosen_sub, chosen_root = nnnn_variants[-1]
        logger.info("Found %d NNNN variants: %s. Picking: %s (content root: %s)",
                    len(nnnn_variants),
                    [v.name for v, _ in nnnn_variants],
                    chosen_sub.name,
                    chosen_root.relative_to(folder_path))
        return chosen_root

    return None


def import_from_script(
    script_path: Path, game_dir: Path, db: Database, snapshot: SnapshotManager, deltas_dir: Path
) -> ModImportResult:
    """Import a mod by running a script in a sandbox and capturing the diff."""
    mod_name = script_path.stem
    result = ModImportResult(mod_name)

    with tempfile.TemporaryDirectory() as sandbox:
        sandbox_path = Path(sandbox)

        # Copy game files the script might modify into sandbox
        # Copy all PAZ/PAMT files (script might target any of them)
        for dir_name in [f"{i:04d}" for i in range(33)]:
            src_dir = game_dir / dir_name
            if src_dir.exists():
                dst_dir = sandbox_path / dir_name
                dst_dir.mkdir(exist_ok=True)
                for f in src_dir.iterdir():
                    if f.is_file() and f.suffix.lower() in (".paz", ".pamt"):
                        shutil.copy2(f, dst_dir / f.name)

        # Copy meta directory
        meta_src = game_dir / "meta"
        if meta_src.exists():
            meta_dst = sandbox_path / "meta"
            shutil.copytree(meta_src, meta_dst)

        # Copy the script into sandbox
        shutil.copy2(script_path, sandbox_path / script_path.name)

        # Execute the script
        suffix = script_path.suffix.lower()
        if suffix == ".bat":
            cmd = ["cmd.exe", "/c", script_path.name]
        elif suffix == ".py":
            cmd = ["py", "-3", script_path.name]
        else:
            result.error = f"Unsupported script type: {suffix}"
            return result

        try:
            _no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.run(
                cmd,
                cwd=str(sandbox_path),
                timeout=SCRIPT_TIMEOUT,
                capture_output=True,
                text=True,
                creationflags=_no_window,
            )
            if proc.returncode != 0:
                logger.warning("Script exited with code %d: %s", proc.returncode, proc.stderr[:500])
        except subprocess.TimeoutExpired:
            result.error = f"Script timed out after {SCRIPT_TIMEOUT} seconds"
            return result
        except Exception as e:
            result.error = f"Script execution failed: {e}"
            return result

        # Now diff the sandbox against vanilla
        result = _process_sandbox_diff(sandbox_path, game_dir, db, snapshot, deltas_dir, mod_name)

    return result


def import_from_bsdiff(
    patch_path: Path, game_dir: Path, db: Database, snapshot: SnapshotManager, deltas_dir: Path
) -> ModImportResult:
    """Import a mod distributed as a bsdiff patch file.

    Auto-detects which game file the patch targets by trying to apply it
    against each file in the snapshot. Uses the bsdiff output size to
    narrow candidates first (fast), then tries actual patching.
    """
    import struct
    import bsdiff4

    mod_name = patch_path.stem
    result = ModImportResult(mod_name)

    delta_bytes = patch_path.read_bytes()

    # Validate it's actually a bsdiff
    if not delta_bytes[:8] == b"BSDIFF40":
        result.error = "Not a valid bsdiff4 patch file."
        return result

    # Read expected output size from bsdiff header (offset 16, 8 bytes LE)
    new_size = struct.unpack("<q", delta_bytes[16:24])[0]
    logger.info("bsdiff patch '%s': expected output size = %d bytes", mod_name, new_size)

    # Find the target game file by trying to apply the patch.
    # First, narrow candidates by checking which files exist in the snapshot.
    # Then try applying the patch — only the correct source file will succeed.
    cursor = db.connection.execute("SELECT file_path, file_size FROM snapshots")
    candidates = cursor.fetchall()

    target_path = None
    patched_bytes = None

    # Try filename-encoded path first (e.g., "0035_0.paz.bsdiff" → "0035/0.paz")
    stem = patch_path.stem
    # Handle double extension like "0035_0.paz.bsdiff" where stem is "0035_0.paz"
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    encoded_path = stem.replace("_", "/")
    for file_path, file_size in candidates:
        if file_path == encoded_path:
            game_file = game_dir / file_path.replace("/", "\\")
            if game_file.exists():
                try:
                    source = game_file.read_bytes()
                    patched_bytes = bsdiff4.patch(source, delta_bytes)
                    target_path = file_path
                    logger.info("bsdiff target found by filename: %s", target_path)
                    break
                except Exception:
                    pass

    # If filename didn't work, try all snapshot files (filter by output size)
    if target_path is None:
        logger.info("Filename match failed, trying %d snapshot files...", len(candidates))
        for file_path, file_size in candidates:
            # Skip files that can't possibly match — bsdiff patches typically
            # produce output close to the original size
            if file_size is not None and abs(file_size - new_size) > file_size * 0.5:
                continue

            game_file = game_dir / file_path.replace("/", "\\")
            if not game_file.exists():
                continue

            try:
                source = game_file.read_bytes()
                patched_bytes = bsdiff4.patch(source, delta_bytes)
                target_path = file_path
                logger.info("bsdiff target found by brute-force: %s", target_path)
                break
            except Exception:
                continue

    if target_path is None:
        result.error = (
            "Could not find which game file this patch targets.\n\n"
            "The bsdiff patch didn't match any game file in the snapshot.\n"
            "Make sure your game files are verified through Steam."
        )
        return result

    # Generate our own delta (vanilla → patched) so it goes through the
    # standard apply pipeline with proper byte-range tracking
    vanilla_file = game_dir / "CDMods" / "vanilla" / target_path.replace("/", "\\")
    if vanilla_file.exists():
        vanilla_bytes = vanilla_file.read_bytes()
    else:
        vanilla_bytes = (game_dir / target_path.replace("/", "\\")).read_bytes()

    our_delta = generate_delta(vanilla_bytes, patched_bytes)
    byte_ranges = get_changed_byte_ranges(vanilla_bytes, patched_bytes)

    # Store mod in database
    priority = _next_priority(db)
    cursor = db.connection.execute(
        "INSERT INTO mods (name, mod_type, priority, source_path) VALUES (?, ?, ?, ?)",
        (prettify_mod_name(mod_name), "paz", priority, str(patch_path)),
    )
    mod_id = cursor.lastrowid

    safe_name = target_path.replace("/", "_") + ".delta"
    delta_dest = deltas_dir / str(mod_id) / safe_name
    save_delta(our_delta, delta_dest)

    for bs, be in byte_ranges:
        db.connection.execute(
            "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, byte_end) "
            "VALUES (?, ?, ?, ?, ?)",
            (mod_id, target_path, str(delta_dest), bs, be),
        )

    db.connection.execute(
        "INSERT OR IGNORE INTO mod_vanilla_sizes (mod_id, file_path, vanilla_size) "
        "VALUES (?, ?, ?)",
        (mod_id, target_path, len(vanilla_bytes)),
    )
    db.connection.commit()

    result.changed_files.append({
        "file_path": target_path,
        "delta_path": str(delta_dest),
        "byte_ranges": byte_ranges,
    })
    logger.info("bsdiff import: %s targets %s (%d byte ranges)",
                mod_name, target_path, len(byte_ranges))
    return result


def import_from_json_patch(
    json_path: Path, game_dir: Path, db: Database, snapshot: SnapshotManager, deltas_dir: Path,
    existing_mod_id: int | None = None,
) -> ModImportResult:
    """Import a mod from a JSON byte-patch file.

    Uses mount-time patching: stores the JSON file and applies patches
    from vanilla at Apply time. No PAZ extraction at import = instant import.

    Falls back to ENTR delta import if mount-time setup fails.
    """
    patch_data = detect_json_patch(json_path if json_path.is_file() else json_path)
    if patch_data is None:
        result = ModImportResult(json_path.stem)
        result.error = "Not a valid JSON patch mod."
        return result

    modinfo = _json_mod_modinfo(patch_data)
    mod_name = _json_mod_display_name(patch_data, json_path.stem)

    # Mount-time fast import: just store JSON, patches applied at Apply time
    mods_dir = game_dir / "CDMods" / "mods"
    fast_result = import_json_fast(
        patch_data, game_dir, db, mods_dir, mod_name,
        existing_mod_id=existing_mod_id, modinfo=modinfo)

    if fast_result is not None and fast_result.get("changed_files"):
        result = ModImportResult(mod_name)
        result.changed_files = fast_result["changed_files"]
        result.mod_id = fast_result.get("mod_id")
        if patch_data.get("patches"):
            _store_json_patches(db, result, patch_data, game_dir)
        return result

    # Fallback: ENTR delta import (if fast import failed, e.g. game file not found)
    if fast_result is None:
        logger.info("Fast import failed for '%s', falling back to ENTR import", mod_name)
        from cdumm.engine.json_patch_handler import import_json_as_entr
        entr_result = import_json_as_entr(
            patch_data, game_dir, db, deltas_dir, mod_name,
            existing_mod_id=existing_mod_id, modinfo=modinfo)

        if entr_result is None:
            result = ModImportResult(mod_name)
            result.error = "Failed to apply JSON patches to game files."
            return result

        if entr_result.get("version_mismatch"):
            result = ModImportResult(mod_name)
            game_ver = entr_result.get("game_version", "unknown")
            mismatched = entr_result.get("mismatched", 0)
            result.error = (
                f"This mod is incompatible with the current game version. "
                f"{mismatched} byte patches don't match — the game data has "
                f"changed since this mod was created (mod targets version "
                f"{game_ver}). The mod author needs to update it.")
            return result

        if not entr_result["changed_files"]:
            result = ModImportResult(mod_name)
            result.error = (
                "This mod's changes are already present in your game files. "
                "Nothing to apply.")
            return result

        result = ModImportResult(mod_name)
        result.changed_files = entr_result["changed_files"]
        if patch_data.get("patches"):
            _store_json_patches(db, result, patch_data, game_dir)
        return result

    # fast_result returned but empty changed_files
    result = ModImportResult(mod_name)
    result.error = (
        "This mod's changes are already present in your game files. "
        "Nothing to apply.")
    return result


def _store_json_patches(db: Database, result, patch_data: dict, game_dir: Path) -> None:
    """Store original JSON patch data in mod_deltas for three-way merge.

    Maps each game_file in the JSON to its mod_deltas row via PAMT lookup,
    then stores the changes array as json_patches.
    """
    import json
    from cdumm.engine.json_patch_handler import _find_pamt_entry

    # Get the mod_id from the result's changed files
    if not result.changed_files:
        return

    # Find mod_id from the first delta
    first_delta = result.changed_files[0].get("delta_path")
    if not first_delta:
        return
    row = db.connection.execute(
        "SELECT mod_id FROM mod_deltas WHERE delta_path = ? LIMIT 1",
        (first_delta,)).fetchone()
    if not row:
        return
    mod_id = row[0]

    vanilla_dir = game_dir / "CDMods" / "vanilla"
    base_dir = vanilla_dir if vanilla_dir.exists() else game_dir

    for patch in patch_data.get("patches", []):
        game_file = patch.get("game_file")
        changes = patch.get("changes")
        if not game_file or not changes:
            continue

        # Find which PAZ file contains this game file
        entry = _find_pamt_entry(game_file, base_dir)
        if not entry:
            continue

        # The PAZ file path in mod_deltas
        import os
        pamt_dir = os.path.basename(os.path.dirname(entry.paz_file))
        paz_file_path = f"{pamt_dir}/{entry.paz_index}.paz"

        # Store the patches JSON on the matching mod_deltas row
        patches_json = json.dumps({
            "game_file": game_file,
            "entry_path": entry.path,
            "changes": changes,
        })

        # Update the specific mod_deltas row for this entry
        # (scoped to entry_path to avoid overwriting other entries in same PAZ)
        updated = db.connection.execute(
            "UPDATE mod_deltas SET json_patches = ? "
            "WHERE mod_id = ? AND entry_path = ?",
            (patches_json, mod_id, entry.path),
        ).rowcount
        if not updated:
            # Fallback for mods without entry_path (old SPRS deltas)
            db.connection.execute(
                "UPDATE mod_deltas SET json_patches = ? "
                "WHERE mod_id = ? AND file_path LIKE ? AND json_patches IS NULL",
                (patches_json, mod_id, f"{pamt_dir}/%"),
            )

    db.connection.commit()
    logger.info("Stored JSON patch data for mod %d (%d patches)",
                mod_id, len(patch_data.get("patches", [])))


def _process_extracted_files(
    extracted_dir: Path,
    game_dir: Path,
    db: Database,
    snapshot: SnapshotManager,
    deltas_dir: Path,
    mod_name: str,
    existing_mod_id: int | None = None,
    modinfo: dict | None = None,
) -> ModImportResult:
    """Common logic for zip and folder imports: match files, generate deltas, store.

    If existing_mod_id is provided, reuses that mod entry (for updates).
    """
    import time as _time
    _stage_t0 = _time.perf_counter()
    _stage_log: list[tuple[str, float]] = []  # (stage, ms)

    def _stage(name: str) -> None:
        nonlocal _stage_t0
        now = _time.perf_counter()
        _stage_log.append((name, (now - _stage_t0) * 1000))
        _stage_t0 = now

    result = ModImportResult(mod_name)

    _emit_progress(2, f"Matching files for {mod_name}...")
    matches = _match_game_files(extracted_dir, game_dir, snapshot)
    _stage("match_game_files")
    if not matches:
        # Build a short inventory of what we DID see so the user (and
        # we, when they paste the error) can tell what's wrong. Most
        # "unrecognized folder" reports have the mod in a non-standard
        # layout CDUMM can't auto-detect — listing the extensions we
        # found makes it obvious whether the drop was a readme, a
        # loose DDS, a weird JSON schema, etc.
        counts: dict[str, int] = {}
        total = 0
        for p in extracted_dir.rglob("*"):
            if p.is_file():
                total += 1
                ext = p.suffix.lower() or "(no-ext)"
                counts[ext] = counts.get(ext, 0) + 1
                if total > 500:  # cap so huge folders don't stall
                    break
        summary = ", ".join(f"{n} {ext}" for ext, n in sorted(
            counts.items(), key=lambda kv: -kv[1])[:6])
        result.error = (
            "This doesn't look like a CDUMM-supported mod. No PAZ/PAMT "
            "files, no valid JSON patch, no recognised Crimson Browser "
            f"manifest, and no DDS texture pack.\nFound {total} file(s): "
            f"{summary}\nIf this is a new format, paste the mod's "
            "Nexus page in the Bug Report card so we can add support."
        )
        return result

    new_count = sum(1 for _, _, is_new in matches if is_new)
    mod_count = sum(1 for _, _, is_new in matches if not is_new)
    _emit_progress(5, f"Matched {len(matches)} files ({mod_count} existing, {new_count} new)")
    logger.info("Matched %d files (%d existing, %d new)", len(matches), mod_count, new_count)

    # Run health check on mod files before importing
    try:
        from cdumm.engine.mod_health_check import check_mod_health, auto_fix_matches
        mod_file_map = {rel: abs_path for rel, abs_path, _ in matches}
        result.health_issues = check_mod_health(mod_file_map, game_dir)
        if result.health_issues:
            critical = [i for i in result.health_issues if i.severity == "critical"]
            logger.info("Health check: %d issues (%d critical)",
                        len(result.health_issues), len(critical))
            # Auto-fix: filter out broken files from import
            fixed = auto_fix_matches(
                [(rel, p) for rel, p, _ in matches],
                result.health_issues, game_dir)
            # Rebuild matches with is_new flags preserved
            fixed_set = {rel for rel, _ in fixed}
            matches = [(rel, p, is_new) for rel, p, is_new in matches if rel in fixed_set]
            logger.info("After auto-fix: %d files to import", len(matches))
    except Exception as e:
        logger.warning("Health check failed (non-fatal): %s", e)
    _stage("health_check")

    force_inplace = 1 if (modinfo and modinfo.get("force_inplace")) else 0
    # Use prettified name for DB storage
    mod_name = result.name
    # Override with modinfo name if provided
    if modinfo and modinfo.get("name"):
        mod_name = prettify_mod_name(modinfo["name"])

    conflict_mode = modinfo.get("conflict_mode", "normal") if modinfo else "normal"
    if conflict_mode not in ("normal", "override"):
        conflict_mode = "normal"
    target_language = modinfo.get("target_language") if modinfo else None

    # Stamp with current game version so we can detect outdated mods later
    game_ver_hash = None
    try:
        from cdumm.engine.version_detector import detect_game_version
        game_ver_hash = detect_game_version(game_dir)
    except Exception:
        pass

    if existing_mod_id is not None:
        mod_id = existing_mod_id
        # Update metadata if modinfo provided
        if modinfo:
            db.connection.execute(
                "UPDATE mods SET author=?, version=?, description=?, force_inplace=?, "
                "game_version_hash=?, conflict_mode=?, target_language=? WHERE id=?",
                (modinfo.get("author"), modinfo.get("version"), modinfo.get("description"),
                 force_inplace, game_ver_hash, conflict_mode, target_language, mod_id),
            )
        elif game_ver_hash:
            db.connection.execute(
                "UPDATE mods SET game_version_hash=? WHERE id=?",
                (game_ver_hash, mod_id))
    else:
        # Store mod in database
        priority = _next_priority(db)
        author = modinfo.get("author") if modinfo else None
        version = modinfo.get("version") if modinfo else None
        description = modinfo.get("description") if modinfo else None
        cursor = db.connection.execute(
            "INSERT INTO mods (name, mod_type, priority, author, version, description, "
            "force_inplace, game_version_hash, conflict_mode, target_language) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mod_name, "paz", priority, author, version, description, force_inplace,
             game_ver_hash, conflict_mode, target_language),
        )
        mod_id = cursor.lastrowid

    result.mod_id = mod_id

    # Archive mod source files for auto-reimport after game updates.
    # Copy the extracted files to CDMods/sources/<mod_id>/ so the mod
    # can be re-imported without the user providing the original files.
    # Skip when reusing an existing mod (e.g., second pass of mixed-format
    # import) — sources were already archived from the first pass.
    if existing_mod_id is None:
        sources_dir = deltas_dir.parent / "sources" / str(mod_id)
        try:
            if sources_dir.exists():
                shutil.rmtree(sources_dir)
            shutil.copytree(extracted_dir, sources_dir, dirs_exist_ok=True)
            db.connection.execute(
                "UPDATE mods SET source_path = ? WHERE id = ?",
                (str(sources_dir), mod_id))
            logger.info("Archived mod source: %s -> %s", mod_name, sources_dir)
        except Exception as e:
            logger.warning("Failed to archive mod source: %s", e)

    _stage("db_setup")
    total_matches = len(matches)
    _paz_entr_handled: set[str] = set()  # PAZ/PAMT files handled by entry-level import
    _fp_sub_timings: dict[str, float] = {
        "is_new_file": 0.0,       # new-file copy path
        "paz_entr_import": 0.0,   # large PAZ via ENTR decomposition
        "fast_track_copy": 0.0,   # FULL_COPY for large different-size files
        "streaming_sparse": 0.0,  # streaming sparse delta (Rust sparse-diff)
        "small_bsdiff": 0.0,      # standard bsdiff for files <10MB
        "skipped_entr": 0.0,      # PAMT/PAZ already handled by ENTR
        "other": 0.0,
    }
    for match_idx, (rel_path, extracted_path, is_new) in enumerate(matches):
        pct = int((match_idx / max(total_matches, 1)) * 90) + 5
        size_mb = extracted_path.stat().st_size / 1048576
        _emit_progress(pct, f"Processing {rel_path} ({size_mb:.0f} MB)...")
        time.sleep(0)  # yield GIL so GUI stays responsive
        _iter_start = time.perf_counter()
        _iter_branch = "other"
        try:
            # Skip files already handled by entry-level PAZ decomposition
            if rel_path in _paz_entr_handled:
                _iter_branch = "skipped_entr"
                logger.info("Skipping %s — handled by entry-level PAZ import", rel_path)
                continue

            if is_new:
                _iter_branch = "is_new_file"
                # New file — store full copy, no delta needed
                safe_name = rel_path.replace("/", "_") + ".newfile"
                delta_path = deltas_dir / str(mod_id) / safe_name
                delta_path.parent.mkdir(parents=True, exist_ok=True)
                # Auto-fix PAMT CRC for new files too
                if rel_path.endswith(".pamt"):
                    raw = extracted_path.read_bytes()
                    if len(raw) >= 12:
                        raw = _verify_and_fix_pamt_crc(raw, rel_path)
                        delta_path.parent.mkdir(parents=True, exist_ok=True)
                        delta_path.write_bytes(raw)
                    else:
                        shutil.copy2(extracted_path, delta_path)
                else:
                    shutil.copy2(extracted_path, delta_path)

                file_size = extracted_path.stat().st_size
                db.connection.execute(
                    "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, byte_end, is_new) "
                    "VALUES (?, ?, ?, ?, ?, 1)",
                    (mod_id, rel_path, str(delta_path), 0, file_size),
                )

                result.changed_files.append({
                    "file_path": rel_path,
                    "delta_path": str(delta_path),
                    "is_new": True,
                })
                logger.info("Stored new file: %s (%d bytes)", rel_path, file_size)
                continue

            # Use vanilla backup if available (accurate base for delta),
            # fall back to current game file
            vanilla_backup = game_dir / "CDMods" / "vanilla" / rel_path.replace("/", "\\")
            vanilla_path = game_dir / rel_path.replace("/", "\\")
            vanilla_source = vanilla_backup if vanilla_backup.exists() else vanilla_path
            if not vanilla_source.exists():
                logger.warning("Vanilla file not found for %s, skipping", rel_path)
                continue

            mod_size = extracted_path.stat().st_size
            van_size = vanilla_source.stat().st_size

            # ── Entry-level decomposition for PAZ files ──────────────
            # Instead of storing byte-level diffs of the entire PAZ, decompose
            # into ENTR deltas per PAMT entry. This way two mods modifying
            # different entries in the same PAZ compose correctly.
            if rel_path.endswith(".paz") and mod_size > 10 * 1024 * 1024:
                _iter_branch = "paz_entr_import"
                entr_ok = _try_paz_entry_import(
                    extracted_path, vanilla_source, rel_path,
                    extracted_dir, game_dir, mod_id, db, deltas_dir, result)
                if entr_ok:
                    # Also mark the corresponding PAMT as handled — the apply
                    # engine rebuilds it from ENTR delta updates
                    _paz_entr_handled.add(rel_path)
                    pamt_rel = rel_path.rsplit("/", 1)[0] + "/0.pamt"
                    _paz_entr_handled.add(pamt_rel)
                    continue
                # For large PAZ files (>100MB), don't fall back to FULL_COPY.
                # A 900MB full copy is the old broken format. Skip the file —
                # the mod will show "outdated" and need reimporting from a
                # fresh download, or the mod author needs to update it.
                if mod_size > 100 * 1024 * 1024:
                    logger.warning(
                        "Skipping %s (%.0f MB) — ENTR decomposition failed, "
                        "refusing to store full PAZ copy. Mod may be outdated.",
                        rel_path, mod_size / 1048576)
                    continue

            # ── Fast-track for different-size large files ─────────────
            # When the mod file is a different size from vanilla, it's a true
            # full replacement — store as FULL_COPY with streaming I/O.
            # Same-size files use the standard sparse delta path so multiple
            # mods can compose their changes at different byte ranges.
            FAST_TRACK_THRESHOLD = 10 * 1024 * 1024  # 10MB

            if mod_size > FAST_TRACK_THRESHOLD and mod_size != van_size:
                _iter_branch = "fast_track_copy"
                from cdumm.engine.delta_engine import FULL_COPY_MAGIC
                safe_name = rel_path.replace("/", "_") + ".bsdiff"
                delta_path = deltas_dir / str(mod_id) / safe_name
                delta_path.parent.mkdir(parents=True, exist_ok=True)

                with open(delta_path, "wb") as out:
                    out.write(FULL_COPY_MAGIC)
                    with open(extracted_path, "rb") as inp:
                        shutil.copyfileobj(inp, out, length=1024 * 1024)

                db.connection.execute(
                    "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
                    "byte_start, byte_end, is_new) VALUES (?, ?, ?, 0, ?, 0)",
                    (mod_id, rel_path, str(delta_path), mod_size),
                )
                db.connection.execute(
                    "INSERT OR IGNORE INTO mod_vanilla_sizes "
                    "(mod_id, file_path, vanilla_size) VALUES (?, ?, ?)",
                    (mod_id, rel_path, van_size),
                )
                result.changed_files.append({
                    "file_path": rel_path,
                    "delta_path": str(delta_path),
                    "byte_ranges": [(0, mod_size)],
                })
                logger.info("Fast-track import: %s (%.1f MB, different size)",
                            rel_path, mod_size / 1048576)
                continue

            # ── Streaming sparse delta for large same-size files ─────
            # For files >10MB with same size, generate SPRS delta by streaming
            # both files in 1MB chunks. Never loads the full files into memory.
            # This handles 912MB PAZ files in ~2 seconds with ~2MB RAM.
            if mod_size > FAST_TRACK_THRESHOLD and mod_size == van_size:
                _iter_branch = "streaming_sparse"
                import struct
                from cdumm.engine.delta_engine import SPARSE_MAGIC

                # Use Rust-native byte-diff scanner when available — replaces
                # a Python byte-by-byte loop that ran 1M iterations per 1MB
                # chunk. Measured 34x faster on 10MB with scattered diffs.
                try:
                    from cdumm_native import find_sparse_diffs as _native_diffs
                except ImportError:
                    _native_diffs = None

                CHUNK = 1024 * 1024
                patches: list[tuple[int, bytes]] = []
                identical = True

                with open(vanilla_source, "rb") as fv, open(extracted_path, "rb") as fm:
                    offset = 0
                    while True:
                        cv = fv.read(CHUNK)
                        cm = fm.read(CHUNK)
                        if not cv:
                            break
                        if cv != cm:
                            identical = False
                            if _native_diffs is not None:
                                patches.extend(_native_diffs(cv, cm, offset))
                            else:
                                # Python fallback — byte-by-byte scan.
                                in_diff = False
                                diff_start = 0
                                for i in range(len(cv)):
                                    if cv[i] != cm[i]:
                                        if not in_diff:
                                            diff_start = offset + i
                                            in_diff = True
                                    else:
                                        if in_diff:
                                            patches.append((diff_start, cm[diff_start - offset:i]))
                                            in_diff = False
                                if in_diff:
                                    patches.append((diff_start, cm[diff_start - offset:]))
                        offset += len(cv)

                if identical:
                    logger.debug("File %s identical to vanilla, skipping", rel_path)
                    continue

                # Build SPRS delta
                buf = bytearray(SPARSE_MAGIC)
                buf += struct.pack("<I", len(patches))
                for off, data in patches:
                    buf += struct.pack("<QI", off, len(data))
                    buf += data
                delta_bytes = bytes(buf)

                safe_name = rel_path.replace("/", "_") + ".bsdiff"
                delta_path = deltas_dir / str(mod_id) / safe_name
                save_delta(delta_bytes, delta_path)

                import hashlib
                # Use streaming to get byte ranges without re-reading
                for off, data in patches:
                    bs, be = off, off + len(data)
                    db.connection.execute(
                        "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
                        "byte_start, byte_end, is_new) VALUES (?, ?, ?, ?, ?, 0)",
                        (mod_id, rel_path, str(delta_path), bs, be),
                    )

                db.connection.execute(
                    "INSERT OR IGNORE INTO mod_vanilla_sizes "
                    "(mod_id, file_path, vanilla_size) VALUES (?, ?, ?)",
                    (mod_id, rel_path, van_size),
                )
                result.changed_files.append({
                    "file_path": rel_path,
                    "delta_path": str(delta_path),
                    "byte_ranges": [(off, off + len(d)) for off, d in patches],
                })
                logger.info("Streaming delta: %s (%.1f MB, %d patches, %d bytes changed)",
                            rel_path, mod_size / 1048576, len(patches),
                            sum(len(d) for _, d in patches))
                continue

            # ── Standard delta path for small files (<10MB) ───────────
            _iter_branch = "small_bsdiff"
            vanilla_bytes = vanilla_source.read_bytes()
            modified_bytes = extracted_path.read_bytes()

            # Auto-fix PAMT CRC if it's wrong (common mod authoring mistake)
            if rel_path.endswith(".pamt") and len(modified_bytes) >= 12:
                modified_bytes = _verify_and_fix_pamt_crc(modified_bytes, rel_path)

            if vanilla_bytes == modified_bytes:
                logger.debug("File %s is identical to vanilla, skipping", rel_path)
                continue

            # Generate delta
            delta_bytes = generate_delta(vanilla_bytes, modified_bytes)

            # Get byte ranges
            byte_ranges = get_changed_byte_ranges(vanilla_bytes, modified_bytes)

            # Save delta to disk
            safe_name = rel_path.replace("/", "_") + ".bsdiff"
            delta_path = deltas_dir / str(mod_id) / safe_name
            save_delta(delta_bytes, delta_path)

            # Store each byte range with a hash of the vanilla bytes at that range.
            import hashlib
            for byte_start, byte_end in byte_ranges:
                vanilla_chunk = vanilla_bytes[byte_start:byte_end]
                vh = hashlib.sha256(vanilla_chunk).hexdigest()[:16]
                db.connection.execute(
                    "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, byte_end, is_new, vanilla_hash) "
                    "VALUES (?, ?, ?, ?, ?, 0, ?)",
                    (mod_id, rel_path, str(delta_path), byte_start, byte_end, vh),
                )

            db.connection.execute(
                "INSERT OR IGNORE INTO mod_vanilla_sizes (mod_id, file_path, vanilla_size) "
                "VALUES (?, ?, ?)",
                (mod_id, rel_path, len(vanilla_bytes)),
            )

            result.changed_files.append({
                "file_path": rel_path,
                "delta_path": str(delta_path),
                "byte_ranges": byte_ranges,
            })
        except Exception as e:
            logger.error("Failed to process %s: %s", rel_path, e, exc_info=True)
            result.error = f"Failed to process {rel_path}: {e}"
            return result
        finally:
            _fp_sub_timings[_iter_branch] = (
                _fp_sub_timings.get(_iter_branch, 0.0)
                + (time.perf_counter() - _iter_start)
            )

    _stage("file_processing_loop")
    # Per-branch breakdown of file_processing_loop — emits only when
    # the stage was >=1s and any single branch took >=200ms, so fast
    # mods don't bloat the log. Sorted by cost descending.
    _fp_total = sum(_fp_sub_timings.values())
    if _fp_total >= 1.0:
        import sys as _fp_sys
        _hot = sorted(_fp_sub_timings.items(), key=lambda kv: kv[1], reverse=True)
        _parts = " ".join(
            f"{n}={int(dt * 1000)}ms" for n, dt in _hot if dt >= 0.2
        )
        if _parts:
            print(
                f"[FILE-PROC-TIMING] {mod_name}: total={int(_fp_total * 1000)}ms "
                f"{_parts}",
                file=_fp_sys.stderr,
            )
    # Clean up PAMT byte-range deltas that were created before the corresponding
    # PAZ was decomposed into ENTR deltas. PAMT files sort before PAZ files
    # alphabetically (0.pamt before 4.paz), so the PAMT delta may already exist
    # by the time ENTR decomposition adds it to _paz_entr_handled.
    for handled_path in _paz_entr_handled:
        if handled_path.endswith(".pamt"):
            cursor = db.connection.execute(
                "SELECT COUNT(*) FROM mod_deltas WHERE mod_id = ? AND file_path = ? "
                "AND entry_path IS NULL",
                (mod_id, handled_path))
            count = cursor.fetchone()[0]
            if count > 0:
                db.connection.execute(
                    "DELETE FROM mod_deltas WHERE mod_id = ? AND file_path = ? "
                    "AND entry_path IS NULL",
                    (mod_id, handled_path))
                logger.info("Cleaned up %d PAMT byte-range deltas for %s "
                            "(handled by ENTR import)", count, handled_path)
                # Remove from changed_files too
                result.changed_files = [
                    cf for cf in result.changed_files
                    if cf.get("file_path") != handled_path or cf.get("entry_path")]

    db.connection.commit()
    _stage("delta_save_and_db")
    logger.info("Imported mod '%s': %d files changed", mod_name, len(result.changed_files))
    # Per-stage timing breakdown via stderr — surfaces in batch mode
    # so the GUI's _on_stderr handler logs it. Only emit when total
    # mod time was >= 1s (sub-second imports don't need diagnosis).
    _total_ms = sum(ms for _, ms in _stage_log)
    if _total_ms >= 1000:
        breakdown = " ".join(f"{n}={ms:.0f}ms" for n, ms in _stage_log)
        import sys as _s
        print(f"[STAGE-TIMING] {mod_name}: total={_total_ms:.0f}ms {breakdown}",
              file=_s.stderr, flush=True)
    return result


def _process_sandbox_diff(
    sandbox_dir: Path,
    game_dir: Path,
    db: Database,
    snapshot: SnapshotManager,
    deltas_dir: Path,
    mod_name: str,
) -> ModImportResult:
    """Diff sandbox output against vanilla game files and create deltas."""
    result = ModImportResult(mod_name)

    # Store mod in database
    priority = _next_priority(db)
    cursor = db.connection.execute(
        "INSERT INTO mods (name, mod_type, priority) VALUES (?, ?, ?)",
        (prettify_mod_name(mod_name), "paz", priority),
    )
    mod_id = cursor.lastrowid

    # Walk sandbox and compare each file against vanilla
    for f in sandbox_dir.rglob("*"):
        if not f.is_file():
            continue
        # Skip the script itself
        if f.suffix.lower() in (".bat", ".py") and f.parent == sandbox_dir:
            continue

        rel = f.relative_to(sandbox_dir)
        rel_posix = rel.as_posix()

        # Check if this is a known game file
        if snapshot.get_file_hash(rel_posix) is None:
            continue

        vanilla_path = game_dir / str(rel)
        if not vanilla_path.exists():
            continue

        vanilla_bytes = vanilla_path.read_bytes()
        modified_bytes = f.read_bytes()

        if vanilla_bytes == modified_bytes:
            continue

        delta_bytes = generate_delta(vanilla_bytes, modified_bytes)
        byte_ranges = get_changed_byte_ranges(vanilla_bytes, modified_bytes)

        safe_name = rel_posix.replace("/", "_") + ".bsdiff"
        delta_path = deltas_dir / str(mod_id) / safe_name
        save_delta(delta_bytes, delta_path)

        for byte_start, byte_end in byte_ranges:
            db.connection.execute(
                "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, byte_end) "
                "VALUES (?, ?, ?, ?, ?)",
                (mod_id, rel_posix, str(delta_path), byte_start, byte_end),
            )

        result.changed_files.append({
            "file_path": rel_posix,
            "delta_path": str(delta_path),
            "byte_ranges": byte_ranges,
        })

    db.connection.commit()
    logger.info("Script import '%s': %d files changed", mod_name, len(result.changed_files))
    return result


def import_script_live(
    script_path: Path, game_dir: Path, db: Database, snapshot: SnapshotManager, deltas_dir: Path
) -> ModImportResult:
    """Run a mod script against the real game files, then capture changes.

    Opens a visible cmd/python window so the user can interact with the script
    (e.g., pick options from a menu). After the script finishes, diffs game files
    against the vanilla snapshot and stores the changes as a managed mod.
    """
    mod_name = script_path.stem
    result = ModImportResult(mod_name)

    suffix = script_path.suffix.lower()
    if suffix == ".bat":
        cmd = ["cmd", "/c", f'"{script_path}" & pause']
    elif suffix == ".py":
        cmd = ["py", "-3", str(script_path)]
    else:
        result.error = f"Unsupported script type: {suffix}"
        return result

    vanilla_dir = deltas_dir.parent / "vanilla"
    vanilla_dir.mkdir(parents=True, exist_ok=True)
    from cdumm.engine.snapshot_manager import hash_file as _hash_file

    # Figure out which game files the script might touch by reading its source
    targeted_files = _detect_script_targets(script_path, game_dir)
    logger.info("Script likely targets: %s", targeted_files if targeted_files else "unknown")

    # Back up targeted files BEFORE the script modifies them
    if targeted_files:
        for rel_path in targeted_files:
            _ensure_vanilla_backup(game_dir, vanilla_dir, rel_path)
    else:
        # Can't determine targets — back up all PAMT and PAPGT (small files)
        for dir_name in [f"{i:04d}" for i in range(33)]:
            pamt = f"{dir_name}/0.pamt"
            if (game_dir / dir_name / "0.pamt").exists():
                _ensure_vanilla_backup(game_dir, vanilla_dir, pamt)
        _ensure_vanilla_backup(game_dir, vanilla_dir, "meta/0.papgt")

    # Record pre-script hashes ONLY for files that have backups
    pre_hashes: dict[str, str] = {}
    for f in vanilla_dir.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(vanilla_dir).as_posix()
        game_file = game_dir / rel.replace("/", "\\")
        if game_file.exists():
            h, _ = _hash_file(game_file)
            pre_hashes[rel] = h

    logger.info("Running script live: %s", script_path)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(script_path.parent),
            shell=True,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        proc.wait()
        logger.info("Script finished with exit code: %d", proc.returncode)
    except Exception as e:
        result.error = f"Failed to run script: {e}"
        return result

    # Scan for changes — compare current hashes against pre-script state
    logger.info("Scanning for changes after script...")
    changed_files: list[str] = []
    for rel_path, old_hash in pre_hashes.items():
        abs_path = game_dir / rel_path.replace("/", "\\")
        if not abs_path.exists():
            continue
        new_hash, _ = _hash_file(abs_path)
        if new_hash != old_hash:
            changed_files.append(rel_path)
            # Back up the vanilla version (from pre-script state) if needed
            _ensure_vanilla_backup(game_dir, vanilla_dir, rel_path)

    if not changed_files:
        result.error = "Script ran but no game file changes were detected."
        return result

    logger.info("Script changed %d files: %s", len(changed_files), changed_files)

    # Generate deltas for changed files
    priority = _next_priority(db)
    cursor = db.connection.execute(
        "INSERT INTO mods (name, mod_type, priority) VALUES (?, ?, ?)",
        (prettify_mod_name(mod_name), "paz", priority))
    mod_id = cursor.lastrowid

    for rel_path in changed_files:
        vanilla_path = vanilla_dir / rel_path.replace("/", "\\")
        current_path = game_dir / rel_path.replace("/", "\\")

        if not vanilla_path.exists():
            logger.warning("No vanilla backup for %s, skipping", rel_path)
            continue

        vanilla_bytes = vanilla_path.read_bytes()
        modified_bytes = current_path.read_bytes()

        delta_bytes = generate_delta(vanilla_bytes, modified_bytes)
        byte_ranges = get_changed_byte_ranges(vanilla_bytes, modified_bytes)

        safe_name = rel_path.replace("/", "_") + ".bsdiff"
        delta_path = deltas_dir / str(mod_id) / safe_name
        save_delta(delta_bytes, delta_path)

        for byte_start, byte_end in byte_ranges:
            db.connection.execute(
                "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, byte_end) "
                "VALUES (?, ?, ?, ?, ?)",
                (mod_id, rel_path, str(delta_path), byte_start, byte_end),
            )

        result.changed_files.append({
            "file_path": rel_path,
            "delta_path": str(delta_path),
            "byte_ranges": byte_ranges,
        })

    db.connection.commit()
    logger.info("Live script import '%s': %d files changed", mod_name, len(result.changed_files))
    return result


def _detect_script_targets(script_path: Path, game_dir: Path) -> list[str]:
    """Read a script's source code to detect which game files it targets.

    Looks for PAZ directory patterns (0000-0099) and file references,
    including os.path.join style references like ("0009") and bare
    directory name strings.
    """
    import re
    targets: list[str] = []

    try:
        content = script_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return targets

    dirs_found: set[str] = set()

    # Look for PAZ directory references like "0008\0.paz" or "0008/0.paz"
    for match in re.finditer(r'(\d{4})[/\\]+(\d+\.(?:paz|pamt))', content, re.IGNORECASE):
        dir_name = match.group(1)
        file_name = match.group(2)
        rel = f"{dir_name}/{file_name}"
        if (game_dir / dir_name / file_name).exists() and rel not in targets:
            targets.append(rel)
            dirs_found.add(dir_name)

    # Look for bare PAZ directory references like "0009" in quotes
    # (catches os.path.join(game_dir, "0009") style)
    for match in re.finditer(r'["\'](\d{4})["\']', content):
        dir_name = match.group(1)
        dir_path = game_dir / dir_name
        if dir_path.exists() and dir_path.is_dir():
            dirs_found.add(dir_name)

    # Look for meta/0.papgt references
    if re.search(r'meta[/\\]+0\.papgt', content, re.IGNORECASE):
        if (game_dir / "meta" / "0.papgt").exists():
            targets.append("meta/0.papgt")

    # For every directory found, include all PAZ and PAMT files
    for d in sorted(dirs_found):
        dir_path = game_dir / d
        if not dir_path.exists():
            continue
        for f in sorted(dir_path.iterdir()):
            if f.is_file() and f.suffix.lower() in ('.paz', '.pamt'):
                rel = f"{d}/{f.name}"
                if rel not in targets:
                    targets.append(rel)

    if dirs_found and "meta/0.papgt" not in targets:
        if (game_dir / "meta" / "0.papgt").exists():
            targets.append("meta/0.papgt")

    return targets


def _ensure_vanilla_backup(game_dir: Path, vanilla_dir: Path, rel_path: str) -> None:
    """Back up a single game file if not already backed up.

    Always a real copy — hard links are unsafe because script mods can
    modify the game file directly, which would corrupt a hard-linked backup.
    """
    src = game_dir / rel_path.replace("/", "\\")
    dst = vanilla_dir / rel_path.replace("/", "\\")
    if not dst.exists() and src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        logger.debug("Backed up vanilla: %s", rel_path)


def import_from_game_scan(
    mod_name: str, game_dir: Path, db: Database, snapshot: SnapshotManager, deltas_dir: Path
) -> ModImportResult:
    """Import a mod by scanning current game files against the vanilla snapshot.

    Use this after the user has manually run a script/installer that modified
    game files directly. Detects all changes and captures them as deltas.
    """
    result = ModImportResult(mod_name)
    changes = snapshot.detect_changes(game_dir)

    if not changes:
        result.error = "No changes detected. Game files match the vanilla snapshot."
        return result

    # Only process modified files (not deleted)
    modified = [(path, change) for path, change in changes if change == "modified"]
    if not modified:
        result.error = "No modified files found (some files may have been deleted)."
        return result

    priority = _next_priority(db)
    cursor = db.connection.execute(
        "INSERT INTO mods (name, mod_type, priority) VALUES (?, ?, ?)",
        (prettify_mod_name(mod_name), "paz", priority),
    )
    mod_id = cursor.lastrowid

    for rel_path, _ in modified:
        vanilla_path = game_dir / rel_path.replace("/", "\\")
        # We need the vanilla version — check the vanilla backup dir first
        vanilla_backup = deltas_dir.parent / "vanilla" / rel_path.replace("/", "\\")

        if vanilla_backup.exists():
            vanilla_bytes = vanilla_backup.read_bytes()
        else:
            # No backup exists — we can't diff without the original
            # Store the snapshot hash so we know what changed
            logger.warning("No vanilla backup for %s, skipping delta generation", rel_path)
            continue

        modified_bytes = vanilla_path.read_bytes()
        if vanilla_bytes == modified_bytes:
            continue

        delta_bytes = generate_delta(vanilla_bytes, modified_bytes)
        byte_ranges = get_changed_byte_ranges(vanilla_bytes, modified_bytes)

        safe_name = rel_path.replace("/", "_") + ".bsdiff"
        delta_path = deltas_dir / str(mod_id) / safe_name
        save_delta(delta_bytes, delta_path)

        for byte_start, byte_end in byte_ranges:
            db.connection.execute(
                "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, byte_end) "
                "VALUES (?, ?, ?, ?, ?)",
                (mod_id, rel_path, str(delta_path), byte_start, byte_end),
            )

        result.changed_files.append({
            "file_path": rel_path,
            "delta_path": str(delta_path),
            "byte_ranges": byte_ranges,
        })

    db.connection.commit()
    logger.info("Game scan import '%s': %d files changed", mod_name, len(result.changed_files))
    return result
