"""#151: no CDUMM tmp-dir name (`cdumm_variant_XXXX`, `cdumm_swap_*`,
`cdumm_cfg_*`) should ever prettify to a user-visible mod name.

Butanokaabii reported Faster Vanilla Animations landing in the mod
list as 'Cdumm Variant 2yfxupya' — that's the `mkdtemp(prefix=…)`
sentinel leaking through to the DB via a variant-picker flow.
prettify_mod_name is the chokepoint every display-name callsite
passes through, so sanitizing here is the last-line defense even when
the specific leak path isn't yet pinned down.
"""
from __future__ import annotations

from cdumm.engine.import_handler import prettify_mod_name


def test_cdumm_variant_prefix_does_not_survive_prettify():
    """'cdumm_variant_2yfxupya' must not become 'Cdumm Variant 2yfxupya'."""
    out = prettify_mod_name("cdumm_variant_2yfxupya")
    assert not out.lower().startswith("cdumm variant"), (
        f"tmp-dir stem leaked to prettified name: {out!r}")


def test_cdumm_swap_prefix_rejected():
    out = prettify_mod_name("cdumm_swap_1234abcd")
    assert not out.lower().startswith("cdumm swap"), out


def test_cdumm_cfg_prefix_rejected():
    out = prettify_mod_name("cdumm_cfg_ff112233")
    assert not out.lower().startswith("cdumm cfg"), out


def test_cdumm_preset_prefix_rejected():
    out = prettify_mod_name("cdumm_preset_99aabb")
    assert not out.lower().startswith("cdumm preset"), out


def test_real_names_still_prettify_normally():
    """Guardrail: the sanitizer must not swallow legitimate names."""
    # 'JSON' isn't in the acronym allowlist, so prettify title-cases it.
    # The point here is the -411-1-2-timestamp suffix gets stripped and
    # no CDUMM-tmp prefix is prepended.
    assert prettify_mod_name("Worldmap Darkmode JSON-411-1-2-1775358894") \
        == "Worldmap Darkmode Json"
    assert prettify_mod_name("Faster Vallia Style Beta-774-0-2-3-1775617564") \
        == "Faster Vallia Style Beta"
    # A mod name that happens to contain "CDUMM" as a word (not a
    # tmp-dir prefix) must still go through. 'Cdumm' is title-cased
    # by the prettifier (only the acronym allowlist stays uppercase).
    assert prettify_mod_name("My CDUMM Tweak v1") == "My Cdumm Tweak"
