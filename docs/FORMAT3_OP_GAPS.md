# Format 3 op coverage — what CDUMM doesn't support yet

> **STATUS (resolved in PR #271):** the gaps below are closed. `match`,
> `clone_record`, `delete_record` and `new_record` (template) now apply;
> `array_append` applies for `dropsetinfo.drops` and gives an actionable
> skip elsewhere. User-facing reference: `docs/FORMAT3_OPS.md`. This note is
> kept for the history of how the gap was derived.

Internal tracking note, not user-facing. Records a verified gap between what
Format 3 (`.field.json`) mods *can* express and what CDUMM's apply pipeline
currently accepts, so it isn't re-derived from scratch later.

---

## 1. Current state

`format3_handler.py` and `format3_apply.py` both define:

```python
_SUPPORTED_OPS = frozenset({"set"})
```

That's the entire list. Verified 2026-07-08 by grepping the whole intent-processing
pipeline (`format3_handler.py`, `format3_apply.py`, every table writer) for
`clone_record`, `new_record`, `delete_record`, `array_append`, and `match` — none
appear as handled op types anywhere.

An intent using an unsupported op is silently skipped per-intent (same failure
shape as an unsupported field): no crash, no corruption, just zero effect. Matches
the existing "refuse rather than guess" discipline elsewhere in the apply pipeline.

## 2. What the wider mod ecosystem uses beyond `set`

Community field-JSON tooling (DMM's V3 format, documented in a mod-author guide
seen 2026-07-08, `Downloads/Compressed/V3 Mod Guide .../V3_MOD_GUIDE.md`) uses:

- `array_append` — append one element to a list field (`field` + `value`) instead
  of replacing the whole array. CDUMM's list writers (`LIST_WRITERS`,
  `SUPPORTED_FIELDS` in `iteminfo_writer.py`) already do whole-field replace via
  `set`; this would be an incremental variant of that.
- `clone_record` — copy an existing record to a new key, then patch a few fields
  on the copy (`source_key`, `new_key`, `patches: [{path, new}, ...]`). This is
  how new equipment/item variants get made without hand-building a whole record.
  Not equivalent to anything CDUMM does today.
- `new_record` — build a record from a full field template. Lower priority than
  `clone_record`: even the community guide recommends cloning over this ("building
  a valid record from nothing is hard, cloning one that already works is easy").
- `delete_record` — remove a record by key. Not seen requested against CDUMM yet.
- `match` selector — `{"match": {field: value, ...}, "op": "set", ...}` applies one
  intent to every record whose fields match (AND across conditions), instead of
  targeting one `key`/`entry`. Turns a thousand-item batch edit (e.g. "give every
  item of type X 5 sockets") into one intent instead of one per item. This is a
  *selector* layered on existing ops, not a new op — cheapest of these to add on
  top of the current `set` path once the matching logic exists.

## 3. Priority read

No open CDUMM issue currently blocked specifically on a missing op (as of
2026-07-08) — every report handled this session used plain `set`. This is a
capability gap, not an active fire. If it becomes one:

1. `match` selector is the most leveraged addition — turns batch-edit mods (a
   real, seen-in-the-wild use case) from "impossible without N hand-authored
   intents" into "just works," and composes with the existing `set` path rather
   than requiring a new write mechanism.
2. `clone_record` is the next most requested capability based on what the
   community guide treats as the standard way to make item variants — but it
   needs real design work: new-key collision handling, whole-record copy +
   patch semantics, and validation that a cloned record round-trips before
   landing.
3. `array_append` / `new_record` / `delete_record` — no evidence of demand yet.
   `array_append` is the safest of the three to add (mechanically close to the
   existing whole-list `set` writers).

## 4. Do not source field names/paths from `exodiaprivate-eng/dmm-parser`

That repo (surfaced via the same mod-author guide) carries a custom license
(CDMTL v1.0, RicePaddySoftware) that explicitly restricts use to their own tool
suite and explicitly prohibits clean-room reimplementation by a competing mod
manager, even without reading their source. CDUMM is exactly the kind of tool
that clause targets. Any op/selector support built from this note should come
from CDUMM's own independent RE (same as every other table in this codebase),
never from that repo's field catalog or parser behavior.
