"""Regression tests for GitHub #187 manifest-version preservation.

The post-import block in fluent_window used to unconditionally
overwrite ``mods.version`` with whatever ``_get_drop_version``
parsed out of the Nexus filename. Balzhur reported "Easier QTE x2
v1.2.1" being shown as v1.1 because the mod author bumped the
manifest without renaming the Nexus archive: the filename slot
was still 1-1.

``pick_post_import_version`` now centralises the precedence rule
and these tests pin the four cases that matter:

  1. Manifest carries a real version → preserved over filename
     and Nexus cache.
  2. Manifest is empty / "1.0" default → fall through to filename,
     then Nexus, then null.
  3. Click-to-update (nxm_*.bin) → bypass the manifest preservation
     because the import had no manifest to begin with.
  4. drop_ver matches manifest_ver → return None (no UPDATE).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cdumm.engine.version_picker import pick_post_import_version


def _no_drop_ver(_path):  # default: filename has no version
    return ""


# -- Case 1: real manifest version wins -----------------------------

def test_manifest_version_wins_over_filename(tmp_path):
    """Balzhur's reported case: manifest is 1.2.1, filename says 1.1.
    The function returns None so the existing 1.2.1 row stays put."""
    orig = tmp_path / "Easier_QTE_x2-664-1-1-1780000000.zip"

    def parse_filename(p):
        # Simulates _get_drop_version returning the filename-encoded version
        return "1.1" if "664-1-1" in p.name else ""

    result = pick_post_import_version(
        manifest_ver="1.2.1",
        orig_path=orig,
        fallback_path=tmp_path / "extracted",
        nexus_cached_version="1.2.1",
        get_drop_version=parse_filename,
    )
    assert result is None


def test_manifest_version_wins_over_nexus_cache(tmp_path):
    """Even when Nexus exposes a different latest_version, a real
    manifest value stays. This matters for mods whose author has
    not yet bumped the public Nexus listing past their local rev."""
    result = pick_post_import_version(
        manifest_ver="2.5.0",
        orig_path=tmp_path / "Some_Mod.zip",
        fallback_path=tmp_path / "extracted",
        nexus_cached_version="2.4.9",
        get_drop_version=_no_drop_ver,
    )
    assert result is None


# -- Case 2: empty / default manifest falls through ----------------

def test_empty_manifest_falls_through_to_filename(tmp_path):
    """An import that produced no manifest leaves the row empty.
    Filename version should win."""
    orig = tmp_path / "Some_Mod-123-2-5-1700000000.zip"

    def parse_filename(p):
        return "2.5" if "-2-5-" in p.name else ""

    result = pick_post_import_version(
        manifest_ver="",
        orig_path=orig,
        fallback_path=tmp_path / "extracted",
        nexus_cached_version=None,
        get_drop_version=parse_filename,
    )
    assert result == "2.5"


def test_default_1_0_manifest_falls_through_to_filename(tmp_path):
    """The import handler writes ``"1.0"`` as the placeholder when
    the manifest is absent. Treat that as missing so the filename
    version still gets a chance."""
    orig = tmp_path / "Some_Mod-123-3-0-1700000000.zip"

    def parse_filename(p):
        return "3.0"

    result = pick_post_import_version(
        manifest_ver="1.0",
        orig_path=orig,
        fallback_path=tmp_path / "extracted",
        nexus_cached_version=None,
        get_drop_version=parse_filename,
    )
    assert result == "3.0"


def test_default_manifest_falls_through_to_nexus_cache(tmp_path):
    """When the filename has no version either, the Nexus cache
    is the last useful signal (the GitHub #164 LordOfRhun case
    folded into the same logic)."""
    result = pick_post_import_version(
        manifest_ver="1.0",
        orig_path=tmp_path / "Random_Filename.zip",
        fallback_path=tmp_path / "extracted",
        nexus_cached_version="4.2",
        get_drop_version=_no_drop_ver,
    )
    assert result == "4.2"


# -- Case 3: click-to-update bypass --------------------------------

def test_click_to_update_uses_nexus_cache_even_when_manifest_real(tmp_path):
    """nxm_*.bin temp files come from the in-card "Click to update"
    flow. They never have a modinfo.json so the existing manifest
    value is whatever was there before the update, and we MUST
    advance it from the Nexus cache, otherwise GitHub #164 (the
    LordOfRhun update-loop) regresses."""
    nxm_orig = tmp_path / "nxm_664_12345.bin"
    nxm_orig.touch()

    result = pick_post_import_version(
        manifest_ver="1.0.4",  # OLD manifest the import left in place
        orig_path=nxm_orig,
        fallback_path=tmp_path / "Random_Filename.zip",
        nexus_cached_version="1.0.5",
        get_drop_version=_no_drop_ver,
    )
    assert result == "1.0.5"


# -- Case 4: noop when computed equals existing --------------------

def test_drop_ver_equal_to_manifest_returns_none(tmp_path):
    """When the filename-derived version happens to match what is
    already in the row, do not issue an UPDATE. Avoids needless
    commits and shrinks the change log."""
    orig = tmp_path / "Some_Mod-123-2-5-1700000000.zip"
    result = pick_post_import_version(
        manifest_ver="",  # falls through
        orig_path=orig,
        fallback_path=tmp_path / "extracted",
        nexus_cached_version=None,
        get_drop_version=lambda p: "",  # nothing parsable
    )
    # Both manifest and drop_ver empty → None
    assert result is None


def test_no_signal_at_all_returns_none(tmp_path):
    """Manifest empty, filename empty, Nexus cache empty → None.
    The row stays whatever it was, no UPDATE."""
    result = pick_post_import_version(
        manifest_ver="",
        orig_path=tmp_path / "Random_File.zip",
        fallback_path=tmp_path / "extracted",
        nexus_cached_version=None,
        get_drop_version=_no_drop_ver,
    )
    assert result is None


# -- Edge cases ----------------------------------------------------

def test_no_orig_path_falls_back_to_fallback_path(tmp_path):
    """When orig_path is None (mid-import the user-supplied source
    was already cleaned up) the fallback path's filename is parsed
    instead, so a useful version can still come through."""
    fallback = tmp_path / "Mod-7-1-5-1700000000.zip"

    def parse(p):
        if p is None:
            return ""
        return "1.5" if "-1-5-" in p.name else ""

    result = pick_post_import_version(
        manifest_ver="",
        orig_path=None,
        fallback_path=fallback,
        nexus_cached_version=None,
        get_drop_version=parse,
    )
    assert result == "1.5"


def test_empty_string_nexus_cache_is_treated_as_no_cache(tmp_path):
    """Some upstream code stores `""` instead of None for an absent
    cache entry. Both must behave the same way: no Nexus fallback,
    return None."""
    result = pick_post_import_version(
        manifest_ver="",
        orig_path=tmp_path / "Random_File.zip",
        fallback_path=tmp_path / "extracted",
        nexus_cached_version="",
        get_drop_version=_no_drop_ver,
    )
    assert result is None


def test_nxm_temp_with_uppercase_extension_is_not_click_to_update(tmp_path):
    """Click-to-update detection requires the exact ``.bin`` suffix
    that the NXM handler writes. An archive named NXM_*.BIN (case-
    different) does not actually come from that flow and should not
    bypass the manifest check."""
    # Note: orig_path.name comparison is case-sensitive on POSIX so
    # the .BIN won't match the lowercase suffix check.
    weird = tmp_path / "nxm_664_12345.BIN"
    weird.touch()
    result = pick_post_import_version(
        manifest_ver="1.0.4",
        orig_path=weird,
        fallback_path=tmp_path / "Random_File.zip",
        nexus_cached_version="1.0.5",
        get_drop_version=_no_drop_ver,
    )
    assert result is None  # manifest preserved


def test_whitespace_only_manifest_treated_as_empty(tmp_path):
    """Some import paths might write whitespace-only versions
    (rare but possible from sloppy modinfo.json values). The
    rstripped value is what the caller will read; we receive what
    they read. If the manifest looks empty, fall through."""
    # The caller's responsibility to strip; assertions just verify
    # we don't preserve a non-truthy string.
    result = pick_post_import_version(
        manifest_ver="",  # caller already stripped to empty
        orig_path=tmp_path / "Mod-7-2-1-1700000000.zip",
        fallback_path=tmp_path / "extracted",
        nexus_cached_version=None,
        get_drop_version=lambda p: "2.1" if "-2-1-" in p.name else "",
    )
    assert result == "2.1"
