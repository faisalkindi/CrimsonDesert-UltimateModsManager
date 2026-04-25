"""import_from_natt_format_3 wires Format3 validator into the
import-handler stage so the user-facing message reflects what the
mod actually looks like, instead of a canned 'coming in future'.

Three buckets the wiring needs to handle:
  - malformed file → loader's ValueError message verbatim
  - all intents unapplicable (e.g., kori228's drops-array mod) →
    validator summary + workaround pointer
  - mixed supported / skipped → both counts + summary
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cdumm.engine.import_handler import import_from_natt_format_3


FIXTURE = Path(__file__).parent / "fixtures" / "format3" \
    / "dropsetinfo_5x_drops.json"


def _run(json_path, tmp_path):
    """Run the import_handler stub with throwaway stub deps."""
    db = MagicMock()
    snapshot = MagicMock()
    return import_from_natt_format_3(
        json_path=json_path,
        game_dir=tmp_path,
        db=db,
        snapshot=snapshot,
        deltas_dir=tmp_path,
    )


def test_kori228_dropsetinfo_mod_surfaces_skip_summary(tmp_path):
    """The actual user-submitted Format 3 mod from issue #41
    targets dropsetinfo._list (variable-length drops array).
    All 695 intents are unapplicable in current state. The user
    must see the count + reason in the error message — not the
    old canned 'coming in future' text."""
    result = _run(FIXTURE, tmp_path)
    assert result.error
    msg = result.error
    assert "695" in msg or "intent" in msg.lower()
    assert "dropsetinfo" in msg.lower()
    # Old canned text should be gone
    assert "field-names" not in msg or "skipped" in msg.lower()


def test_malformed_json_surfaces_loader_message(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not valid json {[", encoding="utf-8")
    result = _run(p, tmp_path)
    assert result.error
    # Either parser-error or the structural-validation error
    assert ("invalid" in result.error.lower()
            or "format 3" in result.error.lower()
            or "parse" in result.error.lower())


def test_mod_with_no_intents_passes_validation(tmp_path):
    """A Format 3 file with format=3 + target + empty intents
    list is structurally valid. Validator returns 0 supported,
    0 skipped. Importer should produce a non-confusing message
    rather than a misleading error."""
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({
        "modinfo": {"title": "Empty"},
        "format": 3,
        "target": "iteminfo.pabgb",
        "intents": [],
    }), encoding="utf-8")
    result = _run(p, tmp_path)
    assert result.error
    # Should mention the target, not crash
    assert "iteminfo" in result.error.lower()


def test_mod_with_unknown_target_table_skips_all_intents(tmp_path):
    """A Format 3 mod targeting a .pabgb table CDUMM doesn't have
    a schema for must surface a clear 'no schema' message — not
    silently fail or claim partial success."""
    p = tmp_path / "unknown.json"
    p.write_text(json.dumps({
        "modinfo": {"title": "UnknownTbl"},
        "format": 3,
        "target": "totallyfaketable.pabgb",
        "intents": [
            {"entry": "X", "key": 1, "field": "y",
             "op": "set", "new": 42},
        ],
    }), encoding="utf-8")
    result = _run(p, tmp_path)
    assert result.error
    # Validator's reason mentions "schema" — that should surface
    assert "schema" in result.error.lower()
