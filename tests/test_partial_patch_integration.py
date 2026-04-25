"""Integration: CSS / HTML partial patches register in mod_deltas
with the right kind, and the import-side scanner detects them
alongside XML patches."""
from __future__ import annotations

from pathlib import Path

import pytest

from cdumm.engine.import_handler import (
    _register_xml_patches, _scan_xml_patches,
)
from cdumm.storage.database import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.initialize()
    yield database
    database.close()


def _seed_mod(db: Database, name: str = "PartialMod") -> int:
    cur = db.connection.execute(
        "INSERT INTO mods (name, mod_type, priority) VALUES (?, 'paz', 1)",
        (name,))
    db.connection.commit()
    return cur.lastrowid


# ── Scanner sees all three handler families ─────────────────────────


def test_scanner_finds_css_html_xml_patches(tmp_path: Path):
    """_scan_xml_patches (kept name) must detect CSS / HTML / XML
    patches together so a mod that's purely partial-patch content
    isn't misclassified as empty."""
    root = tmp_path / "mod"
    root.mkdir()
    (root / "ui.css.patch").write_text(".x { color: red; }",
                                        encoding="utf-8")
    (root / "menu.html.patch").write_text(
        '<set at="#x" data-y="z" />', encoding="utf-8")
    (root / "data.xml.merge").write_text(
        '<xml-merge><foo>bar</foo></xml-merge>', encoding="utf-8")
    (root / "readme.txt").write_text("not a patch", encoding="utf-8")

    hits = _scan_xml_patches(root)
    names = sorted(p.name for p in hits)
    assert names == ["data.xml.merge", "menu.html.patch", "ui.css.patch"]


def test_scanner_ignores_unrelated_files(tmp_path: Path):
    root = tmp_path / "mod"
    root.mkdir()
    (root / "regular.css").write_text("body{}", encoding="utf-8")
    (root / "regular.html").write_text("<p/>", encoding="utf-8")
    (root / "regular.xml").write_text("<root/>", encoding="utf-8")
    assert _scan_xml_patches(root) == []


# ── Registration writes the right kind to mod_deltas ────────────────


def test_register_css_patch_writes_css_patch_kind(
        db: Database, tmp_path: Path):
    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()
    extracted = tmp_path / "mod"
    (extracted / "ui").mkdir(parents=True)
    (extracted / "ui" / "menu.css.patch").write_text(
        ".btn { color: red; }", encoding="utf-8")
    mod_id = _seed_mod(db, "CssMod")

    claimed = _register_xml_patches(extracted, mod_id, "CssMod", db, deltas_dir)
    assert len(claimed) == 1

    rows = db.connection.execute(
        "SELECT kind, file_path, delta_path FROM mod_deltas "
        "WHERE mod_id = ?", (mod_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "css_patch"
    assert rows[0][1] == "ui/menu.css"
    assert Path(rows[0][2]).exists()


def test_register_css_merge_writes_css_merge_kind(
        db: Database, tmp_path: Path):
    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()
    extracted = tmp_path / "mod"
    extracted.mkdir()
    (extracted / "menu.css.merge").write_text(
        ".btn { color: blue; }", encoding="utf-8")
    mod_id = _seed_mod(db, "CssMergeMod")

    _register_xml_patches(extracted, mod_id, "CssMergeMod", db, deltas_dir)
    rows = db.connection.execute(
        "SELECT kind, file_path FROM mod_deltas WHERE mod_id = ?",
        (mod_id,)).fetchall()
    assert rows[0][0] == "css_merge"
    assert rows[0][1] == "menu.css"


def test_register_html_patch_writes_html_patch_kind(
        db: Database, tmp_path: Path):
    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()
    extracted = tmp_path / "mod"
    extracted.mkdir()
    (extracted / "menu.html.patch").write_text(
        '<set at="#x" data-y="z" />', encoding="utf-8")
    mod_id = _seed_mod(db, "HtmlMod")

    _register_xml_patches(extracted, mod_id, "HtmlMod", db, deltas_dir)
    rows = db.connection.execute(
        "SELECT kind, file_path FROM mod_deltas WHERE mod_id = ?",
        (mod_id,)).fetchall()
    assert rows[0][0] == "html_patch"
    assert rows[0][1] == "menu.html"


def test_register_html_merge_writes_html_merge_kind(
        db: Database, tmp_path: Path):
    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()
    extracted = tmp_path / "mod"
    extracted.mkdir()
    (extracted / "menu.html.merge").write_text(
        '<set at="#x" data-y="z" />', encoding="utf-8")
    mod_id = _seed_mod(db, "HtmlMergeMod")

    _register_xml_patches(extracted, mod_id, "HtmlMergeMod", db, deltas_dir)
    rows = db.connection.execute(
        "SELECT kind, file_path FROM mod_deltas WHERE mod_id = ?",
        (mod_id,)).fetchall()
    assert rows[0][0] == "html_merge"
    assert rows[0][1] == "menu.html"


def test_xml_patch_still_works_after_extension(
        db: Database, tmp_path: Path):
    """The detector chain checks XML first; existing XML mods must
    still register as xml_patch / xml_merge, not get accidentally
    consumed by the new CSS/HTML detectors."""
    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()
    extracted = tmp_path / "mod"
    extracted.mkdir()
    (extracted / "data.xml.merge").write_text(
        '<xml-merge><foo>bar</foo></xml-merge>', encoding="utf-8")
    mod_id = _seed_mod(db, "XmlMod")

    _register_xml_patches(extracted, mod_id, "XmlMod", db, deltas_dir)
    rows = db.connection.execute(
        "SELECT kind, file_path FROM mod_deltas WHERE mod_id = ?",
        (mod_id,)).fetchall()
    assert rows[0][0] == "xml_merge"


def test_register_mixed_mod_with_all_three_kinds(
        db: Database, tmp_path: Path):
    """A single mod containing CSS + HTML + XML patches in the same
    folder must register one row per file, with the correct kind on
    each."""
    deltas_dir = tmp_path / "deltas"
    deltas_dir.mkdir()
    extracted = tmp_path / "mod"
    extracted.mkdir()
    (extracted / "a.css.patch").write_text(".x{}", encoding="utf-8")
    (extracted / "b.html.patch").write_text(
        '<remove at=".x" />', encoding="utf-8")
    (extracted / "c.xml.merge").write_text(
        '<xml-merge><z/></xml-merge>', encoding="utf-8")
    mod_id = _seed_mod(db, "MixedMod")

    claimed = _register_xml_patches(
        extracted, mod_id, "MixedMod", db, deltas_dir)
    assert len(claimed) == 3
    rows = db.connection.execute(
        "SELECT kind FROM mod_deltas WHERE mod_id = ? ORDER BY kind",
        (mod_id,)).fetchall()
    kinds = {r[0] for r in rows}
    assert kinds == {"css_patch", "html_patch", "xml_merge"}
