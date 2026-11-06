"""The walker's field order is chosen by VALUES, on the path users see.

There are two independent iteminfo readers: the hand-written
``iteminfo_native_parser``, and the generic walker driven by
``_ordered_fields``. CD 1.16 removed two ItemInfo fields, and a removed
field is the worst drift there is -- the walker keeps reading, takes the
NEXT field's bytes as the missing one, and desyncs everything after it.

Two earlier mistakes are pinned here so they cannot come back.

**It was unreachable.** ``ORDER_VARIANTS`` is keyed ``"ItemInfo"``, but
production reaches selection through ``identify_table_from_path``, which
yields the file stem ``"iteminfo"``. The lookup was a bare ``in`` test
with no case normalisation, so it never matched in production -- while
every test called it with the CamelCase spelling and passed. Green tests
over code that cannot run. So the tests below use the *production*
spelling, and one asserts both spellings agree.

**Walk depth was the wrong arbiter.** ``decode_score`` counts how far a
walk gets, and the failure that matters is a walk that goes exactly as
far while reading the wrong bytes. On live 1.16, before #492, the winning
order scored a median 109 of 109 fields at 87.5% full depth while
``_maxEndurance`` and ``_respawnTimeSeconds`` agreed with the native
parser on **zero** of 5,756 records -- both were read from the wrong
offset, and the walk consumed the same number of bytes doing it. (The
test below has the numbers since then.) The arbiter is now the native
parser's decoded values, which is a real oracle: #336 established it
byte-exact, and it is what settled where the 1.16 10-byte run goes.
"""
from __future__ import annotations

import zlib
from pathlib import Path

import pytest

from cdumm.engine import schema_verify as sv

_FX = Path(__file__).resolve().parent / "fixtures"

# The spelling production actually passes. Using "ItemInfo" here is what
# made the previous version of this file pass over dead code.
TABLE = "iteminfo"


def _fixture(ver: str, name: str):
    d = _FX / f"vanilla{ver}"
    p = d / f"{name}.pabgb.zlib"
    if not p.exists():
        return None
    return (zlib.decompress(p.read_bytes()),
            zlib.decompress((d / f"{name}.pabgh.zlib").read_bytes()))


@pytest.fixture(autouse=True)
def _clear_cache():
    sv._ORDER_CACHE.clear()
    yield
    sv._ORDER_CACHE.clear()


# ── reachability: the bug that made every old test meaningless ──────────

def test_the_production_table_spelling_finds_the_variant():
    """``identify_table_from_path`` yields ``"iteminfo"``. If that spelling
    misses, the whole mechanism is dead code no matter how green it is."""
    assert sv._variants_for("iteminfo") is not None


def test_both_spellings_select_identically():
    t = _fixture("116", "iteminfo")
    if t is None:
        pytest.skip("no 1.16 iteminfo fixture")
    lower = sv.select_order("iteminfo", t[0], t[1])
    sv._ORDER_CACHE.clear()
    camel = sv.select_order("ItemInfo", t[0], t[1])
    assert lower == camel


def test_tables_without_variants_are_unaffected():
    assert sv._variants_for("storeinfo") is None
    assert sv._variants_for("characterinfo") is None


def test_the_display_path_uses_the_chosen_order():
    """The visible symptom is the Game Data grid, so that is where this has
    to be wired. ``parse_records_display`` previously called ``get_schema``
    directly and never saw a variant."""
    import inspect

    from cdumm.semantic import parser

    src = inspect.getsource(parser.parse_records_display)
    assert "schema_for_table" in src


# ── the oracle decides, and it can see what walk depth cannot ───────────

def test_the_oracle_sees_fields_that_walk_depth_calls_fine():
    """The whole reason for the rewrite.

    ``_maxEndurance`` straddles the opaque run that a field-NAME order
    cannot express, so the walker reads it early. It consumes the right
    number of bytes doing it, which is why depth-based scoring rates the
    order as near perfect.

    ``_respawnTimeSeconds`` straddles the run too and is read just as
    wrong, but it is no longer in this set. Since #492 put the missing
    flag byte and the 4 filler bytes in the order, it lands on bytes that
    are 0 on most records whose real value is 0, so it "agrees" there; on
    the 798 records where either side is nonzero it matches none. A field
    in this set is wrong; a field missing from it is not proven right.
    """
    t = _fixture("116", "iteminfo")
    if t is None:
        pytest.skip("no 1.16 iteminfo fixture")
    body, header = t
    truth = sv._iteminfo_oracle(body, header)
    base = sv.verified_order(TABLE)
    dropped = sv._variants_for(TABLE)[0][1]
    order = [f for f in base if f not in dropped]

    depth = sv.decode_score(TABLE, order, body, header)
    values = sv.value_agreement(TABLE, order, body, header, truth)

    assert depth.frac_reached_last > 0.8          # depth says: excellent
    assert set(values.zero_agreement_fields()) == {   # values say: it's wrong
        "_maxEndurance"}
    ok, n = values.per_field["_respawnTimeSeconds"]
    assert ok < n                                   # and this one isn't right


def test_the_1_16_order_wins_on_1_16_by_values():
    t = _fixture("116", "iteminfo")
    if t is None:
        pytest.skip("no 1.16 iteminfo fixture")
    body, header = t
    truth = sv._iteminfo_oracle(body, header)
    base = sv.verified_order(TABLE)
    dropped = sv._variants_for(TABLE)[0][1]
    variant = [f for f in base if f not in dropped]

    assert (sv.value_agreement(TABLE, variant, body, header, truth).agreeing
            > sv.value_agreement(TABLE, base, body, header, truth).agreeing)
    assert sv.select_order(TABLE, body, header)[0] == "cd116"


def test_the_declared_order_still_wins_on_older_builds():
    """Selection must not simply always prefer the newest variant."""
    for ver in ("110", "113"):
        t = _fixture(ver, "iteminfo")
        if t is None:
            continue
        sv._ORDER_CACHE.clear()
        label, order = sv.select_order(TABLE, t[0], t[1])
        assert label == "base", f"{ver} should keep the declared order"
        assert order == sv.verified_order(TABLE)


# ── refusal: no oracle, or an ambiguous answer, means no change ─────────

def test_a_table_with_no_oracle_keeps_the_declared_order(monkeypatch):
    """Preferring a deeper walk on faith is exactly what produced the
    zero-agreement fields above. Without a second decoder to check
    against, there is no answer, so nothing is chosen."""
    t = _fixture("116", "iteminfo")
    if t is None:
        pytest.skip("no 1.16 iteminfo fixture")
    monkeypatch.setattr(sv, "ORDER_ORACLES", {})
    sv._ORDER_CACHE.clear()
    assert sv.select_order(TABLE, t[0], t[1])[0] == "base"


def test_a_tie_refuses_instead_of_taking_the_first(monkeypatch):
    """Ties used to be resolved by declaration order. The declared order
    is the one actually verified against bytes, so it keeps the benefit
    of the doubt."""
    t = _fixture("116", "iteminfo")
    if t is None:
        pytest.skip("no 1.16 iteminfo fixture")
    dropped = sv._variants_for(TABLE)[0][1]
    # Two labels, same field set -> identical agreement -> a tie.
    monkeypatch.setitem(sv.ORDER_VARIANTS, "ItemInfo",
                        (("cd116", dropped), ("cd116_twin", dropped)))
    sv._ORDER_CACHE.clear()
    assert sv.select_order(TABLE, t[0], t[1])[0] == "base"


def test_a_broken_oracle_does_not_take_the_table_down(monkeypatch):
    t = _fixture("116", "iteminfo")
    if t is None:
        pytest.skip("no 1.16 iteminfo fixture")

    def boom(_b, _h):
        raise RuntimeError("oracle exploded")

    monkeypatch.setattr(sv, "ORDER_ORACLES", {TABLE: boom})
    sv._ORDER_CACHE.clear()
    assert sv.select_order(TABLE, t[0], t[1])[0] == "base"


# ── mechanics ───────────────────────────────────────────────────────────

def test_camel_to_snake_bridges_the_two_decoders():
    assert sv._camel_to_snake("_maxEndurance") == "max_endurance"
    assert sv._camel_to_snake("_respawnTimeSeconds") == "respawn_time_seconds"
    assert sv._camel_to_snake("_isBlocked") == "is_blocked"
    assert sv._camel_to_snake("_key") == "key"


def test_selection_is_cached_per_table_body():
    """Scoring decodes the whole table twice over, so it must happen once
    per body, not once per intent."""
    t = _fixture("116", "iteminfo")
    if t is None:
        pytest.skip("no 1.16 iteminfo fixture")
    calls = []
    real = sv._select_order_uncached

    def counted(table, body, header):
        calls.append(table)
        return real(table, body, header)

    sv._select_order_uncached = counted
    try:
        for _ in range(5):
            sv.select_order(TABLE, t[0], t[1])
    finally:
        sv._select_order_uncached = real
    assert calls == [TABLE]
