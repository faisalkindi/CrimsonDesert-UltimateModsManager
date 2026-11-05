"""A failed update or reimport must leave the installed JSON mod alone.

GitHub #479 (reported by AgentKush). ``import_json_as_entr`` with an
``existing_mod_id`` used to delete that mod's ``mod_deltas`` rows and its
``deltas/<id>`` folder up front, and every later failure exit then ran
``DELETE FROM mods WHERE id = ?`` on the EXISTING id. Foreign keys cascade,
so the mod's ``mod_config`` and profile rows went with it. The user saw
only the error, which read as if the old version were still installed.

These tests run on a synthetic game under ``tmp_path`` (no real game
folder): the PAMT lookup and the PAZ extract are replaced by a tiny fake
game made of one file per target name.

Each failure test installs v1 through the normal importer, adds a
``mod_config`` row, then runs an update that cannot succeed and asserts
the mods row, its mod_config row, its mod_deltas rows and its delta file
are all still there. The success tests pin the unchanged happy path:
a good update still replaces the old deltas on the same row.
"""
from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from cdumm.storage.database import Database

PLAIN = bytes(range(256))
TARGET = "gamedata/stamina.pabgb"
RENAMED = "gamedata/stamina_v2_only.pabgb"


@dataclass
class _FakeEntry:
    path: str
    paz_file: str
    offset: int = 100
    comp_size: int = 256
    orig_size: int = 256
    compression_type: int = 0
    flags: int = 0
    paz_index: int = 0
    encrypted: bool = False


class _FakeSnapshot:
    def get_file_hash(self, p):
        return None

    def get_all_files(self):
        return []


@pytest.fixture
def game(tmp_path, monkeypatch):
    """A synthetic game dir where only ``TARGET`` resolves."""
    from cdumm.engine import json_patch_handler as jph

    game_dir = tmp_path / "game"
    paz_dir = game_dir / "0008"
    paz_dir.mkdir(parents=True)
    paz = paz_dir / "0.paz"
    paz.write_bytes(b"\x00" * 16)

    def fake_find(game_file, gd):
        if game_file.lower() == TARGET:
            return _FakeEntry(path=TARGET, paz_file=str(paz))
        return None

    monkeypatch.setattr(jph, "_find_pamt_entry", fake_find)
    monkeypatch.setattr(jph, "_find_pamt_entries", lambda gf, gd: [])
    monkeypatch.setattr(jph, "_extract_from_paz", lambda entry, *a, **k: PLAIN)
    monkeypatch.setattr(
        "cdumm.engine.version_detector.detect_game_version", lambda gd: "v-test")
    return game_dir


def _patch(target: str, offset: int, original: int, patched: int,
           title: str = "Stamina", version: str = "1.0") -> dict:
    return {
        "modinfo": {"title": title, "version": version},
        "patches": [{
            "game_file": target,
            "changes": [{
                "offset": offset, "label": "x",
                "original": f"{original:02x}", "patched": f"{patched:02x}",
            }],
        }],
    }


def _zip(tmp_path: Path, name: str, data: dict) -> Path:
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("Stamina.json", json.dumps(data))
    return p


@pytest.fixture
def installed(tmp_path, game):
    """Install v1 for real and give it a mod_config row. Returns a dict."""
    from cdumm.engine.import_handler import import_from_zip

    db = Database(tmp_path / "cdumm.db")
    db.initialize()
    deltas = tmp_path / "deltas"
    deltas.mkdir()
    v1 = _zip(tmp_path, "v1.zip", _patch(TARGET, 10, 10, 0xAA))
    res = import_from_zip(v1, game, db, _FakeSnapshot(), deltas)
    assert res.error is None, res.error
    mod_id = db.connection.execute("SELECT id FROM mods").fetchone()[0]
    db.connection.execute(
        "INSERT INTO mod_config (mod_id, selected_labels) VALUES (?, ?)",
        (mod_id, '["picked"]'))
    db.connection.execute(
        "UPDATE mods SET enabled = 1, priority = 7 WHERE id = ?", (mod_id,))
    db.connection.commit()
    delta_rows = db.connection.execute(
        "SELECT delta_path FROM mod_deltas WHERE mod_id = ?", (mod_id,)
    ).fetchall()
    assert len(delta_rows) == 1
    delta_file = Path(delta_rows[0][0])
    assert delta_file.is_file()
    return {
        "db": db, "db_path": tmp_path / "cdumm.db", "mod_id": mod_id,
        "deltas": deltas, "game": game, "delta_file": delta_file,
        "delta_bytes": delta_file.read_bytes(), "tmp": tmp_path,
    }


def _assert_untouched(inst):
    db = inst["db"]
    mod_id = inst["mod_id"]
    row = db.connection.execute(
        "SELECT enabled, priority FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row == (1, 7), "mods row missing or changed"
    cfg = db.connection.execute(
        "SELECT selected_labels FROM mod_config WHERE mod_id = ?",
        (mod_id,)).fetchone()
    assert cfg == ('["picked"]',), "mod_config row missing or changed"
    rows = db.connection.execute(
        "SELECT delta_path FROM mod_deltas WHERE mod_id = ?", (mod_id,)
    ).fetchall()
    assert [r[0] for r in rows] == [str(inst["delta_file"])], (
        "mod_deltas rows changed")
    assert inst["delta_file"].is_file(), "delta file removed"
    assert inst["delta_file"].read_bytes() == inst["delta_bytes"], (
        "delta file rewritten")
    leftovers = [p.name for p in inst["deltas"].iterdir()
                 if p.name != str(mod_id)]
    assert leftovers == [], f"staging debris left in deltas dir: {leftovers}"


# ── import_from_zip ──────────────────────────────────────────────────


def test_zip_update_with_mismatched_original_keeps_mod(installed, game):
    """v2's ``original`` bytes are absent from the game: version mismatch."""
    from cdumm.engine.import_handler import import_from_zip

    v2 = _zip(installed["tmp"], "v2.zip", _patch(TARGET, 10, 0xEE, 0xBB))
    res = import_from_zip(v2, game, installed["db"], _FakeSnapshot(),
                          installed["deltas"],
                          existing_mod_id=installed["mod_id"])
    assert res.error and "incompatible" in res.error
    _assert_untouched(installed)


def test_zip_update_with_missing_target_keeps_mod(installed, game):
    """v2 targets a file the game does not have."""
    from cdumm.engine.import_handler import import_from_zip

    v2 = _zip(installed["tmp"], "v2.zip", _patch(RENAMED, 10, 10, 0xBB))
    res = import_from_zip(v2, game, installed["db"], _FakeSnapshot(),
                          installed["deltas"],
                          existing_mod_id=installed["mod_id"])
    assert res.error
    _assert_untouched(installed)


def test_zip_update_failure_message_says_old_version_stays(installed, game):
    from cdumm.engine.import_handler import import_from_zip

    v2 = _zip(installed["tmp"], "v2.zip", _patch(TARGET, 10, 0xEE, 0xBB))
    res = import_from_zip(v2, game, installed["db"], _FakeSnapshot(),
                          installed["deltas"],
                          existing_mod_id=installed["mod_id"])
    assert "previously installed version is still installed" in res.error


def test_zip_update_exception_mid_build_keeps_mod(installed, game, monkeypatch):
    """A crash while writing the new delta must not cost the old one."""
    from cdumm.engine.import_handler import import_from_zip

    def boom(content, metadata, delta_path):
        Path(delta_path).parent.mkdir(parents=True, exist_ok=True)
        Path(delta_path).write_bytes(b"half")
        raise OSError("disk full")

    monkeypatch.setattr("cdumm.engine.delta_engine.save_entry_delta", boom)
    v2 = _zip(installed["tmp"], "v2.zip", _patch(TARGET, 10, 10, 0xBB))
    with pytest.raises(OSError):
        import_from_zip(v2, game, installed["db"], _FakeSnapshot(),
                        installed["deltas"],
                        existing_mod_id=installed["mod_id"])
    installed["db"].connection.rollback()
    _assert_untouched(installed)


def test_zip_update_swap_failure_restores_old_deltas(
        installed, game, monkeypatch):
    """If moving the staged folder into place fails, the old one comes back."""
    import os

    from cdumm.engine import json_patch_handler as jph
    from cdumm.engine.import_handler import import_from_zip

    real_replace = os.replace
    calls = []

    def flaky_replace(src, dst):
        calls.append((Path(src).name, Path(dst).name))
        if Path(src).name.startswith(".staging-"):
            raise OSError("antivirus hold")
        return real_replace(src, dst)

    monkeypatch.setattr(jph.os, "replace", flaky_replace)
    v2 = _zip(installed["tmp"], "v2.zip", _patch(TARGET, 10, 10, 0xBB))
    with pytest.raises(OSError):
        import_from_zip(v2, game, installed["db"], _FakeSnapshot(),
                        installed["deltas"],
                        existing_mod_id=installed["mod_id"])
    assert calls, "the swap was never attempted"
    _assert_untouched(installed)


def test_zip_update_success_replaces_deltas_on_same_row(installed, game):
    from cdumm.engine.delta_engine import load_entry_delta
    from cdumm.engine.import_handler import import_from_zip

    v2 = _zip(installed["tmp"], "v2.zip", _patch(TARGET, 10, 10, 0xBB))
    res = import_from_zip(v2, game, installed["db"], _FakeSnapshot(),
                          installed["deltas"],
                          existing_mod_id=installed["mod_id"])
    assert res.error is None, res.error
    db = installed["db"]
    mod_id = installed["mod_id"]
    assert db.connection.execute("SELECT COUNT(*) FROM mods").fetchone()[0] == 1
    row = db.connection.execute(
        "SELECT enabled, priority, game_version_hash, json_source "
        "FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row[:3] == (1, 7, "v-test")
    assert db.connection.execute(
        "SELECT selected_labels FROM mod_config WHERE mod_id = ?",
        (mod_id,)).fetchone() == ('["picked"]',)
    rows = db.connection.execute(
        "SELECT delta_path FROM mod_deltas WHERE mod_id = ?", (mod_id,)
    ).fetchall()
    assert len(rows) == 1
    new_delta = Path(rows[0][0])
    assert new_delta == installed["delta_file"]
    content, _meta = load_entry_delta(new_delta)
    assert content[10] == 0xBB
    assert Path(row[3]).is_file(), "json_source must point at a real file"
    assert [p.name for p in installed["deltas"].iterdir()] == [str(mod_id)]


# ── import_from_json_patch on the stored json_source ─────────────────


def test_json_patch_update_with_renamed_target_keeps_mod(installed, game):
    from cdumm.engine.import_handler import import_from_json_patch

    db = installed["db"]
    src = Path(db.connection.execute(
        "SELECT json_source FROM mods WHERE id = ?",
        (installed["mod_id"],)).fetchone()[0])
    renamed = installed["tmp"] / "renamed.json"
    data = json.loads(src.read_text(encoding="utf-8"))
    data["patches"][0]["game_file"] = RENAMED
    renamed.write_text(json.dumps(data), encoding="utf-8")

    res = import_from_json_patch(renamed, game, db, _FakeSnapshot(),
                                 installed["deltas"],
                                 existing_mod_id=installed["mod_id"])
    assert res.error
    _assert_untouched(installed)


# ── worker_process ───────────────────────────────────────────────────


def _capture_emit(monkeypatch):
    from cdumm import worker_process as wp
    events: list[dict] = []
    monkeypatch.setattr(wp, "_emit", lambda ev: events.append(ev))
    return events


def test_run_import_with_existing_id_keeps_mod_on_failure(
        installed, game, monkeypatch):
    from cdumm import worker_process as wp

    events = _capture_emit(monkeypatch)
    v2 = _zip(installed["tmp"], "v2.zip", _patch(RENAMED, 10, 10, 0xBB))
    installed["db"].connection.commit()
    wp._run_import(str(v2), str(game), str(installed["db_path"]),
                   str(installed["deltas"]),
                   existing_mod_id=str(installed["mod_id"]))
    assert any(e.get("type") == "error" for e in events), events
    _assert_untouched(installed)


def test_reimport_batch_keeps_mod_when_target_renamed(
        installed, game, monkeypatch):
    from cdumm import worker_process as wp

    events = _capture_emit(monkeypatch)
    db = installed["db"]
    # A second, unrelated mod must be unaffected too.
    db.connection.execute(
        "INSERT INTO mods (name, mod_type, priority) VALUES ('other', 'paz', 1)")
    db.connection.commit()

    src = Path(db.connection.execute(
        "SELECT json_source FROM mods WHERE id = ?",
        (installed["mod_id"],)).fetchone()[0])
    data = json.loads(src.read_text(encoding="utf-8"))
    data["patches"][0]["game_file"] = RENAMED
    renamed = installed["tmp"] / "renamed.json"
    renamed.write_text(json.dumps(data), encoding="utf-8")
    plist = installed["tmp"] / "reimport.txt"
    plist.write_text(f"{installed['mod_id']}\t{renamed}\n", encoding="utf-8")

    wp._run_reimport_batch(str(plist), str(game), str(installed["db_path"]),
                           str(installed["deltas"]))
    items = [e for e in events if e.get("type") == "batch_item"]
    assert len(items) == 1 and items[0]["error"], events
    _assert_untouched(installed)
    assert db.connection.execute(
        "SELECT COUNT(*) FROM mods WHERE name = 'other'").fetchone()[0] == 1


def test_reimport_batch_success_keeps_row_and_config(
        installed, game, monkeypatch):
    """A reimport that works still lands on the same row with its config."""
    from cdumm import worker_process as wp

    events = _capture_emit(monkeypatch)
    db = installed["db"]
    good = installed["tmp"] / "good.json"
    good.write_text(json.dumps(_patch(TARGET, 10, 10, 0xCC)), encoding="utf-8")
    plist = installed["tmp"] / "reimport.txt"
    plist.write_text(f"{installed['mod_id']}\t{good}\n", encoding="utf-8")

    wp._run_reimport_batch(str(plist), str(game), str(installed["db_path"]),
                           str(installed["deltas"]))
    items = [e for e in events if e.get("type") == "batch_item"]
    assert len(items) == 1 and items[0]["error"] is None, events
    mod_id = installed["mod_id"]
    assert db.connection.execute(
        "SELECT selected_labels FROM mod_config WHERE mod_id = ?",
        (mod_id,)).fetchone() == ('["picked"]',)
    assert db.connection.execute("SELECT COUNT(*) FROM mods").fetchone()[0] == 1
    assert db.connection.execute(
        "SELECT COUNT(*) FROM mod_deltas WHERE mod_id = ?",
        (mod_id,)).fetchone()[0] == 1
