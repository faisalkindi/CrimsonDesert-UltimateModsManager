"""Meta guard: every helper in the Nexus-facing code must have at
least one non-definition call site.

This catches the "I wrote a helper, wrote a unit test, committed
it, but the real flow never calls it" trap that produced Bugs 34,
35, and 43 silently. Keeps my process honest — if a helper exists
but isn't invoked in production code, the test fails and the fix
is exposed as unfinished.
"""
from __future__ import annotations

import re
from pathlib import Path


_HELPERS_REQUIRING_CALLERS = {
    # (module_relpath, helper_name): "why it must be wired"
    ("src/cdumm/gui/fluent_window.py", "_snapshot_selected_labels"):
        "Bug 24: preserve preset selections across click-to-update",
    ("src/cdumm/gui/fluent_window.py", "_restore_selected_labels"):
        "Bug 24: restore preset selections post-reimport",
    ("src/cdumm/gui/fluent_window.py", "_clear_pending_post_import_state"):
        "Bug 14: clear scratch state after every import",
    ("src/cdumm/gui/fluent_window.py", "_clear_auth_banner_state"):
        "Bug 32: dismiss auth banner when user saves valid key",
    ("src/cdumm/gui/fluent_window.py", "_assert_https_download_url"):
        "Bug 27: refuse non-HTTPS CDN URLs",
    ("src/cdumm/gui/fluent_window.py", "_validate_download_size"):
        "Bug 29: up-front Content-Length cap",
    ("src/cdumm/gui/fluent_window.py", "_check_download_progress"):
        "Bug 28: streaming-total cap",
    ("src/cdumm/gui/fluent_window.py", "_decide_auth_banner"):
        "Bug 18: one-shot logic for the auth banner",
    ("src/cdumm/gui/fluent_window.py", "_resolve_post_import_target_id"):
        "Bug 7: resolver for post-import DB writes",
    ("src/cdumm/gui/pages/settings_page.py", "_persist_nexus_key_if_valid"):
        "Bug 15: validate-first, save-after contract",
    ("src/cdumm/engine/nexus_api.py", "clear_outdated_after_update"):
        "Bug 9: flip entry to has_update=False post-download",
    ("src/cdumm/engine/nexus_api.py", "filter_outdated"):
        "Bug 2: drop confirmed-current from Settings dialog list",
    ("src/cdumm/engine/nexus_api.py", "get_rate_limit_snapshot"):
        "Bug 22: bug report should include last-known rate limits",
}


def _count_callers(module_rel: str, helper: str) -> int:
    """Count non-definition references to ``helper`` anywhere in
    ``src/cdumm/``. Excludes the ``def helper(`` and ``from ...
    import helper`` lines which aren't call sites.
    """
    src_root = Path(__file__).resolve().parents[1] / "src" / "cdumm"
    count = 0
    def_re = re.compile(rf"^\s*def\s+{re.escape(helper)}\s*\(")
    from_import_re = re.compile(
        rf"from\s+[\w.]+\s+import\s+[^\n]*\b{re.escape(helper)}\b")
    word_re = re.compile(rf"\b{re.escape(helper)}\b")
    for py in src_root.rglob("*.py"):
        for line in py.read_text(encoding="utf-8").splitlines():
            if def_re.search(line):
                continue
            if from_import_re.search(line):
                continue
            if word_re.search(line):
                count += 1
    return count


def test_every_helper_has_at_least_one_real_call_site():
    orphans = []
    for (module_rel, helper), reason in _HELPERS_REQUIRING_CALLERS.items():
        n = _count_callers(module_rel, helper)
        if n < 1:
            orphans.append((module_rel, helper, reason))
    assert not orphans, (
        "orphaned helpers (defined but never called):\n" +
        "\n".join(f"  {m}::{h} — {r}" for m, h, r in orphans))
