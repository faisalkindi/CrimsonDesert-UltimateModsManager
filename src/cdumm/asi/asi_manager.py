"""ASI plugin management — scan, install, enable/disable, conflict detection, config open."""
import configparser
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

ASI_SUFFIX = ".asi"
DISABLED_SUFFIX = ".asi.disabled"


def _stem_from_installed(name: str) -> str | None:
    """Return the bare plugin stem from an installed file name, or
    None if the file isn't a plugin .asi.

    Handles both `Foo.asi` and `Foo.asi.disabled` forms so callers
    that look up `asi_plugin_state` by name (PRIMARY KEY = stem)
    work regardless of which extension the install landed on.
    """
    lo = name.lower()
    if lo.endswith(DISABLED_SUFFIX):
        return name[: -len(DISABLED_SUFFIX)]
    if lo.endswith(".asi"):
        return name[: -len(".asi")]
    return None


def _resolve_version_filename(name: str) -> str | None:
    """Map an installed plugin file name to the matching `.version`
    sidecar filename. Returns None for non-plugin files."""
    stem = _stem_from_installed(name)
    if stem is None:
        return None
    return f"{stem}.version"
ASI_LOADER_NAMES = {"winmm.dll", "version.dll", "dinput8.dll", "dsound.dll"}
SIDECAR_SUFFIX = ".cdumm-files.json"


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
        """Check if Ultimate ASI Loader is present (any known proxy DLL name)."""
        return any((self._bin64 / name).exists() for name in ASI_LOADER_NAMES)

    def enable(self, plugin: AsiPlugin) -> None:
        """Enable a disabled ASI plugin."""
        if plugin.enabled:
            return
        new_path = plugin.path.with_name(plugin.name + ASI_SUFFIX)
        # os.replace is atomic on Windows even when the destination
        # already exists (e.g., a stale .asi sibling left after a
        # crash). Path.rename() raises FileExistsError on Windows in
        # that case and leaves the plugin in an indeterminate state.
        # Round 8 audit catch.
        import os as _os
        _os.replace(str(plugin.path), str(new_path))
        plugin.path = new_path
        plugin.enabled = True
        logger.info("Enabled ASI: %s", plugin.name)

    def disable(self, plugin: AsiPlugin) -> None:
        """Disable an enabled ASI plugin."""
        if not plugin.enabled:
            return
        new_path = plugin.path.with_name(plugin.name + DISABLED_SUFFIX)
        import os as _os
        _os.replace(str(plugin.path), str(new_path))
        plugin.path = new_path
        plugin.enabled = False
        logger.info("Disabled ASI: %s", plugin.name)

    def install(self, source: Path) -> list[str]:
        """Install ASI mod from a file or folder into bin64/.

        Copies .asi, .ini, and ASI loader .dll files.
        Writes a sidecar manifest `<plugin>.cdumm-files.json` listing
        every file copied EXCEPT shared loader DLLs (which other mods
        depend on). Uninstall reads the sidecar to remove exactly the
        files this mod added.

        Returns list of installed file names.
        """
        installed: list[str] = []
        # Files this specific mod owns (excludes shared loader DLLs).
        owned: list[str] = []
        # Plugin name = stem of the .asi we copy. Falls back to the
        # source folder name if multiple .asi end up here (rare).
        plugin_name: str | None = None
        self._bin64.mkdir(parents=True, exist_ok=True)

        # Update-over-disabled handling: when an existing plugin with
        # the same stem is currently disabled, the on-disk file is
        # `<stem>.asi.disabled`. Without intervention, a fresh install
        # writes `<stem>.asi` next to the disabled one, so `scan()`
        # reports two plugins with the same name (one enabled, one
        # disabled). Two fixes happen here:
        #   1. Remove the stale `.asi.disabled` sibling.
        #   2. Land the new payload AS DISABLED so the user's
        #      explicit "I turned this off" choice survives the
        #      update. Returns the actual destination path.
        # Bug from Faisal 2026-04-30.
        def _resolve_dest_for_asi(stem: str, default_name: str) -> Path:
            stale = self._bin64 / f"{stem}{DISABLED_SUFFIX}"
            if stale.exists():
                try:
                    stale.unlink()
                    logger.info(
                        "Removed stale disabled sibling on update: %s",
                        stale.name)
                except OSError as e:
                    logger.warning(
                        "Could not remove stale disabled sibling %s: %s",
                        stale.name, e)
                # Land new payload as disabled to preserve user state.
                return self._bin64 / f"{stem}{DISABLED_SUFFIX}"
            return self._bin64 / default_name

        if source.is_file() and source.suffix.lower() == ASI_SUFFIX:
            dest = _resolve_dest_for_asi(source.stem, source.name)
            shutil.copy2(source, dest)
            installed.append(dest.name)
            owned.append(dest.name)
            plugin_name = source.stem
            for f in source.parent.iterdir():
                if f == source or not f.is_file():
                    continue
                if f.suffix.lower() == ".ini":
                    shutil.copy2(f, self._bin64 / f.name)
                    installed.append(f.name)
                    owned.append(f.name)
                elif f.name.lower() in ASI_LOADER_NAMES:
                    if not (self._bin64 / f.name).exists():
                        shutil.copy2(f, self._bin64 / f.name)
                        installed.append(f.name)
                        # NOT owned — shared between mods.
        elif source.is_dir():
            for f in source.rglob("*"):
                if not f.is_file():
                    continue
                ext = f.suffix.lower()
                if ext == ASI_SUFFIX:
                    dest = _resolve_dest_for_asi(f.stem, f.name)
                    shutil.copy2(f, dest)
                    installed.append(dest.name)
                    owned.append(dest.name)
                    if plugin_name is None:
                        plugin_name = f.stem
                elif ext == ".ini":
                    shutil.copy2(f, self._bin64 / f.name)
                    installed.append(f.name)
                    owned.append(f.name)
                elif f.name.lower() in ASI_LOADER_NAMES:
                    if not (self._bin64 / f.name).exists():
                        shutil.copy2(f, self._bin64 / f.name)
                        installed.append(f.name)
                        # NOT owned — shared between mods.

        if plugin_name and owned:
            sidecar = self._bin64 / f"{plugin_name}{SIDECAR_SUFFIX}"
            try:
                sidecar.write_text(
                    json.dumps({"version": 1, "files": sorted(set(owned))},
                               indent=2),
                    encoding="utf-8",
                )
            except OSError as e:
                logger.warning("Could not write ASI sidecar %s: %s", sidecar, e)

        if installed:
            logger.info("Installed ASI files: %s", installed)
        return installed

    def uninstall(self, plugin: AsiPlugin) -> list[str]:
        """Remove ASI plugin and every file the install recorded.

        Reads the sidecar manifest `<plugin>.cdumm-files.json` to
        determine exactly which files to delete. Falls back to the
        legacy stem-prefix heuristic for plugins installed before
        sidecars existed (so existing installs survive the upgrade).

        Returns list of deleted file names.
        """
        deleted: list[str] = []
        sidecar = self._bin64 / f"{plugin.name}{SIDECAR_SUFFIX}"

        sidecar_files: list[str] | None = None
        if sidecar.exists():
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
                files = data.get("files")
                if isinstance(files, list) and all(isinstance(f, str) for f in files):
                    sidecar_files = files
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Could not read ASI sidecar %s: %s — "
                               "falling back to legacy uninstall", sidecar, e)

        if sidecar_files is not None:
            for name in sidecar_files:
                target = self._bin64 / name
                if target.exists():
                    try:
                        target.unlink()
                        deleted.append(name)
                    except OSError as e:
                        logger.warning("Could not delete %s: %s", target, e)
            try:
                sidecar.unlink()
            except OSError:
                pass
            # The .asi might be on disk under the disabled name. The
            # sidecar tracks only the as-installed name, so cover both.
            disabled = self._bin64 / (plugin.name + DISABLED_SUFFIX)
            if disabled.exists():
                try:
                    disabled.unlink()
                    deleted.append(disabled.name)
                except OSError:
                    pass
        else:
            # Legacy path: pre-sidecar install. Delete the .asi and
            # any .ini whose stem starts with the plugin name.
            if plugin.path.exists():
                plugin.path.unlink()
                deleted.append(plugin.path.name)
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

        Accepts a single .asi file, or a folder (searches recursively).
        Copies .asi and all companion .ini files. Returns list of updated file names.
        """
        updated: list[str] = []
        self._bin64.mkdir(parents=True, exist_ok=True)

        if source.is_file() and source.suffix.lower() == ASI_SUFFIX:
            dest = self._bin64 / (plugin.name + ASI_SUFFIX)
            if not plugin.enabled:
                dest = self._bin64 / (plugin.name + DISABLED_SUFFIX)
            shutil.copy2(source, dest)
            updated.append(dest.name)
            # Copy all .ini files from the same directory
            for ini in source.parent.glob("*.ini"):
                shutil.copy2(ini, self._bin64 / ini.name)
                updated.append(ini.name)
        elif source.is_dir():
            for f in source.rglob("*"):
                if not f.is_file():
                    continue
                if f.suffix.lower() == ASI_SUFFIX:
                    dest = self._bin64 / (plugin.name + ASI_SUFFIX)
                    if not plugin.enabled:
                        dest = self._bin64 / (plugin.name + DISABLED_SUFFIX)
                    shutil.copy2(f, dest)
                    updated.append(dest.name)
                elif f.suffix.lower() == ".ini":
                    shutil.copy2(f, self._bin64 / f.name)
                    updated.append(f.name)

        if updated:
            logger.info("Updated ASI: %s (%s)", plugin.name, updated)
        return updated

    @staticmethod
    def contains_asi(path: Path) -> bool:
        """Check if a path contains ASI plugin files (searches subdirectories and archives)."""
        if path.is_file():
            if path.suffix.lower() == ASI_SUFFIX:
                return True
            # Check inside zip files
            if path.suffix.lower() == ".zip":
                import zipfile
                try:
                    with zipfile.ZipFile(path) as zf:
                        return any(n.lower().endswith(ASI_SUFFIX) for n in zf.namelist())
                except (zipfile.BadZipFile, Exception):
                    return False
            # Check inside 7z files
            if path.suffix.lower() == ".7z":
                try:
                    import py7zr
                    with py7zr.SevenZipFile(path, 'r') as zf:
                        return any(n.lower().endswith(ASI_SUFFIX) for n in zf.getnames())
                except Exception:
                    return False
        if path.is_dir():
            return any(path.rglob(f"*{ASI_SUFFIX}"))
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
        # Reverse fallback: the .asi stem starts with an INI stem.
        # Handles authors who bake the version into the .asi filename
        # but keep the .ini name stable so the user's existing config
        # carries across updates (e.g. EnhancedFlightv31.asi reads
        # EnhancedFlight.ini). Pick the LONGEST matching INI stem and
        # skip INIs that already belong to a different .asi plugin.
        best: Path | None = None
        best_len = 0
        for f in self._bin64.iterdir():
            if f.suffix.lower() != ".ini":
                continue
            ini_stem = f.stem.lower()
            if len(ini_stem) < 4 or ini_stem == stem:
                continue
            if not stem.startswith(ini_stem):
                continue
            # Don't steal another plugin's INI: skip when an .asi (or
            # .asi.disabled) with this exact stem also lives in bin64.
            sibling_asi = self._bin64 / (f.stem + ASI_SUFFIX)
            sibling_disabled = self._bin64 / (f.stem + DISABLED_SUFFIX)
            if sibling_asi.exists() or sibling_disabled.exists():
                continue
            if len(ini_stem) > best_len:
                best = f
                best_len = len(ini_stem)
        return best

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
