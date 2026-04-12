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

    args = parser.parse_args()

    if args.command == "list-mods":
        cmd_list_mods(args)
    elif args.command == "set-enabled":
        cmd_set_enabled(args)
    elif args.command == "apply":
        cmd_apply(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
