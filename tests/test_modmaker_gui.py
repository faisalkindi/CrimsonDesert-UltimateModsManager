"""GUI-logic tests for the Game Data mod maker — the editable-column gate
that decides which grid cells a user may edit into a Format 3 mod.

Runs headless (offscreen). Skips cleanly if PySide6 / qfluentwidgets aren't
available in the environment.
"""
from __future__ import annotations

import os
from types import SimpleNamespace as NS

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
pytest.importorskip("qfluentwidgets")


@pytest.fixture(scope="module")
def _qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def page(_qapp):
    from cdumm.gui.pages.game_data_page import GameDataPage
    return GameDataPage()


def _f(name, fmt, ftype="direct_u32", td=None):
    return NS(name=name, struct_fmt=fmt, field_type=ftype, type_descriptor=td)


def _schema(fields, verified):
    return NS(
        fields=fields,
        verified_fields=frozenset(verified) if verified is not None else None,
    )


def test_editable_columns_only_verified_scalars(page):
    fields = [
        _f("_key", "I"), _f("_name", None, "CString"),
        _f("_increasePrice", "I"), _f("_isBlocked", "B"),
        _f("_desc", None, "CString"),
    ]
    cols = ["_key", "_name", "_increasePrice", "_isBlocked", "_desc"]
    ed = page._editable_columns(
        cols, _schema(fields, ["_increasePrice", "_isBlocked"]))
    assert sorted(ed) == [2, 3]                       # verified scalars only
    assert 0 not in ed and 1 not in ed and 4 not in ed  # meta + strings never


def test_editable_columns_respects_verified_subset(page):
    fields = [_f("_key", "I"), _f("_name", None, "CString"),
              _f("_a", "I"), _f("_b", "I")]
    cols = ["_key", "_name", "_a", "_b"]
    ed = page._editable_columns(cols, _schema(fields, ["_a"]))
    assert sorted(ed) == [2]                          # _b unverified -> locked


def test_uncurated_table_exposes_nothing(page):
    """A table with no verified set (verified_fields is None) must never be
    editable — we can't vouch for any offset, so the maker stays off."""
    fields = [_f("_key", "I"), _f("_name", None, "CString"), _f("_price", "I")]
    cols = ["_key", "_name", "_price"]
    assert page._editable_columns(cols, _schema(fields, None)) == {}


def test_variable_field_not_editable_even_if_verified(page):
    """A verified but variable-length field (CArray override, no struct_fmt)
    is not a fixed-width scalar, so it stays read-only; the scalar next to it
    still edits."""
    fields = [
        _f("_key", "I"), _f("_name", None, "CString"),
        _f("_list", None, "direct", td="CArray<Foo>"),
        _f("_price", "I"),
    ]
    cols = ["_key", "_name", "_list", "_price"]
    ed = page._editable_columns(cols, _schema(fields, ["_list", "_price"]))
    assert 2 not in ed          # variable list -> not editable
    assert 3 in ed              # scalar price -> editable


def test_none_schema_is_empty(page):
    assert page._editable_columns(["_key", "_name"], None) == {}
