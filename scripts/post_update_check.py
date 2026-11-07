#!/usr/bin/env python3
"""Post-update canary: did a game patch break CDUMM's readers?

Run this after every Crimson Desert update, before anyone reports a mod
doing nothing. It reads the installed game and answers one question per
table: *can CDUMM still decode what mods target?*

Why this exists
---------------
Almost every release in this project's history is reactive -- a patch moves
the bytes, mods silently stop applying, a user reports it days later, and
the layout gets re-derived under time pressure. The information needed to
catch it arrives the moment the patch lands; nothing was looking.

CI cannot look: it has no game install. So this is the thing a human runs
on a machine that has one, and its exit code is the number of problems so
it can gate a release.

Design rule: PRODUCTION ENTRY POINTS ONLY
-----------------------------------------
Every check drives the same call the apply path drives. That rule is not
decoration -- it is the single most expensive mistake in this codebase's
history, made at least four times:

  * #351/#352 drove ``parse_stock_list`` with a constant instead of
    ``locate_stock_list``, read the resulting garbage as a format
    regression, and cost two people a day before both retracted it.
  * measuring skill with ``parse_skill_entry`` instead of ``parse_all``
    skips ``_detect_is_no_alert`` entirely and reports 1424/2013 on a
    build that actually reads 2013/2013 (#355).
  * scoring a table with ``verified_order`` instead of ``select_order``
    skips variant selection and reports iteminfo at 10 fields of 113
    instead of 109.

An internal function fed something it is not meant to receive returns
plausible garbage, and plausible garbage looks exactly like drift.

What a failure here means
-------------------------
These readers refuse rather than write to a position they cannot account
for, so the failure mode is "mods report no changes", not "saves get
damaged". That is why this is worth catching early: the symptom is silent.

Usage::

    python scripts/post_update_check.py --game-dir "E:/.../Crimson Desert"
    python scripts/post_update_check.py --game-dir <dir> --baseline
"""
from __future__ import annotations

import argparse
import sys
import zlib
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))


def _load(game_dir: Path, name: str) -> bytes:
    """A table's bytes out of the install, via the path the writer uses."""
    from cdumm.engine.v2_to_format3 import _load_vanilla_table
    return _load_vanilla_table(game_dir, name)


def _fixture(version: str, name: str) -> bytes | None:
    p = _REPO / "tests" / "fixtures" / version / (name + ".zlib")
    if not p.exists():
        return None
    return zlib.decompress(p.read_bytes())


# ── per-table checks, each through the production path ────────────────────

def check_skill(body: bytes, header: bytes) -> tuple[bool, str]:
    """``parse_all`` -- which is what runs the layout detection."""
    from cdumm._vendor import skillinfo_parser as sp
    sp._type_id_sizes.clear()
    entries = sp.parse_all(header, body)
    index = sp.parse_skill_pabgh(header)
    bounds = [o for _k, o in index] + [len(body)]
    total = len(index)
    rt = sum(1 for i, rec in enumerate(entries)
             if sp.serialize_entry(rec) == body[index[i][1]:bounds[i + 1]])
    structured = sum(1 for e in entries
                     if e.get("_buffLevelList") is not None)
    ok = len(entries) == total and rt == total
    return ok, (f"{len(entries)}/{total} parsed, {rt}/{total} byte-exact, "
                f"buff walk structured on {structured} "
                f"({structured / max(total, 1):.0%}), "
                f"_isNoAlert={sp._has_is_no_alert}")


def check_storeinfo(body: bytes, header: bytes) -> tuple[bool, str]:
    """``locate_stock_list`` -- no offset constant, the real mechanism."""
    from cdumm.engine.storeinfo_native_parser import (
        StoreListNotFound,
        _entry_payload,
        detect_storeinfo_layout,
        locate_stock_list,
        serialize_stock_list,
    )
    from cdumm.semantic.parser import parse_pabgh_index
    from cdumm.semantic.record_header import table_header_order
    ks, offs = parse_pabgh_index(header, "storeinfo")
    order = table_header_order(body, offs, ks)
    starts = sorted(offs.values())
    spans = starts + [len(body)]
    # Takes the ENTRY OFFSETS, not the header bytes. Passing the header
    # here silently picks a wrong layout instead of raising.
    layout = detect_storeinfo_layout(body, starts, order)
    located = records = empty = not_found = bad_rt = 0
    for key, off in offs.items():
        end = spans[spans.index(off) + 1]
        try:
            recs, s, e = locate_stock_list(
                body, _entry_payload(body, off, order), end, key, layout)
        except StoreListNotFound as exc:
            if "too" in str(exc) or "provably" in str(exc):
                empty += 1
            else:
                not_found += 1
            continue
        except Exception:                       # noqa: BLE001
            not_found += 1
            continue
        located += 1
        records += len(recs)
        if serialize_stock_list(recs, layout) != body[s:e]:
            bad_rt += 1
    ok = not_found == 0 and bad_rt == 0 and located + empty == len(offs)
    return ok, (f"layout {layout.label!r}: {located} located + {empty} "
                f"provably empty = {located + empty}/{len(offs)}, "
                f"{records} stock records, {not_found} not-found, "
                f"{bad_rt} mis-round-tripped")


def check_statusgroupinfo(body: bytes, header: bytes) -> tuple[bool, str]:
    from cdumm.engine.statusgroupinfo_writer import parse_record_full
    from cdumm.semantic.parser import parse_pabgh_index
    from cdumm.semantic.record_header import table_header_order
    ks, offs = parse_pabgh_index(header, "statusgroupinfo")
    order = table_header_order(body, offs, ks)
    starts = sorted(offs.values())
    ok_n = 0
    widths: set[int] = set()
    for o in offs.values():
        i = starts.index(o)
        end = starts[i + 1] if i + 1 < len(starts) else len(body)
        got = parse_record_full(body, o, end, order)
        if got is None:
            continue
        ok_n += 1
        widths.add(got[1][0][1])
    return ok_n == len(offs), (
        f"{ok_n}/{len(offs)} records parse, reverse-index width "
        f"{sorted(widths) or 'n/a'}")


#: Walk depth each ordered table reached when this canary was last
#: verified, as ``(order_label, median_fields)``.
#:
#: Pinned deliberately, because the useful question is "did this CHANGE",
#: not "is this complete". Several of these tables have never walked to the
#: end -- StageInfo reaches 25 of 81 fields and RegionInfo stalls on
#: ``_gimmickAliasPointerList`` -- and those are open modelling gaps, not
#: patch damage. An absolute threshold would print three failures on a
#: perfectly healthy build, and a check that is red every run is one people
#: stop reading. (Same reasoning as the pinned ruff version in
#: .github/workflows/windows-tests.yml.)
#:
#: A DROP is the signal. An improvement is reported and is also worth
#: knowing, since it means someone widened a layout and this line is stale.
#:
#: Verified 2026-08-11, fingerprint 2471644ba4ce9feb.
_ORDER_BASELINE: dict[str, tuple[str, float]] = {
    "ItemInfo": ("cd116", 109),
    "CharacterInfo": ("base", 14),
    "RegionInfo": ("base", 21),
    # Was 25. The 26 Aug 2026 patch (b24934353) moved something in the
    # never-fully-modelled region: the stall shifted from _playCondition
    # to _closeCondition and the median slipped one field. Investigated
    # under GitHub #377 -- StageInfo has never reached a usable depth
    # (0% of records complete either way), is not editable, and no mod
    # targets it, so this is a modelling-gap shift rather than a
    # capability loss. Re-pinned so the canary guards the NEXT drop.
    "StageInfo": ("base", 24),
    "VehicleInfo": ("base", 20),
    "FieldInfo": ("base", 19),
    # Was 2. #362's derived layouts took it to 3/3 on 100% of 35 records,
    # so the floor moves up with it -- a baseline left below the current
    # depth stops detecting the next regression above it.
    "WantedInfo": ("base", 3),
}


def check_ordered_table(table: str, body: bytes,
                        header: bytes) -> tuple[bool, str]:
    """``select_order`` -- which applies the per-build variant.

    Judged against ``_ORDER_BASELINE`` rather than an absolute bar: a
    regression means a patch moved something, while a table that has always
    stalled early is a known gap and must not read as breakage.
    """
    from cdumm.engine.schema_verify import decode_score, select_order
    label, order = select_order(table, body, header)
    s = decode_score(table, order, body, header)
    detail = (f"order {label!r}: median {s.median_fields:g}/{len(order)} "
              f"fields, {s.frac_reached_last:.0%} of {s.records} records "
              f"complete"
              + (f", stalls on {s.first_bail_field}"
                 if s.first_bail_field else ""))

    want = _ORDER_BASELINE.get(table)
    if want is None:
        return True, detail + "   [no baseline pinned]"
    want_label, want_median = want
    if s.median_fields < want_median:
        return False, (detail + f"   REGRESSED: was median {want_median:g} "
                                f"on order {want_label!r}")
    if label != want_label:
        return False, (detail + f"   LAYOUT CHANGED: baseline selected "
                                f"{want_label!r}")
    if s.median_fields > want_median:
        detail += (f"   (improved from {want_median:g} -- update "
                   f"_ORDER_BASELINE)")
    return True, detail


def check_build_layouts(game_dir: Path) -> tuple[bool, str]:
    """``schema_for_table`` on every table a shipped build layout covers.

    That is the Game Data grid's own call. On game 2.04 it hands a
    name-first table its per-build layout when the bytes are the ones the
    layout was proven on, or when every record still tiles. A name-first
    table that gets no layout is the regression: the grid falls back to
    the pre-2.04 layout there, which reads the wrong bytes. Key-first
    tables (any pre-2.04 build) are counted and skipped.

    Tiling can't see same-width fields swapped, so "fits by tiling" on a
    new build says the offsets still add up, not that the order held.
    """
    from cdumm.engine.schema_verify import schema_for_table
    from cdumm.semantic import build_layouts
    from cdumm.semantic.parser import parse_pabgh_index
    from cdumm.semantic.record_header import NAME_FIRST, table_header_order
    stems = sorted({s for b in build_layouts.builds() for s in b.tables})
    if not stems:
        return False, "no build layouts shipped"
    pinned = tiled = key_first = 0
    lost: list[str] = []
    missing: list[str] = []
    builds: set[str] = set()
    for stem in stems:
        try:
            body = _load(game_dir, stem + ".pabgb")
            header = _load(game_dir, stem + ".pabgh")
        except Exception:                       # noqa: BLE001
            missing.append(stem)
            continue
        key_size, offsets = parse_pabgh_index(header, stem)
        if table_header_order(body, offsets, key_size) != NAME_FIRST:
            key_first += 1
            continue
        schema = schema_for_table(stem, body, header)
        if schema is None or schema.build is None:
            lost.append(stem)
            continue
        builds.add(schema.build)
        if _proven(stem, body, header):
            pinned += 1
        else:
            tiled += 1
    detail = (f"{len(stems)} tables: {pinned} byte-identical to the "
              f"{'/'.join(sorted(builds)) or '-'} layouts' own, {tiled} fit "
              f"by tiling, {len(lost)} name-first with no fitting layout, "
              f"{key_first} key-first (pre-2.04), {len(missing)} not found")
    if lost:
        detail += f"   LOST: {', '.join(lost[:8])}"
    if missing:
        detail += f"   MISSING: {', '.join(missing[:8])}"
    return not lost, detail


def _proven(stem: str, body: bytes, header: bytes) -> bool:
    """Both files are the ones a shipped layout was proven on."""
    import hashlib

    from cdumm.semantic import build_layouts
    pair = (hashlib.sha256(body).hexdigest(),
            hashlib.sha256(header).hexdigest())
    return any(stem in b.tables
               and pair == (b.tables[stem].pabgb_sha256,
                            b.tables[stem].pabgh_sha256)
               for b in build_layouts.builds())


#: (label, loader-name, check). Loader-name is the .pabgb basename; the
#: .pabgh is derived. `None` body means the check loads its own.
_CHECKS = [
    ("skill", "skill", check_skill),
    ("storeinfo", "storeinfo", check_storeinfo),
    ("statusgroupinfo", "statusgroupinfo", check_statusgroupinfo),
]

#: Tables with a verified field order, checked through select_order.
_ORDERED = [("iteminfo", "ItemInfo"), ("characterinfo", "CharacterInfo"),
            ("regioninfo", "RegionInfo"), ("stageinfo", "StageInfo"),
            ("vehicleinfo", "VehicleInfo"), ("fieldinfo", "FieldInfo"),
            ("wantedinfo", "WantedInfo")]


def build_stamp(game_dir: Path) -> str:
    from cdumm.engine.version_detector import detect_game_version
    exe = game_dir / "bin64" / "CrimsonDesert.exe"
    fp = detect_game_version(game_dir) or "?"
    when = ""
    if exe.exists():
        import datetime as _dt
        when = _dt.datetime.fromtimestamp(
            exe.stat().st_mtime, tz=_dt.timezone.utc
        ).astimezone().strftime("%Y-%m-%d %H:%M")
    return f"fingerprint {fp}   exe {when}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--game-dir", required=True, type=Path)
    ap.add_argument("--baseline", action="store_true",
                    help="also run the committed fixtures, so a live "
                         "failure can be told apart from a code regression")
    args = ap.parse_args(argv)
    game = args.game_dir
    if not (game / "bin64").is_dir():
        print(f"not a Crimson Desert install: {game}")
        return 2

    print(f"game: {game}")
    print(f"      {build_stamp(game)}")
    print()
    problems: list[str] = []

    def run(label: str, fn, body, header) -> None:
        try:
            ok, detail = fn(body, header)
        except Exception as e:                  # noqa: BLE001
            print(f"  {label:<18} ERROR  {type(e).__name__}: {str(e)[:70]}")
            problems.append(f"{label}: {type(e).__name__}")
            return
        print(f"  {label:<18} {'ok  ' if ok else 'FAIL'}  {detail}")
        if not ok:
            problems.append(f"{label}: {detail}")

    print("live install:")
    for label, name, fn in _CHECKS:
        try:
            body = _load(game, name + ".pabgb")
            header = _load(game, name + ".pabgh")
        except Exception as e:                  # noqa: BLE001
            print(f"  {label:<18} ERROR  cannot load: {str(e)[:60]}")
            problems.append(f"{label}: load failed")
            continue
        run(label, fn, body, header)

    for name, cls in _ORDERED:
        try:
            body = _load(game, name + ".pabgb")
            header = _load(game, name + ".pabgh")
        except Exception as e:                  # noqa: BLE001
            print(f"  {name:<18} ERROR  cannot load: {str(e)[:60]}")
            problems.append(f"{name}: load failed")
            continue
        run(name, lambda b, h, _c=cls: check_ordered_table(_c, b, h),
            body, header)

    run("2.04 layouts", lambda _b, _h: check_build_layouts(game), None, None)

    if args.baseline:
        print("\ncommitted fixtures (a failure here is a CODE regression, "
              "not a game patch):")
        for ver in ("vanilla113", "vanilla115", "vanilla116"):
            for label, name, fn in _CHECKS:
                b = _fixture(ver, f"{name}.pabgb")
                h = _fixture(ver, f"{name}.pabgh")
                if b is None or h is None:
                    continue
                run(f"{ver}/{label}", fn, b, h)

    print()
    if problems:
        print(f"{len(problems)} PROBLEM(S) -- a game patch has very likely "
              f"moved something:")
        for p in problems:
            print(f"  * {p}")
        print("\nThese readers refuse rather than write to a position they "
              "cannot account for, so the symptom is mods reporting no "
              "changes -- not damaged saves.")
    else:
        print("No problems. Every checked table decodes on this build.")
    return len(problems)


if __name__ == "__main__":
    raise SystemExit(main())
