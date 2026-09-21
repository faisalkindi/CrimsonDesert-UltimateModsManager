"""A loop whose element width sits in a register still gets a verdict.

MSVC hoists a literal width out of a loop when every iteration reads the
same size: ``mov ebp, 1`` before the loop, ``mov r8d, ebp`` inside it.
``field_reads`` already resolved that through ``_const_reg``. The
loop-body scan in ``list_element`` did not, so those readers came back
with no verdict at all, and a table using one could never be described.

stageinfo's ``_logoutMercenaryGroupInfoList`` and
``_hideMercenaryGroupInfoList`` are the worked example, GitHub #409.
Both fields call ``sub_1414960A0``, whose body is::

    1414960B6  mov   r8d, 4            ; u32 count
    1414960C3  call  [rax+8]
    1414960EC  cmp   [rsp+0x68], esi   ; count == 0 -> done
    1414960F2  mov   ebp, 1            ; the width, hoisted
    141496108  mov   r8d, ebp          ; one byte per element
    14149610E  call  [rax+8]
    141496115  movzx edx, byte [rsp+0x50]
    141496156  mov   [rax+rcx*2], bx   ; stored widened to u16

so one byte comes off the stream per element and is widened for
storage. The element size that matters to a layout walk is the stream
size, 1, not the 2 bytes the array holds.

``_const_reg`` keeps its unique-or-nothing rule, so this does not become
a guess: a register written on more than one path still yields nothing.
``sub_14149A200`` takes its width from ``r15d``, which has no unique
definition, and correctly stays unresolved.

History worth keeping, because it cost three retractions on #409. This
test previously asserted against ``0x141494DB0``, and #423 then rewrote
it to assert that ``0x141494DB0`` *abstains*, on the theory that the
loop had been read out of a neighbouring function through a window
overrun. Both framings were wrong in the same way: ``0x141494DB0`` does
not appear among stageinfo's callees at all, so nothing about it was
ever evidence for or against this field. The overrun story was correct
as a general hazard and is why ``.pdata`` anchoring exists, but it was
not what happened here. The lesson is narrower and duller: check that a
VA is the one the extractor actually reports for the field before
building anything on it.

Checked against the installed game because the input is the shipped
executable; there is no fixture for a function body.
"""
from __future__ import annotations

import pytest

#: sub_1414960A0 on b25116796; both fields share one reader.
FIELDS = ("_logoutMercenaryGroupInfoList", "_hideMercenaryGroupInfoList")



@pytest.mark.slow
def test_the_reader_under_test_is_the_one_stageinfo_actually_calls(deriver):
    """Anchor the VA to the extractor, not to a remembered address.

    Skipping this step is what produced #420 and #423. If the binary
    moves or the extractor changes its mind, this fails first and names
    the reason, instead of the width assertion failing for a reason that
    looks like a regression in ``_const_reg``.
    """
    reads = {name: (kind, val) for name, kind, val in
             deriver.field_reads("StageInfo")}
    vas = {reads.get(f) for f in FIELDS}
    assert len(vas) == 1 and next(iter(vas))[0] == "call", (
        "the two mercenary lists no longer share one reader: "
        + str({f: reads.get(f) for f in FIELDS}))


@pytest.mark.slow
def test_a_hoisted_register_width_resolves_to_the_stream_size(deriver, reader_of):
    """The width lives in ``ebp``, and the answer is the stream size."""
    va = reader_of("StageInfo", FIELDS[0])
    deriver._memo.clear()
    assert deriver.list_element(va) == ("fixed", 1)


@pytest.mark.slow
def test_the_function_is_its_own_and_the_loop_is_inside_it(deriver, reader_of):
    """``.pdata`` agrees this is a whole function, loop included.

    The reader starts a function of its own and its sized stream read
    (``call [rax+8]`` after ``mov r8d, ebp``; at 0x14149610E on
    b25116796) falls inside that extent, so the width is genuinely this
    function's and not a neighbour's.
    """
    from capstone import CS_OP_MEM
    va = reader_of("StageInfo", FIELDS[0])
    lo, hi = deriver.function_extent(va)
    assert lo == va
    ins = deriver.body(va)
    reads = [i.address for i in ins
             if i.mnemonic == "call" and i.operands[0].type == CS_OP_MEM]
    assert reads and all(lo <= a < hi for a in reads)


@pytest.mark.slow
def test_a_width_with_no_unique_definition_still_abstains(deriver, reader_of):
    """Unique-or-nothing survives the change; this one must stay unknown."""
    va = reader_of("FactionManagementSpawnData", "_orderList")
    deriver._memo.clear()
    assert deriver.list_element(va) is None
