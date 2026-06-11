"""expand_format3_into_aggregated must never raise (audit finding 11).

The docstring promises "Never raises", but unguarded struct.unpack_from
(and friends) inside the per-target processing could blow out of the
expansion and abort the entire apply. Both per-target loop bodies are
now wrapped: a crash logs, appends a warning naming the target and the
contributing mod(s), and processing continues.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import cdumm.engine.format3_apply as f3a
from cdumm.storage.database import Database


def _write_format3(tmp_path: Path, name: str, target: str,
                   intents: list[dict]) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps({
        "format": 3,
        "target": target,
        "intents": intents,
    }), encoding="utf-8")
    return p


def _db_with_mod(tmp_path: Path, json_source: Path,
                 mod_name: str = "CrashMod") -> Database:
    db = Database(tmp_path / "t.db")
    db.initialize()
    db.connection.execute(
        "INSERT INTO mods (name, mod_type, enabled, priority, "
        "json_source) VALUES (?, 'paz', 1, 1, ?)",
        (mod_name, str(json_source)))
    db.connection.commit()
    return db


def test_per_mod_target_crash_is_contained(tmp_path, monkeypatch):
    src = _write_format3(tmp_path, "mod.field.json", "dropsetinfo.pabgb",
                         [{"entry": "DropSet_X", "key": 1,
                           "field": "drops", "new": []}])
    db = _db_with_mod(tmp_path, src)
    try:
        def _boom(target, intents):
            raise RuntimeError("synthetic validate crash")
        monkeypatch.setattr(f3a, "validate_intents", _boom)

        aggregated: dict = {}
        warnings: list[str] = []
        # Must NOT raise.
        f3a.expand_format3_into_aggregated(
            aggregated, {}, db,
            vanilla_extractor=lambda t: (b"\x00" * 8, b"\x00" * 2),
            warnings_out=warnings)
        assert aggregated == {}
        assert any("CrashMod" in w and "dropsetinfo.pabgb" in w
                   for w in warnings)
    finally:
        db.close()


def test_whole_table_dispatch_crash_is_contained(tmp_path, monkeypatch):
    src = _write_format3(tmp_path, "mod.field.json", "skill.pabgb",
                         [{"entry": "Skill_X", "key": 7,
                           "field": "_useResourceStatList",
                           "new": [{"v": 1}]}])
    db = _db_with_mod(tmp_path, src, mod_name="SkillCrashMod")
    try:
        def _boom(*args, **kwargs):
            raise RuntimeError("synthetic expansion crash")
        monkeypatch.setattr(f3a, "_intents_to_v2_changes", _boom)

        aggregated: dict = {}
        warnings: list[str] = []
        # Must NOT raise even though the whole-table dispatch crashes.
        f3a.expand_format3_into_aggregated(
            aggregated, {}, db,
            vanilla_extractor=lambda t: (b"\x00" * 8, b"\x00" * 2),
            warnings_out=warnings)
        assert "skill.pabgb" not in aggregated
        assert any("skill.pabgb" in w and "SkillCrashMod" in w
                   for w in warnings)
    finally:
        db.close()
