"""UpdateDownloadWorker must write atomically.

GitHub #147 (cde2496): "Failed to load Python DLL python314.dll.
LoadLibrary: The specified module could not be found." after a CDUMM
self-update. The PyInstaller-frozen exe carries its embedded Python
DLL inside the exe; the error fires when the exe is partially written
and the embedded archive is incomplete or truncated. The fix is to
download into ``<dest>.part`` and ``os.replace`` it onto the final
path only on full success, so a failed or aborted run never leaves a
half-written file under ``CDUMM3.exe`` for the user to launch.
"""
from __future__ import annotations

from pathlib import Path


def _patch_urlopen(monkeypatch, behaviour):
    """Replace urllib.request.urlopen for the UpdateDownloadWorker
    module so the worker streams from ``behaviour`` instead of HTTP."""
    import cdumm.engine.update_checker as uc

    monkeypatch.setattr(uc.urllib.request, "urlopen",
                        lambda *a, **kw: behaviour)


class _StreamCM:
    """Minimal context-manager wrapper around a read sequence."""
    def __init__(self, content_length: str | None, chunks, raise_after=None):
        self._chunks = list(chunks)
        self._raise_after = raise_after
        self._delivered = 0
        self.headers = {"Content-Length": content_length} if content_length else {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n):
        if (self._raise_after is not None
                and self._delivered >= self._raise_after):
            raise OSError("simulated connection drop")
        if not self._chunks:
            return b""
        block = self._chunks.pop(0)
        self._delivered += len(block)
        return block


def test_failed_download_does_not_overwrite_existing_file(
        tmp_path, monkeypatch):
    """A download that errors midway must leave the existing file at
    ``dest`` untouched. The .part file must be cleaned up too."""
    from cdumm.engine.update_checker import UpdateDownloadWorker

    dest = tmp_path / "CDUMM3.exe"
    dest.write_bytes(b"REAL_EXISTING_EXE_BYTES")

    stream = _StreamCM(
        content_length="1024",
        chunks=[b"\x00" * 512],
        raise_after=512,
    )
    _patch_urlopen(monkeypatch, stream)

    worker = UpdateDownloadWorker("https://example.invalid/x", str(dest))
    worker.run()

    assert dest.read_bytes() == b"REAL_EXISTING_EXE_BYTES", (
        "A failed download MUST NOT overwrite the existing exe")
    assert not (tmp_path / "CDUMM3.exe.part").exists(), (
        "The .part file MUST be cleaned up after a failed run")


def test_successful_download_replaces_dest_atomically(
        tmp_path, monkeypatch):
    """A clean run writes the full payload to ``dest`` and removes the
    .part file. An older file at ``dest`` is replaced atomically."""
    from cdumm.engine.update_checker import UpdateDownloadWorker

    dest = tmp_path / "CDUMM3.exe"
    dest.write_bytes(b"OLD_EXE")
    payload = b"\xAB" * 4096

    stream = _StreamCM(
        content_length=str(len(payload)),
        chunks=[payload],
    )
    _patch_urlopen(monkeypatch, stream)

    worker = UpdateDownloadWorker("https://example.invalid/x", str(dest))
    worker.run()

    assert dest.read_bytes() == payload
    assert not (tmp_path / "CDUMM3.exe.part").exists()


def test_no_dest_file_yet_failed_download_leaves_no_artifact(
        tmp_path, monkeypatch):
    """If the user has no existing exe at ``dest`` and the download
    fails, neither ``dest`` nor ``dest.part`` should remain. The user
    sees their original (absent) state preserved."""
    from cdumm.engine.update_checker import UpdateDownloadWorker

    dest = tmp_path / "CDUMM3.exe"
    assert not dest.exists()

    stream = _StreamCM(
        content_length=None,
        chunks=[b"\x00" * 256],
        raise_after=256,
    )
    _patch_urlopen(monkeypatch, stream)

    worker = UpdateDownloadWorker("https://example.invalid/x", str(dest))
    worker.run()

    assert not dest.exists()
    assert not (tmp_path / "CDUMM3.exe.part").exists()
