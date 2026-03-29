"""Game version detection via Steam build ID + exe size fingerprinting."""
import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def detect_game_version(game_dir: Path) -> str | None:
    """Return a version fingerprint for the current game installation.

    Uses Steam's buildid (most reliable — changes with every update),
    falling back to game exe size + PAMT sizes.
    """
    try:
        parts = []

        # Primary: Steam build ID from appmanifest
        build_id = _get_steam_build_id(game_dir)
        if build_id:
            parts.append(f"buildid:{build_id}")

        # Secondary: game exe size
        exe = game_dir / "bin64" / "CrimsonDesert.exe"
        if exe.exists():
            parts.append(f"exe:{exe.stat().st_size}")

        # Tertiary: a few PAMT sizes
        for d in ["0000", "0001", "0002"]:
            pamt = game_dir / d / "0.pamt"
            if pamt.exists():
                parts.append(f"{d}:{pamt.stat().st_size}")

        if not parts:
            return None
        combined = "|".join(parts)
        return hashlib.sha256(combined.encode()).hexdigest()[:16]
    except Exception as e:
        logger.warning("Could not detect game version: %s", e)
        return None


def _get_steam_build_id(game_dir: Path) -> str | None:
    """Read Steam build ID from appmanifest file."""
    try:
        # game_dir is like .../steamapps/common/Crimson Desert
        steamapps = game_dir.parent.parent
        for acf in steamapps.glob("appmanifest_*.acf"):
            text = acf.read_text(errors="replace")
            if "Crimson Desert" not in text:
                continue
            for line in text.splitlines():
                line = line.strip()
                if line.startswith('"buildid"'):
                    return line.split('"')[-2]
    except Exception:
        pass
    return None
