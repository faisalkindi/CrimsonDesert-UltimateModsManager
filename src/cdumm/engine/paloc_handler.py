"""`.paloc` localization string tables (GitHub #290).

CDUMM previously treated `.paloc` as an opaque blob -- the only awareness
anywhere was `paz_parse.rewrite_pamt_localization_filename`, which renames
the file inside a PAMT node for language redirects. The contents were never
parsed, so `.cdmod` `localization-patch` components (which append to
individual strings instead of replacing the whole table) could not be
applied.

WIRE FORMAT
-----------

    file   := record*  u32 record_count     <- TRAILER, not a header

    record := u32   tag         # category enum; see below
              u32   reserved    # 0 on every record observed
              u32   key_len
              bytes key         # ASCII DECIMAL STRING, e.g. "42597485641824"
              u32   value_len   # BYTES, not characters
              bytes value       # UTF-8

The keys being decimal *strings* is why a localization-patch ships
``"key": "42597485641824"`` as a string rather than an int.

THE FILE SELF-VERIFIES
----------------------
The 4-byte trailer is the record count. Parse and compare: if they disagree,
the framing is wrong. That check is free, and it is the thing that stops this
decoder from lying the way the iteminfo one did -- a byte-exact round-trip
proves the bytes are PRESERVED, not that they are UNDERSTOOD (#285). Here we
get an independent witness, so we use it.

DO NOT RECOMPUTE `tag`
----------------------
It looks like it could be a length or a checksum. It is not -- measured over
the 187,526 records of the live Ukrainian table:

    tag == value byte length ...  1,049 / 187,526
    tag == value char length ...  2,721 / 187,526
    tag == key length ........      370 / 187,526

38 distinct values, max 52: a category enum. Preserve it verbatim. Had it
been value-derived, carrying it through blindly would have corrupted every
string touched -- and the file would still have parsed clean.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_HDR = struct.Struct("<III")     # tag, reserved, key_len
_U32 = struct.Struct("<I")

#: Sanity bound. Real keys are short decimal strings; a wild length means the
#: framing has desynced and we must fail loudly rather than allocate.
_MAX_KEY = 128


class PalocError(Exception):
    """The table doesn't parse. Never fall back to 'best effort' -- a
    mis-framed write corrupts every string in the game."""


@dataclass
class PalocEntry:
    tag: int
    reserved: int
    key: str          # decimal string, as stored
    value: str        # UTF-8

    def encode(self) -> bytes:
        k = self.key.encode("utf-8")
        v = self.value.encode("utf-8")
        return (_HDR.pack(self.tag, self.reserved, len(k)) + k
                + _U32.pack(len(v)) + v)


def parse_paloc(data: bytes) -> list[PalocEntry]:
    """Parse a .paloc. Raises PalocError if the framing doesn't hold."""
    if len(data) < 4:
        raise PalocError("too short to be a .paloc")

    out: list[PalocEntry] = []
    p = 0
    end = len(data) - 4          # the trailer is not a record
    while p < end:
        if p + _HDR.size > end:
            raise PalocError(f"truncated record header at {p}")
        tag, reserved, klen = _HDR.unpack_from(data, p)
        p += _HDR.size
        if klen > _MAX_KEY or p + klen + 4 > end:
            raise PalocError(f"implausible key_len {klen} at {p}")
        key = data[p:p + klen]
        p += klen
        vlen = _U32.unpack_from(data, p)[0]
        p += 4
        if p + vlen > end:
            raise PalocError(f"value_len {vlen} overruns the table at {p}")
        value = data[p:p + vlen]
        p += vlen
        try:
            out.append(PalocEntry(tag, reserved, key.decode("utf-8"),
                                  value.decode("utf-8")))
        except UnicodeDecodeError as e:
            raise PalocError(f"record {len(out)} is not UTF-8: {e}")

    if p != end:
        raise PalocError(f"records end at {p}, expected {end}")

    declared = _U32.unpack_from(data, end)[0]
    if declared != len(out):
        # The format hands us a witness. Use it.
        raise PalocError(
            f"record count mismatch: the table's trailer says {declared} "
            f"but {len(out)} parsed -- the framing is wrong, refusing")

    logger.debug("paloc: parsed %d records", len(out))
    return out


def serialize_paloc(entries: list[PalocEntry]) -> bytes:
    out = bytearray()
    for e in entries:
        out += e.encode()
    out += _U32.pack(len(entries))       # trailer = record count
    return bytes(out)


def apply_changes(data: bytes, changes: list[dict]) -> tuple[bytes, int, list]:
    """Apply a localization-patch's ``changes[]`` to a .paloc.

    Returns ``(new_bytes, n_applied, missing_keys)``.

    A key the table doesn't have is REPORTED, not silently ignored: a patch
    that matches nothing would otherwise install clean and change nothing --
    the failure shape of #259 / #275 / #278 / #285.
    """
    entries = parse_paloc(data)
    by_key = {e.key: e for e in entries}

    applied = 0
    missing: list[str] = []
    for ch in changes:
        key = str(ch.get("key", ""))
        e = by_key.get(key)
        if e is None:
            missing.append(key)
            continue
        op = ch.get("op", "append")
        if op == "append":
            e.value = e.value + str(ch.get("suffix", ""))
        elif op == "set":
            e.value = str(ch.get("value", ""))
        elif op == "prefix":
            e.value = str(ch.get("prefix", "")) + e.value
        else:
            raise PalocError(
                f"unknown localization op {op!r} on key {key} -- refusing "
                f"rather than guessing what it means")
        applied += 1

    if missing:
        logger.warning(
            "paloc: %d of %d change(s) target keys this table doesn't have "
            "(e.g. %s)", len(missing), len(changes), missing[:3])

    return serialize_paloc(entries), applied, missing
