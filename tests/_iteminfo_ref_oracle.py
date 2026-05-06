"""Reference oracle for iteminfo native parser tests.

The crimson_rs.pyd parser is used here ONLY to verify our native
parser's output. Production code never imports crimson_rs through
this file or directly.
"""
from __future__ import annotations

from typing import Any


def parse_with_oracle(data: bytes) -> list[dict]:
    """Run the .pyd parser. Skip if not loadable."""
    from cdumm.engine.crimson_rs_loader import get_crimson_rs
    crs = get_crimson_rs()
    if crs is None:
        import pytest
        pytest.skip("crimson_rs.pyd not loadable")
    return crs.parse_iteminfo_from_bytes(data)


def deep_dict_diff(a: Any, b: Any, path: str = "") -> list[str]:
    """Return human-readable list of every leaf-level mismatch
    between two dicts. Empty list = identical."""
    out: list[str] = []
    if type(a) is not type(b):
        return [f"{path}: type {type(a).__name__} vs {type(b).__name__}"]
    if isinstance(a, dict):
        keys = set(a) | set(b)
        for k in sorted(keys):
            sub = f"{path}.{k}" if path else k
            if k not in a:
                out.append(f"{sub}: missing in ours")
            elif k not in b:
                out.append(f"{sub}: missing in oracle")
            else:
                out.extend(deep_dict_diff(a[k], b[k], sub))
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{path}: list len {len(a)} vs {len(b)}"]
        for i, (x, y) in enumerate(zip(a, b)):
            out.extend(deep_dict_diff(x, y, f"{path}[{i}]"))
        return out
    if a != b:
        out.append(f"{path}: {a!r} vs {b!r}")
    return out
