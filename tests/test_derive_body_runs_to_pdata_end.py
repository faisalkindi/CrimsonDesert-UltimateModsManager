"""``body()`` decodes to the function's real end, not a fixed window.

It disassembled 600 bytes from the start and stopped at the first
``ret`` no forward branch jumped past. For a function longer than 600
bytes that ``ret`` is never reached, the decode just ends where the
window does, and nothing says so. The head decodes cleanly, every rule
sees a well-formed prefix, and the tail is simply never there.

That is a quieter failure than the window OVERRUN the docstring warns
about, and worse, because it produces a confident wrong answer rather
than a refusal. stageinfo's ``_sequencerDesc`` reader (``sub_14228FF40``)
is 883 bytes. The window ended at byte 591, just after a 4-byte read at
0x14229014A. That read is a COUNT. The loop it bounds starts at byte 576,
inside the window, but its backward branch at 0x1422901FC (byte 700) is
past it, so the decode never saw a loop at all. ``reader_parts``
therefore reported the count as a bare ``('fixed', 4)`` and the whole
reader as 20 flat parts,
61 bytes with empty strings. NattKh's hand model of the same field is 83
bytes on the same record, and the 22-byte difference is exactly the
list and the six sub-readers after it that the window hid. Every field
of stageinfo after ``_sequencerDesc`` inherited that offset.

.pdata already knows where the function ends. ``body()`` now decodes to
that end when ``va`` starts a function, and keeps the window only as
the fallback for an address .pdata does not cover.

Measured: of 147 distinct field readers across the tables with a
verified order, seven are longer than 600 bytes. Six already refused and
still do. One, this one, went from a wrong answer to a refusal, and
stageinfo's walk depth went from 21.7 to 6.0 of 91. The lower number is
the honest one; the higher was a walk past a wrong model.

GitHub #409.
"""
from __future__ import annotations

import pytest

#: sub_14228FF40 on b25116796, 883 bytes there and on b25381195.
SEQUENCER_DESC = ("StageInfo", "_sequencerDesc")
#: The loop's head is inside the old window; its backward branch is not.
#: Byte offsets from the function start, identical on both builds.
LOOP_HEAD_OFF = 576
LOOP_BACK_BRANCH_OFF = 700
OLD_WINDOW = 600



@pytest.mark.slow
def test_the_function_really_is_longer_than_the_old_window(deriver, reader_of):
    """The premise. If the binary changes and this shrinks under 600
    bytes, the rest of this module is no longer testing the bug."""
    va = reader_of(*SEQUENCER_DESC)
    lo, hi = deriver.function_extent(va)
    assert lo == va
    assert hi - lo > OLD_WINDOW
    assert LOOP_HEAD_OFF < OLD_WINDOW < LOOP_BACK_BRANCH_OFF, (
        "the old window must cut the loop in half, head inside and "
        "backward branch outside, for this to be the case the fix was "
        "written for")


@pytest.mark.slow
def test_body_reaches_the_pdata_end(deriver, reader_of):
    va = reader_of(*SEQUENCER_DESC)
    _lo, hi = deriver.function_extent(va)
    ins = deriver.body(va)
    assert ins[-1].address + ins[-1].size == hi
    back = next(i for i in ins if i.address == va + LOOP_BACK_BRANCH_OFF)
    assert back.mnemonic == "jb"
    assert back.operands[0].imm == va + LOOP_HEAD_OFF


@pytest.mark.slow
def test_the_reader_now_refuses_instead_of_flattening_the_list(deriver, reader_of):
    """A struct with a count-prefixed list in the middle is not
    expressible as flat parts, and the honest answer is None until the
    tooling can express it. It must not go back to ('strplus', 41)."""
    va = reader_of(*SEQUENCER_DESC)
    deriver._memo.clear()
    assert deriver.solve_reader(va) is None
    deriver._memo.clear()
    assert deriver.reader_parts(va) is None


@pytest.mark.slow
def test_a_mid_function_address_still_gets_only_the_window(deriver, reader_of):
    """The extent is used only from a function's own start. A body
    requested from inside a function is not extended to the function's
    end, because that address is not where the function begins and the
    caller asked for a window."""
    mid = reader_of(*SEQUENCER_DESC) + LOOP_HEAD_OFF
    ins = deriver.body(mid, window=64)
    assert ins
    assert ins[-1].address < mid + 64 + 16
