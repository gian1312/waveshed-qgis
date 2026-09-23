"""Unit tests for the engine version handshake in waveshed/core/binary_manager.

The plugin gates 90 m / 250 m coverage on ``aether_core --version`` BEFORE
any terrain is fetched (CONTRACT.md §1.5 capability gates). An engine that
does not know the flag is the oldest known engine, never the newest.
"""

import conftest  # noqa: F401

import subprocess
import unittest
from unittest import mock

from waveshed.core import binary_manager as bm


class ParseEngineVersion(unittest.TestCase):

    def test_contract_line(self):
        self.assertEqual(bm.parse_engine_version("aether_core 0.4.3\n"), "0.4.3")

    def test_leading_v_and_surrounding_noise(self):
        out = "[Aether] starting\naether_core v0.5.0\n"
        self.assertEqual(bm.parse_engine_version(out), "0.5.0")

    def test_clap_unknown_argument_is_unknown(self):
        out = "error: unexpected argument '--version' found\n\nUsage: aether_core"
        self.assertIsNone(bm.parse_engine_version(out))

    def test_other_binary_name_is_unknown(self):
        self.assertIsNone(bm.parse_engine_version("aether_converter 0.2.5"))
        self.assertIsNone(bm.parse_engine_version(""))


class EngineSupportsResolution(unittest.TestCase):

    def test_legacy_engine_only_the_four(self):
        for old in (None, "0.4.2", "0.4.0", "0.1.0"):
            for r in bm.LEGACY_ENGINE_RESOLUTIONS:
                self.assertTrue(bm.engine_supports_resolution(old, r), (old, r))
            for r in (90, 250, 1, 15):
                self.assertFalse(bm.engine_supports_resolution(old, r), (old, r))

    def test_new_engine_everything(self):
        for new in ("0.4.3", "0.4.10", "0.5.0", "1.0.0"):
            for r in (2, 5, 10, 30, 90, 250):
                self.assertTrue(bm.engine_supports_resolution(new, r), (new, r))

    def test_gate_constant_is_the_first_fixed_engine(self):
        self.assertEqual(bm.COARSE_RESOLUTION_MIN_ENGINE, "0.4.3")


class ReadEngineVersion(unittest.TestCase):

    def _probe(self, **run_kwargs):
        with mock.patch.object(bm, "find_binary", return_value="/x/aether_core"), \
             mock.patch.object(bm.subprocess, "run", **run_kwargs) as run:
            got = bm.read_engine_version()
        return got, run

    def test_reads_the_version(self):
        got, run = self._probe(return_value=mock.Mock(returncode=0, stdout="aether_core 0.4.3\n"))
        self.assertEqual(got, "0.4.3")
        self.assertEqual(run.call_args[0][0], ["/x/aether_core", "--version"])

    def test_old_engine_exit_2_is_unknown(self):
        got, _ = self._probe(return_value=mock.Mock(returncode=2, stdout="", stderr="error: unexpected argument"))
        self.assertIsNone(got)

    def test_missing_binary_is_unknown_not_an_exception(self):
        with mock.patch.object(bm, "find_binary", side_effect=FileNotFoundError("gone")):
            self.assertIsNone(bm.read_engine_version())

    def test_timeout_is_unknown_not_an_exception(self):
        got, _ = self._probe(side_effect=subprocess.TimeoutExpired("aether_core", 5))
        self.assertIsNone(got)


class CheckEngineForJob(unittest.TestCase):

    def test_old_engine_refuses_coarse_resolution_with_actionable_text(self):
        with mock.patch.object(bm, "read_engine_version", return_value=None), \
             mock.patch.object(bm, "find_binary", return_value="/x/aether_core"):
            with self.assertRaises(bm.EngineTooOldError) as ctx:
                bm.check_engine_for_job(250)
        msg = str(ctx.exception)
        self.assertIn("250 m", msg)
        self.assertIn("2, 5, 10, 30", msg)
        self.assertIn("0.4.3 or newer", msg)
        self.assertIn("Nothing was downloaded", msg)

    def test_old_engine_allows_legacy_resolution_and_reports_unknown(self):
        with mock.patch.object(bm, "read_engine_version", return_value=None), \
             mock.patch.object(bm, "find_binary", return_value="/x/aether_core"):
            self.assertIn("unknown", bm.check_engine_for_job(30))

    def test_new_engine_allows_coarse_and_returns_version(self):
        with mock.patch.object(bm, "read_engine_version", return_value="0.4.3"), \
             mock.patch.object(bm, "find_binary", return_value="/x/aether_core"):
            self.assertEqual(bm.check_engine_for_job(90), "0.4.3")

    def test_is_a_runtime_error_for_generic_handlers(self):
        self.assertTrue(issubclass(bm.EngineTooOldError, RuntimeError))


if __name__ == "__main__":
    unittest.main()
