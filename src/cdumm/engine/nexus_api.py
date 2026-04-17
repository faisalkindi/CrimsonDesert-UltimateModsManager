"""Master-branch stub for the NexusMods API client.

The CDUMM_API_Test branch ships a full client that talks to Nexus for
key validation + mod-update checks. Master intentionally opts out of
Nexus integration — this stub provides no-op functions so any UI path
that conditionally calls these on an API key's presence just returns
empty results.
"""

from __future__ import annotations

from typing import Any


def validate_api_key(key: str) -> Any:
    """No-op: master ships without the Nexus API client. Always None."""
    return None


def check_mod_updates(mods: Any, api_key: str) -> dict:
    """No-op: never reports updates. Return empty dict to satisfy callers."""
    return {}
