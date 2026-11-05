"""A ReShade ``.addon64`` keeps its kind through disable and enable (#481).

``AsiManager.disable`` used to rename every plugin to ``<name>.asi.disabled``
whatever its kind, so ``scan()`` read it back as an ASI plugin and ``enable``
restored it as ``<name>.asi``. One Find Culprit run, or one click on the ASI
page, turned ``CrimsonWeather.addon64`` into ``CrimsonWeather.asi`` for good.

The ASI handling is real here. Apply and the game launch are fakes, with
"crashes when Mod1 is enabled" as the crash rule.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from queue import Queue
from types import SimpleNamespace

import pytest

from cdumm.asi.asi_manager import AsiManager
from cdumm.storage.database import Database

ADDON_BYTES = b"ADDON-BYTES"
ASI_BYTES = b"ASI-PLUGIN-BYTES"


def _names(bin64: Path) -> list[str]:
    return sorted(p.name for p in bin64.iterdir())


def _by_kind(mgr: AsiManager) -> dict[tuple[str, str], bool]:
    return {(p.kind, p.name): p.enabled for p in mgr.scan()}


# ── AsiManager ──────────────────────────────────────────────────────


def test_addon64_round_trip_keeps_its_extension(tmp_path):
    bin64 = tmp_path / "bin64"
    src = tmp_path / "RenoDX.addon64"
    src.write_bytes(ADDON_BYTES)
    mgr = AsiManager(bin64)
    mgr.install(src)
    (plugin,) = mgr.scan()
    assert plugin.kind == "addon64"

    mgr.disable(plugin)
    assert (bin64 / "RenoDX.addon64.disabled").read_bytes() == ADDON_BYTES
    assert not (bin64 / "RenoDX.asi.disabled").exists()
    (plugin,) = mgr.scan()
    assert (plugin.kind, plugin.name, plugin.enabled) == (
        "addon64", "RenoDX", False)

    mgr.enable(plugin)
    assert (bin64 / "RenoDX.addon64").read_bytes() == ADDON_BYTES
    assert not (bin64 / "RenoDX.asi").exists()
    (plugin,) = mgr.scan()
    assert (plugin.kind, plugin.enabled) == ("addon64", True)


def test_uninstall_removes_addon_after_a_round_trip(tmp_path):
    bin64 = tmp_path / "bin64"
    src = tmp_path / "RenoDX.addon64"
    src.write_bytes(ADDON_BYTES)
    mgr = AsiManager(bin64)
    mgr.install(src)
    (plugin,) = mgr.scan()
    mgr.disable(plugin)
    mgr.enable(mgr.scan()[0])

    mgr.uninstall(mgr.scan()[0])
    assert _names(bin64) == []


def test_uninstall_removes_a_disabled_addon(tmp_path):
    bin64 = tmp_path / "bin64"
    src = tmp_path / "RenoDX.addon64"
    src.write_bytes(ADDON_BYTES)
    mgr = AsiManager(bin64)
    mgr.install(src)
    mgr.disable(mgr.scan()[0])

    mgr.uninstall(mgr.scan()[0])
    assert _names(bin64) == []


def test_asi_plugins_still_use_the_asi_disabled_form(tmp_path):
    bin64 = tmp_path / "bin64"
    bin64.mkdir()
    (bin64 / "Foo.asi").write_bytes(ASI_BYTES)
    mgr = AsiManager(bin64)
    mgr.disable(mgr.scan()[0])
    assert _names(bin64) == ["Foo.asi.disabled"]
    assert _by_kind(mgr) == {("asi", "Foo"): False}
    mgr.enable(mgr.scan()[0])
    assert _names(bin64) == ["Foo.asi"]


def test_same_stem_pair_does_not_collide(tmp_path):
    bin64 = tmp_path / "bin64"
    bin64.mkdir()
    (bin64 / "Foo.asi").write_bytes(ASI_BYTES)
    (bin64 / "Foo.addon64").write_bytes(ADDON_BYTES)
    mgr = AsiManager(bin64)

    for plugin in mgr.scan():
        mgr.disable(plugin)
    assert (bin64 / "Foo.asi.disabled").read_bytes() == ASI_BYTES
    assert (bin64 / "Foo.addon64.disabled").read_bytes() == ADDON_BYTES
    assert _by_kind(mgr) == {("asi", "Foo"): False, ("addon64", "Foo"): False}

    for plugin in mgr.scan():
        mgr.enable(plugin)
    assert (bin64 / "Foo.asi").read_bytes() == ASI_BYTES
    assert (bin64 / "Foo.addon64").read_bytes() == ADDON_BYTES


def test_updating_a_disabled_addon_does_not_leave_a_duplicate(tmp_path):
    bin64 = tmp_path / "bin64"
    src = tmp_path / "RenoDX.addon64"
    src.write_bytes(ADDON_BYTES)
    mgr = AsiManager(bin64)
    mgr.install(src)
    mgr.disable(mgr.scan()[0])

    src.write_bytes(b"NEWER")
    mgr.install(src)
    assert _names(bin64) == [
        "RenoDX.addon64.disabled", "RenoDX.cdumm-files.json"]
    assert (bin64 / "RenoDX.addon64.disabled").read_bytes() == b"NEWER"


# ── ASI page rename ─────────────────────────────────────────────────


@pytest.mark.parametrize("enabled,expected", [
    (True, "RenoDX_HDR.addon64"),
    (False, "RenoDX_HDR.addon64.disabled"),
])
def test_page_rename_keeps_the_addon_extension(tmp_path, enabled, expected):
    pytest.importorskip("qfluentwidgets")
    from cdumm.gui.pages.asi_page import AsiPluginsPage

    bin64 = tmp_path / "bin64"
    src = tmp_path / "RenoDX.addon64"
    src.write_bytes(ADDON_BYTES)
    mgr = AsiManager(bin64)
    mgr.install(src)
    plugin = mgr.scan()[0]
    if not enabled:
        mgr.disable(plugin)
        plugin = mgr.scan()[0]

    page = SimpleNamespace(
        _find_plugin=lambda name: plugin, _db=None, refresh=lambda: None)
    AsiPluginsPage._on_asi_renamed(page, "RenoDX", "RenoDX_HDR")

    assert expected in _names(bin64)
    assert not any(n.endswith(".asi") or ".asi." in n for n in _names(bin64))


# ── Find Culprit ────────────────────────────────────────────────────


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot, *_):
        self.slots.append(slot)


class _FakeApply:
    def __init__(self, *_a, **_k):
        self.error_occurred = _Signal()

    def run(self):
        return None


def _make_game(tmp_path, monkeypatch, mods=("Mod1", "Mod2")):
    game = tmp_path / "steamapps" / "common" / "Crimson Desert"
    (game / "bin64").mkdir(parents=True)
    (game / "CDMods").mkdir()
    monkeypatch.setattr(
        "cdumm.engine.cdmods_paths.read_cdmods_path_pointer", lambda: None)
    db = Database(game / "CDMods" / "cdumm.db")
    db.initialize()
    for i, name in enumerate(mods):
        db.connection.execute(
            "INSERT INTO mods (name, mod_type, enabled, priority) "
            "VALUES (?, 'paz', 1, ?)", (name, i))
    db.connection.commit()
    db.close()
    return game


def _run_find_culprit(game, monkeypatch):
    import cdumm.engine.game_monitor as gm
    from cdumm.gui import binary_search_dialog as bsd

    db_path = game / "CDMods" / "cdumm.db"

    def fake_launch(*_a, **_k):
        con = sqlite3.connect(db_path)
        try:
            row = con.execute(
                "SELECT enabled FROM mods WHERE name = 'Mod1'").fetchone()
        finally:
            con.close()
        return bool(row and row[0])

    monkeypatch.setattr(bsd, "ApplyWorker", _FakeApply)
    monkeypatch.setattr(gm, "launch_and_test", fake_launch)

    worker = bsd._AutoBisectWorker(
        None, None, game, game, None,
        asi_mods=bsd.build_asi_suspects(game))
    worker.msg_queue = Queue()
    worker.run()
    msgs = []
    while not worker.msg_queue.empty():
        msgs.append(worker.msg_queue.get_nowait())
    return msgs


def test_find_culprit_leaves_an_addon_as_an_addon(tmp_path, monkeypatch):
    game = _make_game(tmp_path, monkeypatch)
    bin64 = game / "bin64"
    (bin64 / "CrimsonWeather.addon64").write_bytes(ADDON_BYTES)
    (bin64 / "Foo.asi").write_bytes(ASI_BYTES)

    msgs = _run_find_culprit(game, monkeypatch)

    finished = [m for m in msgs if m[0] == "finished"]
    assert finished, msgs
    assert [m["name"] for m in finished[0][1]["minimal_set"]] == ["Mod1"]
    assert _names(bin64) == ["CrimsonWeather.addon64", "Foo.asi"]
    assert (bin64 / "CrimsonWeather.addon64").read_bytes() == ADDON_BYTES
    assert _by_kind(AsiManager(bin64)) == {
        ("addon64", "CrimsonWeather"): True, ("asi", "Foo"): True}


def test_find_culprit_same_stem_pair_keeps_both_files(tmp_path, monkeypatch):
    game = _make_game(tmp_path, monkeypatch)
    bin64 = game / "bin64"
    (bin64 / "Foo.addon64").write_bytes(ADDON_BYTES)
    (bin64 / "Foo.asi").write_bytes(ASI_BYTES)

    msgs = _run_find_culprit(game, monkeypatch)

    assert any(m[0] == "finished" for m in msgs), msgs
    assert (bin64 / "Foo.addon64").read_bytes() == ADDON_BYTES
    assert (bin64 / "Foo.asi").read_bytes() == ASI_BYTES
    assert _names(bin64) == ["Foo.addon64", "Foo.asi"]


def test_suspects_for_a_same_stem_pair_have_distinct_names(tmp_path):
    from cdumm.gui.binary_search_dialog import build_asi_suspects

    game = tmp_path / "game"
    (game / "bin64").mkdir(parents=True)
    (game / "bin64" / "Foo.asi").write_bytes(ASI_BYTES)
    (game / "bin64" / "Foo.addon64").write_bytes(ADDON_BYTES)

    suspects = build_asi_suspects(game)
    names = [s["name"] for s in suspects.values()]
    assert len(names) == 2
    assert len(set(names)) == 2
