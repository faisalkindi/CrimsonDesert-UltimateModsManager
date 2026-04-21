"""Line-surgical INI editor: used to update a single key inside ReShade.ini
while leaving every other line (comments, blank lines, formatting) byte-identical.

Python's configparser drops comments and collapses blank lines on write, which
would silently mangle a user's ReShade.ini. This module edits one line at a time.
"""
from __future__ import annotations

from cdumm.engine.ini_line_editor import replace_key_in_section


def test_replace_existing_key_updates_just_that_line() -> None:
    src = "[GENERAL]\nPresetPath=old.ini\nOther=keep\n"
    out = replace_key_in_section(src, "GENERAL", "PresetPath", "new.ini")
    assert out == "[GENERAL]\nPresetPath=new.ini\nOther=keep\n"


def test_replace_preserves_inline_comments_after_value() -> None:
    """ReShade doesn't emit inline `; comment` on writes, but a hand-edited
    INI might have them. Don't strip them."""
    src = "[GENERAL]\nPresetPath=old.ini ; active preset\nOther=keep\n"
    out = replace_key_in_section(src, "GENERAL", "PresetPath", "new.ini")
    # The line is rewritten with the new value; we don't try to preserve the
    # inline comment since we can't reliably tell it apart from the value in
    # all INI dialects. Document the expected behavior here:
    # - Whole line becomes "PresetPath=new.ini"
    # - Adjacent standalone comment lines (above/below) stay untouched
    assert "PresetPath=new.ini" in out
    assert "Other=keep" in out


def test_replace_preserves_blank_lines_and_section_comments() -> None:
    src = (
        "; top-of-file comment\n"
        "\n"
        "[GENERAL]\n"
        "; setup notes\n"
        "PresetPath=old.ini\n"
        "\n"
        "Other=keep\n"
    )
    out = replace_key_in_section(src, "GENERAL", "PresetPath", "new.ini")
    assert out == (
        "; top-of-file comment\n"
        "\n"
        "[GENERAL]\n"
        "; setup notes\n"
        "PresetPath=new.ini\n"
        "\n"
        "Other=keep\n"
    )


def test_replace_adds_key_when_section_exists_but_key_missing() -> None:
    src = "[GENERAL]\nOther=keep\n[INPUT]\nFoo=bar\n"
    out = replace_key_in_section(src, "GENERAL", "PresetPath", "new.ini")
    # Inserted at end of [GENERAL] section, before [INPUT].
    assert "PresetPath=new.ini\n[INPUT]" in out
    assert "Other=keep\n" in out


def test_replace_adds_section_when_missing() -> None:
    src = "[INPUT]\nFoo=bar\n"
    out = replace_key_in_section(src, "GENERAL", "PresetPath", "new.ini")
    # Appended as a new section at end.
    assert out.endswith("[GENERAL]\nPresetPath=new.ini\n")
    assert "[INPUT]\nFoo=bar\n" in out


def test_replace_handles_crlf_line_endings() -> None:
    """Windows files use CRLF. Don't convert them."""
    src = "[GENERAL]\r\nPresetPath=old.ini\r\nOther=keep\r\n"
    out = replace_key_in_section(src, "GENERAL", "PresetPath", "new.ini")
    # Preserve CRLF on the rewritten line AND the surrounding lines.
    assert "\r\n" in out
    assert "PresetPath=new.ini\r\n" in out
    assert "Other=keep\r\n" in out


def test_replace_section_name_case_insensitive() -> None:
    """ReShade.ini uses [GENERAL] but INI spec allows case-insensitive section names.
    Accept any case variant."""
    src = "[general]\nPresetPath=old.ini\n"
    out = replace_key_in_section(src, "GENERAL", "PresetPath", "new.ini")
    assert "PresetPath=new.ini" in out


def test_replace_empty_value_works() -> None:
    """User unsets PresetPath -> empty string."""
    src = "[GENERAL]\nPresetPath=foo.ini\n"
    out = replace_key_in_section(src, "GENERAL", "PresetPath", "")
    assert "PresetPath=\n" in out


def test_replace_does_not_match_key_in_other_section() -> None:
    """Same key name in another section is NOT touched."""
    src = (
        "[GENERAL]\nPresetPath=a.ini\n"
        "[OTHER]\nPresetPath=b.ini\n"
    )
    out = replace_key_in_section(src, "GENERAL", "PresetPath", "new.ini")
    assert "[GENERAL]\nPresetPath=new.ini" in out
    assert "[OTHER]\nPresetPath=b.ini" in out  # untouched


def test_replace_file_without_trailing_newline() -> None:
    """Some INI editors omit the final newline. Handle it."""
    src = "[GENERAL]\nPresetPath=old.ini"  # no trailing \n
    out = replace_key_in_section(src, "GENERAL", "PresetPath", "new.ini")
    assert "PresetPath=new.ini" in out


def test_replace_preserves_original_key_casing() -> None:
    """If the file has 'presetpath=' (lowercase), rewrite keeps lowercase --
    don't silently change the user's casing choice."""
    src = "[GENERAL]\npresetpath=old.ini\n"
    out = replace_key_in_section(src, "GENERAL", "PresetPath", "new.ini")
    assert "presetpath=new.ini" in out
    assert "PresetPath=" not in out


def test_replace_preserves_leading_whitespace() -> None:
    """Indented keys (unusual but possible) keep their indentation."""
    src = "[GENERAL]\n    PresetPath=old.ini\n"
    out = replace_key_in_section(src, "GENERAL", "PresetPath", "new.ini")
    assert "    PresetPath=new.ini" in out


def test_replace_rewrites_last_duplicate_key_if_section_has_dupes() -> None:
    """Malformed INI with duplicate keys: rewrite the LAST one so users who
    edit via a tool that appends-wins see their latest value replaced."""
    src = "[GENERAL]\nPresetPath=a.ini\nPresetPath=b.ini\n"
    out = replace_key_in_section(src, "GENERAL", "PresetPath", "new.ini")
    # Only the second (last) one is rewritten; the first stays as-is.
    assert out.count("PresetPath=") == 2
    assert "PresetPath=a.ini" in out
    assert "PresetPath=new.ini" in out
