"""Processing P2P tool: frequency / ERP are parameters, and a batch run's
job.tx is the batch's first S row (S3, 2026-11-14).

The tool used to send 433 MHz / 10 W whatever the user wanted, and in batch
mode job.tx was the (ignored) TX_LAT / TX_LON parameters — GPU ITM reads ONE
system elevation under job.tx for every link (engines/p2p.rs, zsys), so that
point could lie anywhere. QGIS is stubbed by conftest.py.
"""

# Bootstrap stubs before any plugin import.
import conftest  # noqa: F401

import os
import tempfile
import unittest
from unittest import mock

from qgis.core import QgsProcessingException

import waveshed.algorithms.coverage as cov
import waveshed.algorithms.p2p as alg

from test_algorithms_p2p import _FakeFeedback, _Run, _StubbedP2P


class _Recorded:
    """Stand-in for a QgsProcessingParameter* class: keeps its arguments."""

    def __init__(self, name, description, **kw):
        self.name, self.description, self.kw = name, description, kw


def _definitions(algo, module):
    """name → _Recorded for every parameter *algo* adds in initAlgorithm."""
    found = {}
    # Subclass each stub so its scoped enums (Type, Behavior) stay reachable.
    patches = [mock.patch.object(module, n, type(n, (_Recorded, getattr(module, n)), {}))
               for n in (
        "QgsProcessingParameterNumber", "QgsProcessingParameterEnum",
        "QgsProcessingParameterFile", "QgsProcessingParameterRasterLayer",
        "QgsProcessingParameterFolderDestination") if hasattr(module, n)]
    for p in patches:
        p.start()
    try:
        algo.addParameter = lambda d: found.__setitem__(d.name, d)
        algo.initAlgorithm({})
    finally:
        for p in patches:
            p.stop()
    return found


class _Params(_StubbedP2P):
    """_StubbedP2P with per-test doubles and an optional batch file."""

    def __init__(self, output_dir, batch="", **doubles):
        super().__init__(output_dir)
        self._batch = batch
        self._DOUBLES = dict(_StubbedP2P._DOUBLES, **doubles)
        self._model = 1  # ITM

    def parameterAsFile(self, parameters, name, context):
        return self._batch

    def parameterAsEnum(self, parameters, name, context):
        return self._model if name == "MODEL" else 0


def _params_sent(algo, out_dir):
    """The P2PParams processAlgorithm hands build_p2p_job."""
    with _Run(out_dir), mock.patch.object(alg, "build_p2p_job",
                                          return_value={}) as build:
        open(os.path.join(out_dir, "p2p_20260101_000000.csv"), "w").close()
        algo.processAlgorithm({}, mock.Mock(), _FakeFeedback())
    return build.call_args.args[0]


class FrequencyAndErpAreParameters(unittest.TestCase):

    def test_same_names_ranges_and_defaults_as_the_coverage_tool(self):
        p2p = _definitions(alg.P2PAlgorithm(), alg)
        cv = _definitions(cov.CoverageAlgorithm(), cov)
        for name in ("FREQ_MHZ", "ERP_WATTS"):
            self.assertIn(name, p2p)
            for key in ("minValue", "maxValue", "defaultValue", "type"):
                self.assertEqual(p2p[name].kw[key], cv[name].kw[key], (name, key))
            self.assertIn("Propagation models only", p2p[name].description)
            self.assertIn("Propagation models only — LOS and MIN_ALT ignore it",
                          cv[name].description)

    def test_location_and_height_say_a_batch_file_overrides_them(self):
        p2p = _definitions(alg.P2PAlgorithm(), alg)
        for name in ("TX_LAT", "TX_LON", "TX_HEIGHT", "RX_LAT", "RX_LON", "RX_HEIGHT"):
            self.assertIn("ignored when a batch file is given", p2p[name].description)

    def test_the_parameters_reach_the_job(self):
        with tempfile.TemporaryDirectory() as out:
            params = _params_sent(_Params(out, FREQ_MHZ=868.0, ERP_WATTS=25.0), out)
        self.assertEqual((params.freq_mhz, params.erp_watts), (868.0, 25.0))
        # Single link: job.tx is the TX parameters.
        self.assertEqual((params.tx_lat, params.tx_lon, params.tx_height, params.tx_mode),
                         (47.0, 8.0, 20.0, "AGL"))


class BatchTxIsTheFirstSource(unittest.TestCase):

    def _batch(self, d, text):
        path = os.path.join(d, "batch.csv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_job_tx_is_the_first_s_row_not_the_tx_parameters(self):
        with tempfile.TemporaryDirectory() as out:
            batch = self._batch(out, "R,RX0,46.70,7.20,5.0,AGL\n"
                                     "S,TX1,46.90,7.40,25.0,AMSL\n"
                                     "S,TX2,46.95,7.45,30.0,AGL\n")
            params = _params_sent(_Params(out, batch=batch, FREQ_MHZ=150.0,
                                          ERP_WATTS=2.0), out)
        self.assertEqual((params.tx_lat, params.tx_lon, params.tx_height, params.tx_mode),
                         (46.90, 7.40, 25.0, "AMSL"))
        self.assertEqual((params.freq_mhz, params.erp_watts), (150.0, 2.0))

    def test_a_batch_without_a_source_is_refused(self):
        with tempfile.TemporaryDirectory() as out:
            batch = self._batch(out, "R,RX0,46.70,7.20,5.0,AGL\n")
            with self.assertRaises(QgsProcessingException) as caught:
                _params_sent(_Params(out, batch=batch), out)
        self.assertIn("no source (S) row", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
