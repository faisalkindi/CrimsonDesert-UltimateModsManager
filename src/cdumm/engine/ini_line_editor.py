"""Line-surgical INI editor.

Python's `configparser` drops comments and collapses blank lines on write. For
`ReShade.ini` that would silently mangle any user or installer-generated
comments and formatting. This module edits ONE line at a time and leaves
everything else byte-identical.

Scope: enough INI semantics to find a `[section]` header (case-insensitive),
locate a `key=` line within it, and rewrite or insert that line. Nothing else
is parsed or normalized.
"""
from __future__ import annotations

import re

_SECTION_HEADER = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")


def _detect_eol(text: str) -> str:
    """Return the dominant line ending in `text`. CRLF for Windows files,
    LF otherwise."""
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text and "\n" not in text:
        return "\r"
    return "\n"


def replace_key_in_section(text: str, section: str, key: str, new_value: str) -> str:
    """Return `text` with `[section] key=...` rewritten to `key=new_value`.

    Behavior:
      - Section match is case-insensitive (INI convention).
      - Original key casing is preserved when rewriting an existing line
        (user wrote `presetpath=` -> we keep `presetpath=` in the rewrite).
      - If the section exists but the key doesn't: key is appended at the end
        of the section (just before the next `[section]` or end of file),
        using the caller-supplied `key` spelling.
      - If the section doesn't exist: appended at end of file as a new section.
      - Existing line ending style (LF / CRLF) is preserved.
      - Other sections' keys with the same name are not touched.
    """
    eol = _detect_eol(text)
    ends_with_newline = text.endswith(("\r\n", "\r", "\n"))
    lines = text.splitlines()

    section_lower = section.lower()
    in_section = False
    section_start_idx: int | None = None
    section_end_idx: int | None = None   # exclusive
    key_line_idx: int | None = None
    original_key_text: str | None = None
    key_capture_re = re.compile(
        r"^(?P<lead>\s*)(?P<original>" + re.escape(key) + r")\s*=",
        re.IGNORECASE,
    )

    for i, line in enumerate(lines):
        header = _SECTION_HEADER.match(line)
        if header is not None:
            name = header.group("name").strip().lower()
            if name == section_lower:
                in_section = True
                section_start_idx = i
                section_end_idx = None
                continue
            # Different section starting.
            if in_section:
                section_end_idx = i
                break
            continue
        if in_section:
            match = key_capture_re.match(line)
            if match is not None:
                key_line_idx = i
                original_key_text = match.group("lead") + match.group("original")

    if in_section and section_end_idx is None:
        section_end_idx = len(lines)

    if key_line_idx is not None:
        # Case A: key found -> rewrite, preserving the original leading
        # whitespace + key casing the user had.
        lines[key_line_idx] = f"{original_key_text}={new_value}"
    elif section_start_idx is not None and section_end_idx is not None:
        # Case B: section exists, key missing -> insert at end of section
        # using the caller-supplied key spelling.
        lines.insert(section_end_idx, f"{key}={new_value}")
    else:
        # Case C: section missing -> append new section at end of file.
        lines.append(f"[{section}]")
        lines.append(f"{key}={new_value}")

    result = eol.join(lines)
    if ends_with_newline:
        result += eol
    return result
