"""Unit tests for waveshed.core.api_key — Base58 API key validation.

QGIS stubs provided by conftest.py.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import datetime
import unittest

from waveshed.core import api_key as ak


def _b58encode(raw: bytes) -> str:
    """Minimal Base58 (Bitcoin alphabet) encoder for building test keys."""
    alphabet = ak._B58_ALPHABET
    num = int.from_bytes(raw, "big")
    out = ""
    while num > 0:
        num, rem = divmod(num, 58)
        out = alphabet[rem] + out
    pad = len(raw) - len(raw.lstrip(b"\x00"))
    return alphabet[0] * pad + out


# 84 distinct non-zero bytes -> a structurally valid ~115-char key.
VALID_KEY = _b58encode(bytes(range(1, 85)))


def _v1_raw(exp_days: int = 258) -> bytes:
    """Build an 84-byte v1 payload: exp(2) + maint(2) + seed(16) + sig(64)."""
    raw = (
        exp_days.to_bytes(2, "big")
        + (7).to_bytes(2, "big")       # maint
        + bytes([0xAB]) * 16           # seed
        + bytes([0x5A]) * 64           # ed25519 sig
    )
    assert len(raw) == 84
    return raw


def _v2_raw(exp_days: int = 258, fingerprint: bytes | None = None) -> bytes:
    """Build a 116-byte v2 payload: v1 fields + fingerprint(32) + sig(64)."""
    if fingerprint is None:
        fingerprint = bytes(range(1, 33))  # non-zero -> machine-locked
    assert len(fingerprint) == 32
    raw = (
        exp_days.to_bytes(2, "big")
        + (7).to_bytes(2, "big")       # maint
        + bytes([0xAB]) * 16           # seed
        + fingerprint                  # 32-byte machine fingerprint
        + bytes([0x5A]) * 64           # ed25519 sig
    )
    assert len(raw) == 116
    return raw


class TestBase58(unittest.TestCase):
    def test_roundtrip(self):
        for raw in (b"\x00", b"hello", bytes(range(1, 85)), b"\x00\x00\x01\x02"):
            self.assertEqual(ak.b58decode(_b58encode(raw)), raw)

    def test_invalid_chars_raise(self):
        for bad in ("0", "O", "I", "l", "abc def"):
            with self.assertRaises(ValueError):
                ak.b58decode(bad)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            ak.b58decode("")


class TestValidateApiKey(unittest.TestCase):
    def test_valid_84_byte_key(self):
        ok, msg, expiry = ak.validate_api_key(VALID_KEY)
        self.assertTrue(ok, msg)
        self.assertIsNone(expiry)
        self.assertGreater(len(VALID_KEY), 100)  # ~115 chars

    def test_valid_key_tolerates_surrounding_whitespace(self):
        ok, _msg, _ = ak.validate_api_key(f"  {VALID_KEY}\n")
        self.assertTrue(ok)

    def test_empty_key(self):
        ok, msg, _ = ak.validate_api_key("   ")
        self.assertFalse(ok)
        self.assertIn("waveshed.io/downloads", msg)

    def test_non_base58(self):
        ok, msg, _ = ak.validate_api_key("has_0_and_O_invalid")
        self.assertFalse(ok)
        self.assertIn("Base58", msg)

    def test_wrong_length(self):
        short = _b58encode(bytes(range(1, 41)))  # 40 bytes
        ok, msg, _ = ak.validate_api_key(short)
        self.assertFalse(ok)
        self.assertIn("expected 84", msg)


class TestKeyVersions(unittest.TestCase):
    """v1 (84-byte) and v2 (116-byte node-locked) keys are both accepted."""

    def test_accepts_v1_84_bytes(self):
        ok, msg, _ = ak.validate_api_key(_b58encode(_v1_raw()))
        self.assertTrue(ok, msg)

    def test_accepts_v2_116_bytes(self):
        ok, msg, _ = ak.validate_api_key(_b58encode(_v2_raw()))
        self.assertTrue(ok, msg)

    def test_rejects_other_lengths(self):
        # 83 (one short of v1), 100 (between), 117 (one over v2) all rejected,
        # and the message lists the accepted lengths.
        for n in (83, 100, 117):
            key = _b58encode(bytes(range(1, n + 1)))
            ok, msg, _ = ak.validate_api_key(key)
            self.assertFalse(ok, f"{n} bytes should be rejected")
            self.assertIn("84 or 116", msg)


class TestInspectKey(unittest.TestCase):
    """Structural metadata decode (no signature verification)."""

    def test_returns_none_for_invalid(self):
        self.assertIsNone(ak.inspect_key(""))
        self.assertIsNone(ak.inspect_key("not-valid-0O"))
        self.assertIsNone(ak.inspect_key(_b58encode(bytes(range(1, 51)))))  # 50 B

    def test_v1_metadata(self):
        info = ak.inspect_key(_b58encode(_v1_raw()))
        self.assertEqual(info["version"], 1)
        self.assertFalse(info["locked"])
        self.assertIsNone(info["fingerprint_hex"])

    def test_v2_locked_with_nonzero_fingerprint(self):
        fp = bytes(range(1, 33))
        info = ak.inspect_key(_b58encode(_v2_raw(fingerprint=fp)))
        self.assertEqual(info["version"], 2)
        self.assertTrue(info["locked"])
        self.assertEqual(info["fingerprint_hex"], fp.hex())
        self.assertEqual(len(info["fingerprint_hex"]), 64)

    def test_v2_zero_fingerprint_not_locked(self):
        info = ak.inspect_key(_b58encode(_v2_raw(fingerprint=bytes(32))))
        self.assertEqual(info["version"], 2)
        self.assertFalse(info["locked"])
        self.assertEqual(info["fingerprint_hex"], "00" * 32)

    def test_expiry_decode(self):
        info = ak.inspect_key(_b58encode(_v1_raw(exp_days=258)))
        self.assertEqual(
            info["expiry"], ak.KEY_EPOCH + datetime.timedelta(days=258)
        )


class TestLicenseEnv(unittest.TestCase):
    def setUp(self):
        ak.store_key("")

    def tearDown(self):
        ak.store_key("")

    def test_missing_key_raises(self):
        with self.assertRaises(ak.ApiKeyError):
            ak.check_key_or_raise()
        with self.assertRaises(ak.ApiKeyError):
            ak.apply_license_env({})

    def test_injects_and_preserves_existing_env(self):
        ak.store_key(VALID_KEY)
        env = {"RUST_LOG": "info"}
        ak.apply_license_env(env)
        self.assertEqual(env[ak.LICENSE_ENV_VAR], VALID_KEY)
        self.assertEqual(env["RUST_LOG"], "info")  # RUST_LOG preserved

    def test_invalid_stored_key_raises(self):
        ak.store_key("not-valid-0O")  # contains non-Base58 chars
        with self.assertRaises(ak.ApiKeyError):
            ak.apply_license_env({})


if __name__ == "__main__":
    unittest.main()
