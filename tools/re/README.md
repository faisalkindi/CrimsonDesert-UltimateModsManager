# Reverse-engineering the CD 1.13 iteminfo record tail (#285)

Reproducible harness for the finding that CDUMM's 1.13 item layout is
**incomplete**, and for identifying what it's missing.

Run any of these from the repo root with `PYTHONPATH=src`.

## The problem it found

Every one of the 6,508 records in the CD 1.13 `iteminfo` table carries
**76-139 bytes of tail that the layout never interprets**. They survive a
byte-exact round-trip only because the parser preserves them opaquely as
`_tail_slack`.

That is the uncomfortable lesson worth writing down: **a byte-exact
whole-table round-trip is silent on this.** It proves the bytes are
*preserved*, not that they are *understood*. It is still the right
acceptance gate — it just cannot, on its own, tell you the decoder is
complete. Check `_tail_slack` too.

## What the tail is

`iteminfo_tail_identify.py` settles it without guessing from shape. CD 1.10
decodes *both* `prefab_data_list` and `gimmick_visual_prefab_data_list`, so
take the items present in both builds and ask which one's `prefab_names`
hashes appear in the 1.13 tail:

| 1.13 tail's first list matches | items |
|---|---|
| 1.10 `prefab_data_list` | **0** |
| 1.10 `gimmick_visual_prefab_data_list` | **3,433** |

The tail is `gimmick_visual_prefab_data_list`, **relocated to the end of the
record** in 1.13. Zero counterexamples.

## The grammar

`iteminfo_tail_grammar.py` searches candidate grammars, scored only by
"consumes the tail to zero bytes AND re-serializes byte-identical". Winner,
at **6,272 / 6,508 (96.4%)**:

```
tail = CArray<Elem> + u8 + u8
Elem = scale : 3 x f32
       L0    : CArray<u32>      <- prefab_names (verified above)
       L1    : CArray<u32>
       L2    : CArray<X>        <- nested struct; EMPTY on all 6,272
       L3    : CArray<u32>
       3 x u8
```

Sanity check that falls out for free: the 821 records with a 6-byte tail are
`00000000 ff 00` — an *empty* list plus the same 2-byte trailer. The base
case is the same grammar.

## What's still open

The 236 records where `L2` is **non-empty**. `iteminfo_tail_solve_nested.py`
enumerates flat token sequences for `X` and intersects across all 236: **no
flat sequence fits**, so `X` is variant-shaped.

Strong hypothesis, consistent with everything above: 1.10's `PrefabData`
(`prefab_names`, `equip_slot_list`, `tribe_gender_list`) and
`GimmickVisualPrefabData` (`tag`, `scale`, `prefab_names`,
`animation_path_list`, `use_gimmick_prefab`) were **merged** in 1.13 — which
is why the element has a scale *and* four lists — and `X` is the
`tribe_gender_list` element. The 1.10 codec (`_read_PrefabDataTribe`) already
treats that element as multi-family with an opaque fallback, so a variant
shape is exactly what we should expect.

That matters beyond tidiness: `tribe_gender_list` is precisely the field
"Equip Everything" (13k downloads) writes.

## Acceptance bar for finishing this

Do not accept a decoder that merely round-trips. Require:

1. `_tail_slack` is **empty** on all 6,508 records — zero bytes uninterpreted; and
2. the whole table re-serializes byte-identical.

(1) without (2) is a mis-parse. (2) without (1) is what got us here.
