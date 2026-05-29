"""Shared SSL context factory for CDUMM HTTPS requests.

PyInstaller frozen executables embed the CA bundle that ships with the
Python build at freeze time.  When any root CA in that bundle expires,
``ssl.create_default_context()`` + ``urlopen()`` raises::

    ssl.SSLCertVerificationError: CERTIFICATE_VERIFY_FAILED:
        certificate has expired

Using certifi's Mozilla CA bundle (refreshed on every ``certifi``
release) avoids this without disabling certificate verification.

All outbound HTTPS calls in CDUMM (Nexus API, GitHub update check,
NXM direct downloads) should obtain their SSL context from
:func:`make_ssl_context` so the fix is applied consistently.
"""

from __future__ import annotations

import logging
import ssl

logger = logging.getLogger(__name__)


def make_ssl_context() -> ssl.SSLContext:
    """Return an SSL context backed by certifi's CA bundle.

    On success the context verifies peer certificates against the
    Mozilla root store shipped with ``certifi``, which is always
    up-to-date regardless of when the PyInstaller exe was frozen.

    Falls back to ``ssl.create_default_context()`` (system / frozen
    CA bundle) with a logged warning when ``certifi`` is not
    importable, so the app can still attempt connections on platforms
    where it shipped without the package rather than crashing.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        logger.warning(
            "certifi not available; falling back to default CA store. "
            "HTTPS connections may fail if the bundled CA bundle is stale."
        )
        return ssl.create_default_context()
