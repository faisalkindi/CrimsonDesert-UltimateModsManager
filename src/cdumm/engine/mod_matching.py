"""Pure duplicate-mod matching logic, decoupled from Qt.

Used by :func:`cdumm.gui.fluent_window.CdummWindow._find_existing_mod`
to decide whether a newly dropped mod is the same as one already in the
library. Previously this logic lived inline in ``fluent_window.py`` and
matched on 4-character substrings in either direction, which caused the
DeathZxZ bug: dropping ``"Infinite Stamina (All Skills Horse Spirit)"``
silently overwrote ``"Infinite Stamina"``.

Two public functions:

:func:`is_same_mod`
    Strict equality on :func:`prettify_mod_name` output. Different
    version numbers and NexusMods-style suffixes collapse to equality;
    different scopes (``"All Skills Horse Spirit"``) do not.

:func:`token_overlap_ratio`
    Jaccard similarity between the prettified word tokens of two names.
    Values in the ``[0.6, 1.0)`` range are shown to the user as
    "near-match" so they can pick between Update / Add as new / Cancel.
"""
from __future__ import annotations

from cdumm.engine.import_handler import prettify_mod_name


def is_same_mod(name_a: str, name_b: str) -> bool:
    """Return True when both names prettify to the same string."""
    return _key(name_a) == _key(name_b)


def token_overlap_ratio(name_a: str, name_b: str) -> float:
    """Jaccard similarity on prettified-word tokens: ``|A ∩ B| / |A ∪ B|``.

    Returns 0.0 for either input being empty after prettification.
    """
    tokens_a = set(_key(name_a).split())
    tokens_b = set(_key(name_b).split())
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def _key(name: str) -> str:
    """Lowercased, prettified representation used as the equality key."""
    return prettify_mod_name(name).lower().strip()
