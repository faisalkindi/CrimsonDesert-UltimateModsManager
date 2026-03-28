"""Check GitHub for new CDUMM releases and auto-update."""
import json
import logging
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

GITHUB_REPO = "faisalkindi/CrimsonDesert-UltimateModsManager"
RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


def check_for_update(current_version: str) -> dict | None:
    """Check if a newer version exists on GitHub.

    Returns {"tag": "v1.0.0", "url": "...", "body": "...", "download_url": "..."} or None.
    """
    try:
        req = urllib.request.Request(RELEASES_URL, headers={"User-Agent": "CDUMM"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        tag = data.get("tag_name", "")
        remote = tag.lstrip("v")
        local = current_version.lstrip("v")
        if _version_newer(remote, local):
            # Find the .exe asset download URL
            download_url = ""
            for asset in data.get("assets", []):
                if asset.get("name", "").lower().endswith(".exe"):
                    download_url = asset.get("browser_download_url", "")
                    break
            return {
                "tag": tag,
                "url": data.get("html_url", ""),
                "body": data.get("body", "")[:500],
                "download_url": download_url,
            }
    except Exception as e:
        logger.debug("Update check failed (non-fatal): %s", e)
    return None


def download_update(download_url: str, progress_callback=None) -> Path | None:
    """Download the new exe to a temp file. Returns path or None on failure."""
    try:
        req = urllib.request.Request(download_url, headers={"User-Agent": "CDUMM"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            tmp = tempfile.NamedTemporaryFile(suffix=".exe", delete=False, dir=tempfile.gettempdir())
            downloaded = 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                tmp.write(chunk)
                downloaded += len(chunk)
                if progress_callback and total > 0:
                    progress_callback(int(downloaded / total * 100))
            tmp.close()
            logger.info("Downloaded update to %s (%d bytes)", tmp.name, downloaded)
            return Path(tmp.name)
    except Exception as e:
        logger.error("Update download failed: %s", e)
        return None


def apply_update(new_exe: Path) -> None:
    """Replace the current exe with the new one and relaunch.

    Creates a batch script that:
    1. Waits for the current process to exit
    2. Replaces the exe
    3. Relaunches the app
    4. Cleans up the batch file
    """
    if not getattr(sys, 'frozen', False):
        logger.warning("Cannot auto-update when running from source")
        return

    current_exe = Path(sys.executable)
    bat = tempfile.NamedTemporaryFile(suffix=".bat", delete=False, mode="w",
                                      dir=tempfile.gettempdir())
    current_dir = current_exe.parent
    bat.write(f"""@echo off
echo Updating CDUMM...
echo Waiting for app to close...
timeout /t 3 /nobreak >nul
:wait
tasklist /fi "PID eq {os.getpid()}" 2>nul | find "{os.getpid()}" >nul
if not errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto wait
)
echo Copying new version...
copy /y "{new_exe}" "{current_exe}"
if errorlevel 1 (
    echo Copy failed, retrying...
    timeout /t 2 /nobreak >nul
    copy /y "{new_exe}" "{current_exe}"
)
del "{new_exe}" 2>nul
echo Launching updated app...
timeout /t 2 /nobreak >nul
cd /d "{current_dir}"
start "" "{current_exe}"
timeout /t 3 /nobreak >nul
del "%~f0"
""")
    bat.close()
    logger.info("Launching updater: %s", bat.name)
    os.startfile(bat.name)
    sys.exit(0)


def _version_newer(remote: str, local: str) -> bool:
    """Compare version strings like '0.8.1' > '0.7.9'."""
    try:
        r = tuple(int(x) for x in remote.split("."))
        l = tuple(int(x) for x in local.split("."))
        return r > l
    except (ValueError, AttributeError):
        return False


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
    """Background worker for downloading the update."""
    progress_updated = Signal(int, str)
    finished = Signal(object)  # Path to downloaded exe or None
    error_occurred = Signal(str)

    def __init__(self, download_url: str) -> None:
        super().__init__()
        self._url = download_url

    def run(self) -> None:
        try:
            def on_progress(pct):
                self.progress_updated.emit(pct, f"Downloading update... {pct}%")
            path = download_update(self._url, progress_callback=on_progress)
            self.finished.emit(path)
        except Exception as e:
            self.error_occurred.emit(str(e))
