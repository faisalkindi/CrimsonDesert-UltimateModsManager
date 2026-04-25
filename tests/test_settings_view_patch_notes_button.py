"""Settings page must offer a way to re-open the patch notes any
time after install. The on-launch auto-show is a one-shot per
upgrade and easy to dismiss accidentally — without a manual entry
point, users who close it have no in-app way to read what changed.
"""
from __future__ import annotations

import re
from pathlib import Path


def _src() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "gui" / "pages" / "settings_page.py"
            ).read_text(encoding="utf-8")


def _en_json() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "translations" / "en.json"
            ).read_text(encoding="utf-8")


def test_settings_page_has_view_patch_notes_handler():
    """A handler must exist that opens PatchNotesDialog with the
    full changelog (latest_only=False) so users can scroll back
    through prior releases."""
    src = _src()
    assert "_on_view_patch_notes" in src, (
        "settings_page must expose a _on_view_patch_notes handler "
        "so the About card's button has something to call")
    anchor = src.find("def _on_view_patch_notes")
    assert anchor != -1
    next_def = src.find("\n    def ", anchor + 20)
    body = src[anchor:next_def if next_def != -1 else anchor + 2000]
    assert "PatchNotesDialog" in body, (
        "handler must construct PatchNotesDialog so the dialog "
        "actually appears")
    assert "latest_only=False" in body, (
        "manual View Patch Notes opens the FULL changelog, not just "
        "the latest entry — that lets users scroll back through "
        "prior releases too")


def test_about_card_is_added_to_settings_layout():
    """The PushSettingCard must be constructed and wired to the
    handler so clicking it actually fires."""
    src = _src()
    assert "self._about_card = PushSettingCard" in src, (
        "must create an _about_card PushSettingCard so the user "
        "has a visible entry point on the Settings page")
    assert re.search(
        r"self\._about_card\.clicked\.connect\(\s*self\._on_view_patch_notes",
        src), (
        "the about card's clicked signal must be connected to the "
        "_on_view_patch_notes handler")


def test_about_card_has_translation_keys():
    """Translation keys for the About card title, description, and
    button label must exist in en.json."""
    en = _en_json()
    for key in ("settings.about_title",
                "settings.about_desc",
                "settings.view_patch_notes",
                "settings.view_patch_notes_failed_title"):
        assert f'"{key}"' in en, (
            f"missing translation key {key!r} in en.json — the "
            "About card will render an untranslated key as a string")
    # about_title takes a {version} parameter so it can show the
    # currently-installed version next to "About CDUMM".
    assert "{version}" in en[en.find('"settings.about_title"'):
                              en.find('"settings.about_title"') + 200], (
        "settings.about_title should embed {version} so it reads "
        "'About CDUMM (v3.2)' rather than a static title")
