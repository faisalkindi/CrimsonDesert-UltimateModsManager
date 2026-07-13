"""The field-order extractor's string parsing, on synthetic input.

The real macOS binary isn't committed (and its order-correctness is the
whole point of running it live), so this pins the *parsing*: given the
reflection error-string bytes, does it recover {class: [fields]} in order,
de-duplicated? The order-verification half is covered by
test_schema_verify.py.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_TOOL = (Path(__file__).resolve().parents[1] / "tools"
         / "extract_field_order.py")
_spec = importlib.util.spec_from_file_location("extract_field_order", _TOOL)
efo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(efo)

UI = "의".encode("utf-8")      # 의  (after class name)
REUL = "를".encode("utf-8")    # 를  (after field name)
TAIL = " 읽어들이는데 실패했다.".encode("utf-8")


def _errstr(cls: str, field: str) -> bytes:
    # "<cls>의 _<field>를 읽어들이는데 실패했다.\0"
    return cls.encode() + UI + b" " + field.encode() + REUL + TAIL + b"\x00"


def test_extract_recovers_class_and_field_in_order():
    blob = (b"\x00\x00"
            + _errstr("ItemInfo", "_isBlocked")
            + _errstr("ItemInfo", "_maxStackCount")
            + _errstr("BuffInfo", "_level")
            + _errstr("ItemInfo", "_itemName"))
    got = efo.extract(blob)
    assert got["ItemInfo"] == ["_isBlocked", "_maxStackCount", "_itemName"]
    assert got["BuffInfo"] == ["_level"]


def test_extract_dedupes_preserving_first_position():
    blob = (_errstr("Tbl", "_a") + _errstr("Tbl", "_b")
            + _errstr("Tbl", "_a")            # repeat -> ignored
            + _errstr("Tbl", "_c"))
    assert efo.extract(blob)["Tbl"] == ["_a", "_b", "_c"]


def test_extract_ignores_non_errorstring_noise():
    # a bare class name and a bare field, with no 의/를 framing, must not
    # register as a pair
    blob = b"ItemInfo\x00_isBlocked\x00" + _errstr("Real", "_x")
    got = efo.extract(blob)
    assert "ItemInfo" not in got
    assert got == {"Real": ["_x"]}


def test_extract_on_empty_is_empty():
    assert efo.extract(b"") == {}
    assert efo.extract(b"\x00" * 100) == {}
