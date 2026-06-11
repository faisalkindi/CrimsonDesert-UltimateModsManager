"""The SSO websocket must use the shared certifi SSL context.

Every urllib HTTPS call in CDUMM goes through
``cdumm.engine.ssl_ctx.make_ssl_context`` so the frozen exe doesn't
depend on the CA bundle PyInstaller froze at build time (GitHub #175,
#178, #179). The SSO websocket was the one HTTPS client that didn't:
``run_forever`` was called without ``sslopt``, so websocket-client
built its own default context. websocket-client (verified against
v1.9.0 ``_http.py::_wrap_sni_socket``) accepts a caller-provided
``ssl.SSLContext`` via ``sslopt={"context": ctx}``.
"""
from __future__ import annotations

import ssl
import sys
import threading
import types


def test_run_forever_receives_certifi_ssl_context(monkeypatch):
    captured: dict = {}
    ran = threading.Event()

    class FakeWebSocketApp:
        def __init__(self, url, **kwargs):
            self.url = url

        def run_forever(self, **kwargs):
            captured.update(kwargs)
            ran.set()

    fake_ws = types.ModuleType("websocket")
    fake_ws.WebSocketApp = FakeWebSocketApp
    monkeypatch.setitem(sys.modules, "websocket", fake_ws)

    from cdumm.engine.nexus_sso import start_sso_flow

    errors: list[str] = []
    start_sso_flow(on_key=lambda key: None, on_error=errors.append)

    assert ran.wait(timeout=5), "run_forever was never called"
    assert not errors, f"SSO flow errored: {errors}"

    sslopt = captured.get("sslopt")
    assert isinstance(sslopt, dict), (
        "run_forever must be called with an sslopt dict carrying the "
        "shared certifi-backed SSL context")
    assert isinstance(sslopt.get("context"), ssl.SSLContext), (
        "sslopt['context'] must be an ssl.SSLContext (the form "
        "websocket-client 1.9.0 reads in _wrap_sni_socket)")
    # The keep-alive ping contract must survive the change.
    assert captured.get("ping_interval") == 30
    assert captured.get("ping_timeout") == 10
