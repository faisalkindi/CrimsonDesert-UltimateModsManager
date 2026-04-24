"""B1: reject a mod at import time if its .pamt doesn't parse.

Before v3.1.7 a mod could ship a malformed 0.pamt and the import
would succeed silently. Apply would then fail on every run (see
``test_pamt_parse_failure_surfaces_to_user.py`` for that side).

Fix: validate the pamt bytes with parse_pamt after CRC auto-fix in
_process_extracted_files. If it raises ValueError, refuse to save
the delta and surface a clear error to the user.

This is a wiring guard — the cost of standing up the full import
flow in a unit test is high. We assert the validation call exists
at the right point in the source.
"""
from __future__ import annotations

import re
from pathlib import Path


def _import_handler_src() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "engine" / "import_handler.py").read_text(
                encoding="utf-8")


def test_import_validates_pamt_after_crc_fix():
    src = _import_handler_src()
    # Anchor on the existing CRC-fix call — our validation must land
    # immediately after it (same bytes, same branch).
    anchor = src.find("_verify_and_fix_pamt_crc(modified_bytes, rel_path)")
    assert anchor != -1, "CRC-fix anchor not found"
    # Scope: next ~800 chars.
    scope = src[anchor:anchor + 2000]
    # v3.1.7.1: the call was extracted into _validate_modified_pamt
    # which wraps parse_pamt with a safe tempfile name. Accept either
    # the direct parse_pamt call (old shape) or the helper (new shape).
    assert ("parse_pamt" in scope
            or "_validate_modified_pamt" in scope), (
        "after CRC fix, the import must call parse_pamt (directly or "
        "via _validate_modified_pamt) to validate the bytes before "
        "saving a delta — otherwise a corrupt pamt gets stored and "
        "apply fails forever")
    # The exception path must be explicit: a raise, not a silent
    # logger call. v3.1.7.1: the raise can live inside the
    # _validate_modified_pamt helper rather than inline at the
    # callsite. Accept either.
    has_inline_raise = re.search(
        r"raise\s+(ValueError|ImportError|RuntimeError)", scope)
    if not has_inline_raise and "_validate_modified_pamt" in scope:
        # Helper path — verify the helper itself raises.
        helper_match = re.search(
            r"def\s+_validate_modified_pamt[^\n]*:\s*\n(.*?)\n(?=def |\Z)",
            src, flags=re.DOTALL)
        assert helper_match and re.search(
            r"raise\s+(ValueError|ImportError|RuntimeError)",
            helper_match.group(1)), (
            "_validate_modified_pamt must raise so corrupt pamts "
            "surface during import")
    else:
        assert has_inline_raise, (
            "a corrupt pamt must raise at import time so the user "
            "sees the problem during the import step, not 7 minutes "
            "into apply")


def test_import_error_message_names_mod_and_file():
    src = _import_handler_src()
    anchor = src.find("_verify_and_fix_pamt_crc(modified_bytes, rel_path)")
    assert anchor != -1
    scope = src[anchor:anchor + 2000]
    # Heuristic: the raised error includes rel_path and some form of
    # "corrupt" or "invalid" marker. We want the user to see WHICH
    # file inside the mod broke.
    assert "rel_path" in scope, (
        "import-level error must name the offending file "
        "(rel_path) so the user knows which pamt is bad")
