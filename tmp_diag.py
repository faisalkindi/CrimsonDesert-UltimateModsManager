import sys, struct, hashlib
sys.path.insert(0, r'C:\Users\faisa\Ai\Mods Dev\CrimsonDesert-Mods\CrimsonDesert-ModManager\src')
from pathlib import Path
from cdumm.archive.hashlittle import compute_pamt_hash, compute_papgt_hash

game_dir = Path(r'E:\SteamLibrary\steamapps\common\Crimson Desert')
vanilla_dir = game_dir / 'CDMods' / 'vanilla'

# 1. Check PAPGT backup validity
papgt_game = game_dir / 'meta' / '0.papgt'
papgt_vanilla = vanilla_dir / 'meta' / '0.papgt'

if papgt_game.exists():
    g = papgt_game.read_bytes()
    gh = struct.unpack_from('<I', g, 4)[0]
    gc = compute_papgt_hash(g)
    print(f'Game PAPGT: {len(g)} bytes, stored_hash=0x{gh:08X}, computed=0x{gc:08X}, valid={gh==gc}')

if papgt_vanilla.exists():
    v = papgt_vanilla.read_bytes()
    vh = struct.unpack_from('<I', v, 4)[0]
    vc = compute_papgt_hash(v)
    print(f'Vanilla PAPGT backup: {len(v)} bytes, stored_hash=0x{vh:08X}, computed=0x{vc:08X}, valid={vh==vc}')
    print(f'Game vs Vanilla PAPGT same: {g == v}')
    print(f'Game PAPGT size vs Vanilla: {len(g)} vs {len(v)}')
else:
    print('No vanilla PAPGT backup exists')

# 2. Check if vanilla backups match snapshot hashes
import sqlite3
db_path = Path(r'C:\Users\faisa\AppData\Local\cdumm\cdumm.db')
conn = sqlite3.connect(str(db_path))

print('\n--- Vanilla Backup vs Snapshot ---')
mismatches = 0
checked = 0
for backup in sorted(vanilla_dir.rglob('*')):
    if not backup.is_file() or backup.name.endswith('.vranges'):
        continue
    rel = str(backup.relative_to(vanilla_dir)).replace('\\', '/')
    snap = conn.execute('SELECT file_hash, file_size FROM snapshots WHERE file_path = ?', (rel,)).fetchone()
    if snap is None:
        print(f'  {rel}: NO SNAPSHOT ENTRY')
        continue

    snap_hash, snap_size = snap
    backup_size = backup.stat().st_size

    # Quick size check first
    if backup_size != snap_size:
        print(f'  MISMATCH {rel}: backup={backup_size} snapshot={snap_size} (SIZE DIFFERS)')
        mismatches += 1
    else:
        # Hash check
        h = hashlib.sha256()
        with open(backup, 'rb') as f:
            while True:
                chunk = f.read(8*1024*1024)
                if not chunk:
                    break
                h.update(chunk)
        backup_hash = h.hexdigest()
        if backup_hash != snap_hash:
            print(f'  MISMATCH {rel}: backup_hash={backup_hash[:16]}... snap_hash={snap_hash[:16]}... (CONTENT DIFFERS)')
            mismatches += 1
    checked += 1

print(f'\nChecked {checked} backups, {mismatches} mismatches')

# 3. List all enabled mods
print('\n--- Installed Mods ---')
mods = conn.execute('SELECT id, name, enabled, priority FROM mods ORDER BY priority').fetchall()
for mid, name, enabled, pri in mods:
    status = 'ON' if enabled else 'OFF'
    delta_count = conn.execute('SELECT COUNT(*) FROM mod_deltas WHERE mod_id = ?', (mid,)).fetchone()[0]
    is_new = conn.execute('SELECT COUNT(*) FROM mod_deltas WHERE mod_id = ? AND is_new = 1', (mid,)).fetchone()[0]
    print(f'  [{status}] #{mid} pri={pri} "{name}" deltas={delta_count} new_files={is_new}')

# 4. Check for orphan mod directories
print('\n--- Orphan Directories (0036+) ---')
for d in sorted(game_dir.iterdir()):
    if d.is_dir() and d.name.isdigit() and len(d.name) == 4 and int(d.name) >= 36:
        files = list(d.iterdir())
        print(f'  {d.name}: {len(files)} files ({", ".join(f.name for f in files[:5])})')

conn.close()
