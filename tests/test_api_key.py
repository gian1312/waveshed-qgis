"""Unit tests for waveshed.core.api_key — Base58 API key validation.

QGIS stubs provided by conftest.py.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

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
