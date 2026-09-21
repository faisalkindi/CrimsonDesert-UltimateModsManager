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

import pytest

#: Pushes r15, then hoists 1 into it. sub_14149B4F0 on b25116796.
INLINE_ELEMENT_READER = ("StageInfo", "_subTimelineBreakDescList")
#: Takes its loop width from a register with no unique definition, so
#: unique-or-nothing must abstain. On b25116796 that was sub_14149A200
#: (r15d); on b25381195 the one reader in the binary with the property
#: is this one (edi). Found by instrumenting _const_reg over every
#: field reader of every class.
AMBIGUOUS_WIDTH_READER = ("FactionManagementSpawnData", "_orderList")



@pytest.mark.slow
def test_the_push_really_is_there_and_really_writes_only_rsp(deriver, reader_of):
    """The premise, asserted rather than assumed.

    If the compiler stops saving r15 here, this test is no longer
    exercising the bug it was written for, and it should say so loudly
    instead of passing for the wrong reason.
    """
    from derive_table_layout import Frame
    ins = deriver.body(reader_of(*INLINE_ELEMENT_READER))
    pushes = [x for x in ins
              if x.mnemonic == "push" and x.op_str.strip() == "r15"]
    assert pushes, "expected this reader to save r15 at entry"
    _read, written = pushes[0].regs_access()
    names = {Frame.q(deriver.md.reg_name(r)) for r in written}
    assert "r15" not in names
    assert "rsp" in names


@pytest.mark.slow
def test_a_width_hoisted_into_a_saved_register_resolves(deriver, reader_of):
    ins = deriver.body(reader_of(*INLINE_ELEMENT_READER))
    use = next(n for n, x in enumerate(ins)
               if x.mnemonic == "mov" and x.op_str.startswith("r8d, r15d"))
    assert deriver._const_reg(ins, use, "r15d") == 1


@pytest.mark.slow
def test_the_inline_assembled_element_is_thirteen_bytes(deriver, reader_of):
    """1 + 4 + 4 + 4, the one inline read plus three hash references."""
    va = reader_of(*INLINE_ELEMENT_READER)
    deriver._memo.clear()
    assert deriver.list_element(va) == ("fixed", 13)
    assert deriver._parts[va] == [
        ("fixed", 1), ("fixed", 4), ("fixed", 4), ("fixed", 4)]


@pytest.mark.slow
def test_unique_or_nothing_survives(deriver, reader_of):
    """The fix must not buy its answers by relaxing the rule.

    This reader's width also lives in a register, but that register has
    more than one definition, so the honest answer is still None.
    """
    va = reader_of(*AMBIGUOUS_WIDTH_READER)
    abstained = []
    orig = type(deriver)._const_reg

    def spy(self, ins, before, reg):
        r = orig(self, ins, before, reg)
        if r is None:
            abstained.append(reg)
        return r
    type(deriver)._const_reg = spy
    try:
        deriver._memo.clear()
        assert deriver.list_element(va) is None
    finally:
        type(deriver)._const_reg = orig
    assert abstained, "expected the refusal to come from _const_reg"


@pytest.mark.slow
def test_stageinfo_has_exactly_one_unresolved_reader_and_it_is_honest(deriver):
    """The reason this mattered. GitHub #409's meta note said six.

    After this fix the count was zero. Then ``body()`` was taught to
    decode to the .pdata end rather than a 600-byte window, and
    ``_sequencerDesc`` (``sub_14228FF40``, 883 bytes) went from a
    confident wrong flat model to a refusal: it is a struct with a
    count-prefixed list in the MIDDLE, which no current shape expresses.
    That refusal is correct and is pinned here so it cannot silently turn
    back into the wrong answer.
    """
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
        unresolved.append(name)
    assert unresolved == ["_sequencerDesc"]
