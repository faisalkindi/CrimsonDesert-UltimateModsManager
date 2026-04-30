"""For JSON-source mount-time mods (apply runs at apply-time from a
JSON file, no pre-computed deltas), the bug-report's
`vanilla_backup=` field was always rendering as `no` because the
backup check iterates `mod_deltas` rows and JSON mods have zero of
those by design. That made the field actively misleading: users
and triagers saw "vanilla_backup=no" and concluded the backup was
missing when nothing was actually wrong with the backup state.

Bug from Robhood19 / Faisal triage 2026-04-30: the misleading
display made it impossible to tell whether the backup was actually
absent or whether this was just the JSON-mod-at-mount-time normal
state.

Fix: for mods with `json_source` set AND zero deltas, render
`vanilla_backup=n/a (mount-time)` instead of `no`.
"""
from __future__ import annotations
from pathlib import Path


def test_resolve_vanilla_backup_display_paz_with_deltas_and_backup(tmp_path):
    from cdumm.gui.bug_report import _resolve_vanilla_backup_display

    vanilla_dir = tmp_path / "vanilla"
    vanilla_dir.mkdir()
    (vanilla_dir / "0008").mkdir()
    (vanilla_dir / "0008" / "0.paz").write_bytes(b"")
    out = _resolve_vanilla_backup_display(
        json_source=None, file_paths=["0008/0.paz"],
        vanilla_dir=vanilla_dir,
    )
    assert out == "yes"


def test_resolve_vanilla_backup_display_paz_with_deltas_no_backup(tmp_path):
    from cdumm.gui.bug_report import _resolve_vanilla_backup_display

    vanilla_dir = tmp_path / "vanilla"
    vanilla_dir.mkdir()
    out = _resolve_vanilla_backup_display(
        json_source=None, file_paths=["0008/0.paz"],
        vanilla_dir=vanilla_dir,
    )
    assert out == "no"


def test_resolve_vanilla_backup_display_json_mount_time(tmp_path):
    """JSON-source mod with zero deltas: render n/a, not no."""
    from cdumm.gui.bug_report import _resolve_vanilla_backup_display

    vanilla_dir = tmp_path / "vanilla"
    vanilla_dir.mkdir()
    out = _resolve_vanilla_backup_display(
        json_source="/path/to/mod.json",
        file_paths=[],  # JSON mount-time mods have no mod_deltas rows
        vanilla_dir=vanilla_dir,
    )
    assert out == "n/a (mount-time)", (
        f"JSON-source mods should render 'n/a (mount-time)', got: {out!r}")


def test_resolve_vanilla_backup_display_no_vanilla_dir():
    """When vanilla_dir is None or missing, fall through to no."""
    from cdumm.gui.bug_report import _resolve_vanilla_backup_display

    out = _resolve_vanilla_backup_display(
        json_source=None, file_paths=["x.paz"],
        vanilla_dir=None,
    )
    assert out == "no"
