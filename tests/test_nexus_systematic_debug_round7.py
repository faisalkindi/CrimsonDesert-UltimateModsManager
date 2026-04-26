"""Round 7 systematic-debugging fixes."""
from __future__ import annotations

import sqlite3
import logging
from pathlib import Path
from unittest.mock import patch

from cdumm.engine.nexus_api import (
    get_download_link, persist_backfill_file_ids,
)


def test_get_download_link_logs_clearly_on_empty_list_response(
        caplog) -> None:
    """When Nexus returns [] (valid response shape, no URI),
    don't log 'unexpected response shape' — that's misleading.
    Branch the log so 'empty' is distinct from 'unknown shape'."""
    caplog.set_level(logging.WARNING, logger="cdumm.engine.nexus_api")
    with patch("cdumm.engine.nexus_api._api_request", return_value=[]):
        result = get_download_link(100, 200, "key")
    assert result is None
    msgs = [r.getMessage() for r in caplog.records]
    unexpected_logs = [m for m in msgs if "unexpected" in m.lower()]
    assert not unexpected_logs, (
        f"empty list response should NOT log 'unexpected shape' — "
        f"list IS the expected shape, just empty. Got: {msgs}")


def test_persist_skips_zero_or_negative_file_id(tmp_path: Path) -> None:
    """File_id <= 0 is invalid — we shouldn't write it to the DB.
    Defensive: silently skip with a debug log. Real backfills from
    check_mod_updates always pass positive file_ids, but the helper
    is a public API and should validate input."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE mods (id INTEGER PRIMARY KEY, "
        "nexus_real_file_id INTEGER)")
    conn.execute("INSERT INTO mods (id, nexus_real_file_id) "
                 "VALUES (1, 100), (2, 200), (3, 300)")
    conn.commit()

    persisted = persist_backfill_file_ids(
        conn, {1: 0, 2: -5, 3: 9001})
    # Only the row with positive file_id should be updated
    assert persisted == 1
    cur = conn.execute(
        "SELECT id, nexus_real_file_id FROM mods ORDER BY id")
    rows = cur.fetchall()
    assert rows == [(1, 100), (2, 200), (3, 9001)], (
        f"only positive file_ids should persist; got {rows}")
