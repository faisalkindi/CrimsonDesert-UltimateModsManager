"""Extension aliases for the game's static-info data tables.

Crimson Desert shipped every game-data table as an index + body pair
named ``<table>.pabgh`` / ``<table>.pabgb`` until the 2026-09-04 client
update renamed the pair to ``<table>.staticinfoheader`` /
``<table>.staticinfobody``. Only the extensions changed — the header is
still ``u16 count`` + ``count x (key, u32 offset)`` and the body is
still the same record stream, verified by feeding the post-update
``gamedata/*.staticinfoheader`` blobs to
:func:`cdumm.semantic.parser.parse_pabgh_index` (every key size, record
count and offset resolves inside its companion body).

Every mod on Nexus, every mod already imported into a user's CDMods
library and every Format 3 document in the wild declares its target as
``<table>.pabgb``, so CDUMM keeps ``.pabgb`` / ``.pabgh`` as the
*logical* names throughout the codebase and resolves both families
wherever a name meets the archive: the PAMT index, the sibling-header
lookups and the overlay writer. Overlay entries are still emitted under
whatever name the live PAMT actually carries (``PazEntry.path``), so a
patched table lands where the game looks for it.

Without this shim the 2026-09-04 update makes every JSON / Format 3 mod
apply cleanly and change nothing: ``_find_pamt_entry`` misses, and the
apply ends in ``APPLY_SILENT_FAILURE``.
"""

# Body ("blob") extensions, newest naming last-resort-free: the legacy
# name stays first because it is the one mods declare.
LEGACY_BODY_EXT = ".pabgb"
LEGACY_HEADER_EXT = ".pabgh"
STATIC_BODY_EXT = ".staticinfobody"
STATIC_HEADER_EXT = ".staticinfoheader"

BODY_EXTS = (LEGACY_BODY_EXT, STATIC_BODY_EXT)
HEADER_EXTS = (LEGACY_HEADER_EXT, STATIC_HEADER_EXT)
TABLE_EXTS = BODY_EXTS + HEADER_EXTS

# ext -> the matching header/body in the SAME naming family, so a path
# derived from a live PAMT entry keeps that entry's naming and a path
# derived from a mod's declared target keeps the mod's naming.
_BODY_TO_HEADER = {
    LEGACY_BODY_EXT: LEGACY_HEADER_EXT,
    STATIC_BODY_EXT: STATIC_HEADER_EXT,
}
_HEADER_TO_BODY = {v: k for k, v in _BODY_TO_HEADER.items()}

# ext -> its equivalent in the other family, for alias generation.
_CROSS_FAMILY = {
    LEGACY_BODY_EXT: STATIC_BODY_EXT,
    STATIC_BODY_EXT: LEGACY_BODY_EXT,
    LEGACY_HEADER_EXT: STATIC_HEADER_EXT,
    STATIC_HEADER_EXT: LEGACY_HEADER_EXT,
}


def split_table_ext(path: str) -> tuple[str, str | None]:
    """Return ``(stem, lowercased table extension)``.

    The extension is ``None`` when ``path`` is not a data table, in
    which case ``stem`` is ``path`` unchanged.
    """
    dot = path.rfind(".")
    if dot < 0:
        return path, None
    ext = path[dot:].lower()
    if ext in _CROSS_FAMILY:
        return path[:dot], ext
    return path, None


def is_body_path(path: str) -> bool:
    """True for ``foo.pabgb`` and its post-update ``foo.staticinfobody``."""
    return path.lower().endswith(BODY_EXTS)


def is_header_path(path: str) -> bool:
    """True for ``foo.pabgh`` and its post-update ``foo.staticinfoheader``."""
    return path.lower().endswith(HEADER_EXTS)


def is_table_path(path: str) -> bool:
    """True for either half of a data-table pair, in either naming."""
    return path.lower().endswith(TABLE_EXTS)


def header_path_for(body_path: str) -> str:
    """Companion header path for ``body_path``, keeping its naming family.

    Returns ``body_path`` unchanged when it isn't a table body, so
    callers can hand it any path without pre-checking.
    """
    stem, ext = split_table_ext(body_path)
    header_ext = _BODY_TO_HEADER.get(ext)
    return stem + header_ext if header_ext else body_path


def body_path_for(header_path: str) -> str:
    """Companion body path for ``header_path``, keeping its naming family."""
    stem, ext = split_table_ext(header_path)
    body_ext = _HEADER_TO_BODY.get(ext)
    return stem + body_ext if body_ext else header_path


def alias_paths(path: str) -> list[str]:
    """Every name this file could be stored or declared under.

    ``path`` itself always comes first, so callers that index or match
    in order keep their existing "real name wins" semantics. Non-table
    paths return a single-element list — this runs once per PAMT entry
    over millions of entries during an index build, so the common case
    stays a string compare and a list literal.
    """
    stem, ext = split_table_ext(path)
    if ext is None:
        return [path]
    return [path, stem + _CROSS_FAMILY[ext]]


def to_legacy_name(path: str) -> str:
    """Rewrite a table path to the ``.pabgb`` / ``.pabgh`` naming.

    CDUMM's internal target names, dispatch tables and user-facing
    warnings all speak the legacy naming, and so does every mod in the
    wild. Normalising a mod-declared target through here means a mod
    that starts shipping the post-update name still hits the same
    whole-table writers. Non-table paths are returned unchanged.
    """
    stem, ext = split_table_ext(path)
    if ext == STATIC_BODY_EXT:
        return stem + LEGACY_BODY_EXT
    if ext == STATIC_HEADER_EXT:
        return stem + LEGACY_HEADER_EXT
    return path


def strip_body_ext(name: str) -> str:
    """Lowercase ``name`` with a table BODY extension removed, path kept.

    The path-preserving counterpart to :func:`strip_table_ext`. Some
    callers deliberately keep the directory (``gamedata/iteminfo``) and
    have downstream guards built around that; widening only the
    extension keeps their behaviour bit-for-bit on a pre-update install
    while still accepting the post-2026-09-04 name.
    """
    stem, ext = split_table_ext(name)
    if ext in _BODY_TO_HEADER:
        return stem.lower()
    return name.lower()


def strip_table_ext(name: str) -> str:
    """Lowercased table name: ``gamedata/iteminfo.pabgb`` -> ``iteminfo``.

    Accepts a full path, a bare filename, or an already-stripped table
    name, in either naming family.
    """
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    if "\\" in name:
        name = name.rsplit("\\", 1)[-1]
    stem, _ext = split_table_ext(name)
    return stem.lower()
