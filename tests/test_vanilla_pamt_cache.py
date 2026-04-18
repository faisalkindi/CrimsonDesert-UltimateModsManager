"""Regression tests for the vanilla PAMT LRU cache.

The cache exists because `parse_pamt` on a 1.2M-entry vanilla PAMT
takes ~9s. A batch import of 29 mods that touch the same game
directory was re-parsing that file 29 times. These tests lock in
the caching, invalidation, and immutability contract so a future
refactor cannot silently regress it.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cdumm.engine.mod_health_check import (
    VanillaPamtEntry,
    _cached_vanilla_pamt_tuples,
    _load_vanilla_pamt,
)


@pytest.fixture(autouse=True)
def _clear_cache_and_force_python(monkeypatch):
    """Tests verify cache + Python-path semantics. The module-level
    Rust handle is patched out so tests don't need to mock cdumm_native
    and real PAMT files on disk."""
    _cached_vanilla_pamt_tuples.cache_clear()
    monkeypatch.setattr(
        "cdumm.engine.mod_health_check._NATIVE_PARSE_PAMT", None,
    )
    yield
    _cached_vanilla_pamt_tuples.cache_clear()


def _fake_entry(path="a/b.xml", paz_index=0, offset=0, comp=100, orig=200, flags=0):
    return SimpleNamespace(
        path=path, paz_index=paz_index,
        offset=offset, comp_size=comp, orig_size=orig, flags=flags,
    )


def test_repeat_calls_same_file_parse_once():
    """3 calls for the same file with identical mtime+size hit cache."""
    calls = []

    def fake_parse(path, paz_dir=None):
        calls.append(path)
        return [_fake_entry()]

    with patch("cdumm.archive.paz_parse.parse_pamt", fake_parse), \
         patch("os.stat", return_value=MagicMock(st_mtime_ns=111, st_size=5000)):
        _load_vanilla_pamt("/fake/0.pamt", "/fake")
        _load_vanilla_pamt("/fake/0.pamt", "/fake")
        _load_vanilla_pamt("/fake/0.pamt", "/fake")

    assert len(calls) == 1


def test_mtime_change_invalidates_cache():
    """File changed on disk → mtime changes → cache miss → fresh parse."""
    calls = []

    def fake_parse(path, paz_dir=None):
        calls.append(path)
        return [_fake_entry()]

    stat_result = MagicMock(st_mtime_ns=111, st_size=5000)
    with patch("cdumm.archive.paz_parse.parse_pamt", fake_parse), \
         patch("os.stat", return_value=stat_result):
        _load_vanilla_pamt("/fake/0.pamt", "/fake")

    stat_result2 = MagicMock(st_mtime_ns=222, st_size=5000)
    with patch("cdumm.archive.paz_parse.parse_pamt", fake_parse), \
         patch("os.stat", return_value=stat_result2):
        _load_vanilla_pamt("/fake/0.pamt", "/fake")

    assert len(calls) == 2


def test_different_files_cache_separately():
    calls = []

    def fake_parse(path, paz_dir=None):
        calls.append(path)
        return [_fake_entry()]

    with patch("cdumm.archive.paz_parse.parse_pamt", fake_parse), \
         patch("os.stat", return_value=MagicMock(st_mtime_ns=111, st_size=5000)):
        _load_vanilla_pamt("/dir_a/0.pamt", "/dir_a")
        _load_vanilla_pamt("/dir_b/0.pamt", "/dir_b")
        _load_vanilla_pamt("/dir_a/0.pamt", "/dir_a")

    assert calls == ["/dir_a/0.pamt", "/dir_b/0.pamt"]


def test_returned_entries_are_immutable():
    """VanillaPamtEntry is a NamedTuple — attempting to mutate must raise."""
    def fake_parse(path, paz_dir=None):
        return [_fake_entry()]

    with patch("cdumm.archive.paz_parse.parse_pamt", fake_parse), \
         patch("os.stat", return_value=MagicMock(st_mtime_ns=111, st_size=5000)):
        entries = _load_vanilla_pamt("/fake/0.pamt", "/fake")

    assert isinstance(entries, tuple)
    assert all(isinstance(e, VanillaPamtEntry) for e in entries)
    with pytest.raises(AttributeError):
        entries[0].offset = 999  # type: ignore[misc]


def test_namedtuple_carries_expected_fields():
    assert VanillaPamtEntry._fields == (
        "path", "paz_index", "offset", "comp_size", "orig_size", "flags",
    )


def test_derived_properties_match_pazentry_semantics():
    """compressed / encrypted / compression_type must match PazEntry."""
    # Uncompressed non-XML entry
    e = VanillaPamtEntry("foo/bar.bin", 0, 0, 100, 100, 0)
    assert e.compressed is False
    assert e.encrypted is False
    assert e.compression_type == 0

    # LZ4 compressed XML entry (flags bit 16..19 = 2, path endswith .xml)
    lz4_flags = 2 << 16
    e2 = VanillaPamtEntry("game/foo.xml", 0, 0, 50, 200, lz4_flags)
    assert e2.compressed is True
    assert e2.encrypted is True  # XML heuristic
    assert e2.compression_type == 2
