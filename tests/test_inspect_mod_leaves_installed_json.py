"""Inspect Mod (a trial import) must not touch the real CDMods/mods folder.

GitHub #480 (AgentKush). ``mod_compat_check.analyze_mod`` redirects only
``deltas_dir`` to a staging folder. A Format 3 mod used to resolve the real
CDMods root on its own and copy its JSON over ``CDMods/mods/<title>.json``.
The trial's database rows were removed afterwards, but an installed mod with
the same title kept the inspected version's JSON, so the next Apply shipped
a version the user never installed. A new title left an orphan JSON behind.

The stored JSON now lives in ``mods/`` next to the importer's ``deltas_dir``.
In a real import that is CDMods/deltas, so CDMods/mods is unchanged; Inspect
passes a staging deltas_dir, so the trial gets its own mods folder.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from cdumm.engine import json_patch_handler
from cdumm.engine.import_handler import import_from_zip
from cdumm.engine.mod_compat_check import analyze_mod
from cdumm.engine.snapshot_manager import SnapshotManager
from cdumm.storage.database import Database

TITLE = "Better Swords"


def _format3_doc(value: str, title: str = TITLE) -> dict:
    return {
        "modinfo": {"title": title, "version": value, "author": "test",
                    "description": ""},
        "format": 3,
        "target": "iteminfo.pabgb",
        "intents": [
            {"entry": "ThiefGloves", "key": 1001250,
             "field": "cooltime", "op": "set", "new": value},
        ],
    }


def _make_zip(path: Path, value: str, title: str = TITLE) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            f"{title}.field.json", json.dumps(_format3_doc(value, title)))
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    game_dir = tmp_path / "game"
    cdmods = game_dir / "CDMods"
    (cdmods / "mods").mkdir(parents=True)
    (cdmods / "deltas").mkdir()

    # The synthetic game folder has no PAZ archives: resolve the target
    # straight to a fake PAMT entry so the file write is what gets tested.
    fake = SimpleNamespace(
        paz_file=str(game_dir / "0008" / "0.paz"), offset=0, comp_size=16)
    monkeypatch.setattr(
        json_patch_handler, "_find_pamt_entry", lambda *a, **k: fake)

    db = Database(tmp_path / "cdumm.db")
    db.initialize()
    snapshot = SnapshotManager(cdmods / "snapshot.json")
    yield SimpleNamespace(
        game_dir=game_dir, cdmods=cdmods, db=db, snapshot=snapshot,
        tmp_path=tmp_path)
    db.close()


def _install_original(env) -> Path:
    """An installed Format 3 mod whose json_source is CDMods/mods/<title>.json."""
    installed = env.cdmods / "mods" / f"{TITLE}.json"
    installed.write_text(json.dumps(_format3_doc("0a")), encoding="utf-8")
    env.db.connection.execute(
        "INSERT INTO mods (name, mod_type, priority, json_source) "
        "VALUES (?, 'paz', 1, ?)", (TITLE, str(installed)))
    env.db.connection.commit()
    return installed


def _mods_listing(env) -> list[str]:
    return sorted(p.name for p in (env.cdmods / "mods").iterdir())


def test_inspect_same_title_keeps_installed_json_byte_identical(env):
    installed = _install_original(env)
    before = installed.read_bytes()
    listing_before = _mods_listing(env)

    zip_path = _make_zip(env.tmp_path / "Better Swords v2.zip", "ff")
    result = analyze_mod(zip_path, env.game_dir, env.db, env.snapshot)

    assert result.error is None
    assert installed.read_bytes() == before
    assert _mods_listing(env) == listing_before
    rows = env.db.connection.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    assert rows == 1


def test_inspect_new_title_leaves_no_orphan_json(env):
    listing_before = _mods_listing(env)

    zip_path = _make_zip(
        env.tmp_path / "Other Mod.zip", "ff", title="Other Mod")
    result = analyze_mod(zip_path, env.game_dir, env.db, env.snapshot)

    assert result.error is None
    assert _mods_listing(env) == listing_before
    # The staging folder is removed again, as before.
    staging = env.cdmods / "_import_staging"
    assert not staging.exists() or not any(staging.iterdir())


def test_real_import_still_stores_json_in_cdmods_mods(env):
    zip_path = _make_zip(env.tmp_path / "Better Swords v1.zip", "0a")
    result = import_from_zip(
        zip_path, env.game_dir, env.db, env.snapshot,
        env.cdmods / "deltas")

    assert result.error is None
    stored = env.cdmods / "mods" / f"{TITLE}.json"
    assert stored.is_file()
    assert json.loads(stored.read_text(encoding="utf-8"))["modinfo"][
        "version"] == "0a"
    json_source = env.db.connection.execute(
        "SELECT json_source FROM mods").fetchone()[0]
    assert Path(json_source) == stored
