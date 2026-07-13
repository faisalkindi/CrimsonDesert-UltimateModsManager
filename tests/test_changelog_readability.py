"""The patch-notes dialog was a small box full of unbroken text.

Reported 2026-07-12 with a screenshot: the "What's New" dialog sat at a
fixed 560x350 regardless of window size, and every note rendered as a bold
lead-in glued to a long paragraph with 3px between items and no leading --
so it read as one continuous blob.

Two independent things to pin, so neither regresses quietly:
  * each note splits into a headline line + a detail line, with real
    spacing and line-height;
  * the dialog scales with the parent window instead of a fixed sliver.
"""
from __future__ import annotations

import re

import pytest

from cdumm.gui.changelog import (
    CHANGELOG, PatchNotesDialog, _render_note, get_changelog_html,
    get_latest_notes_html)


# ── note rendering ───────────────────────────────────────────────────────

def test_bold_lead_in_becomes_its_own_line():
    note = "<b>Sockets work again.</b> The July patch moved the item table."
    out = _render_note(note)
    # headline is a block of its own, not inline with the detail
    assert '<div style="font-weight: 600' in out
    assert "Sockets work again." in out
    # detail follows in a separate block
    assert out.count("<div") == 2
    assert "The July patch moved the item table." in out
    # and the two are no longer run together in one text flow
    assert "</div>" in out.split("The July patch")[0]


def test_note_without_a_lead_in_still_gets_its_spacing():
    note = "Plain single-sentence note with no bold headline."
    out = _render_note(note)
    assert note in out
    assert "margin-bottom: 20px" in out   # separated like every other note


def test_lead_in_spanning_multiple_lines_still_splits():
    """re.DOTALL matters: several notes wrap across source lines."""
    note = "<b>A headline\nthat wraps.</b> And a body\nthat wraps too."
    out = _render_note(note)
    assert "font-weight: 600" in out
    assert "And a body" in out


# ── spacing ──────────────────────────────────────────────────────────────

def test_notes_have_real_separation_and_leading():
    html = get_latest_notes_html()
    # the old values were margin-bottom: 3px and no line-height at all
    assert "margin-bottom: 20px" in html
    assert "line-height: 150%" in html
    assert "margin-bottom: 3px" not in html


def test_gap_between_notes_beats_the_gap_inside_one():
    """The bug in miniature.

    Qt's rich-text engine ignores margin-bottom on <li>, so a naive fix puts
    the spacing there and items end up packed TIGHTER than the headline/body
    gap within a single item -- which reads backwards and still looks like a
    blob. The separation has to live on the last block inside each note.
    """
    out = _render_note("<b>Head.</b> Body text.")
    head_gap = int(re.search(r"margin-bottom: (\d+)px", out).group(1))
    tail_gap = int(re.findall(r"margin-bottom: (\d+)px", out)[-1])
    assert tail_gap > head_gap, (
        "the gap after a note must exceed the gap between its headline and "
        "its own body, or the notes read as one continuous block")


def test_every_note_in_the_real_changelog_renders():
    """Guard against a note shape the renderer chokes on."""
    html = get_changelog_html()
    assert html.count("<li") == sum(len(e["notes"]) for e in CHANGELOG)
    assert "<b><b>" not in html


def test_versions_are_separated_in_the_full_history():
    html = get_changelog_html()
    if len(CHANGELOG) > 1:
        assert "<hr" in html          # visual break between releases
    latest = get_latest_notes_html()
    assert "<hr" not in latest        # ...but not before a lone entry


# ── sizing ───────────────────────────────────────────────────────────────

class _FakeWindow:
    def __init__(self, w, h):
        self._w, self._h = w, h

    def width(self):
        return self._w

    def height(self):
        return self._h


def test_no_parent_falls_back_to_the_floor_not_the_old_sliver():
    w, h = PatchNotesDialog._sized_to(None)
    assert (w, h) == (760, 460)
    assert w > 560          # the old fixed width
    assert h > 350          # the old fixed height


def test_dialog_grows_with_the_window():
    small = PatchNotesDialog._sized_to(_FakeWindow(1280, 800))
    large = PatchNotesDialog._sized_to(_FakeWindow(2560, 1440))
    assert large[0] > small[0]
    assert large[1] > small[1]


def test_small_window_still_gets_a_usable_floor():
    w, h = PatchNotesDialog._sized_to(_FakeWindow(700, 500))
    assert (w, h) == (760, 460)


def test_ultrawide_is_capped_so_lines_stay_readable():
    w, h = PatchNotesDialog._sized_to(_FakeWindow(5120, 2160))
    assert w == 1040
    assert h == 760


@pytest.mark.parametrize("pw,ph", [(1280, 800), (1920, 1080), (2560, 1440)])
def test_size_is_always_within_floor_and_ceiling(pw, ph):
    w, h = PatchNotesDialog._sized_to(_FakeWindow(pw, ph))
    assert 760 <= w <= 1040
    assert 460 <= h <= 760
