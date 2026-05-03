"""Bug E from Nexus 2026-05-03 (unqltango): import fails with
[WinError 112] There is not enough space on the disk when
installing Kliff Female Voice (a large audio mod). User has 3TB
free on D:/ where the game lives, but Python's
tempfile.TemporaryDirectory() defaults to %TEMP% which lives on
C:/ — and C:/ doesn't have room for the uncompressed extract.

Fix: route extraction through a helper that lands the staging
dir under game_dir/CDMods/_import_staging/<uuid>/ so it lives on
the game drive (where the mod will end up anyway).
"""
from __future__ import annotations
from pathlib import Path

import pytest


def test_import_staging_dir_lives_on_game_drive(tmp_path: Path):
    """The helper must produce a directory under game_dir, NOT under
    the system temp dir. That keeps extraction on the same drive as
    the game install, which is where the mod's final files will end
    up via game_dir/CDMods/sources and game_dir/CDMods/deltas."""
    from cdumm.engine.import_handler import import_staging_dir

    game_dir = tmp_path / "game"
    game_dir.mkdir()

    with import_staging_dir(game_dir) as staging:
        staging_path = Path(staging)
        assert staging_path.exists(), (
            "Helper must create the staging directory")
        # Path resolution: staging must be inside game_dir/CDMods/
        # not in %TEMP% (which is wherever Python's gettempdir()
        # resolves on the current platform).
        try:
            staging_path.resolve().relative_to(
                (game_dir / "CDMods").resolve())
        except ValueError:
            pytest.fail(
                f"Staging dir {staging_path} is NOT under "
                f"{game_dir / 'CDMods'}. Bug E: large voice mods "
                f"hit WinError 112 because extraction lands on the "
                f"system temp drive instead of the game drive."
            )

    # After context exit, dir is cleaned up
    assert not staging_path.exists(), (
        f"Staging dir {staging_path} should be removed after context exit"
    )


def test_import_staging_dir_unique_per_call(tmp_path: Path):
    """Multiple concurrent imports must get distinct staging dirs."""
    from cdumm.engine.import_handler import import_staging_dir

    game_dir = tmp_path / "game"
    game_dir.mkdir()

    paths = []
    with import_staging_dir(game_dir) as a:
        with import_staging_dir(game_dir) as b:
            paths.append(Path(a))
            paths.append(Path(b))
            assert paths[0] != paths[1], (
                "Two concurrent staging dirs must be unique")


def test_no_default_tempfile_calls_in_import_handler():
    """Source-text guard: every tempfile.TemporaryDirectory() call in
    import_handler must pass dir= (or use the import_staging_dir
    helper). A bare TemporaryDirectory() defaults to %TEMP% on C:/.
    """
    src_path = (
        Path(__file__).resolve().parent.parent
        / "src" / "cdumm" / "engine" / "import_handler.py"
    )
    text = src_path.read_text(encoding="utf-8")

    import re
    # Find every tempfile.TemporaryDirectory(...) call
    matches = list(re.finditer(
        r"tempfile\.TemporaryDirectory\((?P<args>[^)]*)\)", text))

    # Exclude the helper itself and its docstring — the helper is the
    # ONE legitimate place that wraps tempfile.TemporaryDirectory as a
    # fallback when game_dir/CDMods is unwritable.
    helper_start = text.find("def import_staging_dir")
    helper_end_offset = text.find("\ndef ", helper_start + 1)
    bare_calls = [
        (m.start(), m.group(0)) for m in matches
        if m.group("args").strip() == ""
        and m.start() > helper_end_offset
    ]

    assert not bare_calls, (
        f"import_handler.py has {len(bare_calls)} bare "
        f"tempfile.TemporaryDirectory() calls. Each defaults to "
        f"%TEMP% on C:/, which fails with WinError 112 for large "
        f"mods on systems where C: is near full but the game drive "
        f"has space. Use import_staging_dir(game_dir) helper or "
        f"pass an explicit dir= argument that lives under "
        f"game_dir/CDMods/. Found at offsets: {bare_calls!r}"
    )
