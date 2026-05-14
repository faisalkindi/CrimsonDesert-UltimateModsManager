"""Tests for the Linux ``nxm://`` handler in ``cdumm.engine.nxm_handler``.

The Linux registration path:

1. Writes ``$XDG_DATA_HOME/applications/cdumm-nxm.desktop``.
2. Runs ``xdg-mime default cdumm-nxm.desktop x-scheme-handler/nxm``.
3. Runs ``update-desktop-database`` (best-effort, optional).

Unregister deletes the .desktop file and strips the corresponding
line from ``$XDG_CONFIG_HOME/mimeapps.list``.

These tests monkeypatch ``subprocess.run`` and the XDG env vars so the
suite runs on any host — including the upstream Windows regression
box — without touching the real freedesktop state. The fake
``subprocess.run`` records every invocation so each assertion can
check the exact command line that would have been sent to xdg-mime.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from cdumm.engine import nxm_handler


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def linux_xdg(tmp_path: Path, monkeypatch):
    """Isolate XDG_DATA_HOME / XDG_CONFIG_HOME under tmp_path so the
    .desktop write and mimeapps.list edit don't touch the user's
    real freedesktop state. Also flips IS_LINUX/IS_WINDOWS so the
    Linux branches run regardless of the actual host OS."""
    xdg_data = tmp_path / "data"
    xdg_config = tmp_path / "config"
    xdg_data.mkdir()
    xdg_config.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setattr(nxm_handler, "IS_LINUX", True)
    monkeypatch.setattr(nxm_handler, "IS_WINDOWS", False)
    yield {"data": xdg_data, "config": xdg_config}


class FakeSubprocess:
    """Stand-in for ``subprocess.run`` that records every invocation
    and lets a test simulate the response of ``xdg-mime query
    default x-scheme-handler/nxm``. Real xdg-mime is unavailable on
    Windows CI; tests on Linux developers' boxes mustn't mutate the
    actual user mimeapps either, so we never want to call through."""

    def __init__(self, current_handler: str = ""):
        self.calls: list[list[str]] = []
        self.current_handler = current_handler

    def __call__(self, args, **kwargs) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        stdout = ""
        if (len(args) >= 4
                and args[0] == "xdg-mime"
                and args[1] == "query"
                and args[2] == "default"):
            stdout = self.current_handler
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=stdout, stderr="")


@pytest.fixture
def fake_subprocess(monkeypatch):
    """Patches both ``subprocess.run`` references the module touches:
    the top-level ``subprocess.run`` import via ``subprocess``, and
    the symbol bound inside nxm_handler. Tests get a ``FakeSubprocess``
    instance back to inspect calls / set the simulated current
    handler."""
    fake = FakeSubprocess()
    monkeypatch.setattr(nxm_handler.subprocess, "run", fake)
    return fake


# ── register_linux_handler ──────────────────────────────────────────


class TestRegisterLinuxHandler:
    """Writing the .desktop file, calling xdg-mime, refusing to
    stomp on an existing handler."""

    def test_writes_desktop_file_with_correct_content(
            self, linux_xdg, fake_subprocess) -> None:
        ok = nxm_handler.register_linux_handler()
        assert ok is True

        desktop = (linux_xdg["data"] / "applications"
                   / "cdumm-nxm.desktop")
        assert desktop.exists()
        body = desktop.read_text(encoding="utf-8")
        assert "[Desktop Entry]" in body
        # MimeType binds the nxm:// scheme — this is what xdg-mime
        # actually reads. Pin the exact freedesktop token.
        assert "MimeType=x-scheme-handler/nxm;" in body
        # NoDisplay keeps the entry out of the Activities/menu —
        # CDUMM proper has its own desktop entry.
        assert "NoDisplay=true" in body
        # Exec must contain the URL placeholder so xdg-open passes
        # the nxm:// URL through when the user clicks Mod Manager
        # Download.
        assert "%u" in body
        assert "--nxm" in body

    def test_marks_desktop_file_executable(
            self, linux_xdg, fake_subprocess) -> None:
        """Some desktop environments (Plasma 5 historically) skip
        .desktop files without the executable bit. Mirror what an
        installer would do."""
        nxm_handler.register_linux_handler()
        desktop = (linux_xdg["data"] / "applications"
                   / "cdumm-nxm.desktop")
        assert desktop.stat().st_mode & 0o111  # any exec bit set

    def test_runs_xdg_mime_default_with_our_desktop_filename(
            self, linux_xdg, fake_subprocess) -> None:
        nxm_handler.register_linux_handler()
        # Look for the xdg-mime default call. Other calls (query,
        # update-desktop-database) are also expected.
        default_calls = [
            c for c in fake_subprocess.calls
            if c[:2] == ["xdg-mime", "default"]]
        assert len(default_calls) == 1
        assert default_calls[0] == [
            "xdg-mime", "default",
            "cdumm-nxm.desktop", "x-scheme-handler/nxm"]

    def test_refuses_to_overwrite_existing_handler_without_force(
            self, linux_xdg, fake_subprocess) -> None:
        """Vortex / MO2 / any other already-registered handler must
        not be silently displaced. With ``force=False`` the function
        returns False and writes nothing."""
        fake_subprocess.current_handler = "net.nexusmods.vortex.desktop"
        ok = nxm_handler.register_linux_handler(force=False)
        assert ok is False
        desktop = (linux_xdg["data"] / "applications"
                   / "cdumm-nxm.desktop")
        assert not desktop.exists()
        # No xdg-mime *default* call should have happened — only the
        # initial query that detected the existing handler.
        default_calls = [
            c for c in fake_subprocess.calls
            if c[:2] == ["xdg-mime", "default"]]
        assert default_calls == []

    def test_force_overrides_existing_handler(
            self, linux_xdg, fake_subprocess) -> None:
        """``force=True`` is the explicit-user-opt-in path from the
        Settings page confirmation dialog. It must write the file
        and call xdg-mime even when another handler is registered."""
        fake_subprocess.current_handler = "net.nexusmods.vortex.desktop"
        ok = nxm_handler.register_linux_handler(force=True)
        assert ok is True
        desktop = (linux_xdg["data"] / "applications"
                   / "cdumm-nxm.desktop")
        assert desktop.exists()

    def test_skips_when_xdg_data_home_unwritable(
            self, linux_xdg, fake_subprocess, monkeypatch) -> None:
        """Read-only home / immutable XDG dir / permission-denied is
        rare but real (locked-down corporate Linux boxes). Must not
        raise — log + return False."""
        apps_dir = linux_xdg["data"] / "applications"
        apps_dir.mkdir(parents=True, exist_ok=True)

        def fail_write_text(*args, **kwargs):
            raise OSError("simulated EACCES")

        monkeypatch.setattr(Path, "write_text", fail_write_text)
        assert nxm_handler.register_linux_handler() is False


# ── unregister_linux_handler ────────────────────────────────────────


class TestUnregisterLinuxHandler:

    def test_deletes_desktop_file_when_we_own_the_scheme(
            self, linux_xdg, fake_subprocess) -> None:
        # Pre-stage as if we registered earlier.
        fake_subprocess.current_handler = "cdumm-nxm.desktop"
        apps_dir = linux_xdg["data"] / "applications"
        apps_dir.mkdir()
        desktop = apps_dir / "cdumm-nxm.desktop"
        desktop.write_text("[Desktop Entry]\nName=CDUMM\n")

        ok = nxm_handler.unregister_linux_handler()
        assert ok is True
        assert not desktop.exists()

    def test_refuses_when_another_app_owns_the_scheme(
            self, linux_xdg, fake_subprocess) -> None:
        """Pre-condition: somehow our .desktop is on disk but
        xdg-mime now reports a different default handler (user
        registered Vortex after us via Vortex's UI). Removing our
        file is fine, but we must not delete an unrelated app's
        registration. Defence in depth — the function refuses
        entirely so the operator can investigate."""
        fake_subprocess.current_handler = "net.nexusmods.vortex.desktop"
        apps_dir = linux_xdg["data"] / "applications"
        apps_dir.mkdir()
        desktop = apps_dir / "cdumm-nxm.desktop"
        desktop.write_text("[Desktop Entry]\nName=CDUMM\n")

        ok = nxm_handler.unregister_linux_handler()
        assert ok is False
        # File NOT removed — current handler isn't us, so unregister
        # bailed before touching anything.
        assert desktop.exists()

    def test_strips_mimeapps_list_entry_pointing_at_us(
            self, linux_xdg, fake_subprocess) -> None:
        """mimeapps.list has [Default Applications] / [Added
        Associations] sections. ``xdg-mime default`` writes
        ``x-scheme-handler/nxm=cdumm-nxm.desktop`` under one of them;
        unregister must drop that line so a later
        ``is_handler_registered()`` call doesn't see a stale
        reference. Other lines (unrelated mime types, other
        handlers) must survive untouched."""
        fake_subprocess.current_handler = "cdumm-nxm.desktop"
        mimeapps = linux_xdg["config"] / "mimeapps.list"
        mimeapps.write_text(
            "[Default Applications]\n"
            "x-scheme-handler/nxm=cdumm-nxm.desktop;\n"
            "application/pdf=org.gnome.Evince.desktop;\n"
            "text/html=firefox.desktop;\n",
            encoding="utf-8")
        # Pre-create the .desktop so the unlink path succeeds.
        apps = linux_xdg["data"] / "applications"
        apps.mkdir()
        (apps / "cdumm-nxm.desktop").write_text("[Desktop Entry]\n")

        nxm_handler.unregister_linux_handler()

        rewritten = mimeapps.read_text(encoding="utf-8")
        assert "cdumm-nxm.desktop" not in rewritten
        # Unrelated lines are preserved.
        assert "org.gnome.Evince.desktop" in rewritten
        assert "firefox.desktop" in rewritten

    def test_leaves_mimeapps_lines_for_other_handlers_alone(
            self, linux_xdg, fake_subprocess) -> None:
        """If a future Vortex Flatpak install rewrites the
        x-scheme-handler/nxm line to point at *itself* after we
        registered, an attempt to unregister ours must NOT remove
        Vortex's binding — we'd be stripping a third party's
        registration without their consent."""
        fake_subprocess.current_handler = "net.nexusmods.vortex.desktop"
        mimeapps = linux_xdg["config"] / "mimeapps.list"
        mimeapps.write_text(
            "[Default Applications]\n"
            "x-scheme-handler/nxm=net.nexusmods.vortex.desktop;\n",
            encoding="utf-8")

        ok = nxm_handler.unregister_linux_handler()
        # Function bails out (current handler isn't ours) so the
        # mimeapps file is never touched.
        assert ok is False
        assert ("x-scheme-handler/nxm=net.nexusmods.vortex.desktop"
                in mimeapps.read_text(encoding="utf-8"))


# ── is_handler_registered + dispatcher ──────────────────────────────


class TestIsHandlerRegisteredLinux:

    def test_true_when_xdg_mime_returns_our_desktop_filename(
            self, linux_xdg, fake_subprocess) -> None:
        fake_subprocess.current_handler = "cdumm-nxm.desktop"
        assert nxm_handler.is_handler_registered() is True

    def test_false_when_xdg_mime_returns_different_handler(
            self, linux_xdg, fake_subprocess) -> None:
        fake_subprocess.current_handler = "net.nexusmods.vortex.desktop"
        assert nxm_handler.is_handler_registered() is False

    def test_false_when_nothing_registered(
            self, linux_xdg, fake_subprocess) -> None:
        fake_subprocess.current_handler = ""
        assert nxm_handler.is_handler_registered() is False

    def test_false_when_xdg_mime_missing(
            self, linux_xdg, monkeypatch) -> None:
        """``xdg-mime`` not on PATH (minimal container, NixOS without
        ``xdg-utils``) raises ``FileNotFoundError``. Must not crash
        the Settings page — treat as "no handler"."""
        def missing_subprocess(*args, **kwargs):
            raise FileNotFoundError("xdg-mime")
        monkeypatch.setattr(
            nxm_handler.subprocess, "run", missing_subprocess)
        assert nxm_handler.is_handler_registered() is False


# ── existing_handler_description ────────────────────────────────────


class TestExistingHandlerDescription:

    def test_returns_xdg_mime_output_on_linux(
            self, linux_xdg, fake_subprocess) -> None:
        fake_subprocess.current_handler = "net.nexusmods.vortex.desktop"
        assert (nxm_handler.existing_handler_description()
                == "net.nexusmods.vortex.desktop")

    def test_returns_none_when_no_handler_registered(
            self, linux_xdg, fake_subprocess) -> None:
        fake_subprocess.current_handler = ""
        assert nxm_handler.existing_handler_description() is None


# ── generic dispatchers ─────────────────────────────────────────────


class TestGenericDispatchersLinux:

    def test_register_handler_dispatches_to_linux(
            self, linux_xdg, fake_subprocess) -> None:
        ok = nxm_handler.register_handler()
        assert ok is True
        desktop = (linux_xdg["data"] / "applications"
                   / "cdumm-nxm.desktop")
        assert desktop.exists()

    def test_unregister_handler_dispatches_to_linux(
            self, linux_xdg, fake_subprocess) -> None:
        fake_subprocess.current_handler = "cdumm-nxm.desktop"
        apps = linux_xdg["data"] / "applications"
        apps.mkdir()
        (apps / "cdumm-nxm.desktop").write_text("[Desktop Entry]\n")
        ok = nxm_handler.unregister_handler()
        assert ok is True
