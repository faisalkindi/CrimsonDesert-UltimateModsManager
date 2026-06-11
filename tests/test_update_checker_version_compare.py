"""GitHub update banner version comparison must survive suffix tags.

``_version_newer`` int-split the dotted parts, so a release tagged
``v3.3.21-hotfix`` raised ValueError, was swallowed to False, and the
update banner was suppressed for every user until the next plain tag.
It now reuses ``nexus_api._version_to_tuple`` (semver-aware).
"""
from __future__ import annotations

from cdumm.engine.update_checker import _version_newer


def test_hotfix_suffix_remote_is_newer() -> None:
    assert _version_newer("3.3.21-hotfix", "3.3.20") is True, (
        "a -hotfix tag must not suppress the update banner")


def test_hotfix_suffix_local_is_not_outranked() -> None:
    assert _version_newer("3.3.20", "3.3.21-hotfix") is False


def test_v_prefix_handled() -> None:
    assert _version_newer("v3.4", "3.3.21") is True
    assert _version_newer("3.4", "v3.4") is False


def test_plain_versions_still_compare() -> None:
    assert _version_newer("0.8.1", "0.7.9") is True
    assert _version_newer("0.7.9", "0.8.1") is False


def test_equal_versions_not_newer() -> None:
    assert _version_newer("3.4", "3.4") is False
    assert _version_newer("3.4.0", "3.4") is False  # trailing zero


def test_prerelease_of_same_core_not_newer() -> None:
    # semver: 3.4-rc1 precedes 3.4, so it's not an update.
    assert _version_newer("3.4-rc1", "3.4") is False
    assert _version_newer("3.4", "3.4-rc1") is True


def test_garbage_returns_false_without_raising() -> None:
    assert _version_newer("not-a-version", "1.0") is False
    assert _version_newer("1.0", "") is False
    assert _version_newer("", "") is False
