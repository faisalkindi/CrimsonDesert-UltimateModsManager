"""Round 9 fixes."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

from cdumm.engine.nexus_api import (
    get_mod_files, persist_backfill_file_ids,
)


def test_get_mod_files_skips_non_dict_entries() -> None:
    """One non-dict entry in raw_files must NOT crash the whole
    response. Current code does `f.get(...)` which raises
    AttributeError on None — caught at the outer except and the
    whole mod check returns None. Same fragility as round 8's
    feed bug."""
    api_response = {
        "files": [
            {"file_id": 1, "name": "A", "version": "1",
             "uploaded_timestamp": 100, "file_name": "a.zip",
             "category_id": 1},
            None,  # bad entry
            "not a dict",  # also bad
            {"file_id": 2, "name": "B", "version": "2",
             "uploaded_timestamp": 200, "file_name": "b.zip",
             "category_id": 1},
        ],
        "file_updates": [],
    }
    with patch("cdumm.engine.nexus_api._api_request",
               return_value=api_response):
        result = get_mod_files(100, "key")
    assert result is not None, (
        "non-dict entries in raw_files must be skipped, not "
        "crash the whole response")
    files, _ = result
    assert len(files) == 2, (
        f"expected 2 valid files (skipping the bad ones); "
        f"got {len(files)}: {[f.file_id for f in files]}")
    assert {f.file_id for f in files} == {1, 2}


def test_get_mod_files_skips_non_dict_in_file_updates() -> None:
    """Same defense for the file_updates section."""
    api_response = {
        "files": [
            {"file_id": 1, "name": "A", "version": "1",
             "uploaded_timestamp": 100, "file_name": "a.zip",
             "category_id": 1},
        ],
        "file_updates": [
            {"old_file_id": 1, "new_file_id": 2,
             "uploaded_timestamp": 100},
            None,
            "junk",
        ],
    }
    with patch("cdumm.engine.nexus_api._api_request",
               return_value=api_response):
        result = get_mod_files(100, "key")
    assert result is not None
    _, updates = result
    assert len(updates) == 1


def test_persist_file_id_one_is_persisted(tmp_path: Path) -> None:
    """Lock the round-7 boundary: file_id=1 (smallest valid) must
    NOT be skipped by the file_id<=0 guard."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE mods (id INTEGER PRIMARY KEY, "
        "nexus_real_file_id INTEGER)")
    conn.execute("INSERT INTO mods (id, nexus_real_file_id) "
                 "VALUES (1, 0)")
    conn.commit()

    persisted = persist_backfill_file_ids(conn, {1: 1})
    assert persisted == 1, (
        f"file_id=1 must persist (smallest valid value, no "
        f"off-by-one in the > 0 guard). Got persisted={persisted}")
    cur = conn.execute(
        "SELECT nexus_real_file_id FROM mods WHERE id=1")
    assert cur.fetchone()[0] == 1
