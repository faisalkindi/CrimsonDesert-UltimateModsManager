"""Test CDUMM's CSS patch handler — JMM v9.9.3 parity.

Mods authored for JMM's *.css.patch and *.css.merge formats must
import and apply byte-equivalently in CDUMM.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cdumm.engine.css_patch_handler import (
    apply_merge,
    apply_patches,
    derive_target_from_patch_path,
    detect_patch_file,
    parse_patch_file,
)


# ── Detection ───────────────────────────────────────────────────────


def test_detects_css_patch_extension():
    assert detect_patch_file(Path("foo/bar.css.patch")) == "css_patch"


def test_detects_css_merge_extension():
    assert detect_patch_file(Path("foo/bar.css.merge")) == "css_merge"


def test_rejects_non_css_extensions():
    assert detect_patch_file(Path("foo.css")) is None
    assert detect_patch_file(Path("foo.xml.patch")) is None
    assert detect_patch_file(Path("foo.html.patch")) is None
    assert detect_patch_file(Path("foo.txt")) is None


def test_derives_target_from_patch_path(tmp_path: Path):
    mod_root = tmp_path / "mod"
    (mod_root / "ui").mkdir(parents=True)
    patch = mod_root / "ui" / "menu.css.patch"
    patch.write_text("", encoding="utf-8")
    assert derive_target_from_patch_path(patch, mod_root) == "ui/menu.css"


def test_derives_target_from_merge_path(tmp_path: Path):
    mod_root = tmp_path / "mod"
    (mod_root / "ui").mkdir(parents=True)
    merge = mod_root / "ui" / "menu.css.merge"
    merge.write_text("", encoding="utf-8")
    assert derive_target_from_patch_path(merge, mod_root) == "ui/menu.css"


# ── Parsing ─────────────────────────────────────────────────────────


def test_parse_patch_file_default_op_is_merge():
    content = """
    .button { color: red; }
    """
    ops = parse_patch_file(content, "TestMod")
    assert len(ops) == 1
    assert ops[0].op == "merge"
    assert ops[0].selector == ".button"


def test_parse_patch_file_directive_switches_op():
    content = """
    /* @replace */
    .a { display: none; }
    /* @add */
    .b { color: blue; }
    """
    ops = parse_patch_file(content, "M")
    assert [(o.op, o.selector) for o in ops] == [
        ("replace", ".a"), ("add", ".b")]


def test_parse_patch_file_inline_remove():
    content = '/* @remove ".obsolete" */'
    ops = parse_patch_file(content, "M")
    assert len(ops) == 1
    assert ops[0].op == "remove"
    assert ops[0].selector == ".obsolete"


def test_parse_patch_file_op_resets_after_each_rule():
    """JMM behaviour: directive applies to ONE rule, then resets to
    merge. Verified against JMM v9.9.3 CssPatchApplier.cs."""
    content = """
    /* @replace */
    .a { display: none; }
    .b { color: red; }
    """
    ops = parse_patch_file(content, "M")
    assert ops[0].op == "replace"
    assert ops[1].op == "merge"


# ── Apply: merge ────────────────────────────────────────────────────


def test_merge_overrides_existing_property(tmp_path: Path):
    css = b".button { color: red; padding: 10px; }"
    patch = tmp_path / "mod.css.patch"
    patch.write_text(".button { color: blue; }", encoding="utf-8")
    out, log = apply_patches(css, [("Mod", patch)])
    out_str = out.decode("utf-8")
    assert "color: blue;" in out_str
    assert "color: red" not in out_str
    assert "padding: 10px" in out_str


def test_merge_adds_new_property_alongside_existing(tmp_path: Path):
    css = b".button { color: red; }"
    patch = tmp_path / "mod.css.patch"
    patch.write_text(".button { font-size: 14px; }", encoding="utf-8")
    out, _log = apply_patches(css, [("Mod", patch)])
    out_str = out.decode("utf-8")
    assert "color: red" in out_str
    assert "font-size: 14px" in out_str


def test_merge_appends_rule_when_selector_missing(tmp_path: Path):
    css = b".existing { color: red; }"
    patch = tmp_path / "mod.css.patch"
    patch.write_text(".new { display: block; }", encoding="utf-8")
    out, _log = apply_patches(css, [("Mod", patch)])
    out_str = out.decode("utf-8")
    assert ".existing" in out_str
    assert ".new" in out_str
    assert "display: block" in out_str


# ── Apply: replace ──────────────────────────────────────────────────


def test_replace_swaps_entire_body(tmp_path: Path):
    css = b".banner { color: red; padding: 10px; }"
    patch = tmp_path / "mod.css.patch"
    patch.write_text(
        "/* @replace */\n.banner { display: none; }",
        encoding="utf-8")
    out, _log = apply_patches(css, [("Mod", patch)])
    out_str = out.decode("utf-8")
    assert "display: none" in out_str
    assert "color: red" not in out_str
    assert "padding: 10px" not in out_str


# ── Apply: add ──────────────────────────────────────────────────────


def test_add_skips_when_selector_already_exists(tmp_path: Path):
    css = b".existing { color: red; }"
    patch = tmp_path / "mod.css.patch"
    patch.write_text(
        "/* @add */\n.existing { display: none; }", encoding="utf-8")
    out, log = apply_patches(css, [("Mod", patch)])
    out_str = out.decode("utf-8")
    # rule body unchanged
    assert "color: red" in out_str
    assert "display: none" not in out_str
    assert any("already exists" in line for line in log)


def test_add_appends_when_selector_is_new(tmp_path: Path):
    css = b".existing { color: red; }"
    patch = tmp_path / "mod.css.patch"
    patch.write_text(
        "/* @add */\n.new { display: none; }", encoding="utf-8")
    out, _log = apply_patches(css, [("Mod", patch)])
    out_str = out.decode("utf-8")
    assert ".existing" in out_str
    assert ".new" in out_str
    assert "display: none" in out_str


# ── Apply: remove ───────────────────────────────────────────────────


def test_remove_deletes_rule(tmp_path: Path):
    css = b".keep { color: red; }\n.gone { color: blue; }\n"
    patch = tmp_path / "mod.css.patch"
    patch.write_text('/* @remove ".gone" */', encoding="utf-8")
    out, _log = apply_patches(css, [("Mod", patch)])
    out_str = out.decode("utf-8")
    assert ".keep" in out_str
    assert ".gone" not in out_str


def test_remove_silent_skip_when_selector_missing(tmp_path: Path):
    css = b".keep { color: red; }"
    patch = tmp_path / "mod.css.patch"
    patch.write_text('/* @remove ".never-existed" */', encoding="utf-8")
    out, _log = apply_patches(css, [("Mod", patch)])
    assert out.decode("utf-8") == css.decode("utf-8")


# ── Selector matching ──────────────────────────────────────────────


def test_selector_whitespace_normalised():
    """JMM normalises whitespace runs to single spaces. Both
    '  .a  .b  ' and '.a .b' must match the same rule."""
    content = "  .button   .icon   { color: red; }"
    ops = parse_patch_file(content, "M")
    assert ops[0].selector == ".button .icon"


def test_selector_match_is_case_sensitive(tmp_path: Path):
    css = b".Button { color: red; }"
    patch = tmp_path / "mod.css.patch"
    patch.write_text(".button { color: blue; }", encoding="utf-8")
    out, _log = apply_patches(css, [("Mod", patch)])
    out_str = out.decode("utf-8")
    # case mismatch: .button NOT found, gets appended as new rule
    assert ".Button" in out_str
    assert ".button" in out_str
    # original .Button untouched
    assert "color: red" in out_str


# ── Brace tracking ──────────────────────────────────────────────────


def test_handles_nested_braces_in_strings(tmp_path: Path):
    """A '}' inside a quoted string must not close the rule prematurely."""
    css = b'.a { content: "}{"; color: red; }'
    patch = tmp_path / "mod.css.patch"
    patch.write_text(".a { color: blue; }", encoding="utf-8")
    out, _log = apply_patches(css, [("Mod", patch)])
    out_str = out.decode("utf-8")
    assert "color: blue" in out_str
    assert 'content: "}{"' in out_str


# ── Merge file format ──────────────────────────────────────────────


def test_merge_file_treats_every_rule_as_merge(tmp_path: Path):
    css = b".a { color: red; }\n.b { padding: 5px; }"
    merge = tmp_path / "mod.css.merge"
    merge.write_text(
        ".a { color: blue; }\n.b { margin: 10px; }",
        encoding="utf-8")
    out, _log = apply_merge(css, [("Mod", merge)])
    out_str = out.decode("utf-8")
    assert "color: blue" in out_str
    assert "padding: 5px" in out_str
    assert "margin: 10px" in out_str


# ── BOM handling ───────────────────────────────────────────────────


def test_handles_utf8_bom_in_original(tmp_path: Path):
    css = b"\xef\xbb\xbf.a { color: red; }"
    patch = tmp_path / "mod.css.patch"
    patch.write_text(".a { color: blue; }", encoding="utf-8")
    out, _log = apply_patches(css, [("Mod", patch)])
    assert "color: blue" in out.decode("utf-8")


def test_returns_none_on_invalid_utf8(tmp_path: Path):
    css = b"\xff\xff invalid"
    patch = tmp_path / "mod.css.patch"
    patch.write_text(".a { color: blue; }", encoding="utf-8")
    out, log = apply_patches(css, [("Mod", patch)])
    assert out is None
    assert any("Cannot decode" in line for line in log)
