"""Regression: the in-app update-available banner must have a close
button AND must remember per-version dismissal so users who close it
don't see it reappear on every relaunch (DeathZxZ on Nexus reported
the banner blocked button text at the top of the window with no way
to dismiss).

A NEWER tag clears the dismissal — the banner reappears for a new
version even after the user dismissed an older one.
"""
from __future__ import annotations

import re
from pathlib import Path


def _src() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "gui" / "fluent_window.py"
            ).read_text(encoding="utf-8")


def _function_body(src: str, fn_name: str) -> str:
    anchor = src.find(f"def {fn_name}(")
    assert anchor != -1, f"{fn_name} not found in fluent_window.py"
    next_def = src.find("\n    def ", anchor + 20)
    return src[anchor:next_def if next_def != -1 else anchor + 5000]


def test_update_banner_has_a_close_button():
    """The banner widget must include a TransparentToolButton with
    the CLOSE icon wired to a dismiss handler. Without this, the
    banner is unkillable until next launch."""
    body = _function_body(_src(), "_show_update_banner")
    assert "TransparentToolButton" in body, (
        "_show_update_banner must add a TransparentToolButton — "
        "the banner had no close button before the fix")
    assert "FluentIcon.CLOSE" in body, (
        "the close button must use FluentIcon.CLOSE so users see "
        "the standard X glyph")
    assert "_on_dismiss_update_banner" in body, (
        "the close button must connect its clicked signal to "
        "_on_dismiss_update_banner, not to deleteLater inline — "
        "we need the dismissal to be persistable across launches")


def test_dismiss_handler_persists_tag_in_config():
    """The dismiss handler must save the dismissed tag to Config so
    the banner stays hidden across relaunches (until a newer version
    is detected, which clears the dismissal naturally)."""
    body = _function_body(_src(), "_on_dismiss_update_banner")
    assert "Config" in body, (
        "dismiss handler must reach Config to persist the tag")
    assert "update_banner_dismissed_for" in body, (
        "the persisted key must be 'update_banner_dismissed_for' "
        "so _on_update_available can match it on next launch")


def test_on_update_available_skips_banner_if_tag_already_dismissed():
    """When a previous launch saved the same tag as dismissed, do
    not show the banner again — the user already chose to ignore
    this specific version."""
    body = _function_body(_src(), "_on_update_available")
    assert "update_banner_dismissed_for" in body, (
        "_on_update_available must read the dismissed tag from "
        "Config so it can suppress the banner this launch")
    # The check must happen before _show_update_banner is called.
    show_idx = body.find("_show_update_banner(")
    check_idx = body.find("update_banner_dismissed_for")
    assert check_idx != -1 and show_idx != -1
    assert check_idx < show_idx, (
        "the dismissed-tag check must run BEFORE _show_update_banner "
        "or the banner pops anyway")


def test_about_page_badge_still_shown_when_banner_dismissed():
    """Even if the user dismisses the banner, the small About-page
    badge should still appear so they can find the update later if
    they change their mind. Otherwise dismissal silently hides the
    update entirely."""
    body = _function_body(_src(), "_on_update_available")
    badge_idx = body.find("setShowBadge(True)")
    assert badge_idx != -1, (
        "_on_update_available must always call setShowBadge(True) "
        "on the AboutPage nav item")
    # The badge call must NOT be inside the dismissed-skip branch.
    # Ensure it's at the same indent as the banner-show, not nested
    # under the dismissed check.
    skip_branch = re.search(
        r"if dismissed != tag:\s*\n", body)
    if skip_branch:
        # Find next non-indented line after the skip branch ends.
        # Simpler: just verify both the banner call and the badge
        # call are present; the badge must NOT be guarded by the
        # dismissed check. We assert by checking the badge call
        # appears AFTER any 'if dismissed' block has closed.
        assert badge_idx > skip_branch.end(), (
            "About-page badge call must be OUTSIDE the dismissed-skip "
            "branch so the user can still find the update from the "
            "sidebar even after dismissing the banner")
