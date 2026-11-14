"""Result styling in the units QGIS shows: dBm for SIMPLE_LOSS / ITM, metres
for MIN_ALT (LOS Floor).

Logic tests on the conftest stubs: the QGIS classes are replaced by recording
fakes, GDAL is real (the dBm range reads real GeoTIFFs written here with the
exporter's tags).  The rendering itself was verified in PyQGIS 3.22.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

from _real_gdal import GDAL as gdal, HAVE_GDAL, real_gdal
from waveshed.core import min_alt as ma
from waveshed.core import result_loader as rl

_CORE = sys.modules["qgis.core"]


# ---------------------------------------------------------------------------
# Recording fakes
# ---------------------------------------------------------------------------

class _Color:
    def __init__(self, *args):
        if len(args) == 1 and isinstance(args[0], str):
            self.rgba = args[0]
        else:
            self.rgba = tuple(args) + (255,) * (4 - len(args))
        self.alpha_f = None

    def setAlphaF(self, value):
        self.alpha_f = value


class _Ramp:
    class Type:
        Interpolated = "interpolated"
        Discrete = "discrete"
        Exact = "exact"

    class ColorRampItem:
        def __init__(self, value, color, label=""):
            self.value = value
            self.color = color
            self.label = label

    def __init__(self):
        self.items = []
        self.ramp_type = None
        self.minimum = None
        self.maximum = None
        self.legend = None

    def setColorRampType(self, ramp_type):
        self.ramp_type = ramp_type

    def setColorRampItemList(self, items):
        self.items = list(items)

    def setClip(self, clip):
        self.clip = clip

    def setMinimumValue(self, value):
        self.minimum = value

    def setMaximumValue(self, value):
        self.maximum = value

    def setLegendSettings(self, settings):
        self.legend = settings


class _Shader:
    def setRasterShaderFunction(self, function):
        self.function = function


class _Renderer:
    def __init__(self, provider, band, shader):
        self.provider = provider
        self.band = band
        self.shader = shader
        self.classification = [None, None]

    def setClassificationMin(self, value):
        self.classification[0] = value

    def setClassificationMax(self, value):
        self.classification[1] = value


class _Range:
    def __init__(self, low, high):
        self.low, self.high = low, high


class _LegendSettings:
    def setSuffix(self, suffix):
        self.suffix = suffix

    def setNumericFormat(self, number_format):
        self.number_format = number_format


class _NumberFormat:
    def setNumberDecimalPlaces(self, places):
        self.places = places

    def setShowTrailingZeros(self, show):
        self.trailing_zeros = show


class _Provider:
    def __init__(self, stats=None):
        self.stats = stats
        self.calls = []

    def bandStatistics(self, band, flags):
        if isinstance(self.stats, Exception):
            raise self.stats
        return self.stats

    def setUseSourceNoDataValue(self, band, use):
        self.calls.append(("use_source", band, use))

    def setUserNoDataValue(self, band, ranges):
        self.calls.append(("user", band, [(r.low, r.high) for r in ranges]))


class _Layer:
    def __init__(self, provider):
        self.provider = provider
        self.renderer = None

    def dataProvider(self):
        return self.provider

    def setRenderer(self, renderer):
        self.renderer = renderer

    def triggerRepaint(self):
        pass


class _Stats:
    def __init__(self, low, high):
        self.minimumValue = low
        self.maximumValue = high


class _FakeQgis(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(rl, "QgsColorRampShader", _Ramp),
            mock.patch.object(rl, "QgsRasterShader", _Shader),
            mock.patch.object(rl, "QgsSingleBandPseudoColorRenderer", _Renderer),
            mock.patch.object(rl, "QColor", _Color),
            mock.patch.object(_CORE, "QgsRasterRange", _Range, create=True),
            mock.patch.object(_CORE, "QgsColorRampLegendNodeSettings",
                              _LegendSettings, create=True),
            mock.patch.object(_CORE, "QgsBasicNumericFormat",
                              _NumberFormat, create=True),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)


# ---------------------------------------------------------------------------
# SIMPLE_LOSS / ITM
# ---------------------------------------------------------------------------

class TestDbmRampStops(unittest.TestCase):
    def test_mpt_stops_spread_over_the_range(self):
        stops = rl.dbm_ramp_stops((-140.0, -40.0))
        self.assertEqual([v for v, _, _ in stops],
                         [-140.0, -115.0, -90.0, -65.0, -40.0])
        self.assertEqual([c for _, c, _ in stops], [
            (0, 0, 255, 180), (0, 255, 255, 180), (0, 255, 0, 180),
            (255, 255, 0, 200), (255, 0, 0, 220),
        ])

    def test_every_stop_is_labelled_in_dbm(self):
        stops = rl.dbm_ramp_stops((-140.0, -41.0))
        self.assertEqual([t for _, _, t in stops], [
            "-140.0 dBm", "-115.2 dBm", "-90.5 dBm", "-65.8 dBm", "-41.0 dBm",
        ])

    def test_fallback_only_without_a_usable_range(self):
        for bad in (None, (float("nan"), -40.0), (-90.0, -90.0),
                    (-40.0, -140.0), (float("-inf"), 0.0), ("x", 1)):
            with self.subTest(value_range=bad):
                stops = rl.dbm_ramp_stops(bad)
                self.assertEqual((stops[0][0], stops[-1][0]), (-140.0, -40.0))

    def test_a_range_outside_the_fallback_is_kept(self):
        stops = rl.dbm_ramp_stops((-149.0, 105.0))
        self.assertEqual((stops[0][0], stops[-1][0]), (-149.0, 105.0))


class TestSignalStrengthStyle(_FakeQgis):
    def _styled(self, value_range):
        layer = _Layer(_Provider())
        rl._apply_signal_strength_style(layer, value_range)
        return layer

    def test_interpolated_ramp_with_the_mpt_stops(self):
        ramp = self._styled((-140.0, -41.0)).renderer.shader.function
        self.assertEqual(ramp.ramp_type, _Ramp.Type.Interpolated)
        self.assertEqual([i.value for i in ramp.items],
                         [-140.0, -115.25, -90.5, -65.75, -41.0])
        self.assertEqual(ramp.items[0].color.rgba, (0, 0, 255, 180))
        self.assertEqual(ramp.items[-1].color.rgba, (255, 0, 0, 220))
        self.assertTrue(all(i.label.endswith(" dBm") for i in ramp.items))

    def test_range_pinned_on_shader_and_renderer(self):
        renderer = self._styled((-140.0, -41.0)).renderer
        self.assertEqual(renderer.classification, [-140.0, -41.0])
        ramp = renderer.shader.function
        self.assertEqual((ramp.minimum, ramp.maximum), (-140.0, -41.0))

    def test_fallback_range_pinned_too(self):
        renderer = self._styled(None).renderer
        self.assertEqual(renderer.classification, [-140.0, -40.0])

    def test_legend_carries_the_unit(self):
        legend = self._styled((-140.0, -41.0)).renderer.shader.function.legend
        self.assertEqual(legend.suffix, " dBm")
        self.assertEqual(legend.number_format.places, 1)
        self.assertTrue(legend.number_format.trailing_zeros)

    def test_only_raw_zero_is_no_data(self):
        # Source nodata (raw 0) off, so 0 dBm (raw 150) is drawn; the user
        # no-data is the SINGLE scaled value -150 — a range would be collapsed
        # by Layer Properties.
        calls = self._styled((-140.0, -41.0)).provider.calls
        self.assertIn(("use_source", 1, False), calls)
        self.assertIn(("user", 1, [(-150.0, -150.0)]), calls)


class TestLoadCoverageResultRange(_FakeQgis):
    """load_coverage_result styles SIMPLE_LOSS / ITM over the given range,
    else over dbm_range(tif)."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tif = os.path.join(self.tmp, "r.tif")
        open(self.tif, "wb").close()
        self.layer = _Layer(_Provider())
        self.layer.isValid = lambda: True
        for name, value in (("QgsRasterLayer", lambda *a: self.layer),
                            ("mark_aether_model", lambda *a: None)):
            patch = mock.patch.object(rl, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_given_range_wins(self):
        with mock.patch.object(rl, "dbm_range") as computed:
            rl.load_coverage_result(self.tif, "ITM", value_range=(-130.0, -50.0))
        computed.assert_not_called()
        self.assertEqual(self.layer.renderer.classification, [-130.0, -50.0])

    def test_without_one_the_memo_is_asked(self):
        with mock.patch.object(rl, "dbm_range",
                               return_value=(-120.0, -60.0)) as computed:
            rl.load_coverage_result(self.tif, "simple_loss")
        computed.assert_called_once_with(self.tif)
        self.assertEqual(self.layer.renderer.classification, [-120.0, -60.0])

    def test_los_pins_zero_to_one(self):
        rl.load_coverage_result(self.tif, "LOS")
        self.assertEqual(self.layer.renderer.classification, [0.0, 1.0])
        ramp = self.layer.renderer.shader.function
        self.assertEqual((ramp.minimum, ramp.maximum), (0.0, 1.0))


def _write_prop_tif(path, raw, nodata=0, scale=1.0, offset=-150.0):
    """A Byte GeoTIFF tagged the way aether_export tags 8BIT_PROP."""
    h, w = raw.shape
    ds = gdal.GetDriverByName("GTiff").Create(path, w, h, 1, gdal.GDT_Byte)
    ds.SetGeoTransform([8.0, 0.001, 0, 47.0, 0, -0.001])
    band = ds.GetRasterBand(1)
    if nodata is not None:
        band.SetNoDataValue(nodata)
    band.SetScale(scale)
    band.SetOffset(offset)
    band.WriteArray(raw)
    band = None
    ds = None


@unittest.skipUnless(HAVE_GDAL, "needs the real GDAL bindings")
class TestDbmRange(unittest.TestCase):
    def setUp(self):
        # conftest stubs osgeo; dbm_range imports it at call time, so the real
        # one is swapped in for each test.
        context = real_gdal()
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        rl._DBM_RANGE_CACHE.clear()
        self.addCleanup(rl._DBM_RANGE_CACHE.clear)

    def _tif(self, raw, name="r.tif", **kw):
        path = os.path.join(self.tmp, name)
        _write_prop_tif(path, np.asarray(raw, np.uint8), **kw)
        return path

    def test_exact_range_honours_nodata_and_applies_scale_offset(self):
        # raw 0 is nodata; 10 → -140 dBm, 150 → 0 dBm, 109 → -41 dBm.
        path = self._tif([[0, 10, 150], [109, 0, 0]])
        self.assertEqual(rl.dbm_range(path), (-140.0, 0.0))

    def test_all_no_data_is_none(self):
        self.assertIsNone(rl.dbm_range(self._tif([[0, 0], [0, 0]])))

    def test_missing_or_unreadable_file_is_none(self):
        self.assertIsNone(rl.dbm_range(os.path.join(self.tmp, "absent.tif")))
        junk = os.path.join(self.tmp, "junk.tif")
        with open(junk, "wb") as f:
            f.write(b"not a tiff")
        self.assertIsNone(rl.dbm_range(junk))

    def test_unreadable_file_is_not_remembered(self):
        junk = os.path.join(self.tmp, "late.tif")
        with open(junk, "wb") as f:
            f.write(b"not a tiff")
        self.assertIsNone(rl.dbm_range(junk))
        self.assertEqual(len(rl._DBM_RANGE_CACHE), 0)

    def test_second_call_on_an_unchanged_file_does_not_reopen_it(self):
        path = self._tif([[10, 20]])
        with mock.patch.object(gdal, "Open", wraps=gdal.Open) as opened:
            first = rl.dbm_range(path)
            second = rl.dbm_range(path)
            # Another spelling of the same path is the same entry.
            third = rl.dbm_range(os.path.join(self.tmp, ".", "r.tif"))
        self.assertEqual(first, (-140.0, -130.0))
        self.assertEqual(second, first)
        self.assertEqual(third, first)
        self.assertEqual(opened.call_count, 1)

    def test_a_rewritten_file_is_read_again(self):
        path = self._tif([[10, 20]])
        self.assertEqual(rl.dbm_range(path), (-140.0, -130.0))
        before = os.stat(path).st_mtime_ns
        os.remove(path)
        self._tif([[30, 40, 50]])          # new size
        # Make sure the mtime moves even on a coarse-clock file system.
        os.utime(path, ns=(before + 10**9, before + 10**9))
        with mock.patch.object(gdal, "Open", wraps=gdal.Open) as opened:
            self.assertEqual(rl.dbm_range(path), (-120.0, -100.0))
        self.assertEqual(opened.call_count, 1)

    def test_same_size_new_mtime_is_read_again(self):
        path = self._tif([[10, 20]])
        rl.dbm_range(path)
        stamp = os.stat(path).st_mtime_ns
        _write_prop_tif(path, np.asarray([[60, 70]], np.uint8))
        os.utime(path, ns=(stamp + 2 * 10**9, stamp + 2 * 10**9))
        self.assertEqual(rl.dbm_range(path), (-90.0, -80.0))

    def test_cache_is_bounded(self):
        path = self._tif([[10, 20]])
        with mock.patch.object(rl, "_DBM_RANGE_CACHE_MAX", 3):
            for i in range(5):
                os.utime(path, ns=(10**18 + i, 10**18 + i))
                rl.dbm_range(path)
            self.assertEqual(len(rl._DBM_RANGE_CACHE), 3)

    def test_threads_share_the_memo(self):
        import threading
        path = self._tif([[10, 20]])
        rl.dbm_range(path)                   # the worker's prewarm
        results = []
        with mock.patch.object(gdal, "Open", wraps=gdal.Open) as opened:
            threads = [threading.Thread(
                target=lambda: results.append(rl.dbm_range(path)))
                for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(results, [(-140.0, -130.0)] * 8)
        self.assertEqual(opened.call_count, 0)


# ---------------------------------------------------------------------------
# MIN_ALT (LOS Floor) — metres
# ---------------------------------------------------------------------------

class TestEstimateAltitudeRange(unittest.TestCase):
    def _range(self, stats):
        return rl.estimate_altitude_range_m(_Layer(_Provider(stats)))

    def test_statistics_are_metres(self):
        # QGIS's statistics of the scaled band: 0…1000 m → tidy 0…1000, not
        # the old halving to 500.
        self.assertEqual(self._range(_Stats(0.0, 1000.0)), (0.0, 1000.0))
        self.assertEqual(self._range(_Stats(0.0, 170.0)), (0.0, 250.0))

    def test_sea_level_floor_in_metres(self):
        self.assertEqual(self._range(_Stats(2000.0, 3000.0)), (2000.0, 5000.0))

    def test_highest_real_altitude_is_accepted(self):
        self.assertEqual(self._range(_Stats(0.0, 32767.0))[1], 35000.0)

    def test_sentinel_or_nothing_falls_back(self):
        fallback = (0.0, ma.DEFAULT_RAMP_MAX_M)
        for stats in (_Stats(0.0, ma.MIN_ALT_SENTINEL_M), _Stats(0.0, 0.0),
                      _Stats(None, None), _Stats(0.0, float("nan")),
                      RuntimeError("no stats")):
            with self.subTest(stats=stats):
                self.assertEqual(self._range(stats), fallback)

    def test_sentinel_low_end_reads_as_ground(self):
        self.assertEqual(self._range(_Stats(ma.MIN_ALT_SENTINEL_M, 300.0)),
                         (0.0, 500.0))


class TestMinAltRenderer(_FakeQgis):
    def _items(self, **kwargs):
        renderer = rl.build_min_alt_renderer("provider", 1, **kwargs)
        return renderer, renderer.shader.function.items

    def test_full_ramp_stops_are_metres(self):
        renderer, items = self._items(max_ramp_m=1000.0)
        self.assertEqual([i.value for i in items],
                         [125.0 * k for k in range(9)])
        self.assertEqual(items[0].label, "0 m AGL")
        self.assertEqual(items[-1].label, "1000 m AGL")
        self.assertEqual(renderer.classification, [0.0, 1000.0])

    def test_threshold_cutoff_is_the_first_step_above(self):
        _, items = self._items(threshold_m=100.0, max_ramp_m=1000.0)
        self.assertEqual(items[-2].value, 100.0)       # drawn, at exactly 100
        self.assertEqual(items[-1].value, 100.5)       # hidden from 100.5 up
        self.assertEqual(items[-1].color.rgba, (0, 0, 0, 0))

    def test_threshold_off_the_grid(self):
        _, items = self._items(threshold_m=100.3, max_ramp_m=1000.0)
        self.assertEqual(items[-2].value, 100.3)
        self.assertEqual(items[-1].value, 100.5)

    def test_threshold_legend_spans_to_the_threshold(self):
        renderer, _ = self._items(threshold_m=100.0, max_ramp_m=1000.0,
                                  min_ramp_m=0.0)
        self.assertEqual(renderer.classification, [0.0, 100.0])
        ramp = renderer.shader.function
        self.assertEqual((ramp.minimum, ramp.maximum), (0.0, 100.0))

    def test_sea_level_ramp_starts_at_the_floor(self):
        renderer, items = self._items(min_ramp_m=2000.0, max_ramp_m=3000.0,
                                      reference=ma.REF_AMSL)
        self.assertEqual(items[0].value, 2000.0)
        self.assertEqual(items[-1].value, 3000.0)
        self.assertEqual(items[-1].label, "3000 m AMSL")
        self.assertEqual(renderer.classification, [2000.0, 3000.0])
        self.assertEqual(renderer.shader.function.legend.suffix, " m AMSL")

    def test_tiny_threshold_has_no_duplicate_stops(self):
        _, items = self._items(threshold_m=1.0, max_ramp_m=300.0)
        values = [i.value for i in items]
        self.assertEqual(values, sorted(values))
        self.assertEqual(len({ma.quantized_floor_m(v) for v in values}),
                         len(values))
        self.assertEqual(values[-1], 1.5)

    def test_zero_threshold_still_has_a_range(self):
        renderer, items = self._items(threshold_m=0.0, max_ramp_m=300.0)
        self.assertEqual([i.value for i in items], [0.0, 0.5])
        self.assertEqual(renderer.classification, [0.0, 0.5])


if __name__ == "__main__":
    unittest.main()
