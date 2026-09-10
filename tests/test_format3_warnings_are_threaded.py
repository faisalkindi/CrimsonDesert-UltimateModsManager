"""Every ``_intents_to_v2_changes`` call has to pass ``warnings_out``.

The per-intent writers raise refusals through ``warnings_out``, which
``expand_format3_into_aggregated`` collects into ``f3_warnings`` and
``apply_engine`` emits to the GUI as a "Format 3 warning(s)" InfoBar.
A call site that omits the argument drops every refusal on that path:
the intents are skipped, nothing is written, and the user is told
nothing.

Found while working #414. Three of the four call sites omitted it, all
of them on whole-table branches where intents the whole-table writer
does not claim fall through to the generic path. The refusals raised
there could never reach a user.

This is checked on the source rather than by driving an apply, because
reaching those branches needs a whole-table target, a real vanilla body
and header, and a mod whose intents split across the writer and the
passthrough. The property that matters is simple and local: no call
site forgets the argument.
"""
from __future__ import annotations

import re
from pathlib import Path

_SRC = (Path(__file__).resolve().parents[1]
        / "src" / "cdumm" / "engine" / "format3_apply.py")

#: The call plus whatever follows on the next few lines, so a
#: multi-line call is captured whole.
_CALL = re.compile(
    r"_intents_to_v2_changes\((?P<args>[^)]*)\)", re.DOTALL)


def _call_sites() -> list[str]:
    src = _SRC.read_text(encoding="utf-8")
    # Drop the definition itself; only invocations matter.
    src = src.replace("def _intents_to_v2_changes(", "def _DEFINITION(")
    return [m.group("args") for m in _CALL.finditer(src)]


def test_there_are_call_sites_to_check():
    """Guard against the regex silently matching nothing."""
    assert len(_call_sites()) >= 4


def test_every_call_site_passes_warnings_out():
    missing = [a.strip()[:70] for a in _call_sites()
               if "warnings_out" not in a]
    assert not missing, (
        "these _intents_to_v2_changes call sites drop warnings_out, so "
        f"refusals raised on those paths never reach the user: {missing}")


def test_the_helper_still_accepts_the_argument():
    """If the parameter is ever renamed, the test above goes green for
    the wrong reason."""
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("def _intents_to_v2_changes(")
    sig = src[i:src.index(")", i)]
    assert "warnings_out" in sig
