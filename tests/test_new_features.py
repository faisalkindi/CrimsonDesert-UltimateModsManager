"""Tests for v3.1 features: flag inference, value encoding, XML detection, ZIP content detection."""

import struct
import tempfile
import zipfile
from pathlib import Path

import pytest


# ── Feature 3: Extension-based compression type inference ─────────────

def test_infer_comp_type_dds():
    from cdumm.archive.overlay_builder import _infer_comp_type_from_extension
    assert _infer_comp_type_from_extension("texture.dds") == 1


def test_infer_comp_type_bnk():
    from cdumm.archive.overlay_builder import _infer_comp_type_from_extension
    assert _infer_comp_type_from_extension("sound.bnk") == 0


def test_infer_comp_type_default():
    from cdumm.archive.overlay_builder import _infer_comp_type_from_extension
    assert _infer_comp_type_from_extension("data.json") == 2
    assert _infer_comp_type_from_extension("noext") == 2


def test_infer_comp_type_case_insensitive():
    from cdumm.archive.overlay_builder import _infer_comp_type_from_extension
    assert _infer_comp_type_from_extension("TEXTURE.DDS") == 1
    assert _infer_comp_type_from_extension("Sound.BNK") == 0


# ── Feature 5: Value encoding/decoding ───────────────────────────────

def test_encode_decode_int32():
    from cdumm.engine.json_patch_handler import encode_value, decode_value
    for val in [0, 1, 42, -1, 2147483647]:
        encoded = encode_value(val, "int32_le")
        assert len(encoded) == 8  # 4 bytes = 8 hex chars
        assert decode_value(encoded, "int32_le") == val


def test_encode_decode_float32():
    from cdumm.engine.json_patch_handler import encode_value, decode_value
    for val in [0.0, 1.0, 3.14, -99.5]:
        encoded = encode_value(val, "float32_le")
        assert len(encoded) == 8
        decoded = decode_value(encoded, "float32_le")
        assert abs(decoded - val) < 0.001


def test_encode_decode_uint8():
    from cdumm.engine.json_patch_handler import encode_value, decode_value
    for val in [0, 1, 127, 255]:
        encoded = encode_value(val, "uint8")
        assert len(encoded) == 2
        assert decode_value(encoded, "uint8") == val


def test_encode_decode_int16():
    from cdumm.engine.json_patch_handler import encode_value, decode_value
    for val in [0, 1, -1, 32767]:
        encoded = encode_value(val, "int16_le")
        assert len(encoded) == 4
        assert decode_value(encoded, "int16_le") == val


def test_encode_unknown_type():
    from cdumm.engine.json_patch_handler import encode_value
    with pytest.raises(ValueError):
        encode_value(42, "unknown_type")


# ── Feature 5: apply_custom_values ───────────────────────────────────

def test_apply_custom_values_replaces():
    from cdumm.engine.json_patch_handler import apply_custom_values
    changes = [
        {"offset": 0, "original": "02000000", "patched": "03000000",
         "label": "Mult", "editable_value": {"type": "int32_le"}},
        {"offset": 4, "original": "01", "patched": "02", "label": "Toggle"},
    ]
    result = apply_custom_values(changes, {"0": 7})
    assert result[0]["patched"] == "07000000"
    assert result[1]["patched"] == "02"  # unchanged


def test_apply_custom_values_none():
    from cdumm.engine.json_patch_handler import apply_custom_values
    changes = [{"offset": 0, "patched": "ff"}]
    assert apply_custom_values(changes, None) == changes
    assert apply_custom_values(changes, {}) == changes


# ── Feature 2: XML replacement detection ─────────────────────────────

def test_detect_xml_replacement():
    from cdumm.engine.import_handler import _detect_xml_replacements
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp)
        (p / "OG_inventory__mymod.xml").write_text("<root/>")
        results = _detect_xml_replacements(p)
        assert len(results) == 1
        assert results[0]["target_name"] == "inventory.xml"


def test_detect_xml_double_underscore():
    from cdumm.engine.import_handler import _detect_xml_replacements
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp)
        (p / "OG_ui_main__mod.xml").write_text("<root/>")
        results = _detect_xml_replacements(p)
        assert len(results) == 1
        assert results[0]["target_name"] == "ui_main.xml"


def test_detect_xml_none():
    from cdumm.engine.import_handler import _detect_xml_replacements
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp)
        (p / "normal_file.xml").write_text("<root/>")
        results = _detect_xml_replacements(p)
        assert len(results) == 0


# ── Feature 1: ZIP game content detection ────────────────────────────

def test_has_game_content_paz():
    from cdumm.gui.fluent_window import _has_game_content
    with tempfile.TemporaryDirectory() as tmp:
        zp = Path(tmp) / "test.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("0008/0.paz", b"data")
            zf.writestr("MyMod.asi", b"data")
        assert _has_game_content(zp) is True


def test_has_game_content_asi_only():
    from cdumm.gui.fluent_window import _has_game_content
    with tempfile.TemporaryDirectory() as tmp:
        zp = Path(tmp) / "test.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("MyMod.asi", b"data")
            zf.writestr("MyMod.ini", b"data")
        assert _has_game_content(zp) is False


# ── Feature 2: XML format fixup ──────────────────────────────────────

def test_fix_xml_format():
    from cdumm.engine.crimson_browser_handler import fix_xml_format
    raw = b'<?xml version="1.0"?>\n<root>\n  <item/>\n</root>'
    fixed = fix_xml_format(raw)
    assert fixed.startswith(b'\xef\xbb\xbf')  # UTF-8 BOM
    assert b'<?xml' not in fixed  # declaration removed
    assert b'\r\n' in fixed  # CRLF line endings
