"""Derive on-disk layouts for undecoded .pabgb tables, and prove them.

The blocker for the ~116 undecoded tables is not field ORDER -- that comes
from ``extract_field_order_win`` -- it is field WIDTH. The shipped schema
has no usable width for most of their fields (3 of them for
``actionpointinfo``, 110 of 205 for ``gimmickinfo``), and a walk with one
missing width ends short no matter how right the order is.

This derives the widths from the game itself, in two stages, and accepts
nothing that is not proven.

Stage 1 -- static, from the exe
    Each field is read either as a primitive with its width stated inline
    (``mov r8d, N`` before the vtable call) or through a typed sub-reader
    (``call <imm>``). The sub-readers are compositions, so they are solved
    recursively:

        fixed N    one sized read, no loop, no unaccounted subcall
        4 + n      a 4-byte length, an early-out when it is zero, then a
                   width taken from that slot -- the CString reader
        composite  a sequence of the above, plus already-solved subcalls

    A subcall that performs NO stream read consumes zero bytes. Getting
    that wrong is expensive: requiring every helper to have a model
    (hash-lookup helpers, allocators, loggers) solved 2 of 193 readers
    instead of 5.

    A stream read is a SIZED vtable call -- a width in ``r8d``, then
    ``call qword ptr [rax + 8]``. The width is the load-bearing half; a
    bare vtable call is an ordinary virtual call on some other object.

    A reader whose loop is on a stream-reading path is refused: that is a
    count-prefixed list, and stage 2 derives its element width. A loop in
    a helper that reads nothing consumes nothing and does not disqualify
    its caller.

    Readers stage 1 still cannot derive can be supplied by hand through
    ``HAND_VERIFIED``, kept separate and labelled so it is always visible
    which models are automatic and which are asserted. Those are held to
    the same tiling gate.

Stage 2 -- by constraint, from the table data
    A list reader is ``u32 count`` then ``count * E``. No game file states
    E: ``.debug$P`` is an array of code pointers rather than CodeView type
    records, and no shipped asset carries a schema. But the DATA
    constrains E -- it must make every record tile exactly. So E is
    searched, with the acceptance rule this project already used for
    storeinfo's ``ORDER_ELEM_SIZE``:

        Exactly one size satisfies every record, and it satisfies it for
        all of them. A size that "nearly" works fails outright -- so this
        is unique-or-nothing rather than best-fit.

    Two widths that both tile means the data cannot separate them, and the
    answer is "unknown". That case is real: ``dialogvoiceinfo`` accepts all
    128 candidates because every one of its lists is empty. Reporting it as
    ambiguous rather than taking the first is the difference between this
    being trustworthy and being a guess.

    Solving one reader can make another table single-unknown, so stage 2
    iterates to a fixed point.

THE GATE, for everything: exact tiling on 100% of records. Not "reached the
last field" -- a walk can run out of fields with bytes to spare and has
then understood nothing.

WHAT THIS DOES NOT ESTABLISH: semantics. Tiling proves the record
STRUCTURE and gives every named field its byte offset. It does not prove
that ``_limitDistance`` means a distance. Nothing here is writable until
its values are cross-checked against real records and gated to
``_verified_fields`` -- the community stat mapping was wrong on 18 of 24
entries, all flagged "verified", which is why that gate exists.

Needs ``capstone`` + ``pefile`` (analysis-only, not app dependencies) and a
game install. Read-only throughout::

    python -m pip install capstone pefile
    python tools/derive_table_layout.py --game-dir "E:/.../Crimson Desert"
"""
from __future__ import annotations

import argparse
import collections
import re
import struct
import sys
from bisect import bisect_left, bisect_right
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "tools"))

#: Tables that already have a decoder/writer; nothing to derive for them.
DECODED = {
    "iteminfo", "characterinfo", "dropsetinfo", "equipslotinfo",
    "multichangeinfo", "skill", "storeinfo", "stringinfo", "fieldinfo",
    "stageinfo", "regioninfo", "vehicleinfo", "wantedinfo", "statusinfo",
    "statusgroupinfo", "buffinfo", "knowledgeinfo", "interactioninfo",
    "inventory",
}

#: Consumed by the ENTRY HEADER, not the record body -- ``_stringKey`` IS
#: the entry name. Walking them again double-counts, and it is not a small
#: effect: leaving them in drops RegionInfo's walker from 21 fields to 2.
HEADER_FIELDS = ("_stringKey", "_key")

#: Widest plausible list element for the constraint search.
MAX_ELEM = 128

#: A derived width beyond this is not a width. sub_1440A7CCC comes out of
#: solve_reader as ('fixed', 11565661) -- 11 MB for one field -- because it
#: is not a reader at all and a `mov r8d, <big imm>` in it got taken for a
#: read size. It is inert today only because reads_stream() says False for
#: it so it contributes nothing to any sum, but a model that absurd should
#: never be produced, let alone returned to a caller that might use it. No
#: single record in the install is remotely near this, so the bound costs
#: nothing real and turns a silent nonsense value into a refusal.
MAX_READER_BYTES = 1 << 20

#: Instructions after which control does not fall through. Used both to
#: delimit basic blocks and to REFUSE to attribute a stream read to a field
#: across one: when a field's error block is OUTLINED, what physically
#: precedes it is some other block's tail, usually an epilogue.
_UNCOND_END = frozenset({"ret", "jmp", "int3", "ud2"})

#: An escape hatch for readers stage 1 genuinely cannot derive: a model
#: read off the disassembly by hand, kept SEPARATE from the automatic ones
#: so it is always visible which is which. An unmarked hand constant is how
#: a schema stops being trustworthy.
#:
#: Currently EMPTY, and that is the part worth recording. It held
#: sub_141071DE0 = 13 + n, which I added because stage 1 refused it.
#: Bounding the disassembly to the function's own end (see ``body``) made
#: stage 1 derive that reader by itself, along with 34 others -- so the hand
#: model was never filling a gap in the method, it was covering for a bug in
#: my scan.
#:
#: Kept as a mechanism, kept empty, and kept with this note: the first thing
#: to suspect about a reader stage 1 cannot derive is stage 1.
#:
#: Any entry added here is still held to the tiling gate, and addresses are
#: BUILD-SPECIFIC -- on another build a stale entry simply fails to match a
#: callee, which is the safe direction.
HAND_VERIFIED: dict[int, tuple[str, int]] = {}


def _cs():
    from capstone import CS_ARCH_X86, CS_MODE_64, Cs
    md = Cs(CS_ARCH_X86, CS_MODE_64)
    md.detail = True
    return md


class Frame:
    """Canonical stack-slot identity: an offset from the function's entry rsp.

    Needed because the SAME slot is reached through different registers at
    different points in a function. sub_1412A8A50 stores its element count
    via ``lea rdx, [r11 + 8]`` and then bounds its loop with
    ``cmp dword ptr [rsp + 0xa0], ...``. Those look unrelated until you
    notice ``r11`` was set to rsp on entry and rsp has since moved by five
    pushes (0x28) plus ``sub rsp, 0x70``: rsp+0xa0 == entry+8 == r11+8. One
    slot, two spellings. Keying on (register, displacement) instead treats
    them as two and loses the link between the count and the loop bound.

    rsp is tracked only through the PROLOGUE and then frozen. These
    functions set their frame up once and address locals at a constant
    offset; the only later rsp moves are epilogues, and a linear sweep
    cannot tell an epilogue from fall-through code. Applying
    sub_14129A5F0's early-return ``add rsp, 0x38 / pop / pop`` to the main
    path shifted every later slot by 0x48.
    """

    def __init__(self, md):
        self.md = md
        self.rsp = 0                      # rsp relative to function entry
        self.base: dict[str, int] = {}    # reg -> entry-relative value
        self.zero: set[str] = set()       # regs known to hold 0
        self.frozen = False               # prologue over -> rsp is fixed

    @staticmethod
    def q(r: str) -> str:
        """Normalise a 32-bit register name to its 64-bit counterpart."""
        if r.startswith("e"):
            return "r" + r[1:]
        if r.startswith("r") and r.endswith("d"):
            return r[:-1]
        return r

    def is_zero(self, reg: str) -> bool:
        return self.q(reg) in {self.q(z) for z in self.zero}

    def slot(self, op) -> int | None:
        """Entry-relative offset of a stack memory operand, else None."""
        m = op.mem
        if m.index != 0:
            return None                   # indexed: not a fixed slot
        base = self.md.reg_name(m.base) if m.base else None
        if base is None:
            return None
        if base == "rsp":
            return self.rsp + m.disp
        if base in self.base:
            return self.base[base] + m.disp
        return None

    def step(self, i):
        from capstone import CS_OP_IMM, CS_OP_MEM, CS_OP_REG
        o = i.operands
        m = i.mnemonic
        dst = i.op_str.split(",")[0].strip()
        if m == "call" or m.startswith("j"):
            self.frozen = True
            return
        if m in ("push", "pop") or (
                m in ("sub", "add") and len(o) == 2 and dst == "rsp"
                and o[1].type == CS_OP_IMM):
            if self.frozen:
                return                    # epilogue noise
            if m == "push":
                self.rsp -= 8
            elif m == "pop":
                self.rsp += 8
            else:
                self.rsp += (-o[1].imm if m == "sub" else o[1].imm)
            return
        if m == "mov" and len(o) == 2 and o[0].type == CS_OP_REG:
            src = i.op_str.split(",", 1)[1].strip()
            if src == "rsp":
                self.base[dst] = self.rsp
            elif o[1].type == CS_OP_IMM and o[1].imm == 0:
                self.zero.add(dst)
                self.base.pop(dst, None)
            else:
                self.zero.discard(dst)
                self.base.pop(dst, None)
        elif m == "lea" and len(o) == 2 and o[0].type == CS_OP_REG:
            s = self.slot(o[1]) if o[1].type == CS_OP_MEM else None
            if s is None:
                self.base.pop(dst, None)
            else:
                self.base[dst] = s
            self.zero.discard(dst)
        elif m == "xor" and len(o) == 2 and o[0].type == CS_OP_REG:
            b = i.op_str.split(",", 1)[1].strip()
            if self.q(dst) == self.q(b):
                self.zero.add(dst)
            else:
                self.zero.discard(dst)


class Deriver:
    def __init__(self, game_dir: Path):
        from extract_field_order_win import find_field_strings, find_lea_xrefs, open_image
        self.game = game_dir
        self.md = _cs()
        self.img = open_image(game_dir / "bin64" / "CrimsonDesert.exe")
        strings = find_field_strings(self.img)
        xrefs = find_lea_xrefs(self.img, {s.va for s in strings})
        self.by_cls: dict[str, list[tuple[str, int]]] = {}
        for s in strings:
            for lea in xrefs.get(s.va, []):
                self.by_cls.setdefault(s.cls, []).append((s.fld, lea))
        self.model: dict[int, tuple[str, int]] = {}
        self.elem: dict[int, int] = {}
        self._memo: dict[int, tuple[str, int] | None] = {}
        self._reads: dict[int, bool] = {}
        self._parts: dict[int, list] = {}
        self._pd = None                   # .pdata function extents, lazy

    # ── stage 1: static reader models ────────────────────────────────────

    def body(self, va: int, window: int = 600) -> list:
        """Instructions of the function at ``va``, STOPPING at its end.

        This is not a nicety. Disassembling a fixed window runs off the end
        of a short function into whatever follows it, and then attributes
        the neighbour's loops and calls to this reader. That is how
        sub_1412656D0 -- literally ``read(4)`` then a tail-call thunk --
        came out as "contains a loop, refused".

        Worse than the missed win: bytes from an unrelated function could
        contribute a sized read and produce a WRONG width, which the tiling
        gate would then have to catch. Better not to generate it.

        The end is the first ``ret`` that no forward branch jumps past. A
        ``ret`` before the furthest branch target is just one arm of the
        function, not its end.
        """
        from capstone import CS_OP_IMM
        off = self.img.va_to_off(va)
        if off is None:
            return []
        out = []
        furthest = va
        for i in self.md.disasm(self.img.data[off:off + window], va):
            out.append(i)
            o = i.operands
            if (i.mnemonic.startswith("j") and len(o) == 1
                    and o[0].type == CS_OP_IMM and o[0].imm > furthest):
                furthest = o[0].imm
            if i.mnemonic in ("ret", "int3") and i.address >= furthest:
                break
        return out

    def reads_stream(self, va: int, depth: int = 0,
                     seen: set[int] | None = None) -> bool:
        """Does this function touch the byte stream at all, transitively?

        The distinction this draws is the one that matters most in stage 1.
        A function that performs no stream read consumes ZERO bytes no
        matter what else it does -- including looping. Allocators, string
        builders and hash-lookup helpers all loop, and treating a loop as
        automatically undecidable let them refuse their callers.

        sub_141071DE0 is the case that exposed it: it reads 1 byte, 8
        bytes, then the CString reader, so it is plainly 13 + n. It was
        refused because a post-read helper it calls (sub_1410741B0, which
        just stores a pointer) contains a loop.

        A stream read is a SIZED vtable call: a width put in ``r8d``, then
        ``call qword ptr [rax + 8]``. The width is the load-bearing half.
        A bare ``call qword ptr [rax + 8]`` with no width is an ordinary
        virtual call on some other object, and counting it is what kept
        sub_141071DE0 refused -- its helper sub_1410741B0 makes exactly
        that call on ``r13 + 0x20``, a container rather than the stream.
        """
        from capstone import CS_OP_IMM, CS_OP_MEM, CS_OP_REG
        if va in self._reads:
            return self._reads[va]
        if seen is None:
            seen = set()
        if depth > 4 or va in seen:
            return False
        seen.add(va)
        off = self.img.va_to_off(va)
        if off is None:
            return False
        answer = False
        subs: list[int] = []
        pend = None
        rcx_is_stream = True          # rcx holds the stream on entry
        for i in self.body(va):
            o = i.operands
            dst = i.op_str.split(",")[0].strip()
            if (i.mnemonic in ("mov", "lea") and len(o) == 2
                    and o[0].type == CS_OP_REG and "r8" in dst):
                pend = o[1] if o[1].type in (CS_OP_IMM, CS_OP_MEM) else None
            elif i.mnemonic in ("mov", "lea") and len(o) == 2 and dst == "rcx":
                # rcx only still points at the stream if it came from a
                # register. Loaded from memory or a rip-relative global, it
                # is some other object -- which is how the hash-lookup
                # helper is called, and counting that as a stream read is
                # what refused sub_141297490 and the 10 tables behind it.
                rcx_is_stream = (i.mnemonic == "mov"
                                 and o[1].type == CS_OP_REG)
            elif i.mnemonic == "call" and len(o) == 1:
                if o[0].type == CS_OP_MEM:
                    if pend is not None:          # a SIZED read
                        answer = True
                        break
                elif o[0].type == CS_OP_IMM and rcx_is_stream:
                    subs.append(o[0].imm)
                pend = None
                rcx_is_stream = False             # rcx is clobbered by a call
        if not answer:
            answer = any(self.reads_stream(s, depth + 1, seen) for s in subs)
        self._reads[va] = answer
        return answer

    def fixed_loops(self, ins: list):
        """Loops whose trip count is a compile-time constant.

        Not every backward branch is a count-prefixed list. Some readers
        contain a fixed-length loop -- a vector of four, say -- and its
        consumption is fully determined:

            loop: mov r8d, 8 ; call [rax+8]      ; read 8
                  inc esi
                  cmp esi, 4                     ; trip count, an IMMEDIATE
                  jb  loop

        sub_1412656D0 is exactly that: two 4-byte reads, then four 8-byte
        reads. I first mis-read it as a plain 4-byte reader because my
        earlier scan stopped at the failure path's `ret`, and had I asserted
        that width it would have been simply wrong.

        Returns ``[(body_start, branch_addr, trip)]`` for the loops it can
        pin, or None if ANY backward branch cannot be pinned -- because a
        reader with one underivable loop is not derivable, and a partial
        answer here is a wrong width.

        Deliberately narrow: single counter, `cmp reg, imm` immediately
        governing the branch, unsigned/signed below. Anything else is
        refused rather than assumed.
        """
        from capstone import CS_OP_IMM, CS_OP_REG
        by_addr = {i.address: n for n, i in enumerate(ins)}
        loops = []
        for n, i in enumerate(ins):
            o = i.operands
            if not (i.mnemonic.startswith("j") and len(o) == 1
                    and o[0].type == CS_OP_IMM and o[0].imm < i.address):
                continue
            if i.mnemonic not in ("jb", "jl", "jbe", "jle", "jne"):
                return None
            target = o[0].imm
            if target not in by_addr:
                return None
            # the compare governing this branch: nearest preceding
            # `cmp <reg>, <imm>` within a few instructions
            trip = None
            for k in range(n - 1, max(-1, n - 5), -1):
                c = ins[k]
                co = c.operands
                if (c.mnemonic == "cmp" and len(co) == 2
                        and co[0].type == CS_OP_REG
                        and co[1].type == CS_OP_IMM):
                    trip = co[1].imm
                    if i.mnemonic in ("jbe", "jle"):
                        trip += 1          # inclusive bound
                    break
            if trip is None or not (1 <= trip <= 4096):
                return None
            loops.append((by_addr[target], n, trip))
        return loops

    # ── true function bounds, from .pdata unwind chains ──────────────────

    def _pdata(self):
        """Merged function extents from .pdata, following unwind chains.

        This replaces sweeping from ``leas[0] - SWEEP_PAD``, which was not
        merely imprecise. ``capstone.Cs.disasm`` is a GENERATOR that stops at
        the first byte sequence it cannot decode, so a sweep starting at an
        arbitrary byte desyncs into garbage, hits an invalid opcode, and
        TRUNCATES. On 15 tables it ended before the first field lea -- so no
        field could be found, and with ``ins`` near-empty the block map was
        empty too and field ORDER silently degraded to naive lea-address
        order, which is exactly what the hot-path ranking exists to avoid.

        .pdata does NOT enumerate functions, it enumerates UNWIND ranges. A
        function with outlined blocks has a primary RUNTIME_FUNCTION plus
        continuations flagged UNW_FLAG_CHAININFO (0x4), each carrying a
        trailing RUNTIME_FUNCTION pointing at its parent. Anchoring on the
        nearest BeginAddress bounds a FRAGMENT: measured on this build, 27 of
        112 candidate tables have their field leas spread across more than
        one fragment (up to 6, for QuestInfo). Following the chain to its
        ROOT and merging brings all of them under one function -- 0 tables
        left spanning multiple roots, 0 leas outside .pdata, and decoding the
        merged extents reaches EVERY field lea in 112 of 112 tables.

        UNWIND_INFO: byte0 = Version(3) | Flags(5), Flags & 4 == CHAININFO;
        byte2 = CountOfCodes; the chained RUNTIME_FUNCTION follows the
        unwind-code array, padded to an even count.
        """
        if self._pd is not None:
            return self._pd
        import pefile
        pe = pefile.PE(str(self.game / "bin64" / "CrimsonDesert.exe"),
                       fast_load=True)
        d = pe.OPTIONAL_HEADER.DATA_DIRECTORY[3]        # ENTRY_EXCEPTION
        rva, size = d.VirtualAddress, d.Size
        base = pe.OPTIONAL_HEADER.ImageBase
        pe.close()
        rf = []
        off = self.img.va_to_off(base + rva) if rva else None
        if off is not None:
            blob = self.img.data[off:off + size]
            for p in range(0, len(blob) - 11, 12):
                b, e, u = struct.unpack_from("<III", blob, p)
                if b and e > b:
                    rf.append((base + b, base + e, base + u if u else 0))
        rf.sort()
        by_begin = {b: (b, e, u) for b, e, u in rf}

        def parent(u):
            if not u:
                return None
            o = self.img.va_to_off(u)
            if o is None or o + 4 > len(self.img.data):
                return None
            if not ((self.img.data[o] >> 3) & 0x4):
                return None                   # not a chained fragment
            n = self.img.data[o + 2]          # CountOfCodes
            c = o + 4 + 2 * ((n + 1) & ~1)
            if c + 12 > len(self.img.data):
                return None
            pb, _pe, _pu = struct.unpack_from("<III", self.img.data, c)
            return base + pb

        root: dict[int, int] = {}

        def resolve(b, seen=frozenset()):
            if b in root:
                return root[b]
            if b in seen:
                return b                      # cycle guard
            ent = by_begin.get(b)
            p = parent(ent[2]) if ent else None
            r = (resolve(p, seen | {b})
                 if p is not None and p in by_begin else b)
            root[b] = r
            return r

        for b, _e, _u in rf:
            resolve(b)
        ext: dict[int, list[int]] = {}
        for b, e, _u in rf:
            r = root[b]
            cur = ext.setdefault(r, [b, e])
            cur[0] = min(cur[0], b)
            cur[1] = max(cur[1], e)
        self._pd = (rf, [b for b, _e, _u in rf], by_begin, root, ext)
        return self._pd

    def function_extent(self, va: int):
        """(lo, hi) of the whole function containing ``va``, or None."""
        _rf, starts, by_begin, root, ext = self._pdata()
        i = bisect_right(starts, va) - 1
        if i < 0:
            return None
        b, e, _u = by_begin[starts[i]]
        if not (b <= va < e):
            return None
        lo, hi = ext[root[starts[i]]]
        return (lo, hi)

    def sweep(self, leas: list[int]):
        """(instructions, addresses) covering every lea, boundary-true.

        Decodes each distinct enclosing FUNCTION once, from its real start,
        so every instruction boundary is genuine. Falls back to the old
        padded window only for a lea outside .pdata entirely (none on this
        build), because a missing field is worse than an imprecise one.
        """
        from extract_field_order_win import SWEEP_PAD
        spans, orphans = set(), []
        for a in leas:
            x = self.function_extent(a)
            if x is None:
                orphans.append(a)
            else:
                spans.add(x)
        ins = []
        for lo, hi in sorted(spans):
            off = self.img.va_to_off(lo)
            if off is not None:
                ins += list(self.md.disasm(
                    self.img.data[off:off + (hi - lo)], lo))
        for a in orphans:
            off = self.img.va_to_off(a - SWEEP_PAD)
            if off is not None:
                ins += list(self.md.disasm(
                    self.img.data[off:off + 2 * SWEEP_PAD], a - SWEEP_PAD))
        ins.sort(key=lambda i: i.address)
        out, seen = [], set()
        for i in ins:
            if i.address not in seen:
                seen.add(i.address)
                out.append(i)
        return out, [i.address for i in out]

    def closes_cycle(self, ins: list, branch: int, target_addr: int) -> bool:
        """Does this backward branch actually form a LOOP?

        A branch to an earlier address is not automatically a loop. It is a
        loop only if control can get from the target back to the branch --
        i.e. it closes a cycle. A backward ``jmp`` into a shared tail or
        error block goes backwards and never returns, and treating it as a
        loop refused the whole reader.

        sub_141280820 is the case: three separate backward ``jmp
        0x141280847`` at 0x1412808A4/BD/D6 converge on a shared tail. None
        of them can reach its own branch again, so none is a loop -- but the
        old test saw three and gave up. Guarding on reachability takes stage
        1 from 52 readers to 67 and gains materialmatchinfo.

        Walks the function's own instruction list, so it costs nothing
        beyond the body already disassembled.
        """
        from capstone import CS_OP_IMM
        idx = {i.address: n for n, i in enumerate(ins)}
        start = idx.get(target_addr)
        if start is None:
            return False                     # target outside this body

        def succ(n: int):
            i = ins[n]
            o = i.operands
            m = i.mnemonic
            if m in ("ret", "int3", "ud2"):
                return
            if m == "jmp":
                if len(o) == 1 and o[0].type == CS_OP_IMM:
                    t = idx.get(o[0].imm)
                    if t is not None:
                        yield t
                return                       # unconditional: no fallthrough
            if (m.startswith("j") and len(o) == 1
                    and o[0].type == CS_OP_IMM):
                t = idx.get(o[0].imm)
                if t is not None:
                    yield t
            if n + 1 < len(ins):
                yield n + 1

        seen = {start}
        stack = [start]
        while stack:
            n = stack.pop()
            if n == branch:
                return True
            for s in succ(n):
                if s not in seen:
                    seen.add(s)
                    stack.append(s)
        return False

    def solve_reader(self, va: int, depth: int = 0):
        """('fixed'|'str'|'strplus', base) or None when not derivable."""
        from capstone import CS_OP_IMM, CS_OP_MEM, CS_OP_REG
        if va in self._memo:
            return self._memo[va]
        if depth > 4:
            return None
        self._memo[va] = None                       # cycle guard
        off = self.img.va_to_off(va)
        if off is None:
            return None
        ins = self.body(va)
        if not ins:
            return None
        reads: list[int] = []
        subs: list[int] = []
        pend = None
        pend_var = False
        var_read = False      # STRICT: an unsized read at an actual call site
        var_seen = False      # PERMISSIVE: any non-immediate r8 write
        has_loop = False
        rcx_is_stream = True          # rcx holds the stream on entry
        back: list[tuple[int, int]] = []   # (branch index, target address)
        read_pos: list[int] = []           # instruction index of each read
        sub_pos: list[tuple[int, int]] = []   # (index, callee) of each subcall
        for n, i in enumerate(ins):
            o = i.operands
            _dst = i.op_str.split(",")[0].strip()
            if (i.mnemonic in ("mov", "lea") and len(o) == 2
                    and _dst == "rcx"):
                # See reads_stream: rcx loaded from memory or a global is
                # not our stream, so a call taking it cannot consume from
                # the stream.
                rcx_is_stream = (i.mnemonic == "mov"
                                 and o[1].type == CS_OP_REG)
            if (i.mnemonic in ("mov", "lea") and len(o) == 2
                    and o[0].type == CS_OP_REG
                    and "r8" in i.op_str.split(",")[0]):
                if o[1].type == CS_OP_IMM:
                    pend, pend_var = o[1].imm, False
                elif (i.mnemonic == "lea" and o[1].type == CS_OP_MEM
                      and o[1].mem.disp >= 0):
                    pend, pend_var = o[1].mem.disp, False   # lea r8d,[rsi+4]
                else:
                    # A width from a slot -- but only if a stream read
                    # actually follows. r8 is general-purpose and these
                    # functions use it for other work: `mov r8, qword ptr
                    # [rax + rcx*8]` in sub_141296960 is a hash-bucket
                    # pointer, and treating it as an unsized read refused
                    # that reader and blocked actionpointinfo's 28,958
                    # records. Decided at the call site below, which is the
                    # only place a missing width can matter.
                    pend, pend_var = None, True
                    var_seen = True
            if i.mnemonic == "call":
                if len(o) == 1 and o[0].type == CS_OP_MEM:
                    if pend is not None:
                        reads.append(pend)
                        read_pos.append(n)
                    elif pend_var:
                        # An unsized stream read: the width came from a slot.
                        # This is the CString shape (read 4, then read that
                        # many), which the ('str', 0) rule below depends on.
                        var_read = True
                        read_pos.append(n)
                    pend, pend_var = None, False
                elif len(o) == 1 and o[0].type == CS_OP_IMM:
                    if rcx_is_stream:
                        subs.append(o[0].imm)
                        sub_pos.append((n, o[0].imm))
                rcx_is_stream = False             # a call clobbers rcx
            if (i.mnemonic.startswith("j") and len(o) == 1
                    and o[0].type == CS_OP_IMM and va <= o[0].imm < i.address):
                back.append((n, o[0].imm))
        # A backward branch is only a LOOP if control can return to it. A
        # backward jmp into a shared tail or error block never does, and
        # counting those refused readers that contain no loop at all.
        addr_i = {x.address: k for k, x in enumerate(ins)}
        ranges = [(addr_i[t], n) for n, t in back
                  if t in addr_i and self.closes_cycle(ins, n, t)]

        def _in_loop(p: int) -> bool:
            return any(lo <= p <= hi for lo, hi in ranges)

        # ...and a loop only matters if a STREAM READ IS INSIDE IT. The old
        # test asked whether the function had a loop and whether it read
        # anywhere, which refused readers whose loop touches no stream at
        # all. sub_141296960 is one: a single 2-byte read, then a loop that
        # does a lookup and reads nothing. It consumes 2 bytes, and refusing
        # it blocked actionpointinfo -- 28,958 records, the largest table in
        # the install.
        has_loop = (any(_in_loop(p) for p in read_pos)
                    or any(_in_loop(p) for p, c in sub_pos
                           if self.reads_stream(c)))
        # A loop is only undecidable when its trip count is unknown -- that
        # is a count-prefixed list, and stage 2 derives its element width.
        # A loop whose bound is an immediate is fully determined, and a loop
        # in a helper that reads nothing consumes nothing either way.
        if has_loop:
            loops = self.fixed_loops(ins)
            if not loops:
                return None
            # Re-attribute: a sized read inside a fixed loop counts `trip`
            # times, and once outside. Anything nested is refused above by
            # fixed_loops returning None for a branch it cannot pin.
            in_loop = {}
            for start, end, trip in loops:
                for idx in range(start, end + 1):
                    if idx in in_loop:
                        return None            # nested / overlapping
                    in_loop[idx] = trip
            total = 0
            pend2 = None
            for idx, i2 in enumerate(ins):
                o2 = i2.operands
                d2 = i2.op_str.split(",")[0].strip()
                if (i2.mnemonic in ("mov", "lea") and len(o2) == 2
                        and o2[0].type == CS_OP_REG and "r8" in d2):
                    pend2 = o2[1].imm if o2[1].type == CS_OP_IMM else None
                elif i2.mnemonic == "call":
                    if (len(o2) == 1 and o2[0].type == CS_OP_MEM
                            and pend2 is not None):
                        total += pend2 * in_loop.get(idx, 1)
                    elif (len(o2) == 1 and o2[0].type == CS_OP_REG
                            and pend2 is not None):
                        # `call r9` -- an indirect sized read inside a loop
                        total += pend2 * in_loop.get(idx, 1)
                    pend2 = None
            if not total or total > MAX_READER_BYTES:
                return None
            got = ("fixed", total)
            self._memo[va] = got
            return got
        # CString detection deliberately uses the PERMISSIVE flag. The
        # strict call-site rule misses sub_1411A33C0, which reads its u32
        # length and then reads the bytes through `call qword ptr [rax+0x20]`
        # and `call r8` rather than the usual `call [rax+8]` -- so no
        # unsized read is visible at a recognised call site. The permissive
        # flag is safe HERE because the rule also requires the reader's
        # first read to be exactly 4 bytes, which is the length prefix; it
        # is not safe for the loop veto above, where it counted a
        # hash-bucket pointer load as a read.
        if (var_read or var_seen) and reads[:1] == [4]:
            self._memo[va] = ("str", 0)
            return self._memo[va]
        extra, kind = 0, "fixed"
        for s in subs:
            if not self.reads_stream(s):
                continue                            # consumes no stream bytes
            r = self.solve_reader(s, depth + 1)
            if r is None:
                return None
            if r[0] in ("str", "strplus"):
                kind = "strplus"
            extra += r[1]
        total = sum(reads) + extra
        if total > MAX_READER_BYTES:
            return None                   # not a width; see MAX_READER_BYTES
        got = ("fixed", 0) if (not reads and extra == 0) else (kind, total)
        self._memo[va] = got
        return got

    def element_reader(self, va: int):
        """The per-element callee of a count-prefixed list reader, or None.

        The first stream-reading immediate call in the body. ``list_element``
        already locates the loop; this exposes the callee so a caller can ask
        what ONE element is made of rather than only how wide it is.
        """
        from capstone import CS_OP_IMM
        for i in self.body(va, window=260):
            if (i.mnemonic == "call" and len(i.operands) == 1
                    and i.operands[0].type == CS_OP_IMM
                    and self.reads_stream(i.operands[0].imm)):
                return i.operands[0].imm
        return None

    def list_element(self, va: int, seen: frozenset | None = None):
        """What ONE element of a count-prefixed list reader consumes.

        ``('fixed', n)``    a constant n bytes per element
        ``('variable', s)`` the element contains a CString /
                            LocalizableString read, so it has NO constant
                            width; ``s`` describes what was found
        ``None``            not recognised as a dynamic-count list; no verdict

        Why this exists. 163 of stage 1's 167 refusals are LOOP_UNPINNED,
        and nearly all are CArray<T> readers: ``fixed_loops`` only pins a
        loop whose trip count is an immediate, and here the count is READ
        FROM THE STREAM, so the branch compares against a stack slot. The
        count being dynamic does not make the ELEMENT dynamic, and the loop
        body's own reads settle it without touching table data.

        The ``variable`` verdict is the load-bearing one. HouseInfo and
        FailMessageInfo shipped in v3.13 with a FIXED element width that
        stage 2 had fitted from data, and both were wrong -- the element
        ends in a string, and the constant only fitted because every string
        in that build was one length (12+34 == 46, 17+16 == 33). Both the
        fixed and the variable model tile 100% of records exactly, so the
        data cannot separate them. ``candidates()`` uses this to stop
        offering a fixed width for a reader whose element is variable,
        which is what makes unique-or-nothing mean anything: uniqueness in
        a candidate space that never held the right answer is not
        uniqueness.

        Validated against the eight readers stage 2 had already pinned from
        table data -- a wholly separate line of evidence. It reproduces
        five exactly, correctly reports the two above as variable, and
        returns None for the last rather than guessing. It fires on none of
        the four readers stage 2 proved are plain fixed-width.
        """
        from capstone import CS_OP_IMM, CS_OP_MEM, CS_OP_REG
        seen = frozenset() if seen is None else seen
        if va in seen or len(seen) > 4:
            return None                   # recursion guard
        ins = self.body(va)
        if not ins:
            return None
        idx = {i.address: n for n, i in enumerate(ins)}

        # Slot identity depends on the frame state AT that instruction, so
        # record it per index in one pass.
        fr = Frame(self.md)
        slots: list[dict] = []
        zeros: list[set] = []
        for i in ins:
            slots.append({k: fr.slot(op) for k, op in enumerate(i.operands)
                          if op.type == CS_OP_MEM})
            zeros.append(set(fr.zero))
            fr.step(i)

        # 1. the leading 4-byte read, and the slot it fills: that is the count
        count_slot = count_at = None
        pend = pend_dst = None
        for n, i in enumerate(ins):
            o = i.operands
            dst = i.op_str.split(",")[0].strip()
            if len(o) == 2 and o[0].type == CS_OP_REG and "r8" in dst:
                if i.mnemonic == "mov" and o[1].type == CS_OP_IMM:
                    pend = o[1].imm
                elif (i.mnemonic == "lea" and o[1].type == CS_OP_MEM
                      and o[1].mem.index == 0 and o[1].mem.disp >= 0):
                    # `lea r8d, [r14 + 4]` is width 4, but only if r14 really
                    # holds zero -- otherwise the width is whatever was in it.
                    b = (self.md.reg_name(o[1].mem.base)
                         if o[1].mem.base else None)
                    pend = (o[1].mem.disp if b and Frame.q(b) in
                            {Frame.q(z) for z in zeros[n]} else None)
                elif i.mnemonic == "mov" and o[1].type == CS_OP_REG:
                    # `mov r8d, ebp` -- MSVC hoists a literal width into a
                    # callee-saved register when a loop reads the same size
                    # every iteration, so the constant is defined once
                    # outside the loop. field_reads already resolves this
                    # through _const_reg; the loop-body scan did not, and
                    # dropped the reader to "no verdict" instead.
                    #
                    # stageinfo's _logoutMercenaryGroupInfoList
                    # (sub_141494DB0) is the worked example, GitHub #409:
                    # `mov ebp, 1` before the loop, `mov r8d, ebp` inside
                    # it, one byte per element. _const_reg keeps the
                    # unique-or-nothing rule, so a register written on more
                    # than one path still yields nothing.
                    pend = self._const_reg(ins, n, self.md.reg_name(o[1].reg))
                else:
                    pend = None
            elif (i.mnemonic == "lea" and len(o) == 2 and dst == "rdx"
                  and o[1].type == CS_OP_MEM):
                pend_dst = slots[n].get(1)
            elif (i.mnemonic == "call" and len(o) == 1
                  and o[0].type == CS_OP_MEM):
                if pend == 4 and pend_dst is not None:
                    count_slot, count_at = pend_dst, n
                    break
                pend = pend_dst = None
        if count_slot is None:
            return None

        # 2. a backward branch whose bound is that same slot
        lo = hi = None
        for n, i in enumerate(ins):
            o = i.operands
            if not (i.mnemonic in ("jb", "jl", "jbe", "jle", "jne")
                    and len(o) == 1 and o[0].type == CS_OP_IMM
                    and o[0].imm < i.address):
                continue
            ref = False
            for k in range(n - 1, max(-1, n - 4), -1):
                if ins[k].mnemonic != "cmp":
                    continue
                for ci, co in enumerate(ins[k].operands):
                    if (co.type == CS_OP_MEM
                            and slots[k].get(ci) == count_slot):
                        ref = True
                break
            if not ref:
                continue
            t = o[0].imm
            if t not in idx or idx[t] <= count_at:
                return None               # loop precedes the count read
            if lo is not None:
                return None               # two dynamic loops: no verdict
            lo, hi = idx[t], n
        if lo is None:
            return None

        # 3. what the loop body consumes per iteration, MEMBER BY MEMBER.
        # Keeping the sequence rather than only the sum is what lets a
        # variable-length element still be described: the format can express
        # `CArray<Substruct>`, so an element of u32 + u32 + CString is
        # representable even though its width is not constant. Recording the
        # parts here is what HouseInfo_RegionDataEntry was written by hand
        # from.
        parts: list[tuple[str, int]] = []
        pend = None
        for i in ins[lo:hi + 1]:
            o = i.operands
            dst = i.op_str.split(",")[0].strip()
            if (i.mnemonic in ("mov", "lea") and len(o) == 2
                    and o[0].type == CS_OP_REG and "r8" in dst):
                # Clear a pending width, but do NOT call this an unsized
                # read: r8 is general-purpose and the body uses it for other
                # work (`mov r8, qword ptr [rcx + rdx*8]` in sub_14129A5F0
                # is a hash-bucket pointer). A missing width only matters
                # where a stream call actually happens, below.
                #
                # A register source is resolved the same way field_reads
                # does it: MSVC hoists a constant width out of the loop
                # (`mov ebp, 1` outside, `mov r8d, ebp` inside), and
                # _const_reg returns it only when the register has exactly
                # one definition in the function. Without this the reader
                # got no verdict at all, which is how stageinfo's
                # _logoutMercenaryGroupInfoList sat unresolved (#409).
                if o[1].type == CS_OP_IMM:
                    pend = o[1].imm
                elif i.mnemonic == "mov" and o[1].type == CS_OP_REG:
                    pend = self._const_reg(
                        ins, ins.index(i), self.md.reg_name(o[1].reg))
                else:
                    pend = None
            elif i.mnemonic == "call" and len(o) == 1:
                if o[0].type == CS_OP_MEM:
                    if pend is None:
                        return None       # unsized stream call: no verdict
                    parts.append(("fixed", pend))
                elif o[0].type == CS_OP_IMM and self.reads_stream(o[0].imm):
                    sub = self.solve_reader(o[0].imm)
                    if sub is None:
                        # Before abstaining: a sub-reader that is ITSELF a
                        # count-prefixed list makes THIS element
                        # variable-length, because the inner count is read
                        # per element. Abstaining instead would leave
                        # candidates() free to offer a constant width, which
                        # is how royalsupply fitted ('list', 28): its element
                        # calls sub_1412A11E0, itself 4 + count*20, so 28
                        # only holds while every inner count is 1. That is
                        # the HouseInfo coincidence one level down.
                        if va not in seen:
                            inner = self.list_element(o[0].imm,
                                                      seen | {va})
                            if inner is not None:
                                parts.append(("nested", o[0].imm))
                                pend = None
                                continue
                        return None
                    parts.append(sub)
                pend = None
        if not parts:
            return None
        self._parts[va] = parts
        if all(p[0] == "fixed" for p in parts):
            total = sum(p[1] for p in parts)
            return ("fixed", total) if total > 0 else None
        bad = next(p for p in parts if p[0] != "fixed")
        fixed_before = sum(p[1] for p in parts if p[0] == "fixed")
        if bad[0] == "nested":
            why = (f"sub_{bad[1]:X} is itself a count-prefixed list, so this "
                   f"element varies with the inner count")
        else:
            why = (f"a {bad[0]}({bad[1]}) read, so the element is "
                   f"{fixed_before + bad[1] + 4} + n")
        return ("variable", why)

    def reader_parts(self, va: int, seen: frozenset | None = None):
        """A whole FIELD reader's members in program order, or None.

        The function-body analogue of ``element_parts``, for a field whose
        reader is a struct rather than a list. Same idea and the same
        acceptance rule: keep the sequence rather than only a sum, so a
        reader with no constant width is still describable as a substruct.

        Refuses, rather than guessing, on:
          * RECURSION -- actionpointinfo's ``_actionPoint`` (sub_141271150)
            and sub_1412B1220 call each other, so the field is a TREE. That
            is not expressible as a flat substruct at all, and it is the
            honest ceiling for that table's 28,958 records on this route.
          * an UNSIZED read that is not part of a length-prefixed pair.
          * any sub-reader that cannot itself be modelled or decomposed.

        The one pattern it does resolve beyond the obvious: a 4-byte read
        IMMEDIATELY followed by an unsized read is a length prefix and its
        bytes -- i.e. a CString. solve_reader only recognises that when the
        4-byte read is the reader's FIRST, which is why a struct with
        several fields before its string was refused. Measured: this rule
        takes decomposable tables from 17 to 20.

        Structure only. Nothing here says what a member MEANS.
        """
        from capstone import CS_OP_IMM, CS_OP_MEM, CS_OP_REG
        seen = frozenset() if seen is None else seen
        if va in seen or len(seen) > 6:
            return None
        ins = self.body(va)
        if not ins:
            return None
        # A loop that encloses a read makes this a list or worse; leave those
        # to list_element rather than flattening them here.
        addr_i = {x.address: k for k, x in enumerate(ins)}
        ranges = []
        for n, i in enumerate(ins):
            o = i.operands
            if (i.mnemonic.startswith("j") and len(o) == 1
                    and o[0].type == CS_OP_IMM and o[0].imm < i.address
                    and o[0].imm in addr_i
                    and self.closes_cycle(ins, n, o[0].imm)):
                ranges.append((addr_i[o[0].imm], n))

        out: list = []
        pend = None
        pend_var = False
        last_was_4 = False
        for n, i in enumerate(ins):
            o = i.operands
            dst = i.op_str.split(",")[0].strip()
            if (i.mnemonic in ("mov", "lea") and len(o) == 2
                    and o[0].type == CS_OP_REG and "r8" in dst):
                if o[1].type == CS_OP_IMM:
                    pend, pend_var = o[1].imm, False
                elif (i.mnemonic == "lea" and o[1].type == CS_OP_MEM
                      and o[1].mem.index == 0 and o[1].mem.disp >= 0):
                    pend, pend_var = o[1].mem.disp, False
                else:
                    pend, pend_var = None, True
                continue
            if i.mnemonic != "call" or len(o) != 1:
                continue
            in_loop = any(lo <= n <= hi for lo, hi in ranges)
            if o[0].type == CS_OP_MEM:
                if in_loop:
                    return None            # a read inside a loop: not flat
                if pend is not None:
                    out.append(("fixed", pend))
                    last_was_4 = (pend == 4)
                elif pend_var and last_was_4 and out:
                    out[-1] = ("str", 0)   # u32 length + that many bytes
                    last_was_4 = False
                else:
                    return None            # unsized read we cannot size
            elif o[0].type == CS_OP_IMM and self.reads_stream(o[0].imm):
                if in_loop:
                    return None
                sub = o[0].imm
                m = self.solve_reader(sub)
                if m is not None:
                    out.append(m)
                else:
                    el = self.list_element(sub)
                    if el is not None and el[0] == "fixed":
                        out.append(("list", el[1]))
                    else:
                        inner = self.reader_parts(sub, seen | {va})
                        if inner is None:
                            return None
                        out.append(("nest", tuple(inner)))
                last_was_4 = False
            pend, pend_var = None, False
        return out or None

    def element_parts(self, va: int):
        """The element's members in program order, or None.

        ``[('fixed', n) | ('str', 0) | ('strplus', 9) | ('nested', callee)]``

        This is what makes a variable-length element shippable rather than
        merely refused. ``CArray<[u8;N]>`` cannot describe an element whose
        width varies, but ``CArray<Substruct>`` can, and the format already
        supports it (see SUBSTRUCT_DEFS, e.g. StageInfo_Field608Entry =
        u32 + CString). HouseInfo_RegionDataEntry and FailMessageInfo_Entry
        were read off these same parts by hand; this returns them so the
        remaining tables do not need hand work.

        Structure only. It says nothing about what any member MEANS, so the
        names generated from it must stay out of ``_verified_fields``.
        """
        self.list_element(va)
        return self._parts.get(va)

    # ── field order + per-field read shape ───────────────────────────────

    def field_reads(self, cls: str):
        """[(name, 'fixed'|'call'|'?', width_or_callee)] in hot-path order."""
        from capstone import CS_OP_IMM, CS_OP_REG
        pairs = self.by_cls[cls]
        leas = sorted(a for _f, a in pairs)
        # Decode each enclosing FUNCTION from its real .pdata start rather
        # than a padded window, so every instruction boundary is genuine and
        # the decode cannot truncate before the fields. See sweep()/_pdata().
        ins, addrs = self.sweep(leas)
        if not addrs:
            return [(f, "?", None) for f, _a in pairs]
        inbound = collections.defaultdict(list)
        targets: set[int] = set()
        for i in ins:
            o = i.operands
            if (i.mnemonic.startswith("j") and len(o) == 1
                    and o[0].type == CS_OP_IMM):
                targets.add(o[0].imm)
                inbound[o[0].imm].append(i.address)
            # A conditional branch ends a block too, so the instruction
            # after it leads one. Without this leader an inline (not
            # outlined) error block folds into the block before the
            # field's own "jne ok" and gets keyed on that block's inbound
            # edges, which belong to something else entirely. That is what
            # put _percent after _gaugeTime on QuestGaugeInfo and
            # _isSequencerInterruptEvent before _eventDelayType on
            # AIEventTableInfo in the 2026-08-13 derivation. Same fix as
            # extract_field_order_win.sweep_bytes (GitHub #407).
            if i.mnemonic in _UNCOND_END or i.mnemonic.startswith("j"):
                targets.add(i.address + i.size)
        blocks = sorted(targets | {addrs[0]})
        set_addrs = set(addrs)

        def hot(lea: int):
            # bisect_RIGHT, matching Func.block_start_of in
            # extract_field_order_win.py (the implementation whose ordering
            # was validated against the hand-RE'd tables). An outlined error
            # block starts AT its own lea, so bisect_left lands one block
            # EARLIER and reads that block's inbound edges -- ranking the
            # field by where some other block is branched from. It is why
            # this function disagreed with the verified order on ItemInfo,
            # the very table extract_field_order_win.py cites as its proof.
            j = bisect_right(blocks, lea) - 1
            b = blocks[j] if j >= 0 else blocks[0]
            src = inbound.get(b)
            return (min(src), lea) if src else (lea, lea)

        def guard_index(lea: int) -> int | None:
            """Where to look back from for the read that guards this field.

            For a FALL-THROUGH field, that is the lea itself. For an OUTLINED
            error block it is NOT: the instructions physically before such a
            block belong to some other block, usually an epilogue, so walking
            back from the lea crosses a `ret` and picks up an unrelated
            read. AIEventTableInfo `_eventDelayType` is the case -- the
            lookback stepped over `ret` at 0x141271922 into
            `_isMustHandled`'s `mov r8d, 1` and reported width 1, where the
            sole branch into the block carries `mov r8d, 8`. Refereed by
            tiling: 8 tiles aieventtableinfo 988/988, 1 does not tile at all.

            So when the lea starts a block with inbound edges, look back from
            the EARLIEST branch into it -- the same hot-path source the
            ordering uses.
            """
            b = blocks[bisect_right(blocks, lea) - 1] if blocks else lea
            src = inbound.get(b)
            anchor = min(src) if (src and b == lea) else lea
            k = bisect_left(addrs, anchor)
            return k if k < len(addrs) and addrs[k] == anchor else None

        out = []
        for fld, lea in sorted(pairs, key=lambda p: hot(p[1])):
            if lea not in set_addrs:
                out.append((fld, "?", None))
                continue
            i = guard_index(lea)
            if i is None:
                out.append((fld, "?", None))
                continue
            j, call = i - 1, None
            while j >= 0 and i - j < 24:
                if ins[j].mnemonic in _UNCOND_END:
                    break            # a different block; refuse rather than
                    #                  borrow its read
                if ins[j].mnemonic == "call":
                    call = ins[j]
                    break
                j -= 1
            if call is None:
                out.append((fld, "?", None))
                continue
            o = call.operands
            if len(o) == 1 and o[0].type == CS_OP_IMM:
                out.append((fld, "call", o[0].imm))
                continue
            k, start = j - 1, max(0, j - 24)
            while k >= 0 and j - k < 24:
                if ins[k].mnemonic in _UNCOND_END:
                    start = k + 1     # do not take a width from another block
                    break
                if ins[k].mnemonic == "call":
                    start = k + 1
                    break
                k -= 1
            w = None
            src_reg = None
            for x in ins[start:j]:
                oo = x.operands
                if not (x.mnemonic == "mov" and len(oo) == 2
                        and oo[0].type == CS_OP_REG
                        and "r8" in x.op_str.split(",")[0]):
                    continue
                if oo[1].type == CS_OP_IMM:
                    w, src_reg = oo[1].imm, None
                elif oo[1].type == CS_OP_REG:
                    # `mov r8d, r15d` -- the width is in a register. MSVC
                    # hoists a literal into a callee-saved register when a
                    # deserialiser performs many reads of the same size, so
                    # the constant is defined once and reused. After the
                    # .pdata sweep this accounts for EVERY remaining '?':
                    # 86 fields, all `mov r8d, <reg>` (r15d 41, r12d 30,
                    # r13d 12, r14d 2, ebp 1), and zero misalignments.
                    w, src_reg = None, self.md.reg_name(oo[1].reg)
            if w is None and src_reg:
                w = self._const_reg(ins, j, src_reg)
            out.append((fld, "fixed", w) if w is not None
                       else (fld, "?", None))
        return out

    def _const_reg(self, ins: list, before: int, reg: str):
        """The constant in ``reg`` at instruction ``before``, or None.

        Resolved by UNIQUE DEFINITION: every write to the register anywhere
        in the function is collected, and a value is returned only when
        there is exactly one and it is `mov <reg>, <imm>`. Two definitions
        mean the value depends on a path this scan does not follow, and the
        honest answer is then "unknown" -- the same unique-or-nothing rule
        the widths and list elements are held to.

        Deliberately not a backward walk to the nearest assignment: the
        nearest one in ADDRESS order is not necessarily the one that
        reaches, and picking it would be exactly the kind of plausible
        guess this pipeline exists to refuse.
        """
        from capstone import CS_OP_IMM, CS_OP_REG
        q = Frame.q(reg)
        vals = set()
        for x in ins[:before]:
            o = x.operands
            if len(o) < 1 or o[0].type != CS_OP_REG:
                continue
            dst = x.op_str.split(",")[0].strip()
            if Frame.q(dst) != q:
                continue
            if (x.mnemonic == "mov" and len(o) == 2
                    and o[1].type == CS_OP_IMM):
                vals.add(o[1].imm)
            elif x.mnemonic == "xor" and len(o) == 2:
                b = x.op_str.split(",", 1)[1].strip()
                vals.add(0 if Frame.q(b) == q else None)
            else:
                vals.add(None)              # some other write: not a constant
        if len(vals) != 1:
            return None
        v = next(iter(vals))
        return v if isinstance(v, int) and 0 < v <= MAX_ELEM else None

    # ── the gate ─────────────────────────────────────────────────────────

    def _apply(self, body, p, end, spec):
        """Advance ``p`` past one reader described by ``spec``.

        ``spec`` is ``(kind, n)``. The families mirror the format's own
        grammar, documented in ``semantic/pabgb_types``:

          ('fixed', n)   n bytes                     -- primitive, [T;N]
          ('str',   n)   n bytes, then u32 len + len -- CString and
                                                       LocalizableString
                                                       (which is 13 + n)
          ('list',  n)   u32 count + count * n       -- CArray<fixed>
          ('slist', n)   u32 count + count*(n + u32 len + len)
                                                     -- CArray<CString>
                                                        when n == 0
          ('opt',   n)   u8 flag + (n bytes if set)  -- COptional<fixed>
        """
        kind, n = spec
        if kind == "plist":
            # u32 count, then `count` elements each described by an
            # ordered part sequence. Needed when the element reader holds
            # MORE THAN ONE string: ('slist', n) expresses exactly one, so
            # collapsing such an element keeps its fixed total and drops
            # every string after the first, the same mistake #418 fixed
            # one level up in the field readers.
            import struct as _s
            if p + 4 > end:
                return None
            cnt = _s.unpack_from("<I", body, p)[0]
            p += 4
            if cnt > 100_000:
                return None
            for _ in range(cnt):
                p = self._apply(body, p, end, ("parts", n))
                if p is None or p > end:
                    return None
            return p
        if kind == "parts":
            # An ordered sequence of members, used when a reader holds
            # MORE THAN ONE string. ('str', n) expresses exactly one, so
            # collapsing such a reader to it keeps the fixed total and
            # silently drops every string after the first.
            for part in n:
                p = self._apply(body, p, end, part)
                if p is None or p > end:
                    return None
            return p
        if kind == "fixed":
            return p + n
        if kind == "str":
            p += n
            if p + 4 > end:
                return None
            ln = struct.unpack_from("<I", body, p)[0]
            p += 4
            if ln > 2_000_000 or p + ln > end:
                return None
            return p + ln
        if kind == "list":
            if p + 4 > end:
                return None
            cnt = struct.unpack_from("<I", body, p)[0]
            p += 4
            if cnt > 100_000 or p + cnt * n > end:
                return None
            return p + cnt * n
        if kind == "slist":
            if p + 4 > end:
                return None
            cnt = struct.unpack_from("<I", body, p)[0]
            p += 4
            if cnt > 100_000:
                return None
            for _ in range(cnt):
                # ``n`` is the element's fixed prefix, the bytes before
                # its string. n == 0 is CArray<CString>; n > 0 is the
                # "strplus" element stage 1b reports as "16 + n".
                p += n
                if p + 4 > end:
                    return None
                ln = struct.unpack_from("<I", body, p)[0]
                p += 4
                if ln > 2_000_000 or p + ln > end:
                    return None
                p += ln
            return p
        if kind == "opt":
            if p + 1 > end:
                return None
            flag = body[p]
            p += 1
            if flag == 0:
                return p
            if flag != 1 or p + n > end:
                return None       # a real COptional flag is 0 or 1
            return p + n
        return None

    def _consume(self, body, p, end, fields, elem):
        """``elem`` maps an unresolved callee to a candidate ``(kind, n)``."""
        for name, kind, val in fields:
            if kind == "fixed":
                p += val
            elif kind == "call":
                m = self.model.get(val)
                if m is None:
                    m = elem.get(val)
                if m is None:
                    return None, name
                t, base = m
                # stage-1 models use 'strplus' for "base then a string"
                if t == "plist":
                    spec = ("plist", base)
                elif t == "parts":
                    spec = ("parts", base)
                elif t in ("str", "strplus"):
                    spec = ("str", base)
                elif t == "list":
                    spec = ("list", base)
                else:
                    spec = ("fixed", base)
                p = self._apply(body, p, end, spec)
                if p is None:
                    return None, name
            else:
                return None, name
            if p > end:
                return None, name
        return p, None

    def tiles(self, body, ent, key_size, fields, elem, subset=None) -> bool:
        """Exact tiling on EVERY record, or False. No partial credit.

        ``subset`` restricts which records are checked, used only to PRUNE
        a search: a width that tiles all records must also tile any subset,
        so a subset failure is a sound rejection. Every survivor is then
        re-checked against the full table before it is believed.
        """
        rng = subset if subset is not None else range(len(ent))
        for i in rng:
            o = ent[i][1]
            end = ent[i + 1][1] if i + 1 < len(ent) else len(body)
            if o + key_size + 4 > end:
                return False
            nlen = struct.unpack_from("<I", body, o + key_size)[0]
            got, _bad = self._consume(
                body, o + key_size + 4 + nlen, end, fields, elem)
            if got != end:
                return False
        return True

    def candidates(self, va: int | None = None):
        """Every reader shape stage 2 will try, in one flat list.

        Widened from list-only. A reader stage 1 refused is not necessarily
        a count-prefixed list -- it may be a fixed width or a string whose
        derivation failed for an unrelated reason (an unaccounted subcall,
        say). Only offering the list form meant those could never solve no
        matter how well constrained they were by the data.

        Unique-or-nothing now applies across the WHOLE space: if a fixed
        width and a list width both tile, the data cannot tell them apart
        and the answer is unknown.

        ...with one correction that the space itself was hiding. Every
        ``('list', n)`` here is a list of FIXED-width elements; there is no
        variable-element list shape. For a reader whose element ends in a
        string, the right answer was therefore never in the space, and
        stage 2 fitted the nearest constant instead -- reporting it as
        unique because nothing could contradict it. That is how HouseInfo
        got CArray<[u8;46]> and FailMessageInfo CArray<[u8;33]> into a
        shipped release: every element string in the measured build was one
        length, so 12+34 and 17+16 landed exactly on 100% of records.

        So when ``va`` is given and its element is statically variable, the
        list shapes are withheld. Stage 2 then finds nothing tiles and
        reports the reader unresolved, which is the correct outcome: the
        shape it needs is one this walker cannot express, and saying
        "unknown" is worth more than a constant that happens to fit today.

        And symmetrically: a reader that IS a count-prefixed list must never
        be offered a plain FIXED width. factionrelationgroup is the worked
        example, found by an adversarial review of this pipeline.
        sub_1412C3ED0 reads a u32 count and then 2 bytes per element, yet
        stage 2 pinned it ('fixed', 24) UNIQUELY on 5 records -- because the
        record's four counts happen to sum to 4 every time:

            Graymane        0,2,0,2      HostileCombat   0,3,0,1
            FriendlyCombat  0,2,0,2      NPC_Common      0,1,0,3
            Monster_Common  0,4,0,0

        so consumption is 16 + 2*4 = 24 in all five. Byte +12 of that field
        is two list elements in one record and a u32 list header in another.
        Perturbing the width does not catch it -- every +/-1 of 24 fails, so
        it looks firmly pinned -- and the five records agree with each other,
        so more of THIS table would not help either. Only the reader's own
        code separates the two readings.

        Stage 1b pins such a reader before any search, so this branch is the
        second line of defence for a caller that reaches search() without it.
        """
        el = self.list_element(va) if va is not None else None
        offer_fixed = el is None or el[0] != "fixed"
        offer_lists = el is None or el[0] != "variable"
        out = ([("fixed", n) for n in range(MAX_ELEM + 1)]
               if offer_fixed else [])
        if offer_lists:
            out += [("list", n) for n in range(1, MAX_ELEM + 1)]
        out += [("str", n) for n in range(33)]
        return out

    def search(self, body, ent, key_size, fields, unknown: list[int]):
        """Every shape assignment that tiles the WHOLE table.

        One or two unknowns. A spread of records prunes first and only
        survivors are verified in full -- sound, because a shape that tiles
        every record must also tile any subset, so a subset failure cannot
        be a false rejection.
        """
        n = len(ent)
        step = max(1, n // 24)
        probe = list(range(0, n, step))[:24] or [0]

        if len(unknown) == 1:
            c = unknown[0]
            cands = self.candidates(c)
            cheap = [x for x in cands
                     if self.tiles(body, ent, key_size, fields,
                                   {**self.elem, c: x}, probe)]
            return [(x,) for x in cheap
                    if self.tiles(body, ent, key_size, fields,
                                  {**self.elem, c: x})]

        a, b = unknown
        cands_a, cands_b = self.candidates(a), self.candidates(b)
        # Two unknowns over the full space would be ~300^2 tilings per
        # table. Prune each slot alone first: a shape that cannot tile the
        # probe for ANY partner cannot be part of a full solution.
        viable_a = [x for x in cands_a
                    if any(self.tiles(body, ent, key_size, fields,
                                      {**self.elem, a: x, b: y}, probe[:4])
                           for y in cands_b[::8])]
        viable_b = [y for y in cands_b
                    if any(self.tiles(body, ent, key_size, fields,
                                      {**self.elem, a: x, b: y}, probe[:4])
                           for x in viable_a[::8] or cands_a[::8])]
        cheap = [(x, y) for x in viable_a for y in viable_b
                 if self.tiles(body, ent, key_size, fields,
                               {**self.elem, a: x, b: y}, probe)]
        return [(x, y) for x, y in cheap
                if self.tiles(body, ent, key_size, fields,
                              {**self.elem, a: x, b: y})]


def load_table(game: Path, stem: str):
    from cdumm.engine.v2_to_format3 import _load_vanilla_table
    from cdumm.semantic.parser import parse_pabgh_index
    body = _load_vanilla_table(game, stem + ".pabgb")
    header = _load_vanilla_table(game, stem + ".pabgh")
    key_size, offsets = parse_pabgh_index(header, stem)
    return body, sorted(offsets.items(), key=lambda kv: kv[1]), key_size


def _carries_table_list(p: Path) -> bool:
    """Does this file actually carry the table list we need?

    Checked by CONTENT rather than by name. Matching on the filename is
    what broke here in the first place, and a candidate that opens as
    SQLite but has no ``data_tables`` is no use to us either.
    """
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            con.execute("SELECT 1 FROM data_tables LIMIT 1").fetchone()
            return True
        finally:
            con.close()
    except Exception:                                # noqa: BLE001
        return False


def find_index(explicit: Path | None = None) -> Path | None:
    """The game index CDUMM's Game Data tab builds, newest first.

    Also accepts the timestamped ``.bak`` copies, because a machine that
    has re-indexed since a patch keeps the previous one and the table LIST
    barely changes between builds -- it is only used to enumerate
    candidates, never to read bytes.

    The glob is ``game_index*``, not ``game_index*.sqlite*``. The live
    index is ``game_index.sqlite``, but the rotated copy is
    ``game_index.<timestamp>.bak`` -- the timestamp REPLACES the
    extension rather than following it, so the narrower glob could never
    match the very ``.bak`` files this docstring claimed to accept. The
    symptom was "No game index found under %LOCALAPPDATA%/cdumm" on a
    machine that had one sitting right there.

    Widening the glob means candidates must be validated, so each is
    opened and checked for the table it is wanted for.
    """
    import os
    if explicit:
        return explicit if explicit.exists() else None
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    d = Path(local) / "cdumm"
    if not d.is_dir():
        return None
    cands = sorted((p for p in d.glob("game_index*") if p.is_file()),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return next((p for p in cands if _carries_table_list(p)), None)


def class_for_stem(stem: str, lower: dict[str, str]) -> str | None:
    """Map a table's filename stem onto its reflection class name.

    Most tables are the class name lowercased, but a sizeable minority
    drop the ``Info`` suffix from the FILENAME while keeping it on the
    class: ``faction.pabgb`` <-> ``FactionInfo``, ``inventory.pabgb`` <->
    ``InventoryInfo``, ``board.pabgb`` <-> ``BoardInfo``. Twenty of this
    install's 135 tables are named that way.

    That mattered more than it looks. main() filtered candidates with
    ``s.lower() in lower`` and silently dropped the rest, so the deriver
    never looked at ONE of those tables -- they were not failing the
    tiling gate, they were never offered to it.

    EXACT matches only, ``stem`` then ``stem + "info"``. Deliberately no
    fuzzy matching: difflib cheerfully offers ``QuickSlotInfo`` for
    ``equipslotinfo`` and ``RegionInfo`` for ``reviepointinfo``, and
    binding a table to the wrong class would hand the walker a different
    type's field order -- which tiling might not even catch if the widths
    happened to add up.
    """
    for k in (stem.lower(), stem.lower() + "info"):
        if k in lower:
            return lower[k]
    return None


def table_stems(index: Path) -> list[str]:
    import sqlite3
    con = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
    return sorted({p.split("/")[-1][:-6] for (p,) in con.execute(
        "SELECT path FROM data_tables WHERE path LIKE '%.pabgb'")})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Derive + prove table layouts")
    ap.add_argument("--game-dir", required=True, type=Path)
    ap.add_argument("--tables", nargs="*",
                    help="table stems to try (default: every undecoded one "
                         "the game index knows)")
    ap.add_argument("--index", type=Path,
                    help="path to a cdumm game_index sqlite (default: the "
                         "newest under %%LOCALAPPDATA%%/cdumm)")
    args = ap.parse_args(argv)

    try:
        d = Deriver(args.game_dir)
    except ImportError as e:
        print(f"needs capstone + pefile: {e}")
        return 2

    if args.tables:
        stems = list(args.tables)
    else:
        index = find_index(args.index)
        if index is None:
            print("No game index found under %LOCALAPPDATA%/cdumm. Build one "
                  "from CDUMM's Game Data tab, pass --index, or name tables "
                  "with --tables.")
            return 2
        print(f"index: {index.name}")
        stems = [s for s in table_stems(index) if s.lower() not in DECODED]
    if not stems:
        print("No candidate tables.")
        return 2
    lower = {k.lower(): k for k in d.by_cls}
    resolved = {s: class_for_stem(s, lower) for s in stems}
    dropped = [s for s, c in resolved.items() if c is None]
    stems = [s for s in stems if resolved[s]]
    suffixed = sum(1 for s in stems if resolved[s].lower() != s.lower())
    print(f"candidate tables with a reflection class: {len(stems)} "
          f"({suffixed} matched via the dropped-Info filename convention)")
    if dropped:
        # Never silently. A table with no reflection class is outside the
        # static route entirely, and that is a fact about coverage worth
        # seeing rather than a row quietly missing from the report.
        print(f"no reflection class, cannot be derived statically: "
              f"{len(dropped)} ({', '.join(sorted(dropped))})")

    fields = {s: d.field_reads(resolved[s]) for s in stems}

    # stage 1
    callees = {v for f in fields.values() for _n, k, v in f if k == "call"}
    for c in callees:
        d._memo.clear()
        got = d.solve_reader(c)
        if got is not None:
            # solve_reader summarises a reader as "fixed bytes then ONE
            # string". reader_parts already holds the real member
            # sequence, so when that sequence has more than one string,
            # keep it: the summary is right about the fixed total and
            # wrong about everything after the first string.
            #
            # stageinfo's _sequencerDesc (sub_14228E9D0) is the worked
            # example, GitHub #409. solve_reader calls it strplus(37);
            # reader_parts shows 37 fixed bytes and SIX strings, so the
            # summary under-consumes by five length-prefixed strings on
            # any record where they are not all empty.
            parts = d.reader_parts(c)
            if parts and sum(1 for k, _v in parts if k == "str") > 1:
                d.model[c] = ("parts", tuple(parts))
            else:
                d.model[c] = got
    auto = len(d.model)
    hand = 0
    for c, m in HAND_VERIFIED.items():
        if c in callees and c not in d.model:
            d.model[c] = m
            hand += 1

    # stage 1b -- count-prefixed lists, from the loop BODY rather than from
    # table data. Nearly every refusal above is LOOP_UNPINNED, and nearly
    # every one of those is a CArray<T>: fixed_loops only pins a loop whose
    # trip count is an immediate, and here the count is read from the stream.
    # A dynamic count does not imply a dynamic element.
    static_lists = 0
    variable: dict[int, str] = {}
    for c in sorted(set(callees) - set(d.model)):
        el = d.list_element(c)
        if el is None:
            continue
        if el[0] == "fixed":
            d.elem[c] = ("list", el[1])
            static_lists += 1
        else:
            # A variable element still has a SHAPE, and list_element has
            # already computed it: "the element is 16 + n" means 12 bytes
            # then a CString. Pin it as ('slist', base) with that base,
            # taken from the loop body, NOT searched against table data.
            # Searching is what produced the HouseInfo and FailMessageInfo
            # constants; reading the base off the same loop body that
            # pins a fixed element is the same provenance stage 1b
            # already trusts one line above.
            #
            # Measured on stageinfo (GitHub #409): pinning all six of its
            # variable lists this way walks the record from field 13 to
            # field 23, which is exactly what three passes of proving
            # those readers by hand produced, one at a time.
            # An element whose reader holds MORE THAN ONE string cannot be
            # described by ('slist', n), which expresses exactly one. Same
            # mistake as #418 one level down: the collapsed form keeps the
            # element's fixed total and drops every string after the first.
            # stageinfo's _executeTargetStageList (sub_141493A00) is the
            # worked example: its element reader sub_14145B100 reads TWO
            # strings plus 12 fixed bytes, and solve_reader calls it
            # strplus(12). Pinning the part sequence instead carried the
            # record walk from field 23 to field 24 (GitHub #409).
            er = d.element_reader(c)
            ep = d.reader_parts(er) if er is not None else None
            if ep and sum(1 for _k, _v in ep if _k == "str") > 1:
                d.elem[c] = ("plist", tuple(ep))
                static_lists += 1
                variable[c] = el[1]
                continue
            m_el = re.search(r"element is (\d+) \+ n", el[1] or "")
            if m_el:
                d.elem[c] = ("slist", int(m_el.group(1)) - 4)
                static_lists += 1
            variable[c] = el[1]
    print(f"sub-readers: {auto} solved statically, {hand} hand-verified, "
          f"{static_lists} count-prefixed lists pinned from the loop body "
          f"(of {len(callees)} used as field readers)")
    if variable:
        # Worth printing loudly. A fixed width fitted for one of these is
        # exactly the HouseInfo / FailMessageInfo bug: it can tile 100% of
        # records and still be wrong, because the strings in one build happen
        # to share a length.
        # The shape IS expressible, and saying otherwise sends the next
        # reader off to build support that already exists. `_apply` carries
        # it as ('slist', n), and semantic/pabgb_types.consume_bytes walks
        # CArray<T>, [T;N] and named sub-structs, so a per-element fixed
        # prefix followed by a string has a descriptor. What is missing is
        # that `candidates` never OFFERS an slist, so stage 2 cannot fit
        # one. Measured 2026-09-08: adding slist to the candidate space
        # changes nothing on its own (0 tables proven either way over
        # stageinfo plus 11 other undecoded tables), because the tables
        # that need it are blocked earlier -- stageinfo, for one, is
        # missing three fields the exe reads (GitHub #409). Offering the
        # shape is necessary but not sufficient; do not ship it alone.
        print(f"list elements that are VARIABLE-length ({len(variable)}) -- "
              f"no CONSTANT width may be fitted for these; each is pinned "
              f"as ('slist', n) from its own loop body instead:")
        for c, s in sorted(variable.items()):
            print(f"   sub_{c:X}  {s}")

    loaded = {}
    for s in stems:
        try:
            body, ent, ks = load_table(args.game_dir, s)
        except Exception as e:                       # noqa: BLE001
            # A table absent from this build, or one whose index this
            # parser cannot frame, is simply not a candidate.
            print(f"  skip {s}: {type(e).__name__}")
            continue
        if ent:
            loaded[s] = (body, ent, ks)
    print(f"tables loaded from the install: {len(loaded)}")

    # stage 2, to a fixed point
    proven: dict[str, int] = {}
    ambiguous: dict[str, list[int]] = {}   # keyed, so a table
    # re-tested on a later round is not reported twice
    #: reader -> the shapes still permitted by EVERY table seen so far.
    #: Intersecting across tables is the main source of new answers once
    #: single-table uniqueness runs out.
    viable: dict[int, set] = {}
    crossed: set[int] = set()   # readers pinned only by intersection
    for rnd in range(1, 9):
        added = 0
        for stem in sorted(loaded):
            if stem in proven:
                continue
            f = [x for x in fields[stem] if x[0] not in HEADER_FIELDS]
            if any(k == "?" for _n, k, _v in f):
                continue
            body, ent, ks = loaded[stem]
            unknown = sorted({v for _n, k, v in f if k == "call"
                              and v not in d.model and v not in d.elem})
            if not unknown:
                if d.tiles(body, ent, ks, f, d.elem):
                    proven[stem] = len(ent)
                continue
            if len(unknown) > 2:
                continue

            sols = d.search(body, ent, ks, f, unknown)
            if not sols:
                continue

            # A reader is determined when EVERY tiling solution agrees on
            # it. With two unknowns that is strictly more informative than
            # all-or-nothing: one reader can be pinned while the other is
            # left unconstrained (its lists are empty in this table, so
            # nothing here can speak to it). Extracting the pinned one is
            # safe; assuming the free one is not.
            pinned = {}
            for i, c in enumerate(unknown):
                vals = {s[i] for s in sols}
                # Record what THIS table permits for this reader, and
                # intersect it with what every other table permits. A
                # reader ambiguous here is often pinned by another table
                # that uses it, and intersecting constraints is sound in a
                # way that picking a favourite is not.
                prev = viable.get(c)
                viable[c] = vals if prev is None else (prev & vals)
                if len(vals) == 1:
                    pinned[c] = next(iter(vals))
                elif len(viable[c]) == 1:
                    pinned[c] = next(iter(viable[c]))
                    crossed.add(c)
            note = ""
            if len(unknown) == 2:
                note = (", both of 2 unknowns" if len(pinned) == 2
                        else ", 1 of 2 unknowns (the other is unconstrained "
                             "here)")
            for c, e in pinned.items():
                if c not in d.elem:
                    d.elem[c] = e
                    added += 1
                    shape = (f"4 + count*{e[1]}" if e[0] == "list"
                             else f"{e[1]} + str" if e[0] == "str"
                             else f"{e[1]} bytes")
                    how = (" [by intersecting tables]" if c in crossed
                           else "")
                    print(f"  round {rnd}: sub_{c:X} = {shape}"
                          f"   (from {stem}, {len(ent)} records{note}){how}")
            if len(pinned) == len(unknown):
                proven[stem] = len(ent)
            else:
                free = [c for c in unknown if c not in pinned]
                ambiguous[stem] = sorted({str(s[unknown.index(free[0])])
                                          for s in sols})
        if not added:
            break

    # A table reported AMBIGUOUS in an early round is often PROVEN in a
    # later one, once another table pins the reader that was free here.
    # `ambiguous` was never cleared when that happened, so the report
    # listed the same table under both headings and contradicted itself --
    # dialogvoiceinfo showed up as 519 records proven AND as "130 shapes
    # tile". Proven wins: it is the later, stronger result.
    ambiguous = {s: v for s, v in ambiguous.items() if s not in proven}

    print()
    print(f"PROVEN -- exact tiling on 100% of records: {len(proven)} tables, "
          f"{sum(proven.values()):,} records")
    for s, n in sorted(proven.items()):
        print(f"   {s:<32} {n:>7} records")
    if ambiguous:
        print(f"\nAMBIGUOUS (more than one width tiles -> reported, NOT "
              f"guessed): {len(ambiguous)}")
        for s, hits in sorted(ambiguous.items())[:10]:
            print(f"   {s:<32} {len(hits)} shapes tile, e.g. {hits[:4]}")
    print("\nReminder: tiling proves STRUCTURE, not semantics. Gate every "
          "new field to _verified_fields after a value spot-check before "
          "anything writes to it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
