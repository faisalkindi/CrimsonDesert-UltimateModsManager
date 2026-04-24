"""Standalone hash check — computes xxh3_128 of 5 sample files
directly off disk. No CDUMM involvement. These are the Steam-truth
reference hashes for the snapshot-vs-revert bug investigation.
"""
import os
import xxhash

GAME_DIR = r"E:\SteamLibrary\steamapps\common\Crimson Desert"
FILES = [
    "meta/0.papgt",
    "meta/0.pathc",
    "0008/0.pamt",
    "0012/2.paz",
    "0000/15.paz",
]

def hash_file(path: str) -> tuple[str, int]:
    h = xxhash.xxh3_128()
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        while True:
            chunk = f.read(8 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest(), size

print(f"{'FILE':<20} {'SIZE':>14}  HASH")
for rel in FILES:
    full = os.path.join(GAME_DIR, rel.replace("/", os.sep))
    if not os.path.exists(full):
        print(f"{rel:<20} MISSING")
        continue
    h, sz = hash_file(full)
    print(f"{rel:<20} {sz:>14}  {h}")
