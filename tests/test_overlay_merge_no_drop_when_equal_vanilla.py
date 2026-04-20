"""#145: overlay merge must not drop real patches when merge result
equals vanilla, and must still combine non-overlapping byte edits from
multiple JSON mods targeting the same .pabgb.

Regression reporters: butanokaabii, estereba, SirFapZalot. Scenario:
Fat Stack 9999 and ExtraSockets V2.2.0 both patch gamedata/iteminfo.pabgb
at different byte offsets. In v3.1.1 only Fat Stack takes effect in-game;
ExtraSockets is silently dropped.

Guardrails this test enforces:
  1. Two JSON overlay entries on the same .pabgb with non-overlapping edits
     must produce a merged result containing BOTH mods' bytes.
  2. When both mods' bodies happen to equal vanilla (no effective patch
     either side), the fallback must still produce vanilla bytes — never
     amplify or corrupt.
  3. When one body equals vanilla and the other has real edits, the
     merge must preserve the real edits.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from cdumm.engine.apply_engine import ApplyWorker


def _make_worker(vanilla_bytes: bytes):
    w = ApplyWorker.__new__(ApplyWorker)
    w._db = MagicMock()
    w._game_dir = MagicMock()
    w._vanilla_dir = MagicMock()
    # Stub out vanilla resolver to return a known buffer so merge
    # proceeds without touching disk.
    w._get_vanilla_entry_content = MagicMock(return_value=vanilla_bytes)
    return w


def test_two_non_overlapping_pabgb_mods_keep_both_edits():
    """Fat Stack + ExtraSockets scenario: both must survive the merge."""
    vanilla = b"\x00" * 64
    # Fat Stack edits bytes 0x04-0x08.
    fat_stack = bytearray(vanilla)
    fat_stack[0x04:0x08] = b"\xAA\xAA\xAA\xAA"
    # ExtraSockets edits bytes 0x20-0x24 (no overlap with Fat Stack).
    extra_sockets = bytearray(vanilla)
    extra_sockets[0x20:0x24] = b"\xBB\xBB\xBB\xBB"

    meta = {
        "pamt_dir": "0008",
        "entry_path": "gamedata/iteminfo.pabgb",
    }
    entries = [
        (bytes(fat_stack), {**meta, "priority": 1, "mod_name": "Fat Stack"}),
        (bytes(extra_sockets),
         {**meta, "priority": 2, "mod_name": "ExtraSockets"}),
    ]

    w = _make_worker(vanilla)
    result = w._merge_same_target_overlay_entries(entries)

    assert len(result) == 1, (
        f"expected one merged overlay entry, got {len(result)}")
    merged_body, _ = result[0]

    # BOTH mods' bytes must be present — this is the guardrail that
    # fails under the current last-wins fallback.
    assert merged_body[0x04:0x08] == b"\xAA\xAA\xAA\xAA", (
        "Fat Stack bytes missing from merged overlay")
    assert merged_body[0x20:0x24] == b"\xBB\xBB\xBB\xBB", (
        "ExtraSockets bytes missing from merged overlay")


def test_both_bodies_vanilla_equal_emits_vanilla_not_corruption():
    """Degenerate case: both mods' bodies == vanilla. Result must be
    vanilla-equivalent, never a spurious merge artifact."""
    vanilla = b"\x11" * 32

    meta = {
        "pamt_dir": "0008",
        "entry_path": "gamedata/iteminfo.pabgb",
    }
    entries = [
        (vanilla, {**meta, "priority": 1, "mod_name": "A"}),
        (vanilla, {**meta, "priority": 2, "mod_name": "B"}),
    ]

    w = _make_worker(vanilla)
    result = w._merge_same_target_overlay_entries(entries)

    assert len(result) == 1
    merged_body, _ = result[0]
    assert merged_body == vanilla, (
        f"vanilla-equal inputs must emit vanilla bytes, got {merged_body!r}")


def test_one_vanilla_one_real_patch_preserves_real_patch():
    """If one mod's body matches vanilla (patches failed silently) and
    the other has real edits, the merge must keep the real edits."""
    vanilla = b"\x00" * 32
    # Mod A patches bytes 0x10-0x14.
    real_patch = bytearray(vanilla)
    real_patch[0x10:0x14] = b"\xCC\xDD\xEE\xFF"
    # Mod B's body is indistinguishable from vanilla.
    empty_patch = bytes(vanilla)

    meta = {
        "pamt_dir": "0008",
        "entry_path": "gamedata/iteminfo.pabgb",
    }
    entries = [
        (bytes(real_patch),
         {**meta, "priority": 1, "mod_name": "RealPatch"}),
        (empty_patch, {**meta, "priority": 2, "mod_name": "EmptyPatch"}),
    ]

    w = _make_worker(vanilla)
    result = w._merge_same_target_overlay_entries(entries)

    assert len(result) == 1
    merged_body, _ = result[0]
    assert merged_body[0x10:0x14] == b"\xCC\xDD\xEE\xFF", (
        "real-patch bytes must survive even when paired with a vanilla-"
        "equal partner; current last-wins fallback would drop them when "
        "the real-patch mod is first in feed order")
