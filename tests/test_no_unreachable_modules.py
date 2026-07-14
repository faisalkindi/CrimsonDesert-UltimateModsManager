"""Every engine module must be reachable from the app.

This test exists because the same bug shipped THREE times:

  #288  a `.cdmod` translator that nothing called
  #296  the offset re-anchor -- module + 12 passing tests, imported by nothing,
        so the mods it was meant to fix were still being refused
  #297  the v2 -> Format 3 converter -- verified byte-for-byte, no way for a
        user to reach it

Every time, the tests passed. That is the trap: a unit test proves the code is
CORRECT, and says nothing about whether it is REACHABLE. A feature no user can
invoke is not a feature, and green tests make it look finished.

A human has now missed this three times, so it stops being a human's job.

If this test fails, you have two honest options:
  * wire the module into the app, or
  * add it to KNOWN_LIBRARY_ONLY below with a reason -- which puts the gap in
    writing instead of leaving it to be discovered by a user.
"""
from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "cdumm"

#: Modules that legitimately have no caller inside the app yet. Each needs a
#: reason. This list is a disclosure, not a dumping ground -- if it grows, the
#: project is accumulating code that does nothing.
KNOWN_LIBRARY_ONLY = {
    # Decodes the game's 187,526-string localization table and round-trips it
    # byte-identically. Nothing consumes it until the `.cdmod`
    # localization-patch apply path lands (GitHub #290).
    "paloc_handler",

    # PRE-EXISTING, and found by this test on its very first run.
    # `UpdateOverlay` was wired from v0.9.2 until the v3.0.0 Fluent UI
    # overhaul (455a137) removed the last constructor call. It has been
    # orphaned ever since -- still force-included in cdumm.spec, still
    # translated into all 16 locales, still constructed by nothing.
    # Exempted (not deleted) because it is upstream's code and removing it
    # touches both PyInstaller specs and 16 translation files. It is a
    # deletion candidate, not a bug.
    "update_overlay",
}

#: Reached from tools/, not from the app. Not dead, just not app code.
TOOLS_ONLY = {"schema_verify"}


def _module_names() -> list[str]:
    out = []
    for p in SRC.rglob("*.py"):
        if p.name == "__init__.py" or "_vendor" in p.parts:
            continue
        out.append(p.stem)
    return out


def _imported_names() -> set[str]:
    """Every module name mentioned in an import anywhere under src/cdumm."""
    seen: set[str] = set()
    for p in SRC.rglob("*.py"):
        if "_vendor" in p.parts:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    seen.add(a.name.rsplit(".", 1)[-1])
            elif isinstance(node, ast.ImportFrom):
                # `from cdumm.engine.offset_reanchor import reanchor_changes`
                if node.module:
                    seen.add(node.module.rsplit(".", 1)[-1])
                # `from cdumm.engine import offset_reanchor`
                for a in node.names:
                    seen.add(a.name)
    return seen


def test_no_engine_module_is_unreachable():
    imported = _imported_names()
    unreachable = sorted(
        name for name in _module_names()
        if name not in imported
        and name not in KNOWN_LIBRARY_ONLY
        and name not in TOOLS_ONLY
    )

    assert not unreachable, (
        "these modules are imported by NOTHING in the app:\n  "
        + "\n  ".join(unreachable)
        + "\n\nA module nothing calls is not a feature, however green its "
          "tests are (#288, #296, #297 all shipped exactly this way).\n"
          "Wire it up, or add it to KNOWN_LIBRARY_ONLY with a reason so the "
          "gap is written down instead of being found by a user."
    )


def test_the_disclosure_list_stays_honest():
    """A module listed as library-only must actually still be unreferenced.
    Otherwise the list rots into a lie that hides a real regression."""
    imported = _imported_names()
    now_wired = sorted(n for n in KNOWN_LIBRARY_ONLY if n in imported)
    assert not now_wired, (
        f"{now_wired} are listed as library-only but ARE now imported. "
        "Remove them from KNOWN_LIBRARY_ONLY -- a stale exemption is how a "
        "real regression sneaks back in."
    )
