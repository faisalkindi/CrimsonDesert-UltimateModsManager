"""``nxm://`` URL protocol handler — parse + Windows registry registration.

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
2. :func:`register_windows_handler` — writes the HKCU\\Software\\Classes
   entries needed so Windows hands ``nxm://...`` URLs to CDUMM.
3. :func:`unregister_windows_handler` — removes those entries.
4. :func:`is_handler_registered` — reports whether CDUMM is currently
   the registered handler (so we don't stomp on Vortex/MO2 without
   asking the user).
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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

    return NxmUrl(
        game_domain=game_domain,
        mod_id=mod_id,
        file_id=file_id,
        key=key,
        expires=_int_or_none(expires_raw),
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
    if sys.platform != "win32":
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
    if sys.platform != "win32":
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


def is_handler_registered() -> bool:
    """True when the nxm:// handler under HKCU points at the current
    CDUMM executable."""
    if sys.platform != "win32":
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
