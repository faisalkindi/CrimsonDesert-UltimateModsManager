"""Bug #31: ``closeEvent`` stopped only 2 of the 5 QTimers the main
window creates. Missing:

- ``_db_change_timer`` (debounced DB watcher)
- ``_nxm_poll_timer`` (drains pending_nxm.txt)
- ``_nexus_update_timer`` (30-min Nexus update check)

Qt parenting destroys them on window destruction so no functional
leak in practice, but the explicit list is inconsistent. This pins
the stop list against regressions.
"""
from __future__ import annotations

import re
from pathlib import Path


def _close_event_body() -> str:
    src = (Path(__file__).resolve().parents[1]
           / "src" / "cdumm" / "gui" / "fluent_window.py").read_text(
               encoding="utf-8")
    i = src.rfind("def closeEvent(")
    assert i != -1, "closeEvent not found"
    tail = src[i:]
    lines = tail.splitlines(keepends=True)
    out = [lines[0]]
    for line in lines[1:]:
        # Stop at the NEXT method at same indent.
        if (line.startswith("    def ") and not line.startswith("        def ")) \
                or line.startswith("def "):
            break
        out.append(line)
    return "".join(out)


def test_close_event_stops_nexus_update_timer():
    body = _close_event_body()
    assert re.search(r"_nexus_update_timer", body), (
        "closeEvent must stop _nexus_update_timer")


def test_close_event_stops_db_change_timer():
    body = _close_event_body()
    assert re.search(r"_db_change_timer", body), (
        "closeEvent must stop _db_change_timer")


def test_close_event_stops_nxm_poll_timer():
    body = _close_event_body()
    assert re.search(r"_nxm_poll_timer", body), (
        "closeEvent must stop _nxm_poll_timer")
