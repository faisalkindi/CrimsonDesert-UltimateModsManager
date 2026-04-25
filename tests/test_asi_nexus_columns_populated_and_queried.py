"""Bugs 35 + 43: asi_plugin_state now has nexus_real_file_id and
nexus_last_checked_at columns (Bug 25/26), but no UPDATE writes to
them and no SELECT reads them.

- fluent_window.py:3754 writes only nexus_mod_id.
- fluent_window.py:981 reads only nexus_mod_id; hardcodes the other
  two to 0 at :983-984.
- settings_page.py has the same shape.
"""
from __future__ import annotations

import re
from pathlib import Path


def _read(rel: str) -> str:
    return (Path(__file__).resolve().parents[1]
            / rel).read_text(encoding="utf-8")


def test_asi_post_import_writes_nexus_real_file_id():
    """The ASI metadata write block must update nexus_real_file_id
    when the nxm:// flow provided it."""
    src = _read("src/cdumm/gui/fluent_window.py")
    # Look for the asi_plugin_state write block (anchored on the
    # existing version/nexus_mod_id pair).
    anchor = src.find("INSERT OR IGNORE INTO asi_plugin_state")
    assert anchor != -1, "ASI metadata block not found"
    window = src[anchor:anchor + 2500]
    assert re.search(
        r"UPDATE\s+asi_plugin_state\s+SET\s+nexus_real_file_id",
        window, re.IGNORECASE,
    ), ("ASI post-import metadata block must write "
        "nexus_real_file_id when available")


def test_asi_query_reads_nexus_real_file_id_from_db():
    """The update-check SELECT must read the two new columns instead
    of hardcoding 0."""
    src = _read("src/cdumm/gui/fluent_window.py")
    # Locate the asi_plugin_state SELECT used by the update check.
    anchor = src.find('FROM asi_plugin_state')
    assert anchor != -1
    # Look back to the SELECT start.
    start = src.rfind('"SELECT', 0, anchor)
    assert start != -1
    sel = src[start:anchor + 50]
    assert "nexus_real_file_id" in sel, (
        f"ASI update-check SELECT must read nexus_real_file_id; "
        f"got: {sel!r}")
    assert "nexus_last_checked_at" in sel, (
        f"ASI update-check SELECT must read nexus_last_checked_at; "
        f"got: {sel!r}")


def test_settings_page_asi_query_reads_real_file_id():
    """Manual 'Check for Mod Updates' does the same DB read; must
    also pick up the new columns."""
    src = _read("src/cdumm/gui/pages/settings_page.py")
    anchor = src.find('FROM asi_plugin_state')
    assert anchor != -1
    start = src.rfind('"SELECT', 0, anchor)
    assert start != -1
    sel = src[start:anchor + 50]
    assert "nexus_real_file_id" in sel
    assert "nexus_last_checked_at" in sel
