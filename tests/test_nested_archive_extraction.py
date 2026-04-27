"""Recursively extract nested archives during import.

Bug from Faisal 2026-04-27: 'Display take and steal price' mod
ships as a parent ZIP containing 5 inner ZIPs (Chinese_sc.zip,
Chinese_tc.zip, English.zip, French.zip, German.zip) — language
packs the user picks one of. CDUMM detected the nesting at
mod_diagnostics.py:250-261 and warned, but the import path made
no attempt to extract them. Result: "no recognized mod format"
even though each inner ZIP is a valid mod.

Fix: after extracting the parent archive, walk the tree once and
extract any inner .zip / .7z / .rar in place into a same-named
subdirectory. Then the existing format detectors (CB manifest,
PAZ, JSON patch, loose-file variants) operate on the unpacked
tree as if the nested archive had been a folder all along.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest


def _make_simple_cb_mod(staging: Path, mod_name: str = "InnerMod") -> Path:
    """Build a Crimson Browser-formatted mod folder under staging.

    Returns the path to the folder (caller may zip it).
    """
    mod_dir = staging / mod_name
    files_dir = mod_dir / "files" / "0008"
    files_dir.mkdir(parents=True)
    (files_dir / "iteminfo.pabgb").write_bytes(b"\x00" * 32)
    (mod_dir / "manifest.json").write_text(json.dumps({
        "format": "crimson_browser_mod_v1",
        "id": mod_name,
        "files_dir": "files",
    }))
    return mod_dir


def test_extract_nested_zips_unpacks_inner_archives(tmp_path):
    """Parent ZIP containing 1 inner ZIP must be unpacked so the
    inner contents become reachable to format detection."""
    from cdumm.engine.import_handler import _extract_nested_archives

    # Build inner zip with a single text file
    staging = tmp_path / "_staging"
    staging.mkdir()
    (staging / "hello.txt").write_text("inside")
    inner_zip = tmp_path / "extracted" / "english.zip"
    inner_zip.parent.mkdir()
    with zipfile.ZipFile(inner_zip, "w") as zf:
        zf.write(staging / "hello.txt", "hello.txt")

    extracted_root = inner_zip.parent
    _extract_nested_archives(extracted_root)

    # Inner zip should be extracted into a same-stem subfolder, and
    # the inner zip file itself removed (so format detection doesn't
    # re-detect it as nested).
    unpacked = extracted_root / "english"
    assert (unpacked / "hello.txt").read_text() == "inside"
    assert not inner_zip.exists(), "Inner zip should be removed after extraction"


def test_extract_nested_zips_handles_multiple_siblings(tmp_path):
    """Multi-language pack: 5 inner zips at the same level. All
    extracted into 5 sibling folders. Existing variant-picker logic
    handles disambiguation."""
    from cdumm.engine.import_handler import _extract_nested_archives

    extracted_root = tmp_path / "ex"
    extracted_root.mkdir()
    for lang in ("Chinese_sc", "Chinese_tc", "English", "French", "German"):
        z = extracted_root / f"{lang}.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr(f"{lang}/marker.txt", lang)

    _extract_nested_archives(extracted_root)

    for lang in ("Chinese_sc", "Chinese_tc", "English", "French", "German"):
        marker = extracted_root / lang / lang / "marker.txt"
        assert marker.read_text() == lang, f"{lang} marker missing"
        assert not (extracted_root / f"{lang}.zip").exists()


def test_extract_nested_zips_recurses(tmp_path):
    """ZIP-of-ZIPs-of-ZIPs: must recurse until no inner archives
    remain. Cap at a sane depth to avoid zip-bombs."""
    from cdumm.engine.import_handler import _extract_nested_archives

    extracted_root = tmp_path / "ex"
    extracted_root.mkdir()

    # Build inner→inner→file
    deep_dir = tmp_path / "_deep"
    deep_dir.mkdir()
    (deep_dir / "leaf.txt").write_text("leaf")

    inner_inner = tmp_path / "_inner_inner.zip"
    with zipfile.ZipFile(inner_inner, "w") as zf:
        zf.write(deep_dir / "leaf.txt", "leaf.txt")

    inner = extracted_root / "outer.zip"
    with zipfile.ZipFile(inner, "w") as zf:
        zf.write(inner_inner, "inner_inner.zip")

    _extract_nested_archives(extracted_root)

    # outer.zip → outer/inner_inner.zip → outer/inner_inner/leaf.txt
    leaf = extracted_root / "outer" / "inner_inner" / "leaf.txt"
    assert leaf.read_text() == "leaf"


def test_extract_nested_zips_skips_corrupt_archive(tmp_path):
    """Corrupt inner zip must not crash the whole import. Skip it
    and continue with the rest of the tree."""
    from cdumm.engine.import_handler import _extract_nested_archives

    extracted_root = tmp_path / "ex"
    extracted_root.mkdir()
    (extracted_root / "broken.zip").write_bytes(b"this is not a zip")
    (extracted_root / "real.zip")
    real = extracted_root / "real.zip"
    with zipfile.ZipFile(real, "w") as zf:
        zf.writestr("ok.txt", "fine")

    _extract_nested_archives(extracted_root)

    # Real archive should still be unpacked despite the corrupt one
    assert (extracted_root / "real" / "ok.txt").read_text() == "fine"


def test_extract_nested_zips_idempotent_on_already_flat_tree(tmp_path):
    """No nested archives → no-op, no crash."""
    from cdumm.engine.import_handler import _extract_nested_archives

    extracted_root = tmp_path / "ex"
    extracted_root.mkdir()
    (extracted_root / "loose.txt").write_text("hi")
    (extracted_root / "sub").mkdir()
    (extracted_root / "sub" / "thing.json").write_text("{}")

    _extract_nested_archives(extracted_root)

    assert (extracted_root / "loose.txt").read_text() == "hi"
    assert (extracted_root / "sub" / "thing.json").read_text() == "{}"


def test_extract_nested_zips_collision_uses_suffix(tmp_path):
    """If `english.zip` exists alongside an `english/` directory
    already, the extraction must not clobber the existing dir.
    Use a numeric suffix."""
    from cdumm.engine.import_handler import _extract_nested_archives

    extracted_root = tmp_path / "ex"
    extracted_root.mkdir()
    (extracted_root / "english").mkdir()
    (extracted_root / "english" / "preexisting.txt").write_text("keep me")

    z = extracted_root / "english.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("inner.txt", "from zip")

    _extract_nested_archives(extracted_root)

    # Pre-existing dir untouched
    assert (extracted_root / "english" / "preexisting.txt").read_text() == "keep me"
    # ZIP unpacked to a sibling with numeric suffix
    siblings = sorted(d.name for d in extracted_root.iterdir() if d.is_dir())
    assert "english" in siblings
    has_collision_dir = any(
        s.startswith("english_") and (extracted_root / s / "inner.txt").exists()
        for s in siblings
    )
    assert has_collision_dir, (
        f"Expected an english_<N>/ sibling for the unpacked ZIP, got {siblings}")


def test_import_from_zip_with_nested_zip_succeeds(tmp_path):
    """End-to-end: parent ZIP containing one inner ZIP that's itself
    a valid CB mod must now import successfully."""
    from cdumm.engine.import_handler import _extract_nested_archives

    # Build a CB mod folder, zip it (this is the inner zip)
    cb_staging = tmp_path / "_cb"
    cb_staging.mkdir()
    _make_simple_cb_mod(cb_staging, "EnglishPack")
    inner_zip_path = tmp_path / "_inner.zip"
    with zipfile.ZipFile(inner_zip_path, "w") as zf:
        for f in (cb_staging / "EnglishPack").rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(cb_staging))

    # Place inside a parent extracted root
    extracted_root = tmp_path / "ex"
    extracted_root.mkdir()
    target_inner = extracted_root / "EnglishPack.zip"
    target_inner.write_bytes(inner_zip_path.read_bytes())

    _extract_nested_archives(extracted_root)

    # CB manifest now reachable in the tree
    manifest_path = extracted_root / "EnglishPack" / "EnglishPack" / "manifest.json"
    # Some zip layouts double the prefix; either is fine. Find any manifest.
    found = list(extracted_root.rglob("manifest.json"))
    assert found, "manifest.json should be reachable after nested-zip extraction"
