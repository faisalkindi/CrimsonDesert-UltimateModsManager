"""Pinned regression: ``/content/packages`` substring must not
match unrelated path segments like ``packagesource`` or
``packages_old`` that just happen to share the prefix.

Found via systematic-debugging sweep on the GitHub #74 fix.
"""
from __future__ import annotations

from pathlib import Path

from cdumm.storage.game_finder import is_xbox_install


def test_packagesource_not_detected_as_xbox(tmp_path):
    """A user with ``F:\\Games\\Foo\\Content\\packagesource`` is
    not on an Xbox install. The /content/packages prefix shouldn't
    match because ``packagesource`` isn't ``packages``."""
    game = tmp_path / "Content" / "packagesource"
    game.mkdir(parents=True)
    assert not is_xbox_install(game)


def test_packages_old_not_detected_as_xbox(tmp_path):
    game = tmp_path / "Content" / "packages_old"
    game.mkdir(parents=True)
    assert not is_xbox_install(game)


def test_packages_at_end_of_path_still_detected(tmp_path):
    """The original GitHub #74 case: install path ends in
    Content/packages with no trailing slash. Must still detect."""
    game = tmp_path / "Content" / "packages"
    game.mkdir(parents=True)
    assert is_xbox_install(game)


def test_packages_subdir_still_detected(tmp_path):
    """Standard Xbox layout has package family dirs under
    Content/packages. Must detect when game_dir is one of those."""
    game = (tmp_path / "Content" / "packages"
            / "PearlAbyss.CrimsonDesert_8wekyb3d8bbwe")
    game.mkdir(parents=True)
    assert is_xbox_install(game)
