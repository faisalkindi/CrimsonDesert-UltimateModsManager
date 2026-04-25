"""Bugs #27, #28, #29 — download-safety contract.

27. CDN URL scheme must be HTTPS. Nexus returning an http:// URL
    (misconfig, mirror compromise, MITM) must NOT lead to an install
    of unverified bytes into the user's game folder.
28. Idle-stall protection — a stream that trickles bytes slowly
    enough to keep per-recv timeouts alive would otherwise hang the
    banner forever.
29. Content-Length cap — a response claiming petabytes mustn't fill
    the user's disk.

Pure-logic helpers, tested without real HTTP.
"""
from __future__ import annotations

import pytest


def test_assert_https_scheme_accepts_https():
    from cdumm.gui.fluent_window import _assert_https_download_url
    # No raise.
    _assert_https_download_url("https://cdn.nexusmods.com/1234/file.zip")


def test_assert_https_scheme_rejects_http():
    from cdumm.gui.fluent_window import _assert_https_download_url
    with pytest.raises(ValueError):
        _assert_https_download_url("http://cdn.nexusmods.com/1234/file.zip")


def test_assert_https_scheme_rejects_other_schemes():
    from cdumm.gui.fluent_window import _assert_https_download_url
    for bad in ("ftp://x/y", "file:///etc/passwd", "javascript:alert(0)"):
        with pytest.raises(ValueError):
            _assert_https_download_url(bad)


def test_content_length_within_cap_accepted():
    from cdumm.gui.fluent_window import _validate_download_size
    # 100 MB claimed, 2 GB cap.
    _validate_download_size(
        content_length=100 * 1024 * 1024,
        max_bytes=2 * 1024 * 1024 * 1024,
    )


def test_content_length_above_cap_rejected():
    from cdumm.gui.fluent_window import _validate_download_size
    with pytest.raises(ValueError):
        _validate_download_size(
            content_length=3 * 1024 * 1024 * 1024,  # 3 GB
            max_bytes=2 * 1024 * 1024 * 1024,
        )


def test_content_length_missing_is_ok():
    """Some CDNs don't send Content-Length — we can't validate up
    front. Helper should accept None and let the streaming loop's
    per-chunk accumulator catch oversize cases."""
    from cdumm.gui.fluent_window import _validate_download_size
    _validate_download_size(
        content_length=None, max_bytes=2 * 1024 * 1024 * 1024)


def test_streaming_accumulator_raises_when_total_exceeds_cap():
    """``_check_download_progress`` takes a running byte total +
    cap and raises when exceeded. Used inside the streaming loop
    for the Content-Length-absent case."""
    from cdumm.gui.fluent_window import _check_download_progress
    # Well under cap — fine.
    _check_download_progress(total_bytes=1_000_000, max_bytes=2_000_000)
    with pytest.raises(ValueError):
        _check_download_progress(
            total_bytes=3_000_000, max_bytes=2_000_000)
