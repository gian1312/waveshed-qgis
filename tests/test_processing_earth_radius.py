"""Processing tools: the "Earth radius" parameter follows the GUI's rule
(owner, 2026-11-15): Default = Real (k = 1) for LOS / MIN_ALT, 4/3 for ITM;
Real goes to the engine only from aether_core ``EARTH_REAL_MIN_ENGINE``
(0.5.2) — an older or unknown engine gets FOUR_THIRDS and a warning.
QGIS is stubbed by conftest.py.
"""

# Bootstrap stubs before any plugin import.
import conftest  # noqa: F401

import tempfile
import unittest
from unittest import mock

import waveshed.algorithms.coverage as cov
import waveshed.algorithms.p2p as alg
from waveshed.core import job_builder as jb

from test_algorithms_p2p import _FakeFeedback
from test_processing_p2p import _Params, _definitions, _params_sent

DEFAULT, REAL, FOUR_THIRDS = 0, 1, 2
LABELS = ["Default (LOS: Real, Propagation: 4/3)", "Real (k = 1)", "4/3 earth"]


class _Stop(Exception):
    pass


class _Coverage(cov.CoverageAlgorithm):
    _DOUBLES = {"TX_LAT": 46.8, "TX_LON": 7.3, "TX_HEIGHT": 30.0,
                "FREQ_MHZ": 433.0, "ERP_WATTS": 10.0}

    def __init__(self, out, model, earth):
        self._out, self._enums = out, {"MODEL": model, "EARTH_RADIUS": earth}

    def parameterAsRasterLayer(self, parameters, name, context):
        return mock.Mock()

    def parameterAsDouble(self, parameters, name, context):
        return self._DOUBLES[name]

    def parameterAsEnum(self, parameters, name, context):
        return self._enums.get(name, 0)

    def parameterAsInt(self, parameters, name, context):
        return 10

    def parameterAsString(self, parameters, name, context):
        return self._out


def _coverage_sent(model, earth, version):
    """(earth_radius in the CoverageParams, warnings pushed)."""
    feedback = _FakeFeedback()
    sent = {}

    def build(params, *_a, **_k):
        sent["params"] = params
        raise _Stop

    with tempfile.TemporaryDirectory() as out, \
            mock.patch.object(cov.bm, "setup_warning", return_value=None), \
            mock.patch.object(cov.bm, "check_engine_for_job"), \
            mock.patch.object(cov.bm, "read_engine_version", return_value=version), \
            mock.patch.object(cov, "prepare_terrain", return_value=out), \
            mock.patch.object(cov, "build_coverage_job", side_effect=build):
        with unittest.TestCase().assertRaises(_Stop):
            _Coverage(out, model, earth).processAlgorithm({}, mock.Mock(), feedback)
    return sent["params"].earth_radius, feedback.warnings


def _p2p_sent(model, earth, version):
    algo_model = model

    class _P(_Params):
        def parameterAsEnum(self, parameters, name, context):
            return {"MODEL": algo_model, "EARTH_RADIUS": earth}.get(name, 0)

    feedback = _FakeFeedback()
    with tempfile.TemporaryDirectory() as out, \
            mock.patch.object(alg.bm, "read_engine_version", return_value=version), \
            mock.patch("test_processing_p2p._FakeFeedback", return_value=feedback):
        params = _params_sent(_P(out), out)
    return params.earth_radius, feedback.warnings


class TheParameter(unittest.TestCase):

    def test_both_tools_offer_the_three_options_default_first(self):
        for algo, module in ((cov.CoverageAlgorithm(), cov), (alg.P2PAlgorithm(), alg)):
            param = _definitions(algo, module)["EARTH_RADIUS"]
            self.assertEqual(param.description, "Earth radius")
            self.assertEqual(param.kw["options"], LABELS)
            self.assertEqual(param.kw["defaultValue"], 0)
            self.assertTrue(param.kw["optional"])

    def test_core_rule(self):
        self.assertEqual(jb.earth_radius_default("LOS"), "REAL")
        self.assertEqual(jb.earth_radius_default("MIN_ALT"), "REAL")
        self.assertEqual(jb.earth_radius_default("ITM"), "FOUR_THIRDS")
        self.assertEqual(jb.earth_radius_default("SIMPLE_LOSS"), "FOUR_THIRDS")
        self.assertEqual(jb.earth_radius_default("LOSS"), "FOUR_THIRDS")


class Coverage(unittest.TestCase):
    # cov._MODELS = ["LOS", "ITM", "MIN_ALT"]

    def test_default_follows_the_model(self):
        self.assertEqual(_coverage_sent(0, DEFAULT, "0.5.2"), ("REAL", []))
        self.assertEqual(_coverage_sent(2, DEFAULT, "0.5.2"), ("REAL", []))
        self.assertEqual(_coverage_sent(1, DEFAULT, "0.5.2"), ("FOUR_THIRDS", []))

    def test_explicit_picks(self):
        self.assertEqual(_coverage_sent(1, REAL, "0.5.2"), ("REAL", []))
        self.assertEqual(_coverage_sent(0, FOUR_THIRDS, "0.5.2"), ("FOUR_THIRDS", []))

    def test_old_or_unknown_engine_four_thirds_and_a_warning(self):
        for version in ("0.5.1", None):
            value, warnings = _coverage_sent(0, DEFAULT, version)
            self.assertEqual(value, "FOUR_THIRDS")
            self.assertEqual(len(warnings), 1)
            self.assertIn("0.5.2 or newer", warnings[0])
        # 4/3 needs no newer engine: no warning.
        self.assertEqual(_coverage_sent(1, DEFAULT, "0.5.1"), ("FOUR_THIRDS", []))


class P2P(unittest.TestCase):
    # alg._MODELS = ["LOS", "ITM"]

    def test_default_follows_the_model(self):
        self.assertEqual(_p2p_sent(0, DEFAULT, "0.5.2"), ("REAL", []))
        self.assertEqual(_p2p_sent(1, DEFAULT, "0.5.2"), ("FOUR_THIRDS", []))

    def test_explicit_picks(self):
        self.assertEqual(_p2p_sent(1, REAL, "0.5.2"), ("REAL", []))
        self.assertEqual(_p2p_sent(0, FOUR_THIRDS, "0.5.2"), ("FOUR_THIRDS", []))

    def test_old_engine_four_thirds_and_a_warning(self):
        value, warnings = _p2p_sent(1, REAL, "0.5.1")
        self.assertEqual(value, "FOUR_THIRDS")
        self.assertEqual(len(warnings), 1)
        self.assertIn("0.5.2 or newer", warnings[0])


if __name__ == "__main__":
    unittest.main()
