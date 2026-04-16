"""Tolerant JSON loader for community mod files.

Handles common issues in hand-edited JSON:
  - UTF-8 BOM
  - Single-line // comments
  - Block /* ... */ comments
  - Trailing commas before } or ]
"""

import json
import re
import logging

from pathlib import Path

logger = logging.getLogger(__name__)

# Matches JSON strings, // line comments, or /* block comments.
# Group 1 captures strings (including their quotes) so we can keep them.
# Unmatched portions are comments to strip.
_COMMENT_RE = re.compile(
    r'("(?:[^"\\]|\\.)*")'   # group 1: JSON string literal
    r'|//[^\n]*'             # single-line comment
    r'|/\*.*?\*/',           # block comment
    flags=re.DOTALL,
)


def _strip_comments(text: str) -> str:
    """Remove JS-style comments while preserving string contents."""
    return _COMMENT_RE.sub(lambda m: m.group(1) or '', text)


def load_json_tolerant(path: Path) -> dict | list:
    """Load a JSON file, auto-repairing common syntax issues.

    Tries strict json.loads first. On failure: strips BOM, removes
    // and /* */ comments, strips trailing commas, and retries.
    """
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")  # strips BOM if present

    # Try strict parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strip comments (preserving strings)
    cleaned = _strip_comments(text)
    # Strip trailing commas before } or ]
    cleaned = re.sub(r',\s*([}\]])', r'\1', cleaned)

    try:
        result = json.loads(cleaned)
        logger.info("JSON auto-repaired: %s", path.name)
        return result
    except json.JSONDecodeError:
        raise
