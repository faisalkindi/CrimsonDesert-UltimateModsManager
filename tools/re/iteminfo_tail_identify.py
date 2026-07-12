"""Which field is the 1.13 tail list: prefab_data_list or
gimmick_visual_prefab_data_list?

1.10 decodes BOTH. Take items present in both builds, pull the 1.10
prefab_names hashes out of each field, and see which set matches the
hashes in the 1.13 tail element's first list. That settles identity
without guessing from the shape.
"""
from __future__ import annotations
import struct, sys
from collections import Counter

sys.path.insert(0, "src"); sys.path.insert(0, ".")
from tests.fixture_loaders import load_vanilla110, load_vanilla113
from cdumm.semantic.parser import parse_pabgh_index
from cdumm.engine import iteminfo_native_parser as P


def load(loader):
    b = loader("iteminfo.pabgb"); h = loader("iteminfo.pabgh")
    _k, o = parse_pabgh_index(h, "iteminfo")
    s = sorted(o.values())
    return b, o, s, P.detect_iteminfo_layout(b, s)


b10, o10, s10, f10 = load(load_vanilla110)
b13, o13, s13, f13 = load(load_vanilla113)


def tail_L0(key):
    """First list of the first tail element, on 1.13."""
    s = o13[key]
    i = s13.index(s)
    e = s13[i + 1] if i + 1 < len(s13) else len(b13)
    r = P._Reader(b13, s, rec_end=e)
    P._read_item(r, fields=f13)
    t = bytes(b13[r.pos:e])
    if len(t) < 8:
        return None
    n = struct.unpack_from("<I", t, 0)[0]
    if n == 0 or n > 64:
        return None
    p = 4 + 12                      # skip count + scale
    c = struct.unpack_from("<I", t, p)[0]
    if c > 64 or p + 4 + 4 * c > len(t):
        return None
    return set(struct.unpack_from(f"<{c}I", t, p + 4)) if c else set()


def names_from(it, field):
    out = set()
    for p in (it.get(field) or []):
        if isinstance(p, dict):
            out |= set(p.get("prefab_names") or [])
    return out


hit_prefab = hit_gvp = both = neither = 0
ex = []
for key in sorted(set(o10) & set(o13)):
    t0 = tail_L0(key)
    if not t0:
        continue
    s = o10[key]
    i = s10.index(s)
    e = s10[i + 1] if i + 1 < len(s10) else len(b10)
    try:
        r = P._Reader(b10, s, rec_end=e)
        it = P._read_item(r, fields=f10)
    except Exception:
        continue
    pf = names_from(it, "prefab_data_list")
    gv = names_from(it, "gimmick_visual_prefab_data_list")
    inp = bool(t0 & pf)
    ing = bool(t0 & gv)
    if inp and ing: both += 1
    elif inp: hit_prefab += 1
    elif ing: hit_gvp += 1
    else:
        neither += 1
        if len(ex) < 4:
            ex.append((key, sorted(t0)[:2], sorted(pf)[:2], sorted(gv)[:2]))

print("Does the 1.13 tail element's first list match ...")
print(f"  1.10 prefab_data_list only          : {hit_prefab}")
print(f"  1.10 gimmick_visual_prefab_list only: {hit_gvp}")
print(f"  both                                : {both}")
print(f"  neither                             : {neither}")
print()
for key, t, p, g in ex:
    print(f"  key={key:<7} tail={t} prefab={p} gvp={g}")
