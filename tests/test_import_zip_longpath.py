"""Long-path zip import (#191, falobos76).

DMM's Mod Builder exports each mod as ``ModName/ModName.json`` inside
``ModName.zip`` — the json nested in a folder of the *same* ~100-char
name. Extracted under the staging path (``<game>/CDMods/_import_staging/
<32-hex>/``) that doubled name pushes the full path past Windows' 260-char
MAX_PATH, so ``extractall`` fails with ``[Errno 2] No such file or
directory`` while importing the bare json works. ``_extractall_collapsing_
wrapper`` collapses the redundant single top-level wrapper so paths stay
short (and normal) for the whole downstream pipeline.
"""
from __future__ import annotations

import io
import os
import zipfile

from cdumm.engine.import_handler import _extractall_collapsing_wrapper


def _zip(entries: list[tuple[str, bytes]]) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in entries:
            z.writestr(name, content)
    buf.seek(0)
    return zipfile.ZipFile(buf)


def _files(root) -> list[str]:
    out = []
    for base, _dirs, files in os.walk(root):
        for f in files:
            out.append(
                os.path.relpath(os.path.join(base, f), root).replace("\\", "/"))
    return sorted(out)


def test_dmm_wrapper_is_collapsed(tmp_path):
    """Name/Name.json -> Name.json at the staging root (the #191 case)."""
    name = "BrizMod-DMM_MaxStacks-InfDurability-Gathering5x-Trust100x"
    z = _zip([(f"{name}/", b""), (f"{name}/{name}.json", b'{"format":3}')])
    _extractall_collapsing_wrapper(z, str(tmp_path), name)  # zip stem == wrapper
    assert _files(tmp_path) == [f"{name}.json"]
    # content preserved
    assert (tmp_path / f"{name}.json").read_bytes() == b'{"format":3}'


def test_collapse_shortens_path_below_260(tmp_path):
    """The fix's whole point: a real-length DMM name that overflows 260
    when doubled fits once the wrapper is collapsed. Machine-independent —
    asserts on path lengths, not on extractall raising (dev machines with
    LongPathsEnabled=1 wouldn't raise; the user's packaged exe does)."""
    name = ("BrizMod-DMM_MaxStacks-InfDurability-"
            "IcreaseWildLifeSpawn-NoDragonAndCompanionCooldown-"
            "Gathering5x-Trust100x")  # ~91 chars, like the real mod
    user_base = (r"D:\GIOCHI\Steam\steamapps\common\Crimson Desert"
                 r"\CDMods\_import_staging\f39c0d893efc4e948d1980ee8a28c8e6")
    uncollapsed = len(user_base) + 1 + len(name) + 1 + len(name) + len(".json")
    collapsed = len(user_base) + 1 + len(name) + len(".json")
    assert uncollapsed > 260, "precondition: doubled name overflows MAX_PATH"
    assert collapsed < 260, "collapsed path must fit under MAX_PATH"


def test_paz_dir_is_never_collapsed(tmp_path):
    """A 4-digit PAZ dir (0036/) carries meaning — must survive intact."""
    z = _zip([("0036/data.paz", b"x"), ("0036/0.pamt", b"y")])
    # Even if the archive is (bizarrely) named "0036", the PAZ-dir guard
    # must keep the numbered dir intact.
    _extractall_collapsing_wrapper(z, str(tmp_path), "0036")
    assert _files(tmp_path) == ["0036/0.pamt", "0036/data.paz"]


def test_wrapper_around_paz_dir_collapses_to_the_paz_dir(tmp_path):
    """MyMod.zip -> MyMod/0036/x -> 0036/x: the self-named wrapper goes,
    the PAZ dir stays."""
    z = _zip([("MyMod/0036/data.paz", b"x"), ("MyMod/0036/0.pamt", b"y")])
    _extractall_collapsing_wrapper(z, str(tmp_path), "MyMod")
    assert _files(tmp_path) == ["0036/0.pamt", "0036/data.paz"]


def test_generic_wrapper_not_matching_archive_name_is_preserved(tmp_path):
    """DarkUI.zip with a ui/ folder: the wrapper name (ui) != the archive
    stem (DarkUI), so ui/ is meaningful and MUST survive — its members
    target ui/menu.css etc. (regression: an earlier version collapsed any
    single top-level folder and broke test_partial_patch_integration)."""
    z = _zip([("ui/menu.css.patch", b"x"), ("ui/menu.html.patch", b"y")])
    _extractall_collapsing_wrapper(z, str(tmp_path), "DarkUI")
    assert _files(tmp_path) == ["ui/menu.css.patch", "ui/menu.html.patch"]


def test_bare_top_level_file_is_unchanged(tmp_path):
    """A json already at the zip root has no wrapper to collapse."""
    z = _zip([("mod.json", b"{}")])
    _extractall_collapsing_wrapper(z, str(tmp_path), "mod")
    assert _files(tmp_path) == ["mod.json"]


def test_multiple_top_level_dirs_are_not_collapsed(tmp_path):
    """Ambiguous shape (2+ top-level entries) extracts verbatim."""
    z = _zip([("A/x.json", b"{}"), ("B/y.json", b"{}")])
    _extractall_collapsing_wrapper(z, str(tmp_path), "pack")
    assert _files(tmp_path) == ["A/x.json", "B/y.json"]


def test_no_archive_stem_collapses_nothing(tmp_path):
    """Without a stem to match against, the wrapper is never stripped."""
    z = _zip([("Mod/a.json", b"{}"), ("Mod/b.json", b"{}")])
    _extractall_collapsing_wrapper(z, str(tmp_path))
    assert _files(tmp_path) == ["Mod/a.json", "Mod/b.json"]


def test_path_traversal_member_is_skipped(tmp_path):
    """A member that resolves outside dest must never be written."""
    z = _zip([("Mod/ok.json", b"{}"), ("Mod/../../evil.json", b"x")])
    _extractall_collapsing_wrapper(z, str(tmp_path), "Mod")
    # only the safe member lands; nothing escapes tmp_path
    assert _files(tmp_path) == ["ok.json"]
    assert not (tmp_path.parent / "evil.json").exists()
