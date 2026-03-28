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
        with urllib.request.urlopen(req, timeout=300) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            tmp = tempfile.NamedTemporaryFile(suffix=".exe", delete=False, dir=tempfile.gettempdir())
            downloaded = 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                tmp.write(chunk)
                downloaded += len(chunk)
                if progress_callback:
                    if total > 0:
                        progress_callback(int(downloaded / total * 100))
                    else:
                        mb = downloaded / (1024 * 1024)
                        progress_callback(min(95, int(mb)))  # show MB as rough progress
            tmp.close()
            if downloaded < 1_000_000:
                logger.error("Download too small (%d bytes), likely failed", downloaded)
                Path(tmp.name).unlink(missing_ok=True)
                return None
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
    current_dir = current_exe.parent

    ps1 = tempfile.NamedTemporaryFile(suffix=".ps1", delete=False, mode="w",
                                       dir=tempfile.gettempdir())
    exe_name = current_exe.stem  # "CDUMM"
    ps1.write(f"""
Write-Host "Updating {exe_name}..."
Write-Host ""

# Step 1: Wait for old process to fully exit
Write-Host "Closing {exe_name}..."
Start-Sleep -Seconds 2
Stop-Process -Name "{exe_name}" -Force -ErrorAction SilentlyContinue
# Wait until no process with that name exists
while (Get-Process -Name "{exe_name}" -ErrorAction SilentlyContinue) {{
    Write-Host "Still running, waiting..."
    Start-Sleep -Seconds 1
}}
Write-Host "Process closed."
Start-Sleep -Seconds 5

# Step 2: Replace exe
$src = "{new_exe}"
$dst = "{current_exe}"
$success = $false
for ($i = 0; $i -lt 10; $i++) {{
    try {{
        Copy-Item -Path $src -Destination $dst -Force -ErrorAction Stop
        $success = $true
        Write-Host "Update installed."
        break
    }} catch {{
        Write-Host "File locked, retrying... ($($i+1)/10)"
        Start-Sleep -Seconds 2
    }}
}}

if (-not $success) {{
    Write-Host ""
    Write-Host "ERROR: Could not replace the exe. Please close any programs using it and try again."
    Read-Host "Press Enter to exit"
    exit 1
}}

Remove-Item -Path $src -Force -ErrorAction SilentlyContinue

# Step 3: Done — user relaunches manually
Write-Host ""
Write-Host "Update complete! Please relaunch {exe_name}."
Write-Host ""
Start-Sleep -Seconds 2
Remove-Item -Path $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
""")
    ps1.close()
    logger.info("Launching updater: %s", ps1.name)
    import subprocess
    subprocess.Popen(
        ["powershell", "-ExecutionPolicy", "Bypass", "-File", ps1.name],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    # Clean up lock file before exiting so next launch doesn't think we crashed
    lock_file = Path.home() / "AppData" / "Local" / "cdumm" / ".running"
    if lock_file.exists():
        lock_file.unlink()
    # Kill the process so the exe is fully released for the updater
    import time
    time.sleep(1)
    os._exit(0)


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
