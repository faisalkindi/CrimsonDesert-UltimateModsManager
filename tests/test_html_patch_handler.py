"""Test CDUMM's HTML patch handler — JMM v9.9.3 parity."""
from __future__ import annotations

from pathlib import Path

import pytest

from cdumm.engine.html_patch_handler import (
    apply_patches, derive_target_from_patch_path, detect_patch_file,
    parse_patch_file, parse_simple_syntax,
)


# ── Detection ───────────────────────────────────────────────────────


def test_detects_html_patch_extension():
    assert detect_patch_file(Path("foo.html.patch")) == "html_patch"


def test_detects_html_merge_extension():
    assert detect_patch_file(Path("foo.html.merge")) == "html_merge"


def test_rejects_non_html_extensions():
    assert detect_patch_file(Path("foo.html")) is None
    assert detect_patch_file(Path("foo.css.patch")) is None
    assert detect_patch_file(Path("foo.xml.patch")) is None


def test_derives_target(tmp_path: Path):
    mod_root = tmp_path / "mod"
    (mod_root / "ui").mkdir(parents=True)
    p = mod_root / "ui" / "menu.html.patch"
    p.write_text("", encoding="utf-8")
    assert derive_target_from_patch_path(p, mod_root) == "ui/menu.html"


# ── Simple syntax (HTML-tag ops) ─────────────────────────────────────


def test_set_attr_via_set_tag(tmp_path: Path):
    html = b'<div id="x" class="a"></div>'
    patch = tmp_path / "mod.html.patch"
    patch.write_text(
        '<set at="#x" data-state="open" />', encoding="utf-8")
    out, log = apply_patches(html, [("Mod", patch)])
    assert b'data-state="open"' in out


def test_remove_via_remove_tag(tmp_path: Path):
    html = b'<div><p id="gone">bye</p><p id="keep">hi</p></div>'
    patch = tmp_path / "mod.html.patch"
    patch.write_text('<remove at="#gone" />', encoding="utf-8")
    out, _log = apply_patches(html, [("Mod", patch)])
    assert b"#gone" not in out
    assert b'id="gone"' not in out
    assert b'id="keep"' in out


def test_replace_inner_via_inner_alias(tmp_path: Path):
    html = b'<h1 id="t">old</h1>'
    patch = tmp_path / "mod.html.patch"
    patch.write_text('<inner at="#t">new</inner>', encoding="utf-8")
    out, _log = apply_patches(html, [("Mod", patch)])
    assert b">new<" in out
    assert b">old<" not in out


def test_replace_inner_via_explicit_tag(tmp_path: Path):
    html = b'<h1 id="t">old</h1>'
    patch = tmp_path / "mod.html.patch"
    patch.write_text(
        '<replace-inner at="#t">new</replace-inner>', encoding="utf-8")
    out, _log = apply_patches(html, [("Mod", patch)])
    assert b">new<" in out


def test_replace_swaps_whole_element(tmp_path: Path):
    html = b'<h1 id="t">old</h1>'
    patch = tmp_path / "mod.html.patch"
    patch.write_text(
        '<replace at="#t"><h2>new</h2></replace>', encoding="utf-8")
    out, _log = apply_patches(html, [("Mod", patch)])
    out_str = out.decode("utf-8")
    assert "<h2>new</h2>" in out_str
    assert "<h1" not in out_str


def test_append_inserts_at_end(tmp_path: Path):
    html = b'<ul id="l">\n  <li>a</li>\n</ul>'
    patch = tmp_path / "mod.html.patch"
    patch.write_text(
        '<append at="#l"><li>b</li></append>', encoding="utf-8")
    out, _log = apply_patches(html, [("Mod", patch)])
    out_str = out.decode("utf-8")
    a_pos = out_str.index("<li>a</li>")
    b_pos = out_str.index("<li>b</li>")
    assert a_pos < b_pos


def test_prepend_inserts_at_start(tmp_path: Path):
    html = b'<ul id="l">\n  <li>a</li>\n</ul>'
    patch = tmp_path / "mod.html.patch"
    patch.write_text(
        '<prepend at="#l"><li>b</li></prepend>', encoding="utf-8")
    out, _log = apply_patches(html, [("Mod", patch)])
    out_str = out.decode("utf-8")
    a_pos = out_str.index("<li>a</li>")
    b_pos = out_str.index("<li>b</li>")
    assert b_pos < a_pos


def test_class_plus_minus_in_set_tag(tmp_path: Path):
    html = b'<div id="x" class="old keep"></div>'
    patch = tmp_path / "mod.html.patch"
    patch.write_text(
        '<set at="#x" class="+new -old" />', encoding="utf-8")
    out, _log = apply_patches(html, [("Mod", patch)])
    out_str = out.decode("utf-8")
    assert "new" in out_str
    assert "keep" in out_str
    assert "old" not in out_str.split('class="')[1].split('"')[0]


# ── Comment-directive form ──────────────────────────────────────────


def test_directive_replace_with_payload_block(tmp_path: Path):
    html = b'<h1 id="t">old</h1>'
    patch = tmp_path / "mod.html.patch"
    patch.write_text(
        '<!-- @replace selector="#t" -->\n<h2>new</h2>\n<!-- @end -->',
        encoding="utf-8")
    out, _log = apply_patches(html, [("Mod", patch)])
    assert b"<h2>new</h2>" in out
    assert b"<h1" not in out


def test_directive_set_attr(tmp_path: Path):
    html = b'<img src="old.png" />'
    patch = tmp_path / "mod.html.patch"
    patch.write_text(
        '<!-- @set-attr selector="img" name="src" value="new.png" -->',
        encoding="utf-8")
    out, _log = apply_patches(html, [("Mod", patch)])
    assert b'src="new.png"' in out
    assert b'src="old.png"' not in out


def test_directive_add_class(tmp_path: Path):
    html = b'<button class="btn">click</button>'
    patch = tmp_path / "mod.html.patch"
    patch.write_text(
        '<!-- @add-class selector=".btn" value="primary" -->',
        encoding="utf-8")
    out, _log = apply_patches(html, [("Mod", patch)])
    out_str = out.decode("utf-8")
    cls = out_str.split('class="')[1].split('"')[0].split()
    assert "btn" in cls
    assert "primary" in cls


def test_directive_remove(tmp_path: Path):
    html = b'<div id="keep">y</div><div id="gone">x</div>'
    patch = tmp_path / "mod.html.patch"
    patch.write_text(
        '<!-- @remove selector="#gone" -->', encoding="utf-8")
    out, _log = apply_patches(html, [("Mod", patch)])
    assert b'id="keep"' in out
    assert b'id="gone"' not in out


def test_directive_missing_end_warns(tmp_path: Path):
    html = b'<div id="x">old</div>'
    patch = tmp_path / "mod.html.patch"
    patch.write_text(
        '<!-- @replace selector="#x" -->\n<p>new</p>\n',
        encoding="utf-8")
    out, log = apply_patches(html, [("Mod", patch)])
    assert any("missing matching" in line for line in log)
    # original unchanged
    assert b">old</div>" in out


# ── Selectors ───────────────────────────────────────────────────────


def test_descendant_selector(tmp_path: Path):
    html = (b'<div class="card">'
            b'<button class="primary">A</button>'
            b'</div>'
            b'<button class="primary">B</button>')
    patch = tmp_path / "mod.html.patch"
    patch.write_text(
        '<set at=".card .primary" data-x="hit" />', encoding="utf-8")
    out, _log = apply_patches(html, [("Mod", patch)])
    out_str = out.decode("utf-8")
    # First button (inside .card) gets data-x; second one (outside) does NOT.
    parts = out_str.split("</button>")
    assert 'data-x="hit"' in parts[0]
    assert 'data-x="hit"' not in parts[1]


def test_attribute_selector(tmp_path: Path):
    html = b'<input type="text" /><input type="checkbox" />'
    patch = tmp_path / "mod.html.patch"
    patch.write_text(
        '<set at="input[type=text]" data-x="t" />',
        encoding="utf-8")
    out, _log = apply_patches(html, [("Mod", patch)])
    out_str = out.decode("utf-8")
    parts = out_str.split("/>")
    assert 'data-x="t"' in parts[0]
    assert 'data-x="t"' not in parts[1]


# ── Robustness ──────────────────────────────────────────────────────


def test_void_element_is_self_closing_without_slash(tmp_path: Path):
    html = b'<div><img src="a.png"><p>after</p></div>'
    patch = tmp_path / "mod.html.patch"
    patch.write_text(
        '<set at="img" alt="cat" />', encoding="utf-8")
    out, _log = apply_patches(html, [("Mod", patch)])
    assert b'alt="cat"' in out
    # The <p> after the void <img> still closes the <div> properly,
    # not eaten by the implicit non-closure of img
    assert b"<p>after</p>" in out


def test_no_match_does_not_modify(tmp_path: Path):
    html = b'<div id="x"></div>'
    patch = tmp_path / "mod.html.patch"
    patch.write_text(
        '<set at=".doesnotexist" data-x="hit" />', encoding="utf-8")
    out, log = apply_patches(html, [("Mod", patch)])
    assert out.decode("utf-8") == html.decode("utf-8")
    assert any("matched nothing" in line for line in log)


def test_returns_none_on_invalid_utf8(tmp_path: Path):
    html = b"\xff\xff invalid"
    patch = tmp_path / "mod.html.patch"
    patch.write_text(
        '<set at=".x" data-y="z" />', encoding="utf-8")
    out, log = apply_patches(html, [("Mod", patch)])
    assert out is None
    assert any("Cannot decode" in line for line in log)


def test_handles_utf8_bom(tmp_path: Path):
    html = b'\xef\xbb\xbf<div id="x"></div>'
    patch = tmp_path / "mod.html.patch"
    patch.write_text(
        '<set at="#x" data-x="hit" />', encoding="utf-8")
    out, _log = apply_patches(html, [("Mod", patch)])
    assert b'data-x="hit"' in out


# ── Multi-mod ordering ──────────────────────────────────────────────


def test_two_mods_applied_in_order(tmp_path: Path):
    html = b'<div id="x"></div>'
    p1 = tmp_path / "first.html.patch"
    p1.write_text('<set at="#x" data-a="1" />', encoding="utf-8")
    p2 = tmp_path / "second.html.patch"
    p2.write_text('<set at="#x" data-b="2" />', encoding="utf-8")
    out, _log = apply_patches(html, [("First", p1), ("Second", p2)])
    out_str = out.decode("utf-8")
    assert 'data-a="1"' in out_str
    assert 'data-b="2"' in out_str
