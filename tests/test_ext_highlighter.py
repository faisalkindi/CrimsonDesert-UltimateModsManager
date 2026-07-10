"""The Game Data "New to modding" guides colour their file-type tokens
(.dds, .pabgb, ...) via _ExtHighlighter so they stand out from the prose.
These guard the token regex: it must catch every real extension and must
NOT colour sentence dots or single-letter abbreviations.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from cdumm.gui.pages.game_data_page import _ExtHighlighter

RX = _ExtHighlighter._RX


def test_matches_real_extensions():
    for tok in (".dds", ".png", ".pabgb", ".pac_xml", ".paa_metabin",
                ".mp4", ".spline2d", ".ttf", ".mi", ".uianiminit"):
        assert RX.fullmatch(tok), tok


def test_picks_tokens_out_of_a_mixed_line():
    assert RX.findall(".paseq / .paseqc / .pastage") == [
        ".paseq", ".paseqc", ".pastage"]


def test_ignores_sentence_dots_and_abbreviations():
    # a period ending a sentence is not a file type
    assert RX.findall("that draw surfaces. Next line") == []
    # single-letter abbreviations like e.g. / i.e. must not be coloured
    assert RX.findall("e.g. a thing, i.e. another") == []
