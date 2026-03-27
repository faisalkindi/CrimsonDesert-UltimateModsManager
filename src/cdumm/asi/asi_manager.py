"""ASI plugin management — scan, install, enable/disable, conflict detection, config open."""
import configparser
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

ASI_SUFFIX = ".asi"
DISABLED_SUFFIX = ".asi.disabled"
ASI_LOADER = "winmm.dll"


@dataclass
class AsiPlugin:
    name: str
    path: Path
    enabled: bool
    ini_path: Path | None
    hook_targets: list[str] = field(default_factory=list)


@dataclass
class AsiConflict:
    plugin_a: str
    plugin_b: str
    reason: str


class AsiManager:
    """Manages ASI plugins in the game's bin64 directory."""

    def __init__(self, bin64_dir: Path) -> None:
        self._bin64 = bin64_dir

    def scan(self) -> list[AsiPlugin]:
        """Scan bin64/ for ASI plugins."""
        plugins: list[AsiPlugin] = []

        if not self._bin64.exists():
            return plugins

        for f in sorted(self._bin64.iterdir()):
            if f.suffix.lower() == ASI_SUFFIX:
                ini = self._find_ini(f)
                hooks = self._parse_hook_targets(ini) if ini else []
                plugins.append(AsiPlugin(
                    name=f.stem, path=f, enabled=True,
                    ini_path=ini, hook_targets=hooks,
                ))
            elif f.name.lower().endswith(DISABLED_SUFFIX):
                base_name = f.name[: -len(DISABLED_SUFFIX)]
                ini = self._find_ini(f.with_name(base_name + ".ini"))
                hooks = self._parse_hook_targets(ini) if ini else []
                plugins.append(AsiPlugin(
                    name=base_name, path=f, enabled=False,
                    ini_path=ini, hook_targets=hooks,
                ))

        return plugins

    def has_loader(self) -> bool:
        """Check if Ultimate ASI Loader (winmm.dll) is present."""
        return (self._bin64 / ASI_LOADER).exists()

    def enable(self, plugin: AsiPlugin) -> None:
        """Enable a disabled ASI plugin."""
        if plugin.enabled:
            return
        new_path = plugin.path.with_name(plugin.name + ASI_SUFFIX)
        plugin.path.rename(new_path)
        plugin.path = new_path
        plugin.enabled = True
        logger.info("Enabled ASI: %s", plugin.name)

    def disable(self, plugin: AsiPlugin) -> None:
        """Disable an enabled ASI plugin."""
        if not plugin.enabled:
            return
        new_path = plugin.path.with_name(plugin.name + DISABLED_SUFFIX)
        plugin.path.rename(new_path)
        plugin.path = new_path
        plugin.enabled = False
        logger.info("Disabled ASI: %s", plugin.name)

    def install(self, source: Path) -> list[str]:
        """Install ASI mod from a file or folder into bin64/.

        Copies .asi, .ini, and ASI loader .dll files.
        Returns list of installed file names.
        """
        installed: list[str] = []
        self._bin64.mkdir(parents=True, exist_ok=True)

        ASI_LOADERS = {"winmm.dll", "version.dll", "dinput8.dll", "dsound.dll"}

        if source.is_file() and source.suffix.lower() == ASI_SUFFIX:
            # Single .asi file — also grab all companion files from same dir
            shutil.copy2(source, self._bin64 / source.name)
            installed.append(source.name)
            for f in source.parent.iterdir():
                if f == source or not f.is_file():
                    continue
                if f.suffix.lower() == ".ini":
                    shutil.copy2(f, self._bin64 / f.name)
                    installed.append(f.name)
                elif f.name.lower() in ASI_LOADERS:
                    if not (self._bin64 / f.name).exists():
                        shutil.copy2(f, self._bin64 / f.name)
                        installed.append(f.name)
        elif source.is_dir():
            for f in source.iterdir():
                if not f.is_file():
                    continue
                if f.suffix.lower() in (ASI_SUFFIX, ".ini"):
                    shutil.copy2(f, self._bin64 / f.name)
                    installed.append(f.name)
                elif f.name.lower() in ASI_LOADERS:
                    if not (self._bin64 / f.name).exists():
                        shutil.copy2(f, self._bin64 / f.name)
                        installed.append(f.name)

        if installed:
            logger.info("Installed ASI files: %s", installed)
        return installed

    def uninstall(self, plugin: AsiPlugin) -> list[str]:
        """Remove ASI plugin and all companion INI files from bin64/.

        Returns list of deleted file names.
        """
        deleted: list[str] = []
        if plugin.path.exists():
            plugin.path.unlink()
            deleted.append(plugin.path.name)
        # Delete all INI files matching this plugin name
        stem = plugin.name.lower()
        for f in self._bin64.iterdir():
            if f.suffix.lower() == ".ini" and f.stem.lower().startswith(stem):
                f.unlink()
                deleted.append(f.name)
        if deleted:
            logger.info("Uninstalled ASI: %s (%s)", plugin.name, deleted)
        return deleted

    def update(self, plugin: AsiPlugin, source: Path) -> list[str]:
        """Update an ASI plugin by replacing its files with newer versions.

        Copies .asi and .ini from source, overwriting existing files.
        Returns list of updated file names.
        """
        updated: list[str] = []
        self._bin64.mkdir(parents=True, exist_ok=True)

        if source.is_file() and source.suffix.lower() == ASI_SUFFIX:
            dest = self._bin64 / (plugin.name + ASI_SUFFIX)
            if not plugin.enabled:
                dest = self._bin64 / (plugin.name + DISABLED_SUFFIX)
            shutil.copy2(source, dest)
            updated.append(dest.name)
            ini = source.with_suffix(".ini")
            if ini.exists():
                shutil.copy2(ini, self._bin64 / (plugin.name + ".ini"))
                updated.append(plugin.name + ".ini")
        elif source.is_dir():
            for f in source.iterdir():
                if f.is_file() and f.suffix.lower() == ASI_SUFFIX:
                    dest = self._bin64 / (plugin.name + ASI_SUFFIX)
                    if not plugin.enabled:
                        dest = self._bin64 / (plugin.name + DISABLED_SUFFIX)
                    shutil.copy2(f, dest)
                    updated.append(dest.name)
                elif f.is_file() and f.suffix.lower() == ".ini":
                    shutil.copy2(f, self._bin64 / f.name)
                    updated.append(f.name)

        if updated:
            logger.info("Updated ASI: %s (%s)", plugin.name, updated)
        return updated

    @staticmethod
    def contains_asi(path: Path) -> bool:
        """Check if a path contains ASI plugin files."""
        if path.is_file():
            return path.suffix.lower() == ASI_SUFFIX
        if path.is_dir():
            return any(f.suffix.lower() == ASI_SUFFIX for f in path.iterdir() if f.is_file())
        return False

    def open_config(self, plugin: AsiPlugin) -> bool:
        """Open plugin's INI file in default text editor. Returns True if opened."""
        if plugin.ini_path and plugin.ini_path.exists():
            os.startfile(str(plugin.ini_path))
            return True
        return False

    def detect_conflicts(self, plugins: list[AsiPlugin]) -> list[AsiConflict]:
        """Detect potential conflicts between ASI plugins based on INI configs."""
        conflicts: list[AsiConflict] = []
        enabled = [p for p in plugins if p.enabled]

        for i in range(len(enabled)):
            for j in range(i + 1, len(enabled)):
                a, b = enabled[i], enabled[j]

                # Check for overlapping hook targets
                common_hooks = set(a.hook_targets) & set(b.hook_targets)
                if common_hooks:
                    conflicts.append(AsiConflict(
                        plugin_a=a.name, plugin_b=b.name,
                        reason=f"Both hook: {', '.join(common_hooks)}",
                    ))

                # Check for same DLL proxy name
                if a.name.lower() == b.name.lower():
                    conflicts.append(AsiConflict(
                        plugin_a=a.name, plugin_b=b.name,
                        reason="Same plugin name — only one can load",
                    ))

        return conflicts

    def _find_ini(self, asi_or_ini_path: Path) -> Path | None:
        """Find companion INI file for an ASI plugin."""
        stem = asi_or_ini_path.stem.lower()
        # Try exact match first
        ini = asi_or_ini_path.with_suffix(".ini")
        if ini.exists():
            return ini
        # Try any INI whose name starts with the plugin stem (e.g. Foo_settings.ini for Foo.asi)
        for f in self._bin64.iterdir():
            if f.suffix.lower() == ".ini" and f.stem.lower().startswith(stem):
                return f
        return None

    def _parse_hook_targets(self, ini_path: Path | None) -> list[str]:
        """Extract hook targets from INI config."""
        if not ini_path or not ini_path.exists():
            return []

        targets: list[str] = []
        try:
            config = configparser.ConfigParser(strict=False)
            config.read(str(ini_path), encoding="utf-8")

            for section in config.sections():
                for key in config[section]:
                    key_lower = key.lower()
                    # Look for common hook target indicators
                    if any(kw in key_lower for kw in ["hook", "target", "dll", "function", "address"]):
                        value = config[section][key].strip()
                        if value:
                            targets.append(f"{section}/{key}={value}")

        except Exception:
            logger.debug("Failed to parse INI: %s", ini_path, exc_info=True)

        return targets
