"""H2: a Format 3 mod that previously had skips and re-applies cleanly
must have its last_apply_skipped_count reset to 0.

Pre-fix: ``participating_mod_ids = {m['mod_id'] for m in mod_summary}``
in apply_engine only contains mods that came through the v2
aggregator. A Format 3 mod that contributed only via
``expand_format3_into_aggregated`` (no v2 patches block) is absent
from mod_summary, so persist_skip_summary never resets its row.
The yellow SKIPPED badge stays lit forever even after a clean apply.

Post-fix: ``expand_format3_into_aggregated`` reports the mod ids that
contributed any change (per-mod or whole-table), and the apply path
unions those into participating_mod_ids before persist_skip_summary
runs.
"""
from __future__ import annotations

from cdumm.engine.format3_apply import expand_format3_into_aggregated


class _FakeRow:
    def __init__(self, *args):
        self._args = args

    def __iter__(self):
        return iter(self._args)


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, _sql, *_args):
        return _FakeCursor(self._rows)


class _FakeDb:
    def __init__(self, rows):
        self.connection = _FakeConn(rows)


def test_expand_format3_reports_contributing_mod_ids(tmp_path):
    """Two Format 3 mods, both successfully contributing changes.
    The function must populate the passed-in ``participating_mod_ids``
    set with both mod ids."""
    # Build two minimal Format 3 mod files. Format detection is
    # 'mod_format' == 3 inside the JSON.
    import json

    mod1_path = tmp_path / "mod1.json"
    mod1_path.write_text(json.dumps({
        "mod_format": 3,
        "target": "stamina.pabgb",
        "intents": [
            {"tid": 1, "field": "max_value", "value": 200},
        ],
    }))

    mod2_path = tmp_path / "mod2.json"
    mod2_path.write_text(json.dumps({
        "mod_format": 3,
        "target": "stamina.pabgb",
        "intents": [
            {"tid": 2, "field": "max_value", "value": 300},
        ],
    }))

    # Stub the parse + validate to skip the heavy field_schema path:
    # we only need to verify that when a mod produces ANY change the
    # function adds its id to participating. So we monkeypatch the
    # internal calls that produce per-mod changes to short-circuit
    # to a single fake change keyed by mod_id.
    import cdumm.engine.format3_apply as f3

    original_parse_targets = f3.parse_format3_mod_targets
    original_validate = f3.validate_intents
    original_intents_to = f3._intents_to_v2_changes

    class _ValRes:
        def __init__(self, supported):
            self.supported = supported
            self.skipped = []

    def _stub_parse_targets(p):
        # Return list of (target, intents) keyed by which file we're parsing.
        if "mod1" in str(p):
            return [("stamina.pabgb", [{"tid": 1}])]
        return [("stamina.pabgb", [{"tid": 2}])]

    def _stub_validate(target, intents):
        return _ValRes(intents)

    def _stub_intents_to(target, body, header, intents):
        # Fabricate one v2 change so the per-mod branch runs.
        return [{"label": f"x{intents[0]['tid']}",
                 "offset": 0, "original": "00", "patched": "01"}]

    def _stub_extractor(_target):
        return b"\x00" * 32, b""

    f3.parse_format3_mod_targets = _stub_parse_targets
    f3.validate_intents = _stub_validate
    f3._intents_to_v2_changes = _stub_intents_to

    try:
        rows = [
            (101, "mod 1", str(mod1_path), 10),
            (202, "mod 2", str(mod2_path), 10),
        ]
        db = _FakeDb(rows)
        aggregated: dict[str, list[dict]] = {}
        signatures: dict[str, str] = {}
        participating: set[int] = set()

        expand_format3_into_aggregated(
            aggregated, signatures, db,
            vanilla_extractor=_stub_extractor,
            participating_mod_ids=participating,
        )

        assert 101 in participating, (
            f"mod 101 contributed a change to stamina.pabgb but is "
            f"missing from participating_mod_ids={participating!r}. "
            f"Without this, persist_skip_summary skips it on the "
            f"next clean apply and the SKIPPED badge stays yellow.")
        assert 202 in participating, (
            f"mod 202 contributed a change but is missing from "
            f"participating_mod_ids={participating!r}.")
    finally:
        f3.parse_format3_mod_targets = original_parse_targets
        f3.validate_intents = original_validate
        f3._intents_to_v2_changes = original_intents_to


def test_participating_set_is_optional():
    """Existing call sites that don't pass participating_mod_ids must
    keep working , the parameter is additive."""
    # Empty rows = no work, no crash.
    db = _FakeDb([])
    aggregated: dict[str, list[dict]] = {}
    signatures: dict[str, str] = {}
    expand_format3_into_aggregated(
        aggregated, signatures, db,
        vanilla_extractor=lambda t: None,
    )  # must not raise
