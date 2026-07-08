# Game-Data Unlock — Status & Roadmap

Roadmap for unlocking more Crimson Desert `.pabgb` game-data tables for **Format 3
(`.field.json`) field-name modding** in CDUMM. This is contributor-facing: it
records the core blocker, the `iteminfo` reverse-engineering done so far, and the
concrete paths to finish so nobody has to re-derive it.

---

## 1. The goal

Let users mod more of Crimson Desert's ~322 `.pabgb` data tables by **field name**
(the Format 3 flow), beyond the ~10 tables supported today. The headline target is
`iteminfo` (weapons / armour / consumables).

---

## 2. Where the field-name flow stands

- The engine that **applies** Format 3 mods ships in CDUMM.
- Per-field write locations live in `field_schema/<table>.json` (see
  `field_schema/README.md`) and/or the `_ordered_fields` overrides in
  `schemas/pabgb_type_overrides.json`.
- A **verified-only** mechanism gates both display and apply: `TableSchema.verified_fields`
  marks fields validated against real record data; the Game Data tab renders every
  other field as `(unverified)`, and `format3_apply._resolve_write_pos` refuses to
  write any field a table's `verified_fields` doesn't vouch for. This is what keeps
  an unproven offset from silently corrupting a table. `wantedinfo` is the reference
  example of a single hand-verified field.

---

## 3. The core blocker (read before starting)

**A. Memory-order schema.** The shipped `schemas/pabgb_complete_schema.json`
(from NattKh, MPL-2.0) sorts each table's fields by the field descriptor's **memory
address in the exe**, *not* by on-disk serialization order. So any table without a
hand-authored `_ordered_fields` override reads fields at the wrong offsets and
produces garbage (e.g. `mercenaryinfo` booleans decode to the ASCII bytes of
"mer**cenary**"; `relationinfo` reads the high halves of floats).

**B. Game-version drift.** The current game build is **1.12**. Every *readable*
community reference — NattKh's `iteminfo.hexpat`, the decoded `iteminfo_dump`, and
even the **1.11** `dmm_parser` type stubs — is 1.11 or older. The 1.12 patch changed
record layouts, so even a correct ≤1.11 layout desyncs on current data.

**Net:** every unsupported table needs its complete 1.12 field order reverse-engineered
by hand, and no version-matched reference currently hands it to us. Confirmed
empirically: **0 of ~312 unsupported tables fully decode on 1.12**, and cheap
correctness heuristics (e.g. "`_isBlocked` is 0/1") are unreliable.

---

## 4. `iteminfo` — findings

`iteminfo`'s override **already has the full 113-field `_ordered_fields`** and all
41 sub-structs / tagged variants in `src/cdumm/semantic/pabgb_types.py`. The schema
is not the gap — **1.12 layout drift is.** On 1.12 the walker stalls at field 14
`_itemIconList` for 6477 / 6483 items.

Three 1.12 drift points have been reverse-engineered, cascading the decode from
**11 → 43 of 113 fields**, each decoded field verifying against the launch answer-key
dump (item 1000080 "Hwando"):

1. **`ItemIconData` grew 9 → 14 bytes** and absorbed `map_icon_path`:
   `{ icon_path u32, map_icon_path u32, unk6 [u8;6] }` — and the separate
   `_mapIconPath` field after `_itemIconList` is **removed** (now per-icon).
   Verified by fingerprint: `materialKey==materialMatchInfo` sits at offset 24 from
   the stall for 1-icon items, 38 for 2-icon items → +14 bytes/element.
2. **Insert after `_extractMultiChangeInfo`:**
   `{ u16 marker=0xFFFF, CString filter_category_name, u32 a=0, u32 group_key, u32 b=0 }`.
3. **Insert after `_isAllGimmickSealable`** (7 bytes):
   `{ u8 0, u8 0, u8 1, u32 hash=0x9D7C0DD0 }`.

**Remaining for `iteminfo`:**
- The 5 consecutive `CArray<SealableItemInfo>` sealable-list fields drift on 1.12
  (element size/type changed — 4-byte `u32` keys observed where 13+ byte structs
  were expected; list counts land on garbage). Neither insert-2 sizing (7/8/9) nor
  swapping the lists to `CArray<u32>` cleared it — needs the exact restructure.
- Past the sealable region sit the high-value tail scalars: `_itemTier`, `_cooltime`,
  `_maxEndurance`, durability, charge counts, `_respawnTimeSeconds` (and
  `_equipableLevel`, `_categoryInfo`, which already decode).

> **Do not ship a partial `iteminfo` schema.** A half-mapped item layout can misplace
> writes and corrupt the item table. Finish the full 1.12 walk, or gate strictly to
> the verified subset, before shipping.

---

## 5. Why there's no quick "easy table" instead

A survey for a small, already-correct table found: 79 unsupported tables are
all-primitive, but **none fully decode to the record boundary on 1.12** (the shipped
schema is missing/mis-ordering fields for all of them). Even "supported" tables
aren't fully decoded (`fieldinfo` leaves a 21-byte gap, `dropsetinfo` ~99 bytes) —
they only map the fields people mod. Correctness heuristics are unreliable. So each
table is genuine hand-RE + per-field verification; there is no shortcut table.

---

## 6. Licensing (respect it)

- **Usable now (MPL-2.0):** `NattKh/CRIMSON-DESERT-SAVE-EDITOR-AND-GAME-MODS`
  (root `LICENSE.txt` is MPL-2.0). CDUMM already ships its `pabgb_complete_schema.json`
  under MPL-2.0 per `schemas/NOTICE`; port from here with attribution.
- **Not usable yet (no license = all rights reserved):**
  `NattKh/CrimsonDesertModdingTools` — the repo holding
  `pabgb_full_schema_with_readers.json` (reader-order schema) and extra per-table
  parsers. Do not copy it in until it carries an explicit license (request open at
  `NattKh/CrimsonDesertModdingTools` issue #1). "Public repo" ≠ "free to use."

---

## 7. Two ways to finish

**Path A — reader-order schema (preferred, sustainable).** Once
`CrimsonDesertModdingTools` is licensed (or a 1.12 update ships), take
`pabgb_full_schema_with_readers.json` (or the per-table parsers) and **auto-generate
`_ordered_fields`** for many tables at once; verify each against game data, mark
`verified_fields`, ship. Mechanical rather than byte-by-byte; regenerating after each
patch keeps it from rotting.

**Path B — per-table hand-RE (works today, slow).** The `wantedinfo` method: pick a
table, find one field whose value is **independently cross-checkable** in-game
(`wantedinfo._increasePrice=1500` matched the bounty economy), verify its offset, add
it to `_ordered_fields` + `verified_fields`, ship. Reliable but linear.

**Path C — ASI reflection dumper (durable, biggest effort).** The version-proof source
is the game's own type registry, read at runtime from the packed exe via an injected
ASI probe. A reflection dump yields authoritative field order/types for every table
and every future patch. Large, multi-session RE.

---

## 8. Next-step checklist

- [ ] Watch `CrimsonDesertModdingTools` issue #1. If licensed → **Path A**: script
      `_ordered_fields` generation from the reader-order schema, verify, batch-PR tables.
- [ ] Finish `iteminfo` 1.12: crack the sealable-list restructure → reach the tail
      scalars → verify vs the launch dump → add `_ordered_fields` + `verified_fields`
      + `field_schema/iteminfo` → tests → PR (stacks on the `verified_fields` mechanism).
- [ ] Interim option: ship `iteminfo` with `verified_fields` limited to the fields that
      already decode + verify (§4), so those specific fields display real values and
      become moddable — only if the apply gate reliably refuses everything else.
- [ ] Longer term: the ASI reflection dumper as the patch-proof source.

---

## 9. Correctness & attribution notes

- Never ship an unverified/partial table schema — a wrong offset silently writes to
  the wrong bytes. The `verified_fields` gate exists to keep unproven fields un-writable.
- Hand-RE'd 1.12 layouts break on the next patch; Path A/C are the durable fixes.
- Keep `schemas/NOTICE` and the `pabgb_type_overrides.json` `_meta` provenance accurate
  for anything ported from NattKh (MPL-2.0) or Potter420/crimson-rs (MIT).
