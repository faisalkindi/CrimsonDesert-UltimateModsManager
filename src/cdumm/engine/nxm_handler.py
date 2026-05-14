"""``nxm://`` URL protocol handler — parse + per-OS registration.

NexusMods uses the ``nxm://`` URL scheme to route "Mod Manager Download"
button clicks from the website into a registered desktop application.
The URL format (verified from Vortex issue #21439 and the node-nexus-api
parser) is::

    nxm://{game_domain}/mods/{mod_id}/files/{file_id}?key=X&expires=Y&user_id=Z

Query parameters ``key`` + ``expires`` are the gate that lets free-tier
users get a one-shot download URL (without them, the API rejects with
HTTP 403 "premium only"). ``user_id`` is informational. Collection
downloads carry ``campaign=collection`` instead.

This module:

1. :func:`parse_nxm_url` — validates + tokenizes an incoming URL.
2. :func:`register_handler` / :func:`unregister_handler` — generic
   per-platform dispatchers. Callers should prefer these over the
   ``_windows_`` / ``_linux_`` variants.
3. :func:`register_windows_handler` — writes the HKCU\\Software\\Classes
   entries needed so Windows hands ``nxm://...`` URLs to CDUMM.
4. :func:`register_linux_handler` — writes a freedesktop ``.desktop``
   file under ``~/.local/share/applications`` and associates the
   ``x-scheme-handler/nxm`` MIME type via ``xdg-mime default``.
5. :func:`is_handler_registered` — reports whether CDUMM is currently
   the registered handler on this platform (so we don't stomp on
   Vortex/MO2 without asking the user).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from cdumm.platform import IS_LINUX, IS_WINDOWS

logger = logging.getLogger(__name__)

EXPECTED_GAME_DOMAIN = "crimsondesert"


@dataclass(frozen=True)
class NxmUrl:
    """Structured ``nxm://`` URL. Game always ``crimsondesert`` for CDUMM."""
    game_domain: str
    mod_id: int
    file_id: int
    key: str | None
    expires: int | None
    user_id: int | None
    campaign: str | None


class NxmUrlError(ValueError):
    """Raised when a URL doesn't match the ``nxm://game/mods/N/files/N`` shape."""


def parse_nxm_url(url: str) -> NxmUrl:
    """Parse an ``nxm://...`` URL into its components.

    Accepts: ``nxm://crimsondesert/mods/{mod_id}/files/{file_id}?...``
    Rejects non-nxm schemes or wrong game domains with
    :class:`NxmUrlError`.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() != "nxm":
        raise NxmUrlError(f"not an nxm:// URL (scheme={parsed.scheme!r})")

    game_domain = (parsed.netloc or "").lower()
    if game_domain != EXPECTED_GAME_DOMAIN:
        raise NxmUrlError(
            f"unsupported game domain {game_domain!r} "
            f"(expected {EXPECTED_GAME_DOMAIN!r})")

    # Path looks like '/mods/{mod_id}/files/{file_id}'
    parts = [p for p in parsed.path.split("/") if p]
    if (len(parts) != 4 or parts[0].lower() != "mods"
            or parts[2].lower() != "files"):
        raise NxmUrlError(f"unexpected path shape {parsed.path!r}")
    try:
        mod_id = int(parts[1])
        file_id = int(parts[3])
    except ValueError:
        raise NxmUrlError(f"mod/file id not integers in {parsed.path!r}")

    q = parse_qs(parsed.query)
    key = q.get("key", [None])[0]
    # Sanity-bound key length so a malicious or buggy nxm:// link can't
    # smuggle gigabytes of payload through the API request URL.
    # Nexus's keys are short tokens; 256 is generous. Round 11 audit.
    if key is not None and len(key) > 256:
        raise NxmUrlError(f"nxm key too long ({len(key)} chars)")
    expires_raw = q.get("expires", [None])[0]
    user_id_raw = q.get("user_id", [None])[0]
    campaign = q.get("campaign", [None])[0]

    def _int_or_none(v):
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    expires = _int_or_none(expires_raw)
    # Bound `expires` to a reasonable epoch range (32-bit positive int)
    # so a malicious link can't pass an absurd integer that confuses
    # Nexus's API or downstream URL parsers.
    if expires is not None and (expires < 0 or expires > 0xFFFFFFFF):
        expires = None

    return NxmUrl(
        game_domain=game_domain,
        mod_id=mod_id,
        file_id=file_id,
        key=key,
        expires=expires,
        user_id=_int_or_none(user_id_raw),
        campaign=campaign,
    )


# ── Windows registry handler registration ────────────────────────────


def _exe_path() -> str | None:
    """Absolute path to the running CDUMM executable, or None when running
    from source (where there's no single exe to register)."""
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve())
    return None


def register_windows_handler(force: bool = False) -> bool:
    """Register CDUMM as the ``nxm://`` handler for the current user.

    Writes keys under ``HKEY_CURRENT_USER\\Software\\Classes\\nxm`` —
    this is per-user so we never need admin rights and don't clobber a
    system-wide Vortex/MO2 registration at ``HKLM``.

    Returns True on success. No-ops on non-Windows or when running from
    source (returns False).

    When ``force`` is False and another mod manager already owns the
    scheme, this leaves the existing registration in place and returns
    False so the caller can prompt the user.
    """
    if not IS_WINDOWS:
        return False
    exe = _exe_path()
    if exe is None:
        logger.info("nxm handler: skipping registration (not a frozen build)")
        return False

    try:
        import winreg
    except ImportError:
        return False

    base = r"Software\Classes\nxm"
    command_path = rf"{base}\shell\open\command"

    if not force:
        existing = _read_command_string(winreg)
        if existing and _exe_from_command(existing) not in {exe, None}:
            logger.info(
                "nxm handler: another app is registered (%s); skipping",
                existing)
            return False

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
        winreg.SetValue(k, "", winreg.REG_SZ, "URL:Nexus Mods Download")
        winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, rf"{base}\shell\open\command") as k:
        winreg.SetValue(k, "", winreg.REG_SZ, f'"{exe}" --nxm "%1"')
    logger.info("nxm handler: registered CDUMM as nxm:// handler (%s)", exe)
    return True


def unregister_windows_handler() -> bool:
    """Remove the CDUMM ``nxm://`` registration. Only touches HKCU so
    system-wide handlers aren't affected.

    Bug #30: ownership re-check at the top. The Settings caller
    already gates on ``is_handler_registered()``, but defense in
    depth is cheap and a future direct caller shouldn't be able to
    strip Vortex/MO2 out from under the user. If the current
    registration doesn't point at our own exe, refuse.
    """
    if not IS_WINDOWS:
        return False
    try:
        import winreg
    except ImportError:
        return False

    current_cmd = _read_command_string(winreg)
    ours = _exe_path()
    if current_cmd and ours:
        current = _exe_from_command(current_cmd)
        if current is not None and current.casefold() != ours.casefold():
            logger.info(
                "nxm handler: refusing to unregister — current "
                "handler is %r, not ours (%r)", current, ours)
            return False

    to_delete = [
        r"Software\Classes\nxm\shell\open\command",
        r"Software\Classes\nxm\shell\open",
        r"Software\Classes\nxm\shell",
        r"Software\Classes\nxm",
    ]
    ok = True
    for path in to_delete:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.debug("nxm handler: could not delete %s: %s", path, e)
            ok = False
    if ok:
        logger.info("nxm handler: unregistered")
    return ok


def _is_handler_registered_windows() -> bool:
    """Windows-only branch of :func:`is_handler_registered`. Pulled
    out so the public function can dispatch cleanly across platforms
    without growing a nested conditional."""
    if not IS_WINDOWS:
        return False
    try:
        import winreg
    except ImportError:
        return False
    cmd = _read_command_string(winreg)
    if not cmd:
        return False
    # Windows file paths are case-insensitive — compare case-folded so
    # a registry entry written as ``C:\Users\Foo\Downloads\CDUMM3API.exe``
    # still matches ``C:\Users\foo\downloads\cdumm3api.exe`` from
    # :func:`_exe_path`.
    current = _exe_from_command(cmd)
    ours = _exe_path()
    if current is None or ours is None:
        return False
    return current.casefold() == ours.casefold()


def is_handler_registered() -> bool:
    """True when the nxm:// handler is currently CDUMM on this platform.

    Dispatches to the OS-specific check: Windows reads HKCU\\Software\\
    Classes\\nxm; Linux runs ``xdg-mime query default x-scheme-handler/
    nxm`` and compares against our ``.desktop`` filename. Returns False
    on platforms where CDUMM doesn't (yet) implement URL-scheme
    registration (e.g. macOS — see MACOS.md).
    """
    if IS_WINDOWS:
        return _is_handler_registered_windows()
    if IS_LINUX:
        return _is_handler_registered_linux()
    return False


# ── Linux: freedesktop .desktop + xdg-mime registration ─────────────


# The .desktop file name. Kept short and unique so it doesn't collide
# with another app's installed entry, and so ``xdg-mime query default``
# returns a stable identifier we can match against.
LINUX_DESKTOP_FILE_NAME = "cdumm-nxm.desktop"

# The MIME-type token freedesktop uses for ``nxm://`` URLs. NexusMods
# documents this as ``x-scheme-handler/nxm`` in their Vortex guide and
# every Linux mod manager that supports nxm registers under this key.
LINUX_NXM_MIME_TYPE = "x-scheme-handler/nxm"


def _linux_applications_dir() -> Path:
    """Per-user XDG applications directory where the ``.desktop`` file
    is installed. Respects ``$XDG_DATA_HOME``; falls back to
    ``~/.local/share`` per the spec."""
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "applications"


def _linux_mimeapps_list_path() -> Path:
    """Path to the freedesktop ``mimeapps.list`` that ``xdg-mime
    default`` writes to. Respects ``$XDG_CONFIG_HOME``; falls back
    to ``~/.config``."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "mimeapps.list"


def _linux_desktop_file_path() -> Path:
    return _linux_applications_dir() / LINUX_DESKTOP_FILE_NAME


def _linux_exec_command() -> str:
    """The ``Exec=`` line for the generated ``.desktop`` file.

    Frozen builds (PyInstaller / AppImage) get a direct exe invocation;
    source builds (``pip install -e .`` or otherwise on PYTHONPATH) get
    ``<python> -m cdumm.main`` so the package's entry-point logic still
    runs. The ``%u`` placeholder is the freedesktop token for "the URL
    that triggered this launch" — xdg-open substitutes the clicked
    ``nxm://...`` URL into it.
    """
    exe = str(Path(sys.executable).resolve())
    if getattr(sys, "frozen", False):
        return f"{exe} --nxm %u"
    return f"{exe} -m cdumm.main --nxm %u"


def _linux_desktop_file_content() -> str:
    """Body of the generated ``.desktop`` file. ``NoDisplay=true``
    keeps it out of the application menu — CDUMM is launched normally
    via its own desktop entry (or AppImage); this file exists solely
    to bind the ``nxm://`` MIME type to our executable."""
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=CDUMM (nxm:// handler)\n"
        "Comment=Crimson Desert Ultimate Mods Manager — Nexus URL handler\n"
        f"Exec={_linux_exec_command()}\n"
        "NoDisplay=true\n"
        "Terminal=false\n"
        f"MimeType={LINUX_NXM_MIME_TYPE};\n"
        "Categories=Game;\n"
    )


def _linux_query_current_handler() -> str | None:
    """Run ``xdg-mime query default x-scheme-handler/nxm`` and return
    the .desktop file name (e.g. ``"cdumm-nxm.desktop"``), or ``None``
    when nothing is registered or xdg-mime isn't installed.

    The call is bounded to a short timeout so an unresponsive desktop
    environment can't wedge the Settings page; failures fall through
    to "no handler", which is the safe default for the
    register-button-state UI."""
    try:
        result = subprocess.run(
            ["xdg-mime", "query", "default", LINUX_NXM_MIME_TYPE],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    name = (result.stdout or "").strip()
    return name or None


def _is_handler_registered_linux() -> bool:
    """True when xdg-mime reports our ``.desktop`` file as the
    default for ``x-scheme-handler/nxm``. Checked by comparing the
    file *name* (not path) because ``xdg-mime`` reports the bare
    filename."""
    if not IS_LINUX:
        return False
    return _linux_query_current_handler() == LINUX_DESKTOP_FILE_NAME


def register_linux_handler(force: bool = False) -> bool:
    """Register CDUMM as the ``nxm://`` handler via a freedesktop
    ``.desktop`` file + ``xdg-mime default``.

    Writes ``~/.local/share/applications/cdumm-nxm.desktop`` (or the
    ``$XDG_DATA_HOME`` equivalent) and runs ``xdg-mime default
    cdumm-nxm.desktop x-scheme-handler/nxm`` to set CDUMM as the
    default handler for the current user. Best-effort refreshes the
    desktop-file database via ``update-desktop-database`` so GNOME /
    KDE / XFCE pick up the new entry without a session restart.

    When ``force`` is False and another desktop entry already owns
    the ``nxm://`` scheme (e.g. a previously-installed Vortex flatpak
    or a competing mod manager), this leaves the existing handler in
    place and returns False so the caller can prompt the user.

    No-ops on non-Linux. Returns True on success.
    """
    if not IS_LINUX:
        return False
    if not force:
        current = _linux_query_current_handler()
        if current and current != LINUX_DESKTOP_FILE_NAME:
            logger.info(
                "nxm handler: another app is registered (%s); skipping",
                current)
            return False

    desktop_path = _linux_desktop_file_path()
    try:
        desktop_path.parent.mkdir(parents=True, exist_ok=True)
        desktop_path.write_text(
            _linux_desktop_file_content(), encoding="utf-8")
        # Some desktop environments expect the .desktop file to be
        # executable. Set the bit defensively — owner-rwx + others-rx
        # mirrors what installer scripts produce.
        desktop_path.chmod(0o755)
    except OSError as e:
        logger.warning(
            "nxm handler: could not write %s: %s", desktop_path, e)
        return False

    # Associate the scheme. xdg-mime missing isn't fatal — minimal WMs
    # (sway, hyprland without a portal) read the .desktop file directly
    # via xdg-utils when xdg-open is invoked, so the registration may
    # still work; log and continue.
    try:
        subprocess.run(
            ["xdg-mime", "default",
             LINUX_DESKTOP_FILE_NAME, LINUX_NXM_MIME_TYPE],
            check=False, timeout=5,
            capture_output=True)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        logger.info(
            "nxm handler: xdg-mime call failed (%s) — .desktop file "
            "was still written and may be picked up directly", e)

    # Refresh the desktop-database cache. Optional; absence is harmless.
    try:
        subprocess.run(
            ["update-desktop-database", str(_linux_applications_dir())],
            check=False, timeout=5, capture_output=True)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    logger.info(
        "nxm handler: registered CDUMM as nxm:// handler (%s)", desktop_path)
    return True


def _linux_strip_mimeapps_entry() -> None:
    """Remove the ``x-scheme-handler/nxm=cdumm-nxm.desktop`` line from
    ``mimeapps.list``. xdg-mime has no "unset default" command — it
    only sets — so we edit the file ourselves. Only lines that match
    *our* .desktop filename are stripped; entries pointing at other
    apps are left untouched.

    Idempotent and silently skips a missing file (a user who never
    registered won't have one). Failures are logged at debug — losing
    the cleanup step is recoverable (the file we deleted has gone,
    so the dangling entry resolves to "no handler" anyway)."""
    mimeapps = _linux_mimeapps_list_path()
    if not mimeapps.exists():
        return
    try:
        lines = mimeapps.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        logger.debug("nxm handler: could not read %s: %s", mimeapps, e)
        return
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{LINUX_NXM_MIME_TYPE}="):
            value = stripped.split("=", 1)[1]
            if LINUX_DESKTOP_FILE_NAME in value:
                # Drop this line — it pointed at our (now-deleted) file.
                continue
        out.append(line)
    try:
        mimeapps.write_text("\n".join(out) + "\n", encoding="utf-8")
    except OSError as e:
        logger.debug("nxm handler: could not rewrite %s: %s", mimeapps, e)


def unregister_linux_handler() -> bool:
    """Remove the CDUMM ``.desktop`` file and clean up its mimeapps
    entry. Refuses to act when the current registered handler isn't
    ours — defense in depth so a future direct caller can't strip
    Vortex / MO2 out from under the user.

    No-ops on non-Linux. Returns True on success.
    """
    if not IS_LINUX:
        return False

    current = _linux_query_current_handler()
    if current and current != LINUX_DESKTOP_FILE_NAME:
        logger.info(
            "nxm handler: refusing to unregister — current handler "
            "is %r, not ours", current)
        return False

    desktop_path = _linux_desktop_file_path()
    ok = True
    try:
        if desktop_path.exists():
            desktop_path.unlink()
    except OSError as e:
        logger.warning(
            "nxm handler: could not remove %s: %s", desktop_path, e)
        ok = False

    _linux_strip_mimeapps_entry()

    # Refresh the desktop-database cache so the .desktop disappearance
    # propagates. Optional.
    try:
        subprocess.run(
            ["update-desktop-database", str(_linux_applications_dir())],
            check=False, timeout=5, capture_output=True)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    if ok:
        logger.info("nxm handler: unregistered")
    return ok


# ── Generic dispatchers ─────────────────────────────────────────────


def register_handler(force: bool = False) -> bool:
    """Register CDUMM as the ``nxm://`` handler on the current
    platform. Dispatches to the OS-specific implementation. Returns
    False on platforms where URL-scheme registration isn't (yet)
    implemented (e.g. macOS)."""
    if IS_WINDOWS:
        return register_windows_handler(force=force)
    if IS_LINUX:
        return register_linux_handler(force=force)
    return False


def unregister_handler() -> bool:
    """Remove the CDUMM ``nxm://`` handler registration on the current
    platform. Dispatches to the OS-specific implementation."""
    if IS_WINDOWS:
        return unregister_windows_handler()
    if IS_LINUX:
        return unregister_linux_handler()
    return False


def existing_handler_description() -> str | None:
    """Human-readable description of whatever app currently owns the
    ``nxm://`` scheme on this platform, or ``None`` if nothing does.

    On Windows this is the raw registry command string
    (``"C:\\...\\Vortex.exe" -d "%1"`` style). On Linux it's the
    ``.desktop`` filename returned by ``xdg-mime query default``
    (e.g. ``"net.nexusmods.vortex.desktop"``). The Settings page
    displays this in the "replace existing handler?" prompt without
    interpreting it further — the format differs by OS but the user
    just needs to recognise the other app.
    """
    if IS_WINDOWS:
        try:
            import winreg
        except ImportError:
            return None
        return _read_command_string(winreg)
    if IS_LINUX:
        return _linux_query_current_handler()
    return None


def _read_command_string(winreg_mod) -> str | None:
    path = r"Software\Classes\nxm\shell\open\command"
    try:
        with winreg_mod.OpenKey(winreg_mod.HKEY_CURRENT_USER, path) as k:
            return winreg_mod.QueryValue(k, "")
    except (FileNotFoundError, OSError):
        return None


def _exe_from_command(cmd: str) -> str | None:
    """Extract the executable path from a registry command string like
    ``"C:\\path\\CDUMM.exe" --nxm "%1"``."""
    cmd = cmd.strip()
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        if end > 1:
            return cmd[1:end]
    # Unquoted — take up to the first space
    sp = cmd.find(" ")
    if sp > 0:
        return cmd[:sp]
    return cmd or None


def should_bind_to_existing_row(connection,
                                  nexus_mod_id: int,
                                  nexus_file_id: int,
                                  downloaded_zip,
                                  intended_mod_id: int | None = None
                                  ) -> int | None:
    """Decide whether an nxm:// download should REPLACE an existing
    mod row or import as a NEW one.

    Returns ``existing_mod_id`` (int) when binding is safe, or
    ``None`` when the download should be imported as a new mod.

    ``intended_mod_id`` (Path-explicit-intent fix, 2026-04-27):
    when set to a non-zero value AND that row exists, the helper
    bypasses every heuristic and returns ``intended_mod_id``
    directly. This is the click-to-update path: the user clicked
    "Update" on a SPECIFIC card, so the binding target is
    unambiguous. Heuristics like "did the file_id match?" or
    "did the name match?" are irrelevant — the user already pointed
    at the row to update.

    Without ``intended_mod_id`` (a fresh nxm:// click from the
    Nexus website with no local intent), falls through to the
    heuristic decision tree:

    Bug from Faisal 2026-04-26: Nexus page 208 hosts multiple
    distinct mods (Better Subtitles + No Letterbox). The previous
    binding logic matched on ``nexus_mod_id`` alone, replacing the
    existing mod content with whichever sibling the user clicked
    Mod Manager Download for. nexus_mod_id is a PAGE id, not a
    unique mod identity.

    Decision tree (no explicit intent):

    1. Find rows with matching ``nexus_mod_id``. None → return None
       (caller imports as new).
    2. Multiple rows → return None (existing ambiguity-warn path
       in the caller surfaces this).
    3. Single row found:
       a. Stored ``nexus_real_file_id`` matches incoming → bind
          (same file, dedupe).
       b. Stored ``nexus_real_file_id`` set BUT differs from
          incoming → return None (different file from same page).
       c. Stored ``nexus_real_file_id`` is NULL/0 (legacy import,
          never updated): peek the downloaded zip for a mod-name
          signal. Match against existing row's name. Bind only if
          the names look like the same mod.
    """
    # Explicit-intent fast path. When the user clicked "Click To
    # Update" on a specific local mod, that intent is unambiguous —
    # heuristics that exist to disambiguate sibling-mod-on-same-page
    # vs update-of-existing-mod aren't needed and actively hurt
    # (e.g. renamed mods fail name comparison; updated mods fail
    # file_id comparison). Verify the row still exists defensively;
    # if it's gone (deleted between click and download arriving),
    # return None directly — do NOT fall through to the heuristic.
    # The heuristic could bind to a SIBLING row sharing nexus_mod_id
    # and corrupt it. User intent was specific; if that row is gone,
    # they get a new mod, not a wrong-target replace. Iteration 5
    # systematic-debugging finding 2026-04-27.
    if intended_mod_id:
        try:
            row = connection.execute(
                "SELECT id, COALESCE(nexus_mod_id, 0) FROM mods "
                "WHERE id = ?",
                (int(intended_mod_id),)).fetchone()
        except Exception:
            return None
        if row is None:
            # Row missing → caller imports as new (skip heuristic).
            return None
        # Iteration 6 systematic-debugging defensive guard: if the
        # intended row has a non-NULL nexus_mod_id that DIFFERS from
        # the URL's, the (URL, intent) pair is internally inconsistent
        # (a programming bug elsewhere produced a mismatched click).
        # Don't silently bind a download for mod page X into a row
        # for mod page Y — that would corrupt the row.
        # Legacy rows with NULL/0 nexus_mod_id pass: this is exactly
        # the case where the user is linking a local-zip import to a
        # Nexus update, and the bind itself fills in the gap.
        stored_nexus_id = int(row[1] or 0)
        url_nexus_id = int(nexus_mod_id or 0)
        if stored_nexus_id and url_nexus_id and stored_nexus_id != url_nexus_id:
            return None
        return int(intended_mod_id)
    if not nexus_mod_id:
        return None
    try:
        rows = connection.execute(
            "SELECT id, name, "
            "COALESCE(nexus_real_file_id, 0) FROM mods "
            "WHERE nexus_mod_id = ? ORDER BY id ASC",
            (int(nexus_mod_id),)).fetchall()
    except Exception:
        return None
    if not rows or len(rows) > 1:
        return None
    existing_id, existing_name, existing_file_id = rows[0]
    existing_file_id = int(existing_file_id or 0)
    new_file_id = int(nexus_file_id or 0)

    # Strict file_id match path. When stored file_id is set, trust it.
    if existing_file_id > 0:
        if new_file_id == existing_file_id:
            return int(existing_id)
        # Different files from the same Nexus page → don't bind.
        return None

    # Legacy / unknown file_id: peek the zip to extract a name signal.
    if downloaded_zip is None:
        # No zip to peek; conservative: don't bind. Caller imports
        # as new. Slight UX regression (legitimate updates create
        # duplicate rows) but safer than the wrong-replace bug.
        return None
    new_name = _extract_mod_name_from_zip(downloaded_zip)
    if not new_name:
        return None
    from cdumm.engine.mod_matching import is_same_mod
    if is_same_mod(existing_name or "", new_name):
        return int(existing_id)
    return None


def _extract_mod_name_from_zip(zip_path) -> str:
    """Best-effort: extract a mod name from a downloaded zip.

    Looks for ``modinfo.json`` first (any depth). Falls back to the
    top-level folder name. Returns empty string if nothing usable.
    """
    import json as _json
    import zipfile as _zf
    from pathlib import Path as _Path
    try:
        with _zf.ZipFile(_Path(zip_path)) as zf:
            names = zf.namelist()
            for n in names:
                if n.lower().endswith("modinfo.json"):
                    try:
                        with zf.open(n) as f:
                            data = _json.load(f)
                        if isinstance(data, dict):
                            for k in ("name", "title"):
                                v = data.get(k)
                                if isinstance(v, str) and v.strip():
                                    return v.strip()
                    except Exception:
                        continue
            # Fallback: top-level folder name
            for n in names:
                top = n.split("/", 1)[0]
                if top and top != "modinfo.json":
                    return top
    except Exception:
        pass
    return ""
