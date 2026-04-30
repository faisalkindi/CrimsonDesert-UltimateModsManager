"""Updating a disabled ASI plugin must not leave a stale
`<stem>.asi.disabled` file alongside the freshly installed
`<stem>.asi`.

Bug from Faisal 2026-04-30: dropping an ASI mod as an "update" to
an existing disabled entry creates a double entry in the ASI list.

Root cause: `AsiManager.install()` writes the new `<stem>.asi` but
leaves the old `<stem>.asi.disabled` in place. `scan()` reports
both files as separate plugins (one enabled, one disabled), so the
UI shows two entries for the same name.

Fix: when installing, remove any existing `<stem>.asi.disabled`
file in `bin64/` so only one record of the plugin survives. The
new install replaces it as ENABLED (the user dropped a fresh build,
so re-enabling on update matches their intent).
"""
from __future__ import annotations
from pathlib import Path

import pytest


def test_install_over_disabled_plugin_does_not_create_duplicate(tmp_path):
    """Setup: bin64/Foo.asi.disabled exists (old disabled install).
    Drop a new Foo.asi source. After install, scan() must return
    ONE plugin named 'Foo', not two."""
    from cdumm.asi.asi_manager import AsiManager

    bin64 = tmp_path / "bin64"
    bin64.mkdir()
    # Existing disabled plugin on disk
    (bin64 / "Foo.asi.disabled").write_bytes(b"\x4D\x5A old")

    # Source dir with the new .asi version
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "Foo.asi").write_bytes(b"\x4D\x5A new build")

    asi_mgr = AsiManager(bin64)
    asi_mgr.install(src_dir / "Foo.asi")

    plugins = asi_mgr.scan()
    names = [p.name for p in plugins]
    assert names.count("Foo") == 1, (
        f"Expected ONE 'Foo' plugin after update, got {len(names)}: "
        f"{[(p.name, p.enabled, p.path.name) for p in plugins]}")


def test_install_directory_with_disabled_companion_no_duplicate(tmp_path):
    """Same bug, but install from a directory (the more common path
    for variant + mixed-zip imports)."""
    from cdumm.asi.asi_manager import AsiManager

    bin64 = tmp_path / "bin64"
    bin64.mkdir()
    (bin64 / "Foo.asi.disabled").write_bytes(b"\x4D\x5A old")

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "Foo.asi").write_bytes(b"\x4D\x5A new")

    asi_mgr = AsiManager(bin64)
    asi_mgr.install(src_dir)

    plugins = asi_mgr.scan()
    names = [p.name for p in plugins]
    assert names.count("Foo") == 1, (
        f"Expected ONE 'Foo' plugin after directory install, got: "
        f"{[(p.name, p.enabled, p.path.name) for p in plugins]}")


def test_install_with_no_existing_disabled_unchanged(tmp_path):
    """Regression guard: when there's no existing .asi.disabled,
    install behaves as before (single enabled .asi on disk)."""
    from cdumm.asi.asi_manager import AsiManager

    bin64 = tmp_path / "bin64"
    bin64.mkdir()
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "Bar.asi").write_bytes(b"\x4D\x5A")

    asi_mgr = AsiManager(bin64)
    asi_mgr.install(src_dir / "Bar.asi")

    plugins = asi_mgr.scan()
    assert len(plugins) == 1
    assert plugins[0].name == "Bar"
    assert plugins[0].enabled is True


def test_disabled_install_writes_correct_version_file(tmp_path):
    """When the install lands as `.asi.disabled` (preserving user's
    disabled state), the helper that writes a `<stem>.version`
    sidecar must still fire. Previously the helper checked
    `endswith('.asi')` which is False for `.asi.disabled` filenames,
    so the version sidecar was silently skipped."""
    from cdumm.asi.asi_manager import _resolve_version_filename

    # The pure name resolver: given an installed file name (which
    # may have either suffix), produce the corresponding `.version`
    # sidecar filename.
    assert _resolve_version_filename("Foo.asi") == "Foo.version"
    assert _resolve_version_filename("Foo.asi.disabled") == "Foo.version"
    # Non-asi files get None (callers skip).
    assert _resolve_version_filename("Foo.ini") is None
    assert _resolve_version_filename("winmm.dll") is None


def test_disabled_install_resolves_plugin_name_for_state_db(tmp_path):
    """The asi_plugin_state row uses the bare stem as PRIMARY KEY.
    The helper that extracts the stem from an installed file name
    must handle BOTH `Foo.asi` AND `Foo.asi.disabled`."""
    from cdumm.asi.asi_manager import _stem_from_installed

    assert _stem_from_installed("Foo.asi") == "Foo"
    assert _stem_from_installed("Foo.asi.disabled") == "Foo"
    assert _stem_from_installed("Foo.ini") is None
    assert _stem_from_installed("winmm.dll") is None


def test_install_over_disabled_plugin_preserves_disabled_state(tmp_path):
    """When the user disabled a plugin then drops a new version,
    the new install should respect the user's disabled state. The
    new .asi binary lands AS DISABLED (`.asi.disabled` extension)
    so the user's intent (this plugin is currently off) survives
    the update."""
    from cdumm.asi.asi_manager import AsiManager

    bin64 = tmp_path / "bin64"
    bin64.mkdir()
    (bin64 / "Foo.asi.disabled").write_bytes(b"\x4D\x5A old")

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "Foo.asi").write_bytes(b"\x4D\x5A new build")

    asi_mgr = AsiManager(bin64)
    asi_mgr.install(src_dir / "Foo.asi")

    plugins = asi_mgr.scan()
    foos = [p for p in plugins if p.name == "Foo"]
    assert len(foos) == 1
    assert foos[0].enabled is False, (
        "Update over a disabled plugin should preserve disabled "
        "state. Got enabled plugin instead.")
    # Verify the on-disk file is the new content (update did happen)
    assert foos[0].path.read_bytes() == b"\x4D\x5A new build"
