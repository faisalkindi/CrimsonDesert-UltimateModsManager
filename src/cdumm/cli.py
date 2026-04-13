"""Headless CLI for CDUMM — used by external tools (crash monitor, scripts).

Commands:
    CDUMM.exe list-mods [--json]
    CDUMM.exe set-enabled --mod-id ID --enabled true|false
    CDUMM.exe apply [--game-dir PATH]
"""
import argparse
import json
import logging
import sys
from pathlib import Path

APP_DATA_DIR = Path.home() / "AppData" / "Local" / "cdumm"


def _attach_console():
    """Attach to parent console for windowed exe (console=False in PyInstaller)."""
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        if kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            sys.stdout = open("CONOUT$", "w")
            sys.stderr = open("CONOUT$", "w")


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )


def _resolve_game_dir(override: str | None = None) -> Path | None:
    """Find game directory from override, pointer file, or DB."""
    if override:
        p = Path(override)
        if p.exists():
            return p

    pointer = APP_DATA_DIR / "game_dir.txt"
    if pointer.exists():
        saved = pointer.read_text(encoding="utf-8").strip()
        if saved and Path(saved).exists():
            return Path(saved)

    return None


def _open_db(game_dir: Path):
    from cdumm.storage.database import Database
    db_path = game_dir / "CDMods" / "cdumm.db"
    if not db_path.exists():
        print(f"Error: database not found at {db_path}", file=sys.stderr)
        sys.exit(1)
    db = Database(db_path)
    db.initialize()
    return db


def cmd_list_mods(args):
    game_dir = _resolve_game_dir(args.game_dir)
    if not game_dir:
        print("Error: cannot find game directory. Use --game-dir.", file=sys.stderr)
        sys.exit(1)

    db = _open_db(game_dir)
    from cdumm.engine.mod_manager import ModManager
    mgr = ModManager(db, game_dir / "CDMods" / "deltas")
    mods = mgr.list_mods(args.type)

    if args.json:
        out = []
        for m in mods:
            entry = {
                "id": m["id"],
                "name": m["name"],
                "mod_type": m["mod_type"],
                "enabled": m["enabled"],
                "priority": m["priority"],
            }
            if args.status:
                entry["status"] = mgr.get_mod_game_status(m["id"], game_dir)
            out.append(entry)
        print(json.dumps(out, indent=2))
    else:
        for m in mods:
            if args.status:
                game_status = mgr.get_mod_game_status(m["id"], game_dir)
                print(f"[{game_status:>12s}] #{m['id']:>3d}  {m['name']}  ({m['mod_type']})")
            else:
                status = "ON " if m["enabled"] else "OFF"
                print(f"[{status}] #{m['id']:>3d}  {m['name']}  ({m['mod_type']})")

    db.close()


def cmd_set_enabled(args):
    game_dir = _resolve_game_dir(args.game_dir)
    if not game_dir:
        print("Error: cannot find game directory. Use --game-dir.", file=sys.stderr)
        sys.exit(1)

    enabled = args.enabled.lower() in ("true", "1", "yes", "on")

    db = _open_db(game_dir)
    from cdumm.engine.mod_manager import ModManager
    mgr = ModManager(db, game_dir / "CDMods" / "deltas")

    # Verify mod exists
    mods = mgr.list_mods()
    mod = next((m for m in mods if m["id"] == args.mod_id), None)
    if not mod:
        print(f"Error: mod ID {args.mod_id} not found.", file=sys.stderr)
        db.close()
        sys.exit(1)

    mgr.set_enabled(args.mod_id, enabled)
    state = "enabled" if enabled else "disabled"
    print(f"{mod['name']} (#{args.mod_id}) {state}")
    db.close()


def cmd_apply(args):
    game_dir = _resolve_game_dir(args.game_dir)
    if not game_dir:
        print("Error: cannot find game directory. Use --game-dir.", file=sys.stderr)
        sys.exit(1)

    db_path = game_dir / "CDMods" / "cdumm.db"
    if not db_path.exists():
        print(f"Error: database not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    vanilla_dir = game_dir / "CDMods" / "vanilla"

    # ApplyWorker needs PySide6 for QObject/Signal — import it
    from cdumm.engine.apply_engine import ApplyWorker

    worker = ApplyWorker(game_dir, vanilla_dir, db_path, force_outdated=False)

    errors = []

    def on_progress(pct, msg):
        print(f"[{pct:3d}%] {msg}", file=sys.stderr)

    def on_error(msg):
        errors.append(msg)
        print(f"ERROR: {msg}", file=sys.stderr)

    worker.progress_updated.connect(on_progress)
    worker.error_occurred.connect(on_error)

    worker.run()

    if errors:
        sys.exit(1)
    else:
        print("Apply complete.", file=sys.stderr)
        sys.exit(0)


def cmd_bisect(args):
    """Interactive bisection via CLI. Two sub-modes:
        bisect start [--mod-ids 1,2,3]  → starts session, applies first config, prints JSON state
        bisect report --crashed true/false → reports result, applies next config, prints JSON state
    """
    game_dir = _resolve_game_dir(args.game_dir)
    if not game_dir:
        print("Error: cannot find game directory.", file=sys.stderr)
        sys.exit(1)

    db = _open_db(game_dir)
    from cdumm.engine.mod_manager import ModManager
    from cdumm.engine.binary_search import DeltaDebugSession
    from cdumm.engine.apply_engine import ApplyWorker

    mgr = ModManager(db, game_dir / "CDMods" / "deltas")

    if args.action == "start":
        # Optionally filter to specific mod IDs
        if args.mod_ids:
            filter_ids = set(int(x) for x in args.mod_ids.split(","))
            # Disable mods NOT in the filter list
            for m in mgr.list_mods():
                if m["enabled"] and m["id"] not in filter_ids:
                    mgr.set_enabled(m["id"], False)

        session = DeltaDebugSession(mgr)
        config = session.start_round()

        # Apply the config
        for mod_id, enabled in config.items():
            mgr.set_enabled(mod_id, enabled)

        vanilla_dir = game_dir / "CDMods" / "vanilla"
        db_path = game_dir / "CDMods" / "cdumm.db"
        worker = ApplyWorker(game_dir, vanilla_dir, db_path, force_outdated=False)
        worker.progress_updated.connect(lambda pct, msg: print(f"[{pct:3d}%] {msg}", file=sys.stderr))
        worker.run()

        # Save session state
        _save_bisect_session(db, session)

        # Output state as JSON
        testing = [session.get_mod_name(m) for m in session.current_group]
        print(json.dumps({
            "phase": session.phase,
            "round": session.round_number,
            "testing": testing,
            "testing_count": len(testing),
            "total_suspects": len(session.all_ids),
            "status": session.get_phase_description(),
        }))

    elif args.action == "report":
        # Load saved session
        session = _load_bisect_session(db, mgr)
        if not session:
            print("Error: no active bisection session. Run 'bisect start' first.", file=sys.stderr)
            sys.exit(1)

        crashed = args.crashed.lower() in ("true", "1", "yes")
        status_msg = session.report_crash(crashed)
        print(f"Result: {status_msg}", file=sys.stderr)

        if session.is_done():
            culprit_ids = set(session._changes)
            culprits = [session.get_mod_name(m) for m in culprit_ids]
            # Restore original state BUT keep culprits disabled
            for mod_id, enabled in session.original_state.items():
                if mod_id in culprit_ids:
                    mgr.set_enabled(mod_id, False)
                else:
                    mgr.set_enabled(mod_id, enabled)
            vanilla_dir = game_dir / "CDMods" / "vanilla"
            db_path = game_dir / "CDMods" / "cdumm.db"
            worker = ApplyWorker(game_dir, vanilla_dir, db_path, force_outdated=False)
            worker.progress_updated.connect(lambda pct, msg: print(f"[{pct:3d}%] {msg}", file=sys.stderr))
            worker.run()
            _clear_bisect_session(db)
            print(json.dumps({
                "phase": "done",
                "culprits": culprits,
                "rounds": session.round_number,
            }))
        else:
            # Apply next config
            config = session.start_round()
            for mod_id, enabled in config.items():
                mgr.set_enabled(mod_id, enabled)
            vanilla_dir = game_dir / "CDMods" / "vanilla"
            db_path = game_dir / "CDMods" / "cdumm.db"
            worker = ApplyWorker(game_dir, vanilla_dir, db_path, force_outdated=False)
            worker.progress_updated.connect(lambda pct, msg: print(f"[{pct:3d}%] {msg}", file=sys.stderr))
            worker.run()
            _save_bisect_session(db, session)
            testing = [session.get_mod_name(m) for m in session.current_group]
            print(json.dumps({
                "phase": session.phase,
                "round": session.round_number,
                "testing": testing,
                "testing_count": len(testing),
                "status": status_msg,
            }))

    db.close()


def _save_bisect_session(db, session):
    data = json.dumps({
        "changes": session._changes,
        "n": session._n,
        "partition_index": session._partition_index,
        "testing_complement": session._testing_complement,
        "test_set": session._test_set,
        "current_group": session.current_group,
        "round_number": session.round_number,
        "history": session.history,
        "phase": session.phase,
        "all_ids": session.all_ids,
        "original_state": {int(k): v for k, v in session.original_state.items()},
    })
    db.connection.execute(
        "CREATE TABLE IF NOT EXISTS ddmin_progress (id INTEGER PRIMARY KEY, data TEXT)")
    db.connection.execute(
        "INSERT OR REPLACE INTO ddmin_progress (id, data) VALUES (1, ?)", (data,))
    db.connection.commit()


def _load_bisect_session(db, mgr):
    from cdumm.engine.binary_search import DeltaDebugSession
    try:
        row = db.connection.execute(
            "SELECT data FROM ddmin_progress WHERE id = 1").fetchone()
        if not row:
            return None
        saved = json.loads(row[0])
        session = DeltaDebugSession(mgr)
        session._changes = saved["changes"]
        session._n = saved["n"]
        session._partition_index = saved["partition_index"]
        session._testing_complement = saved["testing_complement"]
        session.round_number = saved["round_number"]
        session.history = saved["history"]
        session.phase = saved["phase"]
        session.all_ids = saved["all_ids"]
        # JSON dict keys are always strings — convert back to int
        session.original_state = {int(k): v for k, v in saved["original_state"].items()}
        # Restore _test_set and current_group (critical for report_crash)
        if "test_set" in saved:
            session._test_set = saved["test_set"]
            session.current_group = saved.get("current_group", list(session._test_set))
        else:
            # Recompute from algorithm state (backward compat)
            partitions = session._split(session._changes, session._n)
            if session._partition_index < len(partitions):
                if not session._testing_complement:
                    session._test_set = partitions[session._partition_index]
                else:
                    session._test_set = [
                        mid for mid in session._changes
                        if mid not in partitions[session._partition_index]
                    ]
            else:
                session._test_set = list(session._changes)
            session.current_group = list(session._test_set)
        return session
    except Exception:
        return None


def _clear_bisect_session(db):
    try:
        db.connection.execute("DELETE FROM ddmin_progress WHERE id = 1")
        db.connection.commit()
    except Exception:
        pass


def main():
    _attach_console()
    _setup_logging()

    parser = argparse.ArgumentParser(
        prog="cdumm",
        description="CDUMM command-line interface for external tool integration.",
    )
    sub = parser.add_subparsers(dest="command")

    # list-mods
    p_list = sub.add_parser("list-mods", help="List mods")
    p_list.add_argument("--json", action="store_true", help="Output as JSON")
    p_list.add_argument("--status", action="store_true", help="Include game file status (active/not applied)")
    p_list.add_argument("--type", default=None, help="Filter by mod_type (paz, asi)")
    p_list.add_argument("--game-dir", default=None, help="Game directory override")

    # set-enabled
    p_set = sub.add_parser("set-enabled", help="Enable or disable a mod")
    p_set.add_argument("--mod-id", type=int, required=True, help="Mod ID")
    p_set.add_argument("--enabled", required=True, help="true or false")
    p_set.add_argument("--game-dir", default=None, help="Game directory override")

    # apply
    p_apply = sub.add_parser("apply", help="Apply current mod state to game files")
    p_apply.add_argument("--game-dir", default=None, help="Game directory override")

    # bisect
    p_bisect = sub.add_parser("bisect", help="Binary search for problem mod")
    p_bisect.add_argument("action", choices=["start", "report"], help="start or report")
    p_bisect.add_argument("--mod-ids", default=None, help="Comma-separated mod IDs to test (optional)")
    p_bisect.add_argument("--crashed", default=None, help="true/false — did the game crash?")
    p_bisect.add_argument("--game-dir", default=None, help="Game directory override")

    args = parser.parse_args()

    if args.command == "list-mods":
        cmd_list_mods(args)
    elif args.command == "set-enabled":
        cmd_set_enabled(args)
    elif args.command == "apply":
        cmd_apply(args)
    elif args.command == "bisect":
        cmd_bisect(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
