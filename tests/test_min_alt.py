"""Unit tests for MIN_ALT (minimum-LOS-altitude) support.

Covers the pure quantization/colour contract in ``core.min_alt``, the job-config
wiring in ``core.job_builder``, the Processing model list, and the layer model
tagging used by the Altitude Explorer.

QGIS stubs provided by conftest.py.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import math
import unittest

from waveshed.core import min_alt as ma
from waveshed.core.job_builder import CoverageParams, build_coverage_job
from waveshed.core.layer_utils import (
    aether_model,
    is_min_alt_layer,
    mark_aether_model,
)


class _FakeLayer:
    """Minimal QgsMapLayer stand-in with custom-property storage."""

    def __init__(self):
        self._props = {}

    def setCustomProperty(self, key, value):
        self._props[key] = value

    def customProperty(self, key, default=None):
        return self._props.get(key, default)


class TestQuantization(unittest.TestCase):
    def test_step_and_sentinel_constants(self):
        self.assertEqual(ma.MIN_ALT_STEP_M, 0.5)
        self.assertEqual(ma.MIN_ALT_SENTINEL, 65535)
        self.assertEqual(ma.MIN_ALT_MAX_RAW, 65534)

    def test_altitude_to_raw_roundtrip(self):
        for altitude in (0.0, 0.5, 1.5, 50.0, 123.5, 300.0):
            raw = ma.altitude_to_raw(altitude)
            self.assertEqual(ma.raw_to_altitude(raw), altitude)

    def test_altitude_to_raw_scale(self):
        # 0.5 m per count: 100 m AGL -> raw 200.
        self.assertEqual(ma.altitude_to_raw(100.0), 200)
        self.assertEqual(ma.altitude_to_raw(0.5), 1)

    def test_altitude_to_raw_clamps_low(self):
        self.assertEqual(ma.altitude_to_raw(-5.0), 0)

    def test_altitude_to_raw_never_collides_with_sentinel(self):
        raw = ma.altitude_to_raw(1e9)
        self.assertEqual(raw, ma.MIN_ALT_MAX_RAW)
        self.assertNotEqual(raw, ma.MIN_ALT_SENTINEL)

    def test_raw_to_altitude_sentinel_is_infinite(self):
        self.assertTrue(math.isinf(ma.raw_to_altitude(ma.MIN_ALT_SENTINEL)))


class TestColourRamp(unittest.TestCase):
    def test_endpoints(self):
        low = ma.altitude_color(0.0, 300.0)
        high = ma.altitude_color(300.0, 300.0)
        self.assertEqual(low, (44, 123, 182))    # deep blue
        self.assertEqual(high, (215, 25, 28))    # red

    def test_saturates_above_max(self):
        self.assertEqual(
            ma.altitude_color(9999.0, 300.0), ma.altitude_color(300.0, 300.0),
        )

    def test_ramp_stops_monotonic_altitude(self):
        stops = ma.ramp_stops_m(300.0, steps=6)
        self.assertEqual(len(stops), 7)
        alts = [a for a, _ in stops]
        self.assertEqual(alts, sorted(alts))
        self.assertEqual(alts[0], 0.0)
        self.assertEqual(alts[-1], 300.0)

    def test_nice_ceiling(self):
        self.assertEqual(ma.nice_ceiling(8), 10)
        self.assertEqual(ma.nice_ceiling(30), 50)
        self.assertEqual(ma.nice_ceiling(120), 250)
        self.assertEqual(ma.nice_ceiling(0), int(ma.DEFAULT_RAMP_MAX_M))


class TestJobBuilder(unittest.TestCase):
    def test_min_alt_propagation_model_serialized(self):
        params = CoverageParams(
            tx_lat=47.0, tx_lon=8.0, model="MIN_ALT", resolution_m=10,
        )
        job = build_coverage_job(params, "/abt", "/out")
        self.assertEqual(job["analysis"]["propagation_model"], "MIN_ALT")
        self.assertEqual(job["analysis"]["task_type"], "SINGLE")


class TestProcessingModelList(unittest.TestCase):
    def test_coverage_algorithm_offers_min_alt(self):
        # The Processing algorithm pulls in the full qgis.core Processing API,
        # which the lightweight conftest stubs do not provide — so assert on the
        # source rather than importing the module.
        import os
        path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "waveshed", "algorithms", "coverage.py",
        )
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertRegex(src, r"_MODELS\s*=\s*\[[^\]]*\"MIN_ALT\"")


class TestLayerTagging(unittest.TestCase):
    def test_mark_and_detect_min_alt(self):
        layer = _FakeLayer()
        mark_aether_model(layer, "min_alt")  # case-insensitive
        self.assertEqual(aether_model(layer), "MIN_ALT")
        self.assertTrue(is_min_alt_layer(layer))

    def test_non_min_alt_layer(self):
        layer = _FakeLayer()
        mark_aether_model(layer, "LOS")
        self.assertFalse(is_min_alt_layer(layer))


try:
    import numpy as np
    _HAVE_NUMPY = True
except ImportError:
    _HAVE_NUMPY = False


@unittest.skipUnless(_HAVE_NUMPY, "numpy not available")
class TestBestSiteReduction(unittest.TestCase):
    def _reduce(self, arrays):
        from waveshed.core.raster_tools import reduce_best_site
        return reduce_best_site([np.array(a, dtype=np.uint16) for a in arrays])

    def test_lowest_altitude_and_argmin_win(self):
        # site 0 needs 100/0.5=200 raw here, site 1 needs 60 raw -> site 1 wins.
        best_alt, best_site = self._reduce([
            [[200, 200]],
            [[60, 400]],
        ])
        self.assertEqual(best_alt.tolist(), [[60, 200]])
        self.assertEqual(best_site.tolist(), [[1, 0]])

    def test_sentinel_never_wins(self):
        from waveshed.core.raster_tools import BEST_SITE_NODATA
        S = ma.MIN_ALT_SENTINEL
        best_alt, best_site = self._reduce([
            [[S, 50]],
            [[80, S]],
        ])
        self.assertEqual(best_alt.tolist(), [[80, 50]])
        self.assertEqual(best_site.tolist(), [[1, 0]])

    def test_all_uncovered_stays_nodata(self):
        from waveshed.core.raster_tools import BEST_SITE_NODATA
        S = ma.MIN_ALT_SENTINEL
        best_alt, best_site = self._reduce([[[S]], [[S]]])
        self.assertEqual(best_alt.tolist(), [[S]])
        self.assertEqual(best_site.tolist(), [[BEST_SITE_NODATA]])

    def test_tie_goes_to_earlier_site(self):
        best_alt, best_site = self._reduce([[[100]], [[100]]])
        self.assertEqual(best_alt.tolist(), [[100]])
        self.assertEqual(best_site.tolist(), [[0]])


if __name__ == "__main__":
    unittest.main()
