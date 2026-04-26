"""Diagnostic dialog must recognize Format 3 mods.

When a user drops a Format 3 mod that fails to apply (e.g. kori228's
issue #41 attachment), the Inspect Mod / diagnostic dialog appends a
'Format Detection' section. v3.2.1 didn't know about Format 3, so it
labeled valid Format 3 zips as 'No recognized mod format detected' —
which contradicted the import error directly above it and confused
users into thinking the mod was malformed when it was actually
working as intended (no schema yet).
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path


def test_diagnostic_recognizes_format3_zip(tmp_path: Path) -> None:
    z = tmp_path / "f3.zip"
    inner = {
        "modinfo": {"title": "F3Test"},
        "format": 3,
        "target": "dropsetinfo.pabgb",
        "intents": [
            {"entry": "X", "key": 1, "field": "drops",
             "op": "set", "new": []}
        ],
    }
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("inner.json", json.dumps(inner))

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    db_path = tmp_path / "test.db"

    from cdumm.engine.mod_diagnostics import diagnose_mod
    report = diagnose_mod(z, game_dir, db_path)

    assert "Format 3" in report or "natt" in report.lower(), (
        f"Diagnostic did not detect Format 3:\n{report}")
    assert "No recognized mod format detected" not in report, (
        f"Diagnostic still says 'No recognized mod format' for "
        f"a valid Format 3 zip:\n{report}")
    # Sanity: the helpful pointer to field_schema/README should appear
    assert "field_schema" in report.lower(), (
        f"Diagnostic missing field_schema pointer:\n{report}")
