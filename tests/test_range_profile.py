"""Adaptive / custom resolution (core.range_profile and everything that uses it).

A range profile makes the far field of a coverage run coarser than the near
field. The pieces tested here: the band rules (ladder, tile-extent nesting,
clipping), the job JSON (one job: base resolution + profile), the
minimum-download terrain filter, and the engine gate.
"""

import conftest  # noqa: F401

import math
import os
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


class BuildingsPerBand(unittest.TestCase):
    """Buildings only where they pay: Adaptive skips bands at >= 90 m, Custom
    chooses per band (same default), "Include buildings" is the master."""

    def test_adaptive_rule_including_250(self):
        bands = [(7000.0, 2), (12000.0, 30), (50000.0, 90), (100000.0, 250)]
        self.assertEqual(rp.band_buildings(bands), [True, True, False, False])
        self.assertEqual(rp.band_buildings(rp.adaptive_bands(2, 50)), [True, True, False])

    def test_master_off_turns_every_band_off(self):
        bands = rp.adaptive_bands(5, 50)
        self.assertEqual(rp.band_buildings(bands, {90: True}, master=False),
                         [False] * len(bands))

    def test_custom_overrides_and_defaults(self):
        bands = [(7000.0, 5), (20000.0, 30), (50000.0, 90)]
        self.assertEqual(rp.band_buildings(bands, {90: True, 5: False}), [False, True, True])
        # A profile kept from before the flag existed (no overrides) -> the rule.
        for old in (None, {}):
            self.assertEqual(rp.band_buildings(bands, old), [True, True, False])

    def test_notes_say_why(self):
        bands = rp.adaptive_bands(2, 50)
        flags = rp.band_buildings(bands)
        notes = rp.buildings_notes(bands, flags, rp.MODE_ADAPTIVE)
        self.assertEqual(notes[-1], "buildings skipped (adaptive: \u2265 90 m)")
        self.assertTrue(notes[0].startswith("buildings on"))
        off = rp.buildings_notes(bands, [False] * 3, rp.MODE_ADAPTIVE, master=False)
        self.assertTrue(all("unchecked" in n for n in off))
        self.assertIn("custom", rp.buildings_notes(bands, flags, rp.MODE_CUSTOM)[2])


class BuildingsPerBandTerrain(unittest.TestCase):
    """prepare_terrain_profile / terrain_plan_profile honour the per-band flag
    and keep the two pools apart."""

    TX = (47.05, 8.48)

    def _run(self, bands, osm, flags):
        calls, syncs = [], []
        with tempfile.TemporaryDirectory() as cache, \
             mock.patch.object(ta, "prepare_terrain",
                               side_effect=lambda *a, **kw: calls.append((a, kw)) or "v"), \
             mock.patch.object(ta, "get_cache_dir", return_value=cache), \
             mock.patch.object(ta, "_sync_view",
                               side_effect=lambda *a, **kw: syncs.append((a, kw)) or []), \
             mock.patch.object(ta, "_record_view_dir"), \
             mock.patch.object(ta, "_log") as log:
            layer = mock.Mock()
            layer.source.return_value = "type=xyz&url=https://example/{z}/{x}/{y}.png"
            view = ta.prepare_terrain_profile(
                layer, *self.TX, bands, binary_manager=None, osm_buildings=osm,
                band_buildings=flags,
                buildings_notes=(rp.buildings_notes(bands, flags, rp.MODE_ADAPTIVE, osm)
                                 if flags is not None else None))
            src = layer.source.return_value
            pools = {None: ta._pool_dir(src, None),
                     "osm": ta._pool_dir(src, ta.buildings_identity(None, True))}
        lines = [c.args[0] for c in log.call_args_list]
        return calls, syncs, view, pools, lines

    def test_each_band_gets_its_flag_and_its_pool(self):
        bands = rp.adaptive_bands(2, 50)                 # 2 / 30 / 90 m
        flags = rp.band_buildings(bands)
        calls, syncs, view, pools, lines = self._run(bands, True, flags)
        self.assertEqual([(a[4], kw["osm_buildings"]) for a, kw in calls],
                         [(2, True), (30, True), (90, False)])
        by_pool = {a[0]: a[2] for a, _ in syncs}
        self.assertEqual(set(by_pool), {pools[None], pools["osm"]})
        self.assertTrue(all("_90m" in n for n in by_pool[pools[None]]))
        self.assertFalse(any("_90m" in n for n in by_pool[pools["osm"]]))
        # One view, both pools linked into it, neither pass prunes the other.
        everything = by_pool[pools[None]] + by_pool[pools["osm"]]
        self.assertEqual(len(everything), len(set(everything)))
        self.assertEqual({a[1] for a, _ in syncs}, {view})
        for _, kw in syncs:
            self.assertEqual(set(kw["keep"]), set(everything))
        self.assertTrue(any("12-50 km @ 90 m" in ln and "buildings skipped (adaptive: \u2265 90 m)"
                            in ln for ln in lines), lines)

    def test_view_identity_follows_the_band_flags(self):
        bands = rp.adaptive_bands(2, 50)
        key = os.path.basename  # each run has its own temp cache dir
        mixed = key(self._run(bands, True, [True, True, False])[2])
        all_on = key(self._run(bands, True, None)[2])
        none = key(self._run(bands, False, None)[2])
        master_off = self._run(bands, False, [True, True, False])
        self.assertEqual(len({mixed, all_on, none}), 3)
        # Master off: nothing gets buildings, whatever the band flags say.
        self.assertEqual(key(master_off[2]), none)
        self.assertTrue(all(not kw["osm_buildings"] for _, kw in master_off[0]))

    def test_real_view_links_each_band_from_its_own_pool(self):
        # No _sync_view mock: tiles really pooled (with / without buildings),
        # really linked; the 90 m tiles must come from the building-free pool.
        import struct
        bands = rp.adaptive_bands(2, 50)
        flags = rp.band_buildings(bands)
        layer = mock.Mock()
        layer.source.return_value = "type=xyz&url=https://example/{z}/{x}/{y}.png"
        src = layer.source.return_value

        def write_abt(path, size=16):
            stride = (size * 2 + 255) & ~255
            hdr = (b"AETH" + struct.pack("<HH", 1, size)
                   + struct.pack("<dddd", 47.0, 8.0, 1.0 / size, 1.0 / size)
                   + struct.pack("<hH", 0, stride))
            with open(path, "wb") as fh:
                fh.write(hdr + (struct.pack("<h", 500) * size
                                + b"\0" * (stride - 2 * size)) * size)

        def fake_prepare(dem, lat, lon, rng, step, bm_, fb, tdir, a0, a1, **kw):
            bbox = ta._compute_sector_bbox(lat, lon, rng, a0, a1)
            tiles = ta._band_tiles(ta._compute_subtiles(bbox, step, lat, lon, rng, a0, a1),
                                   lat, lon, kw["min_range_km"], kw["skip_footprints"])
            pool = ta._pool_dir(src, ta.buildings_identity(None, kw["osm_buildings"]))
            os.makedirs(pool, exist_ok=True)
            for t in tiles:
                write_abt(os.path.join(pool, t["filename"]))
            return "per-band-view"

        with tempfile.TemporaryDirectory() as cache, \
             mock.patch.object(ta, "prepare_terrain", side_effect=fake_prepare), \
             mock.patch.object(ta, "get_cache_dir", return_value=cache), \
             mock.patch.object(ta, "_record_view_dir"):
            view = ta.prepare_terrain_profile(layer, *self.TX, bands, binary_manager=None,
                                              osm_buildings=True, band_buildings=flags)
            plain = ta._pool_dir(src, None)
            osm = ta._pool_dir(src, ta.buildings_identity(None, True))
            in_view = sorted(os.listdir(view))
            self.assertTrue(any("_2m" in n for n in in_view))
            self.assertTrue(any("_90m" in n for n in in_view))
            for name in in_view:
                pool = plain if "_90m" in name else osm
                self.assertTrue(os.path.samefile(os.path.join(view, name),
                                                 os.path.join(pool, name)), name)
                other = osm if pool == plain else plain
                self.assertFalse(os.path.exists(os.path.join(other, name)), name)
            # Same tiles, every band with buildings: another view directory.
            for n in os.listdir(plain):
                write_abt(os.path.join(osm, n))
            all_on = ta.prepare_terrain_profile(layer, *self.TX, bands, binary_manager=None,
                                                osm_buildings=True)
            self.assertNotEqual(all_on, view)
            self.assertEqual(sorted(os.listdir(all_on)), in_view)
            # Re-running the mixed profile keeps its view intact (no cross-pool prune).
            again = ta.prepare_terrain_profile(layer, *self.TX, bands, binary_manager=None,
                                               osm_buildings=True, band_buildings=flags)
            self.assertEqual(again, view)
            self.assertEqual(sorted(os.listdir(view)), in_view)

    def test_flag_count_must_match(self):
        with self.assertRaises(ValueError):
            self._run(rp.adaptive_bands(2, 50), True, [True])

    def test_plan_prices_each_band_against_its_pool(self):
        bands = rp.adaptive_bands(2, 50)
        seen = []

        def fake_plan(source, lat, lon, rng, res, a0, a1, b, min_range_km=0.0,
                      skip_footprints=None):
            seen.append((res, b))
            return {"bbox": None, "tiles_total": 0, "tiles_cached": 0, "tiles_missing": 0,
                    "download_mb": 0, "total_mb": 0, "footprints": set()}

        osm = ta.buildings_identity(None, True)
        with mock.patch.object(ta, "terrain_plan", side_effect=fake_plan):
            ta.terrain_plan_profile("src", *self.TX, bands, buildings=osm,
                                    band_buildings=rp.band_buildings(bands))
            ta.terrain_plan_profile("src", *self.TX, bands, buildings=osm)
        self.assertEqual(seen, [(2, osm), (30, osm), (90, None),
                                (2, osm), (30, osm), (90, osm)])


class MultiSiteBuildingsViews(unittest.TestCase):
    """Several sites in one run share pools; each gets its own view holding one
    tile per footprint, each tile from the pool its band's flag names — also
    across runs with different buildings choices (real filesystem, downloads
    faked: a pool tile's elevation says which pool built it)."""

    A = (47.05659, 8.485293)          # Rigi Kulm
    B = (47.05659, 8.5248)            # 3 km east: shares 2 m and 30 m footprints
    PLAIN, OSM = 500, 700             # half-metres written by each pool

    def setUp(self):
        import struct
        self.struct = struct
        self.layer = mock.Mock()
        self.layer.source.return_value = "type=xyz&url=https://example/{z}/{x}/{y}.png"
        self.src = self.layer.source.return_value
        self.bands = rp.adaptive_bands(2, 15)               # 2 / 30 / 90 m
        self.assertEqual([s for _, s in self.bands], [2, 30, 90])
        self.built = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = tmp.name
        for p in (mock.patch.object(ta, "prepare_terrain", side_effect=self._fake_prepare),
                  mock.patch.object(ta, "get_cache_dir", return_value=self.cache),
                  mock.patch.object(ta, "_record_view_dir")):
            p.start()
            self.addCleanup(p.stop)
        self.pool = {False: ta._pool_dir(self.src, None),
                     True: ta._pool_dir(self.src, ta.buildings_identity(None, True))}

    def _write_abt(self, path, value, size=16):
        st = self.struct
        stride = (size * 2 + 255) & ~255
        hdr = (b"AETH" + st.pack("<HH", 1, size)
               + st.pack("<dddd", 47.0, 8.0, 1.0 / size, 1.0 / size) + st.pack("<hH", 0, stride))
        with open(path, "wb") as fh:
            fh.write(hdr + (st.pack("<h", value) * size + b"\0" * (stride - 2 * size)) * size)

    def _value(self, path):
        with open(path, "rb") as fh:
            fh.seek(44)
            return self.struct.unpack("<h", fh.read(2))[0]

    def _fake_prepare(self, dem, lat, lon, rng, step, bm_, fb, tdir, a0, a1, **kw):
        bbox = ta._compute_sector_bbox(lat, lon, rng, a0, a1)
        tiles = ta._band_tiles(ta._compute_subtiles(bbox, step, lat, lon, rng, a0, a1),
                               lat, lon, kw["min_range_km"], kw["skip_footprints"])
        osm = kw["osm_buildings"]
        pool = self.pool[osm]
        os.makedirs(pool, exist_ok=True)
        for t in tiles:
            path = os.path.join(pool, t["filename"])
            if not os.path.exists(path):            # pooled: built once, shared
                self._write_abt(path, self.OSM if osm else self.PLAIN)
                self.built.append((osm, t["filename"]))
        return "per-band-view"

    def _site(self, tx, flags):
        return ta.prepare_terrain_profile(self.layer, *tx, self.bands, binary_manager=None,
                                          osm_buildings=True, band_buildings=flags)

    def _audit(self, view, flags):
        """{name: osm?} for *view*, asserting every tile is a link to the pool
        its band's flag names, carries that pool's content, and that the view
        holds one tile per footprint."""
        want = {s: f for (_, s), f in zip(self.bands, flags)}
        out, footprints = {}, set()
        for name in sorted(os.listdir(view)):
            step = int(name.rsplit("_", 1)[1][:-5])
            osm = want[step]
            path = os.path.join(view, name)
            self.assertTrue(os.path.samefile(path, os.path.join(self.pool[osm], name)),
                            f"{name} in {view} is not the {'osm' if osm else 'plain'} pool tile")
            self.assertEqual(self._value(path), self.OSM if osm else self.PLAIN, name)
            ul = name[len("tile_"):].rsplit("_", 1)[0]
            fp = (ul, ta.ABT_EXTENT_DEG[step])
            self.assertNotIn(fp, footprints, f"footprint twice in {view}: {name}")
            footprints.add(fp)
            out[name] = osm
        return out

    def test_two_sites_share_pools_and_keep_their_own_views(self):
        adaptive = rp.band_buildings(self.bands)                     # T T F
        custom = rp.band_buildings(self.bands, {30: False})          # T F F
        self.assertEqual((adaptive, custom), ([True, True, False], [True, False, False]))

        # (a) A then B, adaptive: the overlap is built once and shared.
        va = self._site(self.A, adaptive)
        built_after_a = len(self.built)
        vb = self._site(self.B, adaptive)
        self.assertNotEqual(va, vb)
        sa, sb = self._audit(va, adaptive), self._audit(vb, adaptive)
        for step in (2, 30):
            shared = {n for n in set(sa) & set(sb) if n.endswith(f"_{step}m.abt")}
            self.assertTrue(shared, f"sites 3 km apart must share {step} m tiles")
            for n in shared:                                 # same pooled file
                self.assertTrue(os.path.samefile(os.path.join(va, n), os.path.join(vb, n)))
        self.assertFalse([b for b in self.built[built_after_a:]
                          if b[1] in sa], "B rebuilt a tile A had pooled")

        # (b) B's 90 m (plain) and A's 30 m (buildings) cover the same ground
        # as different files; neither pool holds the other's resolution there.
        a30 = {n for n, o in sa.items() if n.endswith("_30m.abt")}
        b90 = {n for n, o in sb.items() if n.endswith("_90m.abt")}
        self.assertTrue(a30 and b90 and not a30 & b90)
        self.assertTrue(all(sa[n] for n in a30) and not any(sb[n] for n in b90))
        self.assertFalse(any(os.path.exists(os.path.join(self.pool[False], n)) for n in a30))
        self.assertFalse(any(os.path.exists(os.path.join(self.pool[True], n)) for n in b90))
        snap = {v: {n: os.stat(os.path.join(v, n)).st_ino for n in os.listdir(v)}
                for v in (va, vb)}

        # (c) Custom, 30 m without buildings: both sites read the plain 30 m
        # tiles, from new views; the shared 30 m footprints are one plain file.
        vca, vcb = self._site(self.A, custom), self._site(self.B, custom)
        self.assertEqual(len({va, vb, vca, vcb}), 4)
        sca, scb = self._audit(vca, custom), self._audit(vcb, custom)
        self.assertEqual(set(sca), set(sa))           # same tiles, other pool for 30 m
        c30 = {n for n in set(sca) & set(scb) if n.endswith("_30m.abt")}
        self.assertTrue(c30)
        for n in c30:
            self.assertTrue(os.path.samefile(os.path.join(vca, n),
                                             os.path.join(self.pool[False], n)))
            self.assertTrue(os.path.samefile(os.path.join(vcb, n), os.path.join(vca, n)))

        # (d) Adaptive again, then custom again: the same views come back
        # untouched — nothing pruned, nothing relinked from the other pool.
        self.assertEqual((self._site(self.A, adaptive), self._site(self.B, adaptive)), (va, vb))
        for v in (va, vb):
            self.assertEqual({n: os.stat(os.path.join(v, n)).st_ino for n in os.listdir(v)},
                             snap[v])
        self.assertEqual(self._audit(va, adaptive), sa)
        self.assertEqual(self._audit(vb, adaptive), sb)
        self.assertEqual(self._audit(vca, custom), sca)
        self.assertEqual(self._audit(vcb, custom), scb)

    def test_stale_or_foreign_tile_in_a_view_is_replaced_or_pruned(self):
        # A view damaged between runs (another pool's file under a band's
        # name, a stray tile) is repaired by the next run of that profile.
        flags = rp.band_buildings(self.bands)
        view = self._site(self.A, flags)
        names = self._audit(view, flags)
        n2 = next(n for n in names if n.endswith("_2m.abt"))
        n90 = next(n for n in names if n.endswith("_90m.abt"))
        os.remove(os.path.join(view, n2))
        self._write_abt(os.path.join(self.pool[False], n2), self.PLAIN)
        os.link(os.path.join(self.pool[False], n2), os.path.join(view, n2))
        # The real pool tile is newer than the foreign copy (as after a rebuild).
        t = os.path.getmtime(os.path.join(view, n2)) + 60
        os.utime(os.path.join(self.pool[True], n2), (t, t))
        self._write_abt(os.path.join(view, "tile_N48.00E9.00_30m.abt"), self.OSM)
        self.assertEqual(self._site(self.A, flags), view)
        self.assertEqual(self._audit(view, flags), names)
        self.assertNotIn("tile_N48.00E9.00_30m.abt", os.listdir(view))
        self.assertTrue(os.path.samefile(os.path.join(view, n90),
                                         os.path.join(self.pool[False], n90)))


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

    def _tab(self, mode, output_res="5"):
        from waveshed.gui.site_analysis_tab import SiteAnalysisTab
        tab = SiteAnalysisTab.__new__(SiteAnalysisTab)
        tab._dialog = mock.Mock(get_mode=mock.Mock(return_value="LOSS"),
                                get_loss_model=mock.Mock(return_value="ITM"))
        tab.combo_resolution = mock.Mock(currentText=mock.Mock(return_value="5"))
        tab.combo_output_res = mock.Mock(currentText=mock.Mock(return_value=output_res),
                                         count=mock.Mock(return_value=6))
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
        tab._custom_buildings = {}
        tab.chk_buildings = mock.Mock(isChecked=mock.Mock(return_value=True))
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

    def test_output_pixel_is_explicit_and_steps_keep_the_near_field(self):
        # The owner's case: fine rays near the TX, a 30 m map — one job, the
        # profile still starting at 5 m, resolution_m (the map grid) 30 m.
        jobs = self._tab(rp.MODE_ADAPTIVE, output_res="30")._build_jobs()
        for params, name in jobs:
            self.assertEqual(params.resolution_m, 30)
            self.assertEqual(params.range_profile[0][1], 5)
            self.assertIn("output 30 m", name)

    def test_fixed_ignores_the_output_pixel_choice(self):
        jobs = self._tab(rp.MODE_FIXED, output_res="30")._build_jobs()
        self.assertEqual({p.resolution_m for p, _ in jobs}, {5})

    def test_fixed_has_no_profile(self):
        jobs = self._tab(rp.MODE_FIXED)._build_jobs()
        self.assertEqual([p.range_profile for p, _ in jobs], [None, None])
        self.assertEqual([p.range_profile_buildings for p, _ in jobs], [None, None])

    def test_adaptive_jobs_carry_the_band_buildings(self):
        for params, _ in self._tab(rp.MODE_ADAPTIVE)._build_jobs():
            self.assertEqual([s for _, s in params.range_profile], [5, 10, 30, 90])
            self.assertEqual(params.range_profile_buildings, [True, True, True, False])
            self.assertIn("\u2265 90 m", params.range_profile_buildings_notes[-1])

    def test_master_off_gives_no_band_buildings(self):
        tab = self._tab(rp.MODE_ADAPTIVE)
        tab.chk_buildings.isChecked.return_value = False
        for params, _ in tab._build_jobs():
            self.assertEqual(params.range_profile_buildings, [False] * 4)

    def test_custom_jobs_use_the_editor_choice(self):
        tab = self._tab(rp.MODE_CUSTOM)
        tab._custom_bands = [(7000.0, 5), (20000.0, 30), (50000.0, 90)]
        tab._custom_buildings = {30: False, 90: True}
        for params, _ in tab._build_jobs():
            self.assertEqual(params.range_profile_buildings, [True, False, True])
            self.assertNotIn("range_profile_buildings",
                             build_coverage_job(params, "/abt", "/out")["analysis"])

    def test_multi_site_flags_follow_each_sites_own_bands(self):
        # One custom profile for the run, clipped per site: flags are keyed by
        # step, so each site's list lines up with its own bands. A site whose
        # range ends inside the first band runs fixed but keeps that band's
        # choice — it used to get buildings from the master switch alone.
        tab = self._tab(rp.MODE_CUSTOM)
        tab._custom_bands = [(7000.0, 5), (20000.0, 30), (50000.0, 90)]
        tab._custom_buildings = {5: False, 30: True}
        tab._read_sites = lambda: [
            {"coords": (47.0, 8.0), "range": 50, "height": 30.0},
            {"coords": (47.0, 8.04), "range": 15, "height": 30.0},
            {"coords": (47.0, 8.08), "range": 5, "height": 30.0}]
        tab._read_altitudes = lambda: [(2.0, "AGL")]
        got = [([s for _, s in p.range_profile or []], p.range_profile_buildings)
               for p, _ in tab._build_jobs()]
        self.assertEqual(got, [([5, 30, 90], [False, True, False]),
                               ([5, 30], [False, True]),
                               ([], [False])])

    def test_short_custom_site_terrain_follows_its_band(self):
        import dataclasses
        from waveshed.gui import site_analysis_tab as sat
        params = CoverageParams(tx_lat=47.0, tx_lon=8.0, max_range_km=5, resolution_m=5,
                                range_profile_buildings=[False])
        calls = []

        class Stop(Exception):
            pass

        for flags, master, want in (([False], True, False), ([True], True, True),
                                    (None, True, True), ([True], False, False)):
            job = (dataclasses.replace(params, range_profile_buildings=flags), "s")
            w = sat._SiteAnalysisWorker([job], mock.Mock(), tempfile.gettempdir(), "", None,
                                        osm_buildings=master)
            with mock.patch.object(sat, "prepare_terrain",
                                   side_effect=lambda **kw: calls.append(kw["osm_buildings"])
                                   or "/v"), \
                 mock.patch.object(sat, "build_coverage_job", side_effect=Stop), \
                 mock.patch.object(bm, "check_engine_for_job", return_value="0.4.10"):
                w.run()
            self.assertEqual(calls.pop(), want, (flags, master))


if __name__ == "__main__":
    unittest.main()
