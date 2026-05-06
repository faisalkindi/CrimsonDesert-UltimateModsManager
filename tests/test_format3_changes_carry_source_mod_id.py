"""Bug found via /systematic-debugging on the just-shipped skip-tracking
feature: Format 3 mods that produce byte-mismatch skips never get a
yellow SKIPPED badge.

Why: chunk 1 of skip-tracking taught the v2 aggregator to tag every
emitted change with ``_source_mod_id`` so the apply pipeline's
``_record_skip`` can attribute byte-mismatch failures back to the
mod that owns them. That tag is what ``persist_skip_summary`` keys
on when it writes ``last_apply_skipped_count``.

But Format 3 mods take a different code path:
``format3_apply.expand_format3_into_aggregated`` extends the same
``aggregated[target]`` list with un-tagged change dicts. When their
patches mismatch on Apply, the skip lands in ``patch_skips`` with no
``_source_mod_id`` , ``persist_skip_summary`` skips it entirely ,
the badge never lights up. Silent failure on the whole Format 3
ecosystem.

Fix: tag every Format 3-emitted change with ``_source_mod_id`` in
the per-mod loop in ``expand_format3_into_aggregated`` , the per-mod
loop already has the mod_id in scope.
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


@pytest.fixture
def synth_schema(monkeypatch):
    fields = [
        FieldSpec(name="_alpha", stream_size=4,
                  field_type="direct_u32", struct_fmt="I"),
    ]
    schema = TableSchema(table_name="pipetest", fields=fields)
    parser_mod._load_schemas()
    cache = dict(parser_mod._loaded_schemas or {})
    cache["pipetest"] = schema
    monkeypatch.setattr(parser_mod, "_loaded_schemas", cache)
    yield schema


def _build_pabgb(entry_id: int, name: str, alpha: int) -> tuple[bytes, bytes]:
    name_b = name.encode("utf-8")
    head = struct.pack("<II", entry_id, len(name_b))
    payload = struct.pack("<I", alpha)
    body = head + name_b + b"\x00" + payload
    header = struct.pack("<H", 1) + struct.pack("<II", entry_id, 0)
    return body, header


def _write_format3(tmp_path: Path, target: str, intents: list[dict]) -> Path:
    p = tmp_path / "mod.json"
    p.write_text(json.dumps({
        "format": 3, "target": target, "intents": intents,
    }), encoding="utf-8")
    return p


def test_format3_changes_carry_source_mod_id(synth_schema, tmp_path):
    """Every change appended by expand_format3_into_aggregated must
    carry ``_source_mod_id`` set to the contributing mod's row id, so
    that downstream byte-mismatch skips can be attributed back to it
    and surfaced via the yellow SKIPPED badge."""
    body, header = _build_pabgb(1, "X", 0x11111111)
    json_path = _write_format3(tmp_path, "pipetest.pabgb", [
        {"entry": "X", "key": 1, "field": "_alpha",
         "op": "set", "new": 0xCAFEBABE}
    ])
    db = _DBWrap(_make_db([
        (42, "Format3Mod", 1, str(json_path), 5),
    ]))

    aggregated: dict = {}
    signatures: dict = {}
    expand_format3_into_aggregated(
        aggregated, signatures, db,
        vanilla_extractor=lambda gf: (body, header),
    )

    changes = aggregated.get("pipetest.pabgb", [])
    assert changes, "Format 3 expansion should emit at least one change"
    for c in changes:
        assert c.get("_source_mod_id") == 42, (
            f"Format 3 change is missing _source_mod_id={42!r}. Without "
            f"this tag, byte-mismatch skips on Apply land in "
            f"patch_skips with no mod attribution , persist_skip_summary "
            f"drops them , Format 3 mods never get a yellow SKIPPED "
            f"badge. Got: {c!r}"
        )
