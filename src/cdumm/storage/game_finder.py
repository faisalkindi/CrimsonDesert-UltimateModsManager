import json
import logging
import re
import sys
from pathlib import Path

from cdumm.platform import IS_MACOS, IS_WINDOWS

logger = logging.getLogger(__name__)

STEAM_DEFAULT_PATHS = [
    Path("C:/Program Files (x86)/Steam"),
    Path("C:/Program Files/Steam"),
]

# macOS Steam install locations. Steam stores its library at
# ``~/Library/Application Support/Steam`` by default; the user can
# add additional libraries elsewhere via ``libraryfolders.vdf`` —
# the Linux-style scan below picks those up.
STEAM_DEFAULT_PATHS_MACOS = [
    Path.home() / "Library" / "Application Support" / "Steam",
]

# Common macOS game install locations users pick when not using Steam.
# Each is searched recursively (one level deep) for ``Crimson Desert.app``
# bundles whose ``Contents/Resources/packages/`` matches the PAZ layout.
MACOS_GAME_LOCATIONS = [
    Path.home() / "Games",
    Path.home() / "Applications",
    Path("/Applications"),
]

# The PAZ data subdirectory inside a native macOS Crimson Desert.app.
MACOS_APP_DATA_SUBPATH = Path("Contents/Resources/packages")

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

    if IS_WINDOWS:
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


def _scan_for_steam_libraries(base_paths: list[Path]) -> list[Path]:
    """Given candidate Steam library base directories, return the
    Crimson Desert install paths inside any that contain it.

    Issue #43 (Feikaz, 2026-04-25): Steam-VDF-only detection misses
    libraries that aren't registered with the user's primary Steam
    install (e.g. game on F:/Steam while Steam itself is on C: and
    its libraryfolders.vdf doesn't list F:/). This helper does an
    independent direct scan so the auto-detect catches that case.
    """
    found: list[Path] = []
    for base in base_paths:
        try:
            game_dir = base / "steamapps" / "common" / "Crimson Desert"
            if (game_dir / GAME_EXE).exists():
                found.append(game_dir)
        except OSError:
            continue
    return found


def _resolve_macos_game_dir(candidate: Path) -> Path | None:
    """Resolve a user-supplied macOS path to the inner PAZ directory.

    Accepts:
    - A ``Crimson Desert.app`` bundle (returns ``Contents/Resources/packages``).
    - A ``packages/`` directory directly (returns it unchanged).
    - Any directory that already looks like the game root (PAZ layout).

    Returns ``None`` if nothing in/around the path resembles a Crimson
    Desert install. Used by both the auto-detect scan and the manual
    folder-picker — pointing CDUMM at the user-visible ``.app`` is the
    obvious-thing-to-do, so we accept it and walk in.
    """
    if not candidate.exists():
        return None
    # Already pointed at the inner data directory.
    if _looks_like_game_root(candidate):
        return candidate
    # Pointed at the .app — walk into Contents/Resources/packages.
    if candidate.suffix == ".app":
        inner = candidate / MACOS_APP_DATA_SUBPATH
        if _looks_like_game_root(inner):
            return inner
    # Pointed at a sibling of the .app — scan one level for any .app
    # whose inner packages dir looks right.
    if candidate.is_dir():
        try:
            for child in candidate.iterdir():
                if child.suffix == ".app":
                    inner = child / MACOS_APP_DATA_SUBPATH
                    if _looks_like_game_root(inner):
                        return inner
        except OSError:
            pass
    return None


def _scan_macos_app_in_dir(parent: Path) -> list[Path]:
    """Look for ``Crimson Desert.app`` (or any .app whose inner
    packages dir is the game root) directly inside ``parent``.

    One level only; we don't deep-recurse user home directories.
    Returns a list of resolved game data paths (the inner packages
    directory, not the .app itself) so callers can treat them like
    Steam library hits.
    """
    found: list[Path] = []
    if not parent.exists() or not parent.is_dir():
        return found
    try:
        for entry in parent.iterdir():
            if entry.suffix != ".app":
                continue
            # Quick name filter — saves a stat on every other .app the
            # user has installed in /Applications.
            name_lower = entry.name.lower()
            if "crimson" not in name_lower:
                continue
            inner = entry / MACOS_APP_DATA_SUBPATH
            if _looks_like_game_root(inner):
                found.append(inner)
                logger.info("Found Crimson Desert (macOS .app) at %s", entry)
    except OSError:
        pass
    return found


def _find_macos_steam_libraries() -> list[Path]:
    """Search macOS Steam libraries for Crimson Desert.

    Steam on macOS uses the same ``steamapps/libraryfolders.vdf``
    layout as Linux/Windows; the only difference is the default root
    location and the fact that Mac-native games install as
    ``<Game>.app`` instead of a folder. The library hit is the
    inner ``Contents/Resources/packages/`` directory.
    """
    candidates: list[Path] = []
    for root in STEAM_DEFAULT_PATHS_MACOS:
        if not root.exists():
            continue
        library_dirs: list[Path] = [root]
        vdf = root / LIBRARY_FOLDERS_VDF
        if vdf.exists():
            library_dirs.extend(_parse_library_folders(vdf))
        for lib in library_dirs:
            common = lib / "steamapps" / "common"
            candidates.extend(_scan_macos_app_in_dir(common))
    return candidates


def _find_macos_game_directories() -> list[Path]:
    """Native macOS auto-detect.

    Walks the well-known places macOS users put games and Steam's
    library tree, returning every plausible Crimson Desert install
    (resolved to the inner packages directory).
    """
    candidates: list[Path] = []
    candidates.extend(_find_macos_steam_libraries())
    for parent in MACOS_GAME_LOCATIONS:
        candidates.extend(_scan_macos_app_in_dir(parent))
    return candidates


def find_game_directories() -> list[Path]:
    """Search Steam, Epic Games Store, and Xbox for Crimson Desert install."""
    # macOS native build: scan Steam macOS + ~/Games + ~/Applications +
    # /Applications for Crimson Desert .app bundles. The Windows-only
    # paths below would never hit on macOS anyway (no C: drive), but
    # short-circuiting keeps the log quiet.
    if IS_MACOS:
        candidates = _find_macos_game_directories()
        # Dedup by resolved path (same as the Windows path below).
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

    candidates: list[Path] = []

    # Steam detection — primary path: VDF parsing from one Steam root.
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
                logger.info("Found Crimson Desert (Steam VDF) at %s", game_dir)
    else:
        logger.info("No Steam root found in default locations")

    # Steam detection — fallback: direct drive scan for common library
    # paths. Catches Steam libraries the primary install's VDF doesn't
    # know about (issue #43). Dedup happens at end via path resolve.
    direct_bases = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        for prefix in ("Steam", "SteamLibrary",
                       "Games/Steam", "Games/SteamLibrary"):
            direct_bases.append(Path(f"{letter}:/{prefix}"))
    for found in _scan_for_steam_libraries(direct_bases):
        candidates.append(found)
        logger.info("Found Crimson Desert (Steam direct scan) at %s", found)

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

    Native macOS: there is no ``bin64/`` and no ``CrimsonDesert.exe``
    — the game is a single ``Crimson Desert.app`` bundle and the PAZ
    archives live under ``Contents/Resources/packages/``. Accept any
    path that resolves (directly or by walking into a .app) to a
    directory containing the PAZ structural markers.
    """
    if IS_MACOS:
        return _resolve_macos_game_dir(path) is not None
    if (path / GAME_EXE).exists():
        return True
    if is_xbox_install(path) and _looks_like_game_root(path):
        return True
    return False


def resolve_game_directory(path: Path) -> Path | None:
    """Return the canonical game-data directory for a user-supplied path.

    Always pair this with :func:`validate_game_directory` when the
    user picks a folder via the welcome wizard or the setup dialog.
    On Windows / Linux this is a no-op pass-through (the user already
    points at the install root). On macOS, the user's natural target
    is the visible ``Crimson Desert.app`` bundle but CDUMM operates
    on the ``Contents/Resources/packages/`` directory inside it; this
    helper walks in so the saved ``game_directory`` config value is
    the path the rest of the app expects to see.

    Returns ``None`` when the path doesn't resolve to a Crimson Desert
    install — callers should treat that the same way they treat a
    failed validation.
    """
    if not path:
        return None
    if IS_MACOS:
        return _resolve_macos_game_dir(path)
    return path if validate_game_directory(path) else None


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
