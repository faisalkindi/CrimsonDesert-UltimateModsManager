import json
import logging
import re
import sys
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

# Epic Games Store display names to match
EPIC_GAME_NAMES = [
    "crimson desert",
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


def _looks_like_game_root(path: Path) -> bool:
    """True when ``path`` contains the numbered PAZ directory layout.

    Used as a fallback for Xbox installs where ``bin64/CrimsonDesert.exe``
    may not be present at the game-dir root (the Microsoft Store splits
    executables into sibling package directories). The PAZ layout itself
    is what the mod manager cares about: ``0008/0.paz``, ``meta/0.papgt``
    etc. If those exist we can mod the game regardless of where the exe
    lives on disk.
    """
    try:
        return ((path / "0008" / "0.paz").exists()
                and (path / "meta" / "0.papgt").exists())
    except OSError:
        return False


def _find_xbox_game_pass() -> list[Path]:
    """Search for Crimson Desert installed via Xbox Game Pass / Microsoft Store.

    Xbox Game Pass games can be installed at:
    - {drive}:/XboxGames/<GameName>/Content/
    - {drive}:/XboxGames/<GameName>/Content/packages/ (Tunsi82 workaround)
    - Custom paths set in Xbox app (detected via .GamingRoot files)
    - C:/Program Files/ModifiableWindowsApps/<GameName>/
    """
    candidates: list[Path] = []

    # Find drives with .GamingRoot (Xbox app marks these)
    gaming_drives: list[str] = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = Path(f"{letter}:/")
        try:
            if (root / ".GamingRoot").exists():
                gaming_drives.append(letter)
        except OSError:
            continue

    # Search gaming drives and all drives for XboxGames folder
    search_letters = set(gaming_drives) | set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    for letter in search_letters:
        for name in XBOX_GAME_NAMES:
            # Check multiple candidate subpaths — Xbox layout differs per
            # title (some use /Content, some /Content/packages where the
            # actual PAZ dirs sit).
            for sub in ["", "Content", "Content/packages"]:
                xbox_path = Path(f"{letter}:/XboxGames/{name}")
                if sub:
                    xbox_path = xbox_path / sub
                try:
                    if (xbox_path / GAME_EXE).exists():
                        candidates.append(xbox_path)
                        logger.info("Found Crimson Desert (Xbox) at %s", xbox_path)
                    elif _looks_like_game_root(xbox_path):
                        # Exe not at root but PAZ layout is — treat as game dir
                        candidates.append(xbox_path)
                        logger.info(
                            "Found Crimson Desert (Xbox, exe-missing) at %s",
                            xbox_path)
                except OSError:
                    continue

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
            except (PermissionError, OSError):
                pass

    return candidates


def _find_epic_games() -> list[Path]:
    """Search for Crimson Desert installed via Epic Games Store.

    Epic stores manifest .item files (JSON) in a Manifests folder.
    Each manifest has DisplayName and InstallLocation fields.
    """
    candidates: list[Path] = []

    # Find manifest directory from registry or default paths
    manifest_dirs: list[Path] = []

    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\WOW6432Node\Epic Games\EpicGamesLauncher"
            ) as key:
                app_data = winreg.QueryValueEx(key, "AppDataPath")[0]
                manifest_dirs.append(Path(app_data) / "Manifests")
        except Exception:
            pass

    # Fallback paths
    manifest_dirs.append(
        Path("C:/ProgramData/Epic/EpicGamesLauncher/Data/Manifests"))
    local_app = Path.home() / "AppData" / "Local" / "EpicGamesLauncher" / "Saved" / "Config"
    # Also check the common programdata path variant
    manifest_dirs.append(
        Path("C:/ProgramData/Epic/UnrealEngineLauncher/LauncherInstalled.dat").parent)

    for manifest_dir in manifest_dirs:
        if not manifest_dir.exists():
            continue
        try:
            for item_file in manifest_dir.glob("*.item"):
                try:
                    data = json.loads(item_file.read_text(encoding="utf-8"))
                    display_name = data.get("DisplayName", "")
                    install_loc = data.get("InstallLocation", "")
                    if not display_name or not install_loc:
                        continue
                    if any(n in display_name.lower() for n in EPIC_GAME_NAMES):
                        game_path = Path(install_loc)
                        if (game_path / GAME_EXE).exists():
                            candidates.append(game_path)
                            logger.info("Found Crimson Desert (Epic) at %s", game_path)
                except Exception:
                    continue
        except Exception:
            continue

    # Fallback: check common Epic install locations
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        for folder in ["Epic Games", "EpicGames"]:
            for name in ["CrimsonDesert", "Crimson Desert"]:
                epic_path = Path(f"{letter}:/{folder}/{name}")
                if epic_path not in candidates and (epic_path / GAME_EXE).exists():
                    candidates.append(epic_path)
                    logger.info("Found Crimson Desert (Epic fallback) at %s", epic_path)

    return candidates


def find_game_directories() -> list[Path]:
    """Search Steam, Epic Games Store, and Xbox for Crimson Desert install."""
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

    # Epic Games Store detection
    epic_candidates = _find_epic_games()
    candidates.extend(epic_candidates)

    # Xbox Game Pass detection
    xbox_candidates = _find_xbox_game_pass()
    candidates.extend(xbox_candidates)

    # Deduplicate by resolved path
    seen: set[str] = set()
    unique: list[Path] = []
    for c in candidates:
        try:
            key = str(c.resolve()).lower()
        except Exception:
            key = str(c).lower()
        if key not in seen:
            seen.add(key)
            unique.append(c)

    return unique


def validate_game_directory(path: Path) -> bool:
    """Check if ``path`` is a valid Crimson Desert install.

    Steam/Epic ship ``bin64/CrimsonDesert.exe`` at the install root, so
    that's the primary check. Xbox Game Pass installs can split the
    executable off from the game data — the PAZ archives live under
    ``Content/packages/`` while the exe is registered elsewhere by the
    Microsoft Store. Accept either layout: if the exe is missing but
    the PAZ archive layout is present, CDUMM has everything it needs
    to mod the game. Tunsi82's workaround confirmed pointing at
    ``C:/XboxGames/Crimson Desert/Content/packages`` works.
    """
    if (path / GAME_EXE).exists():
        return True
    if is_xbox_install(path) and _looks_like_game_root(path):
        return True
    return False


def is_steam_install(game_dir: Path) -> bool:
    """Check if the game directory is a Steam installation."""
    return "steamapps" in str(game_dir).lower()


def is_epic_install(game_dir: Path) -> bool:
    """Check if the game directory is an Epic Games Store installation."""
    path_lower = str(game_dir).lower()
    return "epic games" in path_lower or "epicgames" in path_lower


def is_xbox_install(game_dir: Path) -> bool:
    """Check if the game directory is an Xbox Game Pass installation."""
    path_lower = str(game_dir).lower()
    return ("xboxgames" in path_lower
            or "windowsapps" in path_lower
            or "modifiablewindowsapps" in path_lower)
