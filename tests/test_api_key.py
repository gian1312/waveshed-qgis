"""Unit tests for waveshed.core.api_key — Base58 v3 licence key validation.

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


def _days(date: datetime.date) -> int:
    return (date - ak.KEY_EPOCH).days


#: Far-future expiry so the fixtures never age into "expired".
FAR = datetime.date(2099, 12, 31)


def _v3_raw(
    exp: datetime.date = FAR,
    fingerprint: bytes | None = None,
    license_type: int = 2,
    version: int = 3,
    flags: int | None = None,
) -> bytes:
    """Build a 188-byte v3 licence (placeholder chains and signature).

    [ver][key_id][type][flags] licence_id(16) issued/exp/maint(u16 BE)
    epoch_from epoch_to A_from(32) B_to(32) fingerprint(32) sig(64).
    """
    fp = fingerprint if fingerprint is not None else bytes(32)
    if flags is None:
        flags = 1 if any(fp) else 0
    raw = (
        bytes([version, 0, license_type, flags])
        + bytes(range(1, 17))                    # licence_id
        + _days(datetime.date(2026, 9, 1)).to_bytes(2, "big")
        + _days(exp).to_bytes(2, "big")
        + _days(datetime.date(2027, 6, 30)).to_bytes(2, "big")
        + bytes([2, 5])                          # epochs 2026-Q3 .. 2027-Q2
        + bytes([0xA1]) * 32 + bytes([0xB2]) * 32
        + fp
        + bytes([0x5A]) * 64                     # ed25519 sig
    )
    assert len(raw) == 188
    return raw


VALID_KEY = _b58encode(_v3_raw())


class TestBase58(unittest.TestCase):
    def test_roundtrip(self):
        for raw in (b"\x00", b"hello", _v3_raw(), b"\x00\x00\x01\x02"):
            self.assertEqual(ak.b58decode(_b58encode(raw)), raw)

    def test_invalid_chars_raise(self):
        for bad in ("0", "O", "I", "l", "abc def"):
            with self.assertRaises(ValueError):
                ak.b58decode(bad)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            ak.b58decode("")


class TestValidateApiKey(unittest.TestCase):
    def test_valid_v3_key(self):
        ok, msg, expiry = ak.validate_api_key(VALID_KEY)
        self.assertTrue(ok, msg)
        self.assertEqual(expiry, FAR)
        self.assertIn("commercial", msg)
        self.assertIn("2099-12-31", msg)
        self.assertIn("any machine", msg)
        self.assertGreater(len(VALID_KEY), 250)  # ~257 chars

    def test_valid_key_tolerates_surrounding_whitespace(self):
        ok, _msg, _ = ak.validate_api_key(f"  {VALID_KEY}\n")
        self.assertTrue(ok)

    def test_empty_key(self):
        ok, msg, _ = ak.validate_api_key("   ")
        self.assertFalse(ok)
        self.assertIn("waveshed.io/get-key", msg)

    def test_non_base58(self):
        ok, msg, _ = ak.validate_api_key("has_0_and_O_invalid")
        self.assertFalse(ok)
        self.assertIn("Base58", msg)

    def test_wrong_length(self):
        short = _b58encode(bytes(range(1, 41)))  # 40 bytes
        ok, msg, _ = ak.validate_api_key(short)
        self.assertFalse(ok)
        self.assertIn("expected 188", msg)

    def test_expired_key_rejected_with_date(self):
        past = datetime.date(2026, 1, 2)
        ok, msg, expiry = ak.validate_api_key(_b58encode(_v3_raw(exp=past)))
        self.assertFalse(ok)
        self.assertEqual(expiry, past)
        self.assertIn("expired on 2026-01-02", msg)

    def test_locked_key_message(self):
        ok, msg, _ = ak.validate_api_key(_b58encode(_v3_raw(fingerprint=bytes(range(1, 33)))))
        self.assertTrue(ok, msg)
        self.assertIn("this machine only", msg)


class TestKeyVersions(unittest.TestCase):
    """Only v3 (188-byte) keys are accepted; v1/v2 get an explanation."""

    def test_legacy_v1_v2_rejected_with_hint(self):
        for n in (84, 116):
            ok, msg, _ = ak.validate_api_key(_b58encode(bytes(range(1, n + 1))))
            self.assertFalse(ok, f"{n} bytes should be rejected")
            self.assertIn("old-format", msg)

    def test_rejects_other_lengths(self):
        for n in (83, 187, 189):
            ok, msg, _ = ak.validate_api_key(_b58encode(bytes(range(1, n + 1))))
            self.assertFalse(ok, f"{n} bytes should be rejected")
            self.assertIn("188", msg)

    def test_rejects_bad_header_fields(self):
        for raw, needle in (
            (_v3_raw(version=2), "version"),
            (_v3_raw(license_type=0), "type"),
            (_v3_raw(license_type=6), "type"),
            (_v3_raw(flags=2), "flags"),
            (_v3_raw(flags=1), "flags"),                            # locked, no fingerprint
            (_v3_raw(fingerprint=bytes([7]) * 32, flags=0), "flags"),  # fingerprint, not locked
        ):
            ok, msg, _ = ak.validate_api_key(_b58encode(raw))
            self.assertFalse(ok)
            self.assertIn(needle, msg)


class TestInspectKey(unittest.TestCase):
    """Structural metadata decode (no signature verification)."""

    def test_returns_none_for_invalid(self):
        self.assertIsNone(ak.inspect_key(""))
        self.assertIsNone(ak.inspect_key("not-valid-0O"))
        self.assertIsNone(ak.inspect_key(_b58encode(bytes(range(1, 51)))))  # 50 B
        self.assertIsNone(ak.inspect_key(_b58encode(bytes(range(1, 85)))))  # v1

    def test_unlocked_metadata(self):
        info = ak.inspect_key(VALID_KEY)
        self.assertEqual(info["version"], 3)
        self.assertEqual(info["type_name"], "commercial")
        self.assertEqual(info["licence_id_hex"], bytes(range(1, 17)).hex())
        self.assertEqual((info["epoch_from"], info["epoch_to"]), (2, 5))
        self.assertEqual(info["maintenance"], datetime.date(2027, 6, 30))
        self.assertFalse(info["locked"])
        self.assertIsNone(info["fingerprint_hex"])

    def test_locked_with_fingerprint(self):
        fp = bytes(range(1, 33))
        info = ak.inspect_key(_b58encode(_v3_raw(fingerprint=fp)))
        self.assertTrue(info["locked"])
        self.assertEqual(info["fingerprint_hex"], fp.hex())

    def test_expired_key_still_inspects(self):
        past = datetime.date(2026, 3, 1)
        info = ak.inspect_key(_b58encode(_v3_raw(exp=past, license_type=1)))
        self.assertEqual(info["expiry"], past)
        self.assertEqual(info["type_name"], "free non-commercial")


def _wrap(key: str, every: int = 20, newline_at: int = 40) -> str:
    """Simulate a key copied across wrapped terminal lines.

    Inserts a space every *every* characters and a newline every *newline_at*
    characters, so the result carries both internal spaces and embedded
    newlines while decoding to the same key once whitespace is removed.
    """
    out: list[str] = []
    for i, ch in enumerate(key):
        if i and i % newline_at == 0:
            out.append("\n")
        elif i and i % every == 0:
            out.append(" ")
        out.append(ch)
    return "".join(out)


class TestKeyNormalization(unittest.TestCase):
    """Keys copied from a wrapped terminal (internal spaces / newlines) must
    still validate, inspect, and store as the same clean key."""

    def test_normalize_removes_all_whitespace(self):
        self.assertEqual(ak._normalize_key(" a b\tc\r\nd "), "abcd")
        self.assertEqual(ak._normalize_key(" x y\n"), "xy")  # unicode ws
        self.assertEqual(ak._normalize_key(""), "")
        self.assertEqual(ak._normalize_key(None), "")

    def test_key_with_internal_whitespace_validates(self):
        wrapped = _wrap(VALID_KEY)
        self.assertIn(" ", wrapped)
        self.assertIn("\n", wrapped)
        self.assertNotEqual(wrapped, VALID_KEY)
        ok, msg, _ = ak.validate_api_key(wrapped)
        self.assertTrue(ok, msg)

    def test_locked_key_with_internal_whitespace_validates_and_inspects(self):
        fp = bytes(range(1, 33))
        wrapped = _wrap(_b58encode(_v3_raw(fingerprint=fp)))
        ok, msg, _ = ak.validate_api_key(wrapped)
        self.assertTrue(ok, msg)
        info = ak.inspect_key(wrapped)
        self.assertEqual(info["version"], 3)
        self.assertTrue(info["locked"])
        self.assertEqual(info["fingerprint_hex"], fp.hex())

    def test_embedded_newline_only_validates(self):
        mid = len(VALID_KEY) // 2
        with_newline = VALID_KEY[:mid] + "\n" + VALID_KEY[mid:]
        ok, msg, _ = ak.validate_api_key(with_newline)
        self.assertTrue(ok, msg)

    def test_base58_error_hints_at_copy_issue(self):
        ok, msg, _ = ak.validate_api_key("has_0_and_O_invalid")
        self.assertFalse(ok)
        self.assertIn("Base58", msg)
        self.assertIn("terminal", msg.lower())  # copy-issue hint


class TestStoreKeyNormalization(unittest.TestCase):
    """store_key persists the normalized (whitespace-free) form."""

    def setUp(self):
        ak.store_key("")

    def tearDown(self):
        ak.store_key("")

    def test_store_persists_whitespace_free_form(self):
        key = _b58encode(_v3_raw(fingerprint=bytes(range(1, 33))))
        wrapped = _wrap(key)
        ak.store_key(wrapped)
        stored = ak.get_stored_key()
        self.assertEqual(stored, key)          # clean, whitespace-free
        self.assertNotIn(" ", stored)
        self.assertNotIn("\n", stored)
        # ...and the stored form is directly usable.
        ok, _msg, _ = ak.validate_api_key(stored)
        self.assertTrue(ok)


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

    def test_expired_stored_key_raises(self):
        ak.store_key(_b58encode(_v3_raw(exp=datetime.date(2026, 1, 2))))
        with self.assertRaises(ak.ApiKeyError):
            ak.check_key_or_raise()


if __name__ == "__main__":
    unittest.main()
