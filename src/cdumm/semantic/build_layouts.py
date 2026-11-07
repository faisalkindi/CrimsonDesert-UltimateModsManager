"""Game 2.04's table layouts, per build, for reading only.

Game 2.04 opens every .pabgb record name first (#489) and reads the
fields of most tables in a new order (#458). The shipped schemas,
``pabgb_complete_schema.json`` plus ``pabgb_type_overrides.json``,
describe the pre-2.04 layouts, and everything that writes reads them
through ``parser.get_schema``. This module puts the 2.04 layouts next to
them, without changing what ``get_schema`` returns:

* ``schemas/layouts/<build>.json`` holds one build's layouts: per class,
  its fields in read order as ``[name, spec]``, in the spec grammar that
  the file's ``grammar`` block describes, plus the named readers and the
  condition-logic model the specs refer to. Per table it records what
  the layout was proven on: the record count, the sha256 of both files,
  and a digest of every field's end offset in every record.
* ``_compile`` turns a build into ``TableSchema`` objects whose fields
  carry ordinary ``pabgb_types`` descriptors. The structs and tagged
  variants they need are registered in ``SUBSTRUCT_DEFS`` and
  ``TAGGED_VARIANT_DEFS`` under the build's prefix (``2.04.08/...``), so
  ``consume_bytes`` walks them unchanged, and no new descriptor kind is
  added.
* ``schema_for_bytes`` picks a layout for one table's bytes. It returns
  one only when the table reads name first, a shipped build has a layout
  for it, and that layout fits: the bytes are the ones it was proven on,
  or else every record tiles exactly (a field walk from the end of the
  record header ends on the next record's .pabgh offset).

Tiling can't see two same-width fields swapped, so a layout's values
count as checked only where the build file lists a ``verified`` field
for the table, and only on the exact bytes that check ran on. Anywhere
else ``verified_fields`` is empty, and the Game Data grid shows those
fields as ``(unverified)``.

Nothing that writes reaches these schemas. ``get_schema``,
``has_schema`` and ``identify_table_from_path`` never return them, and
``schema_verify.schema_for_table``, which asks here first, is called only
by the Game Data grid's decoder and ``layout_fit``. Each schema carries
the build in ``TableSchema.build``, and the grid offers no edits on one.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cdumm.archive.table_ext import is_body_path, strip_table_ext
from cdumm.semantic import pabgb_types
from cdumm.semantic.parser import (
    _TYPE_MAP,
    FieldSpec,
    TableSchema,
    parse_pabgh_index,
)
from cdumm.semantic.record_header import (
    NAME_FIRST,
    header_end,
    table_header_order,
)

logger = logging.getLogger(__name__)

FORMAT = "cdumm-build-layouts/1"

_PRIM = {1: "u8", 2: "u16", 4: "u32", 8: "u64"}


def _layout_dirs() -> list[Path]:
    """Where ``schemas/layouts`` can be: the same places as the schemas."""
    here = Path(__file__).resolve()
    dirs = [here.parents[2] / "schemas" / "layouts",
            here.parents[3] / "schemas" / "layouts"]
    if getattr(sys, "frozen", False):
        dirs.insert(0, Path(sys._MEIPASS) / "schemas" / "layouts")
    return dirs


@dataclass(frozen=True)
class _Table:
    cls: str
    key_size: int
    records: int
    pabgb_sha256: str
    pabgh_sha256: str
    field_ends_sha256: str
    verified: frozenset[str]


class BuildLayouts:
    """One build file, compiled."""

    def __init__(self, data: dict[str, Any], source: Path | None = None):
        if data.get("format") != FORMAT:
            raise ValueError(f"{source}: format {data.get('format')!r}, "
                             f"expected {FORMAT!r}")
        self.build: str = data["build"]
        self.source = source
        self.tables: dict[str, _Table] = {
            stem: _table(t) for stem, t in data["tables"].items()}
        self.dlc_tables: dict[str, _Table] = {
            stem: _table(t) for stem, t in data.get("dlc_tables", {}).items()}
        self.fields: dict[str, list[tuple[str, str]]] = _Compiler(
            self.build, data).compile_classes()
        self._schemas: dict[tuple[str, bool], TableSchema] = {}

    def schema(self, stem: str, pinned: bool) -> TableSchema:
        """The ``TableSchema`` for ``stem``.

        ``pinned`` means the bytes are the ones the layout was proven on,
        so the table's ``verified`` fields apply; otherwise none do.
        """
        key = (stem, pinned)
        got = self._schemas.get(key)
        if got is None:
            t = self.tables[stem]
            got = self._schemas[key] = TableSchema(
                table_name=stem,
                fields=[_field_spec(n, d) for n, d in self.fields[t.cls]],
                # The payload starts right after the name-first header:
                # no NUL to skip, and every table has the header.
                no_null_skip=True,
                no_entry_header=False,
                verified_fields=t.verified if pinned else frozenset(),
                build=self.build)
        return got


def _table(t: dict[str, Any]) -> _Table:
    return _Table(cls=t["class"], key_size=int(t["key_size"]),
                  records=int(t["records"]),
                  pabgb_sha256=t["pabgb_sha256"],
                  pabgh_sha256=t["pabgh_sha256"],
                  field_ends_sha256=t["field_ends_sha256"],
                  verified=frozenset(t.get("verified", ())))


def _field_spec(name: str, desc: str) -> FieldSpec:
    width = pabgb_types.primitive_width(desc)
    if width is not None:
        ftype = f"direct_{desc}"
        fmt = (_TYPE_MAP.get(ftype) or (None, 0))[0]
        return FieldSpec(name=name, stream_size=width, field_type=ftype,
                         struct_fmt=fmt, type_descriptor=desc)
    return FieldSpec(name=name, stream_size=0, field_type="build_layout",
                     struct_fmt=None, type_descriptor=desc)


class _Compiler:
    """Spec grammar -> ``pabgb_types`` descriptors, for one build.

    Every struct and variant the specs need gets a name under the build's
    prefix and is registered with ``pabgb_types`` once, after the whole
    build compiled without a collision. Identical specs share one name.

    Three kinds have no direct descriptor and are spelled with the ones
    that exist, which walk the same bytes:

    * a tag with a default case (``union``/``tagswitchd``, ``nullflag``)
      lists every other u8 value with the default's payload;
    * a tag wider than one byte that needs a default, or a 3-byte tag,
      becomes a trie of u8 variants, low byte first;
    * the condition logic is a set of mutually recursive named variants.
      A tree deeper than Python's recursion limit allows raises
      ``RecursionError`` instead of returning None; ``_tiles`` and the
      grid's decode treat that as a refusal.
    """

    def __init__(self, build: str, data: dict[str, Any]):
        self.p = f"{build}/"
        self.data = data
        self.readers: dict[str, Any] = data.get("readers", {})
        self.structs: dict[str, list[tuple[str, str]]] = {}
        self.variants: dict[str, dict] = {}
        self.memo: dict[str, str] = {}
        self.cmemo: dict[str, str] = {}
        # Always refuses: a u8 tag with no cases.
        self.refuse = self._variant("u8", {}, name="refuse")
        self.cond = None

    # -- naming -----------------------------------------------------------

    def _struct(self, members: list[str], name: str | None = None) -> str:
        name = self.p + (name or f"s{len(self.structs)}")
        assert name not in self.structs, name
        self.structs[name] = [(f"m{i}", d) for i, d in enumerate(members)]
        return name

    def _variant(self, disc: str, variants: dict[int, str],
                 prefix: str | None = None, name: str | None = None) -> str:
        name = self.p + (name or f"v{len(self.variants)}")
        assert name not in self.variants, name
        v: dict[str, Any] = {"discriminator": disc, "variants": dict(variants)}
        if prefix is not None:
            v["fixed_prefix"] = [("pre", prefix)]
        self.variants[name] = v
        return name

    # -- field grammar ----------------------------------------------------

    def field(self, sp: list) -> str:
        key = json.dumps(sp, separators=(",", ":"))
        d = self.memo.get(key)
        if d is None:
            d = self.memo[key] = self._field(sp)
        return d

    def _fixed(self, n: int) -> str:
        return _PRIM.get(n) or f"[u8;{int(n)}]"

    @staticmethod
    def _empty(sp: list) -> bool:
        return sp in (["fixed", 0], ["parts", []])

    def _field(self, sp: list) -> str:
        k = sp[0]
        if k == "fixed":
            return self._fixed(sp[1])
        if k == "str":
            if sp[1] == 0:
                return "CString"
            if sp[1] == 9:
                return "LocalizableString"
            return self._struct([self._fixed(sp[1]), "CString"])
        if k == "list":
            return f"CArray<{self._fixed(sp[1])}>"
        if k == "slist":
            if sp[1] == 0:
                return "CArray<CString>"
            return f"CArray<{self._struct([self._fixed(sp[1]), 'CString'])}>"
        if k == "opt":
            return f"COptional<{self._fixed(sp[1])}>"
        if k == "optloc":
            return "COptional<LocalizableString>"
        if k == "optinner":
            return f"COptional<{self.field(sp[1])}>"
        if k == "nullflag":
            inner = self.field(sp[1])
            return self._variant(
                "u8", {0: inner, **{t: "" for t in range(1, 256)}})
        if k == "parts":
            return self._struct([self.field(x) for x in sp[1]])
        if k == "plist":
            return f"CArray<{self._struct([self.field(x) for x in sp[1]])}>"
        if k == "union":
            pre, cases, default = sp[1], sp[2], sp[3]
            var = {int(t): ("" if self._empty(c) else self.field(c))
                   for t, c in cases}
            if default is not None:
                dd = "" if self._empty(default) else self.field(default)
                for t in range(256):
                    var.setdefault(t, dd)
            return self._variant("u8", var, prefix=self.field(pre))
        if k in ("tagswitch", "tagswitchd"):
            w, base = int(sp[1]), sp[2]
            cases = {int(t): c for t, c in sp[3]}
            default = sp[4] if k == "tagswitchd" else None
            if default is not None and w == 1:
                var = {t: self.field(c) for t, c in cases.items()}
                for t in range(256):
                    var.setdefault(t, self.field(default))
                return self._variant("u8", var, prefix=self.field(base))
            if default is None and w in (1, 2, 4):
                disc = {1: "u8", 2: "u16", 4: "u32"}[w]
                return self._variant(
                    disc, {t: self.field(c) for t, c in cases.items()},
                    prefix=self.field(base))
            return self._trie(w, base, cases, default)
        if k == "cond":
            return self._condition()["logic"]
        if k == "condbody":
            return self._condition()["logic_body"]
        if k == "ref":
            name = self.p + sp[1]
            if name not in self.structs:
                # Reserve the name first: a reader can reach itself.
                self.structs[name] = []
                self.structs[name] = [("v", self.field(self.readers[sp[1]]))]
            return name
        if k == "refuse":
            return self.refuse
        raise ValueError(f"unknown layout spec kind {k!r}")

    def _trie(self, w: int, base: list, cases: dict[int, list],
              default: list | None) -> str:
        def level(k: int, cands: dict[int, list]) -> str:
            by: dict[int, dict[int, list]] = {}
            for t, c in cands.items():
                by.setdefault((t >> (8 * k)) & 0xFF, {})[t] = c
            var: dict[int, str] = {}
            for b, sub in by.items():
                if k == w - 1:
                    ((_t, c),) = sub.items()
                    var[b] = self._struct([self.field(base), self.field(c)])
                else:
                    var[b] = level(k + 1, sub)
            if default is not None:
                rest = w - 1 - k
                dpay = self._struct(
                    ([f"[u8;{rest}]"] if rest else [])
                    + [self.field(base), self.field(default)])
                for b in range(256):
                    var.setdefault(b, dpay)
            return self._variant("u8", var)
        return level(0, cases)

    # -- condition grammar ------------------------------------------------

    def _condition(self) -> dict[str, str]:
        if self.cond is not None:
            return self.cond
        model = self.data["condition"]
        caps = model.get("caps", {})
        p = self.p
        self.cond = {"logic": p + "cond.logic", "logic_body": p + "cond.body",
                     "node": p + "cond.node", "func": p + "cond.func",
                     "value": p + "cond.value", "f286": p + "cond.f286",
                     "flag": p + "cond.flag", "zero": p + "cond.zero"}
        c = self.cond
        self.variants[c["flag"]] = {"discriminator": "u8",
                                    "variants": {0: "", 1: ""}}
        self.variants[c["zero"]] = {"discriminator": "u8", "variants": {0: ""}}
        self.structs[c["logic"]] = [("v", self.cspec(model["logic"]))]
        self.structs[c["logic_body"]] = [
            ("v", self.cspec(model["logic_body"]))]
        self.variants[c["node"]] = {
            "discriminator": "u8",
            "variants": {int(t): self.cspec(s)
                         for t, s in model["node_types"].items()}}
        fmax = caps.get("function_id_max", 0xFFFF)
        funcs: dict[int, str] = {}
        for fid, e in model["functions"].items():
            if "refuse" in e or int(fid) > fmax:
                continue
            if e.get("special") == "f286":
                funcs[int(fid)] = c["f286"]
                continue
            # The +0x98 slot: nothing, the target reader (a u8 that must
            # be 0), or reads of its own.
            tail = e.get("tail")
            if tail in ("none", None) or tail == []:
                tails = []
            elif tail == "target98":
                tails = [c["zero"]]
            elif tail[0] == "refuse":
                tails = [self.refuse]
            else:
                tails = [self.cspec(x) for x in tail]
            funcs[int(fid)] = self._struct(
                [self.cspec(x) for x in e["reads"]] + tails)
        self.variants[c["func"]] = {"discriminator": "u16",
                                    "variants": funcs}
        vmax = caps.get("f286_value_type_max", 255)
        self.variants[c["f286"]] = {
            "discriminator": "u8", "fixed_prefix": [("op", "u8")],
            "variants": {int(vt): (f"[u8;{int(wd)}]" if wd else "")
                         for vt, wd in model["f286_widths"].items()
                         if int(vt) <= vmax}}
        self.variants[c["value"]] = {
            "discriminator": "u8",
            "variants": {int(st): self._struct([self.cspec(x) for x in reads])
                         for st, reads in model["node4_values"].items()}}
        return self.cond

    def cspec(self, cs: list) -> str:
        key = json.dumps(cs, separators=(",", ":"))
        d = self.cmemo.get(key)
        if d is None:
            d = self.cmemo[key] = self._cspec(cs)
        return d

    def _cspec(self, cs: list) -> str:
        c = self.cond
        k = cs[0]
        if k == "take":
            return self._fixed(cs[1])
        if k == "u8":
            return "u8"
        if k == "flag":
            return c["flag"]
        if k == "cstr":
            return "CString"
        if k == "loc":
            return "LocalizableString"
        if k == "list":
            return f"CArray<{self._fixed(cs[1])}>"
        if k == "seq":
            return self._struct([self.cspec(x) for x in cs[1]])
        if k == "gate":
            return self._variant(
                "u8", {0: "", 1: self._struct([self.cspec(x) for x in cs[1]])})
        if k == "rep":
            return f"CArray<{self._struct([self.cspec(x) for x in cs[1]])}>"
        if k == "logic":
            return c["logic"]
        if k in ("node", "one"):
            return c["node"]
        if k == "func":
            return c["func"]
        if k == "value":
            return c["value"]
        if k == "pair":
            return self._struct([c["node"], c["node"]])
        if k == "zero_flag":
            return c["zero"]
        if k == "refuse":
            return self.refuse
        raise ValueError(f"unknown condition spec kind {k!r}")

    # -- classes ----------------------------------------------------------

    def compile_classes(self) -> dict[str, list[tuple[str, str]]]:
        out: dict[str, list[tuple[str, str]]] = {}
        for cls, fields in self.data["classes"].items():
            out[cls] = _unique([(n, self.field(sp)) for n, sp in fields])
        self._register()
        return out

    def _register(self) -> None:
        sd, tv = pabgb_types.SUBSTRUCT_DEFS, pabgb_types.TAGGED_VARIANT_DEFS
        clash = sorted((set(self.structs) | set(self.variants))
                       & (set(sd) | set(tv)))
        if clash:
            raise ValueError(f"build layout names already defined: "
                             f"{clash[:5]}")
        sd.update(self.structs)
        tv.update(self.variants)


def _unique(fields: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Give a repeated name a ``#2`` suffix.

    The grid keys a record's values by field name and fills ``_key`` and
    ``_name`` from the record header, so a payload field called ``_name``
    (several 2.04 classes have one) would vanish behind it, and
    characterinfo's second ``_IgnoreScale`` would overwrite its first.
    """
    seen = {"_key", "_name"}
    out = []
    for name, desc in fields:
        n, i = name, 1
        while n in seen:
            i += 1
            n = f"{name}#{i}"
        seen.add(n)
        out.append((n, desc))
    return out


# ── loading ─────────────────────────────────────────────────────────────

_BUILDS: list[BuildLayouts] | None = None


def builds() -> list[BuildLayouts]:
    """Every shipped build, compiled once per process, newest first.

    A file that doesn't load is logged and skipped: this is display-only,
    and a broken file must not take the grid down with it.
    """
    global _BUILDS
    if _BUILDS is not None:
        return _BUILDS
    found: dict[str, Path] = {}
    for d in _layout_dirs():
        if d.is_dir():
            for p in sorted(d.glob("*.json")):
                found.setdefault(p.name, p)
    out = []
    for name in sorted(found, reverse=True):
        p = found[name]
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
            out.append(BuildLayouts(data, p))
        except Exception:
            logger.exception("build layouts %s didn't load; skipped", p)
    _BUILDS = out
    return out


def has_layout(table: str) -> bool:
    """True when some shipped build has a layout for ``table``."""
    stem = table.lower()
    return any(stem in b.tables for b in builds())


def table_for_path(entry_path: str) -> str | None:
    """The table stem of ``entry_path`` when a shipped build has it.

    For the Game Data grid only: 2.04 has tables with no entry in the
    pre-2.04 base schema, so ``identify_table_from_path`` doesn't name
    them, and widening that would hand them to the writers too.
    """
    if not entry_path:
        return None
    filename = entry_path.rsplit("/", 1)[-1]
    if not is_body_path(filename):
        return None
    stem = strip_table_ext(filename).lower()
    return stem if has_layout(stem) else None


# ── choosing a layout for a table's bytes ───────────────────────────────

_TILE_CACHE: dict[tuple[str, str, str, str], bool] = {}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tiles(schema: TableSchema, body: bytes, offsets: dict[int, int],
          key_size: int) -> bool:
    """Every record of ``body``: the fields end exactly where it ends.

    Each record runs from its .pabgh offset to the next one (the last to
    the end of ``body``). The walk starts at the end of the name-first
    header, and a field the walker refuses, or a ``RecursionError`` from
    a condition tree too deep to walk, fails the table.
    """
    ent = sorted(offsets.values())
    descs = [f.type_descriptor for f in schema.fields]
    consume = pabgb_types.consume_bytes
    for i, off in enumerate(ent):
        end = ent[i + 1] if i + 1 < len(ent) else len(body)
        p = header_end(body, off, key_size, NAME_FIRST)
        if p is None or p > end:
            return False
        try:
            for d in descs:
                n = consume(d, body, p, end)
                if n is None:
                    return False
                p += n
        except RecursionError:
            return False
        if p != end:
            return False
    return True


def schema_for_bytes(table: str, body: bytes,
                     header: bytes) -> TableSchema | None:
    """A 2.04 layout for these bytes of ``table``, or None.

    None unless the table reads name first (``table_header_order``), a
    shipped build has a layout for it, and that layout fits these bytes.
    A layout fits when both files are byte-identical to the ones it was
    proven on (the build file records their sha256), or else when every
    record tiles. Only the first case keeps the table's ``verified``
    fields. Newest build first; the tiling result is cached per build and
    pair of files.
    """
    stem = table.lower()
    candidates = [b for b in builds() if stem in b.tables]
    if not candidates:
        return None
    try:
        key_size, offsets = parse_pabgh_index(header, stem)
    except Exception:                                       # noqa: BLE001
        return None
    if not offsets:
        return None
    if table_header_order(body, offsets, key_size) != NAME_FIRST:
        return None
    body_sha, header_sha = _sha256(body), _sha256(header)
    for b in candidates:
        t = b.tables[stem]
        if (body_sha, header_sha) == (t.pabgb_sha256, t.pabgh_sha256):
            return b.schema(stem, pinned=True)
        ck = (b.build, stem, body_sha, header_sha)
        fits = _TILE_CACHE.get(ck)
        if fits is None:
            fits = _TILE_CACHE[ck] = tiles(
                b.schema(stem, pinned=False), body, offsets, key_size)
            logger.info("%s: the %s layout %s these bytes", stem, b.build,
                        "tiles" if fits else "doesn't tile")
        if fits:
            return b.schema(stem, pinned=False)
    return None
