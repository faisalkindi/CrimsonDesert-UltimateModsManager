"""Diagnostic: compare CDUMM snapshot hashes against live disk bytes."""
import hashlib
import os
import sqlite3
import sys
try:
    import xxhash
    HAS_XX = True
except ImportError:
    HAS_XX = False

GAME_DIR = r"E:\SteamLibrary\steamapps\common\Crimson Desert"
DB = os.path.join(GAME_DIR, "CDMods", "cdumm.db")

con = sqlite3.connect(DB)
print("Hash lib:", "xxh3" if HAS_XX else "sha256")

mismatches = []
matches = 0
checked = 0
for fp, stored_hash, stored_size in con.execute(
        "SELECT file_path, file_hash, file_size FROM snapshots"):
    live = os.path.join(GAME_DIR, fp.replace("/", os.sep))
    if not os.path.exists(live):
        mismatches.append((fp, "missing", stored_size, 0))
        continue
    checked += 1
    live_size = os.path.getsize(live)
    if live_size != stored_size:
        mismatches.append((fp, "size", stored_size, live_size))
        continue
    algo = "sha256" if len(stored_hash) == 64 else "xxh3"
    h = xxhash.xxh3_128() if algo == "xxh3" else hashlib.sha256()
    with open(live, "rb") as f:
        while True:
            c = f.read(8 * 1024 * 1024)
            if not c:
                break
            h.update(c)
    live_hash = h.hexdigest()
    if live_hash != stored_hash:
        mismatches.append((fp, "hash_diff",
                           stored_hash[:12], live_hash[:12]))
    else:
        matches += 1

print(f"Checked: {checked}, matches: {matches}, "
      f"mismatches: {len(mismatches)}")
print()
for m in mismatches[:50]:
    print(" ", m)
