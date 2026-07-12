"""`.cdmod` (crimson-mod-package v1) -> Format 3 (#288).

Built from a synthetic package, not from the real Nexus mods: those are
other authors' work and don't belong in the repo. The synthetic one mirrors
the real `No Fall Damage-4.4.cdmod` byte-for-byte in shape (manifest +
patches/semantic.json, a buffinfo variant path and an iteminfo nested path).

The thing these tests exist to prevent is the failure mode that has bitten
this project four times now (#259, #275, #278, #285): the mod imports
clean, reports N intents "ready to apply", and then changes nothing. So the
bar here is that a translated .cdmod is accepted by CDUMM's OWN Format 3
parser -- not merely that our translator produced a dict that looks right.
"""
from __future__ import annotations

import json
import zipfile

import pytest

from cdumm.engine.cdmod_handler import (
    CdmodError, cdmod_to_format3, is_cdmod,
)
from cdumm.engine.format3_handler import parse_format3_mod_targets
from cdumm.engine.import_handler import detect_format


def _pkg(tmp_path, manifest, files=None, name="mod.cdmod"):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        for fn, doc in (files or {}).items():
            z.writestr(fn, json.dumps(doc))
    return p


SEMANTIC = {
    "schema": 1,
    "targets": [
        {
            "file": "buffinfo.pabgb",
            "operations": [{
                "op": "set",
                "path": "buff_data_list[9].data.variant.body.f01",
                "selector": {"key": 1000185,
                             "string_key": "BuffLevel_Food_FallDamageReduce"},
                "value": 100000,
                "conversion": "conservative",
            }],
        },
        {
            "file": "iteminfo.pabgb",
            "operations": [{
                "op": "set",
                "path": "max_stack_count",
                "selector": {"key": 2200},
                "value": 999999,
            }],
        },
    ],
}

MANIFEST = {
    "format": "crimson-mod-package",
    "format_version": 1,
    "name": "No Fall Damage - Field JSON",
    "author": "Sportiax",
    "version": "4.4",
    "description": "Fall Damage Reduction",
    "components": [{"type": "semantic-patch",
                    "path": "patches/semantic.json",
                    "operation_count": 2, "target_count": 2}],
    "source": {"format": "format3"},
}


@pytest.fixture
def pkg(tmp_path):
    return _pkg(tmp_path, MANIFEST, {"patches/semantic.json": SEMANTIC})


# ── detection ───────────────────────────────────────────────────────────

def test_detect_format_gives_cdmod_its_own_branch(pkg):
    """NOT 'zip'. A plain-zip import would extract it, not recognise
    semantic.json as Format 3, and import a mod that does nothing."""
    assert detect_format(pkg) == "cdmod"
    assert is_cdmod(pkg)


def test_a_zip_that_is_not_a_cdmod_is_not_claimed(tmp_path):
    z = tmp_path / "plain.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("a.txt", "hi")
    assert detect_format(z) == "zip"
    assert not is_cdmod(z)


# ── translation ─────────────────────────────────────────────────────────

def test_operations_become_intents(pkg):
    doc = cdmod_to_format3(pkg)
    assert doc["format"] == 3
    assert {t["file"] for t in doc["targets"]} == {
        "buffinfo.pabgb", "iteminfo.pabgb"}

    buff = next(t for t in doc["targets"] if t["file"] == "buffinfo.pabgb")
    i = buff["intents"][0]
    assert i["field"] == "buff_data_list[9].data.variant.body.f01"  # path
    assert i["key"] == 1000185                                      # selector
    assert i["entry"] == "BuffLevel_Food_FallDamageReduce"          # string_key
    assert i["new"] == 100000                                       # value
    assert i["op"] == "set"


def test_cdumms_own_format3_parser_accepts_the_output(pkg, tmp_path):
    """The check that matters. Our translator agreeing with itself proves
    nothing; the Format 3 pipeline accepting the result is the point."""
    out = tmp_path / "translated.field.json"
    out.write_text(json.dumps(cdmod_to_format3(pkg)), encoding="utf-8")

    pairs = parse_format3_mod_targets(out)
    assert len(pairs) == 2
    assert sum(len(intents) for _t, intents in pairs) == 2


def test_modinfo_is_carried_over(pkg):
    mi = cdmod_to_format3(pkg)["modinfo"]
    assert mi["title"] == "No Fall Damage - Field JSON"
    assert mi["author"] == "Sportiax"
    assert mi["version"] == "4.4"


# ── refuse, don't half-apply ────────────────────────────────────────────

def test_a_future_format_version_is_refused(tmp_path):
    """v2 could reshape operations. Translating it blind would silently
    drop or mis-map fields -- the exact bug class this guards."""
    m = dict(MANIFEST, format_version=2)
    p = _pkg(tmp_path, m, {"patches/semantic.json": SEMANTIC})
    with pytest.raises(CdmodError, match="format_version"):
        cdmod_to_format3(p)


def test_a_package_with_no_semantic_patch_is_refused(tmp_path):
    """The real 'Display take and steal price-1.13.01.cdmod' is exactly
    this: its only component is a localization-patch. Refuse loudly rather
    than import a mod that changes nothing."""
    m = dict(MANIFEST, components=[{"type": "localization-patch",
                                    "path": "patches/loc.json"}])
    p = _pkg(tmp_path, m, {"patches/loc.json": {"x": 1}})
    with pytest.raises(CdmodError, match="localization-patch"):
        cdmod_to_format3(p)


def test_an_operation_without_a_key_is_refused(tmp_path):
    """No selector.key means no record to address. Do not guess one."""
    sem = json.loads(json.dumps(SEMANTIC))
    del sem["targets"][1]["operations"][0]["selector"]["key"]
    p = _pkg(tmp_path, MANIFEST, {"patches/semantic.json": sem})
    with pytest.raises(CdmodError, match="selector.key"):
        cdmod_to_format3(p)


def test_a_bogus_package_format_is_refused(tmp_path):
    m = dict(MANIFEST, format="something-else")
    p = _pkg(tmp_path, m, {"patches/semantic.json": SEMANTIC})
    with pytest.raises(CdmodError, match="unknown package format"):
        cdmod_to_format3(p)


# ── routing ─────────────────────────────────────────────────────────────
#
# The translator being correct is worth nothing if nothing calls it. I
# shipped exactly that: `.cdmod` was wired into detect_format(), which
# LOOKS like the gate, and the real dispatch is a table in
# worker_process.py keyed on detect_format()'s return value. Without an
# entry there, a dropped .cdmod fell straight through to "unsupported file
# format" and the translation code never ran.
#
# So pin the wiring, not just the function.

def test_the_worker_dispatch_table_has_a_cdmod_entry():
    import inspect

    from cdumm import worker_process

    src = inspect.getsource(worker_process)
    assert '"cdmod":' in src, (
        "detect_format() returns 'cdmod' but worker_process has no handler "
        "for it, so a dropped .cdmod dies at 'unsupported file format' and "
        "the translator is never called")
    assert "import_from_cdmod" in src


def test_the_importer_exists_and_takes_the_dispatch_signature():
    import inspect

    from cdumm.engine.import_handler import import_from_cdmod

    params = list(inspect.signature(import_from_cdmod).parameters)
    assert params[:5] == [
        "cdmod_path", "game_dir", "db", "snapshot", "deltas_dir"]
    assert "existing_mod_id" in params


def test_an_untranslatable_cdmod_is_refused_at_import_not_imported_empty(
        tmp_path):
    """An empty mod would install clean and change nothing -- the exact
    silent-no-op this whole area keeps producing. Refuse instead."""
    from cdumm.engine.import_handler import import_from_cdmod

    m = dict(MANIFEST, components=[{"type": "localization-patch",
                                    "path": "patches/loc.json"}])
    p = _pkg(tmp_path, m, {"patches/loc.json": {"x": 1}})

    res = import_from_cdmod(p, tmp_path, None, None, tmp_path)
    assert res.error, "an untranslatable .cdmod must surface an error"
    assert "localization-patch" in res.error
    assert not res.changed_files
