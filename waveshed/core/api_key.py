"""Waveshed API key storage and validation.

A Waveshed API key is a Base58 (Bitcoin alphabet) blob whose decoded payload is
either 84 bytes (v1) or 116 bytes (v2, node-locked — v1 plus a 32-byte machine
fingerprint), i.e. roughly 115–160 characters. Validation here is structural
only: non-empty, valid Base58 charset, and a decoded length that is one of the
accepted payload sizes. Real Ed25519 signature verification against the
plugin's embedded public key will be added once key issuance is live (see the
TODO in :func:`validate_api_key`).

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

#: Accepted decoded payload lengths (bytes). v1 keys are 84 bytes; v2
#: node-locked keys are 116 bytes (v1 payload + a 32-byte machine fingerprint).
KEY_PAYLOAD_LENGTHS = (84, 116)

#: Epoch for license expiry: the u16 BE value at payload bytes ``[0:2]`` is the
#: number of days since this date.
KEY_EPOCH = datetime.date(2026, 1, 1)

#: Environment variable the Aether engine reads the license/key from.
LICENSE_ENV_VAR = "AETHER_LICENSE"


# ---------------------------------------------------------------------------
# Base58 (Bitcoin alphabet) decode — pure stdlib
# ---------------------------------------------------------------------------

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {ch: i for i, ch in enumerate(_B58_ALPHABET)}


def _normalize_key(key_string: str) -> str:
    """Return *key_string* with every whitespace character removed.

    A Base58 key never contains whitespace, so a genuine key copied out of a
    wrapped terminal line — which can pick up internal spaces, tabs, or
    newlines in addition to leading/trailing padding — is still the same key
    once all whitespace is stripped. Removing it (not just the ends) is
    therefore lossless and lets such copy artefacts validate correctly.
    ``str.split()`` treats every Unicode whitespace character as a separator.
    """
    return "".join((key_string or "").split())


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

    A valid key is Base58 text whose payload decodes to one of the accepted
    :data:`KEY_PAYLOAD_LENGTHS` (84 bytes for v1, 116 for v2 node-locked).

    Returns
    -------
    tuple[bool, str, datetime.date | None]
        ``(is_valid, message, expiry_date_or_None)``
    """
    if not key_string or not key_string.strip():
        return False, f"No API key provided. {_GET_KEY_HINT}", None

    key = _normalize_key(key_string)
    try:
        raw = b58decode(key)
    except ValueError:
        return (
            False,
            f"Invalid API key: not valid Base58 text (check for characters "
            f"accidentally copied from the terminal). {_GET_KEY_HINT}",
            None,
        )

    if len(raw) not in KEY_PAYLOAD_LENGTHS:
        accepted = " or ".join(str(n) for n in KEY_PAYLOAD_LENGTHS)
        return (
            False,
            f"Invalid API key: decoded to {len(raw)} bytes, expected "
            f"{accepted}. {_GET_KEY_HINT}",
            None,
        )

    # TODO: verify the payload's embedded Ed25519 signature against the
    # plugin's embedded public key once server-side issuance is live.
    return True, "Key accepted", None


# ---------------------------------------------------------------------------
# Structural inspection (public metadata only — NO signature verification)
# ---------------------------------------------------------------------------


def inspect_key(key_string: str) -> dict | None:
    """Return public metadata decoded from a structurally valid key.

    Pure standard library and **structural only** — this does NOT verify the
    Ed25519 signature (the verifying public key lives in the engine binary), so
    treat the result as advisory display metadata, never as proof of validity.

    Returns ``None`` if *key_string* is not structurally valid. Otherwise a
    dict:

    ``version``
        ``1`` (84-byte payload) or ``2`` (116-byte node-locked payload).
    ``expiry``
        :class:`datetime.date` decoded from payload bytes ``[0:2]`` (u16 BE
        days since :data:`KEY_EPOCH`).
    ``locked``
        ``True`` for a v2 key whose 32-byte fingerprint field (payload bytes
        ``[20:52]``) is non-zero, i.e. bound to a specific machine.
    ``fingerprint_hex``
        Lowercase hex of the v2 fingerprint field, or ``None`` for v1.
    """
    is_valid, _message, _ = validate_api_key(key_string)
    if not is_valid:
        return None

    raw = b58decode(_normalize_key(key_string))
    version = 2 if len(raw) == 116 else 1

    exp_days = int.from_bytes(raw[0:2], "big")
    expiry = KEY_EPOCH + datetime.timedelta(days=exp_days)

    fingerprint = raw[20:52] if version == 2 else b""
    return {
        "version": version,
        "expiry": expiry,
        "locked": any(fingerprint),
        "fingerprint_hex": fingerprint.hex() if version == 2 else None,
    }


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

_SETTINGS_KEY = "waveshed/api_key"


def get_stored_key() -> str:
    """Read the API key from persistent QGIS settings."""
    return QgsSettings().value(_SETTINGS_KEY, "")


def store_key(key_string: str) -> None:
    """Write an API key to persistent QGIS settings.

    The key is normalized (all whitespace removed) before storage so the saved
    value is a clean, whitespace-free Base58 string regardless of copy-paste
    artefacts.
    """
    QgsSettings().setValue(_SETTINGS_KEY, _normalize_key(key_string))


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
