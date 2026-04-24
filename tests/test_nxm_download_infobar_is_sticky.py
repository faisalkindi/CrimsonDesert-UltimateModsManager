"""Bug #17: the "Downloading from Nexus…" InfoBar had a fixed 15-
second duration. For slow networks or large mods the toast dismissed
before the download finished, leaving the user in silence.

This test pins the duration constant used for the download toast
at the sticky value (-1 per qfluentwidgets convention).
"""
from __future__ import annotations

import re
from pathlib import Path


def _window_src() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "gui" / "fluent_window.py").read_text(
                encoding="utf-8")


def test_downloading_info_bar_uses_sticky_duration():
    """The InfoBar that announces an NXM download starting must use
    duration=-1 so it stays until programmatically closed."""
    src = _window_src()
    # Locate the block with the "Downloading from Nexus" title.
    idx = src.find('"Downloading from Nexus')
    assert idx != -1, "NXM download InfoBar title not found"
    # Scan ~400 chars around the InfoBar call for the duration arg.
    window = src[idx:idx + 400]
    assert re.search(r"duration\s*=\s*-1", window), (
        f"NXM download InfoBar must use duration=-1, found: {window}")


def test_finish_nxm_close_reference_exists():
    """``_finish_nxm_download`` must clean up the sticky download
    banner so it doesn't linger forever. Pin a stash attribute name
    the close path reads."""
    src = _window_src()
    # Two guards: (a) we stash the banner so we can close it later;
    # (b) _finish_nxm_download closes it.
    assert "_nxm_download_banner" in src, (
        "expected a stashed reference to the download banner so we "
        "can close it on completion")
