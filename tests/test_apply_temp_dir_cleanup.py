"""Apply-time temp dir lifecycle (audit finding 4, 2026-06-11).

The ``cdumm_xlayer_*`` cross-layer stage roots and the ``cdumm_agg_*``
aggregated-JSON dir used to be plain ``mkdtemp`` dirs that nothing
deleted and whose prefixes were missing from
``temp_workspace.CDUMM_PREFIXES``, so even the startup sweep never
reclaimed them. They are now tracked, sweepable, and released at the
end of every apply run via ``_cleanup_apply_temp_dirs``.
"""
from __future__ import annotations

from pathlib import Path

from cdumm.engine.apply_engine import ApplyWorker
from cdumm.engine.temp_workspace import CDUMM_PREFIXES, make_temp_dir


def test_apply_temp_prefixes_are_sweepable():
    assert "cdumm_xlayer_" in CDUMM_PREFIXES
    assert "cdumm_agg_" in CDUMM_PREFIXES


def test_cleanup_apply_temp_dirs_releases_everything(tmp_path):
    worker = ApplyWorker(tmp_path / "game", tmp_path / "vanilla",
                         tmp_path / "t.db")

    stage_a = make_temp_dir("cdumm_xlayer_0036_")
    stage_b = make_temp_dir("cdumm_xlayer_0037_")
    synth = make_temp_dir("cdumm_agg_")
    (stage_a / "0.pamt").write_bytes(b"x")
    (synth / "aggregated.json").write_text("{}", encoding="utf-8")

    worker._paz_dir_overrides = {
        "a": {"stage_root": stage_a, "pamt_dir": "0036"},
        # Two overrides sharing one stage root must not double-release.
        "a2": {"stage_root": stage_a, "pamt_dir": "0036"},
        "b": {"stage_root": stage_b, "pamt_dir": "0037"},
    }
    worker._synth_temp = synth

    worker._cleanup_apply_temp_dirs()

    assert not stage_a.exists()
    assert not stage_b.exists()
    assert not synth.exists()
    # Override map dropped so a later resolver re-collects instead of
    # pointing at deleted paths.
    assert not hasattr(worker, "_paz_dir_overrides")
    assert worker._synth_temp is None

    # Idempotent.
    worker._cleanup_apply_temp_dirs()
