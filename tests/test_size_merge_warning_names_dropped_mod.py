"""When two mods both target the same overlay entry and at least one
changes its size (insert), the apply engine falls back to the
priority-winning entry and emits a yellow warning naming which mods
got dropped. v3.2.6 added the per-mod-name to that warning, but the
overlay-routing path at apply_engine._apply_deltas_for_file dropped
`mod_name` from the metadata before adding the entry to
_overlay_entries. Result: the warning showed `Dropped: 'mod #0'`
instead of the actual mod name.

DerBambusbjoern Nexus comment 2026-04-30 (image:
https://i.imgur.com/liTYZTd.png) caught the regression.

Fix: propagate `mod_name` from the delta dict into the metadata at
the append site.
"""
from __future__ import annotations
import pytest


def test_merge_warning_uses_mod_names_from_metadata():
    """`_merge_same_target_overlay_entries` must name actual mods in
    the size-merge fallback warning, not the placeholder `mod #N`."""
    from cdumm.engine.apply_engine import ApplyWorker

    # Build a real ApplyEngine without going through __init__ (we only
    # need the merge method, not the rest of the pipeline). We DO need
    # the warning signal and _soft_warnings buffer to capture output.
    engine = ApplyWorker.__new__(ApplyWorker)
    engine._soft_warnings = []

    captured: list[str] = []

    class _SignalStub:
        def emit(self, msg):
            captured.append(msg)
    engine.warning = _SignalStub()

    # Stub out vanilla content so the size-merge branch fires.
    def _fake_vanilla(file_path, entry_path):
        return b"X" * 100  # 100 bytes vanilla
    engine._get_vanilla_entry_content = _fake_vanilla

    # Two entries hitting the same (pamt_dir, entry_path) with
    # DIFFERENT sizes (one matches vanilla, one doesn't — triggers
    # size-merge fallback to last-wins).
    entries = [
        (b"X" * 100, {  # mod A: same size as vanilla (would merge)
            "pamt_dir": "0008",
            "entry_path": "gamedata/dropsetinfo.pabgb",
            "mod_name": "DerBambusbjoern's Loot Mod",
            "priority": 5,
        }),
        (b"Y" * 200, {  # mod B: insert (changes size)
            "pamt_dir": "0008",
            "entry_path": "gamedata/dropsetinfo.pabgb",
            "mod_name": "aggregated JSON",
            "priority": 1,
        }),
    ]

    engine._merge_same_target_overlay_entries(entries)

    assert captured, "Expected a size-merge warning to fire"
    msg = captured[0]
    # The warning must name BOTH the active and dropped mod by their
    # real names. v3.2.6's bug regression would produce "mod #0".
    assert "mod #0" not in msg, (
        f"Warning still uses placeholder 'mod #0' instead of mod_name. "
        f"Full message: {msg!r}"
    )
    assert "mod #1" not in msg
    assert "DerBambusbjoern's Loot Mod" in msg, (
        f"Dropped mod name missing from warning: {msg!r}"
    )
    assert "aggregated JSON" in msg, (
        f"Active mod name missing from warning: {msg!r}"
    )


def test_overlay_routing_propagates_mod_name(tmp_path):
    """The overlay-routing branch in _apply_deltas_for_file must
    copy `mod_name` from the delta dict into the metadata so the
    later size-merge fallback can name the dropped mod."""
    from cdumm.engine.apply_engine import ApplyWorker
    from cdumm.engine.delta_engine import save_entry_delta

    engine = ApplyWorker.__new__(ApplyWorker)
    engine._overlay_entries = []
    engine._vanilla_dir = tmp_path / "vanilla"
    engine._vanilla_dir.mkdir()

    # Write a fake ENTR delta so the load_entry_delta path is exercised.
    delta_path = tmp_path / "fake.entr"
    save_entry_delta(
        b"some-mod-bytes",
        {"pamt_dir": "0008", "entry_path": "gamedata/dropsetinfo.pabgb"},
        delta_path,
    )

    deltas = [
        {
            "mod_id": 42,
            "mod_name": "DerBambusbjoern's Loot Mod",
            "priority": 5,
            "entry_path": "gamedata/dropsetinfo.pabgb",
            "delta_path": str(delta_path),
        },
    ]

    # Stub the dependencies _compose_file pulls in
    engine._merge_json_patch_deltas = lambda fp, ds: ([], ds)
    engine._try_semantic_merge = lambda fp, eds: eds
    engine._compose_file("0008/0.paz", deltas)

    assert engine._overlay_entries, "Routing dropped the entry"
    _content, metadata = engine._overlay_entries[0]
    assert metadata.get("mod_name") == "DerBambusbjoern's Loot Mod", (
        f"mod_name lost during overlay routing. Metadata: {metadata!r}"
    )
