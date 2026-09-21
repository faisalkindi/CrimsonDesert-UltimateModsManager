"""A loose-file overlay that ships a Format 3 JSON must import both halves,
and the converter must never pair a snapshot PAMT with a PAZ from another
build.

GitHub #414 (woowoots, Female Kliff Glide Override). The zip ships seven
action-chart files under 0010/actionchart/ and a .field.json that reroutes
Kliff's character records at those charts. Two separate defects:

1. The loose-file path converted the seven files and handed the JSON to
   the CB resolver, which cannot place a Format 3 file in any PAZ dir,
   logged one warning and dropped it. Nothing reached the user. Dragging
   the zip gave the animations without the reroute; dragging the JSON on
   its own, which the reporter ended up doing (mod card: 0 deltas,
   drop_name=...field.json), gave the reroute without the animations.
   Half a mod either way.

2. ``convert_to_paz_mod`` reads the PAMT from the vanilla snapshot when
   one exists, but the PAZ from the live game dir when the snapshot does
   not hold that PAZ, which is the normal case for a dir no mod has
   touched. After a game update without a rescan those come from
   different builds. Measured on a real install (game patched 2026-09-20,
   snapshot from July): the rebuilt archive was internally consistent
   but described the wrong bytes, and the entry-level import either read
   past the end of the file ("mod read: failed to fill whole buffer") or
   diffed 114,476 of 122,563 entries as changed for a seven-file mod. The
   PAMT declares the size of each PAZ it indexes, so the mismatch is
   detectable up front. Separately, every caller passed no config, so a
   custom CDMods root was ignored and ``<game>/CDMods`` searched instead.

Verified end to end on the reporter's zip against the real game with a
clean CDMods root: 7/136838 entries changed for the overlay, plus a
companion Format 3 card, from one drag.
"""
from __future__ import annotations

import contextlib
import json
import struct
from pathlib import Path

import pytest

# ── PAMT/PAZ size guard ───────────────────────────────────────────────


def _pamt_bytes(paz_sizes: list[int]) -> bytes:
    """Minimal PAMT header: magic, count, hash, zero, then per PAZ a hash
    and size with a 4-byte separator after every PAZ but the last."""
    out = bytearray(struct.pack("<IIII", 0x12345678, len(paz_sizes), 0, 0))
    for i, size in enumerate(paz_sizes):
        out += struct.pack("<II", 0xDEADBEEF + i, size)
        if i < len(paz_sizes) - 1:
            out += b"\0\0\0\0"
    return bytes(out)


def test_declared_sizes_are_read_back(tmp_path):
    from cdumm.engine.crimson_browser_handler import _pamt_declared_paz_sizes
    p = tmp_path / "0.pamt"
    p.write_bytes(_pamt_bytes([100, 200, 300]))
    assert _pamt_declared_paz_sizes(p) == [100, 200, 300]


def test_matching_sizes_are_not_flagged(tmp_path):
    from cdumm.engine.crimson_browser_handler import _pamt_disagrees_with_paz_sizes
    van = tmp_path / "vanilla"; van.mkdir()
    game = tmp_path / "game"; game.mkdir()
    (van / "0.pamt").write_bytes(_pamt_bytes([5, 7]))
    (game / "0.paz").write_bytes(b"x" * 5)
    (game / "1.paz").write_bytes(b"y" * 7)
    assert _pamt_disagrees_with_paz_sizes(van / "0.pamt", van, game) is None


def test_snapshot_pamt_against_newer_game_paz_is_flagged(tmp_path):
    """The #414 shape: snapshot has the PAMT, game dir has a PAZ from a
    later build. The declared size disagrees with the file."""
    from cdumm.engine.crimson_browser_handler import _pamt_disagrees_with_paz_sizes
    van = tmp_path / "vanilla"; van.mkdir()
    game = tmp_path / "game"; game.mkdir()
    (van / "0.pamt").write_bytes(_pamt_bytes([264_526_160]))
    (game / "0.paz").write_bytes(b"z" * 10)
    why = _pamt_disagrees_with_paz_sizes(van / "0.pamt", van, game)
    assert why is not None
    assert "0.paz" in why and "264,526,160" in why and "10" in why


def test_snapshot_paz_is_preferred_over_game_paz_for_the_check(tmp_path):
    """When the snapshot holds the PAZ too, that is the file the converter
    will copy, so that is the file the PAMT must agree with."""
    from cdumm.engine.crimson_browser_handler import _pamt_disagrees_with_paz_sizes
    van = tmp_path / "vanilla"; van.mkdir()
    game = tmp_path / "game"; game.mkdir()
    (van / "0.pamt").write_bytes(_pamt_bytes([4]))
    (van / "0.paz").write_bytes(b"abcd")           # matches
    (game / "0.paz").write_bytes(b"different!")    # would not, but unused
    assert _pamt_disagrees_with_paz_sizes(van / "0.pamt", van, game) is None


def test_unreadable_pamt_counts_as_mismatch(tmp_path):
    from cdumm.engine.crimson_browser_handler import _pamt_disagrees_with_paz_sizes
    van = tmp_path / "vanilla"; van.mkdir()
    (van / "0.pamt").write_bytes(b"\0" * 8)         # truncated header
    assert _pamt_disagrees_with_paz_sizes(van / "0.pamt", van, tmp_path) is not None


def test_converter_falls_back_to_game_pamt_when_snapshot_is_stale(tmp_path, monkeypatch, caplog):
    """Drive ``convert_to_paz_mod`` far enough to see which PAMT it
    chooses. The game dir's own PAMT must win when the snapshot's PAMT
    cannot describe the PAZ that will be copied."""
    import logging

    from cdumm.engine import crimson_browser_handler as cbh
    game = tmp_path / "game"
    (game / "0010").mkdir(parents=True)
    (game / "0010" / "0.paz").write_bytes(b"P" * 50)
    (game / "0010" / "0.pamt").write_bytes(_pamt_bytes([50]))
    cdmods = game / "CDMods" / "vanilla" / "0010"
    cdmods.mkdir(parents=True)
    (cdmods / "0.pamt").write_bytes(_pamt_bytes([999]))   # stale
    files = tmp_path / "mod" / "0010" / "actionchart"
    files.mkdir(parents=True)
    (files / "x.paac").write_bytes(b"new")
    seen: dict = {}

    def fake_parse(pamt_path, paz_dir):
        seen["pamt"] = Path(pamt_path)
        return []                                      # nothing to repack
    monkeypatch.setattr(cbh, "parse_pamt", fake_parse, raising=False)
    caplog.set_level(logging.WARNING, logger=cbh.logger.name)
    manifest = {"id": "m", "files_dir": ".", "_base_dir": tmp_path / "mod"}
    with contextlib.suppress(Exception):   # only the PAMT choice is under test
        cbh.convert_to_paz_mod(manifest, game, tmp_path / "work")
    assert seen.get("pamt") == game / "0010" / "0.pamt", seen
    assert any("different build" in r.getMessage() for r in caplog.records)


# ── bundled Format 3 JSON ─────────────────────────────────────────────


def _format3(target: str) -> dict:
    return {
        "format": "natt_format_3",
        "target": target,
        "modinfo": {"title": "Glide", "version": "1", "author": "a"},
        "intents": [{"entry": "Kliff", "key": 1, "field": "f36",
                     "op": "set", "new": 2}],
    }


def test_bundled_jsons_are_found_outside_numbered_dirs(tmp_path, monkeypatch):
    from cdumm.engine import import_handler as ih
    root = tmp_path / "mod"
    (root / "0010" / "actionchart").mkdir(parents=True)
    (root / "meta").mkdir()
    (root / "0010" / "inside.json").write_text(json.dumps(_format3("a.pabgb")))
    (root / "meta" / "m.json").write_text(json.dumps(_format3("a.pabgb")))
    (root / "manifest.json").write_text("{}")
    (root / "Glide.field.json").write_text(json.dumps(_format3("characterinfo.pabgb")))
    (root / "notes.json").write_text('{"hello": 1}')
    monkeypatch.setattr(
        "cdumm.engine.json_patch_handler.is_natt_format_3",
        lambda p: p.name.endswith(".field.json"))
    found = ih._bundled_format3_jsons(root)
    assert [f.name for f in found] == ["Glide.field.json"]


class _Res:
    def __init__(self, name, mod_id, error=None):
        self.name = name; self.mod_id = mod_id; self.error = error; self.info = None


def test_bundled_json_becomes_companion_card_with_same_name(tmp_path, monkeypatch):
    from cdumm.engine import import_handler as ih
    root = tmp_path / "mod"; root.mkdir()
    (root / "Glide.field.json").write_text(json.dumps(_format3("characterinfo.pabgb")))
    monkeypatch.setattr(
        "cdumm.engine.json_patch_handler.is_natt_format_3", lambda p: True)
    calls = []
    monkeypatch.setattr(ih, "import_from_natt_format_3",
                        lambda jp, *a, **k: (calls.append(jp.name), _Res("Glide", 42))[1])
    renamed = []

    class _Conn:
        def execute(self, sql, params=()): renamed.append((sql, params))
        def commit(self): pass

    class _Db:
        connection = _Conn()

    primary = _Res("Female Kliff Glide Override", 1)
    out = ih._import_bundled_format3(root, tmp_path, _Db(), None, tmp_path, primary)
    assert out is primary
    assert calls == ["Glide.field.json"]
    assert any(p == ("Female Kliff Glide Override", 42) for _s, p in renamed)
    assert "second card" in out.info and "both halves" in out.info


def test_bundled_json_failure_is_reported_not_swallowed(tmp_path, monkeypatch):
    from cdumm.engine import import_handler as ih
    root = tmp_path / "mod"; root.mkdir()
    (root / "Glide.field.json").write_text(json.dumps(_format3("nope.pabgb")))
    monkeypatch.setattr(
        "cdumm.engine.json_patch_handler.is_natt_format_3", lambda p: True)
    monkeypatch.setattr(ih, "import_from_natt_format_3",
                        lambda jp, *a, **k: _Res("Glide", None, error="target not found"))
    primary = _Res("X", 1)
    out = ih._import_bundled_format3(root, tmp_path, object(), None, tmp_path, primary)
    assert "could not be imported" in out.info and "target not found" in out.info


def test_primary_error_short_circuits(tmp_path, monkeypatch):
    """A failed overlay import must not spawn a half-mod companion."""
    from cdumm.engine import import_handler as ih
    root = tmp_path / "mod"; root.mkdir()
    (root / "Glide.field.json").write_text(json.dumps(_format3("a.pabgb")))
    monkeypatch.setattr(
        "cdumm.engine.json_patch_handler.is_natt_format_3", lambda p: True)
    monkeypatch.setattr(ih, "import_from_natt_format_3",
                        lambda *a, **k: pytest.fail("must not be called"))
    primary = _Res("X", None, error="boom")
    assert ih._import_bundled_format3(root, tmp_path, object(), None, tmp_path, primary) is primary


def test_converter_leaves_format3_json_alone(tmp_path, monkeypatch, caplog):
    """The 'could not resolve ... .field.json' warning is the symptom the
    reporter's import produced; the converter must skip such files."""
    import logging

    from cdumm.engine import crimson_browser_handler as cbh
    root = tmp_path / "mod"; root.mkdir()
    (root / "Glide.field.json").write_text(json.dumps(_format3("characterinfo.pabgb")))
    game = tmp_path / "game"; game.mkdir()
    monkeypatch.setattr(cbh, "_is_format3_patch", lambda p: True)
    caplog.set_level(logging.DEBUG, logger=cbh.logger.name)
    manifest = {"id": "m", "files_dir": ".", "_base_dir": root}
    cbh.convert_to_paz_mod(manifest, game, tmp_path / "work")
    msgs = [r.getMessage() for r in caplog.records]
    assert not any("could not resolve" in m and "Glide.field.json" in m for m in msgs)
    assert any("Format 3 patch; left to the importer" in m for m in msgs)
