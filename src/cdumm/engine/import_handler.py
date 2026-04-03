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


class ModImportResult:
    """Result of importing a mod."""

    def __init__(self, name: str, mod_type: str = "paz") -> None:
        self.name = name
        self.mod_type = mod_type
        self.changed_files: list[dict] = []  # [{file_path, delta_path, byte_start, byte_end}]
        self.error: str | None = None
        self.health_issues: list = []  # list[HealthIssue] from mod_health_check


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
    # Check if it's a rar
    if suffix == ".rar":
        return "unknown"  # TODO: add rar support
    return "unknown"


import json
import re

from cdumm.engine.crimson_browser_handler import detect_crimson_browser, convert_to_paz_mod
from cdumm.engine.json_patch_handler import detect_json_patch, convert_json_patch_to_paz
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
    for candidate in [extracted_dir / "modinfo.json",
                      *extracted_dir.glob("*/modinfo.json")]:
        if candidate.exists():
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
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
    import os

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

    try:
        van_entries = parse_pamt(str(vanilla_pamt), paz_dir=str(
            (game_dir / "CDMods" / "vanilla" / dir_name)
            if (game_dir / "CDMods" / "vanilla" / dir_name / "0.pamt").exists()
            else (game_dir / dir_name)))
        mod_entries = parse_pamt(str(mod_pamt), paz_dir=str(extracted_dir / dir_name)
                                 if mod_pamt != vanilla_pamt
                                 else str(game_dir / dir_name))
    except Exception as e:
        logger.debug("Failed to parse PAMTs for %s: %s", rel_path, e)
        return False

    # Filter to entries in this specific PAZ file
    van_by_path = {e.path: e for e in van_entries if e.paz_index == paz_index}
    mod_by_path = {e.path: e for e in mod_entries if e.paz_index == paz_index}

    if not van_by_path or not mod_by_path:
        logger.debug("No entries for PAZ index %d in %s", paz_index, rel_path)
        return False

    def _extract_entry(entry, paz_path):
        """Extract and decompress a single entry from a PAZ file."""
        with open(paz_path, "rb") as f:
            f.seek(entry.offset)
            raw = f.read(entry.comp_size)
        if entry.compressed and entry.compression_type == 2:
            try:
                return lz4_decompress(raw, entry.orig_size)
            except Exception:
                basename = os.path.basename(entry.path)
                decrypted = decrypt(raw, basename)
                return lz4_decompress(decrypted, entry.orig_size)
        if entry.encrypted:
            return decrypt(raw, os.path.basename(entry.path))
        return raw

    changed = 0
    paz_file_path = f"{dir_name}/{paz_index}.paz"

    # Compare entries between mod and vanilla
    for entry_path, mod_entry in mod_by_path.items():
        van_entry = van_by_path.get(entry_path)
        if van_entry is None:
            continue  # New entry — handled separately

        try:
            # Quick check: if comp_size and offset are identical, skip
            if (mod_entry.offset == van_entry.offset
                    and mod_entry.comp_size == van_entry.comp_size):
                # Same position and size — likely unchanged, but verify
                # by reading raw bytes (fast, no decompression)
                with open(mod_paz_path, "rb") as f:
                    f.seek(mod_entry.offset)
                    mod_raw = f.read(mod_entry.comp_size)
                with open(vanilla_paz_path, "rb") as f:
                    f.seek(van_entry.offset)
                    van_raw = f.read(van_entry.comp_size)
                if mod_raw == van_raw:
                    continue

            # Entry differs — decompress both and store mod's content
            van_content = _extract_entry(van_entry, vanilla_paz_path)
            mod_content = _extract_entry(mod_entry, mod_paz_path)

            if van_content == mod_content:
                continue  # Decompressed content is the same

            # Detect encryption: try decompressing the vanilla entry.
            # If decompress fails, the entry is encrypted.
            encrypted = van_entry.encrypted
            if not encrypted and van_entry.compressed and van_entry.compression_type == 2:
                try:
                    with open(vanilla_paz_path, "rb") as f:
                        f.seek(van_entry.offset)
                        raw = f.read(van_entry.comp_size)
                    lz4_decompress(raw, van_entry.orig_size)
                except Exception:
                    encrypted = True

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

            safe_name = van_entry.path.replace("/", "_") + ".entr"
            delta_path = deltas_dir / str(mod_id) / safe_name
            save_entry_delta(mod_content, metadata, delta_path)

            db.connection.execute(
                "INSERT INTO mod_deltas (mod_id, file_path, delta_path, "
                "byte_start, byte_end, entry_path) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (mod_id, paz_file_path, str(delta_path),
                 van_entry.offset, van_entry.offset + van_entry.comp_size,
                 van_entry.path))

            result.changed_files.append({
                "file_path": paz_file_path,
                "entry_path": van_entry.path,
                "delta_path": str(delta_path),
            })
            changed += 1

        except Exception as e:
            logger.warning("Entry comparison failed for %s: %s", entry_path, e)
            continue

    if changed == 0:
        logger.debug("No changed entries in %s, falling back to byte-level", rel_path)
        return False

    db.connection.commit()
    logger.info("Entry-level PAZ import: %s — %d/%d entries changed",
                rel_path, changed, len(mod_by_path))
    return True


def detect_loose_file_mod(path: Path) -> dict | None:
    """Detect mods that ship loose game files with a mod.json metadata file.

    Format: mod.json (with "modinfo" key) + files/ directory containing
    replacement files at their PAMT paths (e.g., files/0004/sound/.../file.wem).

    Returns a CB-compatible manifest dict for convert_to_paz_mod, or None.
    """
    for candidate in [path, *[d for d in path.iterdir() if d.is_dir()]]:
        mod_json = candidate / "mod.json"
        files_dir = candidate / "files"
        if not mod_json.exists() or not files_dir.exists():
            continue
        try:
            with open(mod_json, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "modinfo" in data:
                # Translate to CB-compatible manifest
                modinfo = data["modinfo"]
                manifest = {
                    "format": "loose_file_mod",
                    "id": modinfo.get("title", candidate.name),
                    "files_dir": "files",
                    "_manifest_path": mod_json,
                    "_base_dir": candidate,
                    "_modinfo": modinfo,
                }
                logger.info("Detected loose file mod: %s", manifest["id"])
                return manifest
        except Exception:
            continue
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


_assigned_dirs: set[int] = set()  # track dirs assigned in current session


def _next_paz_directory(game_dir: Path) -> str:
    """Find the next available PAZ directory number (0036+)."""
    existing = set()
    for d in game_dir.iterdir():
        if d.is_dir() and d.name.isdigit() and len(d.name) == 4:
            existing.add(int(d.name))
    existing |= _assigned_dirs
    # Start from 36 (base game uses 0000-0035)
    for n in range(36, 200):
        if n not in existing:
            _assigned_dirs.add(n)
            return f"{n:04d}"
    return "0100"  # fallback


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
            return _process_extracted_files(
                converted, game_dir, db, snapshot, deltas_dir, cb_name,
                existing_mod_id=existing_mod_id, modinfo=modinfo)

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
            }
            return _process_extracted_files(
                converted, game_dir, db, snapshot, deltas_dir, lfm_name,
                existing_mod_id=existing_mod_id, modinfo=lfm_modinfo)

    # Check for JSON byte-patch format — use ENTR deltas for proper composition
    jp_data = detect_json_patch(tmp_path)
    if jp_data is not None:
        from cdumm.engine.json_patch_handler import import_json_as_entr
        jp_name = jp_data.get("name", mod_name)
        jp_modinfo = {
            "name": jp_data.get("name"), "version": jp_data.get("version"),
            "author": jp_data.get("author"), "description": jp_data.get("description"),
        }
        if jp_modinfo.get("name"):
            jp_name = jp_modinfo["name"]
        entr_result = import_json_as_entr(
            jp_data, game_dir, db, deltas_dir, jp_name,
            existing_mod_id=existing_mod_id, modinfo=jp_modinfo)
        if entr_result is not None:
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

    # Check for scripts
    scripts = list(tmp_path.glob("*.bat")) + list(tmp_path.glob("*.py"))
    if scripts and not _match_game_files(tmp_path, game_dir, snapshot):
        result.error = "This archive contains a script mod. It should be handled by the script mod flow."
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

    return _process_extracted_files(
        tmp_path, game_dir, db, snapshot, deltas_dir, mod_name,
        existing_mod_id=existing_mod_id, modinfo=modinfo)


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
                return result

        # Check for JSON byte-patch format inside the zip
        jp_data = detect_json_patch(tmp_path)
        if jp_data is not None:
            jp_work = Path(tmp) / "_jp_converted"
            converted = convert_json_patch_to_paz(jp_data, game_dir, jp_work)
            if converted is not None:
                has_files = any(converted.rglob("*")) if converted.exists() else False
                if not has_files:
                    result.error = (
                        "This mod's changes are already present in your game files. "
                        "Nothing to apply."
                    )
                    return result
                jp_name = jp_data.get("name", mod_name)
                jp_modinfo = {
                    "name": jp_data.get("name"),
                    "version": jp_data.get("version"),
                    "author": jp_data.get("author"),
                    "description": jp_data.get("description"),
                }
                result = _process_extracted_files(
                    converted, game_dir, db, snapshot, deltas_dir, jp_name,
                    existing_mod_id=existing_mod_id, modinfo=jp_modinfo)
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

        # Check if zip contains a script instead of game files
        scripts = list(tmp_path.glob("*.bat")) + list(tmp_path.glob("*.py"))
        if scripts and not _match_game_files(tmp_path, game_dir, snapshot):
            result.error = (
                "This zip contains a script mod. "
                "It should be handled by the script mod flow, not the worker."
            )
            return result

        # Detect multi-variant zips
        variant = _find_best_variant(tmp_path)
        if variant:
            logger.info("Multi-variant zip, using: %s", variant.name)
            tmp_path = variant
            mod_name = f"{mod_name} ({variant.name})"

        # Read mod metadata from modinfo.json if present
        modinfo = _read_modinfo(tmp_path)
        if modinfo and modinfo.get("name"):
            mod_name = modinfo["name"]

        result = _process_extracted_files(
            tmp_path, game_dir, db, snapshot, deltas_dir, mod_name,
            existing_mod_id=existing_mod_id, modinfo=modinfo)

    return result


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
                return _process_extracted_files(
                    converted, game_dir, db, snapshot, deltas_dir, cb_name,
                    existing_mod_id=existing_mod_id, modinfo=modinfo)

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
                }
                return _process_extracted_files(
                    converted, game_dir, db, snapshot, deltas_dir, lfm_name,
                    existing_mod_id=existing_mod_id, modinfo=lfm_modinfo)

    # Check for JSON byte-patch format in folder — use ENTR deltas
    jp_data = detect_json_patch(folder_path)
    if jp_data is not None:
        from cdumm.engine.json_patch_handler import import_json_as_entr
        jp_name = jp_data.get("name", mod_name)
        jp_modinfo = {
            "name": jp_data.get("name"), "version": jp_data.get("version"),
            "author": jp_data.get("author"), "description": jp_data.get("description"),
        }
        if jp_modinfo.get("name"):
            jp_name = jp_modinfo["name"]
        entr_result = import_json_as_entr(
            jp_data, game_dir, db, deltas_dir, jp_name,
            existing_mod_id=existing_mod_id, modinfo=jp_modinfo)
        if entr_result is not None:
            result = ModImportResult(jp_name)
            result.changed_files = entr_result["changed_files"]
            if jp_data.get("patches"):
                _store_json_patches(db, result, jp_data, game_dir)
            return result

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
        result.error = "This folder contains a script mod. It should be handled by the script mod flow."
        return result

    # Detect variant folders: parent folder has multiple subdirectories each
    # containing their own 0.paz + 0.pamt (e.g., FatStacks2x/, FatStacks10x/).
    # Find the best single variant to import.
    variant = _find_best_variant(folder_path)
    if variant:
        logger.info("Multi-variant mod detected, using variant: %s", variant.name)
        folder_path = variant
        mod_name = f"{folder_path.parent.name} ({variant.name})"

    # Read mod metadata from modinfo.json if present
    modinfo = _read_modinfo(folder_path)
    if modinfo and modinfo.get("name"):
        mod_name = modinfo["name"]

    return _process_extracted_files(
        folder_path, game_dir, db, snapshot, deltas_dir, mod_name,
        existing_mod_id=existing_mod_id, modinfo=modinfo)


def _find_best_variant(folder_path: Path) -> Path | None:
    """Detect if a folder contains multiple mod variants.

    Returns the best variant subfolder, or None if not a multi-variant mod.
    A multi-variant mod has 2+ subdirectories each containing 0.paz + 0.pamt.
    """
    # Check direct children for variant directories
    variants: list[Path] = []
    for sub in folder_path.iterdir():
        if not sub.is_dir():
            continue
        # A variant has its own 0.paz+0.pamt (directly or inside a numbered subdir)
        has_paz = list(sub.rglob("0.paz"))
        has_pamt = list(sub.rglob("0.pamt"))
        if has_paz and has_pamt:
            variants.append(sub)

    if len(variants) < 2:
        return None  # not multi-variant

    # Multiple variants found. Pick the last one alphabetically (usually highest value).
    # e.g., FatStacks10x > FatStacks2x, or variant names sorted naturally
    variants.sort(key=lambda p: p.name)
    chosen = variants[-1]
    logger.info("Found %d variants: %s. Picking: %s",
                len(variants),
                [v.name for v in variants],
                chosen.name)
    return chosen


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
            proc = subprocess.run(
                cmd,
                cwd=str(sandbox_path),
                timeout=SCRIPT_TIMEOUT,
                capture_output=True,
                text=True,
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
        (mod_name, "paz", priority, str(patch_path)),
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

    Uses ENTR (entry-level) deltas so multiple JSON mods modifying
    different entries in the same PAZ compose correctly.
    """
    from cdumm.engine.json_patch_handler import import_json_as_entr

    patch_data = detect_json_patch(json_path if json_path.is_file() else json_path)
    if patch_data is None:
        result = ModImportResult(json_path.stem)
        result.error = "Not a valid JSON patch mod."
        return result

    mod_name = patch_data.get("name", json_path.stem)
    modinfo = {
        "name": patch_data.get("name"),
        "version": patch_data.get("version"),
        "author": patch_data.get("author"),
        "description": patch_data.get("description"),
    }
    if modinfo.get("name"):
        mod_name = modinfo["name"]

    entr_result = import_json_as_entr(
        patch_data, game_dir, db, deltas_dir, mod_name,
        existing_mod_id=existing_mod_id, modinfo=modinfo)

    if entr_result is None:
        result = ModImportResult(mod_name)
        result.error = "Failed to apply JSON patches to game files."
        return result

    if not entr_result["changed_files"]:
        result = ModImportResult(mod_name)
        result.error = (
            "This mod's changes are already present in your game files. "
            "Nothing to apply — the game may have been updated to include these changes."
        )
        return result

    result = ModImportResult(mod_name)
    result.changed_files = entr_result["changed_files"]

    # Store original JSON patch data for three-way merge support
    if patch_data.get("patches"):
        _store_json_patches(db, result, patch_data, game_dir)

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

        # Update all mod_deltas rows for this mod + PAZ file
        db.connection.execute(
            "UPDATE mod_deltas SET json_patches = ? "
            "WHERE mod_id = ? AND file_path LIKE ?",
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
    result = ModImportResult(mod_name)

    _emit_progress(2, f"Matching files for {mod_name}...")
    matches = _match_game_files(extracted_dir, game_dir, snapshot)
    if not matches:
        result.error = "No recognized game files found in this mod."
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

    if existing_mod_id is not None:
        mod_id = existing_mod_id
        # Update metadata if modinfo provided
        if modinfo:
            db.connection.execute(
                "UPDATE mods SET author=?, version=?, description=? WHERE id=?",
                (modinfo.get("author"), modinfo.get("version"), modinfo.get("description"), mod_id),
            )
    else:
        # Store mod in database
        priority = _next_priority(db)
        author = modinfo.get("author") if modinfo else None
        version = modinfo.get("version") if modinfo else None
        description = modinfo.get("description") if modinfo else None
        cursor = db.connection.execute(
            "INSERT INTO mods (name, mod_type, priority, author, version, description) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mod_name, "paz", priority, author, version, description),
        )
        mod_id = cursor.lastrowid

    # Archive mod source files for auto-reimport after game updates.
    # Copy the extracted files to CDMods/sources/<mod_id>/ so the mod
    # can be re-imported without the user providing the original files.
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

    total_matches = len(matches)
    _paz_entr_handled: set[str] = set()  # PAZ/PAMT files handled by entry-level import
    for match_idx, (rel_path, extracted_path, is_new) in enumerate(matches):
        pct = int((match_idx / max(total_matches, 1)) * 90) + 5
        size_mb = extracted_path.stat().st_size / 1048576
        _emit_progress(pct, f"Processing {rel_path} ({size_mb:.0f} MB)...")
        try:
            # Skip files already handled by entry-level PAZ decomposition
            if rel_path in _paz_entr_handled:
                logger.info("Skipping %s — handled by entry-level PAZ import", rel_path)
                continue

            if is_new:
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

            # ── Fast-track for different-size large files ─────────────
            # When the mod file is a different size from vanilla, it's a true
            # full replacement — store as FULL_COPY with streaming I/O.
            # Same-size files use the standard sparse delta path so multiple
            # mods can compose their changes at different byte ranges.
            FAST_TRACK_THRESHOLD = 10 * 1024 * 1024  # 10MB

            if mod_size > FAST_TRACK_THRESHOLD and mod_size != van_size:
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
                import struct
                from cdumm.engine.delta_engine import SPARSE_MAGIC

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
                            # Find exact diff ranges within this chunk
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

    db.connection.commit()
    logger.info("Imported mod '%s': %d files changed", mod_name, len(result.changed_files))
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
        (mod_name, "paz", priority),
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
        (mod_name, "paz", priority))
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
        (mod_name, "paz", priority),
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
