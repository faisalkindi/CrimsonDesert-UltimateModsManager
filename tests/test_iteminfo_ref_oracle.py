"""Tests for the reference oracle helper used by native iteminfo parser tests."""
from __future__ import annotations

from tests._iteminfo_ref_oracle import deep_dict_diff


def test_deep_dict_diff_identical_returns_empty():
    assert deep_dict_diff({"a": 1}, {"a": 1}) == []


def test_deep_dict_diff_simple_scalar_mismatch():
    assert deep_dict_diff({"a": 1}, {"a": 2}) == ["a: 1 vs 2"]


def test_deep_dict_diff_nested_dict_mismatch():
    a = {"outer": {"inner": 5}}
    b = {"outer": {"inner": 7}}
    diffs = deep_dict_diff(a, b)
    assert diffs == ["outer.inner: 5 vs 7"]


def test_deep_dict_diff_list_length_mismatch():
    diffs = deep_dict_diff({"xs": [1, 2]}, {"xs": [1, 2, 3]})
    assert diffs == ["xs: list len 2 vs 3"]


def test_deep_dict_diff_list_element_mismatch():
    diffs = deep_dict_diff({"xs": [1, 2, 3]}, {"xs": [1, 9, 3]})
    assert diffs == ["xs[1]: 2 vs 9"]


def test_deep_dict_diff_type_mismatch():
    diffs = deep_dict_diff({"a": 1}, {"a": "1"})
    assert diffs == ["a: type int vs str"]


def test_deep_dict_diff_missing_key():
    diffs = deep_dict_diff({"a": 1}, {"a": 1, "b": 2})
    assert diffs == ["b: missing in ours"]


def test_deep_dict_diff_extra_key():
    diffs = deep_dict_diff({"a": 1, "b": 2}, {"a": 1})
    assert diffs == ["b: missing in oracle"]
