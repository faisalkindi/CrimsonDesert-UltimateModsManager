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
    """Detect import format: 'zip', 'folder', 'script', 'bsdiff', or 'unknown'."""
    if path.is_dir():
        return "folder"
    suffix = path.suffix.lower()
    if suffix == ".zip":
        return "zip"
    if suffix in (".bat", ".py"):
        return "script"
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


def _match_game_files(
    extracted_dir: Path, game_dir: Path, snapshot: SnapshotManager
) -> list[tuple[str, Path, bool]]:
    """Find files in extracted_dir that match known game file paths.

    Returns list of (relative_posix_path, absolute_extracted_path, is_new).
    is_new=True means the file doesn't exist in vanilla (mod adds it).
    """
    matches: list[tuple[str, Path, bool]] = []

    for f in extracted_dir.rglob("*"):
        if not f.is_file():
            continue

        parts = f.relative_to(extracted_dir).parts
        matched = False

        # First try exact match against snapshot (existing vanilla files)
        for i in range(len(parts)):
            candidate = "/".join(parts[i:])
            if snapshot.get_file_hash(candidate) is not None:
                matches.append((candidate, f, False))
                matched = True
                break

        if matched:
            continue

        # No snapshot match — check if it looks like a game file by pattern
        # This catches PAZ files not in the snapshot (e.g., from a game update
        # after the snapshot was taken, or truly new mod-added files)
        for i in range(len(parts)):
            candidate = "/".join(parts[i:])
            if _GAME_FILE_RE.match(candidate):
                # Check if the file exists in the game directory — if so, it's
                # a vanilla file missing from the snapshot, not a new file
                game_file = game_dir / candidate.replace("/", "\\")
                is_new = not game_file.exists()
                matches.append((candidate, f, is_new))
                break

    return matches


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

        # Check if zip contains a script instead of game files
        scripts = list(tmp_path.glob("*.bat")) + list(tmp_path.glob("*.py"))
        if scripts and not _match_game_files(tmp_path, game_dir, snapshot):
            result.error = (
                "This zip contains a script mod. "
                "It should be handled by the script mod flow, not the worker."
            )
            return result

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

    # Check if folder contains scripts instead of game files
    scripts = list(folder_path.glob("*.bat")) + list(folder_path.glob("*.py"))
    if scripts and not _match_game_files(folder_path, game_dir, snapshot):
        result = ModImportResult(folder_path.name)
        result.error = "This folder contains a script mod. It should be handled by the script mod flow."
        return result

    # Read mod metadata from modinfo.json if present
    modinfo = _read_modinfo(folder_path)
    if modinfo and modinfo.get("name"):
        mod_name = modinfo["name"]

    return _process_extracted_files(
        folder_path, game_dir, db, snapshot, deltas_dir, mod_name,
        existing_mod_id=existing_mod_id, modinfo=modinfo)


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
