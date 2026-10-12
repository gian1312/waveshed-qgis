"""Adaptive / custom resolution (core.range_profile and everything that uses it).

A range profile makes the far field of a coverage run coarser than the near
field. The pieces tested here: the band rules (ladder, tile-extent nesting,
clipping), the job JSON (one job: base resolution + profile), the
minimum-download terrain filter, and the engine gate.
"""

import conftest  # noqa: F401

import math
import tempfile
import unittest
from unittest import mock

from waveshed.core import binary_manager as bm
from waveshed.core import range_profile as rp
from waveshed.core import terrain_adapter as ta
from waveshed.core.job_builder import CoverageParams, build_coverage_job


class AdaptiveBands(unittest.TestCase):

    def test_owner_spec_5m(self):
        # 5 m to 7 km, then up to 90 m over the next 5 km, 90 m beyond.
        self.assertEqual(rp.adaptive_bands(5, 100), [
            (7000.0, 5), (9500.0, 10), (12000.0, 30), (100000.0, 90)])

    def test_2m_skips_resolutions_whose_tiles_do_not_nest(self):
        bands = rp.adaptive_bands(2, 60)
        self.assertEqual([s for _, s in bands], [2, 30, 90])
        self.assertIsNone(rp.validate_bands(bands, 60))

    def test_no_profile_when_it_would_not_differ(self):
        self.assertIsNone(rp.adaptive_bands(90, 50))   # already the far step
        self.assertIsNone(rp.adaptive_bands(250, 50))
        self.assertIsNone(rp.adaptive_bands(5, 6))     # range inside the near field

    def test_short_range_ends_on_the_ramp_in_order(self):
        # A band clipped to the range ends the list; it must not reorder.
        self.assertEqual(rp.adaptive_bands(5, 10), [
            (7000.0, 5), (9500.0, 10), (10000.0, 30)])
        self.assertEqual(rp.adaptive_bands(5, 8), [(7000.0, 5), (8000.0, 10)])


class BandRules(unittest.TestCase):

    def test_extents_nest(self):
        self.assertTrue(rp.extents_nest([5, 10, 30, 90]))
        self.assertTrue(rp.extents_nest([2, 30, 90, 250]))
        self.assertFalse(rp.extents_nest([2, 5]))
        self.assertFalse(rp.extents_nest([2, 10]))

    def test_validate_refuses_what_the_engine_cannot_run(self):
        self.assertIn("finer", rp.validate_bands([(5000, 30), (10000, 10)], 10))
        self.assertIn("cannot be combined", rp.validate_bands([(5000, 2), (10000, 5)], 10))
        self.assertIn("end at the analysis range", rp.validate_bands([(5000, 5), (8000, 30)], 10))
        self.assertIn("one of", rp.validate_bands([(5000, 5), (10000, 45)], 10))
        many = [(1000.0 * k, 30) for k in range(1, 10)]
        self.assertIn("At most", rp.validate_bands(many, 9))
        self.assertIsNone(rp.validate_bands([(5000, 5), (10000, 30)], 10))

    def test_normalise_clips_merges_and_extends(self):
        self.assertEqual(rp.normalise_bands([(5000, 5), (8000, 5), (30000, 90)], 20),
                         [(8000.0, 5), (20000.0, 90)])
        self.assertEqual(rp.normalise_bands([(5000, 5), (8000, 30)], 20),
                         [(5000.0, 5), (20000.0, 30)])

    def test_describe(self):
        self.assertEqual(rp.describe([(7000.0, 5), (100000.0, 90)]), "5 m to 7 km · 90 m beyond")


class JobJson(unittest.TestCase):

    def test_profile_is_written_and_absent_by_default(self):
        # One job: the chosen resolution stays the output pixel size, the
        # profile only changes the radial step.
        params = CoverageParams(tx_lat=47.0, tx_lon=8.0, resolution_m=5, max_range_km=50,
                                range_profile=[(7000.0, 5), (50000.0, 90)])
        job = build_coverage_job(params, "/abt", "/out")
        self.assertEqual(job["analysis"]["range_profile"], [
            {"until_m": 7000.0, "step_m": 5.0}, {"until_m": 50000.0, "step_m": 90.0}])
        self.assertEqual(job["analysis"]["resolution_m"], 5)
        self.assertEqual(job["analysis"]["max_range_km"], 50)
        plain = build_coverage_job(CoverageParams(tx_lat=47.0, tx_lon=8.0), "/abt", "/out")
        self.assertNotIn("range_profile", plain["analysis"])


class MinimumDownload(unittest.TestCase):
    """A coarse band never fetches ground a finer band already covers."""

    TX = (47.05, 8.48)

    def _tiles(self, res, range_km):
        bbox = ta._compute_sector_bbox(*self.TX, range_km, 0.0, 360.0)
        return ta._compute_subtiles(bbox, res, *self.TX, range_km, 0.0, 360.0)

    def _far_corner_m(self, t):
        north, west, ext = ta.tile_footprint(t)
        cos = math.cos(math.radians(self.TX[0]))
        return max(math.hypot((lon - self.TX[1]) * 111_195 * cos, (lat - self.TX[0]) * 111_195)
                   for lat in (north - ext, north) for lon in (west, west + ext))

    def test_tiles_inside_the_finer_disc_are_dropped(self):
        tiles = self._tiles(10, 60)          # 0.25 deg tiles, small enough to fit
        kept = ta._band_tiles(tiles, *self.TX, min_range_km=40)
        dropped = [t for t in tiles if t not in kept]
        self.assertTrue(dropped)
        self.assertTrue(all(self._far_corner_m(t) < 40_000 for t in dropped))
        self.assertTrue(all(self._far_corner_m(t) >= 40_000 * 0.999 for t in kept))

    def test_same_footprint_tiles_are_dropped(self):
        fine = self._tiles(5, 7)
        coarse = self._tiles(10, 10)
        covered = {ta.tile_footprint(t) for t in fine}
        kept = ta._band_tiles(coarse, *self.TX, skip_footprints=covered)
        self.assertTrue(covered & {ta.tile_footprint(t) for t in coarse})
        self.assertFalse(covered & {ta.tile_footprint(t) for t in kept})

    def test_defaults_keep_everything(self):
        tiles = self._tiles(30, 20)
        self.assertEqual(ta._band_tiles(tiles, *self.TX), tiles)

    def test_profile_plan_has_no_duplicate_footprints(self):
        bands = rp.adaptive_bands(5, 30)
        seen = []

        def fake_plan(source, lat, lon, rng, res, a0, a1, b, min_range_km=0.0,
                      skip_footprints=None):
            bbox = ta._compute_sector_bbox(lat, lon, rng, a0, a1)
            tiles = ta._band_tiles(ta._compute_subtiles(bbox, res, lat, lon, rng, a0, a1),
                                   lat, lon, min_range_km, skip_footprints)
            fps = {ta.tile_footprint(t) for t in tiles}
            seen.append(fps)
            return {"bbox": bbox, "tiles_total": len(tiles), "tiles_cached": 0,
                    "tiles_missing": len(tiles), "download_mb": 1, "total_mb": 1,
                    "footprints": fps}

        with mock.patch.object(ta, "terrain_plan", side_effect=fake_plan):
            total = ta.terrain_plan_profile("src", *self.TX, bands)
        self.assertEqual(len(seen), len(bands))
        for i in range(len(seen)):
            for j in range(i):
                self.assertFalse(seen[i] & seen[j], f"band {i} repeats band {j}")
        self.assertEqual(total["tiles_total"], sum(len(s) for s in seen))

    def test_prepare_profile_links_all_bands_into_one_view(self):
        bands = rp.adaptive_bands(5, 20)
        calls = []
        with tempfile.TemporaryDirectory() as cache, \
             mock.patch.object(ta, "prepare_terrain",
                               side_effect=lambda *a, **kw: calls.append((a, kw)) or "view"), \
             mock.patch.object(ta, "get_cache_dir", return_value=cache), \
             mock.patch.object(ta, "_sync_view", return_value=[]) as sync, \
             mock.patch.object(ta, "_record_view_dir"):
            layer = mock.Mock()
            layer.source.return_value = "type=xyz&url=https://example/{z}/{x}/{y}.png"
            view = ta.prepare_terrain_profile(layer, *self.TX, bands, binary_manager=None)
        # 5 m to 20 km: the 0.25 deg 5 m tiles already cover the whole 10 m ring
        # (10 m shares that grid), so the 10 m band fetches nothing at all.
        self.assertEqual([s for _, s in bands], [5, 10, 30, 90])
        self.assertEqual([a[4] for a, _ in calls], [5, 30, 90])
        self.assertEqual([kw["min_range_km"] for _, kw in calls], [0.0, 9.5, 12.0])
        names = sync.call_args[0][2]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(any("_5m" in n for n in names) and any("_90m" in n for n in names))
        self.assertTrue(view.startswith(cache))


class EngineGate(unittest.TestCase):

    def _check(self, version, profiled):
        # 30 m: legal on every engine, so only the profile gate can refuse.
        with mock.patch.object(bm, "read_engine_version", return_value=version), \
             mock.patch.object(bm, "find_binary", return_value="/x/aether_core"):
            return bm.check_engine_for_job(30, range_profile=profiled)

    def test_old_engine_refuses_a_profile_before_any_download(self):
        for old in ("0.4.8", None):
            with self.subTest(version=old):
                with self.assertRaises(bm.EngineTooOldError) as ctx:
                    self._check(old, True)
                self.assertIn(rp.RANGE_PROFILE_MIN_ENGINE, str(ctx.exception))
                self.assertIn("Nothing was downloaded", str(ctx.exception))

    def test_new_engine_runs_a_profile_and_old_one_a_fixed_job(self):
        self.assertEqual(self._check(rp.RANGE_PROFILE_MIN_ENGINE, True), rp.RANGE_PROFILE_MIN_ENGINE)
        self.assertEqual(self._check("0.4.8", False), "0.4.8")


class OneJobPerSite(unittest.TestCase):
    """Adaptive changes the radial step only: one job, one output, per site."""

    def _tab(self, mode):
        from waveshed.gui.site_analysis_tab import SiteAnalysisTab
        tab = SiteAnalysisTab.__new__(SiteAnalysisTab)
        tab._dialog = mock.Mock(get_mode=mock.Mock(return_value="LOSS"),
                                get_loss_model=mock.Mock(return_value="ITM"))
        tab.combo_resolution = mock.Mock(currentText=mock.Mock(return_value="5"))
        tab.combo_backend = mock.Mock(currentText=mock.Mock(return_value="AUTO"))
        tab.combo_climate = mock.Mock(currentIndex=mock.Mock(return_value=4))
        tab.combo_pol = mock.Mock(currentIndex=mock.Mock(return_value=0))
        tab.combo_earth_radius = mock.Mock(currentText=mock.Mock(return_value="FOUR_THIRDS"))
        for name, value in (("spin_eps", 15.0), ("spin_sgm", 0.005), ("spin_ens", 301.0),
                            ("spin_conf", 0.5), ("spin_rel", 0.5)):
            setattr(tab, name, mock.Mock(value=mock.Mock(return_value=value)))
        for name, on in (("radio_res_fixed", mode == rp.MODE_FIXED),
                         ("radio_res_adaptive", mode == rp.MODE_ADAPTIVE),
                         ("radio_res_custom", mode == rp.MODE_CUSTOM)):
            setattr(tab, name, mock.Mock(isChecked=mock.Mock(return_value=on)))
        tab._custom_bands = None
        tab._read_sites = lambda: [{"coords": (47.0, 8.0), "range": 50, "height": 30.0}]
        tab._read_altitudes = lambda: [(2.0, "AGL"), (10.0, "AGL")]
        tab._get_asset = lambda name: None
        return tab

    def test_adaptive_is_one_job_per_site_and_height(self):
        jobs = self._tab(rp.MODE_ADAPTIVE)._build_jobs()
        self.assertEqual(len(jobs), 2)  # 1 site x 2 heights, no split
        for params, name in jobs:
            self.assertEqual(params.resolution_m, 5)
            self.assertEqual(params.max_range_km, 50)
            self.assertEqual(params.range_profile, rp.adaptive_bands(5, 50))
            self.assertIn("90 m beyond", name)
        self.assertEqual(len({p.output_name for p, _ in jobs}), 2)

    def test_fixed_has_no_profile(self):
        jobs = self._tab(rp.MODE_FIXED)._build_jobs()
        self.assertEqual([p.range_profile for p, _ in jobs], [None, None])


if __name__ == "__main__":
    unittest.main()
