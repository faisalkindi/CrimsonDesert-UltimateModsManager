"""Interface-zoom scale-factor sidecar persistence.

The zoom factor is stored in a sidecar file (not the SQLite config) so
``main`` can read it before the QApplication is created — Qt only reads
QT_SCALE_FACTOR at that point.
"""
from __future__ import annotations


def test_write_then_read_roundtrip(tmp_path, monkeypatch):
    from cdumm.gui import ui_scale
    monkeypatch.setattr(ui_scale, "app_data_dir", lambda: tmp_path)

    ui_scale.write_ui_scale("1.5")

    assert (tmp_path / "ui_scale").read_text(encoding="utf-8") == "1.5"
    assert ui_scale.read_ui_scale() == "1.5"


def test_read_missing_defaults_to_1(tmp_path, monkeypatch):
    from cdumm.gui import ui_scale
    monkeypatch.setattr(ui_scale, "app_data_dir", lambda: tmp_path)

    assert ui_scale.read_ui_scale() == "1.0"


def test_out_of_range_value_falls_back(tmp_path, monkeypatch):
    from cdumm.gui import ui_scale
    monkeypatch.setattr(ui_scale, "app_data_dir", lambda: tmp_path)

    # A stray/hand-edited value must not scale the UI to something wild.
    (tmp_path / "ui_scale").write_text("9.9", encoding="utf-8")
    assert ui_scale.read_ui_scale() == "1.0"

    # And the writer clamps garbage to the safe default.
    ui_scale.write_ui_scale("banana")
    assert (tmp_path / "ui_scale").read_text(encoding="utf-8") == "1.0"


def test_allowed_scales_match_settings_combo():
    """The allowed factors must line up with the settings combo
    (100/110/125/150/175/200%) so the index<->factor mapping in
    settings_page stays correct."""
    from cdumm.gui.ui_scale import ALLOWED_SCALES
    assert ALLOWED_SCALES == ("1.0", "1.1", "1.25", "1.5", "1.75", "2.0")
