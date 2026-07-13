# Format 3 (`.field.json`) operations — mod author reference

CDUMM applies Format 3 mods by name (entry + field), not raw byte offsets.
A mod declares a `target` (the `.pabgb` table it edits) and a list of
`intents`. This document covers every operation CDUMM supports and its
exact JSON shape.

Every reshaping operation is **safe by construction**: it is append-only or
a full rebuild, and CDUMM re-decodes the result and verifies it before
committing. If an operation can't be applied safely it is **skipped with a
reason** (shown after apply) — it never corrupts the table.

Fields you edit must be **verified** for that table (CDUMM proves a field's
byte offset against real record data before allowing writes). Unverified
fields render as `(unverified)` in the Game Data tab and are refused.

---

## `set` — change a field

The default. Change one field on one record, located by `entry` name (and
optionally numeric `key`).

```json
{ "entry": "Hwando", "key": 1000080, "field": "_itemTier", "new": 5 }
```

`op` may be omitted — `set` is implied.

---

## `match` — batch edit

Apply one edit to **every record whose fields all equal the given values**
(AND across conditions), instead of naming a single record.

```json
{ "match": { "_storeType": 3 }, "field": "_buyPriceRate", "new": 50 }
```

- Match on any **verified** field, or on the metadata `_name` / `_key`.
- Internally each match becomes one ordinary `set` per matched record, so
  it composes with everything else.

---

## `clone_record` — copy a record (make a variant)

Deep-copy an existing record to a **new key** (and optional new name), then
patch a few fields on the copy. This is the standard way to make an item or
gear variant.

```json
{
  "op": "clone_record",
  "source_key": 1000080,
  "new_key": 1000090,
  "new_name": "Hwando+",
  "patches": [ { "field": "_itemTier", "new": 5 } ]
}
```

- `new_key` must be unused and fit the table's key width; a collision is
  refused.
- The copy is byte-identical to the source except your patches.
- Patches target **verified scalar fields** (same rule as `set`), and on
  `iteminfo` also **`gear_stat[...]`** — so you can clone a weapon or piece
  of armour and change its damage/defense on the copy:

```json
{
  "op": "clone_record",
  "source_key": 50003,
  "new_key": 50003001,
  "new_name": "Sharpened Blade",
  "patches": [ { "field": "gear_stat[1000000]", "new": 999999 } ]
}
```

  A `gear_stat[...]` edit is a byte-exact same-width overwrite of the stat
  in the copy; editing a stat the item doesn't carry is a clean no-op.

---

## `new_record` — build from a template

Create a record based on an existing one. Provide a `source_key` (or
`template_key`) to copy from, plus `new_key`, an optional `new_name`, and
`patches`. This routes through the same engine as `clone_record`.

```json
{
  "op": "new_record",
  "template_key": 1000080,
  "new_key": 1000091,
  "new_name": "Custom Blade",
  "patches": [ { "field": "_itemTier", "new": 3 } ]
}
```

Building a record from a bare field list (no template) is **not supported** —
it needs a per-table serializer CDUMM doesn't have for most tables, and the
community-recommended path is to clone a record that already works.

---

## `delete_record` — remove a record

Remove a record by key. CDUMM rebuilds the table body from the survivors and
reindexes the companion `.pabgh`.

```json
{ "op": "delete_record", "key": 1000090 }
```

> Byte-safety only: CDUMM guarantees the table stays well-formed, **not**
> that the game is happy with a record other tables still reference. Delete
> records you added, or ones nothing else points at.

---

## `array_append` — add one element to a list

Append a single element to a list field without rewriting the whole list.

```json
{
  "op": "array_append",
  "entry": "DropSet_Faction_Graymane",
  "key": 175001,
  "field": "drops",
  "new": { "flag": 1, "item_key": 1000080, "rates": 5000 }
}
```

Supported today for list fields whose format CDUMM can round-trip
byte-exact, so existing elements are never disturbed:

| Table          | Field |
|----------------|-------|
| `dropsetinfo`  | `drops` |
| `iteminfo`     | any **nested list path** (e.g. `drop_default_data.add_socket_material_item_list`) |

On `iteminfo`, an `array_append` is expanded to a `set` of
`current_list + [element]` and applied through the byte-exact whole-table
writer, so exactly one record grows and its `.pabgh` index is rebuilt —
nothing else moves. The path must point at a list; a bare scalar field is
refused with a note. `array_append` also composes with `match`, so you can
append one element to **every** record matching a selector (e.g. add a
socket-material entry to every item with `drop_default_data.use_socket == 1`)
in a single intent.

Example — add one socket-material entry to a specific item:

```json
{
  "op": "array_append",
  "entry": "Some_Helm",
  "key": 14510,
  "field": "drop_default_data.add_socket_material_item_list",
  "new": { "item": 1000080, "value": 5000 }
}
```

For any list field CDUMM can't yet round-trip byte-exact, `array_append`
is skipped with a note to use a `set` intent whose value is the **full new
list** (the current items plus your addition).

---

## Multiple targets

A single mod file may edit several tables via a `targets` array; each entry
has its own `target` + `intents` and follows all the rules above.

## What happens on apply

CDUMM reports, per mod, how many intents applied and how many were skipped
and why (in the post-apply message and the log). A skipped intent is never a
partial write — it's all-or-nothing per operation.
