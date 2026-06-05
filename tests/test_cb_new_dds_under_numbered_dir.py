"""GitHub #193 (RoGreat, second bug in the thread): a Crimson Browser
mod that ships BRAND-NEW DDS preview textures under a numbered PAZ
directory (``files/0012/ui/texture/image/customizeimage/*.dds``) had
every one of those textures silently dropped with::

    WARNING crimson_browser_handler: CB mod: no PAMT entry for
    'ui/texture/image/customizeimage/barber_oongka_cd_phm_00_beard_00_0028.dds'
    in dir 0012, skipping

The images never loaded in-game even though the import "succeeded".

Root cause (verified against the live game index with cdpaz: those
``customizeimage`` / ``barber_*`` paths exist in NO vanilla PAMT, so
they are new textures, not replacements):

  - New DDS textures are meant to flow through ``leftover_dds`` ->
    ``build_texture_overlay`` (fresh PAZ + PAMT + PATHC). The handler
    comment names exactly this case: "CB mods that ship BOTH known
    XMLs AND new DDS preview textures (e.g. Barber Unlocked, Character
    Creator)".
  - But a file placed under a NUMBERED dir (``files/0012/...``) goes
    straight into ``files_by_dir`` and never touches the ``unresolved``
    bucket that feeds ``leftover_dds``. In the per-dir loop it has no
    PAMT entry (it's new), so all three match attempts fail and it is
    dropped instead of becoming a texture overlay.

Fix: in the per-dir loop, a ``.dds`` with no PAMT entry is a new
texture — route it to ``leftover_dds`` instead of skipping, so the
numbered-dir path and the unresolved path converge on the same overlay
pipeline.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from cdumm.archive.paz_parse import PazEntry
from cdumm.engine.crimson_browser_handler import convert_to_paz_mod


def _write(path: Path, content: bytes = b"") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


# A new barber customize-image texture, shipped under a numbered dir.
_NEW_DDS_REL = (
    "ui/texture/image/customizeimage/"
    "barber_oongka_cd_phm_00_beard_00_0028.dds"
)


def _make_barber_mod(tmp_path: Path) -> dict:
    """A CB mod whose only payload is one NEW DDS under files/0012/."""
    mod = tmp_path / "Barber Oongka"
    _write(mod / "files" / "0012" / _NEW_DDS_REL,
           b"DDS " + b"\x00" * 124 + b"PIXELS")
    return {"_base_dir": mod, "files_dir": "files", "id": "barber-oongka"}


def _unrelated_pamt_entry(paz_dir: str) -> PazEntry:
    """A single vanilla entry that does NOT match the new barber DDS
    (different basename), so all three match attempts in the per-dir
    loop fail for the new texture."""
    return PazEntry(
        path="ui/cd_customize_empty_image.dds",
        paz_file=str(Path(paz_dir) / "0.paz"),
        offset=0,
        comp_size=16,
        orig_size=16,
        flags=0,
        paz_index=0,
    )


def test_new_dds_under_numbered_dir_reaches_texture_overlay(
    tmp_path: Path,
) -> None:
    """#193: the new barber DDS must reach build_texture_overlay rather
    than being silently skipped as a missing PAMT replacement."""
    manifest = _make_barber_mod(tmp_path)

    game_dir = tmp_path / "game"
    # The per-dir branch needs a parseable PAMT to exist for dir 0012.
    _write(game_dir / "0012" / "0.pamt", b"PAMT")
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    seen_dds: list[str] = []

    def fake_parse_pamt(pamt_path, paz_dir=None):
        # Vanilla dir 0012 contains only unrelated entries — the new
        # barber DDS is genuinely absent (matches the live game index).
        return [_unrelated_pamt_entry(paz_dir or str(game_dir / "0012"))]

    def fake_build_overlay(dds_entries, game_dir_arg, work_dir_arg):
        for virtual_path, _src in dds_entries:
            seen_dds.append(virtual_path)
        return ("0036", len(dds_entries))

    with patch(
        "cdumm.engine.crimson_browser_handler.parse_pamt",
        side_effect=fake_parse_pamt,
    ), patch(
        "cdumm.engine.texture_mod_handler.build_texture_overlay",
        side_effect=fake_build_overlay,
    ):
        result = convert_to_paz_mod(manifest, game_dir, work_dir)

    assert result is not None
    assert _NEW_DDS_REL in seen_dds, (
        "The new barber customize DDS shipped under files/0012/ must be "
        "routed through the texture-overlay pipeline, not dropped as a "
        f"missing-PAMT-entry skip. build_texture_overlay saw: {seen_dds}")


def test_non_dds_under_numbered_dir_without_entry_is_still_skipped(
    tmp_path: Path,
) -> None:
    """Guard: only .dds files are new-texture candidates. A non-texture
    file under a numbered dir with no PAMT entry is genuinely
    unresolvable and must NOT be force-fed to the texture overlay."""
    mod = tmp_path / "Bad Mod"
    _write(mod / "files" / "0012" / "ui" / "nonexistent_thing.xml",
           b"<root/>")
    manifest = {"_base_dir": mod, "files_dir": "files", "id": "bad"}

    game_dir = tmp_path / "game"
    _write(game_dir / "0012" / "0.pamt", b"PAMT")
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    seen_dds: list[str] = []

    def fake_parse_pamt(pamt_path, paz_dir=None):
        return [_unrelated_pamt_entry(paz_dir or str(game_dir / "0012"))]

    def fake_build_overlay(dds_entries, game_dir_arg, work_dir_arg):
        for virtual_path, _src in dds_entries:
            seen_dds.append(virtual_path)
        return ("0036", len(dds_entries))

    with patch(
        "cdumm.engine.crimson_browser_handler.parse_pamt",
        side_effect=fake_parse_pamt,
    ), patch(
        "cdumm.engine.texture_mod_handler.build_texture_overlay",
        side_effect=fake_build_overlay,
    ):
        convert_to_paz_mod(manifest, game_dir, work_dir)

    assert seen_dds == [], (
        "A non-DDS file with no PAMT entry must stay skipped — only new "
        f"textures route to the overlay. Overlay saw: {seen_dds}")
