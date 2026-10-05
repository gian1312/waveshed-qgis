"""Waveshed API key storage and validation.

A Waveshed API key is an Aether licence in format v3 (AETHER
``Design Documents/License-v3.md``): a Base58 (Bitcoin alphabet) blob that
decodes to exactly 188 bytes (~257 characters) -- version byte 3, signing key
id, licence type, flags (bit 0 = node-locked), licence id, issued / expiry /
maintenance days, covered release epochs, two key-chain values, the machine
fingerprint and a 64-byte Ed25519 signature. Validation here is structural
only (charset, length, version, type, flags, expiry). The signature, the epoch
coverage of the installed engine, node-lock and revocation are checked by the
engine binary itself at start-up.

Pure standard library — no external dependencies (no PyNaCl / base58 package).
"""

from __future__ import annotations

import datetime

from qgis.core import QgsSettings

from . import site_links


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ApiKeyError(Exception):
    """Raised when an API key is missing, invalid, or expired."""

    pass


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Where users obtain a key (the "Get API Key" button). Built by
#: ``site_links`` so the preview flag lives in one place.
GET_API_KEY_URL = site_links.GET_KEY_URL
_GET_KEY_HINT = f"Get a key at {GET_API_KEY_URL}"

#: Accepted decoded payload lengths (bytes): licence format v3 only.
KEY_PAYLOAD_LENGTHS = (188,)

#: Licence format version byte (payload byte ``[0]``).
KEY_VERSION = 3

#: Decoded lengths of the retired v1 / v2 keys (seed-in-key formats), which
#: current engines refuse -- recognised only to explain why.
LEGACY_KEY_LENGTHS = (84, 116)

#: Licence ``type`` byte (payload byte ``[2]``) -> display name.
LICENSE_TYPES = {
    1: "free non-commercial",
    2: "commercial",
    3: "governmental",
    4: "organisational",
    5: "evaluation",
}

#: Epoch for license expiry: the u16 BE days-since-this-date values at payload
#: bytes ``[20:22]`` (issued), ``[22:24]`` (expiry) and ``[24:26]`` (maintenance).
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


def _parse_v3(raw: bytes) -> dict | str:
    """Decode the public header of a v3 licence, or return an error message."""
    if len(raw) in LEGACY_KEY_LENGTHS:
        return (
            "This is an old-format key (licence v1/v2), which the current Aether "
            "engine no longer accepts. Please get a new key."
        )
    if len(raw) not in KEY_PAYLOAD_LENGTHS:
        return (
            f"Invalid API key: decoded to {len(raw)} bytes, expected "
            f"{KEY_PAYLOAD_LENGTHS[0]}."
        )
    if raw[0] != KEY_VERSION:
        return f"Invalid API key: unsupported licence format version {raw[0]}."
    if raw[2] not in LICENSE_TYPES:
        return f"Invalid API key: unknown licence type {raw[2]}."
    flags = raw[3]
    fingerprint = raw[92:124]
    if flags & ~1 or bool(flags & 1) != any(fingerprint):
        return "Invalid API key: inconsistent licence flags."
    if raw[26] > raw[27]:
        return "Invalid API key: inconsistent release range."

    def day(offset: int) -> datetime.date:
        return KEY_EPOCH + datetime.timedelta(days=int.from_bytes(raw[offset:offset + 2], "big"))

    locked = bool(flags & 1)
    return {
        "version": KEY_VERSION,
        "key_id": raw[1],
        "type": raw[2],
        "type_name": LICENSE_TYPES[raw[2]],
        "licence_id_hex": raw[4:20].hex(),
        "issued": day(20),
        "expiry": day(22),
        "maintenance": day(24),
        "epoch_from": raw[26],
        "epoch_to": raw[27],
        "locked": locked,
        "fingerprint_hex": fingerprint.hex() if locked else None,
    }


def validate_api_key(key_string: str) -> tuple[bool, str, datetime.date | None]:
    """Validate a Waveshed API key structurally.

    A valid key is Base58 text that decodes to a 188-byte v3 licence with a
    known type, consistent flags, and an expiry date not in the past. On
    success the message summarises the key (type, expiry, machine lock).

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

    info = _parse_v3(raw)
    if isinstance(info, str):
        return False, f"{info} {_GET_KEY_HINT}", None

    expiry = info["expiry"]
    if expiry < datetime.date.today():
        return (
            False,
            f"API key expired on {expiry.isoformat()}. {_GET_KEY_HINT}",
            expiry,
        )

    lock = "this machine only" if info["locked"] else "any machine"
    # The Ed25519 signature is verified by the engine binary, not here.
    return (
        True,
        f"Key accepted: {info['type_name']} licence, valid until "
        f"{expiry.isoformat()}, runs on {lock}",
        expiry,
    )


# ---------------------------------------------------------------------------
# Structural inspection (public metadata only — NO signature verification)
# ---------------------------------------------------------------------------


def inspect_key(key_string: str) -> dict | None:
    """Return public metadata decoded from a structurally valid v3 key.

    Pure standard library and **structural only** — this does NOT verify the
    Ed25519 signature (the verifying public key lives in the engine binary), so
    treat the result as advisory display metadata, never as proof of validity.
    An expired key still inspects (its ``expiry`` says so).

    Returns ``None`` if *key_string* is not a structurally valid v3 key.
    Otherwise a dict with ``version`` (3), ``key_id``, ``type`` /
    ``type_name``, ``licence_id_hex``, ``issued`` / ``expiry`` /
    ``maintenance`` (:class:`datetime.date`), ``epoch_from`` / ``epoch_to``
    (covered engine release quarters), ``locked`` and ``fingerprint_hex``
    (the 64-hex machine fingerprint of a node-locked key, else ``None``).
    """
    key = _normalize_key(key_string)
    if not key:
        return None
    try:
        info = _parse_v3(b58decode(key))
    except ValueError:
        return None
    return None if isinstance(info, str) else info


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

    Returns the key's expiry date.
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
