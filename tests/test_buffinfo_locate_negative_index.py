"""locate_buff_field rejects negative item indices.

Found via systematic-debugging sweep on Phase 3f wiring: a path
like ``buff_data_list[-1].data.base.tag`` parses successfully via
``int("-1") == -1``, then ``n >= entry.buff_data_count`` is False
(any positive count > -1), then ``range(-1)`` is empty, then the
walker lands on item 0's header. The intent silently targets the
WRONG item.

Fix: reject any negative index up front, return None.
"""
from __future__ import annotations

import struct


def test_locate_buff_field_rejects_negative_index():
    from cdumm._vendor.buffinfo_parser import locate_buff_field

    # Build a 2-item entry where item 0 is present and resolvable.
    # If the bug exists, [-1] silently lands on item 0 and resolves.
    name = b"X"
    payload = b"\x05" + struct.pack("<I", 1) * 8 + struct.pack("<I", 0) + b""
    # Use the same payload-builder shape as test_buffinfo_payload_common
    from tests.test_buffinfo_payload_common import _build_payload_bytes
    payload = _build_payload_bytes(tag=17, by58=99)  # tag 17: known, 0-byte tail
    raw = (
        struct.pack("<I", 1)
        + struct.pack("<I", len(name)) + name
        + bytes([0])
        + struct.pack("<I", 1)
        + struct.pack("<I", 0xAA) + bytes([0x00]) + payload
        + struct.pack("<I", 1) + struct.pack("<I", 5)
        + struct.pack("<I", 0) + bytes([0])
        + struct.pack("<I", 0) * 3 + bytes([0, 0])
    )
    # Sanity: index 0 resolves
    assert locate_buff_field(
        raw, "buff_data_list[0].data.base.by58") is not None

    # The actual assertion: -1 must NOT silently resolve to item 0.
    assert locate_buff_field(
        raw, "buff_data_list[-1].data.base.by58") is None, (
        "negative item index must be rejected, not silently mapped "
        "to item 0")
    assert locate_buff_field(
        raw, "buff_data_list[-99].data.base.by58") is None
