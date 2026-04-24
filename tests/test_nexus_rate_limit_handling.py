"""Bug #22 + #23: rate-limit snapshot never surfaced, and HTTP 429
isn't handled specifically. Together these mean the user gets no
warning as they approach the cap, and when they hit it, CDUMM
retries every 30 min with no back-off — potentially burning more
quota (or at minimum making the problem take longer to clear).

Fix contracts:

- ``_api_request`` raises ``NexusRateLimited`` on HTTP 429 with the
  reset timestamp the caller can use to delay the next attempt.
- Bug Report includes the last-known rate limit snapshot so support
  requests carry the context.
"""
from __future__ import annotations

from io import BytesIO
from urllib.error import HTTPError

import pytest


def _force_429(monkeypatch):
    import urllib.request
    def _urlopen_429(req, timeout=None, context=None):
        headers = {
            "X-RL-Hourly-Remaining": "0",
            "X-RL-Hourly-Reset": "1775960000",
            "X-RL-Daily-Remaining": "18000",
            "X-RL-Daily-Reset": "1775990000",
        }

        class _H(dict):
            def get(self, k, default=None):
                return super().get(k) or super().get(k.lower(), default)
        raise HTTPError(
            url=str(req), code=429, msg="Too Many Requests",
            hdrs=_H(headers), fp=BytesIO(b""))
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_429)


def test_api_request_raises_rate_limited_on_429(monkeypatch):
    from cdumm.engine import nexus_api
    assert hasattr(nexus_api, "NexusRateLimited"), (
        "nexus_api must expose NexusRateLimited exception type")
    _force_429(monkeypatch)
    with pytest.raises(nexus_api.NexusRateLimited):
        nexus_api._api_request("/users/validate.json", api_key="x")


def test_rate_limited_exception_carries_reset_timestamp(monkeypatch):
    from cdumm.engine import nexus_api
    _force_429(monkeypatch)
    try:
        nexus_api._api_request("/users/validate.json", api_key="x")
        raised = None
    except nexus_api.NexusRateLimited as e:
        raised = e
    assert raised is not None
    # The caller should be able to read the reset epoch to schedule
    # the next check. We'll look for a ``reset_at`` attribute.
    assert hasattr(raised, "reset_at"), (
        "NexusRateLimited must carry reset_at (unix epoch)")
    assert int(raised.reset_at) == 1775960000


def test_rate_limit_snapshot_accessible_via_get_helper():
    """``get_rate_limit_snapshot`` is no longer dead code — the bug
    report uses it. Pin the function's existence + shape."""
    from cdumm.engine.nexus_api import (
        get_rate_limit_snapshot, _last_rate_limit,
    )
    _last_rate_limit.clear()
    _last_rate_limit.update({
        "X-RL-Daily-Remaining": "19500",
        "X-RL-Daily-Reset": "1775990000",
    })
    snap = get_rate_limit_snapshot()
    assert snap["X-RL-Daily-Remaining"] == "19500"
    # Returns a COPY so callers can't mutate internal state.
    snap["X-RL-Daily-Remaining"] = "tampered"
    assert _last_rate_limit["X-RL-Daily-Remaining"] == "19500"
