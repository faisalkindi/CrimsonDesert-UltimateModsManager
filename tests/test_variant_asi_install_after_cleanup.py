"""Variant-bundled ASI install must run BEFORE the variant pre-extract
temp dir is removed.

Bug 2026-05-10 (Democles85, GitHub #81 follow-up): the v3.2.16 variant-
pack picker collected sibling .asi files (Character Creator's
``CharacterCreatorHead.asi``) into ``_pending_asi_from_variant`` so they
could be installed after the body-type import finished. The post-import
handler then deleted ``_pending_variant_cleanup`` (the archive's pre-
extract temp dir) at the very top of its work. By the time the ASI
install loop tried to ``shutil.copy2`` each staged path, the directory
holding those paths had already been removed and every ``p.exists()``
check returned False, so the .asi was silently skipped — the ASI Mods
tab stayed empty and the post-import banner read "imported" with no
plugin count.

The fix defers the variant cleanup ``rmtree`` until AFTER the ASI install
loop has copied the bundled plugins into ``bin64/``. This test pins the
ordering invariant so a future refactor cannot re-introduce the eager-
cleanup regression.

Source-level check rather than a Qt-driven integration test because the
ordering lives inside a closure (``_on_finished``) that ``_launch_import_worker``
builds on every import. Spinning up a real ``QProcess`` for one assertion
would dwarf the value, and the bug is purely about the relative position
of two existing statements.
"""
from __future__ import annotations

from pathlib import Path

import re


def _src() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "gui" / "fluent_window.py"
            ).read_text(encoding="utf-8")


def _line_of(pattern: str, src: str) -> int:
    """Return the 1-indexed line number of the first regex match, or -1."""
    rx = re.compile(pattern)
    for i, line in enumerate(src.splitlines(), start=1):
        if rx.search(line):
            return i
    return -1


def test_variant_cleanup_rmtree_runs_after_asi_install_loop():
    """The ``shutil.rmtree`` that removes ``_pending_variant_cleanup``
    must appear AFTER the loop that copies ``_pending_asi_from_variant``
    plugins into ``bin64/``. Reverse order silently drops every bundled
    ASI because the staged paths point inside the deleted tree."""
    src = _src()

    # Anchor on the variant_asi pull from self — that is the first line
    # of the ASI install block in `_on_finished`.
    asi_pull_line = _line_of(
        r"variant_asi\s*=\s*getattr\(self,\s*['\"]_pending_asi_from_variant['\"]",
        src,
    )
    assert asi_pull_line > 0, (
        "Could not find the variant_asi pull line in fluent_window.py — "
        "has _on_finished been refactored? Update this regression test "
        "to find the new ASI install entry point."
    )

    # The line that captures _pending_variant_cleanup into a local must
    # exist; everything we test is the position of the rmtree that follows.
    capture_line = _line_of(
        r"vtmp\s*=\s*getattr\(self,\s*['\"]_pending_variant_cleanup['\"]",
        src,
    )
    assert capture_line > 0, (
        "Expected `vtmp = getattr(self, '_pending_variant_cleanup', ...)` "
        "in fluent_window.py — variant cleanup state has been moved or "
        "renamed, this test needs the new symbol."
    )

    # The actual rmtree of vtmp. We allow either the deferred form
    # (`if vtmp is not None: shutil.rmtree(str(vtmp), ...)`) or any
    # other call as long as the LINE NUMBER is past the ASI install
    # block.
    rmtree_line = _line_of(
        r"shutil\.rmtree\(\s*str\(\s*vtmp\s*\)", src)
    assert rmtree_line > 0, (
        "Expected a `shutil.rmtree(str(vtmp), ...)` call in "
        "fluent_window.py to clean up the variant pre-extract dir. "
        "Without it the temp dir leaks into %TEMP%."
    )

    assert rmtree_line > asi_pull_line, (
        "REGRESSION: variant pre-extract dir is being removed BEFORE "
        "the ASI install loop runs.\n"
        f"  rmtree(vtmp) at line {rmtree_line}\n"
        f"  variant_asi   at line {asi_pull_line}\n"
        "Bundled .asi paths point inside the deleted tree, so every "
        "`p.exists()` check fails silently and CharacterCreatorHead.asi "
        "(and any other variant-pack ASI) is dropped on the floor. "
        "Move the rmtree to AFTER the ASI install loop. See changelog "
        "entry for v3.2.17."
    )


def test_variant_cleanup_capture_does_not_eagerly_rmtree():
    """The line that snapshots ``_pending_variant_cleanup`` into
    ``vtmp`` must NOT be followed within the next two lines by an
    ``rmtree`` call — that was the exact shape of the regression. The
    capture should only clear the attribute and store the path locally
    for use later in the function."""
    src = _src()
    lines = src.splitlines()
    capture_line = _line_of(
        r"vtmp\s*=\s*getattr\(self,\s*['\"]_pending_variant_cleanup['\"]",
        src,
    )
    assert capture_line > 0
    window = "\n".join(lines[capture_line:capture_line + 4])
    assert "rmtree" not in window, (
        "REGRESSION: variant pre-extract dir is removed in the same "
        "block that captures it. The cleanup must be deferred until "
        "after the ASI install loop — see "
        "test_variant_cleanup_rmtree_runs_after_asi_install_loop "
        "and the v3.2.17 changelog entry.\n"
        f"Lines {capture_line + 1}..{capture_line + 4}:\n{window}"
    )
