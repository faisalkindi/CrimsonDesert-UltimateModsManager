# Game-Data Unlock — Status & Roadmap

How to let CDUMM mod more of Crimson Desert's `.pabgb` data tables by field
name (the Format 3 flow). Contributor-facing: records the blocker, what's
been done, what's been *ruled out*, and the two paths that remain — so
nobody re-derives it.

**Last verified: 2026-07-11, game build 1.13.**

---

## 1. Where we actually are

Measured against the installed game (134 `.pabgb` tables shipped):

| | tables |
|---|---|
| **Full decode + edit** (native writer and/or hand-RE'd `_ordered_fields`) | **13** |
| Schema exists, but in **memory order** → lands on the wrong bytes | 82 |
| No schema at all | 41 |

The 13: `iteminfo`, `characterinfo`, `dropsetinfo`, `equipslotinfo`,
`multichangeinfo`, `skill`, `storeinfo`, `stringinfo`, `fieldinfo`,
`stageinfo`, `regioninfo`, `vehicleinfo`, `wantedinfo`.

**`iteminfo` is done** (this is the part of the old roadmap that was most
stale — it used to say "43 of 113 fields on 1.12"):

- all 6508 records decode on 1.13, 0 opaque, byte-exact round-trip
- all 3151 equipment records decode (they were silently desynced — see
  `iteminfo_native_parser`, the SubItem-tag-17 + `_enchantDataList` fix)
- gear stats (armour/weapon) are readable **and** writable
- the semantic walker reaches 110 of 113 fields (was 11)

---

## 2. The blocker, stated precisely

`schemas/pabgb_complete_schema.json` lists each table's fields **sorted by
the field descriptor's memory address in the exe**, not by on-disk
serialization order. Walk it and you read the wrong bytes — `mercenaryinfo`
booleans decode to the ASCII of "mer**cenary**"; `relationinfo` reads the
high halves of floats.

Two things worth knowing, both established 2026-07-11:

**(a) The order was destroyed by the dump, not by the game.** All 347 types
in the schema are sorted by address `s`. The extractor sorted them. So this
is an *extraction* problem, not an RE-from-scratch problem.

**(b) There is no cheap rule to recover it.** Tested, on the 7 tables where
both the schema order and a verified file order are known: sorting by `s`
(address), by `fn` (function pointer), by `stream`, and the raw schema list
order — **none** reproduces file order on any table.

> Beware a tempting false positive here: sorting by `fn` *appears* to work
> if you sort a list that is already in file order — `fn` is near-constant,
> so a stable sort returns the input unchanged. It is a tautology. Sort the
> *schema* order and compare; it fails.

A rule that is right for 90% of fields is worse than no rule: it silently
writes the other 10% to the wrong bytes.

---

## 3. What the wider ecosystem has (researched 2026-07-11)

Every public tool was checked, with its licence. **None solves this.**

| project | licence | what it does with `.pabgb` |
|---|---|---|
| [LukeFZ/pycrimson](https://github.com/LukeFZ/pycrimson) | **MIT** | Raw bytes only — slices rows by offset, returns `dict[offset → bytes]`. Its own notes call pabgb *"raw table entries"*. **No field decode.** |
| [hzeemr/crimsonforge](https://github.com/hzeemr/crimsonforge) | **MIT** | Heuristic guesser: walks 4 bytes at a time guessing u32/f32/string. **No field names, no struct/array awareness.** Fine for viewing; unsafe to write with. |
| [NattKh/CrimsonDesertModdingTools](https://github.com/NattKh/CrimsonDesertModdingTools) | **NONE** (all rights reserved) | **Has real reader-order parsers** (`parsers/*.py`) — the thing we need. Cannot be used. |
| exodiaprivate-eng/dmm-parser | CDMTL | Explicitly prohibits use by a competing mod manager. **Do not read, do not port.** |

**The finding that matters:** LukeFZ — a serious reverse engineer — hit the
*exact same wall* and did not attempt field decode. That is independent
confirmation the schema genuinely is not in the shipped data files.

### Confirmed: the schema is not in the assets

The game's other formats (`prefab`, `paseq`, `pae`, `parg`, `pasg`,
`meshinfo`, `paa_metabin`, `palevel`) **are** reflection-serialised: each
carries a type table of `{type name → ORDERED property list}` — exactly the
shape we want.

Tested: harvested the type tables from a sample of those assets — parsed 36
of 60, yielding 44 distinct types (`SceneObject`, `Material`,
`SkinnedMeshComponent`, …). **0 of our 134 data-table types appear.** The
reflection assets carry scene/render types only. The data-table type
registry exists **only in the exe**.

---

## 4. The two paths that remain

**Path A — get `CrimsonDesertModdingTools` licensed (cheapest by far).**
It already contains reader-order parsers. With a permissive licence we can
auto-generate `_ordered_fields` for many tables at once and verify each
against game data — mechanical, not byte-by-byte.

Status: licence request is **open at issue #1 since April, zero replies**;
the repo hasn't been pushed to in 3 months.

*New leverage:* we are now in live contact with NattKh — we sent a
substantive PR to `CrimsonDesertCommunityItemMapping` (correcting 18 wrong
stat ids and adding the 51 missing ones). That is a far better moment to
ask than a cold issue was.

**Path C — read the game's own type registry (authoritative, version-proof).**
The registry that says which fields, in which order, is in the exe. Read it
and every table's order falls out — no trusting anyone's hand-work.

This path was investigated directly against the installed 1.13 exe
(`bin64/CrimsonDesert.exe`, 344 MB) on 2026-07-11. Findings, all read-only:

**Strongly positive — the metadata is NOT encrypted at rest:**
- Type-name strings are present: `ItemInfo` is referenced 215 times,
  `BuffInfo` 19, `DropSetInfo` etc. all present.
- **105 of 113 ItemInfo field-name strings are present as plain ASCII** in
  the exe. (The 8 absent are the synthetic `_unk_*` names CDUMM invented,
  not real game strings.) So the packer protects the *code*, not the
  reflection strings.
- The PE maps cleanly (image base `0x140000000`, normal section table), so
  VA→file-offset resolution works.

**Two shortcuts that were tried and DON'T work (recorded so they aren't
retried):**
- *Replaying the old schema's addresses.* `pabgb_complete_schema.json`
  carries an `s` (address) per field. On the current build those addresses
  are **stale** — they resolve to unrelated Havok class metadata
  (`hkClassEnum`, `declaredEnums`, `inertiaTensor`), not ItemInfo
  descriptors. The dump is from an older build; the addresses drifted. You
  cannot statically walk "at address `s`" on the live game.
- *Assuming it's Havok reflection.* The stale pointers landing in Havok data
  made this look plausible, but the counts refute it: `hkClassMember`
  appears **once**, `hkClass` five times — nowhere near enough to describe
  ~322 tables. The pabgb registry is Pearl Abyss's **own** system (matching
  pycrimson's custom `ReflectionType`/`ReflectionProperty`, not `hkClass`).

**So the reliable form of Path C is a live reader-order hook, not static
walking.** When the game loads a `.pabgb`, its per-type transfer function
reads the fields in serialization order. An injected ASI that hooks that
function and logs the call sequence observes the ground-truth order
directly — for every table, on whatever build is running, with no dependence
on a drifting static layout. This is aligned with how CD RE already has to
be done (the exe is packed; live-memory capture is the established method).

pycrimson (MIT) still helps: it documents the shape the transfer produces —
`ReflectionType{ name, properties[] }` /
`ReflectionProperty{ name, type_name, type, fixed_size, flags }` with an
ordered property list — so we know what a correct dump should look like.

Effort: real, multi-session (build the ASI, find the transfer/reader
dispatch signature, capture, verify). But it is the only path that is
**authoritative and survives patches**, and the metadata being unencrypted
means the packer is not a blocker.

**Path B — per-table hand-RE (works today, slow, linear).** The
`wantedinfo` method: pick a table, find one field whose value is
independently cross-checkable in-game, verify its offset, add it to
`_ordered_fields` + `verified_fields`, ship. Reliable, but one table at a
time — and hand-RE'd layouts break on the next patch.

---

## 4a. The prerequisite for ALL of them: a verification harness

"Who's to say the rest works?" is the right question — and it is what sank
naive trust in the community stat mapping (18 of 24 entries wrong, all
flagged `verified`). The answer is not to trust any source, but to make
every source **prove itself against ground truth we already hold.**

CDUMM already has **13 tables with a verified, byte-exact file order**
(§1). That is a free oracle. Any candidate order source — NattKh's parsers
(Path A), a live ASI dump (Path C), or a hand-RE (Path B) — must, for each
of those 13 tables:

1. reproduce the known `_ordered_fields` **exactly**, and
2. round-trip the committed vanilla fixture **byte-for-byte**.

A source that fails on any of the 13 is rejected outright. A source that
passes all 13 has earned the benefit of the doubt on the other 82 — and
even then each new table is gated to `verified_fields` (values
cross-checked against real records) before its fields become writable.

This is cheap to build now (the 13 orders and their fixtures are in-repo),
it is needed regardless of which path wins, and it is the concrete answer
to the trust problem. **Build it first.** It converts "do we believe
NattKh / our own dump?" into "it passed 13/13 or it didn't."

---

## 5. Adjacent opportunity (not the pabgb gap, but real)

pycrimson's **reflection parser is MIT and generic** — and CDUMM currently
has *zero* support for the reflection-serialised formats: `prefab` (47,343
assets), `paseq` (4,688), `palevel`, `meshinfo`, `paa_metabin`, `pae`.

That is an entire modding surface (scenes, sequences, levels, materials)
that is *self-describing* — no schema RE needed, the field names and order
are in the file. If we want breadth rather than depth, this is the cheapest
big win available, and the licence is clean (MIT, with attribution).

---

## 6. Rules

- **Never ship an unverified or partial table schema.** A wrong offset
  silently writes to the wrong bytes. The `verified_fields` gate exists to
  keep unproven fields un-writable — an unproven field is *refused*, not
  guessed.
- Hand-RE'd layouts rot on the next patch. Paths A and C are the durable
  fixes; Path B is a stopgap.
- Keep `schemas/NOTICE` and the `_meta` provenance in
  `pabgb_type_overrides.json` accurate for anything ported from NattKh
  (MPL-2.0), Potter420/crimson-rs (MIT), or LukeFZ/pycrimson (MIT).
- `exodiaprivate-eng/dmm-parser` is CDMTL-licensed and explicitly targets
  competing mod managers. Do not read it, do not port from it, do not
  derive parity from it. Everything in CDUMM is our own RE.

---

## 7. Next steps

- [ ] **Build the verification harness (§4a) first.** It's cheap, it's
      needed by every path, and it is the answer to "who's to say the rest
      works." Nothing else here is trustworthy without it.
- [ ] **Path C is the recommended durable answer**, and it is de-risked:
      the reflection metadata is unencrypted in the exe (§4). Scope the ASI
      reader-order hook. Static replay of the old schema addresses is out
      (they've drifted); the live hook observes ground-truth order and
      survives patches.
- [ ] **Path A in parallel, as the cheap interim:** bump
      `CrimsonDesertModdingTools` issue #1 on the back of the
      community-mapping PR. If it gets licensed, its parsers go straight
      through the §4a harness — no blind trust. Good contact exists now.
- [ ] Adjacent: evaluate MIT-licensed reflection support for `prefab` /
      `paseq` / `palevel` — a whole new moddable surface with no schema
      problem (§5).
- [ ] Housekeeping: the semantic walker is still broken on CD 1.10 (64 of
      113 fields, never reaches `_cooltime`) — documented and pinned, not
      fixed.

> Provenance of §4/§4a: read-only inspection of the installed 1.13 exe on
> 2026-07-11 (string presence, PE section map, descriptor bytes at the
> schema's `s` addresses, Havok-fingerprint counts). No code was extracted
> from the exe; only its own metadata was measured to scope the work.
