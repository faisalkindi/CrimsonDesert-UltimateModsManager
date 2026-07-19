"""After a game update, CDUMM's cached vanilla PAMT index (CDMods/vanilla) has
offsets that no longer match the freshly-patched .paz body, so extraction
throws a raw LZ4 error. _load_vanilla_table must fall back to the game's own
current index (correct for the installed build) rather than crash -- and, when
even that fails, raise a clear ConversionRefused instead of a decompression
traceback.
"""
from __future__ import annotations

import pytest

import cdumm.engine.json_patch_handler as jph
from cdumm.engine import v2_to_format3 as v2
from cdumm.engine.v2_to_format3 import ConversionRefused


class _Entry:
    def __init__(self, tag, game, offset=0, comp_size=0):
        # paz_file under game_dir so the remap branch is a no-op.
        self.paz_file = str(game / f"{tag}.paz")
        self.offset = offset
        self.comp_size = comp_size


def _setup(monkeypatch, game, *, cached_ok, live_ok, live_present=True,
           offsets_agree=True):
    (game / "CDMods" / "vanilla").mkdir(parents=True, exist_ok=True)
    # When offsets disagree the cache is stale (game moved); the code skips it
    # up front without needing an extraction error (the silent-wrong case).
    cached = _Entry("cached", game, offset=100)
    live = (_Entry("live", game, offset=100 if offsets_agree else 999)
            if live_present else None)

    def fake_find(name, d):
        return cached if "vanilla" in str(d).replace("\\", "/") else live

    def fake_extract(entry, paz):
        ok = cached_ok if entry is cached else live_ok
        if not ok:
            raise RuntimeError(
                "LZ4 decompress failed: the offset to copy is not "
                "contained in the decompressed buffer")
        return b"LIVE" if entry is live else b"CACHED"

    monkeypatch.setattr(jph, "_find_pamt_entry", fake_find)
    monkeypatch.setattr(jph, "_extract_from_paz", fake_extract)


def test_uses_cache_when_fresh(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, cached_ok=True, live_ok=True)
    assert v2._load_vanilla_table(tmp_path, "iteminfo.pabgb") == b"CACHED"


def test_falls_back_to_live_index_when_cache_stale(monkeypatch, tmp_path):
    # cached extraction throws (stale after a game update) -> use live index.
    _setup(monkeypatch, tmp_path, cached_ok=False, live_ok=True)
    assert v2._load_vanilla_table(tmp_path, "iteminfo.pabgb") == b"LIVE"


def test_skips_cache_when_offsets_disagree_even_if_it_would_decode(
        monkeypatch, tmp_path):
    # The silent-wrong case: an uncompressed table (many .pabgh) decodes
    # without error at the stale offset but returns garbage. The offset
    # mismatch must make the code use the live index anyway.
    _setup(monkeypatch, tmp_path, cached_ok=True, live_ok=True,
           offsets_agree=False)
    assert v2._load_vanilla_table(tmp_path, "iteminfo.pabgh") == b"LIVE"


def test_clear_error_when_both_fail(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, cached_ok=False, live_ok=False)
    with pytest.raises(ConversionRefused) as ei:
        v2._load_vanilla_table(tmp_path, "iteminfo.pabgb")
    msg = str(ei.value)
    assert "Fix Everything" in msg          # actionable, not a raw traceback
    assert "LZ4" not in msg


def test_missing_everywhere_still_refuses_cleanly(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, cached_ok=False, live_ok=True,
           live_present=False)
    with pytest.raises(ConversionRefused):
        v2._load_vanilla_table(tmp_path, "nope.pabgb")
