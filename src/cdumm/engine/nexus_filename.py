"""Master-branch stub for the Nexus-filename parser.

The CDUMM_API_Test branch has a full implementation that extracts the
NexusMods mod id + file version from filenames like
``MyMod-350--5-1775316604.zip``. Master intentionally ships without
Nexus integration — this stub returns ``(None, None)`` so the callers
that used the parsed values elsewhere simply fall back to their normal
filename handling.
"""

from __future__ import annotations


def parse_nexus_filename(stem: str) -> tuple[int | None, str | None]:
    """No-op: master branch does not integrate with Nexus."""
    return None, None
