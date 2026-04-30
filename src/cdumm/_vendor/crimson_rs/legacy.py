"""
Legacy parser wrapper for pre-April-2026 iteminfo.pabgb files.

The current crimson_rs parser handles the new game format (April 2026+).
Old iteminfo files use a different struct layout that the new parser can't read.
This module loads the old parser from crimson_rs_legacy.pyd to handle them.

Usage:
    from crimson_rs.legacy import parse_old_iteminfo, serialize_old_iteminfo

    old_items = parse_old_iteminfo(old_pabgb_bytes)
    old_bytes = serialize_old_iteminfo(old_items)
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys

_legacy_mod = None


def _load_legacy():
    global _legacy_mod
    if _legacy_mod is not None:
        return _legacy_mod

    legacy_dir = os.path.join(os.path.dirname(__file__), "_legacy")
    legacy_path = os.path.join(legacy_dir, "crimson_rs.pyd")
    if not os.path.isfile(legacy_path):
        raise ImportError(
            f"Legacy parser not found at {legacy_path}. "
            f"Copy the pre-April-2026 crimson_rs.pyd to crimson_rs/_legacy/."
        )

    spec = importlib.util.spec_from_file_location("crimson_rs", legacy_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _legacy_mod = mod
    return mod


def parse_old_iteminfo(data: bytes) -> list[dict]:
    mod = _load_legacy()
    return mod.parse_iteminfo_from_bytes(data)


def serialize_old_iteminfo(items: list[dict]) -> bytes:
    mod = _load_legacy()
    return mod.serialize_iteminfo(items)
