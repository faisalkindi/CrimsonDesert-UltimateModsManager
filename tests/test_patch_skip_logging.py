"""Issue #222 (falobos76): when CDUMM skips byte-patches whose original
bytes don't match the current game (version drift), the skip detail must
be written to the log — not only the transient post-apply InfoBar — so a
saved bug report (which tails cdumm.log) records WHICH patches were
skipped and can be diagnosed without the user's screenshot.

Pins the contract of ``apply_engine.log_patch_skips``: it logs every
(hex-truncated) skip line at WARNING and returns the same lines the
InfoBar reuses, so the pop-up and the report can't drift apart.
"""
from __future__ import annotations

import logging

from cdumm.engine.apply_engine import log_patch_skips

_LOGGER = "cdumm.engine.apply_engine"


def _skip(label, expected="1F1C0000", actual="74830000",
          reason="byte mismatch at offset 36720"):
    return {"label": label, "expected": expected, "actual": actual,
            "reason": reason}


def test_skips_are_logged_at_warning(caplog):
    skips = [_skip("[KLIFF TELEPORT] starting animations"),
             _skip("[KLIFF TELEPORT] ending animations")]
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        lines, overflow = log_patch_skips(skips)

    assert overflow == 0
    assert len(lines) == 2
    text = "\n".join(r.getMessage() for r in caplog.records)
    # the per-patch detail the pop-up shows must now be in the log
    assert "[KLIFF TELEPORT] starting animations" in text
    assert "[KLIFF TELEPORT] ending animations" in text
    assert "expected 1F1C0000" in text
    assert "2 JSON patch(es) skipped" in text


def test_returned_lines_match_logged_lines(caplog):
    """The InfoBar reuses the returned lines; they must be exactly what
    was logged so the pop-up and the bug report can't drift apart."""
    skips = [_skip(f"patch {i}") for i in range(3)]
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        lines, _ = log_patch_skips(skips)
    logged = [r.getMessage() for r in caplog.records]
    for ln in lines:
        assert ln.strip() in logged


def test_whole_table_hex_is_truncated(caplog):
    """Whole-table changes carry multi-MB expected/actual hex; the log
    line must truncate it, not dump the whole blob (falobos76, #191)."""
    big = "AB" * 5000  # stands in for a whole-table expected/actual blob
    skips = [_skip("iteminfo whole-table", expected=big, actual=big,
                   reason="whole-table mismatch")]
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        lines, _ = log_patch_skips(skips)
    assert big not in lines[0]
    assert "..." in lines[0] and "bytes)" in lines[0]
    # the full blob must not appear in any log record either
    assert all(big not in r.getMessage() for r in caplog.records)


def test_overflow_beyond_limit(caplog):
    skips = [_skip(f"patch {i}") for i in range(20)]
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        lines, overflow = log_patch_skips(skips, limit=15)
    assert len(lines) == 15
    assert overflow == 5
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "... and 5 more" in text
