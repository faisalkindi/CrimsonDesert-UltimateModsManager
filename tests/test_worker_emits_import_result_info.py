"""Worker subprocess must propagate ``ModImportResult.info`` to the
GUI via the "done" message.

Round-9 systematic-debugging finding (2026-04-29): the multi-file
partial-skip fix populates ``result.info`` with "Imported, but N
file(s) skipped...", and import_handler.py wires that into
ModImportResult — but worker_process.py:77-79 emits the "done"
message with name / mod_id / mod_type / asi_staged ONLY. The info
field is silently dropped between the import subprocess and the
GUI parent process. User never sees the skip warning.

Fix: include ``info`` in the "done" payload. The GUI's import-
done handler already shows InfoBar.warning when info is set on
the worker result (or it should — round-10 will verify the GUI
side too).
"""
from __future__ import annotations


def test_done_message_includes_info_field():
    """Inspect the worker's "done" emit shape — verify it carries
    the info field from ModImportResult."""
    # Static-source check: scan worker_process.py for the "done"
    # emit around line 77-79 and assert "info" appears in the dict
    # being sent. We can't easily run the actual worker
    # subprocess in a unit test, but the static contract is what
    # the GUI parses, so it's the right thing to pin.
    from pathlib import Path
    src = Path(__file__).parent.parent / "src" / "cdumm" / "worker_process.py"
    text = src.read_text(encoding="utf-8")
    # Find the import "done" emit (line ~77 in current source).
    # Use a coarse marker — the literal "asi_staged" appears in
    # the import-done emit and nowhere else.
    assert "asi_staged" in text, (
        "Could not locate the import 'done' emit — landmark "
        "'asi_staged' missing.")
    # Slice out a window around that landmark (300 chars before
    # and 100 after — should cover the whole emit dict).
    idx = text.index("asi_staged")
    window = text[max(0, idx - 400):idx + 200]
    assert '"info"' in window, (
        f"The import 'done' emit must include result.info so the "
        f"GUI can surface partial-skip warnings. Window:\n{window}")
