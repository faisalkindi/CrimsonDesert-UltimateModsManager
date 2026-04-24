"""F3: post-apply verify must stop false-flagging mods imported
before the current game version, and successful applies must
auto-stamp enabled mods with the current fingerprint so the badge
self-heals.

Problem: user did Steam verify + Fix Everything + Apply on 1.04, and
got a dialog flagging 22 mods as "may crash the game: imported on
a different game version." All mods were imported on 1.03 but work
fine on 1.04. The check was speculation; the real crash-risk signal
is patch byte mismatches at apply time (already surfaced loudly by
json_patch_handler).

Fix:
1. Remove the ``issues.append((name, "Imported on a different game
   version…"))`` path from ``_post_apply_verify``. Integrity issues
   (PAPGT hash, PAMT hash, missing dirs) stay.
2. After successful apply, UPDATE every enabled mod's
   ``game_version_hash`` to the current fingerprint so the field
   tracks "last known-good version" instead of "import version".
"""
from __future__ import annotations

import re
from pathlib import Path


def _fluent_src() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "gui" / "fluent_window.py").read_text(
                encoding="utf-8")


# ── Post-apply verify no longer generates version-mismatch issues ─────

def test_post_apply_verify_does_not_append_version_mismatch():
    src = _fluent_src()
    anchor = src.find("def _post_apply_verify")
    assert anchor != -1
    next_def = src.find("\n    def ", anchor + 20)
    body = src[anchor:next_def if next_def != -1 else anchor + 8000]
    # The noisy append must be gone. Any remaining mention of
    # "different game version" must NOT be paired with issues.append.
    lines_with_msg = [
        ln for ln in body.splitlines()
        if "different game version" in ln.lower()
    ]
    for ln in lines_with_msg:
        assert "issues.append" not in ln, (
            "_post_apply_verify must not append version-hash "
            "mismatches to the 'may crash' issue list — that's a "
            "speculative signal, not a verification failure. Line "
            f"still present: {ln!r}")


def test_post_apply_verify_still_reports_papgt_issues():
    """Regression: PAPGT/PAMT integrity checks must stay in the
    dialog — those ARE real crash risks."""
    src = _fluent_src()
    anchor = src.find("def _post_apply_verify")
    assert anchor != -1
    next_def = src.find("\n    def ", anchor + 20)
    body = src[anchor:next_def if next_def != -1 else anchor + 8000]
    assert "PAPGT hash is invalid" in body, (
        "PAPGT integrity check must remain in post-apply verify"
    )
    assert "PAMT hash mismatch" in body, (
        "PAMT integrity check must remain in post-apply verify")


# ── Auto-stamp after successful apply ────────────────────────────────

def test_stamp_helper_exists():
    from cdumm.engine import version_detector as vd
    assert hasattr(vd, "stamp_enabled_mods_as_current"), (
        "need a public helper that UPDATEs every enabled mod's "
        "game_version_hash to the current detector output")


def test_stamp_helper_updates_only_enabled(tmp_path, monkeypatch):
    from cdumm.engine import version_detector as vd
    from cdumm.storage.database import Database

    gd = tmp_path / "Crimson Desert"
    (gd / "bin64").mkdir(parents=True)
    (gd / "bin64" / "CrimsonDesert.exe").write_bytes(b"X" * 200_000)
    monkeypatch.setattr(vd, "_get_steam_build_id", lambda _g: "new_build")
    vd._cached_version.cache_clear()

    db = Database(tmp_path / "t.db")
    db.initialize()
    db.connection.execute(
        "INSERT INTO mods (name, mod_type, enabled, priority, "
        "game_version_hash) VALUES ('A_on', 'paz', 1, 1, 'old_hash')")
    db.connection.execute(
        "INSERT INTO mods (name, mod_type, enabled, priority, "
        "game_version_hash) VALUES ('B_off', 'paz', 0, 2, 'old_hash')")
    db.connection.commit()

    new_fp = vd.detect_game_version(gd)
    vd.stamp_enabled_mods_as_current(db, gd)

    rows = dict(db.connection.execute(
        "SELECT name, game_version_hash FROM mods").fetchall())
    assert rows["A_on"] == new_fp
    assert rows["B_off"] == "old_hash", (
        "disabled mods must NOT be stamped — their hash should "
        "reflect the last version they were confirmed working on")
    db.close()


def test_stamp_helper_safe_when_detector_returns_none(tmp_path, monkeypatch):
    """If detect_game_version returns None (missing exe, etc.),
    the helper must not crash and must not wipe stored hashes."""
    from cdumm.engine import version_detector as vd
    from cdumm.storage.database import Database

    gd = tmp_path / "Crimson Desert"
    # No exe, no appmanifest → detector returns None
    gd.mkdir()
    monkeypatch.setattr(vd, "_get_steam_build_id", lambda _g: None)
    vd._cached_version.cache_clear()

    db = Database(tmp_path / "t.db")
    db.initialize()
    db.connection.execute(
        "INSERT INTO mods (name, mod_type, enabled, priority, "
        "game_version_hash) VALUES ('A', 'paz', 1, 1, 'prev_hash')")
    db.connection.commit()

    # Must not raise.
    vd.stamp_enabled_mods_as_current(db, gd)
    row = db.connection.execute(
        "SELECT game_version_hash FROM mods WHERE name='A'").fetchone()
    assert row[0] == "prev_hash", (
        "when detector can't produce a fingerprint, the helper must "
        "leave existing hashes untouched")
    db.close()


def test_on_apply_done_calls_stamp_after_success():
    """Successful apply (no errors) must invoke the stamp helper so
    the orange 'outdated' badge clears and post-apply verify stops
    treating these as a different game version."""
    src = _fluent_src()
    # Anchor: the _on_apply's on_apply_done closure.
    anchor = src.find("def on_apply_done(msgs):")
    assert anchor != -1
    # Scope: the whole closure (up to next def or dedented block).
    scope = src[anchor:anchor + 4000]
    assert "stamp_enabled_mods_as_current" in scope, (
        "on_apply_done must call stamp_enabled_mods_as_current after "
        "a successful apply so mods get tagged with the current "
        "fingerprint")
    # Must run only on the success path (no errors).
    errors_idx = scope.find("if errors:")
    stamp_idx = scope.find("stamp_enabled_mods_as_current")
    assert errors_idx != -1 and stamp_idx != -1
    assert stamp_idx > errors_idx, (
        "stamp call must come AFTER the errors-bailout branch — "
        "we only stamp when apply succeeded")
