"""buff_data_count is structural , writing it without adding or
removing actual items would corrupt the entry. The apply path must
NOT expose it as a writable wrapper field.

Found via systematic-debugging sweep on the wiring: locate_buff_field
listed ``buff_data_count`` in _WRAPPER_FIELDS as a u32 write target,
which would let a mod patch the count to any value without changing
the items list. Result: parser would later read N items from the
patched count but only have M actual items, walking past entry end.

This test pins that the intent fails to resolve so no bytes are
emitted.
"""
from __future__ import annotations


def test_buff_data_count_not_writable():
    from cdumm._vendor.buffinfo_parser import (
        locate_buff_field, _WRAPPER_FIELDS,
    )
    assert "buff_data_count" not in _WRAPPER_FIELDS, (
        "buff_data_count is structural and must not be a writable "
        "wrapper field; writes without matching item-list changes "
        "corrupt the entry layout")
