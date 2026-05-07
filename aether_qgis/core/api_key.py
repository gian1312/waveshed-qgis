"""Offline API key validation using Ed25519 signatures.

The API key is a base64url-encoded blob containing a JSON payload followed
by a 64-byte Ed25519 signature.  Validation is performed entirely offline
using an embedded public key — no server round-trip required.

Key format::

    payload = JSON {"email_hash": "sha256...", "issued": "...", "expiry": "...", "tier": "..."}
    signature = Ed25519.sign(payload_bytes, server_private_key)   # 64 bytes
    api_key   = base64url(payload_bytes + signature)
"""

from __future__ import annotations

import base64
import datetime
import json
import logging
from typing import TYPE_CHECKING

from qgis.core import QgsSettings

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ed25519 public key
# ---------------------------------------------------------------------------

# TODO: Replace with actual Ed25519 public key before release
AETHER_API_PUBLIC_KEY: bytes = bytes(32)  # Placeholder — accepts nothing until real key is set

_DEVELOPMENT_SENTINELS = {"", "DEVELOPMENT"}

# ---------------------------------------------------------------------------
# PyNaCl availability
# ---------------------------------------------------------------------------

_HAS_NACL = False
try:
    from nacl.signing import VerifyKey  # type: ignore[import-untyped]
    from nacl.exceptions import BadSignatureError  # type: ignore[import-untyped]

    _HAS_NACL = True
except ImportError:
    VerifyKey = None  # type: ignore[assignment,misc]
    BadSignatureError = None  # type: ignore[assignment,misc]


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
    """Validate an API key offline using the embedded Ed25519 public key.

    Parameters
    ----------
    key_string:
        The base64url-encoded API key string supplied by the user.

    Returns
    -------
    tuple[bool, str, datetime.date | None]
        ``(is_valid, message, expiry_date_or_None)``

        * On success: ``(True, "Valid until YYYY-MM-DD", expiry_date)``
        * On failure: ``(False, "<reason>", None)``
    """

    if not key_string or not key_string.strip():
        return False, "No API key provided", None

    key_string = key_string.strip()

    # Allow development bypass
    if key_string in _DEVELOPMENT_SENTINELS:
        return True, "Development mode — no key validation", None

    # ------------------------------------------------------------------
    # Check PyNaCl availability
    # ------------------------------------------------------------------
    if not _HAS_NACL:
        log.warning(
            "PyNaCl is not installed — Ed25519 verification unavailable. "
            "Install it with:  pip install pynacl"
        )
        return (
            False,
            "PyNaCl is not installed. Please install it (pip install pynacl) "
            "for API key verification.",
            None,
        )

    # ------------------------------------------------------------------
    # Decode
    # ------------------------------------------------------------------
    try:
        # base64url may or may not include padding; be lenient.
        padded = key_string + "=" * (-len(key_string) % 4)
        raw = base64.urlsafe_b64decode(padded)
    except Exception:
        return False, "API key is not valid base64url", None

    if len(raw) <= 64:
        return False, "API key is too short", None

    payload_bytes = raw[:-64]
    signature = raw[-64:]

    # ------------------------------------------------------------------
    # Verify signature
    # ------------------------------------------------------------------
    try:
        vk = VerifyKey(AETHER_API_PUBLIC_KEY)
        vk.verify(payload_bytes, signature)
    except BadSignatureError:
        return False, "API key signature is invalid", None
    except Exception as exc:
        return False, f"Signature verification failed: {exc}", None

    # ------------------------------------------------------------------
    # Parse payload
    # ------------------------------------------------------------------
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False, "API key payload is not valid JSON", None

    expiry_str = payload.get("expiry")
    if not expiry_str:
        return False, "API key payload missing 'expiry' field", None

    try:
        expiry_date = datetime.date.fromisoformat(expiry_str)
    except ValueError:
        return False, f"Invalid expiry date format: {expiry_str}", None

    # ------------------------------------------------------------------
    # Check expiry
    # ------------------------------------------------------------------
    today = datetime.date.today()
    if expiry_date < today:
        return (
            False,
            f"API key expired on {expiry_date.isoformat()}",
            expiry_date,
        )

    return True, f"Valid until {expiry_date.isoformat()}", expiry_date


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

_SETTINGS_KEY = "aether/api_key"


def get_stored_key() -> str:
    """Read the API key from persistent QGIS settings.

    Returns
    -------
    str
        The stored key string, or ``""`` if none has been saved.
    """
    return QgsSettings().value(_SETTINGS_KEY, "")


def store_key(key_string: str) -> None:
    """Write an API key to persistent QGIS settings.

    Parameters
    ----------
    key_string:
        The base64url-encoded API key to store.
    """
    QgsSettings().setValue(_SETTINGS_KEY, key_string)


# ---------------------------------------------------------------------------
# Convenience wrapper
# ---------------------------------------------------------------------------


def check_key_or_raise() -> datetime.date | None:
    """Validate the stored API key and raise on failure.

    Returns
    -------
    datetime.date | None
        The expiry date when validation succeeds (``None`` in dev mode).

    Raises
    ------
    ApiKeyError
        If no key is stored, the key is invalid, or the key has expired.
    """
    key = get_stored_key()

    if not key:
        raise ApiKeyError("No API key configured. Please enter your key in the AETHER settings.")

    is_valid, message, expiry = validate_api_key(key)
    if not is_valid:
        raise ApiKeyError(message)

    return expiry
