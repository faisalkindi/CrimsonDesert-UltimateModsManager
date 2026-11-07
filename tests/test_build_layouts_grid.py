"""The Game Data grid on game 2.04.08 bytes, with the build layouts.

The grid used to decode a 2.04 table with ``schema_for_table`` and then
shape and gate it with ``get_schema``, the pre-2.04 layout. On wantedinfo
that put the pre-2.04 verified field over 2.04's bytes: the price column
showed 256 times the price, decoded and editable (#458). Now the rows,
the columns, the verified gate and editing all come from the 2.04.08
layout, and a build layout is never editable.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("qfluentwidgets")

from cdumm.engine.schema_verify import schema_for_table
from cdumm.semantic.parser import get_schema, parse_records_display
from tests.fixture_loaders import load_vanilla20408


def _wanted():
    body = load_vanilla20408("wantedinfo.pabgb")
    header = load_vanilla20408("wantedinfo.pabgh")
    return (parse_records_display("wantedinfo", body, header),
            schema_for_table("wantedinfo", body, header))


def test_the_price_column_shows_the_price_and_the_rest_is_unverified():
    from cdumm.gui.pages.game_data_page import _shape_records
    recs, schema = _wanted()
    cols, rows, total, _health = _shape_records(recs, schema)
    assert total == 35
    assert cols == ["_key", "_name", "_isBlocked", "_useTargetPrice",
                    "_increasePrice"]
    prices = {r[cols.index("_increasePrice")] for r in rows}
    assert prices == {"30", "50", "100", "500", "1500"}
    assert {r[cols.index("_useTargetPrice")] for r in rows} == {"(unverified)"}


def test_a_build_layout_offers_no_edits_even_on_a_verified_field():
    from cdumm.gui.pages.game_data_page import GameDataPage, _shape_records
    recs, schema = _wanted()
    assert "_increasePrice" in schema.verified_fields
    cols, _rows, _total, _h = _shape_records(recs, schema)
    # _editable_columns doesn't touch the page, so no widget is needed.
    assert GameDataPage._editable_columns(None, cols, schema) == {}
    # The pre-2.04 schema would still offer the price, which is why the
    # grid no longer gates a 2.04 table with get_schema.
    assert GameDataPage._editable_columns(None, cols,
                                          get_schema("wantedinfo"))
