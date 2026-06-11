"""Encryption-heuristic false positive must be CLEARED on plain decode
(audit finding 3).

PazEntry.encrypted has no real flag to read; it guesses from the
extension (.xml / .css / .html / .js). When such an entry actually
stores PLAINTEXT LZ4 (heuristic false positive), decompress_entry used
to return the content while leaving entry.encrypted == True, so repack
ChaCha20-encrypted a plaintext slot and the game read cipher bytes
where it expects plaintext. A successful plain LZ4 decode now sets
_encrypted_override = False.
"""
from __future__ import annotations

from cdumm.archive.paz_crypto import encrypt, lz4_compress
from cdumm.archive.paz_parse import PazEntry
from cdumm.engine.json_patch_handler import decompress_entry


def _xml_entry(comp: bytes, plain: bytes) -> PazEntry:
    return PazEntry(
        path="ui/xml/menu/main.xml", paz_file="x", offset=0,
        comp_size=len(comp), orig_size=len(plain),
        flags=2 << 16, paz_index=0,
    )


def test_plain_lz4_xml_clears_false_positive_override():
    plain = b"<root>" + b"hello world " * 40 + b"</root>"
    comp = lz4_compress(plain)
    entry = _xml_entry(comp, plain)
    assert entry.encrypted is True  # heuristic guess from .xml

    out = decompress_entry(comp, entry)

    assert out == plain
    assert entry._encrypted_override is False, (
        "plain LZ4 decode succeeded WITHOUT decryption, so the entry "
        "is stored as plaintext; the override must be cleared or "
        "repack will encrypt a plaintext slot")
    assert entry.encrypted is False


def test_actually_encrypted_xml_still_sets_override_true():
    plain = b"<root>" + b"secret stuff " * 40 + b"</root>"
    comp = lz4_compress(plain)
    cipher = encrypt(comp, "main.xml")
    entry = _xml_entry(cipher, plain)

    out = decompress_entry(cipher, entry)

    assert out == plain
    assert entry._encrypted_override is True
    assert entry.encrypted is True


def test_existing_true_override_not_clobbered():
    """An override already confirmed True by an earlier extraction pass
    must not be flipped by this code path's guard (it only fires when
    the override is still None)."""
    plain = b"<a>" + b"x" * 200 + b"</a>"
    comp = lz4_compress(plain)
    entry = _xml_entry(comp, plain)
    entry._encrypted_override = True

    # Plain payload decodes fine, but the guard requires override None.
    out = decompress_entry(comp, entry)
    assert out == plain
    assert entry._encrypted_override is True
