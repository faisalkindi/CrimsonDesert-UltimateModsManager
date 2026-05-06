"""Faisal report 2026-05-05: 'I updated mod #25 and it did update,
when I switched page and came back, it created a duplicate mod #28'.

Trace from cdumm.log:
  22:12:07 nxm: explicit-intent bind to mod_id=1554
  22:12:07 Queued for import: NoCooldownForALLItemsV2_field.zip ...
           replacing mod_id=1554
  22:12:07 subprocess args=[..., '1554']  (existing_mod_id=1554 OK)
  22:12:09 Stored Nexus real file_id=7541 on row 1564  (NEW row!)

So the GUI bound the click correctly to mod_id=1554, the subprocess
got existing_mod_id=1554 in argv, but the import created mod_id=1564
anyway. Result: original 1554 + new 1564 = duplicate cards.

Root cause: ``import_from_zip`` (line 2561) and
``import_from_folder`` (line 2995) both call
``import_from_natt_format_3`` WITHOUT the ``existing_mod_id``
keyword argument. The Format 3 importer then INSERTs a new row
instead of UPDATEing the original.

Fix: pass ``existing_mod_id=existing_mod_id`` at both call sites
(matching what ``import_from_7z`` does at line 2184).
"""
from __future__ import annotations

import inspect
from cdumm.engine import import_handler


def _calls_to(func_name: str, source: str) -> list[str]:
    """Return each call to func_name in source as its own string,
    captured up to the closing paren on the same call (assumes
    well-formatted code)."""
    out = []
    pos = 0
    while True:
        idx = source.find(func_name + "(", pos)
        if idx < 0:
            return out
        # Find the matching close paren.
        depth = 0
        end = idx
        while end < len(source):
            if source[end] == "(":
                depth += 1
            elif source[end] == ")":
                depth -= 1
                if depth == 0:
                    end += 1
                    break
            end += 1
        out.append(source[idx:end])
        pos = end


def test_all_callers_of_import_from_natt_format_3_pass_existing_mod_id():
    """Every call to import_from_natt_format_3 inside import_handler.py
    that lives in a function accepting existing_mod_id must forward
    that parameter. Otherwise the Click-to-Update path on a Format 3
    mod creates a duplicate row instead of updating the original."""
    src = inspect.getsource(import_handler)
    calls = _calls_to("import_from_natt_format_3", src)
    # First match is the function definition itself (`def
    # import_from_natt_format_3(...)`) — drop it.
    calls = [c for c in calls if not c.startswith("import_from_natt_format_3(")
             or "json_path" in c]
    assert len(calls) >= 3, (
        f"expected at least 3 callers, got {len(calls)}: {calls}")
    bad = [c for c in calls if "existing_mod_id" not in c]
    assert not bad, (
        "the following call sites drop existing_mod_id, which causes "
        "Click-to-Update on Format 3 mods to create duplicates "
        "instead of updating the original row:\n\n"
        + "\n\n".join(bad))
