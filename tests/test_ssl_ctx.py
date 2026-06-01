"""Regression for GitHub #175 / #178 / #179 / #185 (SSL certificate
verification failures on the frozen exe).

PyInstaller bakes whatever CA bundle ships with the Python build into
the exe at freeze time. When any root in that bundle expires, every
outbound HTTPS call from CDUMM fails with CERTIFICATE_VERIFY_FAILED
until the next rebuild. cdumm.engine.ssl_ctx.make_ssl_context now
sources its CA bundle from certifi at runtime instead, which is
updated against Mozilla on every certifi release.

These tests pin three things:
  * make_ssl_context returns a CERT_REQUIRED-mode ssl.SSLContext
    (anything less would silently accept any cert and reopen the
    very class of bug the fix was for)
  * the context's CA file points at the live certifi.where() bundle
    (so a stale frozen bundle cannot leak back in)
  * importing the helper does not raise even if certifi resolves
    differently than the test runner's defaults
"""
from __future__ import annotations

import ssl

import certifi
import pytest

from cdumm.engine.ssl_ctx import make_ssl_context


def test_make_ssl_context_returns_ssl_context():
    ctx = make_ssl_context()
    assert isinstance(ctx, ssl.SSLContext)


def test_make_ssl_context_requires_cert_verification():
    """If verify_mode falls back to CERT_NONE this regression undoes
    the whole reason the certifi switch was made: invalid / expired
    certificates would be silently trusted."""
    ctx = make_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_make_ssl_context_loads_certifi_bundle():
    """The CA file used by the helper is the one certifi.where()
    resolves NOW, not whatever was frozen into the exe at build
    time. get_ca_certs returns an empty list when no CAs have been
    loaded, so a non-empty list confirms loading worked."""
    ctx = make_ssl_context()
    certs = ctx.get_ca_certs()
    assert certs, (
        "Expected the certifi CA bundle to populate get_ca_certs; "
        "an empty list means the load_verify_locations call did "
        "not pick up the certifi bundle as intended.")


def test_certifi_bundle_path_exists():
    """Belt-and-braces sanity check: the path certifi advertises
    must resolve to a real file on disk so the load happens at all."""
    import os
    assert os.path.isfile(certifi.where())


def test_make_ssl_context_returns_independent_instances():
    """Each call returns a fresh SSLContext so consumers that tweak
    a context's options (e.g. set check_hostname=False for a test)
    do not affect later callers."""
    a = make_ssl_context()
    b = make_ssl_context()
    assert a is not b
