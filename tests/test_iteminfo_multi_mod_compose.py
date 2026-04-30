"""Two iteminfo Format 3 mods both modifying different items must
compose. Each emits a whole-file change against vanilla. The apply
pipeline aggregates them in priority order; the second mod's change
must NOT silently mismatch when applied on top of the first.

Hypothesis: each mod independently produces a `{offset: 0,
original: vanilla, patched: mod_N}` change. Apply path applies the
first, then tries the second whose `original` no longer matches
the buffer (which is now mod_1's output). Second mod silently lost.
"""
from __future__ import annotations
from pathlib import Path

import pytest


_VANILLA_ITEMINFO = Path(r"C:\Users\faisa\AppData\Local\Temp\iteminfo.pabgb")
_VANILLA_PABGH = Path(r"C:\Users\faisa\AppData\Local\Temp\iteminfo.pabgh")


def _have() -> bool:
    return _VANILLA_ITEMINFO.exists() and _VANILLA_PABGH.exists()


@pytest.mark.skipif(not _have(), reason="vanilla extracts not present")
def test_two_iteminfo_mods_targeting_different_items_compose(tmp_path):
    """Mod A modifies item key X, Mod B modifies item key Y. Both
    are whole-table writers on iteminfo.pabgb. The apply pipeline
    must end up with BOTH X's change AND Y's change reflected.

    `expand_format3_into_aggregated` collects intents from ALL
    enabled mods per target before dispatching to the writer, so
    both mods' intents land in one parse+serialize pass. This test
    drives that through the real DB-backed entry point so the
    refactor is end-to-end verified.
    """
    import json
    from cdumm.engine.format3_apply import expand_format3_into_aggregated
    from cdumm.engine.crimson_rs_loader import get_crimson_rs
    from cdumm.engine.json_patch_handler import _apply_byte_patches
    from cdumm.storage.database import Database

    crimson_rs = get_crimson_rs()
    if crimson_rs is None:
        pytest.skip("crimson_rs not loadable")

    pabgb = _VANILLA_ITEMINFO.read_bytes()
    pabgh = _VANILLA_PABGH.read_bytes()
    items = crimson_rs.parse_iteminfo_from_bytes(pabgb)
    candidates = [it for it in items if it.get("enchant_data_list")]
    assert len(candidates) >= 2
    target_a = candidates[0]
    target_b = candidates[1]

    def _make_mod_json(target_item, buff_id: int, tmp: Path) -> Path:
        body = {
            "modinfo": {"title": f"mod_for_{target_item['key']}",
                        "version": "1.0"},
            "format": 3,
            "target": "iteminfo.pabgb",
            "intents": [{
                "entry": target_item.get("string_key", ""),
                "key": target_item["key"],
                "field": "enchant_data_list", "op": "set",
                "new": [{
                    "level": 0,
                    "enchant_stat_data": {
                        "max_stat_list": [], "regen_stat_list": [],
                        "stat_list_static": [],
                        "stat_list_static_level": [],
                    },
                    "buy_price_list": [],
                    "equip_buffs": [{"buff": buff_id, "level": 1}],
                }],
            }],
        }
        p = tmp / f"mod_{target_item['key']}.json"
        p.write_text(json.dumps(body), encoding="utf-8")
        return p

    mod_a_path = _make_mod_json(target_a, 999001, tmp_path)
    mod_b_path = _make_mod_json(target_b, 999002, tmp_path)

    db = Database(tmp_path / "test.db")
    db.initialize()
    db.connection.execute(
        "INSERT INTO mods (name, mod_type, json_source, enabled, "
        "priority) VALUES (?, ?, ?, 1, 100)",
        ("mod_A", "paz", str(mod_a_path)))
    db.connection.execute(
        "INSERT INTO mods (name, mod_type, json_source, enabled, "
        "priority) VALUES (?, ?, ?, 1, 200)",
        ("mod_B", "paz", str(mod_b_path)))
    db.connection.commit()

    def vanilla_extractor(target: str):
        if target == "iteminfo.pabgb":
            return (pabgb, pabgh)
        return None

    aggregated: dict = {}
    signatures: dict = {}
    warnings: list = []
    try:
        expand_format3_into_aggregated(
            aggregated, signatures, db, vanilla_extractor, warnings)
    finally:
        db.close()

    changes = aggregated.get("iteminfo.pabgb", [])
    assert len(changes) == 1, (
        f"expander must produce ONE merged whole-table change "
        f"for iteminfo, got {len(changes)}")

    modified = bytearray(pabgb)
    applied, mismatched, _ = _apply_byte_patches(
        modified, changes, signature=None, vanilla_data=pabgb)
    assert mismatched == 0
    assert applied == 1

    new_items = crimson_rs.parse_iteminfo_from_bytes(bytes(modified))
    new_by_key = {it["key"]: it for it in new_items}
    a_after = new_by_key[target_a["key"]]
    b_after = new_by_key[target_b["key"]]
    assert a_after["enchant_data_list"][0]["equip_buffs"][0]["buff"] == 999001, (
        f"Mod A's edit on item {target_a['key']} did NOT land")
    assert b_after["enchant_data_list"][0]["equip_buffs"][0]["buff"] == 999002, (
        f"Mod B's edit on item {target_b['key']} did NOT land")


@pytest.mark.skipif(not _have(), reason="vanilla extracts not present")
def test_lower_priority_number_wins_when_two_mods_edit_same_item(tmp_path):
    """CDUMM convention: lowest priority number wins. When mod A
    (priority=1, top) and mod B (priority=100) both set
    enchant_data_list on the same item, A's value must end up in
    the final bytes."""
    import json
    from cdumm.engine.format3_apply import expand_format3_into_aggregated
    from cdumm.engine.crimson_rs_loader import get_crimson_rs
    from cdumm.engine.json_patch_handler import _apply_byte_patches
    from cdumm.storage.database import Database

    crimson_rs = get_crimson_rs()
    if crimson_rs is None:
        pytest.skip("crimson_rs not loadable")

    pabgb = _VANILLA_ITEMINFO.read_bytes()
    pabgh = _VANILLA_PABGH.read_bytes()
    items = crimson_rs.parse_iteminfo_from_bytes(pabgb)
    target = next(it for it in items if it.get("enchant_data_list"))

    def _mk(buff_id: int, name: str) -> Path:
        body = {
            "modinfo": {"title": name, "version": "1.0"},
            "format": 3, "target": "iteminfo.pabgb",
            "intents": [{
                "entry": target.get("string_key", ""), "key": target["key"],
                "field": "enchant_data_list", "op": "set",
                "new": [{
                    "level": 0,
                    "enchant_stat_data": {
                        "max_stat_list": [], "regen_stat_list": [],
                        "stat_list_static": [],
                        "stat_list_static_level": [],
                    },
                    "buy_price_list": [],
                    "equip_buffs": [{"buff": buff_id, "level": 1}],
                }],
            }],
        }
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps(body), encoding="utf-8")
        return p

    a = _mk(111111, "winner")
    b = _mk(222222, "loser")

    db = Database(tmp_path / "prio.db")
    db.initialize()
    db.connection.execute(
        "INSERT INTO mods (name, mod_type, json_source, enabled, "
        "priority) VALUES (?, ?, ?, 1, 1)",
        ("winner", "paz", str(a)))
    db.connection.execute(
        "INSERT INTO mods (name, mod_type, json_source, enabled, "
        "priority) VALUES (?, ?, ?, 1, 100)",
        ("loser", "paz", str(b)))
    db.connection.commit()

    def vanilla_extractor(target_path: str):
        return (pabgb, pabgh) if target_path == "iteminfo.pabgb" else None

    aggregated: dict = {}
    try:
        expand_format3_into_aggregated(aggregated, {}, db, vanilla_extractor)
    finally:
        db.close()

    modified = bytearray(pabgb)
    _apply_byte_patches(modified, aggregated["iteminfo.pabgb"],
                        signature=None, vanilla_data=pabgb)
    new_items = crimson_rs.parse_iteminfo_from_bytes(bytes(modified))
    new_by_key = {it["key"]: it for it in new_items}
    final_buff = new_by_key[target["key"]]["enchant_data_list"][0]["equip_buffs"][0]["buff"]
    assert final_buff == 111111, (
        f"Lowest priority number should win. Expected winner's "
        f"buff=111111, got {final_buff}")
