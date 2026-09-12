"""A u32 string-table reference must not be classified as a CString.

``solve_reader`` decides a reader is an inline length-prefixed string when
it sees a leading 4-byte read plus an unsized one. It used to accept the
PERMISSIVE unsized flag (``var_seen``: any non-immediate write to r8),
on the argument that requiring the leading read to be exactly 4 made
that safe, because 4 is the length prefix.

It is not safe, and the counterexample is a whole class of reader rather
than one stray function. A u32 STRING-TABLE REFERENCE reads exactly four
bytes and then treats them as a hash::

    14148F57C  mov   r8d, 4              ; the only stream read
    14148F587  call  [rax+8]
    14148F5B9  div   ecx                 ; hash % bucket count
    14148F5C4  add   r11, [r10+0x78]     ; bucket table
    14148F5E3  mov   r8, [rax+rcx*8]     ; <- sets var_seen
    14148F5F8  mov   eax, 0xffff         ; miss sentinel

The bucket-pointer load writes r8 from memory, so ``var_seen`` was set,
the leading read was 4, and the rule fired. On disk such a field is four
bytes and nothing more, so calling it ``('str', 0)`` made every walk past
it eat the following field's bytes as string payload.

The discriminator is a SECOND STREAM READ. A CString reads its length and
then reads that many bytes, so it makes two calls into the stream vtable.
The hash reference makes exactly one and does the rest in memory. That is
what ``var_read`` tests, and it is what the rule now requires.

``sub_1411A33C0``, the function the permissive flag was kept for, does not
need it: its reads go through immediate calls rather than an r8 width, so
``var_seen`` is False there and it never reached this rule. The flag
bought nothing and cost every hash-referenced string field.

Measured over every table with a verified order, switching to the strict
flag regressed nothing and took sixteen tables further, including these
going from no fully walked record to all of them:

    CharacterAppearanceIndexInfo  0 -> 8350
    UIMapTextureInfo              0 -> 2025
    KnowledgeGroupInfo            0 ->  615
    QuestGaugeInfo                0 ->  511
    GameAdviceInfo                0 ->  478
    ContentsPhaseInfo             0 ->    4
    FactionOperationGroupInfo     0 ->    9

GitHub #409. Checked against the installed game because the input is the
shipped executable; there is no fixture for a function body.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
pytest.importorskip("capstone", reason="analysis-only dependency")
pytest.importorskip("pefile", reason="analysis-only dependency")

#: Reads 4 bytes, then resolves them through the string hash table.
HASH_REF = 0x14148F570
#: Same shape, a second instance so the fix is not pinned to one address.
HASH_REF_2 = 0x14148FA00
#: A genuine CString: reads a u32 length, then reads that many bytes.
CSTRING = 0x1413910A0


def _game_dir() -> Path | None:
    env = os.environ.get("CDUMM_GAME_DIR")
    if env and (Path(env) / "bin64").is_dir():
        return Path(env)
    for root in ("C:", "D:", "E:", "F:"):
        for lib in ("SteamLibrary", "Steam"):
            p = Path(f"{root}/{lib}/steamapps/common/Crimson Desert")
            if (p / "bin64").is_dir():
                return p
    return None


@pytest.fixture(scope="module")
def deriver():
    game = _game_dir()
    if game is None:
        pytest.skip("no Crimson Desert install found (set CDUMM_GAME_DIR)")
    from derive_table_layout import Deriver
    return Deriver(game)


def _stream_calls(deriver, va: int) -> int:
    """Calls through the stream vtable, i.e. `call qword ptr [reg + disp]`."""
    from capstone import CS_OP_MEM
    n = 0
    for ins in deriver.body(va):
        if ins.mnemonic == "call" and ins.operands[0].type == CS_OP_MEM:
            n += 1
    return n


@pytest.mark.slow
@pytest.mark.parametrize("va", [HASH_REF, HASH_REF_2])
def test_a_hash_reference_consumes_four_bytes_and_no_payload(deriver, va):
    deriver._memo.clear()
    assert deriver.solve_reader(va) == ("fixed", 4)


@pytest.mark.slow
@pytest.mark.parametrize("va", [HASH_REF, HASH_REF_2])
def test_the_hash_reference_makes_exactly_one_stream_read(deriver, va):
    """The property the classification rests on, asserted directly.

    If this ever becomes 2, the reader genuinely reads a payload and the
    expectation above should be revisited rather than forced.
    """
    assert _stream_calls(deriver, va) == 1


@pytest.mark.slow
def test_a_real_cstring_is_still_a_cstring(deriver):
    """The fix must not buy its correctness by refusing real strings."""
    deriver._memo.clear()
    assert deriver.solve_reader(CSTRING) == ("str", 0)
    assert _stream_calls(deriver, CSTRING) >= 2


@pytest.mark.slow
def test_characterinfo_string_fields_are_four_bytes_each(deriver):
    """The table that surfaced this, checked end to end.

    ``_uiIconPath`` and ``_category`` are hash references. Before the fix
    the walk died on ``_uiIconPath`` for all 7250 records because the
    preceding field had been read as a string.
    """
    reads = {name: (kind, val) for name, kind, val
             in deriver.field_reads("CharacterInfo")}
    for field in ("_uiIconPath", "_category"):
        kind, val = reads[field]
        assert kind == "call"
        deriver._memo.clear()
        assert deriver.solve_reader(val) == ("fixed", 4), field
