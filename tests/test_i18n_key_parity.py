"""i18n audit guardrails (2026-06 audit).

Three invariants:

1. Every literal ``tr("...")`` key used in the GUI modules touched by
   the audit resolves to a real entry in en.json (no raw-key fallback
   leaking into the UI).
2. de.json carries every key en.json has, and nothing extra: missing
   count == 0 AND extras count == 0.
3. Every ``{placeholder}`` that appears in an English string appears
   in the German string for the same key (and vice versa), so
   ``str.format(**kwargs)`` never drops or KeyErrors a substitution.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TRANSLATIONS = REPO / "src" / "cdumm" / "translations"

# Files reworked by the i18n audit. Every literal tr() key in these
# must exist in en.json.
AUDITED_FILES = [
    "src/cdumm/gui/pages/settings_page.py",
    "src/cdumm/gui/pages/asi_page.py",
    "src/cdumm/gui/pages/mods_page.py",
    "src/cdumm/gui/pages/about_page.py",
    "src/cdumm/gui/pages/reshade_page.py",
    "src/cdumm/gui/pages/bug_report_page.py",
    "src/cdumm/gui/pages/tool_page.py",
    "src/cdumm/gui/pages/activity_page.py",
    "src/cdumm/gui/components/config_panel.py",
    "src/cdumm/gui/preset_picker.py",
    "src/cdumm/gui/conflict_view.py",
    "src/cdumm/gui/bug_report.py",
]

# Literal-only: tr("key" / tr('key'. Deliberately does NOT match
# tr(f"...") dynamic keys (e.g. activity.cat_{category}), those are
# covered by their own per-category keys.
_TR_RE = re.compile(r"""\btr\(\s*(["'])([^"'{}]+)\1""")

# conflict_view routes level labels through a key dict + level_label();
# assert those keys too since no literal tr("conflicts.level_*") exists.
_LEVEL_KEY_RE = re.compile(r"""["'](conflicts\.level_\w+)["']""")


def _load(lang: str) -> dict:
    with open(TRANSLATIONS / f"{lang}.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def en() -> dict:
    return _load("en")


@pytest.fixture(scope="module")
def de() -> dict:
    return _load("de")


def _placeholders(s: str) -> list[str]:
    return sorted(re.findall(r"\{(\w+)\}", s))


def test_all_literal_tr_keys_resolve_in_en(en):
    missing: list[str] = []
    for rel in AUDITED_FILES:
        text = (REPO / rel).read_text(encoding="utf-8")
        keys = {m.group(2) for m in _TR_RE.finditer(text)}
        if rel.endswith("conflict_view.py"):
            keys |= {m.group(1) for m in _LEVEL_KEY_RE.finditer(text)}
        for key in sorted(keys):
            if key not in en:
                missing.append(f"{rel}: {key}")
    assert not missing, (
        "tr() keys with no en.json entry (would render as raw keys):\n"
        + "\n".join(missing))


def test_audited_files_actually_use_tr(en):
    """Sanity check that the regex finds keys at all, guards against a
    silent regex break making the resolution test vacuous."""
    total = 0
    for rel in AUDITED_FILES:
        text = (REPO / rel).read_text(encoding="utf-8")
        total += len(list(_TR_RE.finditer(text)))
    assert total > 100, f"suspiciously few tr() literals found: {total}"


def test_de_missing_zero_vs_en(en, de):
    missing = [k for k in en if k not in de]
    assert not missing, (
        f"de.json missing {len(missing)} key(s) vs en.json: {missing}")


def test_de_has_no_extra_keys(en, de):
    extras = [k for k in de if k not in en]
    assert not extras, f"de.json has keys en.json lacks: {extras}"


def test_placeholder_parity_en_de(en, de):
    bad = []
    for key, en_val in en.items():
        de_val = de.get(key)
        if de_val is None:
            continue  # covered by the missing-keys test
        if _placeholders(en_val) != _placeholders(de_val):
            bad.append(
                f"{key}: en={_placeholders(en_val)} de={_placeholders(de_val)}")
    assert not bad, "placeholder mismatches en<->de:\n" + "\n".join(bad)


def test_translation_files_are_valid_json():
    for f in sorted(TRANSLATIONS.glob("*.json")):
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)
        assert isinstance(data, dict) and data, f"{f.name} empty/invalid"
        assert all(isinstance(v, str) for v in data.values()), (
            f"{f.name} has non-string values")
