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

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
pytest.importorskip("capstone", reason="analysis-only dependency")
pytest.importorskip("pefile", reason="analysis-only dependency")

READER = 0x1414960A0
FIELDS = ("_logoutMercenaryGroupInfoList", "_hideMercenaryGroupInfoList")


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
def test_the_reader_under_test_is_the_one_stageinfo_actually_calls(deriver):
    """Anchor the VA to the extractor, not to a remembered address.

    Skipping this step is what produced #420 and #423. If the binary
    moves or the extractor changes its mind, this fails first and names
    the reason, instead of the width assertion failing for a reason that
    looks like a regression in ``_const_reg``.
    """
    reads = {name: (kind, val) for name, kind, val in
             deriver.field_reads("StageInfo")}
    for field in FIELDS:
        assert reads.get(field) == ("call", READER), (
            f"{field} is no longer read by sub_{READER:X}; re-derive the "
            f"reader VA before trusting the rest of this module")


@pytest.mark.slow
def test_a_hoisted_register_width_resolves_to_the_stream_size(deriver):
    """The width lives in ``ebp``, and the answer is the stream size."""
    deriver._memo.clear()
    assert deriver.list_element(READER) == ("fixed", 1)


@pytest.mark.slow
def test_the_function_is_its_own_and_the_loop_is_inside_it(deriver):
    """``.pdata`` agrees this is a whole function, loop included.

    The reader starts a function of its own and the sized read at
    ``0x14149610E`` falls inside that extent, so the width is genuinely
    this function's and not a neighbour's.
    """
    lo, hi = deriver.function_extent(READER)
    assert lo == READER
    assert lo <= 0x14149610E < hi


@pytest.mark.slow
def test_a_width_with_no_unique_definition_still_abstains(deriver):
    """Unique-or-nothing survives the change; this one must stay unknown."""
    deriver._memo.clear()
    assert deriver.list_element(0x14149A200) is None
