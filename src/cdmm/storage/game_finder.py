import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

STEAM_DEFAULT_PATHS = [
    Path("C:/Program Files (x86)/Steam"),
    Path("C:/Program Files/Steam"),
]

GAME_EXE = Path("bin64/CrimsonDesert.exe")
LIBRARY_FOLDERS_VDF = "steamapps/libraryfolders.vdf"


def _find_steam_root() -> Path | None:
    for p in STEAM_DEFAULT_PATHS:
        if p.exists():
            return p
    # Search all drive roots
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        steam = Path(f"{letter}:/Steam")
        if steam.exists():
            return steam
        steam2 = Path(f"{letter}:/SteamLibrary")
        if steam2.exists():
            return steam2
    return None


def _parse_library_folders(vdf_path: Path) -> list[Path]:
    """Parse libraryfolders.vdf to extract library paths."""
    paths: list[Path] = []
    try:
        text = vdf_path.read_text(encoding="utf-8")
        # Match "path" entries — format: "path"		"E:\\SteamLibrary"
        for match in re.finditer(r'"path"\s+"([^"]+)"', text):
            raw = match.group(1).replace("\\\\", "/").replace("\\", "/")
            paths.append(Path(raw))
    except Exception:
        logger.warning("Failed to parse %s", vdf_path, exc_info=True)
    return paths


def find_game_directories() -> list[Path]:
    """Search all Steam library folders for Crimson Desert install."""
    candidates: list[Path] = []

    steam_root = _find_steam_root()
    if steam_root is None:
        logger.info("No Steam root found in default locations")
        return candidates

    # Collect library folders from VDF
    vdf = steam_root / LIBRARY_FOLDERS_VDF
    library_dirs = [steam_root]
    if vdf.exists():
        library_dirs.extend(_parse_library_folders(vdf))

    # Search each library for Crimson Desert
    for lib_dir in library_dirs:
        game_dir = lib_dir / "steamapps" / "common" / "Crimson Desert"
        if (game_dir / GAME_EXE).exists():
            candidates.append(game_dir)
            logger.info("Found Crimson Desert at %s", game_dir)

    return candidates


def validate_game_directory(path: Path) -> bool:
    """Check if path is a valid Crimson Desert install."""
    return (path / GAME_EXE).exists()
