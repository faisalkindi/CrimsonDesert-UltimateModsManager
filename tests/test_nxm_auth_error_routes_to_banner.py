"""Bug #21: Click-To-Update with an invalid API key landed in the
generic ``("other", str(e))`` error bucket — an error toast saying
"Download failed: Nexus rejected the API key (401)" with no link
to Settings or prompt to re-enter. Route it through the auth-banner
path instead.
"""
from __future__ import annotations

import re
from pathlib import Path


def _window_src() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "gui" / "fluent_window.py").read_text(
                encoding="utf-8")


def test_nxm_worker_catches_nexus_auth_error_explicitly():
    """The ``_worker`` closure inside ``_handle_nxm_url`` must have
    a dedicated ``except NexusAuthError:`` branch that tags the
    completion record as an auth failure (``('auth', ...)``) rather
    than dropping it into the generic bucket."""
    src = _window_src()
    # Scope: start of _handle_nxm_url to the next top-level method.
    i = src.find("def _handle_nxm_url(")
    assert i != -1
    tail = src[i:]
    # Walk forward for a 4-space-indent def that's not 8-space (next method).
    lines = tail.splitlines(keepends=True)
    out = [lines[0]]
    for line in lines[1:]:
        if line.startswith("    def ") and not line.startswith("        def "):
            break
        out.append(line)
    scope = "".join(out)
    assert re.search(r"except\s+NexusAuthError", scope), (
        "expected explicit 'except NexusAuthError' branch in the "
        "_handle_nxm_url worker closure")


def test_finish_nxm_download_handles_auth_kind():
    """``_finish_nxm_download`` must have an ``auth`` branch that
    sets the auth-error flag so ``_apply_nexus_update_colors`` can
    surface the same banner the auto-check uses."""
    src = _window_src()
    i = src.find("def _finish_nxm_download(")
    assert i != -1
    tail = src[i:]
    lines = tail.splitlines(keepends=True)
    out = [lines[0]]
    for line in lines[1:]:
        if line.startswith("    def ") and not line.startswith("        def "):
            break
        out.append(line)
    scope = "".join(out)
    # Required: a branch that matches kind == "auth".
    assert re.search(r'kind\s*==\s*[\'"]auth[\'"]', scope), (
        "_finish_nxm_download must have a branch for kind=='auth'")
    assert "_pending_nexus_auth_error" in scope, (
        "auth branch should set _pending_nexus_auth_error = True "
        "so the banner surfaces through _apply_nexus_update_colors")
