"""Click-To-Update must bind to the row the user clicked, even when
heuristics would refuse.

Bug from Faisal 2026-04-27: clicking the red "Click To Update" pill on
mod card row 1424 (named "Horse X") triggered a fresh download of
nexus_mod_id=1126 file_id=6171. But the Nexus mod was renamed (or the
local row name was) so ``is_same_mod("Horse X", "Legendary Horse Body
Size Increase")`` returned False — `should_bind_to_existing_row` then
returned None and CDUMM created a NEW card instead of updating row 1424.

Same shape hit "Faster Interactions All (RAW)" (row 1412) where the
stored ``nexus_real_file_id`` was for a previous file — different from
the new file_id — so the file_id-mismatch branch refused to bind.

Both cases share the same root cause: when the user explicitly clicks
"Update" on a SPECIFIC card, that intent is unambiguous. The heuristic
that disambiguates "sibling mod on same Nexus page" vs "update of
existing mod" is only needed when the URL arrives WITHOUT explicit
intent (a fresh "Mod Manager Download" click on Nexus website).

Fix: ``should_bind_to_existing_row`` accepts an optional
``intended_mod_id`` parameter. When set (the click-to-update path
provides it), the helper bypasses all heuristics and returns
``intended_mod_id`` directly, after verifying the row exists.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from cdumm.engine.nxm_handler import should_bind_to_existing_row


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE mods ("
        "id INTEGER PRIMARY KEY, name TEXT, "
        "nexus_mod_id INTEGER, nexus_real_file_id INTEGER)")
    return conn


def test_explicit_intent_binds_despite_name_mismatch():
    """User clicks Update on local row "Horse X" — Nexus mod has been
    renamed to "Legendary Horse Body Size Increase". Without intent,
    is_same_mod returns False and the helper refuses to bind. With
    explicit intent, the user's click overrides the name heuristic.
    """
    conn = _make_conn()
    conn.execute(
        "INSERT INTO mods (id, name, nexus_mod_id, nexus_real_file_id) "
        "VALUES (1424, 'Horse X', 1126, 0)")
    conn.commit()

    decision_no_intent = should_bind_to_existing_row(
        conn, nexus_mod_id=1126, nexus_file_id=6171,
        downloaded_zip=None)
    assert decision_no_intent is None, (
        "Without explicit intent the helper rightly refuses (no zip "
        "to peek for name comparison). Confirms the precondition.")

    decision_with_intent = should_bind_to_existing_row(
        conn, nexus_mod_id=1126, nexus_file_id=6171,
        downloaded_zip=None, intended_mod_id=1424)
    assert decision_with_intent == 1424, (
        "Explicit intent must bypass heuristics and bind to the "
        "user's chosen row. Real bug: clicking 'Click To Update' on "
        "Horse X created a parallel 'Legendary Horse Body Size "
        "Increase' card instead of updating row 1424.")


def test_explicit_intent_binds_despite_file_id_mismatch():
    """Faster Interactions All scenario: row 1412 has stored
    nexus_real_file_id=4900, user clicks update for new file_id=6223.
    Without intent, the file_id-mismatch branch refuses (correctly,
    for the sibling-mod case). Explicit intent overrides.
    """
    conn = _make_conn()
    conn.execute(
        "INSERT INTO mods (id, name, nexus_mod_id, nexus_real_file_id) "
        "VALUES (1412, 'Faster Interactions All', 146, 4900)")
    conn.commit()

    no_intent = should_bind_to_existing_row(
        conn, nexus_mod_id=146, nexus_file_id=6223, downloaded_zip=None)
    assert no_intent is None, (
        "file_id mismatch + no intent → heuristic refuses (correctly, "
        "for the sibling-mod safety case)")

    with_intent = should_bind_to_existing_row(
        conn, nexus_mod_id=146, nexus_file_id=6223,
        downloaded_zip=None, intended_mod_id=1412)
    assert with_intent == 1412, (
        "Click-To-Update intent must override the file_id mismatch")


def test_explicit_intent_rejects_nonexistent_row():
    """Defensive: if intended_mod_id points at a row that no longer
    exists (deleted between click and download arrival), don't bind.
    Caller will then import as a new mod."""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO mods (id, name, nexus_mod_id, nexus_real_file_id) "
        "VALUES (1424, 'Horse X', 1126, 0)")
    conn.commit()

    decision = should_bind_to_existing_row(
        conn, nexus_mod_id=1126, nexus_file_id=6171,
        downloaded_zip=None, intended_mod_id=99999)  # not in table
    assert decision is None, (
        "intended_mod_id pointing at a deleted/missing row must NOT "
        "be honored — return None so caller imports as new")


def test_explicit_intent_zero_or_none_falls_through_to_heuristic():
    """When intended_mod_id is 0 or None (fresh nxm:// click from
    Nexus website with no local intent), the helper must use the
    existing heuristic — keeps the sibling-mod safety net intact."""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO mods (id, name, nexus_mod_id, nexus_real_file_id) "
        "VALUES (1426, 'Better Subtitles', 208, 5079)")
    conn.commit()

    # No intent: current sibling-mod behavior (heuristic refuses
    # because file_id 5080 != stored 5079)
    for sentinel in (None, 0):
        decision = should_bind_to_existing_row(
            conn, nexus_mod_id=208, nexus_file_id=5080,
            downloaded_zip=None, intended_mod_id=sentinel)
        assert decision is None, (
            f"intended_mod_id={sentinel!r} must NOT bypass the "
            "heuristic — sibling-mod safety net depends on this")
