"""The verification harness must have teeth AND not cry wolf.

Every gate that unlocks new tables (a licensed reader-order parser, an ASI
reflection dump) will feed CDUMM a claimed field order. The harness is what
decides whether to believe it. So the harness itself has to be proven:

  * it must PASS the ground truth (no false rejections), and
  * it must REJECT a wrong order (real teeth) -- by identity always, and by
    byte-decode for gross errors.

The one honest caveat, pinned below: the byte-decode score has a blind spot
(same-width fields swapped upstream of the walker's stall decode
identically). Order-identity is what covers it. Both are tested.
"""
from __future__ import annotations

import random


from tests.fixture_loaders import load_vanilla113

from cdumm.engine.schema_verify import (
    DecodeScore, decode_score, tables_with_verified_order, verified_order,
    verify_order_source)

ITEM = "ItemInfo"


def _fixture():
    return load_vanilla113("iteminfo.pabgb"), load_vanilla113("iteminfo.pabgh")


def _ground_truth() -> dict[str, list[str]]:
    return {t: verified_order(t) for t in tables_with_verified_order()}


# ── ground truth: the harness must not reject what's real ────────────────

def test_the_seven_verified_tables_are_discovered():
    tabs = tables_with_verified_order()
    assert ITEM in tabs
    # the hand-RE'd set as of this writing; a change here should be
    # deliberate, not accidental
    assert set(tabs) == {
        "CharacterInfo", "FieldInfo", "ItemInfo", "RegionInfo",
        "StageInfo", "VehicleInfo", "WantedInfo"}


def test_ground_truth_passes_itself():
    body, header = _fixture()
    rep = verify_order_source(_ground_truth(), {ITEM: (body, header)})
    assert rep.trustworthy
    assert len(rep.passed) == rep.known_tables == 7
    item = next(r for r in rep.results if r.table == ITEM)
    assert item.order_matches is True
    assert item.decode_ok is True


def test_decode_score_anchors_ground_truth_to_real_bytes():
    """"Verified" must mean "decodes real records", not "asserted".

    The baseline decode of the ground-truth order over the committed
    fixture has to actually reach into all 6508 records, not zero.
    """
    body, header = _fixture()
    base = decode_score(ITEM, verified_order(ITEM), body, header)
    assert base.records == 6508
    assert base.median_fields >= 10, (
        "the ground-truth ItemInfo order barely decodes the real table -- "
        "either the fixture or the order is wrong")


# ── teeth: the harness must reject wrong orders ──────────────────────────

def test_scrambled_order_is_rejected_by_both_gates():
    body, header = _fixture()
    truth = verified_order(ITEM)
    scrambled = truth[:]
    random.Random(42).shuffle(scrambled)

    cand = _ground_truth()
    cand[ITEM] = scrambled
    rep = verify_order_source(cand, {ITEM: (body, header)})

    assert not rep.trustworthy
    item = next(r for r in rep.results if r.table == ITEM)
    assert item.order_matches is False              # identity gate
    assert item.candidate.median_fields < item.baseline.median_fields, (
        "a fully scrambled order should decode strictly worse than truth")
    assert item.passed is False


def test_single_adjacent_swap_is_caught_by_identity():
    """A one-field swap is the subtle error a real parser might make.

    This is the honest edge: two same-width fields swapped upstream of the
    walker's stall can decode to the SAME byte count, so the decode score
    may not flag it. Identity always does. This test pins that division of
    labour -- if decode ever gets sharp enough to catch it too, great, but
    the guarantee lives in identity.
    """
    body, header = _fixture()
    truth = verified_order(ITEM)
    swapped = truth[:]
    swapped[5], swapped[6] = swapped[6], swapped[5]

    cand = _ground_truth()
    cand[ITEM] = swapped
    rep = verify_order_source(cand, {ITEM: (body, header)})

    item = next(r for r in rep.results if r.table == ITEM)
    assert item.order_matches is False, "identity must catch a single swap"
    assert item.passed is False
    assert not rep.trustworthy


def test_missing_field_is_rejected():
    body, header = _fixture()
    truth = verified_order(ITEM)
    cand = _ground_truth()
    cand[ITEM] = truth[:-1]                          # drop the last field
    rep = verify_order_source(cand, {ITEM: (body, header)})
    item = next(r for r in rep.results if r.table == ITEM)
    assert item.order_matches is False
    assert item.passed is False


def test_extra_field_is_rejected():
    truth = verified_order(ITEM)
    cand = _ground_truth()
    cand[ITEM] = truth + ["_totallyMadeUpField"]
    rep = verify_order_source(cand)                  # order check alone
    item = next(r for r in rep.results if r.table == ITEM)
    assert item.order_matches is False
    assert item.passed is False


# ── coverage semantics ───────────────────────────────────────────────────

def test_uncovered_tables_do_not_count_as_passed():
    """A candidate that only speaks for some tables is judged only on
    those -- but an EMPTY candidate is not vacuously trustworthy."""
    rep = verify_order_source({})
    assert rep.covered == []
    assert rep.trustworthy is False
    assert all(r.order_matches is None for r in rep.results)


def test_partial_candidate_is_trustworthy_on_what_it_covers():
    cand = {ITEM: verified_order(ITEM)}              # one table only
    rep = verify_order_source(cand)
    assert len(rep.covered) == 1
    assert rep.trustworthy is True                  # got its one table right
    assert len(rep.results) == 7                    # but reports all knowns


def test_partial_candidate_still_fails_if_its_one_table_is_wrong():
    cand = {ITEM: list(reversed(verified_order(ITEM)))}
    rep = verify_order_source(cand)
    assert rep.trustworthy is False


# ── DecodeScore unit ─────────────────────────────────────────────────────

def test_decode_score_at_least_ordering():
    a = DecodeScore(100, 11.0, 0.0, None)
    b = DecodeScore(100, 4.0, 0.0, "x")
    assert a.at_least(b)
    assert not b.at_least(a)
