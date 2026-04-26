"""Round 8 fix: get_recently_updated must not drop the whole feed
on one bad entry."""
from __future__ import annotations

from unittest.mock import patch

from cdumm.engine.nexus_api import get_recently_updated


def test_feed_skips_bad_entries_keeps_good_ones() -> None:
    """One None entry, one missing mod_id, three valid ones —
    the whole feed must NOT collapse to None. Without resilience
    here, every enabled mod would hit the per-mod endpoint and
    burn the rate-limit budget."""
    api_response = [
        {"mod_id": 100, "latest_file_update": 1000},
        None,  # bad entry
        {"mod_id": 200, "latest_file_update": 2000},
        {"latest_file_update": 3000},  # missing mod_id
        {"mod_id": 300, "latest_file_update": 3000},
    ]
    with patch("cdumm.engine.nexus_api._api_request",
               return_value=api_response):
        result = get_recently_updated("key")
    assert result is not None, (
        "the whole feed should not be dropped because of bad "
        "entries — that triggers per-mod API calls for every "
        "enabled mod and risks rate limits")
    assert set(result.keys()) == {100, 200, 300}, (
        f"expected 3 good entries to survive; got {result}")
    assert result[100] == 1000
    assert result[200] == 2000
    assert result[300] == 3000


def test_feed_returns_none_when_data_itself_invalid() -> None:
    """If the API response itself is unusable (not iterable), we
    still return None — that's distinct from 'feed had some bad
    entries'."""
    with patch("cdumm.engine.nexus_api._api_request",
               return_value=42):  # not iterable
        result = get_recently_updated("key")
    assert result is None
