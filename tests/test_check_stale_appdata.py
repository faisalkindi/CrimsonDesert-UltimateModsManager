"""HIGH 1 regression: ``_check_stale_appdata`` must not prompt to delete
the live state directory.

PR #64 review (faisalkindi, 2026-05-04): on a Windows install with
redirected ``LOCALAPPDATA``, or any future code path where the live
state directory routes through ``platform.app_data_dir()`` and
happens to resolve to the same path the legacy stale-detection
scanned, the prompt would offer to delete the active ``cdumm.db``.

The fix layers two guards on top of the existing ``IS_WINDOWS`` gate:
  1. ``appdata_dir == self._app_data_dir`` — the resolved scan path
     equals the live state dir.
  2. ``Path(self._db.db_path).parent == appdata_dir`` — defense in
     depth in case ``_app_data_dir`` was customized but the DB still
     lives in the scanned location.

Either short-circuit sets ``stale_appdata_checked`` so subsequent
launches skip the scan entirely.

These tests call ``_check_stale_appdata`` against a ``SimpleNamespace``
fake that carries only the attributes the method touches. We patch
``IS_WINDOWS`` to ``True`` so the guards are exercised on every CI
runner (otherwise the macOS / Linux ``IS_WINDOWS`` gate at the top of
the method short-circuits before reaching them).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Importing CdummWindow eagerly drags Qt + the entire engine in. Skip
# the whole file when pytest-qt isn't available (CI installs it as a
# dev dep; guards against a fresh checkout that hasn't run pip yet).
pytest_qt = pytest.importorskip("pytestqt")


def _seed_fake_legacy_appdata(root: Path) -> Path:
    """Create the ``deltas`` / ``vanilla`` / ``cdumm.db`` files the
    scanner looks for. Returns the seeded directory."""
    appdata = root / "legacy-appdata"
    appdata.mkdir()
    (appdata / "deltas").mkdir()
    (appdata / "vanilla").mkdir()
    (appdata / "cdumm.db").write_bytes(b"DO NOT DELETE")
    return appdata


def _make_fake_self(
        app_data_dir: Path,
        db_path: Path | None = None) -> SimpleNamespace:
    """Build a stand-in for the CdummWindow ``self`` carrying only
    the attributes ``_check_stale_appdata`` touches."""
    db = SimpleNamespace(
        db_path=str(db_path) if db_path else "/dev/null",
        connection=MagicMock(),
    ) if db_path else None
    return SimpleNamespace(
        _db=db,
        _app_data_dir=app_data_dir,
    )


def test_refuses_when_appdata_equals_live_state_dir(tmp_path):
    """Guard 1: resolved appdata_dir == self._app_data_dir → no prompt,
    no deletion, ``stale_appdata_checked`` set."""
    from cdumm.gui.fluent_window import CdummWindow

    legacy = _seed_fake_legacy_appdata(tmp_path)

    config_set_calls: list[tuple[str, str]] = []
    fake_config = MagicMock()
    fake_config.get.return_value = None
    fake_config.set.side_effect = (
        lambda k, v: config_set_calls.append((k, v)))

    fake_self = _make_fake_self(app_data_dir=legacy)

    with patch("cdumm.gui.fluent_window.IS_WINDOWS", True), \
            patch("cdumm.gui.fluent_window.MessageBox") as mock_box, \
            patch("cdumm.platform.app_data_dir", return_value=legacy), \
            patch("cdumm.storage.config.Config", return_value=fake_config):
        CdummWindow._check_stale_appdata(fake_self)

    mock_box.assert_not_called()
    assert ("stale_appdata_checked", "1") in config_set_calls
    # The seeded legacy data is still on disk.
    assert (legacy / "deltas").exists()
    assert (legacy / "vanilla").exists()
    assert (legacy / "cdumm.db").read_bytes() == b"DO NOT DELETE"


def test_refuses_when_db_lives_inside_resolved_dir(tmp_path):
    """Guard 2: ``Path(self._db.db_path).parent == appdata_dir`` →
    even if ``_app_data_dir`` was customized to something else, the
    live DB inside the legacy scan target shields it from deletion."""
    from cdumm.gui.fluent_window import CdummWindow

    legacy = _seed_fake_legacy_appdata(tmp_path)
    # Customized _app_data_dir somewhere else — guard 1 doesn't fire.
    customized = tmp_path / "customized-appdata"
    customized.mkdir()

    fake_config = MagicMock()
    fake_config.get.return_value = None
    set_calls: list[tuple[str, str]] = []
    fake_config.set.side_effect = lambda k, v: set_calls.append((k, v))

    # The live DB is INSIDE the legacy scan target.
    fake_self = _make_fake_self(
        app_data_dir=customized,
        db_path=legacy / "cdumm.db",
    )

    with patch("cdumm.gui.fluent_window.IS_WINDOWS", True), \
            patch("cdumm.gui.fluent_window.MessageBox") as mock_box, \
            patch("cdumm.platform.app_data_dir", return_value=legacy), \
            patch("cdumm.storage.config.Config", return_value=fake_config):
        CdummWindow._check_stale_appdata(fake_self)

    mock_box.assert_not_called()
    assert ("stale_appdata_checked", "1") in set_calls
    assert (legacy / "cdumm.db").read_bytes() == b"DO NOT DELETE"


def test_genuine_stale_data_still_prompts(tmp_path):
    """Sanity: when neither guard fires (legacy path is distinct from
    the live state dir AND from the live DB's parent), the prompt
    DOES fire on stale data. Confirms the guards aren't accidentally
    swallowing every legitimate use."""
    from cdumm.gui.fluent_window import CdummWindow

    legacy = _seed_fake_legacy_appdata(tmp_path)
    customized = tmp_path / "live-state"
    customized.mkdir()
    db_path = customized / "cdumm.db"
    db_path.write_bytes(b"")

    fake_config = MagicMock()
    fake_config.get.return_value = None
    fake_config.set = MagicMock()

    fake_self = _make_fake_self(
        app_data_dir=customized,
        db_path=db_path,
    )

    fake_box_instance = MagicMock()
    fake_box_instance.exec.return_value = False  # user clicked Cancel

    with patch("cdumm.gui.fluent_window.IS_WINDOWS", True), \
            patch("cdumm.gui.fluent_window.MessageBox",
                  return_value=fake_box_instance) as mock_box, \
            patch("cdumm.platform.app_data_dir", return_value=legacy), \
            patch("cdumm.storage.config.Config", return_value=fake_config):
        CdummWindow._check_stale_appdata(fake_self)

    # Both guards ruled out (legacy != customized, db parent == customized
    # not legacy), the scan reached the prompt construction.
    assert mock_box.called
    # User cancelled, so files survive — but the important assertion
    # is that the prompt fired.
    assert (legacy / "cdumm.db").exists()
