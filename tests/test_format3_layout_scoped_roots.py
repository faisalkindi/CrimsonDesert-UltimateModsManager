"""The validator vouches for fields this game's record doesn't carry.

Found by running the top 59 Nexus mods through the engine (#285), not by a
bug report — which is the point of doing that.

NOTE (#285): `prefab_data_list` is no longer one of these. It turned out not
to be missing from the game at all -- CD 1.13 relocated it to the end of the
record, where it sat undecoded (and invisible, because opaque preservation
still round-trips byte-exact). It is decoded and writable now, so this file
pins the *remaining* unaddressable root, plus the fact that prefab_data_list
must NOT be refused any more.

The validator accepts a nested iteminfo path when its ROOT is a real field in
ANY layout CDUMM knows. That rule is deliberate (#259): it is what removed the
hardcoded allowlist that was refusing `price_list[0].price.price` and every
gear-stat path. But "any layout CDUMM knows" includes layouts this game is not
running. CDUMM's CD 1.13 layout does not expose
`gimmick_visual_prefab_data_list`, so:

    prefab_data_list[0].tribe_gender_list   ->  validator: ACCEPTED
                                            ->  writer:    resolves nothing
                                            ->  user:      "N intents ready",
                                                           then 0 changes, no
                                                           reason given.

That is the SAME two-gates-disagree bug as #259, pointing the other way:

  * #259  the validator REFUSED what the writer could do   -> useless advice
  * here  the validator ACCEPTS what the writer cannot do  -> silent no-op

The fix has to live in the apply path, because only there are the game's own
bytes to hand: it re-checks nested roots against the layout DETECTED from the
installed table, and refuses the ones the record doesn't carry, naming the
field. Scoping to the detected layout — not to the newest one CDUMM knows — is
what stops it becoming a false refusal on a game version that really does carry
the field.
"""
from __future__ import annotations

import pytest

from tests.fixture_loaders import load_vanilla113

from cdumm.engine.format3_apply import (
    _iteminfo_layout_roots, drop_intents_the_layout_cannot_carry)
from cdumm.engine.format3_handler import Format3Intent, validate_intents

TARGET = "iteminfo.pabgb"
HELM = 14510

#: Not addressable by CDUMM's CD 1.13 layout.
#:
#: `prefab_data_list` USED to be here. It isn't any more: #285 found it -- CD
#: 1.13 merged it into GimmickVisualPrefabData and relocated it to the end of
#: the record, where it sat undecoded as `_tail_slack` while the table still
#: round-tripped byte-exact. It is now decoded and writable, so the guard must
#: NOT refuse it, and the test below moved it into STILL_HERE.
#:
#: The merged struct is exposed under the name mods use (`prefab_data_list`),
#: so the old `gimmick_visual_prefab_data_list` root has no writer of its own.
NOT_ADDRESSABLE_113 = ("gimmick_visual_prefab_data_list",)
GONE_IN_113 = NOT_ADDRESSABLE_113  # kept for the parametrize ids below

#: Addressable on CD 1.13 — the guard must NOT refuse any of these.
STILL_HERE = (
    "drop_default_data.use_socket",                    # sockets, #191
    "price_list[0].price.price",                       # item prices, #259
    "sharpness_data.stat_list[0].change_mb",           # gear stats, #277
    ("enchant_data_list[0].enchant_stat_data"
     ".stat_list_static[0].change_mb"),                # gear stats, tiers
    "prefab_data_list[0].tribe_gender_list",           # Equip Everything, #285
)


@pytest.fixture(scope="module")
def table():
    return load_vanilla113("iteminfo.pabgb"), load_vanilla113("iteminfo.pabgh")


def _intent(field, new=1):
    return Format3Intent(entry="X", key=HELM, field=field, op="set", new=new)


# ── the bug ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("root", GONE_IN_113)
def test_the_validator_still_accepts_these(root):
    """Pinning the bug itself, so the fix can't be mistaken for a no-op.

    If this ever starts failing, the validator has been scoped too — good,
    but then the apply-side guard below is the redundant one, not this."""
    res = validate_intents(TARGET, [_intent(f"{root}[0].tribe_gender_list",
                                            [1])])
    assert res.supported, (
        f"{root} nested path is no longer accepted by the validator; the "
        f"silent-no-op bug may have been fixed elsewhere")


@pytest.mark.parametrize("root", GONE_IN_113)
def test_the_detected_layout_cannot_address_them(table, root):
    """NOTE the wording. The 1.13 *layout* can't address these fields.

    That is NOT the same as the game record lacking them. An earlier version
    of this file said it was, and it was wrong: `prefab_data_list` was never
    gone -- it was relocated to the end of the record and sat there as
    undecoded `_tail_slack` while the table still round-tripped byte-exact.
    It is decoded now (#285), which is why it is no longer in this list.

    So this pins what CDUMM can currently *address*, and the refusal it
    drives must never claim a field is gone from the game."""
    body, header = table
    roots = _iteminfo_layout_roots(body, header)
    assert roots is not None
    assert root not in roots, (
        f"{root} is now addressable in the CD 1.13 layout — if it has been "
        f"decoded, this guard should stop refusing it and the writer should "
        f"just write it (that is what happened to prefab_data_list in #285)")


def test_prefab_data_list_is_no_longer_refused(table):
    """The whole point of #285: this used to be silently no-op'd, then
    honestly refused, and now it actually applies."""
    body, header = table
    roots = _iteminfo_layout_roots(body, header)
    assert "prefab_data_list" in roots

    intents = [_intent("prefab_data_list[0].tribe_gender_list", [1])]
    kept, dropped = drop_intents_the_layout_cannot_carry(
        TARGET, intents, body, header)
    assert dropped == [], "prefab_data_list is decoded now; stop refusing it"
    assert kept == intents


# ── the fix ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("root", GONE_IN_113)
def test_apply_refuses_them_and_says_why(table, root):
    body, header = table
    intents = [_intent(f"{root}[0].tribe_gender_list", [1])]
    kept, dropped = drop_intents_the_layout_cannot_carry(
        TARGET, intents, body, header)
    assert kept == []
    assert len(dropped) == 1
    _i, why = dropped[0]
    # the message must name the field and not be a generic shrug
    assert root in why
    assert "cannot write" in why.lower()
    # ...and it must NOT claim the field is gone from the game. It isn't
    # known to be: the 1.13 record has 76-139 bytes of undecoded tail.
    assert "not present" not in why.lower()
    assert "does not exist" not in why.lower()


def test_the_fields_this_game_does_carry_are_untouched(table):
    """The failure mode of an over-eager guard: refusing things that work.

    Sockets, item prices and gear stats are all nested paths into roots the
    1.13 record DOES carry. Every one of them was fixed this week; none may
    regress here."""
    body, header = table
    intents = [_intent(f) for f in STILL_HERE]
    kept, dropped = drop_intents_the_layout_cannot_carry(
        TARGET, intents, body, header)
    assert dropped == []
    assert len(kept) == len(STILL_HERE)


def test_flat_fields_and_other_tables_pass_straight_through(table):
    body, header = table
    flat = [_intent("max_stack_count", 999)]
    kept, dropped = drop_intents_the_layout_cannot_carry(
        TARGET, flat, body, header)
    assert kept == flat and dropped == []

    # not iteminfo -> not our business, whatever the field looks like
    other = [_intent("anything.at.all")]
    kept, dropped = drop_intents_the_layout_cannot_carry(
        "storeinfo.pabgb", other, body, header)
    assert kept == other and dropped == []


def test_an_undecodable_table_changes_nothing(table):
    """Fail open. If the layout can't be resolved we must not start refusing
    intents that would otherwise have applied — the guard is a safety net,
    not a gate.

    Deliberately uses a root the layout CANNOT address, so the test can only
    pass because of the fail-open path. With a now-writable root it would go
    green either way and stop testing anything."""
    root = NOT_ADDRESSABLE_113[0]
    intents = [_intent(f"{root}[0].tribe_gender_list", [1])]
    kept, dropped = drop_intents_the_layout_cannot_carry(
        TARGET, intents, b"", b"")   # empty bytes -> layout unresolvable
    assert kept == intents and dropped == []


def test_a_path_shaped_target_is_still_recognised_as_iteminfo(table):
    """`_table_name_from_target` returns the full path, not a bare table name
    — the exact gotcha that made `match` select zero records (#275) and
    array_append no-op (#278). Third time it bites, so it is pinned.

    Uses a root the 1.13 layout still can't address, so the assertion is
    about the TARGET being recognised, not about which fields are writable.
    (It used to use prefab_data_list — which #285 then made writable, so the
    test started failing for a reason that had nothing to do with what it
    was pinning. Pin one thing.)"""
    body, header = table
    root = NOT_ADDRESSABLE_113[0]
    intents = [_intent(f"{root}[0].tribe_gender_list", [1])]
    kept, dropped = drop_intents_the_layout_cannot_carry(
        "gamedata/binary__/client/bin/iteminfo.pabgb", intents, body, header)
    assert kept == [] and len(dropped) == 1, (
        "a path-shaped target must still be recognised as iteminfo")
