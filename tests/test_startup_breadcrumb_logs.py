"""Bug B from Nexus 2026-05-03 (gabagoolboi47, Pr0nt): CDUMM3.exe
crashes immediately on launch but stays in Task Manager (= hangs).
"used to work before 1.4". Reporters didn't include logs.

Without log evidence we can't diagnose, so the targeted fix is to
make startup more verbose:

1. logger.info breadcrumbs around each heavy step (schema load,
   fingerprint backfill, etc.) so when a future reporter shares
   their log we see exactly where it stopped.
2. Upgrade the silent `logger.debug` for schema-load failure to
   `logger.warning` so it surfaces in default logs.

This doesn't fix the underlying hang (we don't know what it is)
but ensures the next report gives us actionable info.
"""
from __future__ import annotations
import re
from pathlib import Path

import pytest


_MAIN_PATH = (
    Path(__file__).resolve().parent.parent
    / "src" / "cdumm" / "main.py"
)


def test_startup_logs_before_init_schemas():
    """A breadcrumb logger.info must appear in the startup path right
    before init_schemas() is invoked, so a log captured at INFO level
    shows whether startup reached the schema-load step."""
    text = _MAIN_PATH.read_text(encoding="utf-8")
    schema_call = text.find("init_schemas()")
    assert schema_call > 0, "init_schemas call not found"
    # Look at the 250 chars BEFORE the call
    window = text[max(0, schema_call - 250):schema_call]
    assert re.search(r'logger\.info\(["\'].*schema', window, re.IGNORECASE), (
        "Need a logger.info breadcrumb right before init_schemas() so "
        "we can tell from the user's log whether startup reached this "
        "step. Window before call:\n" + window
    )


def test_startup_logs_before_fingerprint_backfill():
    """Breadcrumb before backfill_stored_fingerprints. If the exe
    hangs there (e.g. AV scanning the game .exe), the log shows we
    got to fingerprint but not past."""
    text = _MAIN_PATH.read_text(encoding="utf-8")
    backfill_call = text.find("backfill_stored_fingerprints(db,")
    assert backfill_call > 0, "backfill call not found"
    window = text[max(0, backfill_call - 250):backfill_call]
    assert re.search(
        r'logger\.info\(["\'].*[Ff]ingerprint',
        window
    ), (
        "Need a logger.info breadcrumb right before "
        "backfill_stored_fingerprints. Window:\n" + window
    )


def test_schema_load_failure_uses_warning_not_debug():
    """The except for init_schemas failure must log at WARNING (or
    higher) so the failure surfaces in default logs. logger.debug
    hides it from the user — the agent's Rank 1 hypothesis for Bug B
    was a silent schema load failure that nobody saw."""
    text = _MAIN_PATH.read_text(encoding="utf-8")
    # Find the except block right after init_schemas
    schema_idx = text.find("init_schemas()")
    assert schema_idx > 0
    # Look at the 200 chars after it for the except handler
    window = text[schema_idx:schema_idx + 350]
    assert "except Exception" in window, "except block not found near init_schemas"
    # The handler must NOT use logger.debug for the exception
    # (logger.warning, .error, or .exception are acceptable).
    assert "logger.debug" not in window or (
        "logger.warning" in window or "logger.exception" in window
    ), (
        "init_schemas exception is logged at debug only — failures "
        "won't appear in default user logs. Use logger.warning/error/"
        "exception so silent schema-load crashes surface. Window:\n"
        + window
    )
