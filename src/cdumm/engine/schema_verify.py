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
   spot: two fields of the same width, swapped upstream of the point where
   the walker stalls, decode identically — the byte count doesn't move.
   Order-identity is what catches those. The decode score's sharpness also
   scales with how far the walker reaches (it stalls early on some tables
   until their nested types are modelled), so it is reported, weighed, but
   never trusted alone.

Order-identity is the workhorse; the decode score keeps it honest. A
source that fails EITHER on any known table is rejected. A source that
passes all of them has earned the benefit of the doubt on the unknown
ones — and even then each new table stays gated to ``verified_fields``
until its values are cross-checked against real records.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

from cdumm.engine.format3_apply import _consume_field_bytes, _payload_offset
from cdumm.semantic import parser as parser_mod
from cdumm.semantic.parser import TableSchema, get_schema, parse_pabgh_index

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
                             no_entry_header=schema.no_entry_header)
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
