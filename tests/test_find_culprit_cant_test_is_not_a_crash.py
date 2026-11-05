"""Find Culprit stops when a round can't be tested instead of blaming a mod (#483).

A refused or failed apply, a launch that raised, and a game process that
never appeared all used to count as "crashed". On game 2.04 every apply is
refused, so ddmin narrowed to the highest-priority mod, flagged it in the
crash registry as crashing by itself and disabled it. The same happened
without a Steam handler (Xbox, Epic, Steam offline).

Those three cases mean "can't test": the run stops, restores what it
changed, flags nothing and says why. A game that really appeared and
exited still counts as a crash.
"""
from __future__ import annotations

import sqlite3
import time as real_time
from queue import Queue
from types import SimpleNamespace

import pytest

import cdumm.engine.game_monitor as gm
from cdumm.gui import binary_search_dialog as bsd
from cdumm.storage.database import Database

from .test_game204_signed_index_safety import _compact_papgt, _game

ASI_BYTES = b"ASI-PLUGIN-BYTES"
NO_STEAM = OSError(
    1155, "No application is associated with the specified file for "
    "this operation")


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot, *_):
        self.slots.append(slot)

    def emit(self, msg):
        for slot in self.slots:
            slot(msg)


def _fake_apply(refuse_from_call: int | None, calls: list):
    """ApplyWorker stand-in: refuses every apply from call N on (1-based)."""
    class _Apply:
        def __init__(self, *_a, **_k):
            self.error_occurred = _Signal()

        def run(self):
            calls.append(1)
            if refuse_from_call is not None and len(calls) >= refuse_from_call:
                self.error_occurred.emit(
                    "Game 2.04 or newer detected. This game version "
                    "rejects a modified archive list.")
    return _Apply


def _setup(tmp_path, monkeypatch, mods=("Mod1", "Mod2", "Mod3", "Mod4")):
    game = tmp_path / "steamapps" / "common" / "Crimson Desert"
    bin64 = game / "bin64"
    bin64.mkdir(parents=True)
    (game / "CDMods").mkdir()
    (bin64 / "crashpad_handler.exe").write_bytes(b"CRASHPAD")
    (bin64 / "Foo.asi").write_bytes(ASI_BYTES)
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


def _query(game, sql):
    con = sqlite3.connect(game / "CDMods" / "cdumm.db")
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _run(game, monkeypatch, *, apply_cls, launch=None):
    """Run the real worker with the given fakes; return (msgs, state)."""
    launches: list = []

    def counting_launch(*a, **k):
        launches.append(1)
        return launch(*a, **k)

    monkeypatch.setattr(bsd, "ApplyWorker", apply_cls)
    if launch is not None:
        monkeypatch.setattr(gm, "launch_and_test", counting_launch)
    worker = bsd._AutoBisectWorker(
        None, None, game, game, None, asi_mods=bsd.build_asi_suspects(game))
    worker.msg_queue = Queue()
    worker.run()
    msgs = []
    while not worker.msg_queue.empty():
        msgs.append(worker.msg_queue.get_nowait())
    return msgs, launches


def _assert_nothing_blamed(game, msgs):
    assert not [m for m in msgs if m[0] == "finished"], msgs
    errors = [m for m in msgs if m[0] == "error"]
    assert errors, msgs
    assert _query(game, "SELECT mod_id FROM crash_registry") == []
    assert _query(game, "SELECT name FROM mods WHERE enabled = 0") == []
    bin64 = game / "bin64"
    assert sorted(p.name for p in bin64.iterdir()) == [
        "Foo.asi", "crashpad_handler.exe"]
    assert (bin64 / "Foo.asi").read_bytes() == ASI_BYTES
    return errors[0][1]


# ── Apply refused or failed ─────────────────────────────────────────


def test_a_refused_round_apply_stops_the_run_and_blames_nothing(
        tmp_path, monkeypatch):
    game = _setup(tmp_path, monkeypatch)
    calls: list = []
    # The verify apply works and the verify launch crashes. Every round
    # apply after that is refused, as on 2.04.
    msgs, launches = _run(
        game, monkeypatch, apply_cls=_fake_apply(2, calls),
        launch=lambda *a, **k: True)

    reason = _assert_nothing_blamed(game, msgs)
    assert "2.04" in reason
    assert len(launches) == 1  # only the verify launch ever ran


def test_a_failed_verify_apply_stops_before_any_launch(tmp_path, monkeypatch):
    game = _setup(tmp_path, monkeypatch)
    msgs, launches = _run(
        game, monkeypatch, apply_cls=_fake_apply(1, []),
        launch=lambda *a, **k: False)

    _assert_nothing_blamed(game, msgs)
    assert launches == []
    assert not any("compatible" in str(m) for m in msgs)


# ── Launch failures ─────────────────────────────────────────────────


def _patch_real_launch(monkeypatch, *, steam_error=None, pid=None):
    monkeypatch.setattr(gm, "find_game_process", lambda: pid)
    monkeypatch.setattr(
        gm, "time",
        SimpleNamespace(time=real_time.time, sleep=lambda _s: None))
    if steam_error is not None:
        def boom(_game_dir):
            raise steam_error
        monkeypatch.setattr(gm, "launch_via_steam", boom)
    else:
        monkeypatch.setattr(
            gm, "launch_via_steam", lambda _game_dir: real_time.time())


def test_no_steam_handler_stops_the_run_and_blames_nothing(
        tmp_path, monkeypatch):
    game = _setup(tmp_path, monkeypatch)
    _patch_real_launch(monkeypatch, steam_error=NO_STEAM)

    msgs, _ = _run(game, monkeypatch, apply_cls=_fake_apply(None, []))

    _assert_nothing_blamed(game, msgs)


def test_a_game_that_never_appears_stops_the_run_and_blames_nothing(
        tmp_path, monkeypatch):
    game = _setup(tmp_path, monkeypatch)
    _patch_real_launch(monkeypatch)

    msgs, _ = _run(game, monkeypatch, apply_cls=_fake_apply(None, []))

    _assert_nothing_blamed(game, msgs)


def test_launch_and_test_raises_instead_of_reporting_a_crash(
        tmp_path, monkeypatch):
    bin64 = tmp_path / "bin64"
    bin64.mkdir()
    (bin64 / "crashpad_handler.exe").write_bytes(b"CRASHPAD")

    _patch_real_launch(monkeypatch, steam_error=NO_STEAM)
    with pytest.raises(gm.GameNotTestable):
        gm.launch_and_test(tmp_path, launch_timeout=3)
    assert (bin64 / "crashpad_handler.exe").exists()

    _patch_real_launch(monkeypatch)
    with pytest.raises(gm.GameNotTestable):
        gm.launch_and_test(tmp_path, launch_timeout=3)
    assert (bin64 / "crashpad_handler.exe").exists()


# ── What still counts as a crash ────────────────────────────────────


def test_a_game_that_appears_and_exits_is_still_a_crash(
        tmp_path, monkeypatch):
    (tmp_path / "bin64").mkdir()
    _patch_real_launch(monkeypatch, pid=4242)
    monkeypatch.setattr(gm, "wait_for_exit", lambda _pid, _ms: 0xC0000005)

    assert gm.launch_and_test(tmp_path, stable_seconds=5) is True


def test_a_real_crash_still_finds_and_flags_the_culprit(
        tmp_path, monkeypatch):
    game = _setup(tmp_path, monkeypatch, mods=("Mod1", "Mod2"))

    def crashes_when_mod1_enabled(*_a, **_k):
        rows = _query(game, "SELECT enabled FROM mods WHERE name = 'Mod1'")
        return bool(rows and rows[0][0])

    msgs, _ = _run(
        game, monkeypatch, apply_cls=_fake_apply(None, []),
        launch=crashes_when_mod1_enabled)

    finished = [m for m in msgs if m[0] == "finished"]
    assert finished, msgs
    assert [m["name"] for m in finished[0][1]["minimal_set"]] == ["Mod1"]
    assert [r[1] for r in _query(
        game, "SELECT mod_id, mod_name FROM crash_registry")] == ["Mod1"]


# ── Refusing to start ───────────────────────────────────────────────


def test_a_2_04_install_is_refused_before_anything_is_touched(
        tmp_path, monkeypatch):
    game = _game(tmp_path, _compact_papgt())
    bin64 = game / "bin64"
    bin64.mkdir()
    (bin64 / "crashpad_handler.exe").write_bytes(b"CRASHPAD")
    (bin64 / "Foo.asi").write_bytes(ASI_BYTES)
    monkeypatch.setattr(
        "cdumm.engine.cdmods_paths.read_cdmods_path_pointer", lambda: None)
    applies: list = []

    def must_not_launch(*_a, **_k):
        raise AssertionError("launch_and_test must not run on 2.04")

    monkeypatch.setattr(bsd, "ApplyWorker", _fake_apply(None, applies))
    monkeypatch.setattr(gm, "launch_and_test", must_not_launch)
    worker = bsd._AutoBisectWorker(
        None, None, game, game, None, asi_mods=bsd.build_asi_suspects(game))
    worker.msg_queue = Queue()
    worker.run()

    msgs = []
    while not worker.msg_queue.empty():
        msgs.append(worker.msg_queue.get_nowait())
    errors = [m for m in msgs if m[0] == "error"]
    assert errors and "2.04" in errors[0][1]
    assert not [m for m in msgs if m[0] == "finished"]
    assert applies == []
    assert sorted(p.name for p in bin64.iterdir()) == [
        "Foo.asi", "crashpad_handler.exe"]
    assert not (game / "CDMods").exists()


def test_the_start_gate_names_2_04_and_non_steam_installs(tmp_path):
    signed = _game(tmp_path, _compact_papgt())
    assert bsd.find_culprit_blocker(signed) == bsd.BLOCKED_SIGNED_INDEX

    plain = tmp_path / "plain"
    plain.mkdir()
    assert bsd.find_culprit_blocker(plain) is None
    assert bsd.find_culprit_blocker(
        plain, require_steam=True) == bsd.BLOCKED_NOT_STEAM

    steam = tmp_path / "steamapps" / "common" / "Crimson Desert"
    steam.mkdir(parents=True)
    assert bsd.find_culprit_blocker(steam, require_steam=True) is None
