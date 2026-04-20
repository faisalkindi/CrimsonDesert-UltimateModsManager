"""D-Weakref coverage: _CollapsibleSection must be held strongly by
the ConfigPanel so PySide6's weak bound-method slot doesn't get GC'd.

PySide6 signal connections hold a WEAK reference to bound-method
slots (Qt forum thread 154590). Without a strong reference on the
panel, clicking the header arrow becomes a silent no-op on Windows.
"""
from __future__ import annotations

import inspect


def test_config_panel_init_exposes_collapsible_sections():
    """The attribute must exist after show_variant_mod runs. Directly
    importing the class without booting Qt gives us enough to smoke-
    check that the field is populated in the right place."""
    from cdumm.gui.components import config_panel
    src = inspect.getsource(config_panel.ConfigPanel.show_variant_mod)
    assert "self._collapsible_sections" in src, (
        "show_variant_mod must assign _collapsible_sections so the list "
        "keeps the section objects alive for the life of the panel")
    # And the init resets to empty on every call, preventing dangling
    # references after switching from a mutex-pack mod to a plain mod.
    assert "self._collapsible_sections: list[_CollapsibleSection] = []" in src, (
        "show_variant_mod must reset _collapsible_sections to [] so "
        "switching mods doesn't leak dangling widget refs")
    assert "self._collapsible_sections.append" in src, (
        "sections must be appended to the strong-ref list")


def test_collapsible_section_is_not_a_qwidget_subclass():
    """The section is a plain Python class with a QPushButton header
    and a QWidget body — NOT a QWidget subclass. The strong-ref rule
    only matters because of this design choice; a QWidget subclass
    would have its layout-parent keep it alive for free."""
    from cdumm.gui.components.config_panel import _CollapsibleSection
    # Would inherit from QObject if it were a Qt widget.
    mro_names = [cls.__name__ for cls in _CollapsibleSection.__mro__]
    assert "QWidget" not in mro_names, (
        "_CollapsibleSection is NOT a QWidget — confirms the "
        "strong-ref requirement documented in config_panel.py")
    assert "object" in mro_names
