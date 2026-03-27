"""Base dispatcher for format-specific record identification.

Maps file extensions/paths to thin parsers that identify record boundaries.
Falls back to byte-range-only reporting for unknown formats.
"""
import logging
from pathlib import PurePosixPath

from cdumm.archive.format_parsers.pabgb_parser import identify_pabgb_records
from cdumm.archive.format_parsers.paac_parser import identify_paac_records
from cdumm.archive.format_parsers.pamt_parser import identify_pamt_records

logger = logging.getLogger(__name__)

# Map file path patterns to parsers
_PARSERS = {
    ".pabgb": identify_pabgb_records,
    ".paac": identify_paac_records,
    ".pamt": identify_pamt_records,
}


def identify_records_for_file(
    file_path: str, byte_start: int, byte_end: int, file_data: bytes | None = None
) -> str | None:
    """Attempt to identify which record a byte range falls within.

    Args:
        file_path: POSIX-style relative path (e.g., "0008/0.paz")
        byte_start: Start of changed byte range
        byte_end: End of changed byte range (exclusive)
        file_data: Raw file bytes (needed for parsing). If None, returns None.

    Returns:
        Human-readable record description, or None if format is unknown.
    """
    if file_data is None:
        return None

    # Determine format from file path
    suffix = PurePosixPath(file_path).suffix.lower()

    # For PAZ files, we can't determine the inner file format from the PAZ path alone
    # The caller should pass the inner file path if available
    parser = _PARSERS.get(suffix)
    if parser is None:
        return None

    try:
        return parser(file_data, byte_start, byte_end)
    except Exception:
        logger.debug("Parser failed for %s at bytes %d-%d", file_path, byte_start, byte_end,
                     exc_info=True)
        return None
