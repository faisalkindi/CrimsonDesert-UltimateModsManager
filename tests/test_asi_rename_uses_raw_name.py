"""ASI inline-rename must operate on the RAW plugin name.

The card label shows ``prettify_mod_name(plugin.name)`` ("CDAnimCancel"
renders as something prettier), but the page-side rename handler looks
the plugin up by its raw on-disk name. The old code emitted the label
text as ``old_name``, so ``_find_plugin(old_name)`` never matched and
the rename silently failed to persist. These tests pin the contract:
the editor seeds with the raw name, and the ``renamed`` signal carries
the raw name as ``old_name``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pytestqt")


def _make_card(qtbot, tmp_path: Path, name: str = "CDAnimCancel"):
    from cdumm.asi.asi_manager import AsiPlugin
    from cdumm.gui.pages.asi_page import AsiCard

    plugin = AsiPlugin(
        name=name,
        path=tmp_path / f"{name}.asi",
        enabled=True,
        ini_path=None,
    )
    card = AsiCard(plugin, order=1)
    qtbot.addWidget(card)
    # _finish_rename guards on the editor's isVisible(), which is
    # always False while the top-level card is hidden. Show it so the
    # rename plumbing behaves as it does in the real window.
    card.show()
    qtbot.waitExposed(card)
    return card


def test_start_rename_seeds_editor_with_raw_name(qtbot, tmp_path):
    card = _make_card(qtbot, tmp_path, name="CDAnimCancel")
    # Sanity: the label is prettified, i.e. NOT the raw file stem.
    from cdumm.engine.import_handler import prettify_mod_name
    assert card._name_label.text() == prettify_mod_name("CDAnimCancel")

    card.start_rename()
    assert card._name_edit.text() == "CDAnimCancel"


def test_finish_rename_emits_raw_old_name(qtbot, tmp_path):
    card = _make_card(qtbot, tmp_path, name="CDAnimCancel")
    received: list[tuple[str, str]] = []
    card.renamed.connect(lambda old, new: received.append((old, new)))

    card.start_rename()
    card._name_edit.setText("MyRenamedMod")
    card._finish_rename()

    assert received == [("CDAnimCancel", "MyRenamedMod")]


def test_finish_rename_without_change_does_not_emit(qtbot, tmp_path):
    """Opening rename and committing the unchanged raw name must not
    fire the signal. With the old label-text comparison, the raw name
    always differed from the prettified label, so an untouched commit
    spuriously emitted a rename."""
    card = _make_card(qtbot, tmp_path, name="CDAnimCancel")
    received: list[tuple[str, str]] = []
    card.renamed.connect(lambda old, new: received.append((old, new)))

    card.start_rename()
    card._finish_rename()

    assert received == []


def test_second_rename_uses_updated_raw_name(qtbot, tmp_path):
    card = _make_card(qtbot, tmp_path, name="CDAnimCancel")
    received: list[tuple[str, str]] = []
    card.renamed.connect(lambda old, new: received.append((old, new)))

    card.start_rename()
    card._name_edit.setText("First")
    card._finish_rename()

    card.start_rename()
    assert card._name_edit.text() == "First"
    card._name_edit.setText("Second")
    card._finish_rename()

    assert received == [("CDAnimCancel", "First"), ("First", "Second")]
