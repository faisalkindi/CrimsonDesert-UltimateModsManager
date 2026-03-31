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

# Xbox Game Pass / Microsoft Store possible locations
XBOX_GAME_NAMES = [
    "Crimson Desert",
    "PearlAbyss.CrimsonDesert",
    "CrimsonDesert",
]


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
        for match in re.finditer(r'"path"\s+"([^"]+)"', text):
            raw = match.group(1).replace("\\\\", "/").replace("\\", "/")
            paths.append(Path(raw))
    except Exception:
        logger.warning("Failed to parse %s", vdf_path, exc_info=True)
    return paths


def _find_xbox_game_pass() -> list[Path]:
    """Search for Crimson Desert installed via Xbox Game Pass / Microsoft Store.

    Xbox Game Pass games can be installed at:
    - C:/XboxGames/<GameName>/Content/
    - D:/XboxGames/<GameName>/Content/
    - Custom paths set in Xbox app
    - C:/Program Files/ModifiableWindowsApps/<GameName>/
    - C:/Program Files/WindowsApps/<PublisherId>/ (usually locked)
    """
    candidates: list[Path] = []

    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        # XboxGames folder (most common for Game Pass)
        for name in XBOX_GAME_NAMES:
            for sub in ["Content", ""]:
                xbox_path = Path(f"{letter}:/XboxGames/{name}")
                if sub:
                    xbox_path = xbox_path / sub
                if (xbox_path / GAME_EXE).exists():
                    candidates.append(xbox_path)
                    logger.info("Found Crimson Desert (Xbox) at %s", xbox_path)

    # ModifiableWindowsApps (accessible Game Pass location)
    for base in ["C:/Program Files/ModifiableWindowsApps",
                 "C:/Program Files (x86)/ModifiableWindowsApps"]:
        base_path = Path(base)
        if base_path.exists():
            try:
                for d in base_path.iterdir():
                    if d.is_dir() and any(n.lower() in d.name.lower()
                                          for n in XBOX_GAME_NAMES):
                        if (d / GAME_EXE).exists():
                            candidates.append(d)
                            logger.info("Found Crimson Desert (WindowsApps) at %s", d)
            except PermissionError:
                pass

    return candidates


def find_game_directories() -> list[Path]:
    """Search Steam and Xbox Game Pass for Crimson Desert install."""
    candidates: list[Path] = []

    # Steam detection
    steam_root = _find_steam_root()
    if steam_root is not None:
        vdf = steam_root / LIBRARY_FOLDERS_VDF
        library_dirs = [steam_root]
        if vdf.exists():
            library_dirs.extend(_parse_library_folders(vdf))

        for lib_dir in library_dirs:
            game_dir = lib_dir / "steamapps" / "common" / "Crimson Desert"
            if (game_dir / GAME_EXE).exists():
                candidates.append(game_dir)
                logger.info("Found Crimson Desert (Steam) at %s", game_dir)
    else:
        logger.info("No Steam root found in default locations")

    # Xbox Game Pass detection
    xbox_candidates = _find_xbox_game_pass()
    candidates.extend(xbox_candidates)

    return candidates


def validate_game_directory(path: Path) -> bool:
    """Check if path is a valid Crimson Desert install."""
    return (path / GAME_EXE).exists()


def is_steam_install(game_dir: Path) -> bool:
    """Check if the game directory is a Steam installation."""
    return "steamapps" in str(game_dir).lower()


def is_xbox_install(game_dir: Path) -> bool:
    """Check if the game directory is an Xbox Game Pass installation."""
    path_lower = str(game_dir).lower()
    return ("xboxgames" in path_lower
            or "windowsapps" in path_lower
            or "modifiablewindowsapps" in path_lower)
