"""Round 6 systematic-debugging fixes: defensive coercion of Nexus
API field values that could be None.

dict.get(key, default) only returns default when key is MISSING.
If key exists with value null/None, get() returns None — not the
default. Several int fields in NexusFileInfo / NexusFileUpdate /
NexusModInfo could end up None and break downstream code (sort,
arithmetic, attribute access on None).
"""
from __future__ import annotations

from unittest.mock import patch

from cdumm.engine.nexus_api import (
    get_mod_files, get_mod_info,
)


def test_get_mod_files_handles_null_uploaded_timestamp() -> None:
    """When Nexus returns null for uploaded_timestamp (rather than
    omitting the key), the sort must NOT raise TypeError. None < int
    crashes Python sort and the entire mod gets silently skipped."""
    api_response = {
        "files": [
            {"file_id": 1, "name": "A", "version": "1",
             "uploaded_timestamp": None,  # null in JSON
             "file_name": "a.zip", "category_id": 1},
            {"file_id": 2, "name": "B", "version": "2",
             "uploaded_timestamp": 1000,
             "file_name": "b.zip", "category_id": 1},
        ],
        "file_updates": [],
    }
    with patch("cdumm.engine.nexus_api._api_request",
               return_value=api_response):
        result = get_mod_files(100, "key")
    assert result is not None, (
        "get_mod_files must not crash when a file has null "
        "uploaded_timestamp — None vs int sort raises TypeError")
    files, _updates = result
    assert len(files) == 2
    # Sort still works; None coerces to 0 → file 1 sorts last
    # (uploaded_timestamp=0 < 1000)
    assert files[0].file_id == 2 and files[1].file_id == 1


def test_get_mod_files_handles_null_file_id() -> None:
    """Defensive: file_id = None shouldn't crash the constructor or
    downstream code that uses file_id as a dict key."""
    api_response = {
        "files": [
            {"file_id": None, "name": "X", "version": "1",
             "uploaded_timestamp": 100, "file_name": "x.zip",
             "category_id": 1},
        ],
        "file_updates": [],
    }
    with patch("cdumm.engine.nexus_api._api_request",
               return_value=api_response):
        result = get_mod_files(100, "key")
    assert result is not None
    files, _ = result
    # file_id should be coerced to 0, not None
    assert files[0].file_id == 0


def test_get_mod_files_handles_null_chain_timestamps() -> None:
    """file_updates with null uploaded_timestamp must not break the
    chain-walk tiebreak (which compares timestamps)."""
    api_response = {
        "files": [
            {"file_id": 1, "name": "A", "version": "1",
             "uploaded_timestamp": 100, "file_name": "a.zip",
             "category_id": 1},
            {"file_id": 2, "name": "A", "version": "2",
             "uploaded_timestamp": 200, "file_name": "b.zip",
             "category_id": 1},
        ],
        "file_updates": [
            {"old_file_id": 1, "new_file_id": 2,
             "uploaded_timestamp": None},
        ],
    }
    with patch("cdumm.engine.nexus_api._api_request",
               return_value=api_response):
        result = get_mod_files(100, "key")
    assert result is not None
    _files, updates = result
    # uploaded_timestamp coerced to 0, not None
    assert updates[0].uploaded_timestamp == 0


def test_get_mod_info_name_default_is_string_not_int() -> None:
    """get_mod_info defaults `name` to mod_id (an int!) if API
    response lacks 'name'. Type contract says str. Default should
    be empty string."""
    api_response = {
        "mod_id": 1234,
        # NO "name" key
        "version": "1.0",
        "author": "x",
    }
    with patch("cdumm.engine.nexus_api._api_request",
               return_value=api_response):
        info = get_mod_info(1234, "key")
    assert info is not None
    assert isinstance(info.name, str), (
        f"get_mod_info should default name to '', got "
        f"{type(info.name).__name__}={info.name!r}")
