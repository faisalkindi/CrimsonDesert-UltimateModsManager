"""Bug 38: parse_nxm_url raises NxmUrlError on malformed URLs. The
call site in _handle_nxm_url didn't catch it, so a bad URL bubbles
up as a generic "Could not handle" toast from _process_pending_nxm
— not a specific "That NXM URL is malformed" message.
"""
from __future__ import annotations

import re
from pathlib import Path


def test_handle_nxm_url_wraps_parse_with_try():
    src = (Path(__file__).resolve().parents[1]
           / "src" / "cdumm" / "gui" / "fluent_window.py").read_text(
               encoding="utf-8")
    i = src.find("def _handle_nxm_url(")
    assert i != -1
    # First ~1800 chars of the method body — bumped from 1000 after
    # 2026-04-27 docstring expansion (intended_mod_id parameter)
    # pushed the parse_nxm_url call past the prior window. Window
    # is still tight enough to catch a real regression where the
    # try/except disappears.
    scope = src[i:i + 1800]
    # parse_nxm_url call must be followed by (or wrapped in) a try.
    assert re.search(r"except\s+NxmUrlError", scope), (
        "_handle_nxm_url must catch NxmUrlError so malformed URLs "
        "get a specific user message instead of a generic crash")
