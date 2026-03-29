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
    """Detect import format: 'zip', 'folder', 'script', 'json_patch', 'bsdiff', or 'unknown'."""
    if path.is_dir():
        return "folder"
    suffix = path.suffix.lower()
    if suffix == ".zip":
        return "zip"
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
    return "unknown"


import json
import re

from cdumm.engine.crimson_browser_handler import detect_crimson_browser, convert_to_paz_mod
from cdumm.engine.json_patch_handler import detect_json_patch, convert_json_patch_to_paz

# Pattern for valid game file paths: NNNN/N.paz, NNNN/N.pamt, meta/0.papgt
_GAME_FILE_RE = re.compile(r'^(\d{4}/\d+\.(?:paz|pamt)|meta/\d+\.papgt)$')


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

        # A standalone mod has completely different content (different PAMT size
        # or drastically different PAZ size). Mods that are modified copies of
        # vanilla (same PAMT size, same PAZ size) are regular patches.
        if mod_pamt_size == vanilla_pamt_size and mod_paz_size == vanilla_paz_size:
            is_standalone = False
            logger.info("Modified vanilla: %s (same PAMT+PAZ size, treating as patch)",
                         dir_name)
        else:
            is_standalone = True
            logger.info("Standalone: %s ships own PAMT+PAZ (mod_pamt=%d vs %d, mod_paz=%d vs %d)",
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

    # Check for JSON byte-patch format in folder
    jp_data = detect_json_patch(folder_path)
    if jp_data is not None:
        with tempfile.TemporaryDirectory() as jp_tmp:
            jp_work = Path(jp_tmp) / "_jp_converted"
            converted = convert_json_patch_to_paz(jp_data, game_dir, jp_work)
            if converted is not None:
                jp_name = jp_data.get("name", mod_name)
                jp_modinfo = {
                    "name": jp_data.get("name"),
                    "version": jp_data.get("version"),
                    "author": jp_data.get("author"),
                    "description": jp_data.get("description"),
                }
                return _process_extracted_files(
                    converted, game_dir, db, snapshot, deltas_dir, jp_name,
                    existing_mod_id=existing_mod_id, modinfo=jp_modinfo)

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

    The patch filename should indicate which file it targets (e.g., 0008_0.paz.bsdiff).
    """
    mod_name = patch_path.stem
    result = ModImportResult(mod_name)

    # For now, store the delta directly — the user needs to have named it correctly
    # or we detect the target from metadata
    delta_bytes = patch_path.read_bytes()

    # Store mod in database
    priority = _next_priority(db)
    cursor = db.connection.execute(
        "INSERT INTO mods (name, mod_type, priority, source_path) VALUES (?, ?, ?, ?)",
        (mod_name, "paz", priority, str(patch_path)),
    )
    mod_id = cursor.lastrowid

    # Store the delta
    delta_dest = deltas_dir / str(mod_id) / patch_path.name
    save_delta(delta_bytes, delta_dest)

    db.connection.execute(
        "INSERT INTO mod_deltas (mod_id, file_path, delta_path) VALUES (?, ?, ?)",
        (mod_id, patch_path.stem.replace("_", "/"), str(delta_dest)),
    )
    db.connection.commit()

    result.changed_files.append({
        "file_path": patch_path.stem.replace("_", "/"),
        "delta_path": str(delta_dest),
    })
    return result


def import_from_json_patch(
    json_path: Path, game_dir: Path, db: Database, snapshot: SnapshotManager, deltas_dir: Path,
    existing_mod_id: int | None = None,
) -> ModImportResult:
    """Import a mod from a JSON byte-patch file.

    The JSON contains patches against specific game files (offsets into
    decompressed content). The handler extracts the target file from PAZ,
    applies patches, repacks, and generates deltas.
    """
    patch_data = detect_json_patch(json_path if json_path.is_file() else json_path)
    if patch_data is None:
        result = ModImportResult(json_path.stem)
        result.error = "Not a valid JSON patch mod."
        return result

    mod_name = patch_data.get("name", json_path.stem)
    result = ModImportResult(mod_name)

    with tempfile.TemporaryDirectory() as tmp:
        work_dir = Path(tmp) / "_patched"
        converted = convert_json_patch_to_paz(patch_data, game_dir, work_dir)
        if converted is None:
            result.error = "Failed to apply JSON patches to game files."
            return result

        modinfo = {
            "name": patch_data.get("name"),
            "version": patch_data.get("version"),
            "author": patch_data.get("author"),
            "description": patch_data.get("description"),
        }
        if modinfo.get("name"):
            mod_name = modinfo["name"]

        result = _process_extracted_files(
            converted, game_dir, db, snapshot, deltas_dir, mod_name,
            existing_mod_id=existing_mod_id, modinfo=modinfo)

    return result


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

    matches = _match_game_files(extracted_dir, game_dir, snapshot)
    if not matches:
        result.error = "No recognized game files found in this mod."
        return result

    new_count = sum(1 for _, _, is_new in matches if is_new)
    mod_count = sum(1 for _, _, is_new in matches if not is_new)
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

    for rel_path, extracted_path, is_new in matches:
        try:
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

            vanilla_path = game_dir / rel_path.replace("/", "\\")
            if not vanilla_path.exists():
                logger.warning("Vanilla file not found for %s, skipping", rel_path)
                continue

            vanilla_bytes = vanilla_path.read_bytes()
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

            # Store each byte range as a separate mod_delta entry
            for byte_start, byte_end in byte_ranges:
                db.connection.execute(
                    "INSERT INTO mod_deltas (mod_id, file_path, delta_path, byte_start, byte_end, is_new) "
                    "VALUES (?, ?, ?, ?, ?, 0)",
                    (mod_id, rel_path, str(delta_path), byte_start, byte_end),
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
