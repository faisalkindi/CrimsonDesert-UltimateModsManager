"""BOM tolerance for mod-author JSONs and bounded sniffing
(audit findings 10 and 12).

Finding 12: the mutex-folder detector read JSONs with encoding="utf-8";
a UTF-8 BOM (common from Windows editors) made json.loads fail and the
folder silently lost mutex detection. All three reads now use utf-8-sig
like the F3 scanner.

Finding 10: detect_patch_file's probe used read_bytes()[:8192], pulling
whole files into memory before slicing; it now reads only 8 KB and
still detects JSON patch files larger than the probe via the gated
full-read fallback. BOM-prefixed patch files must also detect.
"""
from __future__ import annotations

import json
from pathlib import Path

BOM = b"\xef\xbb\xbf"


def _mutex_json(offsets: list[int]) -> str:
    return json.dumps({
        "name": "x",
        "patches": [{
            "game_file": "0008/0.paz",
            "changes": [{"offset": o, "new": "AA"} for o in offsets],
        }],
    })


def test_mutex_folder_detects_bom_prefixed_jsons(tmp_path: Path):
    from cdumm.engine.mutex_json_folder import detect_mutex_folder_jsons

    folder = tmp_path / "AbyssGears"
    folder.mkdir()
    for name in ("GearA.json", "GearB.json"):
        (folder / name).write_bytes(
            BOM + _mutex_json([100, 200]).encode("utf-8"))

    parsed = detect_mutex_folder_jsons(folder)
    assert parsed is not None, (
        "BOM-prefixed mutex JSONs must still be detected as a mutex "
        "set (utf-8-sig read)")
    assert len(parsed) == 2


def test_json_offsets_tolerates_bom(tmp_path: Path):
    from cdumm.engine.mutex_json_folder import json_offsets

    p = tmp_path / "mod.json"
    p.write_bytes(BOM + _mutex_json([42]).encode("utf-8"))
    assert json_offsets(p) == {("0008/0.paz", 42)}


def test_detect_patch_file_handles_bom_json(tmp_path: Path):
    from cdumm.engine.xml_patch_handler import detect_patch_file

    p = tmp_path / "thing.json"
    p.write_bytes(BOM + json.dumps({"operations": []}).encode("utf-8"))
    assert detect_patch_file(p) == "xml_patch"


def test_detect_patch_file_handles_json_larger_than_probe(
        tmp_path: Path):
    from cdumm.engine.xml_patch_handler import detect_patch_file

    ops = [{"op": "set", "xpath": f"/a/b[{i}]", "value": "x" * 50}
           for i in range(300)]
    body = json.dumps({"operations": ops})
    assert len(body) > 8192, "fixture must exceed the 8 KB probe"
    p = tmp_path / "big.json"
    p.write_text(body, encoding="utf-8")
    assert detect_patch_file(p) == "xml_patch"


def test_detect_patch_file_rejects_large_binary(tmp_path: Path):
    from cdumm.engine.xml_patch_handler import detect_patch_file

    p = tmp_path / "asset.bin"
    p.write_bytes(b"\x00\x01\x02\x03" * 8192)
    assert detect_patch_file(p) is None
