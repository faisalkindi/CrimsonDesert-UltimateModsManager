"""Check GitHub for new CDUMM releases."""
import json
import logging
import sys
import urllib.request

from PySide6.QtCore import QObject, Signal

from cdumm.engine.ssl_ctx import make_ssl_context

logger = logging.getLogger(__name__)

GITHUB_REPO = "faisalkindi/CrimsonDesert-UltimateModsManager"
RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# Canonical Windows asset name produced by the release workflow. Stable across
# versions because the spec file always emits ``CDUMM3.exe`` (see cdumm.spec).
WINDOWS_ASSET = "CDUMM3.exe"


def _release_asset_url(version: str, asset: str) -> str:
    """Build the canonical GitHub direct-download URL for a release asset.

    ``version`` may be passed with or without a leading ``v`` — the result
    always contains exactly one ``v`` prefix because that's how GitHub tags
    the releases (matches the ``v*`` trigger in the workflows).
    """
    v = version.lstrip("v")
    return (f"https://github.com/{GITHUB_REPO}"
            f"/releases/download/v{v}/{asset}")


def macos_asset_name(version: str) -> str:
    """Name of the macOS DMG attached to release ``version``.

    Mirrors ``DMG_NAME`` in scripts/build-macos.sh and the artifact path in
    .github/workflows/release-macos.yml — ``CDUMM-<version>-macos-arm64.dmg``.
    """
    return f"CDUMM-{version.lstrip('v')}-macos-arm64.dmg"


def asset_for_current_platform(version: str) -> str | None:
    """Return the asset filename that matches the running platform, or
    ``None`` if there's no first-class direct-download for it.

    Linux currently has no signed release asset — the banner falls back to
    the GitHub release page in that case (current behaviour).
    """
    if sys.platform == "win32":
        return WINDOWS_ASSET
    if sys.platform == "darwin":
        return macos_asset_name(version)
    return None


def check_for_update(current_version: str) -> dict | None:
    """Check if a newer version exists on GitHub.

    Returns {"tag": "v1.0.0", "url": "...", "body": "..."} or None.
    The url points to the GitHub releases page (not a direct download).
    """
    try:
        req = urllib.request.Request(RELEASES_URL, headers={"User-Agent": "CDUMM"})
        with urllib.request.urlopen(req, timeout=10, context=make_ssl_context()) as resp:
            data = json.loads(resp.read())
        tag = data.get("tag_name", "")
        remote = tag.lstrip("v")
        local = current_version.lstrip("v")
        if _version_newer(remote, local):
            return {
                "tag": tag,
                "url": data.get("html_url", ""),
                "body": data.get("body", "")[:500],
            }
    except Exception as e:
        logger.debug("Update check failed (non-fatal): %s", e)
    return None


def _version_newer(remote: str, local: str) -> bool:
    """Compare version strings like '0.8.1' > '0.7.9'.

    Reuses ``nexus_api._version_to_tuple`` (semver-aware: handles
    ``v`` prefixes, pre-release tags, trailing-zero normalization)
    instead of a bare int-split. The int-split raised ValueError on
    tags like ``v3.3.1-hotfix``, which silently suppressed the
    update banner for every user until the next plain tag.
    """
    from cdumm.engine.nexus_api import _version_to_tuple
    try:
        r = _version_to_tuple(remote)
        l = _version_to_tuple(local)
    except (ValueError, AttributeError):
        return False
    if r is None or l is None:
        return False
    return r > l


class UpdateCheckWorker(QObject):
    """Background worker for update check."""
    update_available = Signal(dict)
    finished = Signal()

    def __init__(self, current_version: str) -> None:
        super().__init__()
        self._version = current_version

    def run(self) -> None:
        result = check_for_update(self._version)
        if result:
            self.update_available.emit(result)
        self.finished.emit()


class UpdateDownloadWorker(QObject):
    """Background worker that streams a release asset to ``dest_path``.

    Emits ``progress(received, total)`` with byte counts (``total`` may be
    -1 if the server omits Content-Length), then exactly one of
    ``done(path)`` or ``failed(reason)``. Network errors are caught and
    surfaced via ``failed`` so the GUI can fall back to the release page
    without the banner taking down the app.
    """
    progress = Signal(int, int)
    done = Signal(str)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, url: str, dest_path: str) -> None:
        super().__init__()
        self._url = url
        self._dest = dest_path

    def run(self) -> None:
        # Atomic-write target: download into ``<dest>.part`` and rename
        # only on full success. A half-finished .part file cannot be
        # mistaken for the real CDUMM3.exe, so if the worker is killed,
        # the network drops, the antivirus pauses the stream, or the
        # user closes CDUMM mid-download, the user's existing exe is
        # untouched. Pre-fix, a partial overwrite produced an exe that
        # failed to load its embedded Python DLL on launch
        # (cde2496 GitHub #147: "Failed to load Python DLL
        # python314.dll. LoadLibrary: The specified module could not
        # be found.").
        import os as _os
        tmp_dest = self._dest + ".part"
        try:
            req = urllib.request.Request(
                self._url, headers={"User-Agent": "CDUMM"})
            with urllib.request.urlopen(req, timeout=30, context=make_ssl_context()) as resp:
                total = int(resp.headers.get("Content-Length", -1) or -1)
                received = 0
                # 64 KiB chunks — small enough that progress signals fire
                # several times per second on slow connections, large
                # enough that we don't spam the GUI thread on fast ones.
                chunk = 64 * 1024
                with open(tmp_dest, "wb") as f:
                    while True:
                        block = resp.read(chunk)
                        if not block:
                            break
                        f.write(block)
                        received += len(block)
                        self.progress.emit(received, total)
            try:
                _os.replace(tmp_dest, self._dest)
            except PermissionError as e_replace:
                # #170 (Elec0 / devCKVargas / AwfulLon): when the
                # destination is the running CDUMM exe (Windows
                # default Downloads location matches the live exe
                # path), Windows refuses to overwrite it with
                # [WinError 5] Access is denied. Windows DOES allow
                # renaming a running exe out of the way, so we park
                # the live exe at <dest>.old and replace. main.py
                # cleans up <dest>.old on the next launch. macOS and
                # Linux do not hit this case (their kernels allow
                # overwriting a running binary outright), so re-raise
                # elsewhere.
                if sys.platform != "win32":
                    raise
                backup = self._dest + ".old"
                try:
                    if _os.path.exists(backup):
                        _os.unlink(backup)
                except OSError as e_clean:
                    logger.debug(
                        "Self-replace fallback: stale .old cleanup "
                        "failed (%s), continuing", e_clean)
                try:
                    _os.rename(self._dest, backup)
                except OSError as e_rename:
                    # Last-resort: the running exe is genuinely
                    # unlocked (or the user moved it). Re-raise the
                    # original PermissionError so the caller falls
                    # back to opening the release page.
                    logger.warning(
                        "Self-replace fallback: rename live exe to "
                        ".old failed (%s); surfacing original error",
                        e_rename)
                    raise e_replace
                _os.replace(tmp_dest, self._dest)
                logger.info(
                    "Self-replace fallback used: parked live exe at "
                    "%s, new exe in place at %s", backup, self._dest)
            self.done.emit(self._dest)
        except Exception as e:
            logger.warning("Direct download failed: %s", e)
            # Clean up the partial file so a later retry starts fresh
            # and the user does not see a stray .part next to their exe.
            try:
                if _os.path.exists(tmp_dest):
                    _os.unlink(tmp_dest)
            except Exception:
                pass
            self.failed.emit(str(e))
        finally:
            self.finished.emit()
