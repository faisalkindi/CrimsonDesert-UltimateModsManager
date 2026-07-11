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

There are 15 distinct stat ids in vanilla 1.13. The game does not ship a
stat-name table — the ids are an enum inside the executable — so most of
them are listed here by id only. That is deliberate: an invented name is
worse than a number.

Three are **evidenced** by the game's own item names. Items called
`Item_Stat_<Set>_<Thing>_LVn` carry a common baseline pair plus exactly
one distinguishing stat, and the name says which:

| id | name | evidence |
|---|---|---|
| `1000007` | CriticalRate | `Item_Stat_AbyssGear_CriticalRate_LV1` |
| `1000010` | AttackSpeedRate | `Item_Stat_AbyssGear_AttackSpeedRate_LV1` |
| `1000011` | MoveSpeedRate | `Item_Stat_AbyssGear_MoveSpeedRate_LV1` |

The two most common ids, `1000002` and `1000003`, appear on nearly every
item (9189 and 9377 occurrences). They are almost certainly the primary
offence/defence pair, but nothing in the shipped data *proves* which is
which, so they are not named here.

Other ids seen in vanilla: `1000000`, `1000005`, `1000008`, `1000012`,
`1000017`, `1000026`, `1000027`, `1000036`, `1000037`, `1000043`.

Note that many named gear effects (`FireResistance`, `HpRegen`,
`GuardPVRate`, …) carry **no** extra stat id at all — they act through
`equip_buffs` / `item_effect_info` instead of the stat lists. Don't go
looking for a stat id for those.

## The gotcha that made this look "done" when it wasn't

Reading and writing were fixed at different times, and in between, gear
stats were *readable and silently un-editable*: on CD 1.13 the writer
takes a "relocated layout" path which only did flat field resolution, so
every nested gear-stat intent was dropped as an "unwritable field". The
mod applied cleanly, reported success, and changed nothing.

Both writers now share one apply helper (`apply_nested_intent`) so they
can't drift apart again, and `test_gear_stat_intents_apply_on_the_1_13_writer`
fails loudly if the change ever comes back empty.
