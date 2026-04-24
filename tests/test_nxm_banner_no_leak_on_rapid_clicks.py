"""Bug 39: rapid NXM clicks overwrite self._nxm_download_banner
without closing the previous banner. The orphaned InfoBar widgets
pile up at the top of the window until main-window destruction.
"""
from __future__ import annotations

import re
from pathlib import Path


def test_banner_create_closes_previous_one_first():
    """The banner-creation block must close any pre-existing banner
    before assigning a new one."""
    src = (Path(__file__).resolve().parents[1]
           / "src" / "cdumm" / "gui" / "fluent_window.py").read_text(
               encoding="utf-8")
    # Find the assignment: self._nxm_download_banner = InfoBar.info(
    m = re.search(
        r"(self\._nxm_download_banner\s*=\s*InfoBar\.info\()",
        src)
    assert m, "download banner creation site not found"
    # Look back ~250 chars for a prior-banner close call.
    head = src[max(0, m.start() - 400):m.start()]
    assert re.search(r"_nxm_download_banner.*close", head, re.DOTALL), (
        "must close any existing self._nxm_download_banner before "
        "overwriting — rapid NXM clicks otherwise leak InfoBars")
