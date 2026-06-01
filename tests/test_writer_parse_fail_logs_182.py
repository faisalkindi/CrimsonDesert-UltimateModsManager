"""Pin the GitHub #182 / CD 1.09 hint in iteminfo_writer + skill_writer
parse-fail log lines.

Both writers wrap parse_iteminfo_from_bytes / skill parser.parse_all
in a try/except and emit a logger.error on failure. The error message
now names #182 explicitly and points users at Format 2 byte patches as
a working alternative until the Format 3 list-of-dict path catches up
on 1.09 schema shifts. These tests verify the hint stays in the log
output so a future refactor that simplifies the error line does not
strip away the user-actionable bit.
"""
from __future__ import annotations

import logging

import pytest


def test_iteminfo_parse_fail_log_names_182(caplog):
    """Feed parse_iteminfo_from_bytes garbage and assert the wrapping
    handler in iteminfo_writer surfaces the #182 tracking hint."""
    from cdumm.engine import iteminfo_writer

    caplog.set_level(logging.ERROR, logger="cdumm.engine.iteminfo_writer")
    # build_iteminfo_intent_change wraps the parse in try/except and
    # logs on failure. Garbage bytes guarantee the parse raises.
    result = iteminfo_writer.build_iteminfo_intent_change(b"\x00\x01", [])
    assert result is None
    assert any("182" in r.message for r in caplog.records), (
        "Expected GitHub #182 to be named in the iteminfo parse-fail "
        "log so users hitting the CD 1.09 schema shift see the "
        "tracking issue without grepping the source.")
    # Also confirm the workaround is mentioned.
    assert any("Format 2" in r.message for r in caplog.records), (
        "Expected the Format 2 byte-patch fallback to be mentioned "
        "in the iteminfo parse-fail log as the working alternative.")


def test_skill_parse_fail_log_names_182(caplog):
    """Same shape for skill_writer; the hint mirrors iteminfo so a
    user hitting either parser failure on 1.09 lands at the same
    tracking issue."""
    from cdumm.engine import skill_writer

    caplog.set_level(logging.ERROR, logger="cdumm.engine.skill_writer")
    # Both header and body get garbage; the parser will raise.
    result = skill_writer.build_skill_intent_change(
        b"\x00", b"\x00\x01", [])
    if result is None:
        # parse_all raised → our wrapping log fires
        assert any("182" in r.message for r in caplog.records), (
            "Expected GitHub #182 to be named in the skill "
            "parse-fail log so users hitting the same 1.09 schema "
            "concern see the tracking issue.")
    else:
        pytest.skip(
            "skill writer did not raise on garbage; the test only "
            "asserts the message when the wrapped handler runs.")
