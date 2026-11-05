"""GitHub #379: an nxm:// download names its Nexus mod in the URL, but
``_finish_nxm_download`` only handed the FILE id on to the import. Every
post-import step (PAZ rows, nested-variant rows, ASI plugins) then
re-derived the mod id from the filename and stored none when the name
did not parse, so the mod was never update-checked.

The mod id now travels next to the file id, keyed on the queued path,
and the post-import steps prefer it to the filename.

The ASI tests drive ``CdummWindow._store_asi_version`` with a stand-in
window, a tmp database and a tmp bin64: nothing outside tmp_path is
read or written.
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from cdumm.gui.fluent_window import (
    CdummWindow,
    _clear_pending_post_import_state,
    _pop_nxm_mod_id,
)


def _window_src() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "gui" / "fluent_window.py").read_text(
                encoding="utf-8")


# ── hand-off from the nxm:// handler ────────────────────────────────

def test_finish_nxm_download_hands_the_mod_id_to_the_queue():
    src = _window_src()
    start = src.index("def _finish_nxm_download")
    end = src.index("\n    def ", start + 1)
    body = src[start:end]
    call = body[body.rindex("self._queue_import("):]
    call = call[:call.index(")") + 1]
    assert re.search(r"nexus_mod_id\s*=\s*nexus_mod_id", call), call


def test_queue_import_records_the_mod_id_by_path(tmp_path):
    win = SimpleNamespace(_active_worker=object())  # don't start a worker
    path = tmp_path / "Some Download.zip"
    CdummWindow._queue_import(
        win, path, nexus_real_file_id=7, nexus_mod_id=3941)
    assert win._nexus_nxm_mod_id_map == {str(path): 3941}
    assert win._nexus_real_file_id_map == {str(path): 7}


def test_queue_import_without_nxm_records_nothing(tmp_path):
    win = SimpleNamespace(_active_worker=object())
    CdummWindow._queue_import(win, tmp_path / "Manual.zip")
    assert win._nexus_nxm_mod_id_map == {}


def test_pop_returns_once_then_zero(tmp_path):
    path = tmp_path / "a.zip"
    win = SimpleNamespace(_nexus_nxm_mod_id_map={str(path): 452})
    assert _pop_nxm_mod_id(win, path) == 452
    assert _pop_nxm_mod_id(win, path) == 0


def test_pop_without_a_map_is_zero(tmp_path):
    assert _pop_nxm_mod_id(SimpleNamespace(), tmp_path / "a.zip") == 0


def test_clear_pending_state_forgets_the_mod_id(tmp_path):
    ok, other = tmp_path / "ok.zip", tmp_path / "other.zip"
    win = SimpleNamespace(
        _nexus_real_file_id_map={},
        _nexus_nxm_mod_id_map={str(ok): 1, str(other): 2})
    _clear_pending_post_import_state(win, path=ok)
    assert win._nexus_nxm_mod_id_map == {str(other): 2}


def test_paz_post_import_steps_consult_the_nxm_mod_id():
    """The worker-path post-import and the variant metadata helper both
    pop the nxm mod id right after parsing the filename."""
    src = _window_src()
    assert re.search(
        r"_parse_nexus_filename\(_nexus_stem\)\s*\n(?:\s*#.*\n)*"
        r"\s*nexus_id = _pop_nxm_mod_id\(self, path\) or nexus_id", src)
    helper = src[src.index("def _store_nexus_metadata_on_row"):]
    helper = helper[:helper.index("\n    def ", 1)]
    assert "nexus_id = _pop_nxm_mod_id(self, drop_path) or nexus_id" in helper


# ── _store_asi_version ──────────────────────────────────────────────

def _asi_window(db, game_dir, nxm=None):
    (game_dir / "bin64").mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        _db=db,
        _game_dir=game_dir,
        _get_drop_version=CdummWindow._get_drop_version,
        _nexus_real_file_id_map={},
        _nexus_nxm_mod_id_map=dict(nxm or {}),
        _nexus_updates=None,
    )


def _asi_row(db, name):
    return db.connection.execute(
        "SELECT version, nexus_mod_id FROM asi_plugin_state WHERE name = ?",
        (name,)).fetchone()


def test_asi_install_from_nxm_stores_the_url_mod_id(db, tmp_path):
    """The served name carries no mod id; the nxm:// URL does."""
    src = tmp_path / "dl" / "QuickSlotLockFilter.zip"
    win = _asi_window(db, tmp_path / "game", {str(src): 3029})
    CdummWindow._store_asi_version(win, src, ["QuickSlotLockFilter.asi"])
    assert _asi_row(db, "QuickSlotLockFilter") == (None, 3029)
    assert win._nexus_nxm_mod_id_map == {}


def test_nxm_mod_id_wins_over_the_filename(db, tmp_path):
    src = tmp_path / "dl" / "Foo-111-1-1775958271.zip"
    win = _asi_window(db, tmp_path / "game", {str(src): 222})
    CdummWindow._store_asi_version(win, src, ["Foo.asi"])
    assert _asi_row(db, "Foo") == ("1", 222)


def test_manual_asi_install_parses_space_separated_name(db, tmp_path):
    """No nxm:// entry: the filename is still the source, and the new
    form now parses."""
    src = tmp_path / "dl" / "No Peach Fuzz 3941 1 2026-10-31T18-11Z vEjq5ZqS9.zip"
    win = _asi_window(db, tmp_path / "game")
    CdummWindow._store_asi_version(win, src, ["NoPeachFuzz.asi"])
    assert _asi_row(db, "NoPeachFuzz") == ("1", 3941)


def test_two_plugins_from_one_archive_both_survive(db, tmp_path):
    """The rename cleanup deletes OTHER rows sharing the mod id. When
    the second plugin of an archive was recorded, the first one, just
    installed, matched that query and its .asi and .ini were deleted
    from bin64. Any install with a mod id did this; before #379 a
    space-separated name had no id, so linking those names would have
    spread it."""
    game = tmp_path / "game"
    win = _asi_window(db, game)
    bin64 = game / "bin64"
    for f in ("Alpha.asi", "Alpha.ini", "Beta.asi"):
        (bin64 / f).write_bytes(b"x")
    src = tmp_path / "dl" / "Alpha Pack-500-1-1775958271.zip"
    CdummWindow._store_asi_version(
        win, src, ["Alpha.asi", "Alpha.ini", "Beta.asi"])
    assert (bin64 / "Alpha.asi").exists()
    assert (bin64 / "Alpha.ini").exists()
    assert (bin64 / "Beta.asi").exists()
    assert _asi_row(db, "Alpha") == ("1", 500)
    assert _asi_row(db, "Beta") == ("1", 500)


def test_rename_cleanup_still_removes_the_predecessor(db, tmp_path):
    """The case the cleanup exists for (ed07b56): the author renamed
    EnhancedFlight.asi to EnhancedFlightv31.asi between versions. Holds
    on master too; it guards the exclusion above against overreach."""
    game = tmp_path / "game"
    win = _asi_window(db, game)
    bin64 = game / "bin64"
    (bin64 / "EnhancedFlight.asi").write_bytes(b"old")
    (bin64 / "EnhancedFlightv31.asi").write_bytes(b"new")
    db.connection.execute(
        "INSERT INTO asi_plugin_state (name, nexus_mod_id) VALUES (?, ?)",
        ("EnhancedFlight", 815))
    db.connection.commit()
    src = tmp_path / "dl" / "Enhanced Flight-815-3-1-1777000000.zip"
    CdummWindow._store_asi_version(win, src, ["EnhancedFlightv31.asi"])
    assert not (bin64 / "EnhancedFlight.asi").exists()
    assert _asi_row(db, "EnhancedFlight") is None
    assert (bin64 / "EnhancedFlightv31.asi").exists()
    assert _asi_row(db, "EnhancedFlightv31") == ("3.1", 815)
