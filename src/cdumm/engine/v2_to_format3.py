"""Convert a v2 byte-offset mod into a Format 3 (field-name) mod.

Asked for by falobos76 on GitHub #191: two of the mods he relies on are
abandoned on Nexus, and every Crimson Desert patch moves the bytes out
from under them. Someone then has to re-measure the offsets by hand, or
the mod is dead.

A v2 change says "at byte 4,192,880, replace 64 00 with FF FF".
A Format 3 intent says "set max_endurance to 65535 on item 1002862".

The second one never goes stale: CDUMM looks the item up by key in the
player's own files on every Apply, so Pearl Abyss can move it wherever
they like. Converting a mod once makes it patch-proof from then on.

HOW IT WORKS

The offsets are only meaningful against the table they were measured
against, so we anchor them there first:

  1. Every change's ``original`` bytes MUST match vanilla at that
     offset. If any don't, the mod is stale for this game version and we
     refuse the whole thing -- the offsets no longer mean what they say,
     and a "best effort" conversion would confidently name the wrong
     item. (Refusing is the whole point; see the four bugs in #259,
     #275, #278 and #285, all of which were a guess that looked fine.)
  2. The .pabgh index frames every record, so an offset lands in exactly
     one item; walking the record's fields in order says which field it
     lands in.
  3. A change may write only PART of a field (a float where two bytes
     differ). We overlay the patched bytes onto the field's current
     bytes and decode the whole field, so the intent carries the real
     resulting value rather than a byte fragment.

WHAT THIS DOES NOT DO

Recover a mod that is ALREADY stale. Measured on real mods: for
whole-field writes the reconstruction is forced and exact, but for
partial-byte writes matching on values alone pulls in records the mod
never touched (6 false positives out of 66 on the cooldown floats).
That is a guess, and a guess writes to the wrong item. So: convert a mod
while it still works, and it never goes stale again.
"""
from __future__ import annotations

import json
import logging
import struct
from dataclasses import dataclass, field as dc_field
from pathlib import Path

logger = logging.getLogger(__name__)

#: Tables we can convert. Each needs a decoder that frames records
#: exactly and walks fields in order. iteminfo is the one real v2 mods
#: target; others can be added once measured the same way.
SUPPORTED_TABLES = ("iteminfo",)

_SCALARS = {
    "u8": ("<B", 1), "u16": ("<H", 2), "u32": ("<I", 4),
    "u64": ("<Q", 8), "i64": ("<q", 8), "f32": ("<f", 4),
}

#: A record's own identity. A Format 3 intent addresses an item BY key, so
#: "set key" is not a thing it can express -- and a mod that rewrites a
#: record's ID isn't editing an item, it's replacing one. Caught by a test
#: that accidentally aimed at this field; the writer refused, which is the
#: right answer, but the converter should say so in words rather than emit
#: an intent that can never apply.
_STRUCTURAL = frozenset({"key"})


class ConversionRefused(Exception):
    """The mod cannot be converted safely. Do not write a file."""


@dataclass
class ConversionReport:
    target: str = ""
    intents: list[dict] = dc_field(default_factory=list)
    converted: int = 0
    #: changes we could not name, with the reason. NOT silently dropped:
    #: the caller shows these, and the mod is only written if the user
    #: accepts a partial conversion.
    unconverted: list[tuple[dict, str]] = dc_field(default_factory=list)
    verified: bool = False

    @property
    def total(self) -> int:
        return self.converted + len(self.unconverted)

    def summary(self) -> str:
        lines = [
            f"{self.converted} of {self.total} change(s) converted to "
            f"field-name intents on {self.target}"
        ]
        if self.unconverted:
            reasons: dict[str, int] = {}
            for _, why in self.unconverted:
                reasons[why] = reasons.get(why, 0) + 1
            for why, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {n} not converted: {why}")
        lines.append(
            "verified byte-for-byte against the original mod"
            if self.verified else
            "NOT verified — do not ship this file")
        return "\n".join(lines)


def _field_spans(record: dict, layout, base: int) -> dict[str, tuple]:
    """field name -> (abs_start, abs_end, kind) for one record.

    Re-serializes the record one field at a time; the writer is the
    inverse of the reader, so the byte each field occupies is exactly
    where the reader found it.
    """
    from cdumm.engine.iteminfo_native_parser import (
        _reorder_equip_tail, _Writer, _write_item,
    )
    w = _Writer()
    out: dict[str, tuple] = {}
    # Walk in the record's effective field order so per-field spans match how
    # the writer lays the bytes out (the type_id==0 cooltime reorder, #191).
    layout = _reorder_equip_tail(layout, record)
    for spec in layout:
        a = len(w.buf)
        try:
            _write_item(w, record, fields=[spec])
        except Exception:
            break
        b = len(w.buf)
        if b > a:
            out[spec[0]] = (base + a, base + b, spec[1])
    return out


def _decode(kind: str, raw: bytes):
    fmt = _SCALARS.get(kind)
    if fmt is None or len(raw) != fmt[1]:
        return None
    val = struct.unpack(fmt[0], raw)[0]
    if kind == "f32":
        return round(val, 6)
    return val


def convert_iteminfo(
    changes: list[dict], vanilla_body: bytes, vanilla_header: bytes,
    target: str = "iteminfo.pabgb",
) -> ConversionReport:
    """Turn v2 byte changes on iteminfo into Format 3 intents."""
    import bisect

    from cdumm.engine.iteminfo_native_parser import (
        detect_iteminfo_layout, parse_iteminfo_from_bytes,
    )
    from cdumm.semantic.parser import parse_pabgh_index

    _, offsets = parse_pabgh_index(vanilla_header, "iteminfo")
    if not offsets:
        raise ConversionRefused(
            "the .pabgh index for iteminfo could not be read, so records "
            "cannot be framed and no offset can be named")
    starts = sorted(offsets.values())
    key_at = {off: k for k, off in offsets.items()}
    layout = detect_iteminfo_layout(vanilla_body, starts)
    items = parse_iteminfo_from_bytes(
        vanilla_body, record_offsets=starts, fields=layout)
    by_key = {it["key"]: it for it in items}

    rep = ConversionReport(target=target)
    spans_cache: dict[int, dict] = {}
    # key -> field -> value, so two changes touching different bytes of
    # the same field collapse into ONE intent carrying the final value.
    staged: dict[tuple[int, str], tuple[bytes, int, int, str]] = {}

    for ch in changes:
        off = ch.get("offset")
        try:
            orig = bytes.fromhex(ch.get("original") or "")
            new = bytes.fromhex(ch.get("patched") or "")
        except ValueError:
            rep.unconverted.append((ch, "the change's bytes are not hex"))
            continue
        if not isinstance(off, int) or not orig or len(new) != len(orig):
            rep.unconverted.append((ch, "malformed change"))
            continue

        # (1) the mod must still match this game version, or its offsets
        #     mean nothing and we would name the wrong item.
        if vanilla_body[off:off + len(orig)] != orig:
            raise ConversionRefused(
                f"this mod was built for a different version of Crimson "
                f"Desert: at byte {off} it expects "
                f"{orig.hex()} but your game has "
                f"{vanilla_body[off:off + len(orig)].hex()}.\n\n"
                f"A mod can only be converted while it still works. "
                f"Re-apply the mod's offsets to your game version first "
                f"(or ask its author to), then convert it — and it will "
                f"never go stale again.")

        i = bisect.bisect_right(starts, off) - 1
        if i < 0:
            rep.unconverted.append((ch, "offset is before the first record"))
            continue
        rec_start = starts[i]
        key = key_at[rec_start]
        rec = by_key.get(key)
        if rec is None or rec.get("_opaque_record"):
            rep.unconverted.append((
                ch, "lands in a record CDUMM cannot decode yet"))
            continue

        spans = spans_cache.get(i)
        if spans is None:
            spans = _field_spans(rec, layout, rec_start)
            spans_cache[i] = spans

        # Which named field(s) does this change land on?
        end = off + len(orig)
        single = next(((fn, a, b, k) for fn, (a, b, k) in spans.items()
                       if a <= off and end <= b), None)
        if single is not None:
            segments = [single]
        else:
            # Multi-field: accept only when the change EXACTLY tiles a run of
            # complete, consecutive fields -- e.g. the thief gloves' one write
            # covers cooltime + unk_post_cooltime_a + unk_post_cooltime_b (three
            # i64s) in a single 24-byte blob (#191). Split it into one intent
            # per field. A change that starts or ends mid-field does NOT tile
            # and is left unconverted rather than guessed.
            cov = sorted((a, b, k, fn) for fn, (a, b, k) in spans.items()
                         if off <= a and b <= end)
            if (cov and cov[0][0] == off and cov[-1][1] == end
                    and all(cov[j][1] == cov[j + 1][0]
                            for j in range(len(cov) - 1))):
                segments = [(fn, a, b, k) for a, b, k, fn in cov]
            else:
                segments = []
        if not segments:
            rep.unconverted.append((
                ch, "lands between named fields (CDUMM cannot say which "
                    "field this is on your game version)"))
            continue

        bad = False
        for fname, a, b, kind in segments:
            if fname in _STRUCTURAL:
                rep.unconverted.append((
                    ch, "rewrites the item's ID, which a field-name mod "
                        "addresses items BY and therefore cannot change"))
                bad = True
                break
            if kind not in _SCALARS:
                rep.unconverted.append((
                    ch, f"'{fname}' is a {kind}, which has no single value "
                        f"to set"))
                bad = True
                break
            # (3) a change may write only part of a field. Overlay this
            #     change's slice onto the field's current bytes and decode
            #     the WHOLE field, so the intent carries the real value.
            s = max(off, a)
            e = min(end, b)
            cur = bytearray(staged.get((key, fname), (None,))[0]
                            or vanilla_body[a:b])
            cur[s - a:e - a] = new[s - off:e - off]
            staged[(key, fname)] = (bytes(cur), a, b, kind)
        if bad:
            continue

    for (key, fname), (raw, a, b, kind) in staged.items():
        val = _decode(kind, raw)
        if val is None:
            rep.unconverted.append((
                {"offset": a}, f"could not decode '{fname}' ({kind})"))
            continue
        rep.intents.append({
            "entry": "", "key": key, "field": fname,
            "op": "set", "new": val,
        })
        rep.converted += 1

    rep.intents.sort(key=lambda i: (i["key"], i["field"]))
    return rep


def _apply_v2(body: bytes, changes: list[dict]) -> bytes:
    out = bytearray(body)
    for ch in changes:
        off = int(ch["offset"])
        new = bytes.fromhex(ch["patched"])
        out[off:off + len(new)] = new
    return bytes(out)


def verify(
    rep: ConversionReport, changes: list[dict],
    vanilla_body: bytes, vanilla_header: bytes,
) -> bool:
    """Apply the CONVERTED mod and the ORIGINAL mod, and require the
    resulting table to be byte-for-byte identical.

    Nobody has to take the conversion on trust: if the Format 3 version
    doesn't reproduce exactly what the v2 version did, it isn't written.
    """
    from cdumm.engine.format3_handler import Format3Intent
    from cdumm.engine.iteminfo_writer import build_iteminfo_intent_change

    if rep.unconverted:
        return False       # a partial conversion can't reproduce the whole
    intents = [
        Format3Intent(entry=i["entry"], key=i["key"], field=i["field"],
                      op=i["op"], new=i["new"], old=None)
        for i in rep.intents
    ]
    change = build_iteminfo_intent_change(
        vanilla_body, intents, vanilla_header=vanilla_header)
    if change is None:
        return False
    got = bytes.fromhex(change["patched"])
    want = _apply_v2(vanilla_body, changes)
    rep.verified = (got == want)
    if not rep.verified:
        logger.warning(
            "v2->Format 3 conversion did NOT reproduce the original mod "
            "byte-for-byte (%d vs %d bytes) — refusing to write it",
            len(got), len(want))
    return rep.verified


def is_convertible(json_path: Path) -> bool:
    """Does this mod file carry byte-offset changes we could convert?

    Cheap enough to call while building a context menu: no game files, no
    table parse. Answers "should the Convert action be offered at all".
    """
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    if data.get("format") == 3 or "intents" in data or "targets" in data:
        return False        # already a field-name mod
    for patch in data.get("patches", []):
        target = (patch.get("game_file") or "").lower()
        if not any(t in target for t in SUPPORTED_TABLES):
            continue
        for ch in patch.get("changes", []):
            if isinstance(ch.get("offset"), int) and ch.get("original"):
                return True
    return False


def _load_vanilla_table(game_dir: Path, name: str) -> bytes:
    """Pull a vanilla table out of the game's PAZ archives.

    CDMods/vanilla holds the PAMT indices; the .paz bodies stay in the game
    install, so an entry resolved against the vanilla dir can point at a
    .paz that only exists under game_dir. Map it back rather than failing.
    """
    from cdumm.engine.json_patch_handler import (
        _extract_from_paz, _find_pamt_entry,
    )
    vanilla_dir = game_dir / "CDMods" / "vanilla"

    def _extract(entry):
        paz = Path(entry.paz_file)
        if not paz.exists() and vanilla_dir.exists():
            try:
                paz = game_dir / paz.relative_to(vanilla_dir)
            except ValueError:
                pass
        return _extract_from_paz(entry, str(paz))

    # Prefer CDUMM's cached vanilla index. After a game update its offsets no
    # longer line up with the .paz body (the bodies live in the game install
    # and Steam just replaced them), so extraction throws a raw decompression
    # error. Catch it and fall back to the game's own current index, which is
    # correct for the freshly-installed build -- turning a cryptic LZ4 crash
    # into either a working read or a clear, actionable message.
    cached = (_find_pamt_entry(name, vanilla_dir)
              if vanilla_dir.exists() else None)
    if cached is not None:
        try:
            return _extract(cached)
        except Exception:
            logger.info(
                "vanilla-cached index stale for %s (game updated since the "
                "backup?); falling back to the live game index", name)

    live = _find_pamt_entry(name, game_dir)
    if live is None:
        raise ConversionRefused(
            f"{name} could not be found in your game's archives")
    try:
        return _extract(live)
    except Exception as e:
        raise ConversionRefused(
            f"CDUMM couldn't read the vanilla '{name}' from your game files. "
            f"Its saved vanilla copy is out of date -- this usually happens "
            f"right after a game update. Run Settings → Fix Everything "
            f"to refresh CDUMM's vanilla backup, then try again.") from e


def convert_mod_file(
    json_path: Path, game_dir: Path, out_path: Path,
    mod_name: str | None = None, author: str | None = None,
) -> ConversionReport:
    """Convert a v2 mod file to Format 3 and write it — the whole job.

    GUI-free on purpose: the worker thread calls this, and so do the tests.
    Raises ConversionRefused (with a message written for the user) when the
    mod can't be converted safely. Writes nothing unless the conversion
    reproduces the original mod byte-for-byte.
    """
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    changes: list[dict] = []
    skipped_files: list[str] = []
    for patch in data.get("patches", []):
        target = (patch.get("game_file") or "").lower()
        if any(t in target for t in SUPPORTED_TABLES):
            changes.extend(patch.get("changes", []))
        elif patch.get("changes"):
            skipped_files.append(patch.get("game_file") or "?")

    if not changes:
        raise ConversionRefused(
            "this mod has no changes CDUMM can convert yet. Only "
            f"{', '.join(SUPPORTED_TABLES)} is supported so far — the tables "
            "it edits are: " + (", ".join(sorted(set(skipped_files))) or "none"))

    body = _load_vanilla_table(game_dir, "iteminfo.pabgb")
    header = _load_vanilla_table(game_dir, "iteminfo.pabgh")

    rep = convert_iteminfo(changes, body, header)
    for gf in skipped_files:
        rep.unconverted.append((
            {"game_file": gf},
            f"edits {gf}, which CDUMM cannot convert yet"))

    verify(rep, changes, body, header)
    if not rep.verified:
        raise ConversionRefused(
            "the converted mod did NOT reproduce the original byte-for-byte, "
            "so nothing was written.\n\n" + rep.summary())

    write_format3(
        rep, out_path,
        mod_name or json_path.stem,
        author=author or (data.get("modinfo") or {}).get("author"),
        source=json_path.name)
    return rep


def write_format3(
    rep: ConversionReport, out_path: Path, mod_name: str,
    author: str | None = None, source: str | None = None,
) -> Path:
    """Write the converted mod as a .field.json the user keeps."""
    if not rep.intents:
        raise ConversionRefused("nothing was converted; no file written")
    doc = {
        "format": 3,
        "target": rep.target,
        "modinfo": {
            "title": mod_name,
            "author": author or "",
            "description": (
                "Converted from the byte-offset (v2) version of this mod "
                "by CDUMM. Field-name mods are looked up by item on every "
                "Apply, so this file does not need new offsets when "
                "Crimson Desert updates."
                + (f" Source: {source}" if source else "")
            ),
        },
        "intents": rep.intents,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    return out_path
