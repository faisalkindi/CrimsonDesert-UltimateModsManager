"""A loop whose element width sits in a register still gets a verdict.

MSVC hoists a literal width out of a loop when every iteration reads the
same size: `mov ebp, 1` before the loop, `mov r8d, ebp` inside it.
``field_reads`` already resolved that through ``_const_reg``. The
loop-body scan in ``list_element`` did not, so those readers came back
with no verdict at all, and a table using one could never be described.

stageinfo's ``_logoutMercenaryGroupInfoList`` (``sub_141494DB0``) is the
worked example, GitHub #409. Its body:

    141494DC6  mov  r8d, 4          ; u32 count
    141494DD3  call [rax+8]
    141494DFC  cmp  [rsp+0x68], esi ; count == 0 -> done
    141494E02  mov  ebp, 1          ; the width, hoisted
    141494E18  mov  r8d, ebp        ; one byte per element
    141494E1E  call [rax+8]
    141494E25  movzx edx, byte [rsp+0x50]

so the element is a single byte and the reader is ``CArray<u8>``. The
``movzx`` afterwards is the decoded value being widened for storage, not
a second stream read.

``_const_reg`` keeps its unique-or-nothing rule, so this does not become
a guess: a register written on more than one path still yields nothing.
``sub_14149A200`` takes its width from ``r15d``, which has no unique
definition, and correctly stays unresolved.

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
def test_that_reader_abstains_because_the_loop_is_not_its_own(deriver):
    """The #420 claim was read off a neighbouring function. It is wrong.

    ``0x141494DB0`` is not the start of its own function. ``.pdata`` puts
    it inside the range ``0x141494CF0`` to ``0x141494DF9``, and the loop
    quoted in #420, at ``0x141494E10`` onward, is past that end. Those
    instructions belong to a different fragment, so the `mov ebp, 1` /
    `mov r8d, ebp` pair says nothing about this reader.

    That is precisely the failure ``.pdata`` anchoring exists to prevent,
    and this module's own docstring warns about: a fixed window runs off
    a short function into whatever follows and attributes the
    neighbour's loops to it. My #420 measurement came from a window that
    did exactly that.

    So the honest assertion is the abstention. ``body`` stops at the real
    end, sees no sized stream read, and ``list_element`` returns None
    rather than inventing a width. GitHub #409.
    """
    lo, hi = deriver.function_extent(0x141494DB0)
    assert (lo, hi) == (0x141494CF0, 0x141494DF9)
    assert not (lo <= 0x141494E1E < hi), (
        "the loop #420 quoted must lie OUTSIDE this function, or this "
        "test is arguing against something that is no longer true")
    deriver._memo.clear()
    assert deriver.list_element(0x141494DB0) is None


@pytest.mark.slow
def test_a_width_with_no_unique_definition_still_abstains(deriver):
    """Unique-or-nothing survives the change; this one must stay unknown."""
    deriver._memo.clear()
    assert deriver.list_element(0x14149A200) is None
