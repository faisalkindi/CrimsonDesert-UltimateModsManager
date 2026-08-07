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

from cdumm.archive.format_parsers.characterinfo_full_parser import (
    parse_entry,
    parse_pabgh_index,
)
from cdumm.engine.characterinfo_writer import (
    SUPPORTED_FIELDS,
    build_characterinfo_changes,
)
from tests.fixture_loaders import load_vanilla113

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
# region (1.13 drift). #302 made these writable on records whose position the
# walk can gate-verify; they stay refused on records it cannot.
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
    # 13 hash-block intents + the 6 post-block ones the walker can locate
    # and gate-verify (#302: default_action_action_index / f36 on Kliff,
    # Kliff_AI, PlayerAll).
    #
    # Kliff_Clone contributes NOTHING (GitHub #329). It fails the f32-2.0
    # gate, so its post-block slot can't be placed -- and since that slot IS
    # placeable on Kliff/Kliff_AI/PlayerAll, withholding it only on this
    # record would leave it wearing Damian's appearance while still running
    # its own action index. That record is now abandoned whole rather than
    # written in part, which is why this is 19 and not 23.
    assert len(changes) == 19, (
        f"expected 19 locatable intents, got {len(changes)}")
    assert not [c for c in changes if c["label"].startswith("Kliff_Clone.")], (
        "Kliff_Clone must contribute no changes at all")

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
    """Each hash-block write puts its exact ``new`` value at its slot,
    across every target record that is written at all (not just Kliff).

    Kliff_Clone is excluded because #329 abandons it whole; the test
    immediately below pins that it keeps its vanilla bytes.
    """
    body, header = table
    key_of = {n: k for n, k, _f, _v in _MOD_INTENTS}
    patched = _apply(body, build_characterinfo_changes(
        body, header, _MOD_INTENTS))
    for name, key, field, value in _MOD_INTENTS:
        if field not in _NEW or name == "Kliff_Clone":
            continue
        blk = _block_offset(patched, header, key_of[name])
        got = struct.unpack_from("<I", patched, blk + _NEW[field])[0]
        assert got == value, f"{name}.{field} = {got}, wanted {value}"


# ── the type-tag constant is never touched ──────────────────────────────

def test_lookup_24_writes_its_real_slot_not_the_type_tag_constant(table):
    """The legacy map put lookup_24 at block+16, which is a table-wide
    constant type tag. The new schema must route it to +20, and +16 must be
    left at the constant on every written record.

    Kliff_Clone (1001367) is not in the list: #329 abandons it, so its +20
    correctly still holds vanilla. ``test_abandoned_record_keeps_every_
    vanilla_byte`` covers that record instead.
    """
    body, header = table
    patched = _apply(body, build_characterinfo_changes(
        body, header, _MOD_INTENTS))
    for key in (1, 1002113, 1004085):
        blk = _block_offset(patched, header, key)
        assert struct.unpack_from("<I", patched, blk + 16)[0] == \
            _TYPE_TAG_CONST, "block+16 type tag was overwritten"
        assert struct.unpack_from("<I", patched, blk + 20)[0] == \
            2831867940, "lookup_24 did not land at its real slot (+20)"


# ── #329: a record CDUMM can only half-write stays vanilla ───────────────

def test_abandoned_record_keeps_every_vanilla_byte(table):
    """The #329 crash, pinned on the real table.

    Kliff_Clone asks for six fields. The parser can place four of them
    (the hash-block ones) and cannot place the post-block slot, because
    the record fails the f32-2.0 position gate. Writing the four would
    hand it Damian's appearance, model and skeleton variation while it
    still runs its own action index -- a combination neither vanilla nor
    the mod produces, and the state the reporter's game crashed on.

    So: the whole record must come out byte-identical to vanilla.
    """
    body, header = table
    changes = build_characterinfo_changes(body, header, _MOD_INTENTS)
    patched = _apply(body, changes)

    idx = parse_pabgh_index(header)
    order = sorted(idx.items(), key=lambda kv: kv[1])
    offs = [o for _k, o in order]
    start = idx[1001367]
    i = offs.index(start)
    end = order[i + 1][1] if i + 1 < len(order) else len(body)

    assert patched[start:end] == body[start:end], (
        "Kliff_Clone was modified; a partially-written character record is "
        "exactly what GitHub #329 crashes on")

    # ...and the mod's other records still apply, so this is a per-record
    # refusal rather than the whole mod going dead.
    assert len(changes) == 19
    for name in ("Kliff", "Kliff_AI", "Yann", "PlayerAll"):
        assert [c for c in changes if c["label"].startswith(f"{name}.")], (
            f"{name} must still be written")


def test_refusal_reason_is_reported_not_just_logged(table):
    """The reporter saw a crash with no message in CDUMM. The abandonment
    has to be surfaced, so it is collected into ``refusals_out``."""
    body, header = table
    refusals: list[str] = []
    build_characterinfo_changes(
        body, header, _MOD_INTENTS, refusals_out=refusals)
    assert len(refusals) == 1, refusals
    assert "Kliff_Clone" in refusals[0]
    # It must say what could not be placed, or it isn't actionable.
    # Only character_weight now: `f36` is the gender byte at block+66,
    # which is placeable on every record, so it is no longer part of why
    # this one is abandoned.
    assert "character_weight" in refusals[0]
    assert "f36" not in refusals[0]


def test_a_lone_half_writable_record_is_abandoned_too(table):
    """Placeability must be read from the PARSER, not from the mod.

    A character-swap mod targeting only ONE record, whose post-block slot
    the parser can't place on that record, ends up placing that slot
    nowhere. Judging "is this slot placeable at all" from what the mod
    managed to write would then call it a table-wide gap and write the
    hash-block fields anyway -- reintroducing the exact partial state
    #329 crashes on, just for a smaller mod.

    Reading it from what the parser publishes across the table (Damian
    and thousands of others do publish this slot) gets it right.
    """
    body, header = table
    intents = [
        ("Kliff_Clone", 1001367, "appearance_name", 1767116530),
        ("Kliff_Clone", 1001367, "character_prefab_path", 3755051597),
        ("Kliff_Clone", 1001367, "character_weight", 1287066785),
    ]
    changes = build_characterinfo_changes(body, header, intents)
    assert changes == [], (
        "a lone half-writable record must still be abandoned, even though "
        "the mod itself never proves the slot is placeable")


def test_a_table_wide_gap_does_not_abandon_records(table):
    """The guard is scoped to per-record drift, not to fields CDUMM cannot
    place anywhere.

    ``flag_c`` is modelled but the 1.13 re-port publishes ``_flagC_offset``
    for no record at all. Every targeted record therefore gets the same
    subset, none is inconsistent relative to its siblings, and #150's
    shipped in-game-confirmed behaviour of writing the rest must survive.
    """
    body, header = table
    intents = [
        ("Kliff", 1, "appearance_name", 1767116530),
        ("Kliff", 1, "lookup_24", 2831867940),
        ("Kliff", 1, "flag_c", 2),          # unplaceable on every record
    ]
    changes = build_characterinfo_changes(body, header, intents)
    assert len(changes) == 2, (
        "a field CDUMM can place on no record must not abandon the record")
    assert {c["label"] for c in changes} == {
        "Kliff.appearance_name", "Kliff.lookup_24"}


# ── the deferred fields are skipped, not guessed ────────────────────────

def test_post_block_fields_are_walked_or_refused_never_guessed(table):
    """The 1.13 variable-length post-block region is now WALKED (#302):
    the parser follows the variable bool run to the 900000 anchor, reads
    the list count, computes ``target = anchor + 44 + count*4`` and only
    publishes offsets when the f32-2.0 gate confirms the position.

    So default_action_action_index / f36 are written where the gate
    passes, and everything unverified is still refused by name — the
    contract that matters is that nothing lands on a guess.
    """
    body, header = table
    changes = build_characterinfo_changes(body, header, _MOD_INTENTS)
    written = {(c["label"].split(".", 1)[0], c["label"].split(".", 1)[1])
               for c in changes}

    # character_weight could not be distinguished from the gate field, so
    # it must never be written for any record.
    assert not [r for r, f in written if f == "character_weight"], (
        "character_weight is unmapped and must stay refused")

    # Kliff_Clone fails the gate (its list count is 0 and the preceding
    # u32 is not 2.0f), so no post-block field may be written for it.
    assert not [f for r, f in written
                if r == "Kliff_Clone" and f in _POST_BLOCK], (
        "Kliff_Clone fails the position gate; nothing may be written")

    # Where the gate passes, the walk is trusted: these must apply.
    for rec in ("Kliff", "Kliff_AI", "PlayerAll"):
        assert (rec, "default_action_action_index") in written, (
            f"{rec}.default_action_action_index should now be located")
        assert (rec, "f36") in written, f"{rec}.f36 should now be located"


def test_walked_post_block_matches_the_mod_source_record(table):
    """Byte-exact proof the computed position is right, not merely
    plausible: Damian is the record the mod copies, and the walker reads
    exactly the two values the mod writes elsewhere (1287066785 / 2)."""
    from cdumm.archive.format_parsers.characterinfo_full_parser import (
        parse_entry,
        parse_pabgh_index,
    )
    body, header = table
    idx = parse_pabgh_index(header)
    order = sorted(idx.items(), key=lambda kv: kv[1])
    got = {}
    for rank, (key, start) in enumerate(order):
        end = (order[rank + 1][1]
               if rank + 1 < len(order) else len(body))
        rec = parse_entry(body, start, end)
        if rec and rec.get("name"):
            got[rec["name"]] = rec
    assert got["Damian"].get("_defaultActionActionIndex") == 1287066785
    assert got["Damian"].get("_f36") == 2
    # Kliff's current value is 0 — the mod's job is to make it Damian's.
    assert got["Kliff"].get("_defaultActionActionIndex") == 0
    # Kliff_Clone must publish NO offsets (gate refused).
    assert got["Kliff_Clone"].get("_defaultActionActionIndex") is None
    assert got["Kliff_Clone"].get("_f36_offset") is None


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


def test_gate_is_unambiguous_first_anchor_is_the_only_pass(table):
    """Widening the walk to try EVERY 900000 anchor in a record finds the
    same single position -- measured on live 1.15: 142 records with
    exactly one gate pass, 6963 with none, and *never* two or more.

    That matters twice over: there is no ambiguity for the walk to pick
    wrong, and scanning from the first anchor (what parse_entry does) is
    already optimal rather than leaving positions undiscovered.
    """
    import struct as _s

    from cdumm.archive.format_parsers import characterinfo_full_parser as cp
    body, header = table
    idx = cp.parse_pabgh_index(header)
    order = sorted(idx.items(), key=lambda kv: kv[1])
    for rank, (key, start) in enumerate(order):
        end = (order[rank + 1][1]
               if rank + 1 < len(order) else len(body))
        rec = cp.parse_entry(body, start, end)
        if not rec or rec.get("_isValid_offset") is None:
            continue
        passes = []
        pos = rec["_isValid_offset"] - 3
        while True:
            i = body.find(cp._POST_BLOCK_ANCHOR, pos, end)
            if i < 0:
                break
            if i + 40 <= end:
                cnt = _s.unpack_from("<I", body, i + 36)[0]
                t = i + 44 + cnt * 4
                if (t + 8 <= end and t - 4 >= i
                        and _s.unpack_from("<I", body, t - 4)[0]
                        == cp._POST_BLOCK_GATE):
                    passes.append(t)
            pos = i + 1
        assert len(passes) <= 1, (
            f"{rec.get('name')}: {len(passes)} gate-passing anchors — the "
            f"walk would be ambiguous")
        # And when one exists, it is what parse_entry published.
        if passes:
            assert rec.get("_defaultActionActionIndex_offset") == passes[0]
        else:
            assert rec.get("_defaultActionActionIndex_offset") is None


# ── the mod author's own offsets, as an independent oracle ─────────────
#
# Character Creator 7.5 shipped the SAME edits as raw offset patches with
# hand-written labels naming each field. Those offsets were derived by the
# mod author independently of anything in this repo, which makes them a
# ground truth for the walker: where the walker publishes an offset it
# must agree exactly, and where it cannot verify a position it must stay
# silent rather than write a wrong one.
#
# From "CharacterCreator_Female Animations.json" (Nexus 837, v7.5), the
# four `_defaultActionActionIndex` patches (all 00000000 -> A114B74C):
_AUTHOR_75_OFFSETS = {
    "Kliff": 464,
    "Kliff_Clone": 4503,
    "Kliff_AI": 8414,
    "PlayerAll": 43321,
}


def test_kliff_clone_is_refused_because_the_walk_lands_4_bytes_short(table):
    """The regression this exists to prevent.

    Checked against the live 1.15 table, the walker's offsets agree with
    the author's exactly for Kliff (464), Kliff_AI (8414) and PlayerAll
    (43321) — but for Kliff_Clone, whose list is empty (count=0), the
    computed position is 4499 while the author's real one is 4503. Four
    bytes short, i.e. the neighbouring field.

    So the gate MUST refuse Kliff_Clone. An earlier, looser gate (any
    _f36 in 0..3) accepted that position and would have written into the
    wrong field; only the stricter check catches it. Precision matters
    more than coverage here: a refused field is a visible "can't do
    that", a wrong write is silent corruption.
    """
    body, header = table
    idx = parse_pabgh_index(header)
    order = sorted(idx.items(), key=lambda kv: kv[1])
    published = {}
    for rank, (key, start) in enumerate(order):
        end = (order[rank + 1][1]
               if rank + 1 < len(order) else len(body))
        rec = parse_entry(body, start, end)
        if rec and rec.get("name"):
            published[rec["name"]] = (
                rec.get("_defaultActionActionIndex_offset"), start, end)

    off, start, end = published["Kliff_Clone"]
    assert off is None, (
        "Kliff_Clone's computed position is 4 bytes short of the mod "
        "author's own offset — publishing it would corrupt the "
        "neighbouring field")

    # And the records it does place must land inside their own record.
    for name in ("Kliff", "Kliff_AI", "PlayerAll"):
        off, start, end = published[name]
        assert off is not None, f"{name} should be locatable"
        assert start <= off < end, (
            f"{name}'s offset {off} escaped its own record "
            f"[{start}, {end})")


def test_character_weight_is_the_same_slot_as_default_action_action_index():
    """7.6 renamed `_defaultActionActionIndex` to `character_weight` for
    one record; both must resolve to the same offset key so the rename
    doesn't silently drop the edit."""
    from cdumm.engine.characterinfo_writer import _FIELD_MAP
    assert (_FIELD_MAP["character_weight"]
            == _FIELD_MAP["default_action_action_index"])


# ── #329: the apply dispatch actually carries the refusal out ────────────

def test_apply_dispatch_surfaces_the_refusal_to_the_user(table):
    """The writer refusing is only half the fix.

    #329's reporter saw a crash with no message, and a per-record
    abandonment is NOT a whole-mod dropout -- the mod still produces 19
    changes, so none of the existing "0 byte changes" warnings fire. The
    dispatch has to carry the reason out itself or it stays invisible.

    Goes through the real ``_intents_to_v2_changes`` entry point rather
    than calling the writer again, so the wiring is what's under test.
    """
    from cdumm.engine.format3_apply import _intents_to_v2_changes
    from cdumm.engine.format3_handler import Format3Intent

    body, header = table
    intents = [
        Format3Intent(entry=n, key=k, field=f, op="set", new=v)
        for n, k, f, v in _MOD_INTENTS
    ]
    warnings: list[str] = []
    changes = _intents_to_v2_changes(
        "characterinfo.pabgb", body, header, intents,
        warnings_out=warnings)

    assert len(changes) == 19
    assert len(warnings) == 1, warnings
    assert "Kliff_Clone" in warnings[0]


def test_apply_dispatch_stays_silent_when_nothing_is_abandoned(table):
    """No false alarms: a mod whose records all write completely must not
    produce a warning."""
    from cdumm.engine.format3_apply import _intents_to_v2_changes
    from cdumm.engine.format3_handler import Format3Intent

    body, header = table
    intents = [
        Format3Intent(entry="Kliff", key=1, field="appearance_name",
                      op="set", new=1767116530),
        Format3Intent(entry="Kliff", key=1, field="lookup_24",
                      op="set", new=2831867940),
    ]
    warnings: list[str] = []
    changes = _intents_to_v2_changes(
        "characterinfo.pabgb", body, header, intents,
        warnings_out=warnings)
    assert len(changes) == 2
    assert warnings == []


def test_apply_dispatch_tolerates_no_warnings_channel(table):
    """``warnings_out`` is optional; the default path must not raise."""
    from cdumm.engine.format3_apply import _intents_to_v2_changes
    from cdumm.engine.format3_handler import Format3Intent

    body, header = table
    intents = [
        Format3Intent(entry=n, key=k, field=f, op="set", new=v)
        for n, k, f, v in _MOD_INTENTS
    ]
    assert len(_intents_to_v2_changes(
        "characterinfo.pabgb", body, header, intents)) == 19


@pytest.mark.parametrize("bad_value", ["not-an-int", None, 3.5, True])
def test_a_bad_value_abandons_the_record_rather_than_half_writing(
        table, bad_value):
    """A value CDUMM cannot encode must abandon the record too.

    The all-or-nothing guard keys on fields the mod asked this record to
    carry, so the type check has to run AFTER the record registers as
    expecting the write. Checking it earlier let a non-integer skip
    straight past the guard: the sibling fields were written and the
    record came out half-modded -- the exact state
    test_abandoned_record_keeps_every_vanilla_byte exists to prevent,
    reached by a different route.

    Out-of-range integers were already handled correctly (that check sits
    after registration); these values took the earlier exit.
    """
    body, header = table
    intents = [
        ("Kliff", 1, "appearance_name", 111),
        ("Kliff", 1, "character_prefab_path", 222),
        ("Kliff", 1, "default_action_action_index", bad_value),
        ("Kliff", 1, "lookup_25", 444),
    ]
    refusals: list[str] = []
    changes = build_characterinfo_changes(
        body, header, intents, refusals_out=refusals)

    assert changes == [], (
        f"value {bad_value!r} was rejected but the record's other fields "
        f"were still written -- that is a half-written character record")
    assert len(refusals) == 1, refusals
    assert "Kliff" in refusals[0]


def test_a_bad_value_does_not_abandon_an_unrelated_record(table):
    """Abandonment stays per-record: a bad value on one record must not
    stop a different record the same mod also targets."""
    body, header = table
    intents = [
        ("Kliff", 1, "appearance_name", 111),
        ("Kliff", 1, "default_action_action_index", "not-an-int"),
        ("Kliff_AI", 1, "appearance_name", 333),
    ]
    refusals: list[str] = []
    changes = build_characterinfo_changes(
        body, header, intents, refusals_out=refusals)

    labels = [c["label"] for c in changes]
    assert labels == ["Kliff_AI.appearance_name"], labels
    assert len(refusals) == 1 and "Kliff" in refusals[0]


def test_f36_is_the_gender_byte_at_block_plus_66(table):
    """`f36` is the record's GENDER byte, not the parser's `_f36` u32.

    Character Creator ships this same edit twice: as the Format 3 file
    under test, and as a raw offset patch whose hand-written labels name
    the field ``_gender`` and change it from 01 to 02. The two files agree
    field for field on all five records, and once every other field is
    paired off by its exact value, the one left over is ``f36`` here and
    ``_gender`` there.

    That field is ONE byte at block+66, and it must be written there.
    """
    body, header = table
    changes = build_characterinfo_changes(body, header, _MOD_INTENTS)

    idx = parse_pabgh_index(header)
    order = sorted(idx.items(), key=lambda kv: kv[1])
    blocks: dict[str, int] = {}
    for rank, (key, start) in enumerate(order):
        end = (order[rank + 1][1]
               if rank + 1 < len(order) else len(body))
        rec = parse_entry(body, start, end)
        if rec and rec.get("name"):
            blk = rec.get("_upperActionChartPackageGroupName_offset")
            if blk is not None:
                blocks[rec["name"]] = blk

    f36 = {c["label"].split(".")[0]: c
           for c in changes if c["label"].endswith(".f36")}
    assert f36, "the mod sets f36; it must produce changes"

    for name, change in f36.items():
        assert change["offset"] == blocks[name] + 66, (
            f"{name}.f36 must land on the gender byte at block+66, "
            f"got block+{change['offset'] - blocks[name]}")
        # One byte wide, not four.
        assert len(change["patched"]) == 2, change
        assert change["patched"].lower() == "02", change
        # And it must actually be a change, not a write of the same value.
        assert change["original"].lower() != change["patched"].lower(), (
            f"{name}.f36 writes its existing value -- a no-op edit is the "
            f"signature of the wrong slot")


def test_block_plus_66_is_an_enum_on_every_record(table):
    """Shape check on the slot the mapping above depends on.

    Gender is a small enum present on every record. Across the full live
    table block+66 holds only 0, 1 or 2 on all 7244 records; the parser's
    `_f36_offset`, by contrast, is published on 142 of them. Assert the
    enum shape here so a future layout change that breaks it is caught.
    """
    body, header = table
    idx = parse_pabgh_index(header)
    order = sorted(idx.items(), key=lambda kv: kv[1])
    seen: set[int] = set()
    n = 0
    for rank, (key, start) in enumerate(order):
        end = (order[rank + 1][1]
               if rank + 1 < len(order) else len(body))
        rec = parse_entry(body, start, end)
        if not rec:
            continue
        blk = rec.get("_upperActionChartPackageGroupName_offset")
        if blk is None or blk + 66 >= len(body):
            continue
        seen.add(body[blk + 66])
        n += 1

    assert n, "no records carried a block offset"
    assert seen <= {0, 1, 2}, (
        f"block+66 is meant to be the gender enum, saw {sorted(seen)}")
