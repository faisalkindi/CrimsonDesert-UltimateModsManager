"""``post_update_check.check_build_layouts``, the canary for game 2.04.

The check loads every table a build layout covers from the install and asks
``schema_for_table``, the Game Data grid's own call, which layout it gets.
Here the install is the committed 2.04.08 fixtures: ``_load`` serves those,
and every other table is "not found". No game folder is touched.
"""
from __future__ import annotations

import importlib.util
import zlib
from pathlib import Path

import pytest

from cdumm.semantic.parser import parse_pabgh_index
from cdumm.semantic.record_header import NAME_FIRST, header_end

_REPO = Path(__file__).resolve().parents[1]
_FIX = _REPO / "tests" / "fixtures"


def _canary():
    spec = importlib.util.spec_from_file_location(
        "post_update_check", _REPO / "scripts" / "post_update_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _serve(folder: str, patch: dict | None = None):
    def load(_game_dir, name):
        if patch and name in patch:
            return patch[name]
        p = _FIX / folder / (name + ".zlib")
        if not p.exists():
            raise FileNotFoundError(name)
        return zlib.decompress(p.read_bytes())
    return load


def _fixture(folder: str, name: str) -> bytes:
    return zlib.decompress((_FIX / folder / (name + ".zlib")).read_bytes())


@pytest.fixture
def canary(monkeypatch):
    mod = _canary()
    monkeypatch.setattr(mod, "_load", _serve("vanilla20408"))
    return mod


def test_the_committed_2_04_08_tables_all_fit_byte_for_byte(canary):
    ok, detail = canary.check_build_layouts(Path("unused"))
    assert ok, detail
    assert detail.startswith("137 tables: 21 byte-identical"), detail
    assert "0 fit by tiling, 0 name-first with no fitting layout" in detail
    assert "116 not found" in detail


def test_a_changed_table_that_still_tiles_is_counted_as_fitting(canary,
                                                               monkeypatch):
    body = bytearray(_fixture("vanilla20408", "wantedinfo.pabgb"))
    header = _fixture("vanilla20408", "wantedinfo.pabgh")
    ks, offs = parse_pabgh_index(header, "wantedinfo")
    body[header_end(body, min(offs.values()), ks, NAME_FIRST) + 2] ^= 1
    monkeypatch.setattr(canary, "_load", _serve(
        "vanilla20408", {"wantedinfo.pabgb": bytes(body)}))
    ok, detail = canary.check_build_layouts(Path("unused"))
    assert ok, detail
    assert "20 byte-identical" in detail and "1 fit by tiling" in detail


def test_a_name_first_table_its_layout_no_longer_fits_is_a_failure(
        canary, monkeypatch):
    short = _fixture("vanilla20408", "wantedinfo.pabgb")[:-1]
    monkeypatch.setattr(canary, "_load", _serve(
        "vanilla20408", {"wantedinfo.pabgb": short}))
    ok, detail = canary.check_build_layouts(Path("unused"))
    assert not ok
    assert "1 name-first with no fitting layout" in detail
    assert "LOST: wantedinfo" in detail


def test_pre_2_04_tables_are_skipped_not_failed(monkeypatch):
    mod = _canary()
    monkeypatch.setattr(mod, "_load", _serve("vanilla116"))
    ok, detail = mod.check_build_layouts(Path("unused"))
    assert ok, detail
    assert "0 name-first with no fitting layout" in detail
    assert "key-first (pre-2.04)" in detail
