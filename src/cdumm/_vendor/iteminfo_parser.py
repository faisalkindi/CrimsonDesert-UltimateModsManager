"""
iteminfo_parser.py
==================
Standalone parser/writer for Crimson Desert iteminfo.pabgb (game update 2026-05).

Field names follow the interface defined in the crimson_rs TypedDict stubs
bundled with CDUMM (NattKh / Potter420, MPL-2.0). This is an independent
clean-room implementation derived from binary analysis of game data.

SPDX-License-Identifier: MIT

Copyright (c) 2026 CiscoStu

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

Usage:
    python iteminfo_parser.py --key 1001250
"""
from __future__ import annotations

import argparse
import lz4.block
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_PASSIVE_LIST_OFFSET_FROM_BASE = 161
_ENCHANT_STAT_ANCHOR = 1000002
COOLTIME_1S_MS = 1000


def _parse_pabgh(hdr: bytes) -> dict[int, int]:
    pos = 0
    count = struct.unpack_from('<I', hdr, pos)[0]
    pos += 4
    if count * 8 > len(hdr):
        count = struct.unpack_from('<H', hdr, 0)[0]
        pos = 2
    idx: dict[int, int] = {}
    for _ in range(count):
        key    = struct.unpack_from('<I', hdr, pos)[0]; pos += 4
        offset = struct.unpack_from('<I', hdr, pos)[0]; pos += 4
        idx[key] = offset
    return idx


def _rebuild_pabgh(idx: dict[int, int]) -> bytes:
    buf = bytearray()
    buf += struct.pack('<I', len(idx))
    for key, offset in sorted(idx.items(), key=lambda kv: kv[1]):
        buf += struct.pack('<II', key, offset)
    return bytes(buf)


@dataclass
class PassiveSkill:
    skill: int
    level: int

@dataclass
class EquipBuff:
    buff:  int
    level: int

@dataclass
class ItemEntry:
    key:               int
    name:              str
    raw:               bytes
    passive_count_off: int
    enchant_stat_off:  Optional[int]
    enchant_buffs_off: int
    cooltime_off:      Optional[int]
    gimmick_info_off:  Optional[int]
    equip_type_info_off: Optional[int]
    item_type_off:     Optional[int]
    passive_skills:    list[PassiveSkill]
    equip_buffs:       list[EquipBuff]
    cooltime_ms:       Optional[int]
    gimmick_info:      Optional[int]
    equip_type_info:   Optional[int]
    item_type_val:     Optional[int]


def _parse_entry(raw: bytes) -> ItemEntry:
    key      = struct.unpack_from('<I', raw, 0)[0]
    name_len = struct.unpack_from('<I', raw, 4)[0]
    name     = raw[8:8 + name_len].decode('utf-8', errors='replace')
    base     = 8 + name_len

    pc_off = base + _PASSIVE_LIST_OFFSET_FROM_BASE
    count  = struct.unpack_from('<I', raw, pc_off)[0]
    skills: list[PassiveSkill] = []
    pos = pc_off + 4
    for _ in range(count):
        sk = struct.unpack_from('<I', raw, pos)[0]; pos += 4
        lv = struct.unpack_from('<I', raw, pos)[0]; pos += 4
        skills.append(PassiveSkill(sk, lv))

    passive_list_end = pc_off + 4 + count * 8
    stat_count_off = None
    stat_count     = 0
    anchor_pos     = None
    for scan in range(passive_list_end + 4, len(raw) - 8):
        v = struct.unpack_from('<I', raw, scan)[0]
        if 1_000_000 <= v <= 1_100_000:
            c = struct.unpack_from('<I', raw, scan - 4)[0]
            if 1 <= c <= 20:
                tentative = scan + c * 12
                if tentative + 4 >= len(raw): continue
                slv_c = struct.unpack_from('<I', raw, tentative)[0]
                tentative += 4 + slv_c * 5
                if tentative + 4 >= len(raw) or slv_c > 50: continue
                bp_c = struct.unpack_from('<I', raw, tentative)[0]
                tentative += 4 + bp_c * 20
                if tentative + 4 > len(raw) or bp_c > 50: continue
                stat_count_off = scan - 4
                stat_count     = c
                anchor_pos     = scan
                break
    if anchor_pos is None:
        raise ValueError(f'enchant stat anchor not found in entry for key={key}')

    cur = anchor_pos + stat_count * 12
    slv_count = struct.unpack_from('<I', raw, cur)[0]; cur += 4 + slv_count * 5
    bp_count  = struct.unpack_from('<I', raw, cur)[0]; cur += 4 + bp_count * 20

    eb_off   = cur
    eb_count = struct.unpack_from('<I', raw, eb_off)[0]
    buffs: list[EquipBuff] = []
    pos = eb_off + 4
    for _ in range(eb_count):
        bk = struct.unpack_from('<I', raw, pos)[0]; pos += 4
        bl = struct.unpack_from('<I', raw, pos)[0]; pos += 4
        buffs.append(EquipBuff(bk, bl))

    ct_off: Optional[int] = None
    ct_val: Optional[int] = None
    eb_end = eb_off + 4 + eb_count * 8
    for scan in range(eb_end, len(raw) - 23):
        v = struct.unpack_from('<q', raw, scan)[0]
        # (v & 0xFF) != 0 skips byte-shifted false triples created when the
        # byte immediately before the real triple is 0x00
        if 1_000 <= v <= 36_000_000 and (v & 0xFF) != 0:
            if struct.unpack_from('<q', raw, scan + 8)[0] == v == struct.unpack_from('<q', raw, scan + 16)[0]:
                ct_off = scan
                ct_val = v
                break

    eti_off: Optional[int] = None
    eti_val: Optional[int] = None
    # step=4 from base+44 avoids the byte-overlap false positive at base+41
    for scan in range(base + 44, min(base + 60, len(raw) - 3), 4):
        v = struct.unpack_from('<I', raw, scan)[0]
        if v > 2_000_000_000:
            eti_off = scan
            eti_val = v
            break

    # item_type sits at base+95 in the post-2026-05 format (base+76 first insertion +19)
    it_off = base + 95
    it_val = raw[it_off] if it_off < len(raw) else None

    gi_off: Optional[int] = None
    gi_val: Optional[int] = None
    enchant_region_start = stat_count_off if stat_count_off is not None else len(raw)
    for scan in range(passive_list_end, enchant_region_start - 3, 4):
        v = struct.unpack_from('<I', raw, scan)[0]
        if 1_000_000 <= v <= 100_000_000:
            pre  = struct.unpack_from('<I', raw, scan - 4)[0] if scan >= 4 else 1
            post = struct.unpack_from('<I', raw, scan + 4)[0] if scan + 8 <= len(raw) else 1
            if pre == 0 and post == 0:
                gi_off = scan
                gi_val = v
                break

    return ItemEntry(
        key=key, name=name, raw=raw,
        passive_count_off=pc_off,
        enchant_stat_off=stat_count_off,
        enchant_buffs_off=eb_off,
        cooltime_off=ct_off,
        gimmick_info_off=gi_off,
        equip_type_info_off=eti_off,
        item_type_off=it_off,
        passive_skills=skills,
        equip_buffs=buffs,
        cooltime_ms=ct_val,
        gimmick_info=gi_val,
        equip_type_info=eti_val,
        item_type_val=it_val,
    )


def _write_entry(entry: ItemEntry,
                 new_passives:        Optional[list[PassiveSkill]] = None,
                 new_buffs:           Optional[list[EquipBuff]]    = None,
                 new_cooltime:        Optional[int]                = None,
                 new_gimmick_info:    Optional[int]                = None,
                 new_equip_type_info: Optional[int]                = None,
                 new_item_type:       Optional[int]                = None) -> bytes:
    data = bytearray(entry.raw)

    if new_passives is not None:
        old_count = struct.unpack_from('<I', data, entry.passive_count_off)[0]
        old_bytes = 4 + old_count * 8
        new_block = struct.pack('<I', len(new_passives))
        for ps in new_passives:
            new_block += struct.pack('<II', ps.skill, ps.level)
        data[entry.passive_count_off : entry.passive_count_off + old_bytes] = new_block

    if new_buffs is not None:
        raw_now = bytes(data)
        anchor_pos = raw_now.find(struct.pack('<I', _ENCHANT_STAT_ANCHOR))
        stat_count = struct.unpack_from('<I', raw_now, anchor_pos - 4)[0]
        cur = anchor_pos + stat_count * 12
        slv = struct.unpack_from('<I', raw_now, cur)[0]; cur += 4 + slv * 5
        bp  = struct.unpack_from('<I', raw_now, cur)[0]; cur += 4 + bp  * 20
        eb_off       = cur
        old_eb_count = struct.unpack_from('<I', raw_now, eb_off)[0]
        old_bytes    = 4 + old_eb_count * 8
        new_block    = struct.pack('<I', len(new_buffs))
        for b in new_buffs:
            new_block += struct.pack('<II', b.buff, b.level)
        data[eb_off : eb_off + old_bytes] = new_block

    if new_cooltime is not None and entry.cooltime_off is not None:
        for _i in range(3):   # cooltime is stored in three consecutive identical i64 slots
            struct.pack_into('<q', data, entry.cooltime_off + _i * 8, new_cooltime)

    if new_gimmick_info is not None and entry.gimmick_info_off is not None:
        struct.pack_into('<I', data, entry.gimmick_info_off, new_gimmick_info)

    if new_equip_type_info is not None and entry.equip_type_info is not None:
        old_b = struct.pack('<I', entry.equip_type_info)
        new_b = struct.pack('<I', new_equip_type_info)
        idx = bytes(data).find(old_b)
        if 0 <= idx < 200:
            data[idx:idx + 4] = bytearray(new_b)

    if new_item_type is not None and entry.item_type_off is not None:
        data[entry.item_type_off] = new_item_type & 0xFF

    return bytes(data)


class IteminfoFile:
    def __init__(self, body: bytes, hdr: bytes):
        self.body    = bytearray(body)
        self.hdr     = hdr
        self.idx     = _parse_pabgh(hdr)
        self._sorted = sorted(self.idx.items(), key=lambda kv: kv[1])

    @classmethod
    def from_game(cls, game_dir: str | Path) -> 'IteminfoFile':
        sys.path.insert(0, str(Path(__file__).parent.parent.parent /
                               'CrimsonDesert-UltimateModsManager-master' / 'src'))
        from cdumm.archive.paz_parse import parse_pamt
        paz_dir = Path(game_dir) / '0008'
        entries = parse_pamt(str(paz_dir / '0.pamt'))

        def _load(e):
            with open(e.paz_file, 'rb') as f:
                f.seek(e.offset); raw = f.read(e.comp_size)
            if e.comp_size != e.orig_size:
                return lz4.block.decompress(raw, uncompressed_size=e.orig_size)
            return raw

        body_e  = next(e for e in entries if 'iteminfo.pabgb' in e.path)
        pabgh_e = next(e for e in entries if 'iteminfo.pabgh' in e.path)
        return cls(lz4.block.decompress(_load(body_e), uncompressed_size=body_e.orig_size),
                   _load(pabgh_e))

    @classmethod
    def from_files(cls, pabgb_path: str | Path, pabgh_path: str | Path) -> 'IteminfoFile':
        return cls(Path(pabgb_path).read_bytes(), Path(pabgh_path).read_bytes())

    def _entry_bounds(self, key: int) -> tuple[int, int]:
        offsets = [o for _, o in self._sorted]
        start   = self.idx[key]
        i       = offsets.index(start)
        end     = offsets[i + 1] if i + 1 < len(offsets) else len(self.body)
        return start, end

    def read(self, key: int) -> ItemEntry:
        start, end = self._entry_bounds(key)
        return _parse_entry(bytes(self.body[start:end]))

    def write(self, key: int,
              passives:        Optional[list[PassiveSkill]] = None,
              buffs:           Optional[list[EquipBuff]]    = None,
              cooltime:        Optional[int]                = None,
              gimmick_info:    Optional[int]                = None,
              equip_type_info: Optional[int]                = None,
              item_type:       Optional[int]                = None) -> int:
        start, end = self._entry_bounds(key)
        old_entry  = _parse_entry(bytes(self.body[start:end]))
        new_raw    = _write_entry(old_entry, passives, buffs, cooltime,
                                  gimmick_info, equip_type_info, item_type)
        delta = len(new_raw) - (end - start)
        self.body[start:end] = new_raw
        if delta != 0:
            for k, off in self.idx.items():
                if off > start:
                    self.idx[k] = off + delta
            self._sorted = sorted(self.idx.items(), key=lambda kv: kv[1])
        return delta

    def get_body(self) -> bytes:
        return bytes(self.body)

    def get_pabgh(self) -> bytes:
        return _rebuild_pabgh(self.idx)


def _cli():
    ap = argparse.ArgumentParser(description='Read or patch iteminfo.pabgb for a given item key.')
    ap.add_argument('--game-dir', default=r'E:\UltraFastSteam\steamapps\common\Crimson Desert')
    ap.add_argument('--pabgb', help='Path to iteminfo.pabgb')
    ap.add_argument('--pabgh', help='Path to iteminfo.pabgh')
    ap.add_argument('--key', type=int, required=True)
    args = ap.parse_args()

    if args.pabgb and args.pabgh:
        f = IteminfoFile.from_files(args.pabgb, args.pabgh)
    else:
        sys.path.insert(0, str(Path(__file__).parent.parent.parent /
                               'CrimsonDesert-UltimateModsManager-master' / 'src'))
        f = IteminfoFile.from_game(args.game_dir)

    e = f.read(args.key)
    print(f'key={e.key}  name={e.name!r}')
    print(f'  passive_skills : {[(p.skill, p.level) for p in e.passive_skills]}')
    print(f'  equip_buffs    : {[(b.buff, b.level) for b in e.equip_buffs]}')
    print(f'  cooltime_ms    : {e.cooltime_ms}')
    print(f'  equip_type_info: {e.equip_type_info}')
    print(f'  item_type      : {e.item_type_val}')
    print(f'  gimmick_info   : {e.gimmick_info}')


if __name__ == '__main__':
    _cli()
