"""Bug found via /systematic-debugging continuation: the Format 3
whole-table writer dispatch (iteminfo.pabgb / skill.pabgb) emits one
merged change per target containing intents from every contributing
mod. Bug B taught the per-mod loop to stamp ``_source_mod_id`` and
Bug D taught it to stamp ``_target_file`` , but the whole-table
branch ran a parallel loop and was never updated. Its emitted change
goes into the aggregator un-tagged.

Per-mod attribution for whole-table is genuinely hard (one byte
change represents N mods' merged intents). That's a deeper schema
problem and stays deferred. But ``_target_file`` is trivially
correct , the loop already has ``target`` in scope and every
emitted change goes to that one file. Stamping it lets the badge
tooltip name the asset whenever future attribution work lands.
"""
from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path

import pytest

from cdumm.engine.format3_apply import expand_format3_into_aggregated
from cdumm.semantic import parser as parser_mod
from cdumm.semantic.parser import FieldSpec, TableSchema


def _make_db(rows: list[tuple]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE mods ("
        "id INTEGER PRIMARY KEY, name TEXT, enabled INTEGER,"
        " json_source TEXT, priority INTEGER, mod_type TEXT)"
    )
    conn.execute(
        "CREATE TABLE mod_config (mod_id INTEGER, custom_values TEXT)"
    )
    for r in rows:
        conn.execute(
            "INSERT INTO mods (id, name, enabled, json_source, "
            "priority, mod_type) VALUES (?, ?, ?, ?, ?, 'paz')",
            r,
        )
    conn.commit()
    return conn


class _DBWrap:
    def __init__(self, conn):
        self.connection = conn


def test_whole_table_change_carries_target_file(tmp_path, monkeypatch):
    """When a Format 3 mod targets iteminfo.pabgb (a whole-table
    writer target), the change emitted by the post-loop dispatch
    must carry ``_target_file = "iteminfo.pabgb"``. Without this,
    the badge tooltip's file column stays blank for every
    whole-table apply failure."""
    # Stub the dispatch so we don't need a real iteminfo.pabgb fixture.
    # The test only cares about whether _target_file is stamped on
    # changes appended to aggregated[target] from the whole-table
    # branch , not whether the writer emits correct bytes.
    from cdumm.engine import format3_apply as fa

    fake_change = {
        "entry": "merged",
        "rel_offset": 0,
        "original": "00",
        "patched": "01",
        "label": "whole_table_merged",
    }

    def fake_intents_to_v2_changes(target, body, header, intents):
        return [dict(fake_change)]  # always succeed with one merged change

    monkeypatch.setattr(fa, "_intents_to_v2_changes",
                        fake_intents_to_v2_changes)

    # Force the target into the whole-table set
    monkeypatch.setattr(fa, "_WHOLE_TABLE_TARGETS",
                        {"iteminfo.pabgb"}, raising=False)

    # We still need parse_format3_mod_targets + validate_intents to
    # return something. Stub both. The plural shape is now the
    # canonical entry the apply path uses.
    fake_intent_obj = type("I", (), {"key": 1})()
    monkeypatch.setattr(fa, "parse_format3_mod_targets",
                        lambda p: [("iteminfo.pabgb", [fake_intent_obj])])

    class FakeValidation:
        supported = [fake_intent_obj]
        skipped: list = []

    monkeypatch.setattr(fa, "validate_intents",
                        lambda t, ints: FakeValidation())

    # Minimal mod row so the SELECT returns one
    json_path = tmp_path / "wholeMod.json"
    json_path.write_text("{}", encoding="utf-8")
    db = _DBWrap(_make_db([
        (7, "WholeTableMod", 1, str(json_path), 5),
    ]))

    aggregated: dict = {}
    signatures: dict = {}
    expand_format3_into_aggregated(
        aggregated, signatures, db,
        vanilla_extractor=lambda gf: (b"\x00" * 16, b"\x00" * 16),
    )

    # The whole-table branch must have stamped _target_file on the
    # merged change.
    changes = aggregated.get("iteminfo.pabgb") or []
    assert changes, (
        "Whole-table dispatch should have emitted a merged change for "
        "iteminfo.pabgb. Got nothing in aggregated."
    )
    for c in changes:
        assert c.get("_target_file") == "iteminfo.pabgb", (
            f"Whole-table change must carry _target_file='iteminfo.pabgb' "
            f"so byte-mismatch skips reach the badge tooltip with the "
            f"asset name attached. Got: {c!r}"
        )
