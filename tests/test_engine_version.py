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


class ConverterRepairCapability(unittest.TestCase):
    """``repair_source_tiles`` is gated on ``aether_converter --version``."""

    def test_parse_converter_line(self):
        self.assertEqual(
            bm.parse_engine_version("aether_converter 0.2.6\n", "aether_converter"),
            "0.2.6")
        self.assertIsNone(
            bm.parse_engine_version("aether_core 0.2.6", "aether_converter"))

    def test_gate(self):
        for version, expected in (("0.2.5", False), ("0.2.6", True),
                                  ("0.3.0", True), (None, False)):
            with mock.patch.object(bm, "probe_converter_version",
                                   return_value=version):
                self.assertIs(bm.converter_supports_repair("/x/aether_converter"),
                              expected, version)
        self.assertEqual(bm.CONVERTER_REPAIR_MIN_VERSION, "0.2.6")

    def test_probe_runs_the_converter_and_reads_its_line(self):
        bm._VERSION_CACHE.clear()
        self.addCleanup(bm._VERSION_CACHE.clear)
        ok = mock.Mock(returncode=0, stdout="aether_converter 0.2.6\n", stderr="")
        with mock.patch.object(bm, "prepare_engine_dir"), \
                mock.patch.object(bm.subprocess, "run", return_value=ok) as run:
            self.assertEqual(bm.probe_converter_version("/x/aether_converter"),
                             "0.2.6")
        self.assertEqual(run.call_args[0][0], ["/x/aether_converter", "--version"])


class ConverterTileVoidsCapability(unittest.TestCase):
    """``[Tile] <name> voids=<n>`` is expected from ``aether_converter
    --version`` >= 0.2.11 only (toolkit CONTRACT changelog 46)."""

    def test_gate(self):
        for version, expected in (("0.2.10", False), ("0.2.11", True),
                                  ("0.2.12", True), ("0.3.0", True),
                                  ("0.2.9", False), (None, False)):
            with mock.patch.object(bm, "probe_converter_version",
                                   return_value=version) as probe:
                self.assertIs(
                    bm.converter_supports_tile_voids("/x/aether_converter"),
                    expected, version)
            probe.assert_called_once_with("/x/aether_converter")
        self.assertEqual(bm.CONVERTER_TILE_VOIDS_MIN_VERSION, "0.2.11")


class ConverterBuildingsReadyCapability(unittest.TestCase):
    """``buildings_pbf_ready`` / ``buildings_pbf_wait_s`` are sent to a
    DOWNLOAD job from ``aether_converter --version`` >= 0.2.10 only
    (toolkit CONTRACT changelog 45), to an INGEST job from >= 0.2.12 only
    (CONTRACT item 48): an older build ignores them and reads the building
    directory as it finds it."""

    def _gate(self, fn, cases):
        for version, expected in cases:
            with mock.patch.object(bm, "probe_converter_version",
                                   return_value=version) as probe:
                self.assertIs(fn("/x/aether_converter"), expected, version)
            probe.assert_called_once_with("/x/aether_converter")

    def test_download_gate(self):
        self._gate(bm.converter_supports_buildings_ready,
                   (("0.2.9", False), ("0.2.10", True), ("0.2.11", True),
                    ("0.3.0", True), (None, False)))
        self.assertEqual(bm.CONVERTER_BUILDINGS_READY_MIN_VERSION, "0.2.10")

    def test_ingest_gate(self):
        self._gate(bm.converter_supports_ingest_buildings_ready,
                   (("0.2.10", False), ("0.2.11", False), ("0.2.12", True),
                    ("0.2.13", True), ("0.3.0", True), ("0.1.99", False),
                    (None, False)))
        self.assertEqual(bm.CONVERTER_INGEST_BUILDINGS_READY_MIN_VERSION,
                         "0.2.12")

    def test_list_gate(self):
        # buildings_pbf_list (toolkit CONTRACT changelog 51): download and
        # ingest batch jobs from 0.2.14 only — an older build does not know
        # the field.
        self._gate(bm.converter_supports_buildings_list,
                   (("0.2.12", False), ("0.2.13", False), ("0.2.14", True),
                    ("0.2.15", True), ("0.3.0", True), ("0.1.99", False),
                    (None, False)))
        self.assertEqual(bm.CONVERTER_BUILDINGS_LIST_MIN_VERSION, "0.2.14")


if __name__ == "__main__":
    unittest.main()
