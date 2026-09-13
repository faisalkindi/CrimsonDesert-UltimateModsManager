"""Naming a register is not defining it.

``_const_reg`` resolves a width held in a register by UNIQUE DEFINITION:
collect every write to it in the function and answer only when there is
exactly one and it is `mov <reg>, <imm>`. It decided what counted as a
write from the operand position, so `push r15` counted, because r15 is
its first operand.

A push writes rsp and memory. It does not define r15. Counting it put a
None beside the real constant, `vals` had two entries, and the
unique-or-nothing rule abstained on a register that had exactly one
definition. Any reader whose hoisted width sat in a callee-saved register
was therefore refused, since saving that register at entry is precisely
what callee-saved means.

capstone's ``regs_access`` reports what an instruction actually writes,
so it is asked rather than inferred:

    push r15      -> writes ['rsp']
    mov  r15d, 1  -> writes ['r15d']

The worked example is stageinfo's ``_subTimelineBreakDescList``
(``sub_14149B4F0``), the last reader in that table ``derive_table_layout``
could not fit. Its element is assembled inline in the loop rather than by
a single element reader: one 1-byte read whose width is the hoisted r15d,
then three calls to the 4-byte hash reference ``sub_14148F570``. So the
element is 1+4+4+4 = 13 stream bytes, stored as an 8-byte memory element.

Measured over every table with a verified order, this regressed nothing
and improved two:

    VibratePatternInfo   0 -> 28 records fully walked (all of them)
    RelationInfo         mean walk depth 0.0 -> 2.0

and it took StageInfo's unresolved readers from one to none.

GitHub #409.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
pytest.importorskip("capstone", reason="analysis-only dependency")
pytest.importorskip("pefile", reason="analysis-only dependency")

#: _subTimelineBreakDescList's reader: pushes r15, then hoists 1 into it.
INLINE_ELEMENT_READER = 0x14149B4F0
#: Takes its width from r15d, which has no unique definition. Must abstain.
AMBIGUOUS_WIDTH_READER = 0x14149A200


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


@pytest.mark.slow
def test_the_push_really_is_there_and_really_writes_only_rsp(deriver):
    """The premise, asserted rather than assumed.

    If the compiler stops saving r15 here, this test is no longer
    exercising the bug it was written for, and it should say so loudly
    instead of passing for the wrong reason.
    """
    from derive_table_layout import Frame
    ins = deriver.body(INLINE_ELEMENT_READER)
    pushes = [x for x in ins
              if x.mnemonic == "push" and x.op_str.strip() == "r15"]
    assert pushes, "expected this reader to save r15 at entry"
    _read, written = pushes[0].regs_access()
    names = {Frame.q(deriver.md.reg_name(r)) for r in written}
    assert "r15" not in names
    assert "rsp" in names


@pytest.mark.slow
def test_a_width_hoisted_into_a_saved_register_resolves(deriver):
    ins = deriver.body(INLINE_ELEMENT_READER)
    use = next(n for n, x in enumerate(ins)
               if x.mnemonic == "mov" and x.op_str.startswith("r8d, r15d"))
    assert deriver._const_reg(ins, use, "r15d") == 1


@pytest.mark.slow
def test_the_inline_assembled_element_is_thirteen_bytes(deriver):
    """1 + 4 + 4 + 4, the one inline read plus three hash references."""
    deriver._memo.clear()
    assert deriver.list_element(INLINE_ELEMENT_READER) == ("fixed", 13)
    assert deriver._parts[INLINE_ELEMENT_READER] == [
        ("fixed", 1), ("fixed", 4), ("fixed", 4), ("fixed", 4)]


@pytest.mark.slow
def test_unique_or_nothing_survives(deriver):
    """The fix must not buy its answers by relaxing the rule.

    This reader's width also lives in a register, but that register has
    more than one definition, so the honest answer is still None.
    """
    deriver._memo.clear()
    assert deriver.list_element(AMBIGUOUS_WIDTH_READER) is None


@pytest.mark.slow
def test_stageinfo_has_no_unresolved_readers_left(deriver):
    """The reason this mattered. GitHub #409's meta note said six."""
    import re
    unresolved = []
    for name, kind, va in deriver.field_reads("StageInfo"):
        if kind != "call":
            continue
        deriver._memo.clear()
        if deriver.solve_reader(va) is not None:
            continue
        el = deriver.list_element(va)
        if el is not None and el[0] == "fixed":
            continue
        er = deriver.element_reader(va)
        parts = deriver.reader_parts(er) if er is not None else None
        if parts and sum(1 for k, _v in parts if k == "str") > 1:
            continue
        if el is not None and re.search(r"element is (\d+) \+ n", el[1] or ""):
            continue
        unresolved.append((name, hex(va)))
    assert unresolved == []
