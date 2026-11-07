"""The Game Data grid on game 2.04.08 bytes, with the build layouts.

On 579e9e9 the grid decoded and gated 2.04 WantedInfo with the pre-2.04
schema (``schema_for_table`` returned ``get_schema``'s own object): it read
2.04's bytes in the pre-2.04 order and applied the pre-2.04 verified field,
so the price column showed 256 times the price, decoded and editable
(#458). Now the rows, the columns, the verified gate and editing come from
the 2.04.08 layout (``_grid_schema``), and a build layout is never
editable. On 2.04 bytes that no shipped layout fits, the pre-2.04 columns
come back with nothing verified, so nothing is editable either.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("qfluentwidgets")

from cdumm.semantic.parser import get_schema, parse_records_display
from tests.fixture_loaders import load_vanilla20408


def _wanted(body=None):
    from cdumm.gui.pages.game_data_page import _grid_schema
    body = body if body is not None else load_vanilla20408("wantedinfo.pabgb")
    header = load_vanilla20408("wantedinfo.pabgh")
    recs = parse_records_display("wantedinfo", body, header)
    schema, no_204_layout = _grid_schema("wantedinfo", body, header)
    return recs, schema, no_204_layout


def test_the_price_column_shows_the_price_and_the_rest_is_unverified():
    from cdumm.gui.pages.game_data_page import _shape_records
    recs, schema, no_204_layout = _wanted()
    assert schema.build == "2.04.08" and not no_204_layout
    cols, rows, total, _health = _shape_records(recs, schema)
    assert total == 35
    assert cols == ["_key", "_name", "_isBlocked", "_useTargetPrice",
                    "_increasePrice"]
    prices = {r[cols.index("_increasePrice")] for r in rows}
    assert prices == {"30", "50", "100", "500", "1500"}
    assert {r[cols.index("_useTargetPrice")] for r in rows} == {"(unverified)"}


def test_a_build_layout_offers_no_edits_even_on_a_verified_field():
    from cdumm.gui.pages.game_data_page import GameDataPage, _shape_records
    recs, schema, _no = _wanted()
    assert "_increasePrice" in schema.verified_fields
    cols, _rows, _total, _h = _shape_records(recs, schema)
    # _editable_columns doesn't touch the page, so no widget is needed.
    assert GameDataPage._editable_columns(None, cols, schema) == {}
    # The pre-2.04 schema would offer the price at its pre-2.04 offset,
    # which is why a 2.04 table is never gated with get_schema.
    assert GameDataPage._editable_columns(None, cols,
                                          get_schema("wantedinfo"))


def test_2_04_bytes_no_layout_fits_show_nothing_as_verified_or_editable():
    """A later patch can change a table so that no shipped layout fits.
    The grid then falls back to the pre-2.04 columns, but its verified
    field is the pre-2.04 one at the pre-2.04 offset, so it must not be
    shown as verified or offered for editing."""
    from cdumm.gui.pages.game_data_page import GameDataPage, _shape_records
    short = load_vanilla20408("wantedinfo.pabgb")[:-1]   # no longer tiles
    recs, schema, no_204_layout = _wanted(short)
    assert no_204_layout and schema.build is None
    assert schema.verified_fields == frozenset()
    assert [f.name for f in schema.fields] == [
        f.name for f in get_schema("wantedinfo").fields]
    cols, rows, _total, _h = _shape_records(recs, schema)
    assert {r[cols.index("_increasePrice")] for r in rows} == {"(unverified)"}
    assert GameDataPage._editable_columns(None, cols, schema) == {}
    # get_schema itself is untouched.
    assert get_schema("wantedinfo").verified_fields == frozenset(
        {"_increasePrice"})
