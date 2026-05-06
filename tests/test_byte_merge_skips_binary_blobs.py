"""Nexus regression (mrkillerhomer, 2026-05-03 v3.2.7 -> v3.2.8):
three texture mods (Nexus 920, 2233, 2126) that worked together on
v3.2.7 caused infinite-loading on v3.2.8 / v3.2.8.1. Reverting to
v3.2.7 made them work again.

Root cause: commit 57cfa29 'fix(apply): byte-merge fallback now
runs for non-pabgb entries' was meant to cover .paseq sequencer
files (GitHub #59 sub-issue 1). It DID, but it also accidentally
enabled byte-merge for DDS textures, .pathc texture data, audio,
and other self-contained binary blobs where partial-byte-edits
across mods produce a corrupt Frankenstein file.

mrkillerhomer's three mods overlap on textures in 0009. Pre-v3.2.8
they fell to last-wins (one mod's texture replaces the previous).
Post-v3.2.8 byte-merge fired and produced a corrupt DDS the GPU
choked on, hanging the loading screen.

Fix: filter byte-merge to formats where partial-byte-edits make
semantic sense (.pabgb tables, sequencer/XML/CSS/HTML, animation
conditions). Self-contained binary blobs fall back to last-wins.
"""
from __future__ import annotations

import pytest


def test_byte_merge_filter_excludes_dds():
    """Texture files must NOT be byte-merged."""
    from cdumm.engine.apply_engine import _entry_supports_byte_merge

    assert not _entry_supports_byte_merge(
        "character/textures/cd_phm_armor_diff.dds")
    assert not _entry_supports_byte_merge(
        "ui/icons/skill.dds")
    assert not _entry_supports_byte_merge(
        "anything.DDS")  # case-insensitive


def test_byte_merge_filter_excludes_pathc():
    """PATHC texture-data files: same self-contained-blob category."""
    from cdumm.engine.apply_engine import _entry_supports_byte_merge

    assert not _entry_supports_byte_merge(
        "meta/0.pathc")
    assert not _entry_supports_byte_merge(
        "character/textures/skin.pathc")


def test_byte_merge_filter_excludes_audio():
    from cdumm.engine.apply_engine import _entry_supports_byte_merge

    assert not _entry_supports_byte_merge("voice/line_001.ogg")
    assert not _entry_supports_byte_merge("music/theme.wav")
    assert not _entry_supports_byte_merge("vo/dialog.pawm")


def test_byte_merge_filter_excludes_image_formats():
    from cdumm.engine.apply_engine import _entry_supports_byte_merge

    assert not _entry_supports_byte_merge("ui/banner.png")
    assert not _entry_supports_byte_merge("ui/photo.jpg")
    assert not _entry_supports_byte_merge("ui/sprite.tga")


def test_byte_merge_filter_includes_pabgb():
    from cdumm.engine.apply_engine import _entry_supports_byte_merge

    assert _entry_supports_byte_merge(
        "gamedata/binary__/client/bin/iteminfo.pabgb")
    assert _entry_supports_byte_merge(
        "gamedata/dropsetinfo.pabgb")


def test_byte_merge_filter_includes_paseq_and_text_formats():
    """The original GitHub #59 sub-issue 1 fix targeted these formats.
    Make sure they STILL byte-merge after the regression fix."""
    from cdumm.engine.apply_engine import _entry_supports_byte_merge

    assert _entry_supports_byte_merge(
        "character/sequencer/cd_phm_anim.paseq")
    assert _entry_supports_byte_merge("ui/xml/menu.pac_xml")
    assert _entry_supports_byte_merge(
        "ui/xml/gamemain/play/worldmapview.css")
    assert _entry_supports_byte_merge(
        "ui/html/main.html")
    assert _entry_supports_byte_merge(
        "character/anim/condition.paac")


def test_byte_merge_filter_includes_xml_variants():
    from cdumm.engine.apply_engine import _entry_supports_byte_merge

    # Pearl Abyss XML variants
    assert _entry_supports_byte_merge("foo.pac_xml")
    assert _entry_supports_byte_merge("foo.xml")
    assert _entry_supports_byte_merge("foo.pamb_xml")


def test_byte_merge_filter_excludes_unknown_extensions_default():
    """Unknown extensions default to NOT-mergeable. Last-wins is the
    safer fallback than producing a garbage merge for an unfamiliar
    binary format."""
    from cdumm.engine.apply_engine import _entry_supports_byte_merge

    assert not _entry_supports_byte_merge("blob.unknown_ext")
    assert not _entry_supports_byte_merge("noext")
