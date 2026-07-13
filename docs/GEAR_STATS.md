# Editing gear stats (armour / weapons)

Gear stats are readable and editable as of CD 1.13. Previously they were
neither: the equipment records didn't decode at all, so every one of the
3151 equipment items in `iteminfo` came back with empty stats.

## Where the stats live

Two places, both on the item's `iteminfo` record:

```
sharpness_data
  max_sharpness                       weapon sharpness cap
  stat_list[N]                        the item's BASE stats
    stat                              stat id (see below)
    change_mb                         the value

enchant_data_list[T]                  one entry per enchant tier, T = 0..N-1
  level                               == T
  enchant_stat_data
    stat_list_static[N]               stat values AT that enchant tier
    stat_list_static_level[N]
    max_stat_list[N]
    regen_stat_list[N]
  equip_buffs[N]
  item_effect_info
```

`enchant_data_list` is the interesting one: it holds the value the stat
takes at each enchant level. Marni_Devotee_PlateArmor_Helm, for example,
has a base `1000` and tiers running `2000, 3000, 4000, ...` — one entry
per tier, monotonically non-decreasing. (That monotonicity is asserted in
the test suite; it's the check that catches a mis-decoded layout, which
would otherwise still round-trip byte-exact.)

## Writing them (Format 3)

Use a dotted or bracketed path as the `field`. Both dialects work, on
both `set` and `match`:

```json
{
  "entry": "Marni_Devotee_PlateArmor_Helm",
  "key": 14510,
  "field": "sharpness_data.stat_list.0.change_mb",
  "op": "set",
  "new": 7777
}
```

```json
{
  "entry": "Marni_Devotee_PlateArmor_Helm",
  "key": 14510,
  "field": "enchant_data_list[0].enchant_stat_data.stat_list_static[0].change_mb",
  "op": "set",
  "new": 8888
}
```

Combine with `match` to hit a whole class of gear in one intent — e.g.
every item with a socket, or every item of one `equip_type_info`.

A path that doesn't resolve (index out of range, missing segment) is
**skipped and logged**, never guessed at. A value whose type doesn't match
the existing one is refused rather than allowed to corrupt the table on
serialize.

## Stat IDs

The names are **in the game**: `gamedata/statusinfo.pabgb` maps every stat
id to its name. CDUMM reads it from your install (`load_stat_names()`), and
ships a snapshot of the CD 1.13 table as a fallback
(`cdumm.engine.stat_names.STAT_NAMES_CD113`, 75 entries). Prefer the live
read — a hardcoded table goes stale the moment the game adds a stat.

The 15 ids that vanilla 1.13 gear actually uses:

| id | name | notes |
|---|---|---|
| `1000000` | Hp | |
| `1000002` | DDD | damage dealt — on nearly every item |
| `1000003` | DPV | defence — on nearly every item |
| `1000005` | DDV | |
| `1000007` | CriticalRate | |
| `1000008` | AttackedDamageRate | |
| `1000010` | AttackSpeedRate | |
| `1000011` | MoveSpeedRate | |
| `1000012` | ClimbSpeedRate | |
| `1000017` | IceResistance | |
| `1000026` | Stamina | |
| `1000027` | Mp | |
| `1000036` | Pressure | |
| `1000037` | Stamina_UseResourceDecreaseRate | |
| `1000043` | GuardPVRate | 92% of its carriers are shields |

### ⚠️ The community mapping is wrong

`buff_names_community.json` (NattKh/CrimsonDesertCommunityItemMapping) is
widely used and is **incorrect on at least seven of these ids** — every
entry in it is marked `verified: true`. If you build a mod against it you
will boost a different stat than you intended.

| id | game (`statusinfo`) | community says |
|---|---|---|
| `1000005` | DDV | ~~DPV Rate~~ |
| `1000006` | CriticalDamage | ~~Critical Rate~~ |
| `1000007` | **CriticalRate** | ~~Critical Damage~~ |
| `1000008` | AttackedDamageRate | ~~Attack Damage Rate~~ |
| `1000012` | ClimbSpeedRate | ~~Casting Speed Rate~~ |
| `1000017` | IceResistance | ~~HP Regen~~ |
| `1000026` | Stamina | ~~Air Attack Damage~~ |
| `1000019` | EquipMainWeapon | ~~Guard PV Rate~~ (that's `1000043`) |

`1000006` / `1000007` are straight **swapped**.

Three independent checks say the game's table is the right one:

1. **The raw bytes.** In `statusinfo.pabgb` the `u32` key sits physically
   adjacent to the name string: `key=1000007 strlen=12 name=CriticalRate`.
   No parser is involved, so a misaligned decode can't explain it.
2. **The game names items after the stat they grant.**
   `Item_Stat_AbyssGear_CriticalRate_LV1` carries stat `1000007`, and
   `statusinfo` calls `1000007` CriticalRate. Same for AttackSpeedRate
   (`1000010`) and MoveSpeedRate (`1000011`). Three agreements, zero
   disagreements.
3. **The community's ids aren't used by any gear.** They put "Guard PV
   Rate" at `1000019` and "Critical Rate" at `1000006` — **zero** vanilla
   gear items use either id. Meanwhile `1000043` (GuardPVRate, per the
   game) is on 83 items, 76 of them shields. A mapping that names ids
   nothing uses, and fails to name four ids that gear does use
   (`1000027`, `1000036`, `1000037`, `1000043`), is a guess.

Checks 2 and 3 run in CI (`tests/test_stat_names.py`).

**One caveat, stated honestly.** What is proven is the id → *internal stat
name* mapping. Whether BlackSpace's internal name faithfully describes the
in-game *effect* is a separate question that files cannot answer — it is
possible they named the enum `CriticalRate` while the effect is really crit
damage, and that the community verified the effect empirically. Only in-game
testing settles that. It does not change what you edit: the stat the game
calls `CriticalRate` is `1000007`.

Note that many named gear effects (`FireResistance`, `HpRegen`,
`GuardPVRate` on the `Item_Stat_*` carriers, …) grant **no** stat id at all
— they act through `equip_buffs` / `item_effect_info` instead of the stat
lists. Don't go looking for a stat id for those.

## The gotcha that made this look "done" when it wasn't

Reading and writing were fixed at different times, and in between, gear
stats were *readable and silently un-editable*: on CD 1.13 the writer
takes a "relocated layout" path which only did flat field resolution, so
every nested gear-stat intent was dropped as an "unwritable field". The
mod applied cleanly, reported success, and changed nothing.

Both writers now share one apply helper (`apply_nested_intent`) so they
can't drift apart again, and `test_gear_stat_intents_apply_on_the_1_13_writer`
fails loudly if the change ever comes back empty.
