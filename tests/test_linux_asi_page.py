"""Linux placeholder-mode regression tests for ``AsiPluginsPage``.

When the ASI page builds in placeholder mode (macOS / Linux), the
normal scroll/card/summary-bar widgets are never created. Methods
that the host (``fluent_window``) invokes unconditionally during
startup — ``set_managers``, ``refresh``, ``set_nexus_updates`` —
must early-return rather than reach into widgets that don't exist.

Lesson learned the hard way: an earlier pass added the Linux branch
to ``_build_ui`` but forgot to widen the three corresponding
``if IS_MACOS: return`` guards to also cover Linux. CDUMM booted
all the way through the welcome wizard, populated its database,
loaded schemas, then crashed inside ``_init_navigation`` when
``set_managers`` walked through to ``refresh`` and hit
``AttributeError: 'AsiPluginsPage' object has no attribute
'_scroll_layout'``. These tests pin the contract so that bug
can't come back.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pytestqt")

from cdumm.gui.pages import asi_page  # noqa: E402


@pytest.fixture
def linux_host(monkeypatch):
    """Flip the module-level platform flags so the Linux branches in
    ``AsiPluginsPage`` run regardless of the host OS the tests are
    invoked on (the upstream Windows regression box, macOS, etc.)."""
    monkeypatch.setattr(asi_page, "IS_LINUX", True)
    monkeypatch.setattr(asi_page, "IS_MACOS", False)
    monkeypatch.setattr(asi_page, "IS_WINDOWS", False)


class TestLinuxPlaceholderMode:
    """Methods invoked during normal startup must not crash when the
    page is in placeholder mode."""

    def test_set_managers_is_a_noop_on_linux(
            self, qtbot, tmp_path: Path, linux_host) -> None:
        """``fluent_window._init_navigation`` calls ``set_managers``
        on every page during boot. On Linux the ASI page is a
        placeholder; ``set_managers`` must NOT try to wire up
        ``AsiManager`` (the engine machinery is intentionally absent)
        and must NOT call ``refresh`` (which would touch the
        non-existent ``_scroll_layout``)."""
        page = asi_page.AsiPluginsPage()
        qtbot.addWidget(page)
        # Should return without touching the engine. No exception,
        # no _asi_manager assignment.
        page.set_managers(game_dir=tmp_path, db=None)
        assert page._asi_manager is None, (
            "set_managers must not wire up AsiManager in Linux "
            "placeholder mode — the engine is intentionally absent")

    def test_refresh_is_a_noop_on_linux(
            self, qtbot, linux_host) -> None:
        """``refresh`` walks ``_scroll_layout`` and ``_summary_bar``.
        Neither exists on the placeholder page. The method must
        early-return before touching them."""
        page = asi_page.AsiPluginsPage()
        qtbot.addWidget(page)
        # No exception even though _scroll_layout doesn't exist.
        page.refresh()
        assert not hasattr(page, "_scroll_layout"), (
            "Placeholder page should never construct _scroll_layout — "
            "if this assertion fails, _build_ui is no longer "
            "short-circuiting on Linux")

    def test_set_nexus_updates_is_a_noop_on_linux(
            self, qtbot, linux_host) -> None:
        """``fluent_window`` calls ``set_nexus_updates`` after every
        Nexus poll. The placeholder page has no version pills to
        update; the method must early-return."""
        page = asi_page.AsiPluginsPage()
        qtbot.addWidget(page)
        # Pass a non-empty dict to make sure the method actually
        # gets exercised (an empty dict could short-circuit elsewhere).
        page.set_nexus_updates({12345: object()})

    def test_full_init_navigation_sequence_does_not_crash(
            self, qtbot, tmp_path: Path, linux_host) -> None:
        """End-to-end: construct the page, wire managers, refresh,
        push a Nexus updates dict — the exact sequence that
        ``fluent_window._init_navigation`` invokes. Locks in the
        whole startup-time contract, not just one method."""
        page = asi_page.AsiPluginsPage()
        qtbot.addWidget(page)
        page.set_managers(game_dir=tmp_path, db=None)
        page.refresh()
        page.set_nexus_updates({})
