"""Bug #10: ``slug_placeholder()`` checks ``APPLICATION_SLUG ==
"cdumm"``. If Nexus happens to assign the literal slug "cdumm" as
CDUMM's real slug, the check stays True forever and the GUI keeps
showing "Pending approval" even after approval landed.

Fix: the flag should be tied to a dedicated constant that's flipped
when approval lands, not inferred from the slug string.
"""
from __future__ import annotations

import pytest


def test_slug_placeholder_returns_true_when_approval_flag_is_false():
    """While the approval flag is False, ``slug_placeholder`` must
    report True regardless of the slug string."""
    import cdumm.engine.nexus_sso as sso
    original = getattr(sso, "_SLUG_APPROVED", None)
    assert original is not None, (
        "nexus_sso must expose _SLUG_APPROVED flag so callers can "
        "decouple approval state from the slug string")
    try:
        sso._SLUG_APPROVED = False
        assert sso.slug_placeholder() is True
    finally:
        sso._SLUG_APPROVED = original


def test_slug_placeholder_returns_false_when_approval_flag_is_true():
    """Once approval lands, flipping ``_SLUG_APPROVED`` to True must
    make ``slug_placeholder`` return False — even if the slug still
    happens to be the literal ``cdumm``."""
    import cdumm.engine.nexus_sso as sso
    original_flag = getattr(sso, "_SLUG_APPROVED", None)
    original_slug = sso.APPLICATION_SLUG
    try:
        sso._SLUG_APPROVED = True
        # Leave slug as-is; the function must rely on the flag, not
        # a string comparison.
        assert sso.slug_placeholder() is False
    finally:
        sso._SLUG_APPROVED = original_flag
        sso.APPLICATION_SLUG = original_slug
