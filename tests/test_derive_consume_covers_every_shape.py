"""Every shape ``_apply`` walks must have a branch in ``_consume``.

``_consume`` translates a pinned model into the ``(kind, n)`` spec
``_apply`` takes. Its final ``else`` produces ``("fixed", base)``, so a
shape added to ``_apply`` without a matching branch here does not fail
loudly: it is silently read as a bare ``base`` bytes.

That happened. #417 taught stage 1b to pin ``('slist', n)`` and ``_apply``
to walk it, and this mapping never got the branch, so a pinned
variable-element list was consumed as ``n`` bytes with no count, no
elements and no strings. It made stage 2 disagree with a hand walk that
mapped the same model correctly, and it flattered the stageinfo probe:
the walk appeared to reach field 24 while skipping straight past the
fields it could not really read. With the branch in place it stops at
field 22, which is the honest position.

This is checked structurally rather than by driving a table, because the
property is exactly "the two lists agree" and a behavioural test would
only cover the shapes someone remembered to exercise.
"""
from __future__ import annotations

import re
from pathlib import Path

_SRC = (Path(__file__).resolve().parents[1]
        / "tools" / "derive_table_layout.py")


def _section(name: str) -> str:
    src = _SRC.read_text(encoding="utf-8")
    start = src.index(f"    def {name}(self")
    nxt = src.index("\n    def ", start + 1)
    return src[start:nxt]


def _apply_kinds() -> set[str]:
    """Shapes ``_apply`` handles, from its `if kind == "..."` tests."""
    return set(re.findall(r'kind == "(\w+)"', _section("_apply")))


def _consume_kinds() -> set[str]:
    """Shapes ``_consume`` maps, from its `t == "..."` / `t in (...)` tests."""
    body = _section("_consume")
    kinds = set(re.findall(r't == "(\w+)"', body))
    for group in re.findall(r't in \(([^)]*)\)', body):
        kinds |= set(re.findall(r'"(\w+)"', group))
    return kinds


def test_both_sides_were_found():
    """Guard against the regexes quietly matching nothing."""
    assert len(_apply_kinds()) >= 4
    assert len(_consume_kinds()) >= 4


def test_consume_maps_every_shape_apply_can_walk():
    # 'fixed' is the documented default of the final else, so it needs no
    # branch of its own; everything else does.
    missing = sorted(_apply_kinds() - _consume_kinds() - {"fixed"})
    assert not missing, (
        "these shapes are walkable by _apply but have no branch in "
        f"_consume, so they degrade to ('fixed', base) in silence: "
        f"{missing}")


def test_slist_specifically_is_mapped():
    """The one that actually got lost. Pinned by name so a refactor that
    drops it again fails here rather than in a probe months later."""
    assert "slist" in _apply_kinds()
    assert "slist" in _consume_kinds()
