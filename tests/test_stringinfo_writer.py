"""GitHub #224 (Female Armor Module / lurkser, pinapana, AgentKush):
stringinfo.pabgb Format 3 mods set a variable-length ``_buffer`` string
located by numeric key. The PABGB schema drops the field (stream=None),
so before the stringinfo writer these mods produced "0 byte changes".

Record layout (verified against vanilla build 23831243, all 30,940
records round-trip byte-exact):
  u32 _key, u8 _isBlocked, u32 _stringKey, u32 buffer_len, buffer bytes.

Trust anchor: parse + serialize the extracted vanilla stringinfo.pabgb
must be byte-identical, and a buffer rewrite must (a) change exactly the
targeted record, (b) keep the companion .pabgh offsets consistent, and
(c) be reproducible by replaying the emitted change dicts with the apply
pipeline's cumulative shift.
"""
from __future__ import annotations

import struct

import pytest

from cdumm.engine.stringinfo_writer import (
    _HEADER_LEN,
    apply_stringinfo,
    build_stringinfo_changes,
    parse_pabgh,
)
from tests.fixture_loaders import has_vanilla110, load_vanilla110

_SKIP = not has_vanilla110("stringinfo.pabgb")
_REASON = "vanilla stringinfo fixture not present"


def _bounds(pabgb: bytes, pabgh: bytes) -> dict[int, tuple[int, int]]:
    entries = parse_pabgh(pabgh)
    order = sorted(entries, key=lambda kv: kv[1])
    out = {}
    for rank, (key, start) in enumerate(order):
        end = order[rank + 1][1] if rank + 1 < len(order) else len(pabgb)
        out[key] = (start, end)
    return out


def _buffer_of(pabgb: bytes, start: int, end: int) -> bytes:
    blen = struct.unpack_from("<I", pabgb, start + _HEADER_LEN)[0]
    off = start + _HEADER_LEN + 4
    return pabgb[off:off + blen]


def _replay_changes(body: bytes, changes: list[dict]) -> bytes:
    """Apply absolute-offset replace changes with cumulative shift, the
    way the mount-time apply pipeline does."""
    work = bytearray(body)
    shift = 0
    for c in sorted(changes, key=lambda c: c["offset"]):
        off = c["offset"] + shift
        old = bytes.fromhex(c["original"])
        new = bytes.fromhex(c["patched"])
        assert work[off:off + len(old)] == old, "original bytes mismatch"
        work[off:off + len(old)] = new
        shift += len(new) - len(old)
    return bytes(work)


@pytest.mark.skipif(_SKIP, reason=_REASON)
def test_round_trip_identity():
    body = load_vanilla110("stringinfo.pabgb")
    head = load_vanilla110("stringinfo.pabgh")
    new_body, new_head = apply_stringinfo(body, head, {})
    assert new_body == body
    assert new_head == head


@pytest.mark.skipif(_SKIP, reason=_REASON)
def test_empty_intents_emit_nothing():
    body = load_vanilla110("stringinfo.pabgb")
    head = load_vanilla110("stringinfo.pabgh")
    changes, pabgh_change = build_stringinfo_changes(body, head, [])
    assert changes == []
    assert pabgh_change is None


@pytest.mark.skipif(_SKIP, reason=_REASON)
def test_buffer_rewrite_grows_and_stays_consistent():
    body = load_vanilla110("stringinfo.pabgb")
    head = load_vanilla110("stringinfo.pabgh")
    bounds = _bounds(body, head)

    # Pick a real record (not the last, so growth never hits the tail
    # guard) and set a longer string.
    entries = parse_pabgh(head)
    by_off = sorted(entries, key=lambda kv: kv[1])
    target_key = by_off[10][0]
    new_value = "khione1_cd_phw_00_ub_00_0205_unit_test_marker"

    changes, pabgh_change = build_stringinfo_changes(
        body, head, [("", target_key, "buffer", new_value)])
    assert len(changes) == 1, "exactly the one targeted record changes"
    assert pabgh_change is not None, "record grew, offsets must rebuild"

    # Replaying the emitted change reproduces the writer's own body.
    full_body, full_head = apply_stringinfo(
        body, head, {target_key: new_value.encode("utf-8")})
    replayed = _replay_changes(body, changes)
    assert replayed == full_body
    assert bytes.fromhex(pabgh_change["patched"]) == full_head

    # The targeted record now holds the new string; every other record
    # is byte-identical.
    new_bounds = _bounds(full_body, full_head)
    for key, (vs, ve) in bounds.items():
        ns, ne = new_bounds[key]
        if key == target_key:
            assert _buffer_of(full_body, ns, ne) == new_value.encode("utf-8")
        else:
            assert full_body[ns:ne] == body[vs:ve], (
                f"untargeted record {key} changed")


@pytest.mark.skipif(_SKIP, reason=_REASON)
def test_buffer_rewrite_shrinks():
    body = load_vanilla110("stringinfo.pabgb")
    head = load_vanilla110("stringinfo.pabgh")
    entries = parse_pabgh(head)
    by_off = sorted(entries, key=lambda kv: kv[1])
    target_key = by_off[20][0]

    full_body, full_head = apply_stringinfo(
        body, head, {target_key: b"x"})
    new_bounds = _bounds(full_body, full_head)
    ns, ne = new_bounds[target_key]
    assert _buffer_of(full_body, ns, ne) == b"x"
    # Total file shrank by the buffer-length delta.
    assert len(full_body) < len(body)


@pytest.mark.skipif(_SKIP, reason=_REASON)
def test_unknown_key_dropped_cleanly():
    body = load_vanilla110("stringinfo.pabgb")
    head = load_vanilla110("stringinfo.pabgh")
    changes, pabgh_change = build_stringinfo_changes(
        body, head, [("", 0xDEADBEEF, "buffer", "nope")])
    assert changes == []
    assert pabgh_change is None


@pytest.mark.skipif(_SKIP, reason=_REASON)
def test_non_string_value_dropped():
    body = load_vanilla110("stringinfo.pabgb")
    head = load_vanilla110("stringinfo.pabgh")
    entries = parse_pabgh(head)
    target_key = entries[0][0]
    changes, _ = build_stringinfo_changes(
        body, head, [("", target_key, "buffer", 12345)])
    assert changes == []


# --- end-to-end through the real Format 3 apply pipeline -------------

import json  # noqa: E402
import sqlite3  # noqa: E402


class _DBWrap:
    def __init__(self, conn):
        self.connection = conn


def _make_db(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE mods (id INTEGER PRIMARY KEY, name TEXT, "
        "enabled INTEGER, json_source TEXT, priority INTEGER, "
        "mod_type TEXT)")
    for r in rows:
        conn.execute(
            "INSERT INTO mods (id, name, enabled, json_source, priority, "
            "mod_type) VALUES (?, ?, ?, ?, ?, 'paz')", r)
    conn.commit()
    return conn


@pytest.mark.skipif(_SKIP, reason=_REASON)
def test_format3_stringinfo_mod_applies_end_to_end(tmp_path):
    """A real Format 3.1 mod with a stringinfo buffer intent must,
    through expand_format3_into_aggregated, produce a stringinfo.pabgb
    change plus the companion stringinfo.pabgh offset rebuild."""
    from cdumm.engine.format3_apply import expand_format3_into_aggregated

    body = load_vanilla110("stringinfo.pabgb")
    head = load_vanilla110("stringinfo.pabgh")
    target_key = parse_pabgh(head)[0][0]

    mod = {
        "modinfo": {"title": "stringinfo e2e", "author": "test"},
        "format": 3, "format_minor": 1,
        "targets": [{
            "file": "stringinfo.pabgb",
            "intents": [{
                "entry": "", "key": target_key, "field": "buffer",
                "op": "set", "new": "cdumm_e2e_stringinfo_marker",
            }],
        }],
    }
    json_path = tmp_path / "stringinfoMod.field.json"
    json_path.write_text(json.dumps(mod), encoding="utf-8")
    db = _DBWrap(_make_db([(1, "StringinfoMod", 1, str(json_path), 5)]))

    aggregated: dict = {}
    signatures: dict = {}
    expand_format3_into_aggregated(
        aggregated, signatures, db,
        vanilla_extractor=lambda gf: (body, head)
        if gf == "stringinfo.pabgb" else None,
    )

    pabgb_changes = aggregated.get("stringinfo.pabgb") or []
    pabgh_changes = aggregated.get("stringinfo.pabgh") or []
    assert pabgb_changes, "buffer write must reach the .pabgb aggregator"
    assert pabgh_changes, "record grew, .pabgh offsets must rebuild"
    for c in pabgb_changes:
        assert c.get("_target_file") == "stringinfo.pabgb"
    for c in pabgh_changes:
        assert c.get("_target_file") == "stringinfo.pabgh"

    # The emitted change really writes the new string.
    replayed = _replay_changes(body, pabgb_changes)
    bounds = _bounds(replayed, bytes.fromhex(pabgh_changes[0]["patched"]))
    ns, ne = bounds[target_key]
    assert _buffer_of(replayed, ns, ne) == b"cdumm_e2e_stringinfo_marker"
