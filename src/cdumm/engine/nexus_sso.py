"""Nexus Mods SSO (Single Sign-On) WebSocket client.

Reference protocol (verified from
``github.com/Nexus-Mods/sso-integration-demo``, retrieved 2026-04-17):

1. Open WebSocket to ``wss://sso.nexusmods.com``.
2. Send a JSON handshake::

       {"id": "<uuid-v4>", "token": <stored-token-or-null>, "protocol": 2}

3. Server replies::

       {"success": true, "data": {"connection_token": "..."}, "error": null}

   Cache the ``connection_token`` so future reconnects skip the browser
   step if the server still recognises us.

4. Open a browser to::

       https://www.nexusmods.com/sso?id=<uuid>&application=<slug>

   The user signs in and authorises CDUMM. The ``<slug>`` is the
   application slug assigned by Nexus staff during registration.

5. Server sends the user's API key over the same WebSocket::

       {"success": true, "data": {"api_key": "..."}, "error": null}

6. Close the WebSocket. Persist the API key via
   :func:`cdumm.storage.config.Config.set` the same way a manually-
   entered key is stored.

Blocked on registration: CDUMM's slug is not yet issued, so Phase 2C's
SSO button displays "Pending approval" until the slug lands.
``start_sso_flow`` still runs the full protocol against a stub slug
for internal testing — Nexus returns an error the user would see, but
every earlier step is exercised.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
import webbrowser
from typing import Callable

logger = logging.getLogger(__name__)

SSO_WS_URL = "wss://sso.nexusmods.com"
SSO_BROWSER_URL = "https://www.nexusmods.com/sso?id={uuid}&application={slug}"

# Slug assigned by Nexus Mods staff on 2026-04-24. CDUMM is now a
# registered application in Nexus's API system; the SSO browser
# handoff resolves against this slug.
APPLICATION_SLUG = "kindiboy-cdumm"
PROTOCOL_VERSION = 2

# True since 2026-04-24 when Nexus approved CDUMM as an application.
# ``slug_placeholder()`` reads this flag rather than string-comparing
# APPLICATION_SLUG so the SSO button shows "Sign in with Nexus" (not
# "Pending approval") regardless of whether the slug literal ever
# changes.
_SLUG_APPROVED = True


class SsoUnavailable(RuntimeError):
    """Raised when the websocket client library isn't installed or the
    handshake with the SSO server fails."""


def start_sso_flow(on_key: Callable[[str], None],
                   on_error: Callable[[str], None],
                   connection_token: str | None = None,
                   on_token: Callable[[str], None] | None = None) -> None:
    """Run the SSO flow on a background thread.

    ``on_key`` is called with the final API key on success. ``on_error``
    is called with a user-readable message on failure.
    ``on_token`` — when supplied — receives the ``connection_token`` that
    the SSO server returns during the handshake so the caller can
    persist it and resume the same login if the browser step gets
    interrupted (per the node-nexus-api protocol-2 contract). All three
    callbacks run on the WebSocket thread — marshal back to the GUI
    thread via :meth:`PySide6.QtCore.QMetaObject.invokeMethod` in the
    caller.

    The ``websocket-client`` package (``pip install websocket-client``)
    provides the WebSocket transport. It's imported lazily so CDUMM
    only pulls it in when SSO is exercised.
    """
    try:
        import websocket  # type: ignore
    except ImportError as e:
        on_error(
            "The 'websocket-client' Python package is required for "
            "Login with Nexus. Install it with "
            "`pip install websocket-client` or paste a personal API "
            "key instead.")
        logger.warning("SSO: websocket-client missing: %s", e)
        return

    sso_id = str(uuid.uuid4())
    state: dict = {"stage": "connecting", "token": connection_token}

    def on_open(ws):
        state["stage"] = "handshake"
        ws.send(json.dumps({
            "id": sso_id,
            "token": state.get("token"),
            "protocol": PROTOCOL_VERSION,
        }))

    def on_message(ws, message):
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            on_error(f"Malformed SSO response: {message[:120]}")
            ws.close()
            return
        if not payload.get("success"):
            err = payload.get("error") or "SSO error"
            on_error(str(err))
            ws.close()
            return
        data = payload.get("data") or {}
        if "connection_token" in data and state["stage"] == "handshake":
            state["token"] = data["connection_token"]
            state["stage"] = "awaiting_user"
            # Surface the token so callers can persist it and reuse on
            # a subsequent attempt after the user closes the browser.
            if on_token is not None:
                try:
                    on_token(data["connection_token"])
                except Exception as cb_err:
                    logger.debug("SSO on_token callback raised: %s", cb_err)
            # Open the user's browser to the SSO authorisation page.
            browser_url = SSO_BROWSER_URL.format(
                uuid=sso_id, slug=APPLICATION_SLUG)
            webbrowser.open(browser_url)
            logger.info("SSO: browser opened to %s", browser_url)
            return
        if "api_key" in data:
            state["stage"] = "done"
            on_key(data["api_key"])
            ws.close()
            return

    def on_ws_error(ws, error):
        on_error(f"SSO websocket error: {error}")

    def on_close(ws, code, msg):
        if state["stage"] not in {"done", "closed"}:
            logger.info("SSO: websocket closed at stage=%s", state["stage"])
        state["stage"] = "closed"

    def runner():
        try:
            ws = websocket.WebSocketApp(
                SSO_WS_URL,
                on_open=on_open, on_message=on_message,
                on_error=on_ws_error, on_close=on_close)
            # Nexus's node-nexus-api README requires a ping every 30s
            # while the user authorises in the browser. Without this the
            # CloudFront-fronted WebSocket drops on long authorisations
            # and the user never gets a key.
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            on_error(f"SSO failed: {e}")

    t = threading.Thread(target=runner, daemon=True, name="cdumm-sso")
    t.start()


def slug_placeholder() -> bool:
    """True while CDUMM is awaiting Nexus approval for an official slug.

    Reads the :data:`_SLUG_APPROVED` flag instead of string-comparing
    the slug so this still works if Nexus assigns the literal slug
    ``cdumm`` as our approved value.
    """
    return not _SLUG_APPROVED
