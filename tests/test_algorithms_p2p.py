"""Unit tests for waveshed/algorithms — the Processing-toolbox P2P algorithm.

QGIS (including the QgsProcessing stubs) is provided by conftest.py.

The algorithm used to end by running ``aether_export`` on ``<name>.tiles``.
aether_core writes ``<name>.csv`` directly and never writes a .tiles/.bit pair
for a P2P job, so the toolbox algorithm raised on every single run — the GUI
tab has had the correct "verify the CSV" version all along. These tests pin
that the export step is gone and that a missing CSV is what gets reported.
"""

# Bootstrap stubs before any plugin import.
import conftest  # noqa: F401

import inspect
import io
import os
import tempfile
import unittest
from unittest import mock

from qgis.core import QgsProcessingException

import waveshed.algorithms.p2p as alg
import waveshed.core.terrain_adapter as ta


_OUTPUT_NAME = "p2p_20260101_000000"


def _code_only(source: str) -> str:
    """*source* without ``#`` comment lines, so a comment naming the old call
    cannot satisfy (or break) a wiring assertion."""
    return "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("#"))


class _FakeFeedback:
    """The handful of QgsProcessingFeedback methods the algorithm calls."""

    def __init__(self):
        self.texts = []

    def setProgressText(self, text):
        self.texts.append(text)

    def setProgress(self, pct):
        pass

    def pushInfo(self, msg):
        pass

    def isCanceled(self):
        return False


class _FakeProc:
    """A subprocess.Popen stand-in that exits 0 having printed nothing."""

    def __init__(self, *args, **kwargs):
        self.args = args[0] if args else []
        self.returncode = 0
        self.stdout = io.StringIO("")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def wait(self):
        return 0

    def terminate(self):
        pass


class _StubbedP2P(alg.P2PAlgorithm):
    """P2PAlgorithm plus the ``parameterAs*`` accessors QGIS normally supplies.

    conftest's ``QgsProcessingAlgorithm`` is inert, so the real base class
    methods do not exist under test — the algorithm's own pipeline is what is
    being exercised here, not QGIS's parameter marshalling.
    """

    _DOUBLES = {
        "TX_LAT": 47.0, "TX_LON": 8.0, "TX_HEIGHT": 20.0,
        "RX_LAT": 47.05, "RX_LON": 8.05, "RX_HEIGHT": 10.0,
    }

    def __init__(self, output_dir):
        self._output_dir = output_dir

    def parameterAsRasterLayer(self, parameters, name, context):
        layer = mock.Mock()
        layer.source.return_value = "/data/dem.tif"
        return layer

    def parameterAsFile(self, parameters, name, context):
        return ""

    def parameterAsDouble(self, parameters, name, context):
        return self._DOUBLES[name]

    def parameterAsEnum(self, parameters, name, context):
        return 0

    def parameterAsString(self, parameters, name, context):
        return self._output_dir


class _Run:
    """Context manager patching everything the algorithm shells out to."""

    def __init__(self, output_dir):
        self.output_dir = output_dir
        self.binaries = []
        self._patches = []

    def _find_binary(self, name):
        self.binaries.append(name)
        return f"/nonexistent/{name}"

    def __enter__(self):
        fixed_dt = mock.Mock()
        fixed_dt.now.return_value.strftime.return_value = "20260101_000000"
        self._patches = [
            mock.patch.object(alg, "datetime", fixed_dt),
            mock.patch.object(alg, "find_binary", side_effect=self._find_binary),
            mock.patch.object(alg, "prepare_terrain",
                              return_value=self.output_dir),
            mock.patch.object(alg, "build_p2p_job", return_value={}),
            mock.patch.object(alg, "write_job_file",
                              return_value=os.path.join(self.output_dir,
                                                        "job.json")),
            mock.patch.object(alg.api_key, "apply_license_env"),
            # p2p.py no longer imports subprocess at all — every engine run
            # goes through terrain_adapter.run_converter_streaming, so the
            # process fakes are installed on the adapter's subprocess module
            # (the same module object Python-wide).
            mock.patch.object(ta.subprocess, "Popen", _FakeProc),
            mock.patch.object(ta.subprocess, "run",
                              side_effect=AssertionError(
                                  "no subprocess.run: P2P has no export step")),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self._patches):
            patch.stop()
        return False


class TestP2PProducesItsCsvDirectly(unittest.TestCase):

    def _run(self, out_dir):
        with _Run(out_dir) as run:
            algo = _StubbedP2P(out_dir)
            result = algo.processAlgorithm({}, mock.Mock(), _FakeFeedback())
        return result, run

    def test_returns_the_csv_aether_core_wrote(self):
        with tempfile.TemporaryDirectory() as out_dir:
            csv_path = os.path.join(out_dir, _OUTPUT_NAME + ".csv")
            open(csv_path, "w").close()
            result, _run = self._run(out_dir)
        self.assertEqual(result[alg.P2PAlgorithm.OUTPUT_DIR], csv_path)

    def test_aether_export_is_never_launched(self):
        # The bug: find_binary("aether_export") + subprocess.run on a .tiles
        # file that aether_core does not write. _Run makes subprocess.run
        # explode, so reaching it at all fails the test.
        with tempfile.TemporaryDirectory() as out_dir:
            open(os.path.join(out_dir, _OUTPUT_NAME + ".csv"), "w").close()
            _result, run = self._run(out_dir)
        self.assertEqual(run.binaries, ["aether_core"])

    def test_missing_csv_is_reported_against_the_csv_path(self):
        with tempfile.TemporaryDirectory() as out_dir:
            with self.assertRaises(QgsProcessingException) as caught:
                self._run(out_dir)
        self.assertIn(_OUTPUT_NAME + ".csv", str(caught.exception))
        self.assertIn("aether_core", str(caught.exception))

    def test_a_stray_tiles_file_does_not_count_as_output(self):
        # The old code took <name>.tiles as its export input, so a leftover
        # tiles file from some other run was the only thing standing between
        # the algorithm and "success" with no P2P results at all.
        with tempfile.TemporaryDirectory() as out_dir:
            open(os.path.join(out_dir, _OUTPUT_NAME + ".tiles"), "w").close()
            open(os.path.join(out_dir, _OUTPUT_NAME + ".bit"), "w").close()
            with self.assertRaises(QgsProcessingException):
                self._run(out_dir)

    def test_source_no_longer_mentions_the_exporter(self):
        source = _code_only(inspect.getsource(alg.P2PAlgorithm.processAlgorithm))
        self.assertNotIn("aether_export", source)
        self.assertNotIn(".tiles", source)


class TestP2PTabPricesTheTilePool(unittest.TestCase):
    """The tab's pre-run estimate must price the pool, like Site Analysis.

    ``estimate_terrain_disk_mb`` counts every tile a link needs whether or not
    it is already on disk, so a fully-cached link still warned about tens of
    gigabytes of "download" — which is how users learn to click through the
    warning. ``terrain_plan`` prices only what is missing.
    """

    def test_run_uses_terrain_plan(self):
        import waveshed.gui.p2p_tab as tab
        source = _code_only(inspect.getsource(tab.P2PTab._on_run))
        self.assertIn("terrain_plan(", source)
        self.assertNotIn("estimate_terrain_disk_mb", source)

    def test_module_no_longer_imports_the_whole_set_estimator(self):
        import waveshed.gui.p2p_tab as tab
        self.assertTrue(hasattr(tab, "terrain_plan"))
        self.assertFalse(hasattr(tab, "estimate_terrain_disk_mb"))


if __name__ == "__main__":
    unittest.main()
