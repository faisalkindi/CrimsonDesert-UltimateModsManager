#!/usr/bin/env python3
"""Coverage scanner: which Format-3 mod intents can CDUMM not apply yet?

Runs every intent of every Format-3 mod under the given paths through
``validate_intents`` -- the exact classifier the import path uses -- and
reports the intents it *skips*, grouped by ``(table, field)`` with the
engine's own reason.

Crucially this needs **no game install**: ``validate_intents`` decides
coverage from the shipped ``schemas/pabgb_complete_schema.json`` +
``field_schema/`` + the registered ``LIST_WRITERS``. So it can run in CI
over a corpus of top mods and surface a new gap the moment a game patch or
a new mod introduces one -- finding gaps before users report them, which
is exactly what the #285 gap scan did by hand.

Usage::

    python scripts/coverage_scan.py <dir-or-file> [more paths ...]

Every ``*.json`` / ``*.field.json`` under each directory is scanned; files
that aren't Format-3 mods (PAZ ``modinfo.json``, v2 byte-patches, etc.) are
skipped silently. The exit code is the number of distinct ``(table, field)``
gaps found (0 = everything covered), so CI can gate on it.
"""
from __future__ import annotations

import collections
import re
import sys
from pathlib import Path

# Allow running straight from a checkout without an editable install.
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cdumm.engine.format3_handler import (  # noqa: E402
    parse_format3_mod_targets, validate_intents, _table_name_from_target,
)

Gap = collections.namedtuple("Gap", "table field reason mod")


def _base_field(field: str) -> str:
    """Strip list indices / nested paths: ``entries[0].etl_hashes`` -> ``entries``."""
    return re.split(r"[.\[]", field, maxsplit=1)[0]


def scan_file(path: Path) -> list[Gap]:
    """Return one Gap per skipped intent in a single Format-3 mod file.

    Returns [] for anything that isn't a parseable Format-3 mod.
    """
    try:
        pairs = parse_format3_mod_targets(path)
    except Exception:
        return []  # not a Format-3 mod (PAZ metadata, v2 patch, malformed)
    gaps: list[Gap] = []
    for target, intents in pairs:
        try:
            result = validate_intents(target, intents)
        except Exception:
            continue
        table = _table_name_from_target(target)
        for intent, reason in result.skipped:
            gaps.append(Gap(table, _base_field(intent.field), reason, path.name))
    return gaps


def iter_mod_files(paths: list[str]):
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            yield p
        elif p.is_dir():
            yield from sorted(p.rglob("*.json"))


def scan(paths: list[str]) -> list[Gap]:
    gaps: list[Gap] = []
    for f in iter_mod_files(paths):
        gaps.extend(scan_file(f))
    return gaps


def main(argv: list[str]) -> int:
    paths = argv[1:]
    if not paths:
        print(__doc__)
        return 2

    gaps = scan(paths)
    by_key: collections.Counter = collections.Counter()
    mods: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
    reason: dict[tuple[str, str], str] = {}
    for g in gaps:
        key = (g.table, g.field)
        by_key[key] += 1
        mods[key].add(g.mod)
        reason.setdefault(key, g.reason)

    if not by_key:
        print("No uncovered Format-3 intents found across the scanned mods.")
        return 0

    print("Uncovered (table.field): intents  mods")
    print("-" * 60)
    for (table, field), n in by_key.most_common():
        ms = ", ".join(sorted(mods[(table, field)])[:5])
        print(f"  {table}.{field}: {n}  [{ms}]")
        print(f"      reason: {reason[(table, field)]}")
    return len(by_key)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
