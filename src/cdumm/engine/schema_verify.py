"""Verification harness for candidate field-ORDER sources.

The unlock for the other ~82 game tables is a source of correct on-disk
field order (a licensed reader-order parser, or an ASI reflection dump —
see docs/GAME_DATA_UNLOCK_ROADMAP.md). The open question for any such
source is: *why trust it?* The community stat mapping was wrong on 18 of
24 entries, all flagged "verified". Hand-work is not evidence.

This module makes a candidate source **prove itself** against the tables
CDUMM already knows the byte-exact order for, before it is trusted on the
tables it doesn't.

A candidate order source is a mapping::

    { "ItemInfo": ["_isBlocked", "_maxStackCount", ...], ... }

(table name -> field names in on-disk order). It is checked two ways:

1. **Order identity.** For every table CDUMM has a verified
   ``_ordered_fields`` for, the candidate's order must match it exactly.
   Fast, and it catches gross errors (a scramble, a missing field, a
   field in the wrong slot).

2. **Fixture decode score.** Where a committed vanilla fixture exists, the
   candidate order is used to actually *walk the real bytes*. A correct
   order consumes far into each record; a grossly wrong one desyncs almost
   immediately. This does two things order-identity can't: it **anchors
   our own ground truth to real data** (so "verified" means "decodes 6508
   real records", not "someone said so"), and it flags gross corruption.

   It is corroboration, NOT the primary gate, and it has a known blind
   spot. The narrow statement is that two fields of the same width, swapped
   upstream of where the walker stalls, decode identically — the byte count
   doesn't move. The blind spot is actually WIDER than that: the score is a
   function of bytes consumed, so **any** rearrangement preserving the total
   width over a span is invisible to it, not only swaps of equal-width
   pairs. Measured: reordering six of CharacterInfo's fields (moving two u8s
   from before a u16 + two u64s + a u8 to after them) leaves the score
   unchanged at 14/14 fields on 100% of records. Order-identity is what
   catches those, and where two orders disagree only in that way the decode
   score cannot referee at all — that needs ``value_agreement``.

   The decode score's sharpness also scales with how far the walker reaches
   (it stalls early on some tables until their nested types are modelled),
   so it is reported, weighed, but never trusted alone.

Order-identity is the workhorse; the decode score keeps it honest. A
source that fails EITHER on any known table is rejected. A source that
passes all of them has earned the benefit of the doubt on the unknown
ones — and even then each new table stays gated to ``verified_fields``
until its values are cross-checked against real records.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any

from cdumm.engine.format3_apply import _consume_field_bytes, _payload_offset
from cdumm.semantic import parser as parser_mod
from cdumm.semantic.parser import TableSchema, get_schema, parse_pabgh_index
from cdumm.semantic.record_header import table_header_order

logger = logging.getLogger(__name__)

_SCHEMA_DIR = Path(__file__).resolve().parents[3] / "schemas"


def tables_with_verified_order() -> list[str]:
    """Tables that carry a hand-verified ``_ordered_fields`` override.

    These are the ground truth: their on-disk field order was reverse
    engineered and confirmed byte-exact. Everything else in the shipped
    schema is in memory order and is NOT trustworthy.
    """
    over_path = _SCHEMA_DIR / "pabgb_type_overrides.json"
    over = json.loads(over_path.read_text(encoding="utf-8-sig"))
    return sorted(
        name for name, ov in over.items()
        if isinstance(ov, dict) and ov.get("_ordered_fields"))


def verified_order(table: str) -> list[str]:
    """The verified on-disk field order for ``table``.

    Sourced from the loaded schema (which applies ``_ordered_fields``), so
    it is exactly the order CDUMM's own walker/writer uses — not a second
    copy that could drift.
    """
    schema = get_schema(table)
    if schema is None:
        raise KeyError(f"no schema for {table!r}")
    return [f.name for f in schema.fields]


# ── fixture-backed byte decode ───────────────────────────────────────────

@dataclass(frozen=True)
class DecodeScore:
    """How well a field order walks a real table.

    Higher is better. A correct order reaches deep into every record; a
    wrong one bails early. `median_fields` is the headline number.
    """
    records: int
    median_fields: float
    frac_reached_last: float      # fraction of records that consumed every
    #                               field in the order without bailing
    first_bail_field: str | None  # most common field the walk dies on

    def at_least(self, other: "DecodeScore") -> bool:
        """True if this order decodes no worse than ``other``."""
        return (self.median_fields >= other.median_fields
                and self.frac_reached_last >= other.frac_reached_last - 1e-9)


def _schema_in_order(table: str, order: list[str]) -> TableSchema:
    """A copy of ``table``'s schema with fields put in ``order``.

    Unknown field names (not in the base schema) are dropped — the walk
    simply can't model a field it has no spec for, which caps the score
    rather than crashing. That's the honest behaviour: an order naming
    fields we can't type is worth less than one that doesn't.
    """
    base = get_schema(table)
    by_name = {f.name: f for f in base.fields}
    fields = [by_name[n] for n in order if n in by_name]
    return TableSchema(
        table_name=base.table_name,
        fields=fields,
        no_null_skip=base.no_null_skip,
        no_entry_header=base.no_entry_header,
    )


def decode_score(table: str, order: list[str],
                 body: bytes, header: bytes) -> DecodeScore:
    """Walk every record of ``body`` using ``order`` and score the fit.

    Uses the loaded-schema cache as-is. Callers wanting a pristine load
    (e.g. after another test mutated a schema) should set
    ``parser_mod._loaded_schemas = None`` first; ``verify_order_source``
    does this once.
    """
    schema = _schema_in_order(table, order)
    key_size, offsets = parse_pabgh_index(header, table)
    hdr_order = table_header_order(body, offsets, key_size)
    entries = sorted(offsets.items(), key=lambda kv: kv[1])
    total = len(entries)
    if total == 0:
        return DecodeScore(0, 0.0, 0.0, None)

    n_fields = len(schema.fields)
    consumed_counts: list[int] = []
    reached_last = 0
    bail_field: dict[str, int] = {}

    for i, (_key, off0) in enumerate(entries):
        end = entries[i + 1][1] if i + 1 < total else len(body)
        po = _payload_offset(body, off0, key_size,
                             no_null_skip=schema.no_null_skip,
                             no_entry_header=schema.no_entry_header,
                             header_order=hdr_order)
        if po is None:
            consumed_counts.append(0)
            continue
        off = po
        n = 0
        for f in schema.fields:
            c = _consume_field_bytes(body, off, f, end)
            if c is None:
                bail_field[f.name] = bail_field.get(f.name, 0) + 1
                break
            off += c
            n += 1
        consumed_counts.append(n)
        if n == n_fields:
            reached_last += 1

    worst = (max(bail_field.items(), key=lambda kv: kv[1])[0]
             if bail_field else None)
    return DecodeScore(
        records=total,
        median_fields=float(median(consumed_counts)),
        frac_reached_last=reached_last / total,
        first_bail_field=worst,
    )


# ── the harness ──────────────────────────────────────────────────────────

@dataclass
class TableResult:
    table: str
    covered: bool                 # candidate provided an order for it
    order_matches: bool | None    # vs verified order (None if not covered)
    baseline: DecodeScore | None  # verified order, on the fixture
    candidate: DecodeScore | None  # candidate order, on the fixture
    decode_ok: bool | None        # candidate decodes >= baseline

    @property
    def passed(self) -> bool:
        if not self.covered:
            return False
        if not self.order_matches:
            return False
        if self.decode_ok is False:
            return False
        return True


@dataclass
class VerificationReport:
    results: list[TableResult] = field(default_factory=list)

    @property
    def known_tables(self) -> int:
        return len(self.results)

    @property
    def covered(self) -> list[TableResult]:
        return [r for r in self.results if r.covered]

    @property
    def passed(self) -> list[TableResult]:
        return [r for r in self.results if r.passed]

    @property
    def trustworthy(self) -> bool:
        """True iff every table the candidate DID cover passed.

        A candidate that covers few tables but gets them all right is
        trustworthy on those; coverage breadth is reported separately so
        the caller can weigh it.
        """
        cov = self.covered
        return bool(cov) and all(r.passed for r in cov)

    def summary(self) -> str:
        lines = [f"verified tables: {self.known_tables} | "
                 f"covered: {len(self.covered)} | passed: {len(self.passed)}"]
        for r in self.results:
            if not r.covered:
                lines.append(f"  {r.table:<16} — not covered by candidate")
                continue
            tag = "PASS" if r.passed else "FAIL"
            detail = "order matches" if r.order_matches else "ORDER MISMATCH"
            if r.candidate is not None:
                detail += (f"; decode med {r.candidate.median_fields:g}"
                           f" vs baseline {r.baseline.median_fields:g}")
            lines.append(f"  {r.table:<16} {tag}  ({detail})")
        return "\n".join(lines)


#: Alternative field orders a game build may use, keyed by table.
#:
#: A game patch can REMOVE a field, and a removed field is the worst kind
#: of drift: the walker keeps reading, consumes the next field's bytes as
#: the missing one, and desyncs everything after it. CD 1.16 dropped two
#: ItemInfo fields and the walker fell from 110 of 113 fields to 10 --
#: the "11-field iteminfo grid" wall returning one version later.
#:
#: So the order is CHOSEN against the table in front of us, the same way
#: iteminfo's record layout and storeinfo's are. Each variant lists the
#: fields that build does not have; the base order is always a candidate
#: and only loses to a variant that decodes strictly better.
#:
#: Adding a build is one line here. Getting it wrong costs nothing: a
#: variant that does not decode better is never selected.
ORDER_VARIANTS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "ItemInfo": (
        # CD 1.16. What each removal is actually for:
        #
        # * _inventoryInfo (u16) is GONE -- absent from ItemInfo's field
        #   list in the 1.16 binary.
        # * _gimmickVisualPrefabDataList is GONE too -- likewise absent
        #   from the 1.16 binary, which merged it into PrefabData.
        # * _repairDataList and _prefabDataList still EXIST (they are
        #   fields 110 and 111 of 115 in the 1.16 binary). They are
        #   dropped here because 1.16 wrapped that whole region in 10
        #   leading and 18 trailing bytes, and a field-NAME list cannot
        #   express an opaque run -- the native parser handles it with
        #   two named opaque fields, which this mechanism has no way to
        #   name. Dropping them is what lets the walk continue past the
        #   wrapper instead of desyncing on it.
        #
        # Do NOT read this list as "1.16 removed four fields". Two were
        # removed; two are unreachable to a name-only order.
        ("cd116", ("_inventoryInfo", "_repairDataList",
                   "_prefabDataList", "_gimmickVisualPrefabDataList")),
    ),
}


#: Scoring walks every record, so remember the answer per table body.
#: Keyed on length + a cheap digest rather than the bytes themselves.
_ORDER_CACHE: dict[tuple[str, int, int], tuple[str, list[str]]] = {}


@dataclass(frozen=True)
class ValueAgreement:
    """How many decoded values an order gets *right*, not how far it walks.

    ``per_field`` is kept because the aggregate hides the interesting
    part: a field that agrees on zero records is a field being read from
    the wrong offset, and that is invisible in any total.
    """
    agreeing: int
    comparable: int
    per_field: dict[str, tuple[int, int]]

    @property
    def rate(self) -> float:
        return self.agreeing / self.comparable if self.comparable else 0.0

    def zero_agreement_fields(self) -> list[str]:
        return sorted(f for f, (ok, n) in self.per_field.items()
                      if n and not ok)


def _camel_to_snake(name: str) -> str:
    """``_maxEndurance`` -> ``max_endurance``.

    The walker names fields as the binary's reflection strings do; the
    native parser names them in Python style. Comparing the two decoders
    means bridging exactly that.
    """
    out: list[str] = []
    for i, ch in enumerate(name.lstrip("_")):
        if ch.isupper() and i:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def _iteminfo_oracle(body: bytes, header: bytes) -> dict[int, dict[str, Any]]:
    """``{entry key: {snake_field: value}}`` from the native iteminfo parser."""
    from cdumm.engine.iteminfo_native_parser import (
        detect_iteminfo_layout,
        parse_iteminfo_from_bytes,
    )
    _key_size, offsets = parse_pabgh_index(header, "iteminfo")
    if not offsets:
        return {}
    order = sorted(offsets.values())
    layout = detect_iteminfo_layout(body, order)
    truth: dict[int, dict[str, Any]] = {}
    for rec in parse_iteminfo_from_bytes(body, order, layout):
        if rec.get("_opaque_record"):
            continue        # carried verbatim; it decodes no values to check
        key = rec.get("key")
        if key is not None:
            truth[key] = rec
    return truth


#: table -> a second, independent decoder to check field values against.
#: Only tables that have one may have their field order chosen.
ORDER_ORACLES: dict[str, Any] = {"iteminfo": _iteminfo_oracle}


def _oracle_for(table: str):
    return ORDER_ORACLES.get(table.lower())


#: Records compared per order when choosing one. Selection asks which of
#: two orders agrees with the oracle *more*, and that margin is not close:
#: on live 1.16 it is 405,280 values against 23,116. Comparing all 6,581
#: records to re-establish that costs ~3s per distinct table body and buys
#: nothing -- an evenly spread few hundred settles it just as firmly, and
#: a field read from the wrong offset is wrong in every record, so the
#: zero-agreement check survives sampling intact.
AGREEMENT_SAMPLE = 400


def value_agreement(table: str, order: list[str], body: bytes, header: bytes,
                    truth: dict[int, dict[str, Any]],
                    sample: int | None = AGREEMENT_SAMPLE) -> ValueAgreement:
    """Count decoded values that match the oracle, walking as the GUI does.

    Deliberately drives ``decode_record_display`` -- the function the Game
    Data grid actually calls -- so the number measures the path being
    wired, not a private re-implementation of it.

    ``sample`` caps how many records are compared, spread evenly across
    the table rather than taken from the front (the first entries of a
    table are not representative of it). Pass ``None`` to compare every
    record.
    """
    from cdumm.semantic.parser import (
        _parse_entry_header,
        decode_record_display,
    )
    schema = _schema_in_order(table, order)
    key_size, offsets = parse_pabgh_index(header, table)
    hdr_order = table_header_order(body, offsets, key_size)
    entries = sorted(offsets.items(), key=lambda kv: kv[1])
    # A record ends where the NEXT one begins, so the boundary always comes
    # from the full list. Taking it from the sampled list instead would hand
    # every sampled record a too-generous end and let a runaway count look
    # survivable -- the sampling would then change the answer, not just the
    # cost of computing it.
    picked: list[int] | range = range(len(entries))
    if sample is not None and len(entries) > sample:
        stride = len(entries) / sample
        picked = [int(i * stride) for i in range(sample)]
    n_picked = len(picked)

    agreeing = comparable = undecodable = 0
    first_error: str | None = None
    per_field: dict[str, tuple[int, int]] = {}
    for i in picked:
        key, off = entries[i]
        want = truth.get(key)
        if want is None:
            continue
        end = entries[i + 1][1] if i + 1 < len(entries) else len(body)
        if off >= len(body):
            continue
        entry = body[off:end]
        if not schema.no_entry_header:
            _parse_entry_header(entry, 0, key_size, hdr_order)
        try:
            got = decode_record_display(entry, schema, key_size, hdr_order)
        except Exception as exc:                              # noqa: BLE001
            # An order that cannot decode a record earns nothing for it,
            # which is the scoring answer. But swallowing the reason in
            # silence is the habit this whole change exists to correct,
            # so it is counted and reported rather than dropped.
            undecodable += 1
            if first_error is None:
                first_error = f"{type(exc).__name__}: {exc}"
            continue
        for fname, value in got.items():
            if not isinstance(value, (int, str, float, bool)):
                continue
            ref = want.get(_camel_to_snake(fname))
            if not isinstance(ref, (int, str, float, bool)):
                continue
            ok, n = per_field.get(fname, (0, 0))
            hit = 1 if ref == value else 0
            per_field[fname] = (ok + hit, n + 1)
            agreeing += hit
            comparable += 1
    if undecodable:
        logger.debug(
            "%s: this order failed to decode %d of %d compared records "
            "(first: %s)", table, undecodable, n_picked, first_error)
    return ValueAgreement(agreeing, comparable, per_field)


def _variants_for(table: str) -> tuple[tuple[str, tuple[str, ...]], ...] | None:
    """``ORDER_VARIANTS`` lookup, case-insensitively.

    ``ORDER_VARIANTS`` is keyed by the schema's spelling (``"ItemInfo"``),
    but production reaches this through ``identify_table_from_path``, which
    yields the file stem -- ``"iteminfo"``. A bare ``in`` test therefore
    matched only in tests that happened to use the CamelCase spelling, so
    the whole mechanism was green and unreachable at the same time.
    ``get_schema`` already lowercases; this now matches it.
    """
    want = table.lower()
    for key, variants in ORDER_VARIANTS.items():
        if key.lower() == want:
            return variants
    return None


def select_order(table: str, body: bytes, header: bytes) -> tuple[str, list[str]]:
    """``(label, order)`` -- the declared order, or a variant that beats it.

    Returns ``("base", ...)`` when the table declares no variants or none
    of them decodes better, so this can be called unconditionally.
    """
    if _variants_for(table) is None:
        return "base", verified_order(table)

    ck = (table, len(body), hash(body[:4096]) ^ hash(body[-4096:]))
    hit = _ORDER_CACHE.get(ck)
    if hit is not None:
        return hit
    got = _select_order_uncached(table, body, header)
    _ORDER_CACHE[ck] = got
    return got


def _select_order_uncached(table: str, body: bytes,
                           header: bytes) -> tuple[str, list[str]]:
    """Pick a field order by how many decoded VALUES the oracle confirms.

    Walk depth used to decide this, and walk depth is blind to the error
    class that matters. On live 1.16 the declared order scores a median
    109 of 109 fields at 87.5% full depth while ``_respawnTimeSeconds``
    and ``_maxEndurance`` agree with the native parser on **zero** of
    5,756 records -- the walker lands both 10 bytes off, inside the
    opaque run, and consumes exactly the same number of bytes doing it.
    ``decode_score`` cannot see that, because the byte arithmetic is
    identical either way.

    So the arbiter is now the native parser's values. It is a real
    oracle: #336 established it byte-exact against the whole table, and
    the same oracle is what settled where the 1.16 10-byte run goes.

    A table with no oracle gets **no variant at all**. Preferring a
    deeper walk on faith is what produced the situation above, and there
    is no way to check the answer without a second decoder to check it
    against.
    """
    base = verified_order(table)
    variants = _variants_for(table)
    if not base or not variants:
        return "base", base

    oracle = _oracle_for(table)
    if oracle is None:
        logger.info(
            "%s: declares order variants but has no value oracle; keeping "
            "the declared order rather than choosing on walk depth alone",
            table)
        return "base", base

    try:
        truth = oracle(body, header)
    except Exception as exc:                                  # noqa: BLE001
        logger.warning("%s: value oracle failed (%s); keeping the declared "
                       "order", table, exc)
        return "base", base
    if not truth:
        return "base", base

    scored = [("base", base, value_agreement(table, base, body, header, truth))]
    for label, dropped in variants:
        order = [f for f in base if f not in dropped]
        scored.append(
            (label, order, value_agreement(table, order, body, header, truth)))

    best = max(s[2].agreeing for s in scored)
    winners = [s for s in scored if s[2].agreeing == best]
    if len(winners) > 1:
        # Ambiguity is refusal, not first-fit. The declared order is the
        # one that was actually verified against bytes, so it keeps the
        # benefit of the doubt.
        logger.warning(
            "%s: %d field orders agree with the oracle on %d values "
            "(%s); refusing to choose and keeping the declared order",
            table, len(winners), best, ", ".join(w[0] for w in winners))
        return "base", base

    label, order, score = winners[0]
    base_score = scored[0][2]
    if label == "base" or score.agreeing <= base_score.agreeing:
        return "base", base

    logger.info(
        "%s: field order %r confirms %d of %d decoded values against the "
        "native parser, against %d for the declared order; using it",
        table, label, score.agreeing, score.comparable, base_score.agreeing)

    # A field that agrees on NO record is not noise, it is a field being
    # read from the wrong offset -- the winning order still shows it in
    # the grid, so say so rather than let it pass as data. On 1.16 this
    # names _maxEndurance and _respawnTimeSeconds, which straddle the
    # opaque run a name-only order cannot express; the declared order
    # gets them wrong on 1.13 too, so this is long-standing rather than
    # something the variant introduces.
    broken = score.zero_agreement_fields()
    if broken:
        logger.warning(
            "%s: %d field(s) match the native parser on no record at all "
            "and should not be trusted in the grid: %s",
            table, len(broken), ", ".join(broken))
    return label, order


def schema_for_table(table: str, body: bytes, header: bytes) -> TableSchema:
    """``get_schema(table)``, but with the field order that actually fits.

    Drop-in for ``get_schema`` at any call site that has the table bytes.
    Identical to ``get_schema`` for every table with no variants declared,
    and for any build the declared order already fits.
    """
    label, order = select_order(table, body, header)
    if label == "base":
        return get_schema(table)
    return _schema_in_order(table, order)


def verify_order_source(
    candidate: dict[str, list[str]],
    fixtures: dict[str, tuple[bytes, bytes]] | None = None,
) -> VerificationReport:
    """Check ``candidate`` against every table with a verified order.

    ``fixtures`` maps table name -> (pabgb bytes, pabgh bytes); where one
    is present the stronger byte-decode check runs as well as the order
    check. Tables with no fixture get the order check only.
    """
    fixtures = fixtures or {}
    parser_mod._loaded_schemas = None      # one pristine load for the run
    report = VerificationReport()
    for table in tables_with_verified_order():
        truth = verified_order(table)
        cand = candidate.get(table)
        if cand is None:
            report.results.append(
                TableResult(table, False, None, None, None, None))
            continue

        order_matches = cand == truth
        baseline = candidate_score = decode_ok = None
        if table in fixtures:
            body, header = fixtures[table]
            baseline = decode_score(table, truth, body, header)
            candidate_score = decode_score(table, cand, body, header)
            decode_ok = candidate_score.at_least(baseline)

        report.results.append(TableResult(
            table=table,
            covered=True,
            order_matches=order_matches,
            baseline=baseline,
            candidate=candidate_score,
            decode_ok=decode_ok,
        ))
    return report


# ── superset candidates ──────────────────────────────────────────────────
#
# `verify_order_source` compares with `cand == truth`, which is the right
# test for a candidate that claims to be a table's COMPLETE order. Some
# sources are not that shape. A reflection-derived order names every field
# the binary has a read-error string for, which can be more fields than the
# shipped schema models (CharacterInfo: 190 named vs 164 in the schema) and
# fewer than it in the other direction (9 CharacterInfo fields have no error
# string at all). It also uses the binary's real names where the schema
# still carries hand-written placeholders (`_characterName` vs `_stringKey2`).
#
# Exact equality rejects such a source for being differently-shaped rather
# than for being wrong, which tells you nothing. The test that does carry
# information is whether the two agree on the fields they BOTH name, in
# order. That still catches the failure that matters -- a field in the wrong
# slot -- while not penalising a source for knowing more or less.
#
# This is deliberately weaker than `verify_order_source` and does not
# replace it. A superset source earns the right to CORROBORATE a verified
# order, or to supply order for tables that have none; it does not earn
# `_ordered_fields` on its own, because the fields it omits still have to
# be placed by something.


@dataclass
class RelativeOrderResult:
    table: str
    shared: list[str]              # fields both orders name, verified order
    candidate_sequence: list[str]  # the same fields, in candidate order
    first_divergence: int | None   # index into `shared`, None if identical
    candidate_only: list[str]      # named by candidate, absent from verified
    verified_only: list[str]       # in verified order, candidate never names

    @property
    def matches(self) -> bool:
        return self.first_divergence is None

    @property
    def complete(self) -> bool:
        """True when the candidate names every verified field.

        A candidate that matches but is not complete cannot become
        ``_ordered_fields`` by itself: the fields in ``verified_only`` have
        no position from this source.
        """
        return not self.verified_only

    def summary(self) -> str:
        tag = "MATCH" if self.matches else f"DIVERGES@{self.first_divergence}"
        return (f"{self.table:<16} {tag}  shared={len(self.shared)}"
                f" cand_only={len(self.candidate_only)}"
                f" unplaced={len(self.verified_only)}")


def relative_order_matches(table: str, candidate: list[str]
                           ) -> RelativeOrderResult:
    """Check ``candidate`` against ``table``'s verified order, on shared names.

    Raises ``KeyError`` if ``table`` has no verified order to check against.
    """
    truth = verified_order(table)
    if not truth:
        raise KeyError(f"{table} has no verified _ordered_fields")
    pos = {f: i for i, f in enumerate(candidate)}
    tset = set(truth)
    shared = [f for f in truth if f in pos]
    seq = sorted(shared, key=lambda f: pos[f])
    div = next((i for i, (a, b) in enumerate(zip(shared, seq)) if a != b),
               None)
    return RelativeOrderResult(
        table=table,
        shared=shared,
        candidate_sequence=seq,
        first_divergence=div,
        candidate_only=[f for f in candidate if f not in tset],
        verified_only=[f for f in truth if f not in pos],
    )


def verify_order_source_relative(candidate: dict[str, list[str]]
                                 ) -> list[RelativeOrderResult]:
    """``relative_order_matches`` over every table with a verified order.

    Tables the candidate does not cover are omitted rather than failed --
    coverage is reported by the caller, which can weigh it separately.
    """
    parser_mod._loaded_schemas = None
    out = []
    for table in tables_with_verified_order():
        cand = candidate.get(table)
        if not cand:
            continue
        out.append(relative_order_matches(table, cand))
    return out


# ── does the shipped schema actually FRAME this table's records? ──────────
#
# Distinct from everything above, and needed for a different reason. The
# checks above ask whether a candidate ORDER is trustworthy. This asks a
# blunter question about a table the app is about to display: does the
# schema account for every byte of a record, on the build in front of us?
#
# It matters because the answer is often no. Measured on game buildid
# 24613230, walking all 135 tables in the install through get_schema() and
# _consume_field_bytes: 49 tile exactly, 4 partially, and 38 tables whose
# ONLY decoder is the generic schema frame no record correctly at all --
# 61,795 records' worth. Those are pre-existing base-schema entries whose
# widths do not add up on this build.
#
# Nothing there is editable: _editable_columns refuses every field when
# `verified_fields is None`, which is the case for a base-only table. So it
# is not dangerous. But the grid still renders, and a grid built from a
# layout that is known-wrong the moment anyone checks reads as data. That is
# what this exists to label.

@dataclass(frozen=True)
class LayoutFit:
    """How much of a table the schema the GRID USES actually accounts for.

    Two different properties, and conflating them is a trap I walked into
    while writing this. ``exact`` is byte-exact tiling -- the walk consumed
    the record to its last byte. That is the right gate for DERIVING and
    SHIPPING a layout, and it is what the whole derivation pipeline is held
    to. It is the WRONG test for "should the user trust this grid",
    because a schema that models the first N fields correctly and leaves
    unmodelled tail bytes shows N correct columns and still fails it.
    Measured: iteminfo, characterinfo, stageinfo, regioninfo and storeinfo
    all tile 0 of 200 sampled records while displaying perfectly useful
    columns.

    ``complete`` is the display-relevant one: did the walk get through the
    schema's whole field list without a field failing to consume? If it
    dies at field 2 of 15, every column after that is meaningless, and THAT
    is what deserves a warning.
    """

    records: int          # records in the table
    checked: int          # how many were sampled
    complete: int         # of those, how many walked the entire field list
    exact: int            # of those, how many ALSO ended on the last byte
    schema_fields: int
    median_fields: int    # typical number of fields the walk gets through

    @property
    def ratio(self) -> float:
        return (self.complete / self.checked) if self.checked else 0.0

    @property
    def usable(self) -> bool:
        """The walk finishes the field list on every sampled record."""
        return self.checked > 0 and self.complete == self.checked

    @property
    def broken(self) -> bool:
        """The walk finishes on NO sampled record.

        The strong signal: the schema cannot frame this build's data at all,
        so any column past the failure point is noise. Deliberately not
        "some records failed" -- a layout that works on most records and
        stumbles on an edge case still shows a useful grid.
        """
        return self.checked > 0 and self.complete == 0


def layout_fit(table: str, body: bytes, header: bytes,
               max_samples: int = 200) -> LayoutFit | None:
    """Sample records and report how far the display schema actually gets.

    Sampled rather than exhaustive so this is cheap enough for the display
    path: actionpointinfo has 28,958 records and the answer does not get
    more true after the two-hundredth. Samples are spread evenly rather than
    taken from the front, because the first records of a table are the least
    representative -- a layout that is wrong about a variable-length field
    often survives the short early ones.

    Returns None when the question cannot be asked (no schema, no index),
    which the caller must treat as "unknown", not as "broken".
    """
    try:
        # schema_for_table, NOT get_schema. This must measure the schema the
        # GRID ACTUALLY USES: parse_records_display calls schema_for_table,
        # which picks the field-order variant that fits this build.
        schema = schema_for_table(table, body, header)
        if schema is None or not schema.fields:
            return None
        key_size, offsets = parse_pabgh_index(header, table)
    except Exception:                                       # noqa: BLE001
        return None
    if not offsets:
        return None
    hdr_order = table_header_order(body, offsets, key_size)
    ent = sorted(offsets.items(), key=lambda kv: kv[1])
    n = len(ent)
    step = max(1, n // max_samples)
    idxs = list(range(0, n, step))[:max_samples]
    complete = exact = 0
    depths: list[int] = []
    for i in idxs:
        off = ent[i][1]
        end = ent[i + 1][1] if i + 1 < n else len(body)
        try:
            p = _payload_offset(body, off, key_size,
                                no_null_skip=schema.no_null_skip,
                                no_entry_header=schema.no_entry_header,
                                header_order=hdr_order)
        except Exception:                                   # noqa: BLE001
            p = None
        if p is None:
            depths.append(0)
            continue
        walked = 0
        for f in schema.fields:
            try:
                c = _consume_field_bytes(body, p, f, end)
            except Exception:                               # noqa: BLE001
                c = None
            if c is None:
                break
            p += c
            walked += 1
        depths.append(walked)
        if walked == len(schema.fields):
            complete += 1
            if p == end:
                exact += 1
    return LayoutFit(records=n, checked=len(idxs), complete=complete,
                     exact=exact, schema_fields=len(schema.fields),
                     median_fields=int(median(depths)) if depths else 0)
