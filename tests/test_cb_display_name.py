"""CB display-name picker: use manifest.id only if it LOOKS like a
display name, else fall back to the archive's prettified name.

Witcher HUD case: author set manifest.id = 'mm' (a machine placeholder)
and CDUMM displayed 'Mm' instead of 'Witcher HUD' (which is right
there in the archive filename).
"""
from __future__ import annotations

from cdumm.engine.import_handler import _pick_cb_display_name


def test_descriptive_id_wins_over_archive_name():
    """If the author set a real display-name in manifest.id, keep it."""
    # Has a space → obviously a display name.
    assert _pick_cb_display_name(
        manifest_id="Witcher HUD",
        archive_stem="Witcher HUD-1432-1-1776623946") == "Witcher HUD"


def test_lazy_two_char_id_falls_back_to_archive():
    """C-Mm case: 2-char lowercase id → not a display name."""
    out = _pick_cb_display_name(
        manifest_id="mm",
        archive_stem="Witcher HUD-1432-1-1776623946")
    assert "Witcher" in out and "HUD" in out, (
        f"expected archive-based name, got {out!r}")


def test_short_lowercase_id_falls_back_to_archive():
    """4-char lowercase 'test' → not a display name."""
    out = _pick_cb_display_name(
        manifest_id="test",
        archive_stem="Better Status Bar-820-1-3-1775748542")
    assert "Better" in out or "Status" in out, (
        f"expected archive-based name, got {out!r}")


def test_snake_case_id_with_enough_chars_wins():
    """'my_cool_hud' — 11 chars, looks intentional — keep it."""
    out = _pick_cb_display_name(
        manifest_id="my_cool_hud",
        archive_stem="Some archive-100-1-0-1234567890")
    assert out == "my_cool_hud"


def test_mixed_case_short_id_wins():
    """'HUDv2' — 5 chars, mixed case — intentional, keep it."""
    out = _pick_cb_display_name(
        manifest_id="HUDv2",
        archive_stem="fallback-100-1-0-1234567890")
    assert out == "HUDv2"


def test_empty_archive_stem_forces_id_fallback():
    """If archive is unnamed, keep whatever the id is."""
    out = _pick_cb_display_name(
        manifest_id="mm",
        archive_stem="")
    assert out == "mm"


def test_none_manifest_id_uses_archive():
    """If manifest has no id at all, use the archive."""
    out = _pick_cb_display_name(
        manifest_id=None,
        archive_stem="Witcher HUD-1432-1-1776623946")
    assert "Witcher" in out
