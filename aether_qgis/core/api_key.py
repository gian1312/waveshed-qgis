"""API key storage and validation stub.

The API key system is a placeholder for future server-side key issuance.
Currently, any non-empty key is accepted.  Real Ed25519 signature
verification will be added once the key-generation server is operational.
No external dependencies (no PyNaCl).
"""

from __future__ import annotations

import datetime

from qgis.core import QgsSettings


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ApiKeyError(Exception):
    """Raised when an API key is missing, invalid, or expired."""

    pass


# ---------------------------------------------------------------------------
# Core validation
# ---------------------------------------------------------------------------


def validate_api_key(key_string: str) -> tuple[bool, str, datetime.date | None]:
    """Validate an API key.

    Currently accepts any non-empty string.  Will be replaced with
    Ed25519 public-key signature verification once the server-side
    key generation endpoint is live.

    Returns
    -------
    tuple[bool, str, datetime.date | None]
        ``(is_valid, message, expiry_date_or_None)``
    """
    if not key_string or not key_string.strip():
        return False, "No API key provided", None

    # TODO: implement real Ed25519 verification against embedded public key
    return True, "Key accepted", None


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

_SETTINGS_KEY = "aether/api_key"


def get_stored_key() -> str:
    """Read the API key from persistent QGIS settings."""
    return QgsSettings().value(_SETTINGS_KEY, "")


def store_key(key_string: str) -> None:
    """Write an API key to persistent QGIS settings."""
    QgsSettings().setValue(_SETTINGS_KEY, key_string)


# ---------------------------------------------------------------------------
# Convenience wrapper
# ---------------------------------------------------------------------------


def check_key_or_raise() -> datetime.date | None:
    """Validate the stored API key and raise on failure.

    Returns ``None`` (no expiry tracking yet).
    """
    key = get_stored_key()
    if not key:
        raise ApiKeyError(
            "No API key configured. Please enter your key in AETHER Settings."
        )

    is_valid, message, expiry = validate_api_key(key)
    if not is_valid:
        raise ApiKeyError(message)

    return expiry
