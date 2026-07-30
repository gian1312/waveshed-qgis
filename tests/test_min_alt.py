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
from waveshed.core.job_builder import (
    VALID_RESOLUTIONS,
    CoverageParams,
    P2PParams,
    build_coverage_job,
    build_p2p_job,
)
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


class TestResolutions(unittest.TestCase):
    def test_valid_resolutions_include_coarse(self):
        # Coarse resolutions were added (90 m, 250 m) alongside the fine set.
        self.assertEqual(VALID_RESOLUTIONS, [2, 5, 10, 30, 90, 250])

    def test_coarse_resolutions_accepted_by_coverage(self):
        for r in (90, 250):
            job = build_coverage_job(
                CoverageParams(model="MIN_ALT", resolution_m=r), "/abt", "/out",
            )
            self.assertEqual(job["analysis"]["resolution_m"], r)

    def test_coarse_resolutions_accepted_by_p2p(self):
        for r in (90, 250):
            job = build_p2p_job(P2PParams(resolution_m=r), "/abt", "/out")
            self.assertEqual(job["analysis"]["resolution_m"], r)

    def test_unsupported_resolution_still_rejected(self):
        with self.assertRaises(ValueError):
            build_coverage_job(CoverageParams(resolution_m=45), "/abt", "/out")


class TestFeatureRename(unittest.TestCase):
    """The MIN_ALT feature is shown to users as 'Minimum LOS Altitude', but the
    engine wire value must stay 'MIN_ALT'. Assert on source (the GUI modules
    pull in Qt classes the lightweight stubs do not provide)."""

    def _read(self, *parts):
        import os
        path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "waveshed", *parts,
        )
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_mode_radio_uses_new_display_name(self):
        src = self._read("gui", "main_dialog.py")
        self.assertIn('QRadioButton("Minimum LOS Altitude")', src)
        self.assertNotIn('QRadioButton("Min Altitude")', src)

    def test_engine_wire_value_unchanged(self):
        src = self._read("gui", "main_dialog.py")
        self.assertIn('return "MIN_ALT"', src)


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
except Exception:  # noqa: BLE001 — a numpy wheel built for another platform
    # raises AttributeError, not ImportError. Never let that abort collection.
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

    def test_merge_cancels_before_touching_gdal(self):
        # A merge whose should_cancel is already True must raise MergeCanceled
        # before any GDAL work (no output files, GUI can abort a long merge).
        from waveshed.core.raster_tools import MergeCanceled, merge_best_site
        with self.assertRaises(MergeCanceled):
            merge_best_site(
                [("/a.tif", "A"), ("/b.tif", "B")],
                "/out_alt.tif", "/out_site.tif",
                should_cancel=lambda: True,
            )

    def test_fold_writes_through_a_slice(self):
        # merge_best_site folds each warped strip into a *view* of the shared
        # accumulators, so the fold has to mutate in place rather than rebind.
        from waveshed.core.raster_tools import BEST_SITE_NODATA, fold_best_site
        S = ma.MIN_ALT_SENTINEL
        best_alt = np.full((4, 2), S, dtype=np.uint16)
        best_site = np.full((4, 2), BEST_SITE_NODATA, dtype=np.uint8)

        fold_best_site(
            best_alt[2:4], best_site[2:4],
            np.array([[10, S], [S, 20]], dtype=np.uint16), 3,
        )

        self.assertEqual(best_alt.tolist(), [[S, S], [S, S], [10, S], [S, 20]])
        self.assertEqual(
            best_site.tolist(),
            [[BEST_SITE_NODATA] * 2, [BEST_SITE_NODATA] * 2,
             [3, BEST_SITE_NODATA], [BEST_SITE_NODATA, 3]],
        )

    def test_merge_refuses_an_unholdable_union_grid(self):
        from waveshed.core.raster_tools import (
            MAX_MERGE_PIXELS,
            _check_merge_size,
        )
        side = int(MAX_MERGE_PIXELS ** 0.5)
        _check_merge_size(side, side, 2)  # at the limit: allowed
        with self.assertRaises(ValueError) as ctx:
            _check_merge_size(side * 4, side * 4, 2)
        self.assertIn("resolution", str(ctx.exception))


class _FakeBand:
    """Stands in for a GDAL band exposing only what _contour_levels reads."""

    def __init__(self, min_raw, max_raw):
        self._range = (min_raw, max_raw)

    def ComputeRasterMinMax(self, approx_ok):  # noqa: N802 — GDAL spelling
        if self._range is None:
            raise RuntimeError("no valid pixels found")
        return self._range


class TestContourLevels(unittest.TestCase):
    """Level selection is what keeps contouring bounded, so it is worth pinning
    down without needing GDAL."""

    def _levels(self, min_m, max_m, interval_m=25.0, base_m=0.0):
        from waveshed.core.raster_tools import _contour_levels
        band = _FakeBand(min_m / ma.MIN_ALT_STEP_M, max_m / ma.MIN_ALT_STEP_M)
        return _contour_levels(band, interval_m, base_m)

    def test_levels_fall_strictly_inside_the_data_range(self):
        levels_raw, levels_m = self._levels(0.0, 300.0)
        self.assertEqual(levels_m, [float(m) for m in range(25, 300, 25)])
        # Contours are traced on the raw band, so levels go out in raw counts.
        self.assertEqual(levels_raw, [m / ma.MIN_ALT_STEP_M for m in levels_m])

    def test_range_narrower_than_one_step_yields_nothing(self):
        self.assertEqual(self._levels(10.0, 20.0), ([], []))

    def test_all_sentinel_raster_yields_nothing(self):
        from waveshed.core.raster_tools import _contour_levels
        band = _FakeBand(None, None)
        band._range = None
        self.assertEqual(_contour_levels(band, 25.0, 0.0), ([], []))

    def test_too_fine_an_interval_is_reported_not_attempted(self):
        from waveshed.core.raster_tools import MAX_CONTOUR_LEVELS
        with self.assertRaises(ValueError) as ctx:
            self._levels(0.0, 3000.0, interval_m=1.0)
        message = str(ctx.exception)
        self.assertIn("3000", message)
        # The message has to name a workable interval, not just say no.
        self.assertRegex(message, r"at least \d")
        self.assertLessEqual(len(self._levels(0.0, 3000.0, 20.0)[1]),
                             MAX_CONTOUR_LEVELS)

    def test_base_offsets_the_level_grid(self):
        _, levels_m = self._levels(0.0, 100.0, interval_m=50.0, base_m=10.0)
        self.assertEqual(levels_m, [10.0, 60.0])


class TestContourClasses(unittest.TestCase):
    """Legend bounds decide whether a contour is drawn at all — a value matching
    no class renders as nothing — so they are checked against QGIS' own rule."""

    @staticmethod
    def _class_of(classes, value):
        # QgsGraduatedSymbolRenderer::rangeForValue — every range is inclusive
        # at both ends, and the first hit in list order wins.
        for i, (lower, upper, _label, _color) in enumerate(classes):
            if lower <= value <= upper:
                return i
        return None

    def _levels(self, first=25, stop=300, step=25):
        return [float(m) for m in range(first, stop, step)]

    def test_every_level_is_drawn_and_classes_ascend(self):
        from waveshed.core.result_loader import (
            _MAX_CONTOUR_CLASSES,
            _contour_classes,
        )
        levels = self._levels()  # 11 levels -> 8 classes, uneven chunks
        classes = _contour_classes(levels)
        self.assertLessEqual(len(classes), _MAX_CONTOUR_CLASSES)

        assigned = [self._class_of(classes, lv) for lv in levels]
        self.assertNotIn(None, assigned, "a level fell outside every class")
        # Ascending altitude must never step back down a class, or the colour
        # ramp would read out of order.
        self.assertEqual(assigned, sorted(assigned))
        # No class is dead weight in the legend.
        self.assertEqual(len(set(assigned)), len(classes))

    def test_label_names_the_class_a_level_actually_lands_in(self):
        from waveshed.core.result_loader import _contour_classes
        levels = self._levels()
        classes = _contour_classes(levels)
        for level in levels:
            label = classes[self._class_of(classes, level)][2]
            bounds = [float(p) for p in label.removesuffix(" m").split("–")]
            with self.subTest(level=level, label=label):
                self.assertGreaterEqual(level, bounds[0])
                self.assertLessEqual(level, bounds[-1])

    def test_outermost_classes_reach_past_the_data(self):
        from waveshed.core.result_loader import _contour_classes
        classes = _contour_classes([25.0, 50.0, 75.0])
        self.assertLess(classes[0][0], 25.0)
        self.assertGreater(classes[-1][1], 75.0)

    def test_single_level(self):
        from waveshed.core.result_loader import _contour_classes
        classes = _contour_classes([100.0])
        self.assertEqual(len(classes), 1)
        self.assertEqual(classes[0][2], "100 m")
        self.assertEqual(self._class_of(classes, 100.0), 0)

    def test_colours_run_cool_to_warm_with_altitude(self):
        from waveshed.core.result_loader import _contour_classes
        colors = [c[3] for c in _contour_classes(self._levels())]
        self.assertGreater(colors[-1][0], colors[0][0])   # red rises
        self.assertLess(colors[-1][2], colors[0][2])      # blue falls


class TestNiceInterval(unittest.TestCase):
    def test_rounds_up_to_a_readable_step(self):
        from waveshed.core.raster_tools import _nice_interval
        for value, expected in [
            (0.4, 0.5), (1.0, 1.0), (3.0, 5.0), (7.0, 10.0),
            (15.0, 25.0), (240.0, 250.0), (600.0, 1000.0),
        ]:
            with self.subTest(value=value):
                self.assertEqual(_nice_interval(value), expected)

    def test_ladder_matches_the_altitude_slider(self):
        # A suggested interval the slider cannot snap to reads as arbitrary.
        from waveshed.core.raster_tools import _nice_interval
        for value in (7.0, 15.0, 60.0, 240.0, 600.0):
            with self.subTest(value=value):
                self.assertEqual(_nice_interval(value),
                                 float(ma.nice_ceiling(value)))


class TestAltitudeReference(unittest.TestCase):
    def test_unknown_and_missing_read_as_above_ground(self):
        # Layers stamped before AMSL existed carry nothing, and the solver only
        # ever emitted AGL — so the default has to be AGL, not an error.
        for value in (None, "", "  ", "furlongs"):
            with self.subTest(value=value):
                self.assertEqual(ma.normalize_reference(value), ma.REF_AGL)

    def test_case_and_padding_are_forgiven(self):
        self.assertEqual(ma.normalize_reference(" amsl "), ma.REF_AMSL)

    def test_readout_strings(self):
        self.assertEqual(ma.reference_suffix(ma.REF_AMSL), " m AMSL")
        self.assertEqual(ma.reference_suffix(None), " m AGL")
        self.assertEqual(ma.reference_phrase(ma.REF_AMSL), "above sea level")
        self.assertEqual(ma.reference_phrase(ma.REF_AGL), "above ground")


class TestNiceFloor(unittest.TestCase):
    def test_rounds_down_to_a_readable_step(self):
        for value, expected in [
            (0.0, 0), (0.0, 0), (85.0, 80), (437.0, 400),
            (1234.0, 1000), (3200.0, 3000),
        ]:
            with self.subTest(value=value):
                self.assertEqual(ma.nice_floor(value), expected)

    def test_never_exceeds_its_input(self):
        for value in (1, 9, 26, 99, 251, 999, 4999, 12345):
            with self.subTest(value=value):
                self.assertLessEqual(ma.nice_floor(value), value)

    def test_negative_reads_as_sea_level(self):
        self.assertEqual(ma.nice_floor(-120.0), 0)


class TestBandStops(unittest.TestCase):
    @staticmethod
    def _band(altitude_m, color=(1, 2, 3), visible=True):
        return ma.AltitudeBand(altitude_m, color, visible)

    def test_no_bands_no_stops(self):
        # The caller falls back to the continuous full-range ramp.
        self.assertEqual(ma.band_stops([]), [])

    def test_one_band_is_a_bound_plus_a_transparent_tail(self):
        stops = ma.band_stops([self._band(100.0, (9, 8, 7))])
        self.assertEqual(len(stops), 2)
        self.assertEqual(stops[0].raw, ma.altitude_to_raw(100.0))
        self.assertEqual(stops[0].color, (9, 8, 7))
        self.assertEqual(stops[0].label, "≤ 100 m AGL")
        # Everything above the top band must be transparent, not the top colour.
        self.assertEqual(stops[-1].raw, ma.MIN_ALT_SENTINEL)
        self.assertIsNone(stops[-1].color)

    def test_bands_come_back_in_altitude_order_with_ring_labels(self):
        stops = ma.band_stops([
            self._band(300.0, (3, 3, 3)),
            self._band(100.0, (1, 1, 1)),
            self._band(200.0, (2, 2, 2)),
        ])
        self.assertEqual([s.raw for s in stops[:-1]],
                         [ma.altitude_to_raw(m) for m in (100.0, 200.0, 300.0)])
        self.assertEqual([s.color for s in stops[:-1]],
                         [(1, 1, 1), (2, 2, 2), (3, 3, 3)])
        self.assertEqual([s.label for s in stops[:-1]], [
            "≤ 100 m AGL", "100 – 200 m AGL", "200 – 300 m AGL",
        ])

    def test_stops_ascend_so_a_discrete_ramp_can_bisect_them(self):
        stops = ma.band_stops([self._band(m) for m in (50.0, 125.0, 400.0)])
        raws = [s.raw for s in stops]
        self.assertEqual(raws, sorted(raws))
        self.assertEqual(len(set(raws)), len(raws))

    def test_duplicate_altitudes_collapse(self):
        # A second band at the same altitude would own an empty ring, and a
        # repeated stop value makes the ramp ambiguous.
        stops = ma.band_stops([self._band(100.0, (1, 1, 1)),
                               self._band(100.0, (2, 2, 2))])
        self.assertEqual(len(stops), 2)
        self.assertEqual(stops[0].color, (1, 1, 1))

    def test_altitudes_within_one_count_collapse(self):
        # 100.0 and 100.2 m both quantise to raw 200 — one ring, not two.
        stops = ma.band_stops([self._band(100.0), self._band(100.2)])
        self.assertEqual(len(stops), 2)

    def test_a_hidden_band_still_bounds_the_one_above_it(self):
        # Hiding "<= 100 m" must leave a hole, not hand its area to "<= 200 m".
        stops = ma.band_stops([
            self._band(100.0, (1, 1, 1), visible=False),
            self._band(200.0, (2, 2, 2)),
        ])
        self.assertEqual(stops[0].raw, ma.altitude_to_raw(100.0))
        self.assertIsNone(stops[0].color)
        self.assertEqual(stops[1].color, (2, 2, 2))
        self.assertEqual(stops[1].label, "100 – 200 m AGL")

    def test_labels_follow_the_reference(self):
        stops = ma.band_stops([self._band(1500.0)], ma.REF_AMSL)
        self.assertEqual(stops[0].label, "≤ 1500 m AMSL")


class TestBandPalette(unittest.TestCase):
    def test_palette_cycles(self):
        self.assertEqual(ma.band_color(0), ma.BAND_PALETTE[0])
        self.assertEqual(ma.band_color(len(ma.BAND_PALETTE)),
                         ma.BAND_PALETTE[0])

    def test_next_colour_skips_the_ones_on_the_map(self):
        used = [ma.BAND_PALETTE[0], ma.BAND_PALETTE[2]]
        self.assertEqual(ma.next_band_color(used), ma.BAND_PALETTE[1])

    def test_next_colour_falls_back_when_all_are_taken(self):
        self.assertIn(ma.next_band_color(list(ma.BAND_PALETTE)),
                      ma.BAND_PALETTE)

    def test_palette_hues_are_distinct(self):
        self.assertEqual(len(set(ma.BAND_PALETTE)), len(ma.BAND_PALETTE))


class TestAltitudeStamps(unittest.TestCase):
    def test_unstamped_layer_reads_as_above_ground(self):
        from waveshed.core.layer_utils import altitude_reference
        self.assertEqual(altitude_reference(_FakeLayer()), ma.REF_AGL)

    def test_reference_round_trip(self):
        from waveshed.core.layer_utils import (
            altitude_reference,
            mark_altitude_reference,
        )
        layer = _FakeLayer()
        mark_altitude_reference(layer, "amsl")
        self.assertEqual(altitude_reference(layer), ma.REF_AMSL)

    def test_derived_and_terrain_round_trip(self):
        from waveshed.core.layer_utils import (
            derived_from,
            mark_derived_from,
            mark_terrain_dir,
            terrain_dir,
        )
        layer = _FakeLayer()
        self.assertEqual(derived_from(layer), "")
        self.assertEqual(terrain_dir(layer), "")
        mark_derived_from(layer, "layer_abc")
        mark_terrain_dir(layer, "/cache/abc")
        self.assertEqual(derived_from(layer), "layer_abc")
        self.assertEqual(terrain_dir(layer), "/cache/abc")


class TestJobTerrainLookup(unittest.TestCase):
    """The link from a result raster back to the terrain it was computed over."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _result(self, terrain_dir, name="site1_alt1_20240101"):
        import json
        import os
        tif = os.path.join(self.tmp.name, f"{name}.tif")
        open(tif, "w").close()
        with open(os.path.join(self.tmp.name, f"{name}_job.json"), "w") as f:
            json.dump({"processing": {"terrain_dir": terrain_dir}}, f)
        return tif

    def test_reads_the_terrain_dir_beside_the_result(self):
        from waveshed.core.job_builder import read_job_terrain_dir
        import os
        cache = os.path.join(self.tmp.name, "abt_cache")
        os.makedirs(cache)
        self.assertEqual(read_job_terrain_dir(self._result(cache)), cache)

    def test_a_terrain_dir_that_is_gone_is_not_offered(self):
        # The cache is disposable; a path that no longer exists must read as
        # "no terrain" so the caller asks rather than failing inside GDAL.
        from waveshed.core.job_builder import read_job_terrain_dir
        import os
        missing = os.path.join(self.tmp.name, "swept_away")
        self.assertIsNone(read_job_terrain_dir(self._result(missing)))

    def test_no_job_file_no_terrain(self):
        from waveshed.core.job_builder import (
            job_file_for_result,
            read_job_terrain_dir,
        )
        import os
        orphan = os.path.join(self.tmp.name, "orphan.tif")
        open(orphan, "w").close()
        self.assertIsNone(job_file_for_result(orphan))
        self.assertIsNone(read_job_terrain_dir(orphan))
        self.assertIsNone(read_job_terrain_dir(""))

    def test_unreadable_job_file_does_not_raise(self):
        from waveshed.core.job_builder import read_job_terrain_dir
        import os
        tif = os.path.join(self.tmp.name, "broken.tif")
        open(tif, "w").close()
        with open(os.path.join(self.tmp.name, "broken_job.json"), "w") as f:
            f.write("{not json")
        self.assertIsNone(read_job_terrain_dir(tif))

    def test_round_trips_with_what_the_run_writes(self):
        # The reader must track write_job_file's naming, not a copy of it.
        from waveshed.core.job_builder import (
            CoverageParams,
            build_coverage_job,
            read_job_terrain_dir,
            write_job_file,
        )
        import os
        cache = os.path.join(self.tmp.name, "cache")
        os.makedirs(cache)
        params = CoverageParams(output_name="run7", model="MIN_ALT")
        job = build_coverage_job(params, cache, self.tmp.name)
        write_job_file(job, self.tmp.name, params.output_name)
        tif = os.path.join(self.tmp.name, "run7.tif")
        open(tif, "w").close()
        self.assertEqual(read_job_terrain_dir(tif), cache)


@unittest.skipUnless(_HAVE_NUMPY, "numpy not available")
class TestAmslArithmetic(unittest.TestCase):
    """``terrain + required AGL``, in the MIN_ALT encoding."""

    def _amsl(self, agl_raw, terrain_m):
        from waveshed.core.raster_tools import to_amsl_raw
        return to_amsl_raw(
            np.array(agl_raw, dtype=np.uint16),
            np.array(terrain_m, dtype=np.float32),
        ).tolist()

    def test_adds_the_ground_in_half_metre_counts(self):
        # 50 m AGL (raw 100) over 400 m of terrain is 450 m AMSL (raw 900).
        self.assertEqual(self._amsl([[100]], [[400.0]]), [[900]])

    def test_ground_level_coverage_becomes_the_terrain_height(self):
        self.assertEqual(self._amsl([[0]], [[1234.0]]),
                         [[ma.altitude_to_raw(1234.0)]])

    def test_unreachable_stays_unreachable(self):
        S = ma.MIN_ALT_SENTINEL
        self.assertEqual(self._amsl([[S]], [[400.0]]), [[S]])

    def test_missing_terrain_blanks_the_pixel(self):
        # Inventing sea level where the DEM has a hole would put a confident
        # wrong altitude on the map; a hole is the honest answer.
        from waveshed.core.abt import MOSAIC_NODATA_M
        self.assertEqual(self._amsl([[100]], [[MOSAIC_NODATA_M]]),
                         [[ma.MIN_ALT_SENTINEL]])

    def test_nan_terrain_blanks_the_pixel(self):
        # A warped float DEM spells its own no-data as NaN.
        self.assertEqual(self._amsl([[100]], [[float("nan")]]),
                         [[ma.MIN_ALT_SENTINEL]])

    def test_ground_below_sea_level_clamps_to_zero(self):
        # The u16 encoding cannot hold a negative altitude.
        self.assertEqual(self._amsl([[100]], [[-380.0]]), [[100]])

    def test_saturates_instead_of_wrapping_into_the_sentinel(self):
        # 60000 counts (30 km) plus 5 km of terrain overflows u16; wrapping
        # would land somewhere random, and landing on 65535 would silently
        # delete a reachable pixel.
        out = self._amsl([[60000]], [[5000.0]])
        self.assertEqual(out, [[ma.MIN_ALT_MAX_RAW]])
        self.assertLess(out[0][0], ma.MIN_ALT_SENTINEL)

    def test_mixed_tile(self):
        S = ma.MIN_ALT_SENTINEL
        self.assertEqual(
            self._amsl([[0, 200, S], [100, S, 40]],
                       [[100.0, 100.0, 100.0], [-9999.0, 250.0, 250.0]]),
            [[200, 400, S], [S, S, 540]],
        )

    def test_honours_a_source_specific_nodata(self):
        from waveshed.core.raster_tools import to_amsl_raw
        out = to_amsl_raw(
            np.array([[900, 1000]], dtype=np.uint16),
            np.array([[100.0, 100.0]], dtype=np.float32),
            nodata_raw=1000,
        )
        self.assertEqual(out.tolist(), [[1100, ma.MIN_ALT_SENTINEL]])


if __name__ == "__main__":
    unittest.main()
