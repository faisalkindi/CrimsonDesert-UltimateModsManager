"""Headless worker subprocess for CDUMM.

Runs import/apply/revert/snapshot in a SEPARATE PROCESS so Python's GIL
cannot starve the GUI thread.  Communicates via JSON lines on stdout.

Protocol (each line is valid JSON):
    {"type": "progress", "pct": 50, "msg": "Processing..."}
    {"type": "done", ...}          — operation-specific result fields
    {"type": "error", "msg": "..."}
    {"type": "warning", "msg": "..."}   — non-fatal warnings (revert)
    {"type": "activity", "cat": "...", "msg": "...", "detail": "..."}
"""

import json
import logging
import sys
from pathlib import Path


def _emit(obj: dict) -> None:
    """Write a JSON line to stdout for the parent process to read."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


# ── Import ────────────────────────────────────────────────────────────

def _run_import(mod_path: str, game_dir: str, db_path: str,
                deltas_dir: str, existing_mod_id: str | None = None) -> None:
    from cdumm.storage.database import Database
    from cdumm.engine.snapshot_manager import SnapshotManager
    from cdumm.engine.import_handler import (
        detect_format, import_from_zip, import_from_7z, import_from_folder,
        import_from_json_patch, import_from_bsdiff, import_from_script,
        import_from_rar, set_import_progress_cb,
    )

    mod_path = Path(mod_path)
    game_dir = Path(game_dir)
    db_path = Path(db_path)
    deltas_dir = Path(deltas_dir)
    existing_id = int(existing_mod_id) if existing_mod_id else None

    db = Database(db_path)
    db.initialize()
    snapshot = SnapshotManager(db)

    fmt = detect_format(mod_path)
    _emit({"type": "progress", "pct": 0, "msg": f"Detected format: {fmt}"})
    set_import_progress_cb(lambda pct, msg: _emit({"type": "progress", "pct": pct, "msg": msg}))

    dispatch = {
        "zip": lambda: import_from_zip(mod_path, game_dir, db, snapshot, deltas_dir, existing_mod_id=existing_id),
        "7z": lambda: import_from_7z(mod_path, game_dir, db, snapshot, deltas_dir, existing_mod_id=existing_id),
        "rar": lambda: import_from_rar(mod_path, game_dir, db, snapshot, deltas_dir, existing_mod_id=existing_id),
        "folder": lambda: import_from_folder(mod_path, game_dir, db, snapshot, deltas_dir, existing_mod_id=existing_id),
        "script": lambda: import_from_script(mod_path, game_dir, db, snapshot, deltas_dir),
        "json_patch": lambda: import_from_json_patch(mod_path, game_dir, db, snapshot, deltas_dir, existing_mod_id=existing_id),
        "bsdiff": lambda: import_from_bsdiff(mod_path, game_dir, db, snapshot, deltas_dir),
    }

    handler = dispatch.get(fmt)
    if handler is None:
        suffix = mod_path.suffix.lower()
        msg = f"Unsupported file format: {suffix or 'unknown'}"
        _emit({"type": "error", "msg": msg})
        db.close()
        return

    result = handler()
    db.close()

    if result and result.error:
        _emit({"type": "error", "msg": result.error})
    elif result:
        _emit({"type": "done", "name": result.name, "mod_id": result.mod_id,
               "mod_type": result.mod_type, "error": None,
               "asi_staged": result.asi_staged or []})
    else:
        _emit({"type": "error", "msg": "Import returned no result"})


# ── Batch Import ─────────────────────────────────────────────────────

def _run_batch_import(paths_file: str, game_dir: str, db_path: str,
                      deltas_dir: str) -> None:
    """Import multiple mods in a single process. Paths read from a file (one per line)."""
    from cdumm.storage.database import Database
    from cdumm.engine.snapshot_manager import SnapshotManager
    from cdumm.engine.import_handler import (
        detect_format, import_from_zip, import_from_7z, import_from_folder,
        import_from_json_patch, import_from_bsdiff, import_from_script,
        import_from_rar, set_import_progress_cb,
    )

    game_dir = Path(game_dir)
    db_path = Path(db_path)
    deltas_dir = Path(deltas_dir)

    # Read paths from file
    mod_paths = []
    for line in Path(paths_file).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            mod_paths.append(Path(line))

    if not mod_paths:
        _emit({"type": "error", "msg": "No mod paths in batch file"})
        return

    db = Database(db_path)
    db.initialize()
    snapshot = SnapshotManager(db)

    total = len(mod_paths)
    _emit({"type": "batch_start", "total": total})

    dispatch_map = {
        "zip": import_from_zip, "7z": import_from_7z,
        "rar": import_from_rar, "folder": import_from_folder,
        "script": import_from_script, "json_patch": import_from_json_patch,
        "bsdiff": import_from_bsdiff,
    }

    for idx, mod_path in enumerate(mod_paths):
        _emit({"type": "batch_progress", "index": idx, "total": total,
               "name": mod_path.name})

        set_import_progress_cb(lambda pct, msg, _i=idx: _emit({
            "type": "progress", "pct": pct, "msg": msg,
            "batch_index": _i, "batch_total": total}))

        fmt = detect_format(mod_path)
        handler = dispatch_map.get(fmt)
        if handler is None:
            _emit({"type": "batch_item", "index": idx, "name": mod_path.name,
                   "error": f"Unsupported format: {mod_path.suffix}"})
            continue

        try:
            if fmt == "script":
                result = handler(mod_path, game_dir, db, snapshot, deltas_dir)
            elif fmt == "bsdiff":
                result = handler(mod_path, game_dir, db, snapshot, deltas_dir)
            else:
                result = handler(mod_path, game_dir, db, snapshot, deltas_dir)
        except Exception as e:
            _emit({"type": "batch_item", "index": idx, "name": mod_path.name,
                   "error": str(e)})
            continue

        if result and result.error:
            _emit({"type": "batch_item", "index": idx, "name": mod_path.name,
                   "error": result.error})
        elif result:
            # Check if this mod is configurable (has labeled JSON changes)
            if result.mod_id:
                try:
                    row = db.connection.execute(
                        "SELECT json_source FROM mods WHERE id = ?",
                        (result.mod_id,)).fetchone()
                    js = row[0] if row else None
                    if js and Path(js).exists():
                        from cdumm.gui.preset_picker import has_labeled_changes
                        import json as _jj
                        jdata = _jj.loads(Path(js).read_text(encoding="utf-8"))
                        if has_labeled_changes(jdata):
                            db.connection.execute(
                                "UPDATE mods SET configurable = 1, source_path = ? WHERE id = ?",
                                (str(mod_path), result.mod_id))
                            db.connection.commit()
                except Exception:
                    pass
                # Store drop_name and extract version from folder name
                try:
                    from cdumm.engine.nexus_filename import (
                        extract_version_from_filename, parse_nexus_filename,
                    )
                    drop_name = mod_path.name
                    db.connection.execute(
                        "UPDATE mods SET drop_name = ? WHERE id = ? AND (drop_name IS NULL OR drop_name = '')",
                        (drop_name, result.mod_id))
                    existing_ver = db.connection.execute(
                        "SELECT version FROM mods WHERE id = ?", (result.mod_id,)).fetchone()
                    if not (existing_ver and existing_ver[0]):
                        # Unified extractor: Nexus timestamp format first,
                        # then v-prefix, then bare dotted version.
                        stem = mod_path.stem if mod_path.is_file() else mod_path.name
                        version_val = extract_version_from_filename(stem)
                        # nexus_id still useful for update-check linking
                        # (no-op on master where the client is a stub).
                        nexus_id, _ = parse_nexus_filename(stem)
                        if version_val:
                            db.connection.execute(
                                "UPDATE mods SET version = ? WHERE id = ?",
                                (version_val, result.mod_id))
                        if nexus_id:
                            db.connection.execute(
                                "UPDATE mods SET nexus_mod_id = ? "
                                "WHERE id = ? AND nexus_mod_id IS NULL",
                                (nexus_id, result.mod_id))
                    db.connection.commit()
                except Exception:
                    pass
            _emit({"type": "batch_item", "index": idx, "name": result.name,
                   "mod_id": result.mod_id, "mod_type": result.mod_type,
                   "error": None, "asi_staged": result.asi_staged or []})
        else:
            _emit({"type": "batch_item", "index": idx, "name": mod_path.name,
                   "error": "Import returned no result"})

    db.close()
    _emit({"type": "done", "batch_total": total})


# ── Apply ─────────────────────────────────────────────────────────────

def _run_apply(game_dir: str, vanilla_dir: str, db_path: str,
               force_outdated: str = "0") -> None:
    from cdumm.storage.database import Database
    from cdumm.engine.apply_engine import ApplyWorker

    worker = ApplyWorker(
        game_dir=Path(game_dir),
        vanilla_dir=Path(vanilla_dir),
        db_path=Path(db_path),
        force_outdated=(force_outdated == "1"),
    )
    worker.progress_updated.connect(
        lambda pct, msg: _emit({"type": "progress", "pct": pct, "msg": msg}))
    worker.error_occurred.connect(
        lambda err: _emit({"type": "error", "msg": err}))
    # Non-fatal warnings (e.g. mount-time fallback used) surface to the
    # GUI via InfoBar.warning in on_apply_done — same shape as Revert.
    worker.warning.connect(
        lambda msg: _emit({"type": "warning", "msg": msg}))

    worker.run()
    _emit({"type": "done"})


# ── Revert ────────────────────────────────────────────────────────────

def _run_revert(game_dir: str, vanilla_dir: str, db_path: str) -> None:
    from cdumm.storage.database import Database
    from cdumm.engine.apply_engine import RevertWorker

    worker = RevertWorker(
        game_dir=Path(game_dir),
        vanilla_dir=Path(vanilla_dir),
        db_path=Path(db_path),
    )
    worker.progress_updated.connect(
        lambda pct, msg: _emit({"type": "progress", "pct": pct, "msg": msg}))
    worker.error_occurred.connect(
        lambda err: _emit({"type": "error", "msg": err}))
    worker.warning.connect(
        lambda msg: _emit({"type": "warning", "msg": msg}))

    worker.run()
    _emit({"type": "done"})


# ── Snapshot ──────────────────────────────────────────────────────────

def _run_snapshot(game_dir: str, db_path: str) -> None:
    from cdumm.engine.snapshot_manager import SnapshotWorker

    worker = SnapshotWorker(
        game_dir=Path(game_dir),
        db_path=Path(db_path),
    )
    worker.progress_updated.connect(
        lambda pct, msg: _emit({"type": "progress", "pct": pct, "msg": msg}))
    worker.error_occurred.connect(
        lambda err: _emit({"type": "error", "msg": err}))
    worker.activity.connect(
        lambda cat, msg, det: _emit({"type": "activity", "cat": cat, "msg": msg, "detail": det}))

    # SnapshotWorker.finished emits count — capture it
    _result = [0]
    worker.finished.connect(lambda count: _result.__setitem__(0, count))

    worker.run()
    _emit({"type": "done", "count": _result[0]})


# ── Verify ────────────────────────────────────────────────────────────

def _run_verify(game_dir: str, db_path: str) -> None:
    import os
    from cdumm.storage.database import Database
    from cdumm.engine.snapshot_manager import hash_file

    game_dir = Path(game_dir)
    db = Database(Path(db_path))
    db.initialize()

    cursor = db.connection.execute(
        "SELECT file_path, file_hash, file_size FROM snapshots")
    snap_entries = cursor.fetchall()

    if not snap_entries:
        _emit({"type": "error", "msg": "No snapshot found"})
        db.close()
        return

    results = {"vanilla": [], "modded": [], "missing": [], "extra_dirs": [], "total": len(snap_entries)}

    for i, (file_path, snap_hash, snap_size) in enumerate(snap_entries):
        pct = int((i / len(snap_entries)) * 100)
        _emit({"type": "progress", "pct": pct, "msg": f"Checking {file_path}"})

        game_file = game_dir / file_path.replace("/", os.sep)
        if not game_file.exists():
            results["missing"].append(file_path)
            continue

        actual_size = game_file.stat().st_size
        if actual_size != snap_size:
            results["modded"].append(f"{file_path} — size {actual_size} != vanilla {snap_size}")
        else:
            actual_hash, _ = hash_file(game_file)
            if actual_hash != snap_hash:
                results["modded"].append(f"{file_path} — content differs (same size)")
            else:
                results["vanilla"].append(file_path)

    # Check for extra directories (>= 0036)
    for item in sorted(game_dir.iterdir()):
        if item.is_dir() and item.name.isdigit() and int(item.name) >= 36:
            results["extra_dirs"].append(item.name)

    db.close()
    _emit({"type": "done", "results": results})


# ── Check Mods Health ─────────────────────────────────────────────────

def _run_check_mods(game_dir: str, db_path: str) -> None:
    import os
    from cdumm.storage.database import Database

    game_dir = Path(game_dir)
    db = Database(Path(db_path))
    db.initialize()
    issues = []

    # 1. Check vanilla file sizes
    _emit({"type": "progress", "pct": 10, "msg": "Checking vanilla file sizes..."})
    try:
        size_rows = db.connection.execute(
            "SELECT m.name, vs.file_path, vs.vanilla_size "
            "FROM mod_vanilla_sizes vs JOIN mods m ON vs.mod_id = m.id "
            "WHERE m.enabled = 1"
        ).fetchall()
        for i, (mod_name, fp, expected_size) in enumerate(size_rows):
            if i % 10 == 0:
                pct = 10 + int((i / max(len(size_rows), 1)) * 70)
                _emit({"type": "progress", "pct": pct, "msg": f"Checking {fp}"})
            vanilla_path = game_dir / "CDMods" / "vanilla" / fp.replace("/", os.sep)
            game_path = game_dir / fp.replace("/", os.sep)
            src = vanilla_path if vanilla_path.exists() else game_path
            if src.exists():
                actual_size = src.stat().st_size
                if actual_size != expected_size:
                    issues.append([mod_name,
                        f"{fp} size changed ({expected_size} -> {actual_size}) — "
                        f"game updated, mod needs re-importing"])
    except Exception:
        pass

    # 2. Check delta files exist
    _emit({"type": "progress", "pct": 85, "msg": "Checking delta files..."})
    delta_rows = db.connection.execute(
        "SELECT m.name, md.delta_path, md.file_path "
        "FROM mod_deltas md JOIN mods m ON md.mod_id = m.id "
        "WHERE m.enabled = 1"
    ).fetchall()
    checked_paths = set()
    for mod_name, dp, fp in delta_rows:
        if dp in checked_paths:
            continue
        checked_paths.add(dp)
        if not Path(dp).exists():
            issues.append([mod_name, f"Missing delta file for {fp}"])

    db.close()
    _emit({"type": "done", "issues": issues})


# ── Fix Everything ────────────────────────────────────────────────────

def _run_fix(game_dir: str, vanilla_dir: str, db_path: str,
             steam_verified: str = "0") -> None:
    import shutil
    from cdumm.storage.database import Database

    game_dir = Path(game_dir)
    vanilla_dir = Path(vanilla_dir)
    steam = steam_verified == "1"
    results = []  # list of {"title": ..., "desc": ..., "color": ...}

    # Step 1: Revert
    _emit({"type": "progress", "pct": 5, "msg": "Reverting to vanilla..."})
    try:
        from cdumm.engine.apply_engine import RevertWorker
        revert_db = Database(Path(db_path))
        revert_db.initialize()
        rw = RevertWorker.__new__(RevertWorker)
        rw._game_dir = game_dir
        rw._vanilla_dir = vanilla_dir
        rw._db = revert_db
        rw._revert()
        revert_db.close()
        results.append({"title": "Revert Complete",
                        "desc": "All game files restored to vanilla.", "color": "#A3BE8C"})
    except Exception as e:
        results.append({"title": "Revert Warning",
                        "desc": f"Revert issue: {e}", "color": "#EBCB8B"})

    # Step 2: Clean orphan directories
    _emit({"type": "progress", "pct": 40, "msg": "Cleaning orphan directories..."})
    cleaned = 0
    try:
        db2 = Database(Path(db_path))
        db2.initialize()
        for d in sorted(game_dir.iterdir()):
            if (d.is_dir() and d.name.isdigit() and len(d.name) == 4
                    and int(d.name) >= 36):
                snap_check = db2.connection.execute(
                    "SELECT COUNT(*) FROM snapshots WHERE file_path LIKE ?",
                    (d.name + "/%",)).fetchone()[0]
                if snap_check == 0:
                    shutil.rmtree(d, ignore_errors=True)
                    cleaned += 1
        db2.close()
        if cleaned:
            results.append({"title": f"Cleaned {cleaned} orphan directories",
                            "desc": "", "color": "#A3BE8C"})
    except Exception as e:
        results.append({"title": "Cleanup Warning", "desc": str(e), "color": "#EBCB8B"})

    # Step 3: Steam-verified extras
    if steam:
        _emit({"type": "progress", "pct": 70, "msg": "Clearing vanilla backups..."})
        try:
            if vanilla_dir.exists():
                shutil.rmtree(vanilla_dir, ignore_errors=True)
                vanilla_dir.mkdir(parents=True, exist_ok=True)
            results.append({"title": "Backups Cleared",
                            "desc": "Fresh rescan will rebuild them.", "color": "#A3BE8C"})
        except Exception as e:
            results.append({"title": "Backup Cleanup Warning",
                            "desc": str(e), "color": "#EBCB8B"})

    _emit({"type": "done", "results": results, "steam_verified": steam, "cleaned": cleaned})


# ── Inspect Mod ───────────────────────────────────────────────────────

def _run_inspect(mod_path: str, game_dir: str, db_path: str) -> None:
    from cdumm.storage.database import Database
    from cdumm.engine.snapshot_manager import SnapshotManager
    from cdumm.engine.import_handler import set_import_progress_cb
    from cdumm.engine.mod_diagnostics import diagnose_mod

    mod_path = Path(mod_path)
    game_dir = Path(game_dir)
    db = Database(Path(db_path))
    db.initialize()
    snapshot = SnapshotManager(db)

    set_import_progress_cb(lambda pct, msg: _emit({"type": "progress", "pct": pct, "msg": msg}))
    _emit({"type": "progress", "pct": 0, "msg": f"Analyzing {mod_path.name}..."})

    from cdumm.engine.test_mod_checker import test_mod
    result = test_mod(mod_path, game_dir, db, snapshot)

    # Always run diagnostics — provides file structure analysis,
    # PAMT lookups, and actionable info for the mod author
    _emit({"type": "progress", "pct": 90, "msg": "Running diagnostics..."})
    diag_report = diagnose_mod(mod_path, game_dir, Path(db_path),
                               result.error or "")
    db.close()

    if result.error:
        _emit({"type": "done", "error": result.error, "mod_name": result.mod_name,
               "changed_files": [], "conflicts": [], "compatible_mods": [],
               "diagnostic_report": diag_report})
    else:
        conflicts = []
        for c in result.conflicts:
            conflicts.append(str(c.explanation) if hasattr(c, "explanation") else str(c))
        changed = []
        for f in result.changed_files:
            changed.append(f.get("path", str(f)) if isinstance(f, dict) else str(f))
        _emit({"type": "done", "error": None, "mod_name": result.mod_name,
               "changed_files": changed, "conflicts": conflicts,
               "compatible_mods": result.compatible_mods,
               "diagnostic_report": diag_report})


# ── Diagnose ──────────────────────────────────────────────────────────

def _run_diagnose(mod_path: str, game_dir: str, db_path: str,
                  original_error: str = "") -> None:
    from cdumm.engine.mod_diagnostics import diagnose_mod
    report = diagnose_mod(Path(mod_path), Path(game_dir), Path(db_path), original_error)
    _emit({"type": "done", "report": report})


# ── Entry point ───────────────────────────────────────────────────────

def worker_main(args: list[str]) -> None:
    """Entry point for --worker subprocess mode."""
    # Redirect logging to stderr AND to a log file for post-mortem debugging
    log_handlers = [logging.StreamHandler(sys.stderr)]
    try:
        log_file = Path(sys.argv[0]).resolve().parent / "cdumm_worker.log"
        fh = logging.FileHandler(str(log_file), mode="w", encoding="utf-8")
        log_handlers.append(fh)
    except Exception:
        pass
    logging.basicConfig(
        handlers=log_handlers, level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args:
        _emit({"type": "error", "msg": "No worker command specified"})
        sys.exit(1)

    cmd = args[0]
    try:
        if cmd == "import":
            # import <mod_path> <game_dir> <db_path> <deltas_dir> [existing_mod_id]
            _run_import(args[1], args[2], args[3], args[4],
                        args[5] if len(args) > 5 else None)
        elif cmd == "import_batch":
            # import_batch <paths_file> <game_dir> <db_path> <deltas_dir>
            _run_batch_import(args[1], args[2], args[3], args[4])
        elif cmd == "apply":
            # apply <game_dir> <vanilla_dir> <db_path> [force_outdated]
            _run_apply(args[1], args[2], args[3],
                       args[4] if len(args) > 4 else "0")
        elif cmd == "revert":
            # revert <game_dir> <vanilla_dir> <db_path>
            _run_revert(args[1], args[2], args[3])
        elif cmd == "snapshot":
            # snapshot <game_dir> <db_path>
            _run_snapshot(args[1], args[2])
        elif cmd == "verify":
            # verify <game_dir> <db_path>
            _run_verify(args[1], args[2])
        elif cmd == "check_mods":
            # check_mods <game_dir> <db_path>
            _run_check_mods(args[1], args[2])
        elif cmd == "fix":
            # fix <game_dir> <vanilla_dir> <db_path> [steam_verified]
            _run_fix(args[1], args[2], args[3],
                     args[4] if len(args) > 4 else "0")
        elif cmd == "inspect":
            # inspect <mod_path> <game_dir> <db_path>
            _run_inspect(args[1], args[2], args[3])
        elif cmd == "diagnose":
            # diagnose <mod_path> <game_dir> <db_path> [original_error]
            _run_diagnose(args[1], args[2], args[3],
                          args[4] if len(args) > 4 else "")
        else:
            _emit({"type": "error", "msg": f"Unknown worker command: {cmd}"})
            sys.exit(1)
    except Exception as e:
        logging.error("Worker crashed: %s", e, exc_info=True)
        _emit({"type": "error", "msg": str(e)})
        sys.exit(1)
