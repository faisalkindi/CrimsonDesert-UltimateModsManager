"""``_detect_plain_xml_replacements`` must pick up the XML-family
extensions Crimson Desert actually uses, not just plain ``.xml``.

Bug 2026-05-09 (Threelite on Nexus): "I want to make a mod by
editing xml files... .xml files work but .app_xml files don't
replace?"

Vanilla PAMT census (real game install) shows several XML-family
extensions in active use:

  .xml                7,390 files
  .app_xml            5,604 files
  .pac_xml           12,708 files
  .prefabdata_xml     2,597 files

The basename-replacement detector only globbed ``*.xml``, so a mod
author dropping a loose ``.app_xml`` at the top level of their mod
zip got ignored. The fix is to widen the glob to all XML-family
extensions the game ships, while keeping the OG_-prefix and
numbered-PAZ-dir filters intact so the detector still doesn't
double-process content that belongs to other detection paths.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _stage(tmp_path: Path, names: list[str]) -> Path:
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    for n in names:
        (extracted / n).write_bytes(b"<root/>")
    return extracted


def test_detector_picks_up_app_xml_at_top_level(tmp_path):
    from cdumm.engine.import_handler import _detect_plain_xml_replacements
    extracted = _stage(tmp_path, ["foo.app_xml"])
    results = _detect_plain_xml_replacements(extracted)
    assert any(r["target_name"] == "foo.app_xml" for r in results), (
        f"_detect_plain_xml_replacements should pick up loose "
        f".app_xml files (5604 such files exist in vanilla PAMTs); "
        f"got {results!r}"
    )


def test_detector_picks_up_pac_xml(tmp_path):
    from cdumm.engine.import_handler import _detect_plain_xml_replacements
    extracted = _stage(tmp_path, ["something.pac_xml"])
    results = _detect_plain_xml_replacements(extracted)
    assert any(r["target_name"] == "something.pac_xml" for r in results)


def test_detector_picks_up_prefabdata_xml(tmp_path):
    from cdumm.engine.import_handler import _detect_plain_xml_replacements
    extracted = _stage(tmp_path, ["xyz.prefabdata_xml"])
    results = _detect_plain_xml_replacements(extracted)
    assert any(r["target_name"] == "xyz.prefabdata_xml" for r in results)


def test_detector_still_picks_up_plain_xml(tmp_path):
    """Sanity: the existing .xml path must still work."""
    from cdumm.engine.import_handler import _detect_plain_xml_replacements
    extracted = _stage(tmp_path, ["hud.xml"])
    results = _detect_plain_xml_replacements(extracted)
    assert any(r["target_name"] == "hud.xml" for r in results)


def test_detector_still_skips_og_prefixed_xml(tmp_path):
    """Sanity: OG_ files are handled by `_detect_xml_replacements`,
    not this detector. They must NOT show up here."""
    from cdumm.engine.import_handler import _detect_plain_xml_replacements
    extracted = _stage(tmp_path, ["OG_legacy.xml"])
    results = _detect_plain_xml_replacements(extracted)
    assert not any(r["target_name"] == "OG_legacy.xml" for r in results)


def test_detector_still_skips_files_inside_numbered_paz_dirs(
        tmp_path):
    """Sanity: XML-family files inside NNNN/ dirs are PAZ mod
    content, not loose basename replacements."""
    from cdumm.engine.import_handler import _detect_plain_xml_replacements
    extracted = tmp_path / "extracted"
    paz_dir = extracted / "0008"
    paz_dir.mkdir(parents=True)
    (paz_dir / "inside.app_xml").write_bytes(b"<root/>")
    results = _detect_plain_xml_replacements(extracted)
    assert not any(
        r["target_name"] == "inside.app_xml" for r in results
    ), (
        "files inside NNNN/ paz dirs must be skipped by the "
        "loose-XML basename detector regardless of extension"
    )


def test_detector_does_not_pick_up_unrelated_extensions(tmp_path):
    """Sanity: ``foo.txt`` and ``foo.json`` must still be ignored."""
    from cdumm.engine.import_handler import _detect_plain_xml_replacements
    extracted = _stage(tmp_path, ["readme.txt", "manifest.json"])
    results = _detect_plain_xml_replacements(extracted)
    assert results == [], (
        f"non-XML-family files leaked into the detector: {results!r}"
    )
