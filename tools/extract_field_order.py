"""Extract per-table field ORDER from a Crimson Desert binary's reflection
error strings, and verify it against the tables CDUMM already knows.

Method (from NattKh's PABGB_DECODE_PROCESS.md, MPL-2.0): the game emits a
Korean read-error string per field, `<Class>의 _<field>를 읽어들이는데
실패했다` ("Failed to read _<field> of <Class>"). On the **unstripped
macOS** binary these appear in field READ ORDER, so scanning them per class
yields the on-disk field order for every table — the exact thing the
shipped memory-order schema lacks.

    python tools/extract_field_order.py  path/to/CrimsonDesert_Steam-macos

IMPORTANT — which binary:
  * macOS (`CrimsonDesert_Steam-*`): strings are in READ ORDER. Use this.
  * Windows (`CrimsonDesert.exe`): the SAME strings exist and give correct
    field *membership*, but NOT order (the string table is laid out in a
    different order than the reader calls them). This script will WARN and
    still run, but the order it produces from a Windows binary will fail
    verification — that's expected, not a bug.

Whatever binary you give it, the output is run through
`cdumm.engine.schema_verify.verify_order_source`: the extracted order for
every table CDUMM already has a verified `_ordered_fields` for must match
exactly (and decode the committed fixture). If it fails the 7 known
tables, the extraction is not trustworthy on the unknown ~82 and the
script says so, loudly, instead of emitting a schema that would silently
corrupt tables.
"""
from __future__ import annotations

import re
import sys
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cdumm.engine.schema_verify import (  # noqa: E402
    tables_with_verified_order, verified_order, verify_order_source)

# <Class>의 ... _<field>를   —  의 = \xec\x9d\x98, 를 = \xeb\xa5\xbc
_PAIR = re.compile(
    rb"([A-Za-z][A-Za-z0-9]{1,60})\xec\x9d\x98"    # ClassName + 의
    rb".{0,48}?"                                    # (particles / spaces)
    rb"(_[A-Za-z][A-Za-z0-9]{0,60})\xeb\xa5\xbc",   # _fieldName + 를
    re.DOTALL)


def extract(binary: bytes) -> "dict[str, list[str]]":
    """Return {ClassName: [field, ...]} in the binary's string order,
    de-duplicated (first occurrence wins)."""
    out: "OrderedDict[str, list[str]]" = OrderedDict()
    for m in _PAIR.finditer(binary):
        cls = m.group(1).decode("ascii", "replace")
        fld = m.group(2).decode("ascii", "replace")
        fields = out.setdefault(cls, [])
        if fld not in fields:
            fields.append(fld)
    return dict(out)


# Reflection class name -> pabgb table name. The reflection classes are
# PascalCase (`ItemInfo`); the tables/overrides use the same names, so this
# is identity for the verified set. Kept explicit so a future rename is one
# edit, not a scattered assumption.
def _table_for_class(cls: str) -> str:
    return cls


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    if len(argv) != 2:
        print("usage: python tools/extract_field_order.py <binary-path>")
        return 2
    path = Path(argv[1])
    data = path.read_bytes()
    print(f"binary: {path}  ({len(data):,} bytes)")

    if data[:2] == b"MZ":
        print("!! This looks like the WINDOWS exe. Its error strings give "
              "field membership but NOT read order — verification below is "
              "EXPECTED to fail. Use the macOS binary for real order.\n")

    by_class = extract(data)
    print(f"reflection classes with field strings: {len(by_class)}")

    # Map to table-name keyed candidate, restricted to classes we can name.
    candidate = {_table_for_class(c): f for c, f in by_class.items()}

    report = verify_order_source(candidate)
    print("\n" + report.summary())

    known = set(tables_with_verified_order())
    print(f"\nextracted orders for {len(candidate)} classes; "
          f"{len(known & candidate.keys())}/{len(known)} verified tables "
          f"covered.")

    if report.trustworthy:
        print("\n✅ VERIFIED: extraction reproduces every known table. The "
              "orders for the OTHER classes can be trusted enough to add "
              "(still gate each to verified_fields after a value spot-check).")
        # show a few unknown tables now unlocked, as a preview
        newly = [t for t in sorted(candidate) if t not in known][:15]
        print("   sample of newly-orderable classes:", newly)
        return 0

    print("\n❌ NOT VERIFIED: the extracted order does not reproduce the "
          "known tables, so it is NOT safe to use for the unknown ones.")
    for r in report.results:
        if r.covered and not r.passed:
            truth = verified_order(r.table)
            cand = candidate.get(r.table, [])
            # first divergence, to make debugging concrete
            div = next((i for i, (a, b) in enumerate(zip(truth, cand))
                        if a != b), min(len(truth), len(cand)))
            print(f"   {r.table}: first divergence at index {div}")
            print(f"     known    [{div}:{div+4}] = {truth[div:div+4]}")
            print(f"     extracted[{div}:{div+4}] = {cand[div:div+4]}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
