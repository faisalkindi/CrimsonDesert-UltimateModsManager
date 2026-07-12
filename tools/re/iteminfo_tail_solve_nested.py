"""Find the nested (slot-2) element grammar by intersection.

Outer grammar is settled (96.4%):
    tail = CArray<Elem> + 2B
    Elem = 3xf32, L0:carr32, L1:carr32, L2:CArray<X>, L3:carr32, 3xu8

X is unknown. Enumerate token sequences for X; keep only those that make
the WHOLE tail consume exactly and re-serialize byte-exact -- on every one
of the 236 records that currently fail. Then intersect.
"""
from __future__ import annotations
import struct, sys
from itertools import product

sys.path.insert(0, "src"); sys.path.insert(0, ".")
from tests.fixture_loaders import load_vanilla113
from cdumm.semantic.parser import parse_pabgh_index
from cdumm.engine import iteminfo_native_parser as P

body = load_vanilla113("iteminfo.pabgb")
header = load_vanilla113("iteminfo.pabgh")
_ks, offs = parse_pabgh_index(header, "iteminfo")
starts = sorted(offs.values())
fields = P.detect_iteminfo_layout(body, starts)

tails = []
for i, s in enumerate(starts):
    e = starts[i + 1] if i + 1 < len(starts) else len(body)
    try:
        r0 = P._Reader(body, s, rec_end=e)
        P._read_item(r0, fields=fields)
    except Exception:
        continue
    tails.append(bytes(body[r0.pos:e]))


class R:
    __slots__ = ("b", "p", "e")
    def __init__(s, b): s.b, s.p, s.e = b, 0, len(b)


def u8(r):
    if r.p >= r.e: raise EOFError
    v = r.b[r.p]; r.p += 1; return ("u8", v)


def u16(r):
    if r.p + 2 > r.e: raise EOFError
    v = struct.unpack_from("<H", r.b, r.p)[0]; r.p += 2; return ("u16", v)


def u32(r):
    if r.p + 4 > r.e: raise EOFError
    v = struct.unpack_from("<I", r.b, r.p)[0]; r.p += 4; return ("u32", v)


def f32(r):
    if r.p + 4 > r.e: raise EOFError
    v = struct.unpack_from("<f", r.b, r.p)[0]; r.p += 4; return ("f32", v)


def carr32(r):
    n = struct.unpack_from("<I", r.b, r.p)[0]; r.p += 4
    if n > 512 or r.p + 4 * n > r.e: raise ValueError
    v = [struct.unpack_from("<I", r.b, r.p + 4 * i)[0] for i in range(n)]
    r.p += 4 * n
    return ("carr32", v)


def carr8(r):
    n = struct.unpack_from("<I", r.b, r.p)[0]; r.p += 4
    if n > 2048 or r.p + n > r.e: raise ValueError
    v = list(r.b[r.p:r.p + n]); r.p += n
    return ("carr8", v)


TOK = {"u8": u8, "u16": u16, "u32": u32, "f32": f32,
       "carr32": carr32, "carr8": carr8}

WR = {
    "u8": lambda w, v: w.append(v & 0xFF),
    "u16": lambda w, v: w.extend(struct.pack("<H", v)),
    "u32": lambda w, v: w.extend(struct.pack("<I", v)),
    "f32": lambda w, v: w.extend(struct.pack("<f", v)),
    "carr32": lambda w, v: (w.extend(struct.pack("<I", len(v))),
                            [w.extend(struct.pack("<I", x)) for x in v]),
    "carr8": lambda w, v: (w.extend(struct.pack("<I", len(v))),
                           w.extend(bytes(v))),
}


def parse_tail(t, xseq):
    r = R(t)
    out = []
    n = u32(r)[1]
    if n > 64: raise ValueError
    for _ in range(n):
        sc = [f32(r)[1] for _ in range(3)]
        L0 = carr32(r)[1]
        L1 = carr32(r)[1]
        c2 = u32(r)[1]
        if c2 > 64: raise ValueError
        L2 = []
        for _ in range(c2):
            L2.append([TOK[tk](r) for tk in xseq])
        L3 = carr32(r)[1]
        tb = [u8(r)[1] for _ in range(3)]
        out.append((sc, L0, L1, L2, L3, tb))
    tr = [u8(r)[1] for _ in range(2)]
    if r.p != len(t):
        raise ValueError("short")
    # re-serialize
    w = bytearray()
    w.extend(struct.pack("<I", len(out)))
    for sc, L0, L1, L2, L3, tb in out:
        for f in sc: w.extend(struct.pack("<f", f))
        WR["carr32"](w, L0); WR["carr32"](w, L1)
        w.extend(struct.pack("<I", len(L2)))
        for elem in L2:
            for (tk, val) in elem:
                WR[tk](w, val)
        WR["carr32"](w, L3)
        for b in tb: w.append(b)
    for b in tr: w.append(b)
    if bytes(w) != t:
        raise ValueError("not byte-exact")
    return True


def _try(t, seq):
    try:
        parse_tail(t, seq)
        return True
    except Exception:
        return False


# the records the settled grammar cannot do -> exactly the ones with a
# non-empty slot-2 list
hard = []
for t in tails:
    try:
        parse_tail(t, [])          # empty X can't consume a real element
        continue
    except Exception:
        hard.append(t)
print(f"tails={len(tails)}  needing the nested element: {len(hard)}")

names = ["u8", "u16", "u32", "f32", "carr32", "carr8"]
scores = []
for depth in (1, 2, 3, 4, 5):
    for seq in product(names, repeat=depth):
        if not any(s.startswith("carr") for s in seq):
            continue
        good = sum(1 for t in hard
                   if _try(t, list(seq)))
        if good:
            scores.append((good, seq))

scores.sort(reverse=True)
print()
print(f"{'X grammar':<44} {'hard recs parsed':>16}")
print("-" * 62)
for good, s in scores[:12]:
    print(f"{' + '.join(s):<44} {good:>7} / {len(hard)}")
if not scores:
    print("  nothing parsed a single hard record")
