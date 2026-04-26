"""Corrupt-PAMT warnings must dedup per mod.

Bug report from Faisal 2026-04-26 (Richardker2545 + DerBambusbjoern +
Giony all hit it on Nexus): Enhanced Internal Graphics mod ships many
NNNN/0.paz directories. Each one triggers a separate parse_pamt
attempt in collect_paz_dir_overrides; each fails; each appends its own
copy of the same 'Mod X has a corrupt archive' warning. User sees the
SAME warning text repeated 30+ times in the InfoBar.

Fix: track which mod_ids have already produced a warning in this
collect call, and skip duplicates.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

from cdumm.engine.apply_engine import collect_paz_dir_overrides


def _make_db_with_corrupt_multi_dir_mod(tmp_path: Path) -> MagicMock:
    """Build a DB containing one mod that ships 5 NNNN/0.paz dirs,
    each with a corrupt 0.pamt that will fail parse_pamt."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE mods ("
        "id INTEGER PRIMARY KEY, name TEXT, mod_type TEXT, "
        "enabled INTEGER, priority INTEGER)"
    )
    conn.execute(
        "CREATE TABLE mod_deltas ("
        "id INTEGER PRIMARY KEY, mod_id INTEGER, file_path TEXT, "
        "delta_path TEXT)"
    )
    conn.execute(
        "INSERT INTO mods (id, name, mod_type, enabled, priority) "
        "VALUES (1, 'Graphics Mod', 'paz', 1, 50)"
    )
    # Five NNNN/0.paz directories, each with a corresponding 0.pamt
    # whose first bytes are garbage (parse_pamt will reject as
    # 'folder_size exceeds file size').
    for n, dn in enumerate(("0001", "0005", "0010", "0020", "0030"), 1):
        paz_path = tmp_path / f"{dn}_0.paz.newfile"
        pamt_path = tmp_path / f"{dn}_0.pamt.newfile"
        paz_path.write_bytes(b"\x00" * 32)
        # Corrupt PAMT: 300 bytes total but folder section claims 50MB.
        # Mirrors the user's actual screenshot.
        garbage = bytearray(300)
        # Plant a huge folder_size value where parse_pamt looks for it.
        garbage[16:20] = (50_331_648).to_bytes(4, "little")
        pamt_path.write_bytes(bytes(garbage))
        conn.execute(
            "INSERT INTO mod_deltas (mod_id, file_path, delta_path) "
            "VALUES (1, ?, ?)",
            (f"{dn}/0.paz", str(paz_path))
        )
        conn.execute(
            "INSERT INTO mod_deltas (mod_id, file_path, delta_path) "
            "VALUES (1, ?, ?)",
            (f"{dn}/0.pamt", str(pamt_path))
        )
    conn.commit()

    class _DBWrap:
        def __init__(self, c):
            self.connection = c

    return _DBWrap(conn)


def test_corrupt_pamt_warning_dedups_per_mod(tmp_path: Path) -> None:
    """One mod with 5 broken NNNN dirs must produce exactly ONE
    warning, not 5. The user only needs to know which mod is broken
    and what to do (reimport), not the same message five times."""
    db = _make_db_with_corrupt_multi_dir_mod(tmp_path)
    warnings: list[str] = []

    collect_paz_dir_overrides(db, warnings_out=warnings)

    graphics_warnings = [
        w for w in warnings if "Graphics Mod" in w]
    assert len(graphics_warnings) == 1, (
        f"expected exactly 1 warning for the broken mod, got "
        f"{len(graphics_warnings)}:\n"
        + "\n".join(f"  - {w}" for w in graphics_warnings))


def test_corrupt_pamt_warning_still_fires_for_distinct_mods(
        tmp_path: Path) -> None:
    """Dedup must be PER MOD, not global. Two different broken mods
    must each get their own warning so the user knows BOTH are broken."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE mods ("
        "id INTEGER PRIMARY KEY, name TEXT, mod_type TEXT, "
        "enabled INTEGER, priority INTEGER)"
    )
    conn.execute(
        "CREATE TABLE mod_deltas ("
        "id INTEGER PRIMARY KEY, mod_id INTEGER, file_path TEXT, "
        "delta_path TEXT)"
    )
    for mid, name in ((1, "ModA"), (2, "ModB")):
        conn.execute(
            "INSERT INTO mods (id, name, mod_type, enabled, priority) "
            "VALUES (?, ?, 'paz', 1, ?)",
            (mid, name, mid * 10)
        )
        for dn in ("0001", "0002"):
            paz_path = tmp_path / f"m{mid}_{dn}_0.paz"
            pamt_path = tmp_path / f"m{mid}_{dn}_0.pamt"
            paz_path.write_bytes(b"\x00" * 32)
            garbage = bytearray(300)
            garbage[16:20] = (50_331_648).to_bytes(4, "little")
            pamt_path.write_bytes(bytes(garbage))
            conn.execute(
                "INSERT INTO mod_deltas "
                "(mod_id, file_path, delta_path) VALUES (?, ?, ?)",
                (mid, f"{dn}/0.paz", str(paz_path))
            )
            conn.execute(
                "INSERT INTO mod_deltas "
                "(mod_id, file_path, delta_path) VALUES (?, ?, ?)",
                (mid, f"{dn}/0.pamt", str(pamt_path))
            )
    conn.commit()

    class _DBWrap:
        def __init__(self, c):
            self.connection = c

    warnings: list[str] = []
    collect_paz_dir_overrides(_DBWrap(conn), warnings_out=warnings)

    a_warns = [w for w in warnings if "ModA" in w]
    b_warns = [w for w in warnings if "ModB" in w]
    assert len(a_warns) == 1, (
        f"ModA should warn once, got {len(a_warns)}")
    assert len(b_warns) == 1, (
        f"ModB should warn once, got {len(b_warns)}")
