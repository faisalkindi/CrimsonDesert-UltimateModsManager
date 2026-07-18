"""Candidate grammars for the CD 1.13 iteminfo tail.

Judged ONLY by: consumes the tail to zero remaining bytes AND re-serializes
byte-identical, on all 6508 records. Nothing else counts.
"""
from __future__ import annotations
import struct
import sys

sys.path.insert(0, "src"); sys.path.insert(0, ".")
from tests.fixture_loaders import load_vanilla113
from cdumm.semantic.parser import parse_pabgh_index
from cdumm.engine import iteminfo_native_parser as P

body = load_vanilla113("iteminfo.pabgb")
header = load_vanilla113("iteminfo.pabgh")
_ks, offs = parse_pabgh_index(header, "iteminfo")
starts = sorted(offs.values())
fields = P.detect_iteminfo_layout(body, starts)

MAXC = 4096


class R:
    def __init__(s, b, p, e): s.b, s.p, s.e = b, p, e
    def u8(s):
        if s.p + 1 > s.e: raise EOFError
        v = s.b[s.p]; s.p += 1; return v
    def u16(s):
        if s.p + 2 > s.e: raise EOFError
        v = struct.unpack_from("<H", s.b, s.p)[0]; s.p += 2; return v
    def u32(s):
        if s.p + 4 > s.e: raise EOFError
        v = struct.unpack_from("<I", s.b, s.p)[0]; s.p += 4; return v
    def f32(s):
        if s.p + 4 > s.e: raise EOFError
        v = struct.unpack_from("<f", s.b, s.p)[0]; s.p += 4; return v
    def carr32(s):
        n = s.u32()
        if n > MAXC: raise ValueError("count")
        return [s.u32() for _ in range(n)]


class W:
    def __init__(s): s.b = bytearray()
    def u8(s, v): s.b.append(v & 0xFF)
    def u16(s, v): s.b += struct.pack("<H", v)
    def u32(s, v): s.b += struct.pack("<I", v)
    def f32(s, v): s.b += struct.pack("<f", v)
    def carr32(s, l):
        s.u32(len(l))
        for x in l: s.u32(x)


def elem_read(r, ncarr, ntail_u8):
    e = {"scale": [r.f32(), r.f32(), r.f32()],
         "lists": [r.carr32() for _ in range(ncarr)],
         "tb": [r.u8() for _ in range(ntail_u8)]}
    return e


def elem_write(w, e):
    for f in e["scale"]: w.f32(f)
    for l in e["lists"]: w.carr32(l)
    for b in e["tb"]: w.u8(b)


def make(ncarr, ntail_u8, trailer):
    def rd(r):
        n = r.u32()
        if n > 64: raise ValueError("elem count")
        els = [elem_read(r, ncarr, ntail_u8) for _ in range(n)]
        tr = [r.u8() for _ in range(trailer)]
        return {"els": els, "tr": tr}

    def wr(w, v):
        w.u32(len(v["els"]))
        for e in v["els"]: elem_write(w, e)
        for b in v["tr"]: w.u8(b)
    return rd, wr


CANDS = {}
for nc in (3, 4, 5):
    for nt in (1, 2, 3, 4):
        for tr in (0, 1, 2, 3):
            CANDS[f"carray(scale+{nc}L+{nt}B) + {tr}B"] = make(nc, nt, tr)

tails = []
for i, s in enumerate(starts):
    e = starts[i + 1] if i + 1 < len(starts) else len(body)
    try:
        r0 = P._Reader(body, s, rec_end=e)
        P._read_item(r0, fields=fields)
    except Exception:
        continue
    tails.append(bytes(body[r0.pos:e]))

print(f"tails collected: {len(tails)}\n")

best = []
for label, (rd, wr) in CANDS.items():
    ok = 0
    for t in tails:
        try:
            r = R(t, 0, len(t))
            v = rd(r)
            if r.p != len(t):
                continue
            w = W(); wr(w, v)
            if bytes(w.b) == t:
                ok += 1
        except Exception:
            continue
    if ok:
        best.append((ok, label))

best.sort(reverse=True)
print(f"{'grammar':<34} {'EXACT':>7}  {'%':>6}")
print("-" * 52)
for ok, label in best[:12]:
    print(f"{label:<34} {ok:>7}  {100*ok/len(tails):5.1f}%")
if not best:
    print("  none")
