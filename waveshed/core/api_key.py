"""Waveshed API key storage and validation.

A Waveshed API key is an 84-byte payload encoded with Base58 (the Bitcoin
alphabet), i.e. roughly 115 characters. Validation here is structural only:
non-empty, valid Base58 charset, and decodes to exactly 84 bytes. Real
Ed25519 signature verification against the plugin's embedded public key will
be added once key issuance is live (see the TODO in :func:`validate_api_key`).

Pure standard library — no external dependencies (no PyNaCl / base58 package).
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
# Constants
# ---------------------------------------------------------------------------

#: Where users obtain a key.
GET_API_KEY_URL = "https://waveshed.io/downloads"
_GET_KEY_HINT = f"Get a key at {GET_API_KEY_URL}"

#: Decoded payload length (bytes) of a valid Waveshed API key.
KEY_PAYLOAD_BYTES = 84

#: Environment variable the Aether engine reads the license/key from.
LICENSE_ENV_VAR = "AETHER_LICENSE"


# ---------------------------------------------------------------------------
# Base58 (Bitcoin alphabet) decode — pure stdlib
# ---------------------------------------------------------------------------

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {ch: i for i, ch in enumerate(_B58_ALPHABET)}


def b58decode(text: str) -> bytes:
    """Decode a Base58 (Bitcoin alphabet) string to raw bytes.

    Raises
    ------
    ValueError
        If *text* is empty or contains a non-Base58 character.
    """
    if not text:
        raise ValueError("empty string")

    num = 0
    for ch in text:
        idx = _B58_INDEX.get(ch)
        if idx is None:
            raise ValueError(f"invalid base58 character: {ch!r}")
        num = num * 58 + idx

    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    # Each leading '1' encodes one leading zero byte.
    n_pad = len(text) - len(text.lstrip("1"))
    return b"\x00" * n_pad + body


# ---------------------------------------------------------------------------
# Core validation
# ---------------------------------------------------------------------------


def validate_api_key(key_string: str) -> tuple[bool, str, datetime.date | None]:
    """Validate a Waveshed API key structurally.

    A valid key is Base58 text whose payload decodes to exactly
    :data:`KEY_PAYLOAD_BYTES` bytes.

    Returns
    -------
    tuple[bool, str, datetime.date | None]
        ``(is_valid, message, expiry_date_or_None)``
    """
    if not key_string or not key_string.strip():
        return False, f"No API key provided. {_GET_KEY_HINT}", None

    key = key_string.strip()
    try:
        raw = b58decode(key)
    except ValueError:
        return (
            False,
            f"Invalid API key: not valid Base58 text. {_GET_KEY_HINT}",
            None,
        )

    if len(raw) != KEY_PAYLOAD_BYTES:
        return (
            False,
            f"Invalid API key: decoded to {len(raw)} bytes, expected "
            f"{KEY_PAYLOAD_BYTES}. {_GET_KEY_HINT}",
            None,
        )

    # TODO: verify the 84-byte payload's embedded Ed25519 signature against
    # the plugin's embedded public key once server-side issuance is live.
    return True, "Key accepted", None


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

_SETTINGS_KEY = "waveshed/api_key"


def get_stored_key() -> str:
    """Read the API key from persistent QGIS settings."""
    return QgsSettings().value(_SETTINGS_KEY, "")


def store_key(key_string: str) -> None:
    """Write an API key to persistent QGIS settings."""
    QgsSettings().setValue(_SETTINGS_KEY, key_string)


# ---------------------------------------------------------------------------
# Convenience wrappers
# ---------------------------------------------------------------------------


def check_key_or_raise() -> datetime.date | None:
    """Validate the stored API key and raise :class:`ApiKeyError` on failure.

    Returns ``None`` (no expiry tracking yet).
    """
    key = get_stored_key()
    if not key:
        raise ApiKeyError(
            f"API key required to run the Aether engine. Enter your key in "
            f"Waveshed Settings — {_GET_KEY_HINT}."
        )

    is_valid, message, expiry = validate_api_key(key)
    if not is_valid:
        raise ApiKeyError(message)

    return expiry


def apply_license_env(env: dict[str, str]) -> dict[str, str]:
    """Validate the stored key and set ``AETHER_LICENSE`` on *env* in place.

    Call immediately before launching an ``aether_core`` subprocess so a
    missing/invalid key surfaces as a friendly :class:`ApiKeyError` instead of
    an opaque engine failure. Raises :class:`ApiKeyError` if validation fails
    (leaving *env* unmodified).
    """
    check_key_or_raise()
    env[LICENSE_ENV_VAR] = get_stored_key()
    return env
