"""Bug report dialog — collects logs, system info, and mod state for diagnostics."""
import logging
import os
import platform
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    InfoBar,
    InfoBarPosition,
    MessageBoxBase,
    PrimaryPushButton,
    PushButton,
    SubtitleLabel,
)

from cdumm.i18n import tr
from cdumm.storage.database import Database

logger = logging.getLogger(__name__)

from cdumm import __version__ as APP_VERSION


_USER_PATH_RE = re.compile(
    r"([A-Za-z]:[\\/]Users[\\/])([^\\/\r\n\"'<>|:*?]+)([\\/])",
    re.IGNORECASE,
)


def scrub_windows_paths(text: str) -> str:
    """Replace `C:\\Users\\<name>\\` with `C:\\Users\\USER\\` to protect privacy.

    Preserves the slash style that was used (backslash or forward-slash) and the
    drive letter. Leaves the rest of the path intact. Case-insensitive on the
    Users/drive portion.
    """
    if not text:
        return text
    return _USER_PATH_RE.sub(lambda m: f"{m.group(1)}USER{m.group(3)}", text)


def _short_python_version() -> str:
    """Strip the compiler-info tail from sys.version."""
    v = sys.version.splitlines()[0]
    # e.g. '3.14.3 (tags/v3.14.3:323c59a, Feb  3 2026, 16:04:56)' -> '3.14.3'
    return v.split(" ", 1)[0]


def _is_admin_windows() -> str:
    """Return 'yes' / 'no' / 'unknown' for Windows administrator privilege."""
    if sys.platform != "win32":
        return "n/a"
    try:
        import ctypes
        result = ctypes.windll.shell32.IsUserAnAdmin()
        if result == 1:
            return "yes"
        if result == 0:
            return "no"
        return "unknown"
    except Exception:
        return "unknown"


def _detect_game_platform(game_dir: Path | None) -> str:
    """Identify Steam / Xbox (Game Pass) / Epic / standalone from the install path.

    Uses the same heuristics as storage.game_finder (path tokens) plus a check
    for Steam's steam_appid.txt sidecar, and adds Xbox's MicrosoftGame.Config
    marker when present.
    """
    if not game_dir or not game_dir.exists():
        return "unknown"
    try:
        from cdumm.storage.game_finder import (
            is_steam_install, is_epic_install, is_xbox_install)
    except Exception:
        is_steam_install = is_epic_install = is_xbox_install = lambda _p: False
    try:
        if is_steam_install(game_dir):
            return "Steam"
        if is_xbox_install(game_dir):
            return "Xbox (Game Pass / Microsoft Store)"
        if is_epic_install(game_dir):
            return "Epic Games Store"
        # Fallback: sniff well-known files inside the install dir
        for marker in ("steam_api64.dll", "steam_appid.txt"):
            if (game_dir / marker).exists() or (game_dir / "bin64" / marker).exists():
                return "Steam (detected via sidecar)"
        if (game_dir / "MicrosoftGame.Config").exists():
            return "Xbox (Microsoft Store)"
        return "standalone / unknown"
    except Exception:
        return "unknown"


def _duplicate_nexus_links(db: Database) -> list[tuple[int, list[str]]]:
    """Return [(nexus_mod_id, [mod names...]), ...] for IDs used by 2+ PAZ mods."""
    try:
        rows = db.connection.execute(
            "SELECT nexus_mod_id, GROUP_CONCAT(name, '||') "
            "FROM mods "
            "WHERE mod_type='paz' AND nexus_mod_id IS NOT NULL "
            "GROUP BY nexus_mod_id "
            "HAVING COUNT(*) > 1").fetchall()
    except Exception:
        return []
    return [(r[0], (r[1] or "").split("||")) for r in rows]


def _file_level_conflicts(db: Database, enabled_only: bool = True,
                         limit: int = 20) -> list[tuple[str, int, list[str]]]:
    """Return [(file_path, mod_count, [mod names...]), ...] for files touched
    by 2+ (enabled) mods, ordered by overlap count desc."""
    filter_sql = ("AND md.mod_id IN (SELECT id FROM mods WHERE enabled = 1) "
                  if enabled_only else "")
    try:
        rows = db.connection.execute(
            f"SELECT md.file_path, COUNT(DISTINCT md.mod_id) AS n, "
            f"       GROUP_CONCAT(DISTINCT m.name) "
            f"FROM mod_deltas md JOIN mods m ON m.id = md.mod_id "
            f"WHERE 1=1 {filter_sql}"
            f"GROUP BY md.file_path HAVING n > 1 "
            f"ORDER BY n DESC, md.file_path LIMIT ?", (limit,)).fetchall()
    except Exception:
        return []
    out: list[tuple[str, int, list[str]]] = []
    for fp, n, names in rows:
        out.append((fp, n, (names or "").split(",")))
    return out


def _outdated_mods(db: Database, current_hash: str | None) -> list[tuple[int, str, str | None]]:
    """Return [(mod_id, name, mod's stored hash), ...] for mods whose
    game_version_hash differs from the current game's hash."""
    if not current_hash:
        return []
    try:
        rows = db.connection.execute(
            "SELECT id, name, game_version_hash FROM mods "
            "WHERE mod_type='paz' AND enabled=1 AND game_version_hash IS NOT NULL "
            "AND game_version_hash != ?", (current_hash,)).fetchall()
    except Exception:
        return []
    return [(r[0], r[1], r[2]) for r in rows]


def _is_relevant_log_line(ln: str) -> bool:
    """Keep only lines likely useful for diagnostics."""
    if "DEBUG cdumm.semantic.parser" in ln:
        return False
    if " DEBUG " in ln and "cdumm" in ln:
        # Skip all non-semantic DEBUG lines too — usually noisy internals.
        # Exception: DEBUG lines that mention apply/import/revert/snapshot.
        kw = ("apply", "import", "revert", "snapshot", "error", "fail")
        if not any(k in ln.lower() for k in kw):
            return False
    return True


def _format_import_date(d: str | None) -> str:
    if not d:
        return "?"
    # Trim seconds for compactness: '2026-04-16 20:31:32' -> '2026-04-16 20:31'
    return d[:16] if len(d) >= 16 else d


def _render_version(ver: str | None) -> str:
    """Render a stored version string for display.

    Returns '—' for missing, or 'v<stripped>' otherwise. Strips a leading
    'v'/'V' so versions saved as 'v2.5' don't render as 'vv2.5' after the
    formatter prepends its own 'v'.
    """
    if not ver:
        return "—"
    s = ver.strip()
    while s and s[0] in ("v", "V"):
        s = s[1:]
    return f"v{s}" if s else "—"


def _bytes_human(n: int) -> str:
    """Format a byte count as a human-readable string."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


def _folder_stats(path: Path, max_scan_seconds: float = 3.0) -> tuple[int, int]:
    """Return (file_count, total_bytes) of a folder. Bails out after a few
    seconds on huge trees so the bug report never hangs."""
    import time
    if not path or not path.exists() or not path.is_dir():
        return 0, 0
    count = 0
    total = 0
    start = time.monotonic()
    try:
        for f in path.rglob("*"):
            if not f.is_file():
                continue
            count += 1
            try:
                total += f.stat().st_size
            except OSError:
                pass
            if time.monotonic() - start > max_scan_seconds:
                break
    except Exception:
        pass
    return count, total


def _disk_free(path: Path) -> str:
    """Return a human-readable 'free/total' for the disk containing path."""
    try:
        du = shutil.disk_usage(str(path))
        return f"{du.free / 1024 ** 3:.1f} GB free of {du.total / 1024 ** 3:.1f} GB"
    except Exception as e:
        return f"(error: {e})"


def _is_program_files(path: Path) -> bool:
    s = str(path).lower()
    return "program files" in s


def _read_compat_flags(exe_path: Path) -> list[str]:
    """Read Windows AppCompatFlags\\Layers for the given exe.

    Per Microsoft docs (learn.microsoft.com), per-user flags live at
    HKCU\\Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers
    with the full exe path as the value name and flags separated by spaces.
    Per-machine flags mirror this under HKLM. Returns flag list combined
    from both hives, or an empty list when not running on Windows or when
    no flags are set.
    """
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except Exception:
        return []
    key = r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
    flags: list[str] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, key) as k:
                try:
                    val, _ = winreg.QueryValueEx(k, str(exe_path))
                    if isinstance(val, str):
                        for tok in val.split():
                            if tok and tok not in flags:
                                flags.append(tok)
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass
        except Exception:
            pass
    return flags


def _verify_fast(db: Database, game_dir: Path) -> dict:
    """Size-only verify of snapshot vs current disk. Much faster than the
    full verify (no rehashing). Same-size = assumed unchanged for TL;DR."""
    result = {"total": 0, "vanilla": 0, "modded": 0, "missing": 0}
    try:
        rows = db.connection.execute(
            "SELECT file_path, file_size FROM snapshots").fetchall()
    except Exception:
        return result
    result["total"] = len(rows)
    for fp, snap_size in rows:
        p = game_dir / fp.replace("/", os.sep)
        try:
            if not p.exists():
                result["missing"] += 1
                continue
            if p.stat().st_size != snap_size:
                result["modded"] += 1
            else:
                result["vanilla"] += 1
        except Exception:
            result["missing"] += 1
    return result


def _delta_size_for_mod(mod_id: int, deltas_dir: Path) -> int:
    """Return the total bytes stored under CDMods/deltas/<mod_id>/."""
    d = deltas_dir / str(mod_id)
    if not d.exists() or not d.is_dir():
        return 0
    total = 0
    try:
        for f in d.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except Exception:
        pass
    return total


def _has_vanilla_backup(file_paths: list[str], vanilla_dir: Path) -> bool:
    """True if at least one of the mod's game files has a vanilla backup."""
    if not vanilla_dir or not vanilla_dir.exists():
        return False
    for fp in file_paths:
        if (vanilla_dir / fp.replace("/", os.sep)).exists():
            return True
    return False


def _exe_info(exe_path: Path) -> str:
    """Summarize game exe path, size, mtime for quick 'is this the exe you think' check."""
    if not exe_path or not exe_path.exists():
        return f"{exe_path} (MISSING)"
    try:
        st = exe_path.stat()
        size_mb = st.st_size / 1024 ** 2
        mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        return f"{exe_path} ({size_mb:.1f} MB, mtime {mtime})"
    except Exception as e:
        return f"{exe_path} (error: {e})"


def generate_bug_report(db: Database | None, game_dir: Path | None,
                        app_data_dir: Path | None) -> str:
    """Build a structured bug report for user-facing diagnostics."""
    body: list[str] = []
    tldr_flags: list[str] = []  # red-flag bullets collected during scan
    tldr_ok: list[str] = []     # positive observations
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── System ─────────────────────────────────────────────────────────
    body.append("--- SYSTEM ---")
    body.append(f"OS: {platform.platform()}")
    frozen = getattr(sys, "frozen", False)
    body.append(f"Python: {_short_python_version()} "
                 f"({'frozen' if frozen else 'source'})")
    admin_state = _is_admin_windows()
    body.append(f"Running as administrator: {admin_state}")
    if admin_state == "yes":
        tldr_flags.append(
            "CDUMM is running as administrator — drag-drop and mod install "
            "will often fail. Close CDUMM and relaunch WITHOUT 'Run as admin'.")
    elif admin_state == "no":
        tldr_ok.append("Not running as admin (good)")

    game_exe: Path | None = None
    if game_dir:
        dir_ok = game_dir.exists()
        body.append(f"Game Dir: {game_dir} ({'exists' if dir_ok else 'MISSING'})")
        if not dir_ok:
            tldr_flags.append(
                f"Game directory '{game_dir}' doesn't exist on disk — reinstall "
                "the game or pick the correct folder in CDUMM.")
        if _is_program_files(game_dir):
            body.append("  ! WARNING: Game is installed under Program Files. "
                         "Windows restricts writes here; mod install can fail.")
            tldr_flags.append(
                "Game is installed under Program Files. Windows permissions "
                "often break mod install — consider moving the Steam library "
                "to a non-protected drive.")
        body.append(f"Game Platform: {_detect_game_platform(game_dir)}")
        # Game exe specifics — same-size check, admin compat flag, mtime
        game_exe = game_dir / "bin64" / "CrimsonDesert.exe"
        if not game_exe.exists():
            game_exe = game_dir / "CrimsonDesert.exe"
        body.append(f"Game Exe: {_exe_info(game_exe)}")
        if game_exe.exists():
            compat = _read_compat_flags(game_exe)
            if compat:
                body.append(f"Game Exe Compat Flags: {' '.join(compat)}")
                if any("RUNASADMIN" in c.upper() for c in compat):
                    tldr_flags.append(
                        "Game exe has RUNASADMIN set in Windows compatibility "
                        "flags — this causes WINerror 740 and blocks drag-drop. "
                        "Right-click CrimsonDesert.exe → Properties → "
                        "Compatibility → uncheck 'Run as administrator'.")
            else:
                body.append("Game Exe Compat Flags: none")
        # Current game version hash (fingerprint of installed game files)
        try:
            from cdumm.engine.version_detector import detect_game_version
            gv = detect_game_version(game_dir)
            body.append(f"Game Version Hash: {gv or '(not detected)'}")
        except Exception as e:
            body.append(f"Game Version Hash: (error: {e})")
        # Disk free on the game drive
        body.append(f"Game Drive Free: {_disk_free(game_dir)}")
    if app_data_dir:
        body.append(f"App Data: {app_data_dir}")
        try:
            total = sum(f.stat().st_size for f in app_data_dir.rglob("*")
                        if f.is_file())
            body.append(f"App Data Size: {total / 1048576:.1f} MB")
        except Exception:
            pass
        body.append(f"App Data Drive Free: {_disk_free(app_data_dir)}")
    body.append("")

    # ── Storage ────────────────────────────────────────────────────────
    if game_dir:
        body.append("--- STORAGE ---")
        cdmods_dir = game_dir / "CDMods"
        if cdmods_dir.exists():
            body.append(f"CDMods: {cdmods_dir}")
            for sub in ("vanilla", "deltas", "sources", "overlay"):
                sd = cdmods_dir / sub
                if sd.exists():
                    n, sz = _folder_stats(sd)
                    body.append(f"  {sub}/: {n} files, {_bytes_human(sz)}")
                else:
                    body.append(f"  {sub}/: (not present)")
        else:
            body.append("CDMods: (not present — fresh install or folder moved)")
            tldr_flags.append(
                "CDMods/ folder is missing inside the game directory. "
                "Applied mods and backups cannot be located. Reopen CDUMM to "
                "rebuild it, then reimport any mods you need.")
        body.append("")

    # ── Database-backed sections ──────────────────────────────────────
    if db:
        # Config
        body.append("--- SETTINGS ---")
        try:
            from cdumm.storage.config import Config
            cfg = Config(db)
            theme = cfg.get("theme") or "(default)"
            lang = cfg.get("language") or "(default)"
            api_set = bool(cfg.get("nexus_api_key"))
            body.append(f"  Theme: {theme}")
            body.append(f"  Language: {lang}")
            body.append(f"  NexusMods API key configured: {api_set}")
        except Exception as e:
            body.append(f"  Error reading settings: {e}")
        body.append("")

        # Snapshot
        body.append("--- SNAPSHOT ---")
        snap_count = 0
        snap_created: str | None = None
        try:
            row = db.connection.execute(
                "SELECT COUNT(*), MAX(created_at) FROM snapshots").fetchone()
            snap_count, snap_created = (row[0] or 0), row[1]
            body.append(f"  Files tracked: {snap_count}")
            body.append(f"  Created: {snap_created or '(never)'}")
        except Exception as e:
            body.append(f"  Error: {e}")
        body.append("")

        if snap_count == 0:
            tldr_flags.append(
                "No snapshot present — Rescan Game Files before applying mods.")

        # Snapshot staleness check (game exe modified after snapshot?)
        try:
            if snap_created and game_exe and game_exe.exists():
                snap_dt = datetime.fromisoformat(
                    (snap_created or "").replace("T", " ").split(".")[0])
                exe_dt = datetime.fromtimestamp(game_exe.stat().st_mtime)
                if exe_dt > snap_dt:
                    tldr_flags.append(
                        f"Game exe was modified ({exe_dt:%Y-%m-%d %H:%M}) after "
                        f"your snapshot ({snap_dt:%Y-%m-%d %H:%M}) — game likely "
                        "updated or verified. Revert mods and Rescan Game Files.")
        except Exception:
            pass

        # Verify state (size-only, fast)
        body.append("--- VERIFY STATE (size-only, fast check) ---")
        if game_dir:
            vs = _verify_fast(db, game_dir)
            body.append(f"  Tracked: {vs['total']}")
            body.append(f"  Match snapshot (likely vanilla): {vs['vanilla']}")
            body.append(f"  Differ from snapshot (modded / unknown): {vs['modded']}")
            body.append(f"  Missing on disk: {vs['missing']}")
            if vs["missing"] > 0:
                tldr_flags.append(
                    f"{vs['missing']} file(s) tracked by the snapshot are "
                    "missing on disk — reverify via Steam.")
        else:
            body.append("  (game dir unknown)")
        body.append("")

        # PAZ Mods
        body.append("--- PAZ MODS ---")
        try:
            deltas_dir = (game_dir / "CDMods" / "deltas") if game_dir else None
            vanilla_dir = (game_dir / "CDMods" / "vanilla") if game_dir else None
            cursor = db.connection.execute(
                "SELECT id, name, enabled, priority, version, nexus_mod_id, "
                "import_date, configurable, source_path, applied, drop_name "
                "FROM mods WHERE mod_type = 'paz' ORDER BY priority")
            mods = cursor.fetchall()
            if not mods:
                body.append("  (no PAZ mods installed)")
            else:
                body.append(f"  Count: {len(mods)}")
                zero_delta_enabled = 0
                missing_source = 0
                for (mid, name, enabled, prio, ver, nexus_id,
                     import_date, configurable, source_path, applied,
                     drop_name) in mods:
                    state = "ON " if enabled else "OFF"
                    was_applied = " applied" if applied else ""
                    cfg_flag = " configurable" if configurable else ""
                    ver_str = _render_version(ver)
                    nx_str = f"nexus={nexus_id}" if nexus_id else "unlinked"
                    dc = db.connection.execute(
                        "SELECT COUNT(*) FROM mod_deltas WHERE mod_id = ?",
                        (mid,)).fetchone()[0]
                    dsize = _delta_size_for_mod(
                        mid, deltas_dir) if deltas_dir else 0
                    # Mod file paths (for vanilla-backup lookup)
                    file_paths = [r[0] for r in db.connection.execute(
                        "SELECT DISTINCT file_path FROM mod_deltas WHERE mod_id = ?",
                        (mid,)).fetchall()]
                    has_backup = (_has_vanilla_backup(file_paths, vanilla_dir)
                                   if vanilla_dir else False)
                    src_status = "?"
                    if source_path:
                        src_status = ("ok" if Path(source_path).exists()
                                       else "MISSING")
                    body.append(f"  #{prio:>3} [{state}] {name}")
                    body.append(
                        f"        id={mid}  {ver_str}  {nx_str}  "
                        f"deltas={dc} ({_bytes_human(dsize)})  "
                        f"imported={_format_import_date(import_date)}"
                        f"{was_applied}{cfg_flag}")
                    body.append(
                        f"        source={src_status}  "
                        f"vanilla_backup={'yes' if has_backup else 'no'}")
                    if drop_name:
                        body.append(f"        drop_name={drop_name}")
                    if dc == 0 and enabled:
                        body.append("        !! WARNING: enabled but has no deltas")
                        zero_delta_enabled += 1
                    if source_path and src_status == "MISSING":
                        missing_source += 1
                if zero_delta_enabled:
                    tldr_flags.append(
                        f"{zero_delta_enabled} enabled mod(s) have zero deltas — "
                        "they won't change anything in-game. Reimport them.")
                if missing_source:
                    tldr_flags.append(
                        f"{missing_source} mod(s) have source_path pointing to "
                        "a deleted file — preset re-picking won't work for those.")
        except Exception as e:
            body.append(f"  Error reading mods: {e}")
        body.append("")

        # ASI Plugins
        body.append("--- ASI PLUGINS ---")
        try:
            cursor = db.connection.execute(
                "SELECT name, version, nexus_mod_id, priority, install_date "
                "FROM asi_plugin_state ORDER BY priority")
            asi_rows = cursor.fetchall()
            if not asi_rows:
                body.append("  (none)")
            else:
                body.append(f"  Count: {len(asi_rows)}")
                for name, ver, nexus_id, prio, install_date in asi_rows:
                    ver_str = _render_version(ver)
                    nx_str = f"nexus={nexus_id}" if nexus_id else "unlinked"
                    body.append(
                        f"  {name:<34} {ver_str:<11} {nx_str:<14} "
                        f"installed={_format_import_date(install_date)}")
        except Exception as e:
            body.append(f"  Error reading ASI plugins: {e}")
        body.append("")

        # Conflicts (from conflicts table — semantic conflict resolver output)
        body.append("--- CONFLICTS ---")
        try:
            cursor = db.connection.execute(
                "SELECT c.level, c.file_path, c.explanation, "
                "ma.name, mb.name "
                "FROM conflicts c "
                "JOIN mods ma ON c.mod_a_id = ma.id "
                "JOIN mods mb ON c.mod_b_id = mb.id")
            conflicts = cursor.fetchall()
            if conflicts:
                for level, fpath, explanation, name_a, name_b in conflicts:
                    body.append(f"  [{level}] {name_a} vs {name_b}")
                    body.append(f"    File: {fpath}")
                    body.append(f"    {explanation}")
            else:
                body.append("  (none)")
        except Exception as e:
            body.append(f"  Error reading conflicts: {e}")
        body.append("")

        # File-level overlap (mods touching the same game file)
        body.append("--- FILE OVERLAPS (enabled mods, top 20) ---")
        overlaps = _file_level_conflicts(db, enabled_only=True, limit=20)
        if not overlaps:
            body.append("  (no two enabled mods share any game file)")
        else:
            for fp, n, names in overlaps:
                preview = ", ".join(names[:5]) + (
                    f" (+{len(names) - 5} more)" if len(names) > 5 else "")
                body.append(f"  [{n} mods] {fp}")
                body.append(f"      -> {preview}")
            if overlaps[0][1] >= 3:
                tldr_flags.append(
                    f"Top overlap: {overlaps[0][1]} enabled mods all touch "
                    f"'{overlaps[0][0]}'. Whoever has the highest priority wins — "
                    "re-order if the wrong one is winning.")
        body.append("")

        # Installation checks — things outside CDUMM's own DB that affect behavior
        body.append("--- INSTALLATION CHECKS ---")
        bin64 = (game_dir / "bin64") if game_dir else None
        if bin64 and bin64.exists():
            body.append(f"  bin64: {bin64}")
            loader_names = ("winmm.dll", "dinput8.dll", "version.dll",
                             "xinput1_3.dll", "d3d11.dll")
            loaders_present = [n for n in loader_names if (bin64 / n).exists()]
            if loaders_present:
                body.append(f"  ASI loader DLL(s): {', '.join(loaders_present)}")
            else:
                body.append("  ASI loader DLL(s): NONE FOUND")
                # Only flag as critical if user has ASI plugins
                try:
                    asi_n = db.connection.execute(
                        "SELECT COUNT(*) FROM asi_plugin_state").fetchone()[0]
                except Exception:
                    asi_n = 0
                if asi_n:
                    tldr_flags.append(
                        f"{asi_n} ASI plugin(s) are installed but no ASI loader "
                        "DLL is in bin64/ — ASI mods will not run. Install "
                        "Ultimate ASI Loader (winmm.dll) into bin64/.")
            # Count installed .asi files
            asi_files = list(bin64.glob("*.asi"))
            body.append(f"  .asi files in bin64: {len(asi_files)}")
        else:
            body.append("  bin64: (not present)")
            tldr_flags.append(
                "bin64/ folder missing — game install is incomplete or the "
                "game_dir is wrong.")

        # Implicit-metadata presence
        if game_dir:
            meta_files = {
                "meta/0.papgt": (game_dir / "meta" / "0.papgt").exists(),
                "meta/0.pathc": (game_dir / "meta" / "0.pathc").exists(),
            }
            for k, v in meta_files.items():
                body.append(f"  {k}: {'present' if v else 'MISSING'}")
            if not all(meta_files.values()):
                tldr_flags.append(
                    "Required meta/ file is missing on disk — the game will "
                    "crash at launch. Run Steam verify.")
        body.append("")

        # Health checks — mod-state issues that commonly cause bug reports
        body.append("--- HEALTH CHECKS ---")
        issues_found = False

        # Current game hash for outdated detection
        current_hash: str | None = None
        try:
            from cdumm.engine.version_detector import detect_game_version
            current_hash = detect_game_version(game_dir) if game_dir else None
        except Exception:
            current_hash = None

        # Enabled mods with zero deltas
        try:
            rows = db.connection.execute(
                "SELECT m.name FROM mods m "
                "WHERE m.enabled = 1 AND m.mod_type='paz' "
                "AND NOT EXISTS (SELECT 1 FROM mod_deltas md WHERE md.mod_id = m.id)"
            ).fetchall()
            if rows:
                issues_found = True
                body.append(f"  ! {len(rows)} enabled mod(s) have no deltas "
                             "(won't change anything in-game):")
                for (n,) in rows:
                    body.append(f"      - {n}")
        except Exception as e:
            body.append(f"  (zero-delta scan failed: {e})")

        # Outdated mods
        outdated = _outdated_mods(db, current_hash)
        if outdated:
            issues_found = True
            body.append(f"  ! {len(outdated)} enabled mod(s) target a different "
                         "game version (probably won't apply correctly):")
            for mid, name, mod_hash in outdated:
                short_hash = (mod_hash[:12] + "...") if mod_hash else "?"
                body.append(f"      - {name} (stored hash {short_hash})")
            tldr_flags.append(
                f"{len(outdated)} enabled mod(s) target an older game version. "
                "Either force-apply with risk or wait for mod author to update.")

        # Missing snapshot
        if snap_count == 0:
            issues_found = True
            body.append("  ! No snapshot present — Rescan Game Files before "
                         "applying mods")

        # Duplicate NexusMods links
        dupes = _duplicate_nexus_links(db)
        if dupes:
            body.append(f"  - {len(dupes)} NexusMods link(s) reused by "
                         "multiple imported mods (variants from the same page):")
            for nexus_id, names in dupes:
                body.append(f"      nexus={nexus_id}: {', '.join(names)}")

        if not issues_found and not dupes:
            body.append("  All checks passed.")
            tldr_ok.append("All mod-state health checks passed")
        body.append("")

        # Recent activity (structured events)
        body.append("--- RECENT ACTIVITY (last 20) ---")
        try:
            cursor = db.connection.execute(
                "SELECT timestamp, category, message, detail "
                "FROM activity_log ORDER BY id DESC LIMIT 20")
            events = list(cursor.fetchall())
            if not events:
                body.append("  (empty)")
            else:
                for ts, cat, msg, detail in reversed(events):
                    ts_short = ts[:16] if ts and len(ts) >= 16 else (ts or "")
                    body.append(f"  {ts_short}  [{cat}] {msg}")
                    if detail:
                        d = detail.replace("\n", " ").strip()
                        if len(d) > 140:
                            d = d[:137] + "..."
                        if d:
                            body.append(f"      -> {d}")
        except Exception as e:
            body.append(f"  Error reading activity log: {e}")
        body.append("")

    # ── Log tail (filtered) ────────────────────────────────────────────
    body.append("--- LOG (last 50 relevant lines) ---")
    if app_data_dir:
        log_path = app_data_dir / "cdumm.log"
        if log_path.exists():
            try:
                raw = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                relevant = [ln for ln in raw if _is_relevant_log_line(ln)]
                tail = relevant[-50:] if len(relevant) > 50 else relevant
                if not tail:
                    body.append("  (no relevant lines in the last run)")
                for ll in tail:
                    body.append(f"  {ll}")
            except Exception as e:
                body.append(f"  Error reading log: {e}")
        else:
            body.append("  (log file not found)")
    body.append("")

    # ── Crash trace (if last session actually recorded a trace) ───────
    # We only emit this section and flag it in the TL;DR when the trace
    # file exists AND has non-whitespace content — an empty file left over
    # from a previous session shouldn't claim there was a crash.
    if app_data_dir:
        trace_path = app_data_dir / "crash_trace.txt"
        if trace_path.exists():
            try:
                text = trace_path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                text = ""
                body.append("--- CRASH TRACE (previous session) ---")
                body.append(f"  Error reading crash trace: {e}")
                body.append("")
                tldr_flags.append(
                    "crash_trace.txt exists but couldn't be read — check "
                    "file permissions on the CDUMM app-data folder.")
            else:
                if text.strip():
                    tldr_flags.append(
                        "Previous session crashed — crash trace is included "
                        "at the bottom of this report.")
                    body.append("--- CRASH TRACE (previous session) ---")
                    if len(text) > 4000:
                        text = text[-4000:]
                        body.append("  (truncated — showing last 4000 chars)")
                    for ll in text.splitlines():
                        body.append(f"  {ll}")
                    body.append("")

    # ── Assemble header + TL;DR + body ────────────────────────────────
    out: list[str] = []
    out.append("=" * 60)
    out.append("CRIMSON DESERT ULTIMATE MODS MANAGER -- BUG REPORT")
    out.append("=" * 60)
    out.append(f"Generated: {now}")
    out.append(f"App Version: {APP_VERSION}")
    out.append("")

    # TL;DR
    out.append("--- TL;DR ---")
    if tldr_flags:
        out.append(f"  {len(tldr_flags)} issue(s) detected:")
        for i, f in enumerate(tldr_flags, 1):
            out.append(f"    {i}. {f}")
    else:
        out.append("  No red flags detected in automatic scan.")
    if tldr_ok:
        out.append("  OK:")
        for m in tldr_ok:
            out.append(f"    - {m}")
    out.append("")

    out.extend(body)
    out.append("=" * 60)
    out.append("END OF BUG REPORT")
    out.append("=" * 60)
    return "\n".join(out)


class BugReportDialog(MessageBoxBase):
    """Fluent-style bug report dialog."""

    def __init__(self, report_text: str, parent=None, is_crash: bool = False) -> None:
        super().__init__(parent)
        self._base_report = report_text

        self.titleLabel = SubtitleLabel(tr("bug.title"))
        self.viewLayout.addWidget(self.titleLabel)

        if is_crash:
            desc = BodyLabel(
                "The app didn't close normally last time. Please describe what "
                "you were doing when it happened, then copy or save this report."
            )
        else:
            desc = BodyLabel(
                "Describe the problem below, then copy or save the report.\n"
                "Attach it to your Nexus Mods bug report page."
            )
        desc.setWordWrap(True)
        self.viewLayout.addWidget(desc)

        # Severity
        sev_row = QHBoxLayout()
        sev_row.addWidget(CaptionLabel(tr("bug.severity")))
        self._severity = ComboBox()
        self._severity.addItems([
            tr("bug.crash"), tr("bug.wrong"),
            tr("bug.visual"), tr("bug.other"),
        ])
        if is_crash:
            self._severity.setCurrentIndex(0)
        self._severity.setFixedWidth(220)
        sev_row.addWidget(self._severity)
        sev_row.addStretch()
        self.viewLayout.addLayout(sev_row)

        # Theme-aware QTextEdit styling (plain Qt, not qfluentwidgets)
        from qfluentwidgets import isDarkTheme
        if isDarkTheme():
            _te_style = ("QTextEdit { background: #1C2028; color: #E2E8F0; "
                         "border: 1px solid #2D3340; border-radius: 6px; padding: 8px; }")
        else:
            _te_style = ("QTextEdit { background: #FAFBFC; color: #1A202C; "
                         "border: 1px solid #E2E8F0; border-radius: 6px; padding: 8px; }")

        # User description field
        self.viewLayout.addWidget(CaptionLabel(tr("bug.what_happened")))
        self._desc_edit = QTextEdit()
        self._desc_edit.setMaximumHeight(80)
        self._desc_edit.setPlaceholderText(tr("bug.placeholder"))
        self._desc_edit.setStyleSheet(_te_style)
        self.viewLayout.addWidget(self._desc_edit)

        # Report preview
        self.viewLayout.addWidget(CaptionLabel(tr("bug.preview")))
        self._text_edit = QTextEdit()
        self._text_edit.setReadOnly(True)
        self._text_edit.setPlainText(report_text)
        self._text_edit.setFontFamily("Consolas")
        self._text_edit.setMinimumHeight(250)
        self._text_edit.setStyleSheet(_te_style)
        self.viewLayout.addWidget(self._text_edit)

        # Update preview when user types or changes severity
        self._severity.currentTextChanged.connect(lambda _: self._update_preview())
        self._desc_edit.textChanged.connect(self._update_preview)

        # Action buttons
        btn_row = QHBoxLayout()

        copy_btn = PrimaryPushButton(tr("bug.copy"))
        copy_btn.clicked.connect(self._copy)
        btn_row.addWidget(copy_btn)

        save_btn = PushButton(tr("bug.save"))
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)

        btn_row.addStretch()
        self.viewLayout.addLayout(btn_row)

        # Override default buttons
        self.yesButton.setText(tr("main.close"))
        self.cancelButton.hide()

        self.widget.setMinimumWidth(700)

    def _update_preview(self) -> None:
        self._text_edit.setPlainText(self._get_full_report())

    def _get_full_report(self) -> str:
        severity = self._severity.currentText()
        desc = self._desc_edit.toPlainText().strip()
        header = f"--- SEVERITY: {severity} ---\n"
        if desc:
            header += f"\n--- USER DESCRIPTION ---\n{desc}\n"
        header += "\n"
        return header + self._base_report

    def _copy(self) -> None:
        clipboard = QApplication.clipboard()
        clipboard.setText(self._get_full_report())
        InfoBar.success(
            title=tr("main.copied"),
            content=tr("bug.copied"),
            duration=3000, position=InfoBarPosition.TOP, parent=self,
        )

    def _save(self) -> None:
        from cdumm.storage.config import default_export_dir
        default_name = (
            f"cdumm_bug_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
        default_path = default_export_dir(getattr(self, "_db", None)) / default_name
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Bug Report",
            str(default_path),
            "Text Files (*.txt)",
        )
        if path:
            Path(path).write_text(self._get_full_report(), encoding="utf-8")
            InfoBar.success(
                title=tr("main.saved"),
                content=tr("bug.saved", path=path),
                duration=4000, position=InfoBarPosition.TOP, parent=self,
            )
