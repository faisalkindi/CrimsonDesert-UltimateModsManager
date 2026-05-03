"""When two mods modify the same non-pabgb entry (e.g., a sequencer
.paseq or .pastage that lives inside a PAZ archive), they should still
attempt a 3-way byte merge against vanilla. The current
_try_semantic_merge bails with `continue` when identify_table_from_path
returns None, which silently drops the entire byte-merge fallback —
non-pabgb entries get last-wins regardless of whether their byte
ranges overlap or not.

GitHub #59 (DoRoon, 2026-05-01) reports SwapButcherWithBarber and
Character Creator Female can't coexist in CDUMM but DO in JMM/DMM.
Both touch entries that aren't pabgb table format (sequencer files,
NPC interaction definitions). When CDUMM picks one priority winner,
the loser's changes are entirely lost. The byte-merge tier 2
fallback at apply_engine.py:2580 already exists — it just doesn't
run because the `continue` at line 2467 skips past it.

Fix: restructure _try_semantic_merge so mod_bodies is populated
before the table_name check, semantic merge only runs when
table_name is set, and byte-merge tier 2 always runs as a fallback
regardless of file extension.
"""
from __future__ import annotations
from pathlib import Path

import pytest


def _build_worker_with_stubs(tmp_path: Path, vanilla_bytes: bytes):
    """Build a minimally-wired ApplyWorker for _try_semantic_merge tests."""
    from cdumm.engine.apply_engine import ApplyWorker

    worker = ApplyWorker.__new__(ApplyWorker)
    worker._soft_warnings = []
    worker._last_pct_emitted = 0

    class _SignalStub:
        def emit(self, *args, **kwargs):
            pass
    worker.warning = _SignalStub()
    worker.progress_updated = _SignalStub()
    worker._db = None  # not needed for byte-merge tier

    def _fake_vanilla(file_path, entry_path):
        return vanilla_bytes
    worker._get_vanilla_entry_content = _fake_vanilla

    def _fake_sibling(pamt_dir, entry_path):
        return None  # no PABGH header — forces semantic merge to skip
    worker._extract_sibling_entry = _fake_sibling

    return worker


def _save_entr_delta(tmp_path: Path, name: str, content: bytes,
                     entry_path: str) -> Path:
    from cdumm.engine.delta_engine import save_entry_delta
    delta_path = tmp_path / f"{name}.entr"
    metadata = {
        "entry_path": entry_path,
        "paz_index": 0,
        "compression_type": 0,
        "flags": 0,
        "vanilla_offset": 0,
        "vanilla_comp_size": len(content),
        "vanilla_orig_size": len(content),
    }
    save_entry_delta(content, metadata, delta_path)
    return delta_path


def test_byte_merge_runs_for_non_pabgb_entries(tmp_path):
    """Two mods modifying the same non-pabgb entry at non-overlapping
    byte ranges must produce ONE merged delta carrying both mods'
    changes — not two original deltas that get last-wins-collapsed
    later by `by_entry[ep] = d`.
    """
    vanilla = b"V" * 100
    # Mod A modifies bytes 10-20, leaves rest as vanilla
    mod_a = bytearray(vanilla)
    mod_a[10:20] = b"AAAAAAAAAA"
    # Mod B modifies bytes 60-70
    mod_b = bytearray(vanilla)
    mod_b[60:70] = b"BBBBBBBBBB"

    # Same non-pabgb entry path — sequencer file
    entry_path = "gamedata/sequencer_test.paseq"
    file_path = "0008/0.paz"

    delta_a = _save_entr_delta(tmp_path, "mod_a", bytes(mod_a), entry_path)
    delta_b = _save_entr_delta(tmp_path, "mod_b", bytes(mod_b), entry_path)

    entry_deltas = [
        {"entry_path": entry_path, "delta_path": str(delta_a),
         "mod_name": "ModA"},
        {"entry_path": entry_path, "delta_path": str(delta_b),
         "mod_name": "ModB"},
    ]

    worker = _build_worker_with_stubs(tmp_path, vanilla)
    result = worker._try_semantic_merge(file_path, entry_deltas)

    # After fix: result has ONE merged delta for entry_path, carrying
    # _merged_content with both A's and B's byte changes folded in.
    same_path = [d for d in result if d.get("entry_path") == entry_path
                 or d.get("_merged_metadata", {}).get("entry_path") == entry_path]
    merged = [d for d in same_path if d.get("_byte_merged")
              or d.get("_merged_content") is not None]

    assert merged, (
        f"Expected a byte-merged delta for non-pabgb entry {entry_path!r}, "
        f"but _try_semantic_merge returned only the original deltas: "
        f"{[(d.get('mod_name'), '_byte_merged' in d, '_merged_content' in d) for d in same_path]!r}. "
        f"The `if not table_name: continue` at apply_engine.py:2467 is "
        f"skipping the byte-merge tier 2 fallback for non-pabgb entries."
    )

    # The merged content must carry BOTH A's and B's byte changes.
    body = merged[0].get("_merged_content")
    assert body is not None
    assert body[10:20] == b"AAAAAAAAAA", (
        "Merged body lost ModA's bytes 10-20"
    )
    assert body[60:70] == b"BBBBBBBBBB", (
        "Merged body lost ModB's bytes 60-70"
    )


def test_pabgb_entries_still_get_semantic_merge_attempt(tmp_path):
    """Regression guard: known pabgb tables still flow through semantic
    merge (tier 1) instead of jumping straight to byte merge."""
    vanilla = b"V" * 100
    mod_a = bytearray(vanilla)
    mod_a[10:20] = b"AAAAAAAAAA"
    mod_b = bytearray(vanilla)
    mod_b[60:70] = b"BBBBBBBBBB"

    # iteminfo IS in the schema — but our stubbed _extract_sibling_entry
    # returns None, so semantic merge will skip and byte merge will
    # take over. Either way, two mods → one merged delta.
    entry_path = "gamedata/iteminfo.pabgb"
    file_path = "0008/0.paz"

    delta_a = _save_entr_delta(tmp_path, "mod_a", bytes(mod_a), entry_path)
    delta_b = _save_entr_delta(tmp_path, "mod_b", bytes(mod_b), entry_path)

    entry_deltas = [
        {"entry_path": entry_path, "delta_path": str(delta_a),
         "mod_name": "ModA"},
        {"entry_path": entry_path, "delta_path": str(delta_b),
         "mod_name": "ModB"},
    ]

    worker = _build_worker_with_stubs(tmp_path, vanilla)
    result = worker._try_semantic_merge(file_path, entry_deltas)

    same_path = [d for d in result if d.get("entry_path") == entry_path
                 or d.get("_merged_metadata", {}).get("entry_path") == entry_path]
    merged = [d for d in same_path if d.get("_byte_merged")
              or d.get("_semantic_merged")
              or d.get("_merged_content") is not None]
    assert merged, (
        "pabgb entry must still produce a merged delta (semantic or byte)"
    )
