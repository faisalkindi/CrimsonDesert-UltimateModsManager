"""Mod diagnostic analyzer — comprehensive analysis for any mod.

Inspects a mod archive/folder and produces a human-readable report
covering ALL possible failure modes. Designed for both proactive
inspection (Inspect Mod tool) and reactive diagnosis (import failure).

The report is suitable for sharing with mod authors.
"""

import json
import logging
import os
import struct
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)


def diagnose_mod(mod_path: Path, game_dir: Path, db_path: Path,
                 original_error: str = "") -> str:
    """Analyze a mod and return a comprehensive diagnostic report."""
    sections = []
    _s = sections.append

    _s("=" * 60)
    _s(f"DIAGNOSTIC REPORT: {mod_path.name}")
    _s("=" * 60)
    if original_error:
        _s(f"Import error: {original_error}")
    _s("")

    # ── 1. File basics ────────────────────────────────────────────
    if not mod_path.exists():
        _s("ISSUE: File/folder does not exist.")
        return "\n".join(sections)

    if mod_path.is_file():
        size_mb = mod_path.stat().st_size / 1048576
        _s(f"Type: File ({mod_path.suffix or 'no extension'})")
        _s(f"Size: {size_mb:.1f} MB")

        # RAR check — supported via 7-Zip
        if mod_path.suffix.lower() == ".rar":
            from cdumm.engine.import_handler import _find_7z
            if _find_7z():
                _s("")
                _s("RAR format detected. 7-Zip found — import will extract automatically.")
            else:
                _s("")
                _s("WARNING: RAR format detected but 7-Zip is not installed.")
                _s("FIX: Install 7-Zip from https://7-zip.org,")
                _s("     or extract the .rar and drop the folder directly.")

        # Try to open as zip
        if mod_path.suffix.lower() in (".zip", ".7z", ""):
            _s("")
            _diagnose_archive(mod_path, game_dir, db_path, sections)
    elif mod_path.is_dir():
        files = list(mod_path.rglob("*"))
        file_count = sum(1 for f in files if f.is_file())
        _s(f"Type: Folder")
        _s(f"Files: {file_count}")
        _s("")
        _diagnose_folder_contents(mod_path, game_dir, db_path, sections)
    else:
        _s("ISSUE: Not a recognized file or folder.")

    return "\n".join(sections)


# ── Archive analysis ──────────────────────────────────────────────────

def _diagnose_archive(mod_path: Path, game_dir: Path, db_path: Path,
                      sections: list[str]) -> None:
    _s = sections.append

    # Validate archive
    if mod_path.suffix.lower() == ".7z":
        _s("Archive format: 7-Zip")
        try:
            import py7zr
            with py7zr.SevenZipFile(mod_path) as z:
                names = z.getnames()
            _s(f"Contents: {len(names)} file(s)")
        except ImportError:
            _s("Cannot inspect .7z contents (py7zr not available).")
            _s("The mod will be extracted during import.")
            return
        except Exception as e:
            _s(f"ISSUE: Cannot read .7z archive: {e}")
            _s("FIX: Archive may be corrupted. Re-download it.")
            return
    else:
        try:
            with zipfile.ZipFile(mod_path) as zf:
                names = zf.namelist()
            _s(f"Archive format: ZIP (valid)")
            _s(f"Contents: {len(names)} file(s)")
        except zipfile.BadZipFile:
            _s("ISSUE: Not a valid ZIP file (corrupted or wrong format).")
            _s("FIX: Re-download the file, or if it's a .rar renamed")
            _s("     to .zip, extract it properly first.")
            return
        except Exception as e:
            _s(f"ISSUE: Cannot read archive: {e}")
            return

    if not names:
        _s("ISSUE: Archive is empty.")
        return

    _s("")

    # ── File structure analysis ───────────────────────────────────
    _s("--- File Structure ---")
    _categorize_contents(names, sections)
    _s("")

    # ── Nesting check ─────────────────────────────────────────────
    _check_nesting(names, sections)

    # ── Format detection ──────────────────────────────────────────
    _s("--- Format Detection ---")
    detected = []

    # Check for JSON patch
    json_files = [n for n in names if n.lower().endswith(".json")
                  and not n.startswith("__MACOSX")]
    for jf in json_files:
        try:
            with zipfile.ZipFile(mod_path) as zf:
                with zf.open(jf) as f:
                    data = json.load(f)
            if isinstance(data, dict) and "patches" in data:
                detected.append("json_patch")
                _s(f"Detected: JSON Patch mod ({jf})")
                _diagnose_json_patch(data, jf, game_dir, sections)
            elif isinstance(data, dict) and "files_dir" in data:
                detected.append("crimson_browser")
                _s(f"Detected: Crimson Browser mod ({jf})")
                _diagnose_cb_manifest(data, jf, names, sections)
            elif isinstance(data, dict) and jf.lower().endswith(("manifest.json", "modinfo.json")):
                _s(f"Found manifest: {jf}")
                _diagnose_manifest(data, jf, names, sections)
        except Exception:
            pass

    # Check for PAZ directory structure
    numbered_dirs = set()
    for n in names:
        parts = n.split("/")
        if len(parts) >= 2 and parts[0].isdigit() and len(parts[0]) == 4:
            numbered_dirs.add(parts[0])
    if numbered_dirs:
        detected.append("paz_dirs")
        _s(f"Detected: PAZ directory mod (dirs: {sorted(numbered_dirs)})")
        _diagnose_paz_dirs(numbered_dirs, names, game_dir, sections)

    # Check for ASI/DLL
    asi_files = [n for n in names if n.lower().endswith((".asi", ".dll", ".ini"))
                 and not n.startswith("__MACOSX")]
    if asi_files:
        detected.append("asi")
        _s(f"Detected: ASI plugin ({', '.join(asi_files[:5])})")
        _diagnose_asi(asi_files, game_dir, sections)

    # Check for scripts
    script_files = [n for n in names if n.lower().endswith((".bat", ".py"))
                    and not n.startswith("__MACOSX")]
    if script_files:
        detected.append("script")
        _s(f"Detected: Script mod ({', '.join(script_files[:5])})")

    # Check for bsdiff
    bsdiff_files = [n for n in names if n.lower().endswith((".bsdiff", ".xdelta"))]
    if bsdiff_files:
        detected.append("bsdiff")
        _s(f"Detected: Binary patch ({', '.join(bsdiff_files[:5])})")

    # Check for loose game files (xml, css, etc. without numbered dirs)
    loose_game = [n for n in names if n.lower().endswith(
        (".xml", ".css", ".pabgb", ".paac", ".dds"))
        and not n.startswith("__MACOSX")]
    if loose_game and "paz_dirs" not in detected:
        detected.append("loose_files")
        _s(f"Detected: Loose game files ({len(loose_game)} files)")
        _s("  These files may need a Crimson Browser manifest or")
        _s("  numbered PAZ directory structure to import correctly.")

    if not detected:
        _s("ISSUE: No recognized mod format detected.")
        _s("")
        _s("Expected one of:")
        _s("  - NNNN/ directories with .paz/.pamt files (PAZ mod)")
        _s("  - .json file with 'patches' array (JSON patch mod)")
        _s("  - manifest.json with 'files_dir' (Crimson Browser mod)")
        _s("  - .asi/.dll files (ASI plugin)")
        _s("  - .bat/.py files (Script mod)")
        _s("")
        _s("FIX: The mod may use a format CDUMM doesn't support,")
        _s("     or the archive structure may be wrong.")

    _s("")

    # ── Game version compatibility ────────────────────────────────
    _check_game_version(game_dir, db_path, sections)


# ── Content categorization ────────────────────────────────────────────

def _categorize_contents(names: list[str], sections: list[str]) -> None:
    _s = sections.append
    extensions = {}
    for n in names:
        if n.endswith("/"):
            continue
        ext = os.path.splitext(n)[1].lower() or "(no extension)"
        extensions[ext] = extensions.get(ext, 0) + 1

    for ext, count in sorted(extensions.items(), key=lambda x: -x[1])[:10]:
        _s(f"  {ext}: {count} file(s)")
    if len(extensions) > 10:
        _s(f"  ... and {len(extensions) - 10} more file types")

    # Show top-level structure
    top_level = set()
    for n in names:
        parts = n.split("/")
        if parts[0]:
            top_level.add(parts[0] + ("/" if len(parts) > 1 else ""))
    if len(top_level) <= 10:
        _s("Top-level entries:")
        for t in sorted(top_level):
            _s(f"  {t}")


def _check_nesting(names: list[str], sections: list[str]) -> None:
    """Check for excessive nesting or duplicate folder names."""
    _s = sections.append
    # Check for nested zips
    inner_zips = [n for n in names if n.lower().endswith((".zip", ".rar", ".7z"))
                  and not n.startswith("__MACOSX")]
    if inner_zips:
        _s("WARNING: Archive contains nested archives:")
        for iz in inner_zips[:5]:
            _s(f"  {iz}")
        _s("  These won't be extracted automatically.")
        _s("")

    # Check for __MACOSX junk
    macosx = [n for n in names if n.startswith("__MACOSX")]
    if macosx:
        _s(f"NOTE: Contains {len(macosx)} macOS metadata files (__MACOSX/).")
        _s("  These are harmless and will be ignored.")
        _s("")

    # Check for double-nesting (common zip extraction artifact)
    dirs = set()
    for n in names:
        parts = n.split("/")
        if len(parts) >= 3 and parts[0] == parts[1]:
            dirs.add(parts[0])
    if dirs:
        _s("WARNING: Double-nested folders detected:")
        for d in sorted(dirs)[:3]:
            _s(f"  {d}/{d}/...")
        _s("  This is common when a zip extracts into a folder of the same name.")
        _s("")


# ── JSON patch diagnosis ──────────────────────────────────────────────

def _diagnose_json_patch(data: dict, source: str, game_dir: Path,
                         sections: list[str]) -> None:
    _s = sections.append

    # Show modinfo
    modinfo = data.get("modinfo", {})
    if isinstance(modinfo, dict):
        for k in ("title", "version", "author", "description"):
            if k in modinfo:
                _s(f"  {k}: {modinfo[k]}")

    patches = data.get("patches", [])
    presets = data.get("presets", [])
    fmt = data.get("format", "unknown")
    _s(f"  Format version: {fmt}")
    _s(f"  Patches: {len(patches)} game file(s)")
    if presets:
        _s(f"  Presets: {len(presets)}")
    _s("")

    if not patches:
        _s("  ISSUE: patches array is empty — nothing to apply.")
        return

    # Validate each patch
    for i, patch in enumerate(patches):
        game_file = patch.get("game_file", "")
        changes = patch.get("changes", [])
        desc = patch.get("description", "")

        _s(f"  Patch {i+1}: {game_file}")
        if desc:
            _s(f"    Description: {desc}")
        _s(f"    Changes: {len(changes)}")

        if not game_file:
            _s("    ISSUE: Missing 'game_file' field.")
            continue

        if not changes:
            _s("    ISSUE: No changes defined for this patch.")
            continue

        # Validate change structure
        bad_changes = []
        for j, c in enumerate(changes):
            if not isinstance(c, dict):
                bad_changes.append(f"Change {j+1}: not a dict")
            elif "entry" not in c:
                bad_changes.append(f"Change {j+1}: missing 'entry' key")
        if bad_changes:
            for bc in bad_changes[:3]:
                _s(f"    ISSUE: {bc}")
            if len(bad_changes) > 3:
                _s(f"    ... and {len(bad_changes) - 3} more invalid changes")

        # Check target exists in PAMTs
        _check_game_file_exists(game_file, game_dir, sections, indent="    ")
        _s("")

    # If any patches have original_bytes, verify against current game files
    has_any_originals = any(
        "original" in c for p in patches for c in p.get("changes", [])
        if isinstance(c, dict))
    if has_any_originals:
        try:
            _verify_json_patch_bytes(data, game_dir, sections)
        except Exception as e:
            _s(f"  Byte verification error: {e}")
            _s("")


def _check_game_file_exists(game_file: str, game_dir: Path,
                            sections: list[str], indent: str = "  ") -> None:
    """Check if a game file path exists in any PAMT index."""
    _s = sections.append
    from cdumm.archive.paz_parse import parse_pamt

    gf_lower = game_file.lower()
    gf_basename = gf_lower.rsplit("/", 1)[-1]
    search_stem = gf_basename.split(".")[0]

    basename_matches = []
    fuzzy_matches = []

    for d in sorted(game_dir.iterdir()):
        if not d.is_dir() or not d.name.isdigit() or len(d.name) != 4:
            continue
        pamt = d / "0.pamt"
        if not pamt.exists():
            continue
        try:
            entries = parse_pamt(str(pamt), paz_dir=str(d))
            for e in entries:
                ep = e.path.lower()
                if ep == gf_lower:
                    _s(f"{indent}Status: FOUND in {d.name}/ (exact match)")
                    _s(f"{indent}  PAZ: {os.path.basename(e.paz_file)}, "
                       f"offset: {e.offset}, comp: {e.comp_size}, "
                       f"orig: {e.orig_size}, type: 0x{(e.flags >> 16) & 0xF:02X}")
                    return
                if gf_lower.endswith("/" + ep) or ep.endswith("/" + gf_lower):
                    _s(f"{indent}Status: FOUND in {d.name}/ (path match: {e.path})")
                    return
                bn = ep.rsplit("/", 1)[-1]
                if bn == gf_basename:
                    basename_matches.append(f"{d.name}/{e.path}")
                elif search_stem in ep:
                    fuzzy_matches.append(f"{d.name}/{e.path}")
        except Exception:
            pass

    if basename_matches:
        _s(f"{indent}Status: NOT FOUND (but basename matches exist):")
        for m in basename_matches[:5]:
            _s(f"{indent}  -> {m}")
        _s(f"{indent}SUGGESTION: The mod may need to use one of these paths instead.")
    elif fuzzy_matches:
        _s(f"{indent}Status: NOT FOUND. Similar entries:")
        for m in fuzzy_matches[:5]:
            _s(f"{indent}  -> {m}")
        if len(fuzzy_matches) > 5:
            _s(f"{indent}  ... and {len(fuzzy_matches) - 5} more")
    else:
        _s(f"{indent}Status: NOT FOUND in any PAZ archive")
        _s(f"{indent}This file does not exist in the current game version.")
        _s(f"{indent}The mod may be built for a different game version")
        _s(f"{indent}or the file path is incorrect.")


def _verify_json_patch_bytes(data: dict, game_dir: Path,
                             sections: list[str]) -> None:
    """Verify JSON patch original_bytes match current game files.

    If a game update changed the target file, the expected bytes won't match.
    This is the #1 cause of mod incompatibility after updates.
    """
    _s = sections.append
    from cdumm.archive.paz_parse import parse_pamt
    from cdumm.engine.json_patch_handler import _find_pamt_entry, decompress_entry

    _s("--- Byte Verification ---")
    patches = data.get("patches", [])
    verified = 0
    mismatched = 0
    skipped = 0

    for patch in patches:
        game_file = patch.get("game_file", "")
        changes = patch.get("changes", [])
        if not game_file or not changes:
            continue

        # Find the PAMT entry
        try:
            entry = _find_pamt_entry(game_file, game_dir)
        except Exception:
            entry = None

        if entry is None:
            _s(f"  {game_file}: skipped (not found in PAMTs)")
            skipped += 1
            continue

        # Try to extract and verify bytes
        try:
            raw = open(entry.paz_file, 'rb').read()
            chunk = raw[entry.offset:entry.offset + entry.comp_size]
            content = decompress_entry(chunk, entry)

            # Build name→offset map for v2 entry-anchored patches (characterinfo only).
            name_offsets: dict[str, int] | None = None
            needs_v2_index = any(c.get("entry") for c in changes)
            if needs_v2_index:
                try:
                    from cdumm.engine.json_patch_handler import (
                        _build_name_offsets_for_v2,
                    )
                    pabgh_file = game_file.rsplit(".", 1)[0] + ".pabgh"
                    pabgh_entry = _find_pamt_entry(pabgh_file, game_dir)
                    if pabgh_entry is not None:
                        pabgh_raw = open(pabgh_entry.paz_file, 'rb').read()
                        pabgh_chunk = pabgh_raw[pabgh_entry.offset:pabgh_entry.offset + pabgh_entry.comp_size]
                        pabgh_plain = decompress_entry(pabgh_chunk, pabgh_entry)
                        name_offsets = _build_name_offsets_for_v2(
                            game_file, bytes(content), pabgh_plain)
                except Exception:
                    name_offsets = None

            unsupported_v2 = 0
            v2_resolved = 0
            v2_name_missing: list[str] = []
            for change in changes:
                original = change.get("original")
                raw_offset = change.get("offset")
                entry_name = change.get("entry", "")
                record_key = change.get("record_key")
                rel_offset = change.get("rel_offset", change.get("relative_offset"))

                # Normalise original bytes — accept list[int] OR hex string.
                original_bytes: bytes | None = None
                if isinstance(original, list):
                    try:
                        original_bytes = bytes(original)
                    except (TypeError, ValueError):
                        original_bytes = None
                elif isinstance(original, str) and original:
                    try:
                        original_bytes = bytes.fromhex(original)
                    except ValueError:
                        original_bytes = None

                # Normalise offset — accept int, decimal string, or hex string.
                offset_int: int | None = None
                if isinstance(raw_offset, int):
                    offset_int = raw_offset
                elif isinstance(raw_offset, str) and raw_offset:
                    try:
                        offset_int = int(raw_offset, 0)
                    except ValueError:
                        try:
                            offset_int = int(raw_offset, 16)
                        except ValueError:
                            offset_int = None

                # v2 entry-anchored format: `entry` name + `rel_offset`.
                # Try to resolve through the name→offset map.
                if (offset_int is None and record_key is None and entry_name
                        and rel_offset is not None):
                    if name_offsets is not None:
                        body_off = name_offsets.get(str(entry_name))
                        if body_off is not None:
                            try:
                                rel_int = (int(rel_offset, 0)
                                           if isinstance(rel_offset, str)
                                           else int(rel_offset))
                            except (ValueError, TypeError):
                                rel_int = None
                            if rel_int is not None:
                                offset_int = body_off + rel_int
                                v2_resolved += 1
                        else:
                            v2_name_missing.append(str(entry_name))
                            continue
                    else:
                        unsupported_v2 += 1
                        continue

                if original_bytes is not None and offset_int is not None:
                    actual_bytes = bytes(content[offset_int:offset_int + len(original_bytes)])
                    if actual_bytes == original_bytes:
                        verified += 1
                    else:
                        mismatched += 1
                        exp_hex = original_bytes[:8].hex()
                        got_hex = actual_bytes[:8].hex()
                        exp_suffix = "..." if len(original_bytes) > 8 else ""
                        got_suffix = "..." if len(actual_bytes) > 8 else ""
                        _s(f"  MISMATCH in {game_file} at offset {offset_int}:")
                        _s(f"    Expected: {exp_hex}{exp_suffix}")
                        _s(f"    Actual:   {got_hex}{got_suffix}")
                        _s(f"    Entry: {entry_name}")
                        _s(f"    -> Game file has been updated. Mod needs new offsets.")
                else:
                    skipped += 1

            if unsupported_v2:
                _s(f"  {unsupported_v2} change(s) use v2 entry-anchored format for")
                _s(f"  a file type CDUMM doesn't have a name-resolver for yet.")
                _s(f"  (Currently: characterinfo.pabgb is supported; others TODO.)")
            if v2_name_missing:
                _s(f"  {len(v2_name_missing)} entry name(s) not found in the current")
                _s(f"  game's {game_file} — these records don't exist in your install:")
                for nm in v2_name_missing[:10]:
                    _s(f"    - {nm!r}")
                if len(v2_name_missing) > 10:
                    _s(f"    ... and {len(v2_name_missing) - 10} more")
                _s(f"  The mod targets a different game version OR uses fabricated")
                _s(f"  entry names. Ask the author to regenerate it against your build.")
            if v2_resolved:
                _s(f"  {v2_resolved} v2 entry-anchored change(s) resolved via name index.")
        except Exception as e:
            _s(f"  {game_file}: verification error — {e}")
            skipped += 1

    if verified > 0 or mismatched > 0:
        _s(f"  Summary: {verified} verified, {mismatched} mismatched, {skipped} skipped")
        if mismatched > 0:
            _s(f"  CONCLUSION: {mismatched} byte offset(s) don't match the current game.")
            _s(f"  This mod was likely built for a different game version.")
            _s(f"  The mod author needs to update the byte offsets.")
    elif skipped > 0:
        _s(f"  Could not verify byte offsets ({skipped} changes without original_bytes)")
    _s("")


# ── Crimson Browser manifest diagnosis ────────────────────────────────

def _diagnose_cb_manifest(data: dict, source: str, names: list[str],
                          sections: list[str]) -> None:
    _s = sections.append
    for k in ("id", "title", "author", "version", "format"):
        if k in data:
            _s(f"  {k}: {data[k]}")

    files_dir = data.get("files_dir", "files")
    base = source.rsplit("/", 1)[0] + "/" if "/" in source else ""
    prefix = base + files_dir + "/"
    matching = [n for n in names if n.startswith(prefix)]
    _s(f"  files_dir: '{files_dir}' -> {len(matching)} file(s) found")

    if not matching:
        _s(f"  ISSUE: No files found under '{files_dir}/' directory.")
        _s(f"  FIX: The files_dir in manifest.json may be wrong,")
        _s(f"       or the files are at a different path in the archive.")


def _diagnose_manifest(data: dict, source: str, names: list[str],
                       sections: list[str]) -> None:
    _s = sections.append
    for k in ("title", "author", "version", "description", "format", "id"):
        if k in data:
            _s(f"  {k}: {data[k]}")
    _s(f"  Keys: {', '.join(data.keys())}")


# ── PAZ directory structure diagnosis ─────────────────────────────────

def _diagnose_paz_dirs(numbered_dirs: set, names: list[str],
                       game_dir: Path, sections: list[str]) -> None:
    _s = sections.append

    # Known vanilla directories: 0000-0035 (with gaps like 0018)
    vanilla_max = 35
    has_meta = any(n.startswith("meta/") for n in names)

    for d in sorted(numbered_dirs):
        dir_num = int(d)
        paz_in_dir = [n for n in names if n.startswith(d + "/") and n.lower().endswith(".paz")]
        pamt_in_dir = [n for n in names if n.startswith(d + "/") and n.lower().endswith(".pamt")]
        other = [n for n in names if n.startswith(d + "/") and not n.endswith("/")
                 and not n.lower().endswith((".paz", ".pamt"))]

        game_paz_dir = game_dir / d
        exists = game_paz_dir.exists()

        _s(f"  {d}/: {len(paz_in_dir)} .paz, {len(pamt_in_dir)} .pamt, {len(other)} other")

        if dir_num > vanilla_max:
            _s(f"    NOTE: {d}/ is an OVERLAY directory (vanilla goes up to 0035).")
            _s(f"    This is a pre-compiled mod that uses the overlay system.")
            if not has_meta:
                _s(f"    WARNING: No meta/ directory found in the archive.")
                _s(f"    Pre-compiled mods need meta/0.papgt to register the overlay.")
        elif not exists:
            _s(f"    WARNING: Game directory {d}/ does not exist.")
            _s(f"    The mod may target a different game version.")

        if not paz_in_dir and not pamt_in_dir:
            _s(f"    NOTE: No .paz/.pamt files — may be a loose file mod.")

    if has_meta:
        _s(f"  meta/: Pre-compiled PAPGT included")
        papgt_files = [n for n in names if n.lower() == "meta/0.papgt"]
        if not papgt_files:
            _s(f"    WARNING: meta/ directory exists but 0.papgt not found.")
            _s(f"    The PAPGT file is required for the game to load the overlay.")

        if not paz_in_dir and not pamt_in_dir:
            _s(f"    NOTE: No .paz/.pamt files — may be a loose file mod.")


# ── ASI plugin diagnosis ──────────────────────────────────────────────

def _diagnose_asi(asi_files: list[str], game_dir: Path,
                  sections: list[str]) -> None:
    _s = sections.append
    bin64 = game_dir / "bin64"
    loader = bin64 / "winmm.dll"

    if not loader.exists():
        _s("  WARNING: ASI Loader (winmm.dll) not found in bin64/.")
        _s("  ASI mods won't work without it. Install via CDUMM's ASI page.")

    for af in asi_files[:10]:
        name = af.rsplit("/", 1)[-1]
        existing = bin64 / name
        if existing.exists():
            _s(f"  {name}: already installed in bin64/")
        else:
            _s(f"  {name}: not yet installed")


# ── Game version check ────────────────────────────────────────────────

def _check_game_version(game_dir: Path, db_path: Path,
                        sections: list[str]) -> None:
    _s = sections.append
    _s("--- Game Version ---")
    try:
        from cdumm.storage.database import Database
        db = Database(Path(db_path))
        db.initialize()

        # Check if snapshot exists
        snap_count = db.connection.execute(
            "SELECT COUNT(*) FROM snapshots").fetchone()[0]
        if snap_count == 0:
            _s("  No vanilla snapshot found.")
            _s("  Run 'Rescan' after verifying game files through Steam.")
        else:
            _s(f"  Vanilla snapshot: {snap_count} files indexed")

        # Check game version fingerprint
        from cdumm.storage.config import Config
        config = Config(db)
        fp = config.get("game_version_fingerprint")
        if fp:
            from cdumm.engine.version_detector import detect_game_version
            current = detect_game_version(game_dir)
            if current and current != fp:
                _s("  WARNING: Game has been updated since last snapshot.")
                _s("  Some mods may be incompatible. Consider rescanning.")
            elif current:
                _s("  Game version matches snapshot.")
        db.close()
    except Exception as e:
        _s(f"  Could not check game version: {e}")


# ── Folder analysis ──────────────────────────────────────────────────

def _diagnose_folder_contents(folder: Path, game_dir: Path, db_path: Path,
                              sections: list[str]) -> None:
    """Analyze a folder-based mod."""
    _s = sections.append
    all_files = [f for f in folder.rglob("*") if f.is_file()]

    # Categorize
    extensions = {}
    for f in all_files:
        ext = f.suffix.lower() or "(no extension)"
        extensions[ext] = extensions.get(ext, 0) + 1

    _s("--- File Structure ---")
    for ext, count in sorted(extensions.items(), key=lambda x: -x[1])[:10]:
        _s(f"  {ext}: {count} file(s)")
    _s("")

    # Check for JSON patches
    json_files = [f for f in all_files if f.suffix.lower() == ".json"]
    for jf in json_files:
        try:
            with open(jf, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and "patches" in data:
                _s("--- Format Detection ---")
                _s(f"Detected: JSON Patch mod ({jf.name})")
                _diagnose_json_patch(data, jf.name, game_dir, sections)
        except Exception:
            pass

    # Check for manifest/modinfo
    for name in ("manifest.json", "modinfo.json"):
        mf = folder / name
        if not mf.exists():
            # Search one level deep
            for sub in folder.iterdir():
                if sub.is_dir():
                    candidate = sub / name
                    if candidate.exists():
                        mf = candidate
                        break
        if mf.exists():
            try:
                with open(mf, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    _s(f"--- Format Detection ---")
                    _s(f"Found: {name}")
                    rel_names = [str(f.relative_to(folder)) for f in all_files]
                    if "files_dir" in data:
                        _s(f"Detected: Crimson Browser mod")
                        _diagnose_cb_manifest(data, str(mf.relative_to(folder)),
                                              rel_names, sections)
                    else:
                        _diagnose_manifest(data, str(mf.relative_to(folder)),
                                           rel_names, sections)
            except Exception:
                pass

    # Check for numbered directories
    numbered = [d for d in folder.iterdir()
                if d.is_dir() and d.name.isdigit() and len(d.name) == 4]
    if numbered:
        rel_names = [str(f.relative_to(folder)).replace("\\", "/") for f in all_files]
        _s("--- Format Detection ---")
        _s(f"Detected: PAZ directory mod ({[d.name for d in numbered]})")
        _diagnose_paz_dirs({d.name for d in numbered}, rel_names, game_dir, sections)

    _s("")
    _check_game_version(game_dir, db_path, sections)
