"""nxm:// URL parser — cover the real URL shapes Nexus emits."""
from __future__ import annotations

import pytest

from cdumm.engine.nxm_handler import NxmUrl, NxmUrlError, parse_nxm_url


def test_full_url_with_key_and_expires():
    u = parse_nxm_url(
        "nxm://crimsondesert/mods/591/files/12345"
        "?key=abc123&expires=1775999999&user_id=99")
    assert u.mod_id == 591
    assert u.file_id == 12345
    assert u.key == "abc123"
    assert u.expires == 1775999999
    assert u.user_id == 99
    assert u.campaign is None


def test_collection_url_with_campaign():
    # Vortex issue #21439 shows this shape
    u = parse_nxm_url(
        "nxm://crimsondesert/mods/207/files/1?campaign=collection")
    assert u.mod_id == 207
    assert u.file_id == 1
    assert u.campaign == "collection"
    assert u.key is None


def test_premium_direct_call_without_key():
    # Some clients use nxm:// with no query — premium direct download
    u = parse_nxm_url("nxm://crimsondesert/mods/591/files/12345")
    assert u.mod_id == 591
    assert u.key is None
    assert u.expires is None


def test_wrong_scheme_rejected():
    with pytest.raises(NxmUrlError):
        parse_nxm_url("https://nexusmods.com/crimsondesert/mods/1")


def test_wrong_game_domain_rejected():
    with pytest.raises(NxmUrlError, match="game domain"):
        parse_nxm_url("nxm://skyrimspecialedition/mods/1/files/1")


def test_malformed_path_rejected():
    with pytest.raises(NxmUrlError, match="path shape"):
        parse_nxm_url("nxm://crimsondesert/mods/591")


def test_non_integer_ids_rejected():
    with pytest.raises(NxmUrlError, match="integers"):
        parse_nxm_url("nxm://crimsondesert/mods/abc/files/def")
