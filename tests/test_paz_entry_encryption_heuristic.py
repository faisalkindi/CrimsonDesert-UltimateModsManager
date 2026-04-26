"""Encryption-detection regression for non-XML text formats.

Bug report (TheUnLuckyOnes, 2026-04-26): Dark Mode Map mod crashed
the game on map open. Root cause: CDUMM's encryption heuristic in
PazEntry only checked .xml extension. CSS files inside ui/xml/ are
also encrypted by the engine, but the heuristic returned False, so
the apply pipeline wrote the modded CSS to the overlay PAZ
unencrypted. The game's VFS then tried to ChaCha20-decrypt those
unencrypted bytes, got garbage, and crashed when parsing the file.

This was fixed in v2.1.2 (per its changelog) but the v3.0 rewrite
narrowed the heuristic back down to '.xml' only — silent regression.
JMM ships the same Dark Mode Map mod and it works there.
"""
from __future__ import annotations

from cdumm.archive.paz_parse import PazEntry


def _make(path: str) -> PazEntry:
    """Build a stub PazEntry for heuristic-only testing."""
    return PazEntry(
        path=path, paz_file="x", offset=0, comp_size=10,
        orig_size=20, flags=0, paz_index=0,
    )


def test_xml_files_still_detected_as_encrypted() -> None:
    """No regression on the original case — XML files must still
    be flagged as encrypted."""
    assert _make("ui/xml/foo/bar.xml").encrypted is True
    assert _make("anywhere/baz.xml").encrypted is True


def test_css_inside_ui_xml_detected_as_encrypted() -> None:
    """Dark Mode Map's worldmapview.css path. This is the actual
    file path from the bug report; the heuristic must catch it."""
    css_path = "ui/xml/gamemain/play/worldmapview.css"
    assert _make(css_path).encrypted is True, (
        f"CSS file at {css_path} not flagged as encrypted. The "
        f"apply pipeline will write the modded overlay without "
        f"ChaCha20 re-encryption and the game will crash on map "
        f"open. This is the v2.1.2 regression returning.")


def test_html_and_js_inside_ui_xml_detected_as_encrypted() -> None:
    """Other text formats the engine encrypts. Same root cause —
    if any of these get modded, the overlay must re-encrypt."""
    assert _make("ui/xml/menu/main.html").encrypted is True
    assert _make("ui/xml/scripts/init.js").encrypted is True


def test_binary_formats_not_flagged_as_encrypted() -> None:
    """No regression — binary formats stay unencrypted. PABGB,
    PAZ-internal stuff, textures must not get accidentally
    flagged as encrypted."""
    assert _make("0008/data.pabgb").encrypted is False
    assert _make("textures/foo.dds").encrypted is False
    assert _make("audio/music.bnk").encrypted is False
    assert _make("0014/0.paz").encrypted is False


def test_explicit_override_still_wins() -> None:
    """When extraction auto-detects encryption and sets
    _encrypted_override, that takes precedence over the heuristic."""
    e = _make("0008/data.pabgb")
    e._encrypted_override = True
    assert e.encrypted is True
    e2 = _make("ui/xml/foo.xml")
    e2._encrypted_override = False
    assert e2.encrypted is False
