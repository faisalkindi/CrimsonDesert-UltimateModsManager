"""GitHub #66 follow-up + Faisal 2026-05-04 ZIP review: the newer
Field-JSON dialect ships *multiple* targets per file:

    {
      "format": 3,
      "modinfo": {...},
      "targets": [
        { "file": "buffinfo.pabgb", "intents": [...] },
        { "file": "iteminfo.pabgb", "intents": [...] }
      ]
    }

The original spec (FIELD_JSON_V3_SPEC.md) only documented the
singular ``target: <str>`` + top-level
``intents`` shape. The new ``targets: [{file, intents}]`` shape is a
forward-compat extension. CDUMM's ``parse_format3_mod`` was hard-
coded to the singular form and bombed at import with
"missing target string" on every multi-target mod, even though the
intents themselves are otherwise the same shape.

This test pins ``parse_format3_mod_targets`` , a new entry point
that returns a list of (target_file, intents) pairs and accepts
both the singular and the plural top-level shape. The existing
``parse_format3_mod`` becomes a thin wrapper that only succeeds on
single-target files (so its callers don't silently lose intents).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fixture_loaders import real_mod_fixture


def _write(p: Path, body: dict) -> Path:
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


def test_parse_targets_handles_singular_shape(tmp_path):
    """The legacy single-target shape must produce a 1-entry list
    so callers can iterate uniformly."""
    from cdumm.engine.format3_handler import parse_format3_mod_targets

    p = _write(tmp_path / "single.field.json", {
        "format": 3,
        "target": "skill.pabgb",
        "intents": [
            {"entry": "x", "key": 1, "field": "_y", "op": "set", "new": 1},
        ],
    })
    pairs = parse_format3_mod_targets(p)
    assert isinstance(pairs, list)
    assert len(pairs) == 1
    target, intents = pairs[0]
    assert target == "skill.pabgb"
    assert len(intents) == 1
    assert intents[0].field == "_y"


def test_parse_targets_handles_plural_shape(tmp_path):
    """The new multi-target shape must produce one pair per target,
    preserving the per-target intent list."""
    from cdumm.engine.format3_handler import parse_format3_mod_targets

    p = _write(tmp_path / "multi.field.json", {
        "format": 3,
        "modinfo": {"title": "Multi", "version": "1"},
        "targets": [
            {
                "file": "buffinfo.pabgb",
                "intents": [
                    {"entry": "B1", "key": 100, "field": "f1", "new": 1},
                    {"entry": "B2", "key": 101, "field": "f2", "new": 2},
                ],
            },
            {
                "file": "iteminfo.pabgb",
                "intents": [
                    {"entry": "I1", "key": 200, "field": "g1", "new": 3},
                ],
            },
        ],
    })
    pairs = parse_format3_mod_targets(p)
    assert len(pairs) == 2
    assert pairs[0][0] == "buffinfo.pabgb"
    assert len(pairs[0][1]) == 2
    assert [it.entry for it in pairs[0][1]] == ["B1", "B2"]
    assert pairs[1][0] == "iteminfo.pabgb"
    assert len(pairs[1][1]) == 1
    assert pairs[1][1][0].entry == "I1"


def test_parse_targets_rejects_empty_targets_list(tmp_path):
    p = _write(tmp_path / "empty.field.json", {
        "format": 3,
        "targets": [],
    })
    from cdumm.engine.format3_handler import parse_format3_mod_targets
    with pytest.raises(ValueError, match="empty|at least one"):
        parse_format3_mod_targets(p)


def test_parse_targets_rejects_target_entry_missing_file(tmp_path):
    p = _write(tmp_path / "bad.field.json", {
        "format": 3,
        "targets": [
            {"intents": [{"entry": "x", "key": 1, "field": "y", "new": 0}]},
        ],
    })
    from cdumm.engine.format3_handler import parse_format3_mod_targets
    with pytest.raises(ValueError, match=r"target.*\bfile\b|missing.*\bfile\b"):
        parse_format3_mod_targets(p)


def test_parse_targets_rejects_target_entry_with_no_intents(tmp_path):
    p = _write(tmp_path / "noints.field.json", {
        "format": 3,
        "targets": [
            {"file": "x.pabgb"},  # no intents
        ],
    })
    from cdumm.engine.format3_handler import parse_format3_mod_targets
    with pytest.raises(ValueError, match="intents"):
        parse_format3_mod_targets(p)


def test_legacy_parse_format3_mod_still_works_on_singular(tmp_path):
    """Backwards-compat: old call sites that haven't been migrated
    yet keep working on legacy single-target files."""
    from cdumm.engine.format3_handler import parse_format3_mod

    p = _write(tmp_path / "single.field.json", {
        "format": 3,
        "target": "skill.pabgb",
        "intents": [
            {"entry": "x", "key": 1, "field": "_y", "op": "set", "new": 1},
        ],
    })
    target, intents = parse_format3_mod(p)
    assert target == "skill.pabgb"
    assert len(intents) == 1


def test_legacy_parse_format3_mod_rejects_multi_target_explicitly(tmp_path):
    """When a multi-target file hits a caller that hasn't been
    migrated, raising loudly is safer than silently dropping every
    intent past the first target. The error message must point the
    caller at parse_format3_mod_targets."""
    from cdumm.engine.format3_handler import parse_format3_mod

    p = _write(tmp_path / "multi.field.json", {
        "format": 3,
        "targets": [
            {"file": "a.pabgb", "intents": [{"entry": "x", "key": 1, "field": "y", "new": 0}]},
            {"file": "b.pabgb", "intents": [{"entry": "x", "key": 1, "field": "y", "new": 0}]},
        ],
    })
    with pytest.raises(ValueError, match="parse_format3_mod_targets|multi-target"):
        parse_format3_mod(p)


def test_importer_handles_multi_target_mod(tmp_path, monkeypatch):
    """Importing a multi-target Format 3 mod must NOT raise
    'missing target string'. The mod row gets created, and one
    mod_deltas row per target so conflict detection sees every
    file the mod touches."""
    from unittest.mock import MagicMock

    from cdumm.storage.database import Database

    db = Database(tmp_path / "test.db")
    db.initialize()
    p = tmp_path / "multi.field.json"
    p.write_text(json.dumps({
        "format": 3,
        "modinfo": {"title": "MultiMod", "version": "1.0"},
        "targets": [
            {
                "file": "dropsetinfo.pabgb",
                "intents": [
                    {"entry": "X", "key": 1, "field": "_dropTagNameHash",
                     "op": "set", "new": 9876},
                ],
            },
            {
                "file": "iteminfo.pabgb",
                "intents": [
                    {"entry": "Y", "key": 2, "field": "enchant_data_list",
                     "op": "set", "new": []},
                ],
            },
        ],
    }), encoding="utf-8")

    fake_entry_drop = MagicMock()
    fake_entry_drop.paz_file = "/fake/0008/0.paz"
    fake_entry_drop.offset = 100
    fake_entry_drop.comp_size = 50
    fake_entry_item = MagicMock()
    fake_entry_item.paz_file = "/fake/0008/0.paz"
    fake_entry_item.offset = 200
    fake_entry_item.comp_size = 50

    def _fake_find(game_file, _game_dir):
        if "dropset" in game_file:
            return fake_entry_drop
        if "iteminfo" in game_file:
            return fake_entry_item
        return None

    monkeypatch.setattr(
        "cdumm.engine.json_patch_handler._find_pamt_entry",
        _fake_find)

    from cdumm.engine.import_handler import import_from_natt_format_3
    result = import_from_natt_format_3(
        json_path=p, game_dir=tmp_path, db=db,
        snapshot=MagicMock(), deltas_dir=tmp_path)

    assert result.error is None or result.error == "", (
        f"multi-target import should not error, got {result.error!r}")

    rows = db.connection.execute(
        "SELECT entry_path FROM mod_deltas"
    ).fetchall()
    entry_paths = sorted(r[0] for r in rows)
    assert "dropsetinfo.pabgb" in entry_paths, (
        f"first target dropsetinfo.pabgb missing from mod_deltas: "
        f"{entry_paths!r}")
    assert "iteminfo.pabgb" in entry_paths, (
        f"second target iteminfo.pabgb missing from mod_deltas , "
        f"conflict detection won't see the second file: {entry_paths!r}")
    db.close()


def test_real_world_double_resource_buff_zip_imports(tmp_path):
    """End-to-end against the real Adfaz mod that triggered this
    work (Faisal's zip review on 2026-05-04). The mod has 1 target
    (buffinfo.pabgb) with 4185 intents. Parsing must succeed even
    though apply will downstream-skip every intent (nested-indexed
    fields are deferred)."""
    import zipfile
    zp = real_mod_fixture("Double Resource Buff Effect - Field JSON-2276-1-1777879568.zip")
    if not zp.exists():
        pytest.skip(f"Reference zip not present at {zp}")

    out = tmp_path / "mod.field.json"
    with zipfile.ZipFile(zp) as z:
        with z.open("Double_Resource_Buff_Effect_Fieldjson.json") as src:
            out.write_bytes(src.read())

    from cdumm.engine.format3_handler import parse_format3_mod_targets
    pairs = parse_format3_mod_targets(out)
    assert len(pairs) == 1
    target, intents = pairs[0]
    assert target == "buffinfo.pabgb"
    assert len(intents) == 4185, (
        f"Expected the full 4185-intent payload to round-trip, "
        f"got {len(intents)} intents")
    # The mod's first intent: ``buff_data_list[0].absent_flag`` set to 0.
    first = intents[0]
    assert first.field == "buff_data_list[0].absent_flag"
    assert first.entry == "BuffLevel_Socket_ContributionExp"
    assert first.op == "set"
