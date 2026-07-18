"""Game Data search-table helpers: human-readable sizes, numeric size sort,
category colouring, path splitting, and the per-row cell builder (Name holds
the full path in UserRole; Size is a numeric-sorting, right-aligned cell)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt

from cdumm.gui.pages.game_data_page import (
    _build_row_items, _human_size, _NumericItem, _split_path, _type_colour)


def test_human_size():
    assert _human_size(0) == "0 B"
    assert _human_size(512) == "512 B"
    assert _human_size(1536) == "1.5 KB"
    assert _human_size(781646) == "763 KB"
    assert _human_size(5_000_000) == "4.8 MB"
    assert _human_size(3_221_225_472) == "3.0 GB"


def test_type_colour():
    assert _type_colour(".wem") == _type_colour(".bnk")     # both audio
    assert _type_colour(".PABGB") == _type_colour(".pabgb")  # case-insensitive
    assert _type_colour(".dds") is not None
    assert _type_colour(".zzz_unknown") is None
    assert _type_colour("") is None


def test_split_path():
    assert _split_path("character/npc/merchant.pabgb") == (
        "character/npc", "merchant.pabgb")
    assert _split_path("loose.dds") == ("", "loose.dds")
    assert _split_path("a\\b\\c.pat") == ("a\\b", "c.pat")


def test_numeric_item_sorts_by_value(qtbot):
    big = _NumericItem("763 KB", 781646)
    small = _NumericItem("1.5 KB", 1536)
    assert small < big
    assert not (big < small)


def test_build_row_items(qtbot):
    name, folder, archive, typ, size = _build_row_items({
        "path": "character/npc/merchant.pabgb", "archive": "0011",
        "ext": ".pabgb", "orig_size": 781646})

    assert name.text() == "merchant.pabgb"
    assert name.data(Qt.ItemDataRole.UserRole) == "character/npc/merchant.pabgb"
    assert folder.text() == "character/npc"
    assert archive.text() == "0011"
    assert typ.text() == ".pabgb"
    assert not typ.font().bold()   # Type must inherit the table font, not override it

    assert isinstance(size, _NumericItem)
    assert size.text() == "763 KB"                  # human-readable display
    assert size.toolTip() == "781,646 bytes"        # exact bytes preserved
    assert size.textAlignment() & Qt.AlignmentFlag.AlignRight


def test_size_column_sorts_by_bytes_not_text(qtbot):
    """End-to-end: clicking the Size header must order rows by real byte
    count, not by the "763 KB" display string."""
    from qfluentwidgets import TableWidget
    t = TableWidget()
    qtbot.addWidget(t)
    t.setColumnCount(5)
    data = [
        {"path": "a/small.dds", "archive": "1", "ext": ".dds", "orig_size": 100},
        {"path": "a/huge.wem", "archive": "1", "ext": ".wem", "orig_size": 9_000_000},
        {"path": "a/mid.pat", "archive": "1", "ext": ".pat", "orig_size": 50_000},
    ]
    t.setSortingEnabled(False)
    t.setRowCount(len(data))
    for i, r in enumerate(data):
        for c, cell in enumerate(_build_row_items(r)):
            t.setItem(i, c, cell)
    t.setSortingEnabled(True)

    t.sortItems(4, Qt.SortOrder.AscendingOrder)
    assert [t.item(row, 0).text() for row in range(3)] == [
        "small.dds", "mid.pat", "huge.wem"]
    t.sortItems(4, Qt.SortOrder.DescendingOrder)
    assert [t.item(row, 0).text() for row in range(3)] == [
        "huge.wem", "mid.pat", "small.dds"]


def test_zebra_delegate_attaches_and_tracks_selection(qtbot):
    """setItemDelegate must reassign qfluentwidgets' self.delegate so hover /
    selection updates reach our stripe delegate."""
    from qfluentwidgets import TableWidget
    from cdumm.gui.pages.game_data_page import _ZebraDelegate
    t = TableWidget()
    qtbot.addWidget(t)
    d = _ZebraDelegate(t)
    t.setItemDelegate(d)
    assert t.itemDelegate() is d
    assert t.delegate is d
