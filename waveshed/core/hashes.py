"""MD5 for fingerprints and cache keys — never for security — on Python 3.8+.

``hashlib.md5(usedforsecurity=False)`` tells a FIPS-restricted OpenSSL that
the digest is a checksum, not a credential; the keyword exists from Python
3.9. QGIS 3.28 on Ubuntu 20.04 runs Python 3.8, where the keyword raises
``TypeError`` — so every fingerprint in the plugin goes through :func:`md5`,
which falls back to the plain call there.
"""

from __future__ import annotations

import hashlib


def md5(data: bytes = b""):
    """``hashlib.md5(data)`` marked as a non-security digest where Python
    allows it (3.9+); the plain digest on 3.8."""
    try:
        return hashlib.md5(data, usedforsecurity=False)
    except TypeError:  # Python 3.8: no usedforsecurity keyword
        return hashlib.md5(data)  # nosec B324 — fingerprints, not security


__all__ = ["md5"]
