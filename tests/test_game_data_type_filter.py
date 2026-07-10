"""Regression: the Game Data "Type" filter must list EVERY indexed
extension, not just the 40 most common.

Once the index is built the dropdown is repopulated from it. It used to keep
only the top 40 extensions by file count, which silently dropped rare but
modding-critical formats - notably ``.pabgb`` keyed data tables (~130 files),
the exact format the mod maker edits - because bulk assets (``.wem`` /
``.paa`` / ``.dds``, hundreds of thousands each) crowded them out. The cap is
gone; these tests guard it staying gone.
"""
from __future__ import annotations

import os
import sqlite3

# Headless-safe import of the page module (no widget is constructed here).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from cdumm.gui.pages.game_data_page import GameDataPage


class _Capture:
    """Minimal stand-in for the page. ``_populate_type_filter`` only calls
    ``self._set_type_items``, so a real Qt widget isn't needed."""

    def __init__(self) -> None:
        self.items = None

    def _set_type_items(self, items) -> None:
        self.items = items


def _index_with(exts_counts):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE assets (ext TEXT)")
    for ext, n in exts_counts:
        con.executemany("INSERT INTO assets (ext) VALUES (?)", [(ext,)] * n)
    con.commit()
    return con


def test_type_filter_lists_all_extensions_no_40_cap():
    # 45 bulk formats so a rare one lands past position 40, plus the rare
    # .pabgb keyed-table format the mod maker actually edits.
    rows = [(f".bulk{i:02d}", 1000 - i) for i in range(45)]
    rows.append((".pabgb", 5))     # rarest -> a top-40 cap would drop it
    cap = _Capture()
    GameDataPage._populate_type_filter(cap, _index_with(rows))

    exts = [ext for _label, ext in cap.items]
    assert cap.items[0] == ("All types", None)
    assert ".pabgb" in exts
    assert len(exts) == 1 + 46     # "All types" + 45 bulk + .pabgb


def test_type_filter_skips_blank_and_none_extensions():
    cap = _Capture()
    GameDataPage._populate_type_filter(
        cap, _index_with([(".dds", 3), ("", 2), ("(none)", 4)]))
    exts = [ext for _label, ext in cap.items]
    assert exts == [None, ".dds"]


def test_type_filter_labels_show_grouped_counts():
    cap = _Capture()
    GameDataPage._populate_type_filter(
        cap, _index_with([(".pabgb", 134), (".wem", 375716)]))
    labels = [label for label, _ext in cap.items]
    assert ".wem  (375,716)" in labels   # most-common first, comma-grouped
    assert ".pabgb  (134)" in labels
