"""Headless game-launch logic, extracted from FluentWindow so the
CLI can launch the game without importing Qt.

GitHub #63 (AwkwardOrpheus, 2026-05-02): users on handheld devices
(Steam Deck, ROG Ally) want to register CDUMM as a non-Steam launcher
and press Play once instead of opening CDUMM, clicking Apply, then
clicking Play. The CLI subcommand `--launch-game` runs the apply
pipeline and then invokes this module on success.
"""
from __future__ import annotations
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def _open_uri(uri: str) -> None:
    """Open a URI via the OS default handler.

    Wrapped so tests can monkey-patch the dispatch without intercepting
    the lower-level platform-specific calls. Delegates to
    :func:`cdumm.platform.open_path`, which handles Windows
    (``os.startfile``), macOS (``open``), and Linux
    (``xdg-open`` / ``gio open``) correctly.

    Previously this used a manual ``if win32: os.startfile / else
    xdg-open`` branch, which was a latent macOS bug — ``xdg-open``
    is Linux-only and silently fails on macOS where ``open`` is the
    canonical handler. Routing through ``platform.open_path`` closes
    that gap (PR #64 review bonus sweep).
    """
    from cdumm.platform import open_path
    open_path(uri)


def _run_exe(exe: Path, cwd: Path) -> None:
    """Spawn an executable in the given working directory."""
    subprocess.Popen([str(exe)], cwd=str(cwd))


def _find_game_exe(game_dir: Path) -> Path:
    """Locate the game executable in <game_dir>/bin64/.

    Raises FileNotFoundError if neither CrimsonDesert.exe nor the
    lowercase variant exists.
    """
    bin64 = game_dir / "bin64"
    for candidate in ["CrimsonDesert.exe", "crimsondesert.exe"]:
        exe = bin64 / candidate
        if exe.exists():
            return exe
    raise FileNotFoundError(
        f"CrimsonDesert.exe not found in {bin64}")


def launch_game(game_dir: Path) -> None:
    """Launch Crimson Desert via the appropriate channel for the install.

    Detection order:
    1. Steam install -> steam://rungameid/<app_id> (preserves overlay/DRM)
    2. Xbox install -> shell:AppsFolder URI
    3. Direct exe in bin64/ as fallback

    Raises:
        FileNotFoundError: bin64/CrimsonDesert.exe is missing.
        Other exceptions propagate from the launch handler so callers
        can exit non-zero with a real error.
    """
    exe = _find_game_exe(game_dir)

    from cdumm.storage.game_finder import is_steam_install, is_xbox_install

    if is_steam_install(game_dir):
        from cdumm.engine.game_monitor import get_steam_app_id
        app_id = get_steam_app_id(game_dir)
        logger.info("Launching Crimson Desert via Steam (app_id=%s)", app_id)
        _open_uri(f"steam://rungameid/{app_id}")
        return

    if is_xbox_install(game_dir):
        logger.info("Launching Crimson Desert via Xbox shell URI")
        _open_uri(
            "shell:AppsFolder\\PearlAbyss.CrimsonDesert_8wekyb3d8bbwe!Game")
        return

    logger.info("Launching Crimson Desert directly: %s", exe)
    _run_exe(exe, exe.parent)
