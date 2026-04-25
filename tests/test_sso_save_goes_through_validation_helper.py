"""Bug 52: ``_sso_finished_ok`` wrote the API key directly via
``Config(self._db).set("nexus_api_key", key)``, bypassing
``_persist_nexus_key_if_valid`` and the auth-banner clear. A user
with a rejected-auth banner who logs in via SSO would still see
the stale banner.
"""
from __future__ import annotations

from pathlib import Path


def test_sso_success_slot_clears_auth_banner():
    src = (Path(__file__).resolve().parents[1]
           / "src" / "cdumm" / "gui" / "pages" / "settings_page.py"
           ).read_text(encoding="utf-8")
    i = src.find("def _sso_finished_ok(")
    assert i != -1
    # Scope ends at next method def (4-space indent).
    rest = src[i + len("def _sso_finished_ok("):]
    j = rest.find("\n    def ")
    scope = rest[:j] if j >= 0 else rest[:2000]
    assert "_clear_auth_banner_state" in scope, (
        "_sso_finished_ok must also dismiss the auth banner — the "
        "SSO-returned key is by definition valid, so the rejection "
        "banner (if any) is stale")
