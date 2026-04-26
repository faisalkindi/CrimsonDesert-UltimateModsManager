"""NXM download must not bind to a sibling mod sharing nexus_mod_id.

Bug from Faisal 2026-04-26: Nexus page 208 hosts TWO distinct mods,
'Better Subtitles' and 'No Letterbox'. User had Better Subtitles
imported (mod_id=1426, nexus_mod_id=208). Clicked 'Mod Manager
Download' for No Letterbox. CDUMM saw nexus_mod_id=208 already
existed → bound the new download to row 1426 → REPLACED Better
Subtitles content with No Letterbox while keeping the old name.

The bind logic at fluent_window.py:2311 used nexus_mod_id alone as
identity. Page IDs aren't unique mod identities; a Nexus page can
host multiple distinct mods. Need stricter check.

This test exercises the bind decision via a focused helper so we
don't need the GUI loaded.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

# Helper under test (we'll add it to nexus_api or a new module).
from cdumm.engine.nxm_handler import should_bind_to_existing_row


def _make_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE mods ("
        "id INTEGER PRIMARY KEY, name TEXT, "
        "nexus_mod_id INTEGER, nexus_real_file_id INTEGER)")
    return conn


def test_dont_bind_when_existing_file_id_differs(
        tmp_path: Path) -> None:
    """User has row with nexus_real_file_id=5079 (old Better
    Subtitles file). New download is file_id=5080 (No Letterbox
    on the same page). Different files → don't bind."""
    conn = _make_conn(tmp_path)
    conn.execute(
        "INSERT INTO mods (id, name, nexus_mod_id, nexus_real_file_id) "
        "VALUES (1426, 'Better Subtitles', 208, 5079)")
    conn.commit()

    decision = should_bind_to_existing_row(
        conn, nexus_mod_id=208, nexus_file_id=5080,
        downloaded_zip=None)

    assert decision is None, (
        "different stored file_id means user has a different file "
        "from the same Nexus page — don't bind, import as new. "
        f"Got existing_id={decision}.")


def test_bind_when_existing_file_id_matches(
        tmp_path: Path) -> None:
    """Same file_id stored as the new download — same mod, same
    file (probably a redundant click). Bind to deduplicate."""
    conn = _make_conn(tmp_path)
    conn.execute(
        "INSERT INTO mods (id, name, nexus_mod_id, nexus_real_file_id) "
        "VALUES (1426, 'Better Subtitles', 208, 5080)")
    conn.commit()

    decision = should_bind_to_existing_row(
        conn, nexus_mod_id=208, nexus_file_id=5080,
        downloaded_zip=None)

    assert decision == 1426


def test_bind_legacy_null_file_id_when_name_matches(
        tmp_path: Path) -> None:
    """User imported as local zip (no nexus_real_file_id stored).
    Clicks Mod Manager Download for the same mod's update. Names
    in the downloaded zip should match → bind for legitimate
    update."""
    conn = _make_conn(tmp_path)
    conn.execute(
        "INSERT INTO mods (id, name, nexus_mod_id, nexus_real_file_id) "
        "VALUES (1426, 'Better Subtitles', 208, NULL)")
    conn.commit()

    # Build a fake zip that looks like Better Subtitles content
    import zipfile
    zip_path = tmp_path / "Better Subtitles update.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Better Subtitles/modinfo.json",
                    '{"name": "Better Subtitles"}')
        zf.writestr("Better Subtitles/0012/4.paz", b"data")

    decision = should_bind_to_existing_row(
        conn, nexus_mod_id=208, nexus_file_id=5080,
        downloaded_zip=zip_path)

    assert decision == 1426, (
        "legacy null file_id + matching mod name → should bind "
        "for legitimate update flow. Got: " + repr(decision))


def test_dont_bind_legacy_null_file_id_when_name_differs(
        tmp_path: Path) -> None:
    """User has 'Better Subtitles' (legacy, no file_id). Downloads
    'No Letterbox' from same Nexus page. Names don't match → don't
    bind, import as new mod. THIS IS THE FAISAL BUG."""
    conn = _make_conn(tmp_path)
    conn.execute(
        "INSERT INTO mods (id, name, nexus_mod_id, nexus_real_file_id) "
        "VALUES (1426, 'Better Subtitles', 208, NULL)")
    conn.commit()

    import zipfile
    zip_path = tmp_path / "No Letterbox.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("No Letterbox/modinfo.json",
                    '{"name": "No Letterbox"}')

    decision = should_bind_to_existing_row(
        conn, nexus_mod_id=208, nexus_file_id=5080,
        downloaded_zip=zip_path)

    assert decision is None, (
        "different mod from same Nexus page — must NOT bind. "
        "This is the Faisal 2026-04-26 bug where Better Subtitles "
        "got replaced by No Letterbox content. "
        f"Got: {decision}")


def test_bind_when_no_existing_row(tmp_path: Path) -> None:
    """No mod with this nexus_mod_id exists yet. Decision is
    None → caller imports as new. (The function returns None for
    both 'no existing row' and 'don't bind' — caller treats both
    the same way.)"""
    conn = _make_conn(tmp_path)
    decision = should_bind_to_existing_row(
        conn, nexus_mod_id=999, nexus_file_id=5080,
        downloaded_zip=None)
    assert decision is None
