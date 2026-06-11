"""mod_diagnostics: stale meta/ block removal + lightweight DB read.

1. ``_diagnose_paz_dirs`` carried a copy-pasted NOTE inside the
   ``if has_meta:`` block that read ``paz_in_dir``/``pamt_in_dir``
   left over from the previous loop iteration: wrong-directory NOTE
   when the loop ran, latent NameError when ``numbered_dirs`` was
   empty (meta-only archive).
2. ``_check_game_version`` built a full ``Database`` +
   ``initialize()`` (schema + migrations, creating the DB file if
   absent) just to read two values; it now uses a plain sqlite3
   SELECT and never creates the file.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from cdumm.engine.mod_diagnostics import (
    _check_game_version,
    _diagnose_paz_dirs,
)


def test_meta_only_archive_does_not_raise(tmp_path: Path) -> None:
    """numbered_dirs empty + meta present used to NameError on the
    stale paz_in_dir reference."""
    sections: list[str] = []
    _diagnose_paz_dirs(set(), ["meta/0.papgt"], tmp_path, sections)
    text = "\n".join(sections)
    assert "meta/" in text
    # No stale NOTE may appear: there is no numbered dir to report on.
    assert "No .paz/.pamt files" not in text


def test_meta_block_does_not_duplicate_loop_note(tmp_path: Path) -> None:
    """The per-directory loop emits the 'No .paz/.pamt' NOTE where it
    belongs; the meta/ block must not re-emit it with stale data."""
    sections: list[str] = []
    names = ["0040/readme.txt", "meta/0.papgt"]
    _diagnose_paz_dirs({"0040"}, names, tmp_path, sections)
    text = "\n".join(sections)
    assert text.count("No .paz/.pamt files") == 1, (
        "stale copy-pasted NOTE in the meta/ block fired with "
        "variables from the previous loop iteration")


def test_check_game_version_missing_db_not_created(tmp_path: Path) -> None:
    db_path = tmp_path / "cdumm.db"
    sections: list[str] = []
    _check_game_version(tmp_path, db_path, sections)
    text = "\n".join(sections)
    assert "Could not check game version" in text
    assert not db_path.exists(), (
        "diagnostics must not create a database file as a side effect")


def test_check_game_version_reads_existing_db(tmp_path: Path) -> None:
    db_path = tmp_path / "cdumm.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE snapshots (id INTEGER PRIMARY KEY, "
            "file_path TEXT)")
        conn.execute(
            "CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO snapshots (file_path) VALUES ('a')")
        conn.execute("INSERT INTO snapshots (file_path) VALUES ('b')")
        conn.commit()
    finally:
        conn.close()

    sections: list[str] = []
    _check_game_version(tmp_path, db_path, sections)
    text = "\n".join(sections)
    assert "Vanilla snapshot: 2 files indexed" in text
