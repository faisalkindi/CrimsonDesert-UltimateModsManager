"""Check GitHub for new CDUMM releases."""
import json
import logging
import urllib.request

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

GITHUB_REPO = "faisalkindi/CrimsonDesert-UltimateModsManager"
RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


def check_for_update(current_version: str) -> dict | None:
    """Check if a newer version exists on GitHub.

    Returns {"tag": "v1.0.0", "url": "...", "body": "..."} or None.
    The url points to the GitHub releases page (not a direct download).
    """
    try:
        req = urllib.request.Request(RELEASES_URL, headers={"User-Agent": "CDUMM"})
        with urllib.request.urlopen(req, timeout=10) as resp:
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
