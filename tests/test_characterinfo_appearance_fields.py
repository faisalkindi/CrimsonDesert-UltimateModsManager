"""CD current-DMM characterinfo field names — Character Creator 7.6 (#302).

The Character Creator / Female Animations 7.6 mod (Nexus 837) ships a
``.field.json`` (Format 3) that patches ``characterinfo.pabgb`` using the
current DMM Mod Builder's *semantic* field names — ``appearance_name``,
``character_prefab_path``, ``skeleton_name``, ``lookup_24``, ``lookup_25``
(plus three post-block fields). Nine of its twenty-five intents were being
"applied" under the legacy naming, but to the WRONG block offsets, and the
rest were skipped — so the mod didn't take, and the author reverted to raw
offset-patching.

DMM renamed the action-chart slots between versions, so the same field
names resolve to different block deltas than the legacy #150/#192 set. The
mapping here is pinned against the live table, not guessed: the mod copies
the **Damian** record's animation setup onto Kliff, so Damian holds each
target value at its real slot. Verified on the live 1.13/1.14 table:

    appearance_name       block + 0
    character_prefab_path  block + 4
    skeleton_name          block + 8
    lookup_24              block + 20
    lookup_25              block + 24

``block + 16`` is a table-wide constant (3938836851 across all 7105
records) — a type tag — which is exactly where the legacy ``lookup_24``
mapping pointed, so that legacy mapping wrote to a constant. The new schema
routes ``lookup_24`` to its real slot (+20).

The fixture is a 6-record slice of the real table: the mod's five targets
(Kliff, Kliff_Clone, Kliff_AI, Yann, PlayerAll) plus Damian as the source /
oracle. Because the mod copies Damian, "Kliff's block now equals Damian's
block" is the byte-exact proof that every offset is right.
"""
from __future__ import annotations

import struct

import pytest

from tests.fixture_loaders import load_vanilla113
from cdumm.engine.characterinfo_writer import (
    build_characterinfo_changes, SUPPORTED_FIELDS,
)
from cdumm.archive.format_parsers.characterinfo_full_parser import (
    parse_pabgh_index, parse_entry,
)

# The real 7.6 mod's intents: (entry_name, key, field, new_value).
_HASH = 0  # marker for readability only
_MOD_INTENTS: list[tuple[str, int, str, int]] = [
    ("Kliff", 1, "appearance_name", 1767116530),
    ("Kliff", 1, "character_prefab_path", 3755051597),
    ("Kliff", 1, "default_action_action_index", 1287066785),  # post-block
    ("Kliff", 1, "f36", 2),                                    # post-block
    ("Kliff", 1, "lookup_24", 2831867940),
    ("Kliff", 1, "lookup_25", 3511542393),
    ("Kliff", 1, "skeleton_name", 3000129643),
    ("Kliff_Clone", 1001367, "appearance_name", 1767116530),
    ("Kliff_Clone", 1001367, "character_prefab_path", 3755051597),
    ("Kliff_Clone", 1001367, "character_weight", 1287066785),  # post-block
    ("Kliff_Clone", 1001367, "f36", 2),                        # post-block
    ("Kliff_Clone", 1001367, "lookup_24", 2831867940),
    ("Kliff_Clone", 1001367, "lookup_25", 3511542393),
    ("Kliff_AI", 1002113, "appearance_name", 1767116530),
    ("Kliff_AI", 1002113, "character_prefab_path", 3755051597),
    ("Kliff_AI", 1002113, "default_action_action_index", 1287066785),
    ("Kliff_AI", 1002113, "f36", 2),                           # post-block
    ("Kliff_AI", 1002113, "lookup_24", 2831867940),
    ("Kliff_AI", 1002113, "lookup_25", 3511542393),
    ("Yann", 1004085, "appearance_name", 1767116530),
    ("Yann", 1004085, "character_prefab_path", 3755051597),
    ("Yann", 1004085, "lookup_24", 2831867940),
    ("PlayerAll", 100, "default_action_action_index", 1287066785),
    ("PlayerAll", 100, "f36", 2),                              # post-block
    ("PlayerAll", 100, "lookup_25", 3511542393),
]

# The five hash-block fields that CAN be located, and their block deltas.
_NEW = {"appearance_name": 0, "character_prefab_path": 4,
        "skeleton_name": 8, "lookup_24": 20, "lookup_25": 24}
# The three the 7.6 mod also sets that sit in the post-block variable-length
# region (1.13 drift) and are deliberately NOT written.
_POST_BLOCK = {"default_action_action_index", "character_weight", "f36"}

_DAMIAN_KEY = 4
_TYPE_TAG_CONST = 3938836851  # block+16, invariant across the whole table


@pytest.fixture(scope="module")
def table() -> tuple[bytes, bytes]:
    return (load_vanilla113("characterinfo.pabgb"),
            load_vanilla113("characterinfo.pabgh"))


def _block_offset(body: bytes, header: bytes, key: int) -> int:
    idx = parse_pabgh_index(header)
    order = sorted(idx.items(), key=lambda kv: kv[1])
    offs = [o for _, o in order]
    o = idx[key]
    i = offs.index(o)
    end = order[i + 1][1] if i + 1 < len(order) else len(body)
    return parse_entry(body, o, end)["_upperActionChartPackageGroupName_offset"]


def _apply(body: bytes, changes: list[dict]) -> bytes:
    work = bytearray(body)
    for c in changes:
        off = c["offset"]
        orig = bytes.fromhex(c["original"])
        patched = bytes.fromhex(c["patched"])
        assert work[off:off + len(orig)] == orig, "original byte mismatch"
        work[off:off + len(patched)] = patched
    return bytes(work)


# ── the mod applies ─────────────────────────────────────────────────────

def test_all_17_hash_block_intents_apply_and_match_the_source(table):
    """THE test: the 17 locatable intents apply, and Kliff (the one record
    that sets all five) ends up byte-identical to Damian on every hash-block
    field — because the mod copies Damian, this is the byte-exact proof the
    offsets are right, not merely that a write happened."""
    body, header = table
    changes = build_characterinfo_changes(body, header, _MOD_INTENTS)
    assert len(changes) == 17, (
        f"expected 17 locatable intents, got {len(changes)}")

    patched = _apply(body, changes)
    assert len(patched) == len(body), "writes must not resize the table"

    dblk = _block_offset(body, header, _DAMIAN_KEY)
    kblk = _block_offset(patched, header, 1)
    for field, delta in _NEW.items():
        got = struct.unpack_from("<I", patched, kblk + delta)[0]
        want = struct.unpack_from("<I", body, dblk + delta)[0]
        assert got == want, (
            f"Kliff.{field} (block+{delta}) = {got}, Damian = {want}")


def test_every_write_lands_its_intent_value(table):
    """Each of the 17 writes puts its exact ``new`` value at its slot,
    across all five target records (not just Kliff)."""
    body, header = table
    key_of = {n: k for n, k, _f, _v in _MOD_INTENTS}
    patched = _apply(body, build_characterinfo_changes(
        body, header, _MOD_INTENTS))
    for name, key, field, value in _MOD_INTENTS:
        if field not in _NEW:
            continue
        blk = _block_offset(patched, header, key_of[name])
        got = struct.unpack_from("<I", patched, blk + _NEW[field])[0]
        assert got == value, f"{name}.{field} = {got}, wanted {value}"


# ── the type-tag constant is never touched ──────────────────────────────

def test_lookup_24_writes_its_real_slot_not_the_type_tag_constant(table):
    """The legacy map put lookup_24 at block+16, which is a table-wide
    constant type tag. The new schema must route it to +20, and +16 must be
    left at the constant on every targeted record."""
    body, header = table
    patched = _apply(body, build_characterinfo_changes(
        body, header, _MOD_INTENTS))
    for key in (1, 1001367, 1002113, 1004085):
        blk = _block_offset(patched, header, key)
        assert struct.unpack_from("<I", patched, blk + 16)[0] == \
            _TYPE_TAG_CONST, "block+16 type tag was overwritten"
        assert struct.unpack_from("<I", patched, blk + 20)[0] == \
            2831867940, "lookup_24 did not land at its real slot (+20)"


# ── the deferred fields are skipped, not guessed ────────────────────────

def test_post_block_fields_are_skipped_not_written(table):
    """default_action_action_index / character_weight / f36 sit in the
    1.13 variable-length post-block region and cannot be fix-located, so
    they are reported unsupported rather than written to a guess."""
    body, header = table
    changes = build_characterinfo_changes(body, header, _MOD_INTENTS)
    written = {c["label"].split(".", 1)[1] for c in changes}
    assert not (_POST_BLOCK & written), (
        f"a post-block field was written to a guessed offset: "
        f"{_POST_BLOCK & written}")


# ── the discriminator, and legacy mods are untouched ────────────────────

def test_markers_select_the_new_layout(table):
    """With a new-schema marker present, skeleton_name resolves to +8."""
    body, header = table
    changes = build_characterinfo_changes(body, header, [
        ("Kliff", 1, "appearance_name", 1767116530),  # marker
        ("Kliff", 1, "skeleton_name", 3000129643),
    ])
    blk = _block_offset(body, header, 1)
    off = {c["label"]: c["offset"] for c in changes}
    assert off["Kliff.skeleton_name"] - blk == 8, (
        "with a new-schema marker, skeleton_name must resolve to block+8")


def test_legacy_mod_keeps_the_old_offsets(table):
    """A legacy intent set (no new-schema markers) must still resolve
    skeleton_name to its old slot (+20) and lookup_24 to +16 — changing
    those would silently corrupt mods already in the wild."""
    body, header = table
    changes = build_characterinfo_changes(body, header, [
        ("Kliff", 1, "skeleton_name", 123),
        ("Kliff", 1, "lookup_24", 456),
    ])
    blk = _block_offset(body, header, 1)
    off = {c["label"]: c["offset"] - blk for c in changes}
    assert off["Kliff.skeleton_name"] == 20, "legacy skeleton_name moved"
    assert off["Kliff.lookup_24"] == 16, "legacy lookup_24 moved"


def test_new_markers_are_in_the_validation_accept_set():
    """format3_handler gates characterinfo intents on the writer's
    SUPPORTED_FIELDS; the two new markers must be in it or the writer never
    sees them (the same accept/write drift the #150 comment warned about)."""
    assert "appearance_name" in SUPPORTED_FIELDS
    assert "character_prefab_path" in SUPPORTED_FIELDS


# ── no collateral damage ────────────────────────────────────────────────

def test_source_and_untargeted_records_are_byte_identical(table):
    """Only the five targeted records' hash blocks change; the Damian
    source record and every other byte are untouched."""
    body, header = table
    patched = _apply(body, build_characterinfo_changes(
        body, header, _MOD_INTENTS))
    idx = parse_pabgh_index(header)
    order = sorted(idx.items(), key=lambda kv: kv[1])
    offs = [o for _, o in order]
    targets = {1, 1001367, 1002113, 1004085, 100}
    for key, o in order:
        if key in targets:
            continue
        i = offs.index(o)
        end = order[i + 1][1] if i + 1 < len(order) else len(body)
        assert patched[o:end] == body[o:end], (
            f"non-target record key={key} changed")
