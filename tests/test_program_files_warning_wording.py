"""Bug (1Phase1 Nexus comment, 2026-04-23): the Program Files warning
dialog tells users to move their Steam library to ``C:\\SteamLibrary``
but that advice is physically impossible when Steam is installed on C:.

Steam enforces ONE library folder per drive/partition (verified via
multiple Steam Community help threads from 2020-2023, e.g.
https://steamcommunity.com/discussions/forum/1/2527030866859486860/
and https://steamcommunity.com/discussions/forum/1/1489987633998210185/).
If Steam itself lives under ``C:\\Program Files\\Steam``, there is
already one library on C: and Steam's "Add library folder" dialog
rejects ``C:\\SteamLibrary`` with "This drive already has a library
folder."

The corrected wording must instruct users to use a DIFFERENT DRIVE
(``D:\\`` or another letter), not a different folder on the same
drive.

This test pins the corrected text so the hint can't regress to the
impossible guidance again.
"""
from __future__ import annotations

from pathlib import Path


def test_program_files_warning_tells_user_to_use_a_different_drive():
    """The dialog body must say 'different drive', not 'different
    location'. Using 'different location' on its own misleads users
    into trying another folder on the same drive.
    """
    src = (Path(__file__).resolve().parents[1]
           / "src" / "cdumm" / "gui" / "fluent_window.py").read_text(
               encoding="utf-8")
    # Pin the phrase that fixes 1Phase1's feedback.
    assert "different drive" in src, (
        "Program Files warning should use 'different drive' wording. "
        "Steam allows only one library per drive, so 'different "
        "location' suggests an action that Steam rejects.")


def test_program_files_warning_example_path_is_not_on_c_drive():
    """The example in the dialog must not suggest C:\\SteamLibrary as
    the target. If the user's game is on C: already (which is the
    ONLY time this warning fires, per the `if 'program files' not in
    game_path: return` gate), pointing them at another folder on C:
    produces Steam's 'This drive already has a library folder' error.
    """
    src = (Path(__file__).resolve().parents[1]
           / "src" / "cdumm" / "gui" / "fluent_window.py").read_text(
               encoding="utf-8")
    # Find the Program Files warning block by anchoring on the dialog title.
    assert "Game Location Warning" in src
    # Extract the surrounding ~30 lines and check the example path
    # doesn't lead with C:.
    idx = src.index("Game Location Warning")
    block = src[idx:idx + 2000]
    # The only example path should use a non-C drive letter.
    assert "C:\\SteamLibrary" not in block, (
        "Example path must not be C:\\SteamLibrary — Steam rejects "
        "a second library on the same drive. Use D:\\SteamLibrary "
        "(or similar) instead.")


def test_english_translation_key_also_corrected():
    """The in-app dialog at fluent_window.py is hardcoded, but the
    translated key `dialog.game_location_warning_body` exists in
    translation files for the welcome wizard / similar flows. The
    English source of truth (en.json) must also be corrected so that
    any call site using `tr()` gets the right wording.
    """
    import json
    en = (Path(__file__).resolve().parents[1]
          / "src" / "cdumm" / "translations" / "en.json").read_text(
              encoding="utf-8")
    data = json.loads(en)
    body = data.get("dialog.game_location_warning_body", "")
    assert "C:\\SteamLibrary" not in body, (
        "en.json dialog.game_location_warning_body still suggests "
        "C:\\SteamLibrary as the target, which Steam rejects when "
        "Steam is already on C:.")
    assert "different drive" in body.lower() or "another drive" in body.lower(), (
        f"en.json body doesn't tell the user to use a different drive. "
        f"Got: {body!r}")
