"""scottykyzer Nexus 2026-05-09 (CDUMM v3.2.13): the "Apply Completed
with Warnings" banner reads as

    Vanilla backup missing for cross-layer base: 'Better Unique Gears'
    (priority=2) provides gamedata/iteminfo.pabgb -- stacking JSON
    patches on top, using hash-verified live copy and creating the
    backup now. Subsequent applies will use the backup directly.

That's two distinct concepts mashed into one sentence:
  1. cross-layer base substitution (a different mod's PAZ is used as
     the base for JSON stacking) -- the SUCCESS case.
  2. lazy live-PAZ backup creation -- the live-as-vanilla self-heal.

The grammar is broken because the warn_callback contract is:
"caller passes a paz_rel fragment, the worker's _warn wraps it with
'Vanilla backup missing for {paz_rel}, using hash-verified live ...'".
The cross-layer call site at apply_engine.py:869-873 violates that
contract by passing a complete sentence. _warn then prepends "Vanilla
backup missing for" to the cross-layer sentence, producing the broken
message.

Fix: change the warn_callback contract so resolve_vanilla_source
builds the complete message itself. Each call site (cross-layer vs
self-heal) emits its own clean text.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


def test_cross_layer_override_passes_clean_message(tmp_path):
    from cdumm.engine.apply_engine import resolve_vanilla_source

    vanilla_dir = tmp_path / "vanilla"
    game_dir = tmp_path / "game"
    vanilla_dir.mkdir()
    game_dir.mkdir()

    override_entry = MagicMock()
    override_entry.paz_file = str(tmp_path / "deltas" / "betterunique.paz")
    override_entry.path = "gamedata/iteminfo.pabgb"
    paz_dir_overrides = {
        "gamedata/iteminfo.pabgb": {
            "mod_id": 99,
            "mod_name": "Better Unique Gears",
            "priority": 2,
            "pamt_dir": "0008",
            "paz_delta_path": str(tmp_path / "deltas" / "betterunique.paz"),
            "pamt_delta_path": str(tmp_path / "deltas" / "betterunique.pamt"),
            "stage_root": str(tmp_path),
            "entry": override_entry,
        }
    }

    snapshot_mgr = MagicMock()
    warns: list[str] = []
    result = resolve_vanilla_source(
        "gamedata/iteminfo.pabgb",
        vanilla_dir,
        game_dir,
        snapshot_mgr,
        warn_callback=warns.append,
        paz_dir_overrides=paz_dir_overrides,
    )

    assert result is override_entry, (
        "cross-layer override must return the override mod's entry "
        "as the base for JSON stacking")
    assert warns, (
        "cross-layer override must surface a warning so the user "
        "knows the base is a mod, not vanilla")

    msg = warns[0]
    # Cross-layer is not a missing-backup case. The phrase "Vanilla
    # backup missing" must not appear -- in the past it was prepended
    # by the worker's _warn wrapper which assumed every callback
    # invocation was a self-heal event.
    assert "vanilla backup missing" not in msg.lower(), (
        f"cross-layer warning leaked the self-heal phrase; got:\n  "
        f"{msg}")
    # The message should plainly explain that another mod is providing
    # the base for JSON stacking, naming the mod and the file.
    assert "Better Unique Gears" in msg
    assert "gamedata/iteminfo.pabgb" in msg


def test_self_heal_passes_complete_backup_missing_message(
    monkeypatch, tmp_path,
):
    """The self-heal path (vanilla absent, live PAZ hash-verified)
    must pass the FULL "Vanilla backup missing for X, ..." sentence to
    warn_callback, not just the paz_rel fragment. Today the worker's
    _warn wrapper builds the sentence; this test enforces that
    resolve_vanilla_source itself builds it instead, so the wrapper
    can stop second-guessing the message."""
    import hashlib
    from cdumm.engine.apply_engine import resolve_vanilla_source

    vanilla_dir = tmp_path / "vanilla"
    game_dir = tmp_path / "game"
    vanilla_dir.mkdir()
    (game_dir / "0008").mkdir(parents=True)

    live_paz = game_dir / "0008" / "0.paz"
    live_paz.write_bytes(b"PRISTINE_PAZ_BYTES")
    live_pamt = game_dir / "0008" / "0.pamt"
    live_pamt.write_bytes(b"PRISTINE_PAMT_BYTES")

    expected_hash = hashlib.sha256(b"PRISTINE_PAZ_BYTES").hexdigest()

    fake_entry = MagicMock()
    fake_entry.paz_file = str(live_paz)
    fake_entry.path = "gamedata/iteminfo.pabgb"

    snapshot_mgr = MagicMock()
    snapshot_mgr.get_file_hash.return_value = expected_hash

    def _fake_hash_file(p):
        return hashlib.sha256(Path(p).read_bytes()).hexdigest(), 0

    monkeypatch.setattr(
        "cdumm.engine.snapshot_manager.hash_file", _fake_hash_file)
    monkeypatch.setattr(
        "cdumm.engine.json_patch_handler._find_pamt_entry",
        lambda target, base_dir:
            fake_entry if base_dir == game_dir else None)

    warns: list[str] = []
    resolve_vanilla_source(
        "gamedata/iteminfo.pabgb", vanilla_dir, game_dir,
        snapshot_mgr, warn_callback=warns.append,
        paz_dir_overrides=None)

    assert warns, "self-heal must still warn"
    msg = warns[0]
    assert "vanilla backup missing" in msg.lower(), (
        f"self-heal warning must say 'Vanilla backup missing for X, "
        f"using hash-verified live copy ...' verbatim; got:\n  {msg}")
    assert "0008/0.paz" in msg, (
        f"self-heal warning must name the affected paz; got:\n  {msg}")
