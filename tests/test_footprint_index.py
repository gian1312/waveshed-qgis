"""Tile -> file matching through ONE footprint index per run (2026-10-20).

A 43,650-file folder over a 2,610-tile Map Converter run spent ~100 s in the
stale-tile fingerprints and ~87 s in the job build: ``_tile_contributors``
tested every file against every tile (112 M bbox tests per pass), twice.
Pinned here:

* ``FootprintIndex`` (0.1 deg buckets) gives EXACTLY the full scan's
  contributor list — same files, same order, same halo boxes — on random
  layouts (files over several cells, tiles on cell borders, big halos, high
  latitudes, both hemispheres, odd bounds);
* each tile is matched once per run: the fingerprint and the job's
  ``sources[]`` share the match (Map Converter, Site Analysis, profile);
* ``preflight_ingest_jobs`` checks each distinct path once;
* the pool-checked download estimate is one directory listing, same rule
  as ``_tile_ready``;
* the download ESTIMATE (``_pool_estimate_tiles``) counts a pool tile only
  where the files above the first XYZ entry do not cover the tile + halo by
  rectangles (the exact set is what the two-phase build's Phase 2 leaves —
  ``tests/test_two_phase_convert.py``); a tile without a pool tile has a
  job without a pool source that passes the preflight;
* ``ensure_pool_tiles`` and the Site Analysis stale check use the same one
  listing (no header read per tile);
* the worker says what it does while matching.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import json
import math
import os
import random
import tempfile
import unittest
from unittest import mock


import waveshed.core.terrain_adapter as ta
from waveshed.core import terrain_index as ti
import waveshed.gui.map_converter_tab as mct
from waveshed.gui.map_converter_tab import _LayerEntry, _MapConverterWorker
from _real_gdal import HAVE_GDAL, OSR, real_gdal
from test_terrain_adapter import _write_pool_abt


def _b(w, e, s, n):
    return {"west": w, "east": e, "south": s, "north": n}


def _info(path, bounds, halo=0.001, crs="EPSG:4326", **extra):
    info = {"path": path, "crs": None, "crs_authid": crs,
            "native_bounds": dict(bounds), "wgs84_bounds": dict(bounds),
            "halo_deg": halo, "fp": "1:2"}
    info.update(extra)
    return info


def _full_scan(infos, tb, res):
    return ta._tile_contributors(infos, tb, res)


def _random_layout(rng, n_files):
    """Files of every awkward kind: inside one cell, over several cells,
    edges ON the 0.1 deg cell borders, huge, zero-width, big halos."""
    base_lon = rng.choice((-75.3, -0.25, 7.0, 179.2))
    base_lat = rng.choice((-45.0, -0.15, 46.5, 84.0))
    infos = []
    for k in range(n_files):
        kind = rng.random()
        if kind < 0.35:                       # small, anywhere
            w = base_lon + rng.uniform(-0.6, 0.6)
            s = base_lat + rng.uniform(-0.6, 0.6)
            b = _b(w, w + rng.uniform(0.001, 0.05), s,
                   s + rng.uniform(0.001, 0.05))
        elif kind < 0.6:                      # spans several cells
            w = base_lon + rng.uniform(-0.8, 0.5)
            s = base_lat + rng.uniform(-0.8, 0.5)
            b = _b(w, w + rng.uniform(0.1, 0.45), s,
                   s + rng.uniform(0.1, 0.45))
        elif kind < 0.85:                     # edges exactly on cell borders
            cx = math.floor(base_lon * 10) + rng.randint(-6, 6)
            cy = math.floor(base_lat * 10) + rng.randint(-6, 6)
            b = _b(cx * 0.1, (cx + rng.randint(1, 3)) * 0.1,
                   cy * 0.1, (cy + rng.randint(1, 3)) * 0.1)
        elif kind < 0.9:                      # zero width / height
            w = base_lon + rng.uniform(-0.5, 0.5)
            s = base_lat + rng.uniform(-0.5, 0.5)
            b = _b(w, w, s, s + 0.02)
        elif kind < 0.95:                     # continental
            b = _b(base_lon - 20, base_lon + 20, base_lat - 5, base_lat + 5)
        else:                                 # huge halo
            w = base_lon + rng.uniform(-0.6, 0.6)
            s = base_lat + rng.uniform(-0.6, 0.6)
            b = _b(w, w + 0.01, s, s + 0.01)
        halo = (rng.choice((0.0, 1e-5, 7.2e-5, 0.001, 0.0032, 0.036))
                if kind < 0.95 else rng.uniform(0.05, 0.2))
        infos.append(_info(f"/f/{k:04d}.tif", b, halo=halo))
    return infos, base_lon, base_lat


def _random_tiles(rng, base_lon, base_lat, n):
    tiles = []
    for _ in range(n):
        kind = rng.random()
        size = rng.choice((0.1, 0.25, 0.5, 0.013, 1.0))
        if kind < 0.5:                        # on the grid (cell borders)
            w = (math.floor(base_lon * 10) + rng.randint(-8, 8)) * 0.1
            n_ = (math.floor(base_lat * 10) + rng.randint(-8, 8)) * 0.1
        else:                                 # anywhere
            w = base_lon + rng.uniform(-0.9, 0.9)
            n_ = base_lat + rng.uniform(-0.9, 0.9)
        tiles.append((_b(w, w + size, n_ - size, n_),
                      rng.choice((2.0, 5.0, 10.0, 30.0, 90.0, 250.0))))
    return tiles


class FootprintIndexMatchesFullScan(unittest.TestCase):

    def assert_same(self, infos, tb, res, idx):
        got = ta._tile_contributors(infos, tb, res, index=idx)
        want = _full_scan(infos, tb, res)
        self.assertEqual([(i, id(f), h) for i, f, h in got],
                         [(i, id(f), h) for i, f, h in want],
                         f"tile {tb} @ {res} m")
        return want

    def test_random_layouts_give_the_full_scan_list(self):
        hits = 0
        for seed in range(60):
            rng = random.Random(seed)
            infos, lon, lat = _random_layout(rng, rng.randint(5, 120))
            idx = ta.FootprintIndex(infos)
            for tb, res in _random_tiles(rng, lon, lat, 60):
                hits += bool(self.assert_same(infos, tb, res, idx))
        self.assertGreater(hits, 500, "the layouts must actually hit files")

    def test_halo_boundary_at_a_cell_border(self):
        # Tile west edge ON a cell border; files end just inside / outside
        # the halo box (bboxes_intersect's 1e-9 epsilon included).
        tb, res = _b(8.0, 8.1, 47.0, 47.1), 2.0
        cos = max(math.cos(math.radians(47.05)), 0.2)
        halo = 0.004
        edge = 8.0 - halo / cos
        infos = [
            _info("/in.tif", _b(7.95, edge + 2e-9, 47.0, 47.1), halo=halo),
            _info("/out.tif", _b(7.95, edge + 1e-9, 47.0, 47.1), halo=halo),
            _info("/touch.tif", _b(7.95, edge, 47.0, 47.1), halo=halo),
            # A tiny halo here: the BIG one of another file must not let
            # this file in (the exact test is per file).
            _info("/small.tif", _b(7.99, 7.9995, 47.0, 47.1), halo=1e-5),
        ]
        want = self.assert_same(infos, tb, res, ta.FootprintIndex(infos))
        self.assertEqual([f["path"] for _i, f, _h in want], ["/in.tif"])

    def test_odd_bounds_are_judged_by_the_exact_test(self):
        nan = float("nan")
        infos = [
            _info("/ok.tif", _b(8.01, 8.02, 47.01, 47.02)),
            _info("/nan.tif", _b(nan, 8.02, 47.01, 47.02)),
            _info("/inv.tif", _b(8.5, 8.0, 47.01, 47.02)),
            _info("/inf.tif", _b(-math.inf, math.inf, 47.0, 47.1)),
        ]
        idx = ta.FootprintIndex(infos)
        for tb in (_b(8.0, 8.1, 47.0, 47.1), _b(30.0, 30.1, 10.0, 10.1)):
            self.assert_same(infos, tb, 30.0, idx)

    def test_bound_to_its_own_list(self):
        infos = [_info("/a.tif", _b(8.0, 8.1, 47.0, 47.1))]
        idx = ta.FootprintIndex(infos)
        with self.assertRaises(ValueError):
            ta._tile_contributors(list(infos), _b(8.0, 8.1, 47.0, 47.1),
                                  30.0, index=idx)

    def test_each_tile_is_matched_once(self):
        infos = [_info(f"/{k}.tif", _b(8.0 + k * 0.03, 8.01 + k * 0.03,
                                       47.0, 47.1)) for k in range(30)]
        idx = ta.FootprintIndex(infos)
        tb = _b(8.0, 8.1, 47.0, 47.1)
        with mock.patch.object(ta, "_scan_contributors",
                               wraps=ta._scan_contributors) as scan:
            a = ta.tile_source_fingerprint(infos, tb, 2.0, index=idx)
            s1, _t = ta.build_tile_sources(infos, tb, "/tmp", "t", 2.0,
                                           index=idx)
            ta.build_tile_sources(infos, tb, "/tmp", "t", 2.0, index=idx)
        self.assertEqual(scan.call_count, 1)
        # Only the files of the buckets around the tile (8.0-8.2 E) reach
        # the exact test, not all 30 (8.0-8.9 E).
        self.assertEqual(list(scan.call_args[0][1]), list(range(7)))
        self.assertEqual(a, ta.tile_source_fingerprint(infos, tb, 2.0))
        self.assertEqual(s1, ta.build_tile_sources(infos, tb, "/tmp", "t",
                                                   2.0)[0])

    def test_fingerprints_and_sources_equal_the_full_scan(self):
        rng = random.Random(7)
        infos, lon, lat = _random_layout(rng, 200)
        infos = [i for i in infos if all(map(math.isfinite,
                                             i["wgs84_bounds"].values()))]
        idx = ta.FootprintIndex(infos)
        for tb, res in _random_tiles(rng, lon, lat, 80):
            self.assertEqual(
                ta.tile_source_fingerprint(infos, tb, res, index=idx),
                ta.tile_source_fingerprint(infos, tb, res))
            self.assertEqual(
                ta.build_tile_sources(infos, tb, "/tmp", "t", res,
                                      index=idx),
                ta.build_tile_sources(infos, tb, "/tmp", "t", res))


# ---------------------------------------------------------------------------
# Once per run — Map Converter, Site Analysis, range profile
# ---------------------------------------------------------------------------

_EXT = {"south": 47.1, "north": 47.25, "west": 8.1, "east": 8.35}


def _grid_infos(ext=_EXT, step=0.05, halo=7.2e-5, skip=()):
    """4326 files tiling the 2 m tiles over *ext* (plus one step around
    them) edge to edge; *skip* = ``(i, j)`` grid cells left out."""
    tiles = mct._snap_bbox(ext, [2])
    infos = []
    w0, s0 = _grid_origin(ext, step)
    nx = int(math.ceil((tiles["east"] - tiles["west"]) / step)) + 3
    ny = int(math.ceil((tiles["north"] - tiles["south"]) / step)) + 3
    for j in range(ny):
        for i in range(nx):
            if (i, j) in skip:
                continue
            b = _b((w0 + i) * step, (w0 + i + 1) * step,
                   (s0 + j) * step, (s0 + j + 1) * step)
            infos.append(_info(f"/dem/{i:02d}_{j:02d}.tif", b, halo=halo))
    return infos


def _grid_origin(ext=_EXT, step=0.05):
    tiles = mct._snap_bbox(ext, [2])
    return (math.floor(tiles["west"] / step + 1e-9) - 1,
            math.floor(tiles["south"] / step + 1e-9) - 1)


def _cell_in(tb, step=0.05):
    """The ``(i, j)`` of :func:`_grid_infos` holding a point inside *tb*."""
    w0, s0 = _grid_origin(step=step)
    return (int(math.floor((tb["west"] + 0.01) / step)) - w0,
            int(math.floor((tb["south"] + 0.01) / step)) - s0)


class MatchedOncePerRun(unittest.TestCase):

    def test_map_converter_jobs_reuse_the_fingerprint_match(self):
        expected = mct._enumerate_tiles(mct._snap_bbox(_EXT, [2]), [2])
        ent = {"kind": "files", "infos": _grid_infos()}
        with tempfile.TemporaryDirectory() as out, \
             mock.patch.object(ta, "_scan_contributors",
                               wraps=ta._scan_contributors) as scan:
            fps = mct._stack_tile_fingerprints(expected, [ent], None)
            jobs, _t = mct._build_tile_jobs(expected, out, True, [ent], None,
                                            out)
        self.assertEqual(scan.call_count, len(expected),
                         "a tile was matched against the files twice")
        self.assertEqual(len(fps), len(expected))
        # The same sources the old full scan gives.
        plain = {"kind": "files", "infos": ent["infos"]}
        for job, (_r, t) in zip(jobs, expected):
            tb = mct._tile_bbox_of(2, t)
            want, _ = ta.build_tile_sources(plain["infos"], tb, out, "x",
                                            t["exact_res_m"])
            self.assertEqual(job["sources"], want)

    def _folder_run(self, tmp, infos, fake_converter):
        with mock.patch.object(ta, "get_cache_dir", return_value=tmp), \
             mock.patch.object(ta, "_gather_source_infos",
                               return_value=(infos, False)), \
             mock.patch.object(ta, "classify_raster_layer",
                               return_value="dem"), \
             mock.patch.object(ta, "footprint_index",
                               wraps=ta.footprint_index) as built, \
             mock.patch.object(ta, "tile_source_fingerprint",
                               wraps=ta.tile_source_fingerprint) as fp, \
             mock.patch.object(ta, "build_tile_sources",
                               wraps=ta.build_tile_sources) as src, \
             mock.patch.object(ta, "preflight_ingest_jobs"), \
             mock.patch.object(ta, "_run_converter",
                               side_effect=fake_converter):
            layer = mock.Mock()
            layer.source.return_value = os.path.join(tmp, "dem.tif")
            bm_ = mock.Mock()
            bm_.find_binary.return_value = "aether_converter"
            with self.assertRaises(_Stop):
                ta.prepare_terrain(layer, 47.17, 8.22, 3.0, 30, bm_,
                                   terrain_dir=None)
        return built, fp, src

    def test_site_analysis_builds_one_index_for_fingerprints_and_jobs(self):
        def stop(*_a, **_k):
            raise _Stop()
        with tempfile.TemporaryDirectory() as tmp:
            infos = _grid_infos(_b(8.15, 8.3, 47.12, 47.22), step=0.1,
                                halo=0.001)
            built, fp, src = self._folder_run(tmp, infos, stop)
        self.assertEqual(built.call_count, 1)
        self.assertTrue(fp.call_args_list and src.call_args_list)
        index = fp.call_args_list[0].kwargs["index"]
        self.assertIsInstance(index, ta.FootprintIndex)
        for call in fp.call_args_list + src.call_args_list:
            self.assertIs(call.kwargs["index"], index)

    def test_profile_reads_and_indexes_the_source_once(self):
        calls = []

        def fake_prepare(*_a, **kw):
            calls.append(kw)
            return "view"

        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "dem")
            os.makedirs(folder)
            infos = _grid_infos()
            with mock.patch.object(ta, "get_cache_dir", return_value=tmp), \
                 mock.patch.object(ta, "_gather_source_infos",
                                   return_value=(infos, False)) as gather, \
                 mock.patch.object(ta, "footprint_index",
                                   wraps=ta.footprint_index) as built, \
                 mock.patch.object(ta, "prepare_terrain",
                                   side_effect=fake_prepare), \
                 mock.patch.object(ta, "_sync_view", return_value=[]), \
                 mock.patch.object(ta, "_record_view_dir"):
                from waveshed.core import range_profile as rp
                ta.prepare_terrain_profile(
                    None, 47.17, 8.22, rp.adaptive_bands(5, 20),
                    binary_manager=None, terrain_dir=folder)
        self.assertGreater(len(calls), 1)
        self.assertEqual(gather.call_count, 1)
        self.assertEqual(built.call_count, 1)
        first = calls[0]["source_infos"]
        self.assertIs(first[0], infos)
        self.assertIs(first[2].infos, infos)
        for kw in calls:
            self.assertIs(kw["source_infos"], first)

    def test_a_handed_in_index_is_used_for_the_band(self):
        def stop(*_a, **_k):
            raise _Stop()
        infos = _grid_infos(_b(8.15, 8.3, 47.12, 47.22), step=0.1,
                            halo=0.001)
        index = ta.FootprintIndex(infos)
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(ta, "get_cache_dir", return_value=tmp), \
             mock.patch.object(ta, "_gather_source_infos") as gather, \
             mock.patch.object(ta, "classify_raster_layer",
                               return_value="dem"), \
             mock.patch.object(ta, "build_tile_sources",
                               wraps=ta.build_tile_sources) as src, \
             mock.patch.object(ta, "preflight_ingest_jobs"), \
             mock.patch.object(ta, "_run_converter", side_effect=stop):
            layer = mock.Mock()
            layer.source.return_value = os.path.join(tmp, "dem.tif")
            bm_ = mock.Mock()
            bm_.find_binary.return_value = "aether_converter"
            with self.assertRaises(_Stop):
                ta.prepare_terrain(layer, 47.17, 8.22, 3.0, 30, bm_,
                                   source_infos=(infos, False, index))
        gather.assert_not_called()
        self.assertTrue(src.call_args_list)
        for call in src.call_args_list:
            self.assertIs(call.args[0], infos)
            self.assertIs(call.kwargs["index"], index)


class _Stop(Exception):
    pass


# ---------------------------------------------------------------------------
# preflight_ingest_jobs: each distinct path once
# ---------------------------------------------------------------------------

class PreflightDedupe(unittest.TestCase):

    def test_each_distinct_path_is_checked_once(self):
        with tempfile.TemporaryDirectory() as root:
            srcs = []
            for k in range(3):
                p = os.path.join(root, f"s{k}.tif")
                open(p, "w").close()
                srcs.append(p)
            jobs = [{"output_path": os.path.join(root, "out", f"t{k}.abt"),
                     "sources": [{"path": srcs[k % 3]},
                                 {"path": srcs[(k + 1) % 3]}]}
                    for k in range(12)]
            with mock.patch.object(ta.os.path, "exists",
                                   wraps=os.path.exists) as exists, \
                 mock.patch.object(ta, "ensure_engine_dirs",
                                   wraps=ta.ensure_engine_dirs) as dirs:
                ta.preflight_ingest_jobs(jobs)
            checked = [c.args[0] for c in exists.call_args_list
                       if c.args[0].endswith(".tif")]
            self.assertEqual(sorted(checked), sorted(srcs))
            self.assertEqual(dirs.call_count, 1)

    def test_the_first_missing_in_job_order_is_named(self):
        with tempfile.TemporaryDirectory() as root:
            ok = os.path.join(root, "ok.tif")
            open(ok, "w").close()
            jobs = [{"output_path": os.path.join(root, "t0.abt"),
                     "sources": [{"path": ok}, {"path": ok}]},
                    {"output_path": os.path.join(root, "t1.abt"),
                     "sources": [{"path": ok},
                                 {"path": os.path.join(root, "gone1.tif")}]},
                    {"output_path": os.path.join(root, "t2.abt"),
                     "sources": [{"path": os.path.join(root, "gone2.tif")}]}]
            with self.assertRaises(RuntimeError) as caught:
                ta.preflight_ingest_jobs(jobs)
        self.assertIn("gone1.tif", str(caught.exception))


# ---------------------------------------------------------------------------
# Pool check: one listing, _tile_ready's rule
# ---------------------------------------------------------------------------

_XYZ = ("type=xyz&url=https%3A//example.com/%7Bz%7D/%7Bx%7D/%7By%7D.png"
        "&zmax=15")


class PoolCheckIsOneListing(unittest.TestCase):

    def _pool(self, root):
        pool = ta._pool_dir(_XYZ)
        os.makedirs(pool, exist_ok=True)
        specs = [{"filename": f"t{k}.abt", "size_px": 64} for k in range(6)]
        whole = ta._abt_whole_bytes(64)
        _write_pool_abt(os.path.join(pool, "t0.abt"), size=64)    # whole
        _write_pool_abt(os.path.join(pool, "t1.abt"), size=64)    # short
        with open(os.path.join(pool, "t1.abt"), "r+b") as fh:
            fh.truncate(whole - 1)
        _write_pool_abt(os.path.join(pool, "t2.abt"), size=64)    # flagged
        open(os.path.join(pool, "t2.abt" + ta._REBUILD_SUFFIX), "w").close()
        _write_pool_abt(os.path.join(pool, "t3.abt"), size=64)    # longer
        with open(os.path.join(pool, "t3.abt"), "ab") as fh:
            fh.write(b"\0" * 10)
        os.makedirs(os.path.join(pool, "t4.abt"))                 # a dir
        # t5 missing
        return pool, specs

    def test_listing_rule_is_tile_ready(self):
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(ta, "get_cache_dir", return_value=root):
            pool, specs = self._pool(root)
            self.assertEqual(os.path.getsize(os.path.join(pool, "t0.abt")),
                             ta._abt_whole_bytes(64))
            listing = ta._pool_listing(pool)
            got = [ta._listed_tile_ready(listing, s, pool) for s in specs]
            want = [ta._tile_ready(pool, s["filename"]) for s in specs]
        self.assertEqual(got, want)
        self.assertEqual(got, [True, False, False, True, False, False])

    def test_estimate_lists_once_and_reads_no_header(self):
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(ta, "get_cache_dir", return_value=root):
            pool, specs = self._pool(root)
            # Whole, flagged, longer, a directory, missing — no header read.
            # (Only a file SHORTER than its spec's tile has its own header
            # read: test_a_short_tile_reads_its_own_header.)
            specs = [dict(s, ul_lat=47.0 + k * 0.1, ul_lon=8.0,
                          exact_res_m=30.0) for k, s in enumerate(specs)
                     if s["filename"] != "t1.abt"]
            missing = [s for s in specs
                       if not ta._tile_ready(pool, s["filename"])]
            want = ta.estimate_pool_download_units(_XYZ, missing,
                                                   pool_check=False)
            with mock.patch.object(ta.os, "scandir",
                                   wraps=os.scandir) as scan, \
                 mock.patch.object(ta, "_tile_ready",
                                   side_effect=AssertionError("per tile")), \
                 mock.patch.object(ta, "_tile_is_whole",
                                   side_effect=AssertionError("header")):
                got = ta.estimate_pool_download_units(_XYZ, specs,
                                                      pool_check=True)
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(got, want)
        self.assertGreater(got, 0)

    def test_a_short_tile_reads_its_own_header(self):
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(ta, "get_cache_dir", return_value=root):
            pool, specs = self._pool(root)
            listing = ta._pool_listing(pool)
            with mock.patch.object(ta, "_tile_is_whole",
                                   wraps=ta._tile_is_whole) as whole:
                ready = [ta._listed_tile_ready(listing, s, pool)
                         for s in specs]
            self.assertEqual(ready, [True, False, False, True, False, False])
            self.assertEqual(sorted(os.path.basename(c.args[0])
                                    for c in whole.call_args_list),
                             ["t1.abt"],
                             "only the short file had its header read")

    def test_missing_pool_counts_every_tile(self):
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(ta, "get_cache_dir", return_value=root):
            specs = [{"filename": "a.abt", "size_px": 64, "ul_lat": 47.0,
                      "ul_lon": 8.0, "exact_res_m": 30.0}]
            self.assertEqual(
                ta.estimate_pool_download_units(_XYZ, specs, pool_check=True),
                ta.estimate_pool_download_units(_XYZ, specs,
                                                pool_check=False))


# ---------------------------------------------------------------------------
# The download estimate: pool tiles where the files' rectangles leave ground
# ---------------------------------------------------------------------------

class PoolEstimateByRectangles(unittest.TestCase):
    """``_pool_estimate_tiles`` — geometry only (the pixels are the
    converter's ``[Tile]`` count in Phase 2): a tile counts for the pool
    unless the files above the first XYZ entry cover it plus its halo."""

    def setUp(self):
        self.expected = mct._enumerate_tiles(mct._snap_bbox(_EXT, [2]), [2])
        self.names = [t["filename"] for _r, t in self.expected]
        self.assertGreater(len(self.expected), 2)

    def _estimate(self, entries):
        return [t["filename"] for _r, t
                in mct._pool_estimate_tiles(self.expected, entries)]

    def test_fully_covered_tiles_need_no_pool(self):
        files = {"kind": "files", "infos": _grid_infos()}
        self.assertEqual(self._estimate([files, {"kind": "xyz",
                                                 "uri": _XYZ}]), [])

    def test_partially_covered_tiles_are_counted(self):
        # One 0.05 deg file missing inside the first tile's ground.
        tb = mct._tile_bbox_of(2, self.expected[0][1])
        files = {"kind": "files", "infos": _grid_infos(skip={_cell_in(tb)})}
        self.assertEqual(self._estimate([files, {"kind": "xyz",
                                                 "uri": _XYZ}]),
                         [self.names[0]])

    def test_the_halo_must_be_covered_too(self):
        # Files exactly on the tiles' outer edge: the tiles themselves are
        # covered, the halo past the run's outer edge is not.
        ext = mct._snap_bbox(_EXT, [2])
        outer = _b(ext["west"], ext["east"], ext["south"], ext["north"])
        files = {"kind": "files",
                 "infos": [_info("/dem/one.tif", outer, halo=7.2e-5)]}
        got = self._estimate([files, {"kind": "xyz", "uri": _XYZ}])
        border = [n for (_r, t), n in zip(self.expected, self.names)
                  if mct._tile_bbox_of(2, t)["west"] <= outer["west"] + 1e-9]
        self.assertTrue(border)
        for n in border:
            self.assertIn(n, got)

    def test_no_folder_counts_every_tile(self):
        self.assertEqual(self._estimate([{"kind": "xyz", "uri": _XYZ}]),
                         self.names)

    def test_a_lower_priority_folder_covers_nothing(self):
        files = {"kind": "files", "infos": _grid_infos()}
        self.assertEqual(self._estimate([{"kind": "xyz", "uri": _XYZ},
                                         files]), self.names)

    def test_no_xyz_entry_needs_no_pool(self):
        self.assertEqual(self._estimate(
            [{"kind": "files", "infos": _grid_infos()}]), [])

    def test_rotated_files_and_unknown_crs_prove_nothing(self):
        rot = [dict(i, rotated=True) for i in _grid_infos()]
        nocrs = [dict(i, crs_authid="", crs_wkt="") for i in _grid_infos()]
        for infos in (rot, nocrs):
            self.assertEqual(self._estimate(
                [{"kind": "files", "infos": infos},
                 {"kind": "xyz", "uri": _XYZ}]), self.names)

    def test_a_tile_without_a_pool_tile_has_no_pool_source(self):
        # The download gets only the tiles still pending; the other tiles'
        # jobs carry no pool source and pass the preflight.
        xyz = {"kind": "xyz", "uri": _XYZ}
        files = {"kind": "files", "infos": _grid_infos()}
        asked = []

        def fake_pool(uri, specs, res, **_kw):
            asked.extend(t["filename"] for t in specs)
            return {t["filename"]: f"/pool/{t['filename']}" for t in specs}

        with mock.patch.object(ta, "ensure_pool_tiles",
                               side_effect=fake_pool):
            mct._download_xyz_entries([xyz], self.expected[:1])
        self.assertEqual(asked, [self.names[0]])
        with tempfile.TemporaryDirectory() as out:
            jobs, _t = mct._build_tile_jobs(self.expected, out, True,
                                            [files, xyz], None, out)
        pool_refs = {os.path.basename(j["output_path"]):
                     [s["path"] for s in j["sources"]
                      if s["path"].startswith(os.path.abspath("/pool"))]
                     for j in jobs}
        self.assertEqual(pool_refs[self.names[0]],
                         [os.path.abspath(f"/pool/{self.names[0]}")])
        for n in self.names[1:]:
            self.assertEqual(pool_refs[n], [], "a covered tile got the pool")


@unittest.skipUnless(HAVE_GDAL, "needs the real GDAL")
class PoolEstimateLv95(unittest.TestCase):
    """1 km LV95 files (exact native squares, rotated in WGS84) — the union
    test runs in the files' own CRS."""

    def _srs(self):
        wgs, lv = OSR.SpatialReference(), OSR.SpatialReference()
        wgs.ImportFromEPSG(4326)
        lv.ImportFromEPSG(2056)
        for s in (wgs, lv):
            s.SetAxisMappingStrategy(OSR.OAMS_TRADITIONAL_GIS_ORDER)
        return wgs, lv

    def _km_range(self, tb):
        """The LV95 km cells over the tile's ground, plus two all round."""
        wgs, lv = self._srs()
        to_lv = OSR.CoordinateTransformation(wgs, lv)
        pts = to_lv.TransformPoints([(tb["west"], tb["south"]),
                                     (tb["east"], tb["south"]),
                                     (tb["east"], tb["north"]),
                                     (tb["west"], tb["north"])])
        es = [p[0] / 1e3 for p in pts]
        ns = [p[1] / 1e3 for p in pts]
        return (range(math.floor(min(es)) - 2, math.ceil(max(es)) + 2),
                range(math.floor(min(ns)) - 2, math.ceil(max(ns)) + 2))

    def _infos(self, es, ns, skip=()):
        wgs, lv = self._srs()
        to_ll = OSR.CoordinateTransformation(lv, wgs)
        wkt = lv.ExportToWkt()
        infos = []
        for e in es:
            for n in ns:
                if (e, n) in skip:
                    continue
                x0, y0, x1, y1 = e * 1e3, n * 1e3, (e + 1) * 1e3, (n + 1) * 1e3
                pts = to_ll.TransformPoints([(x0, y0), (x1, y0), (x1, y1),
                                             (x0, y1)])
                entry = {"crs_authid": "EPSG:2056", "crs_wkt": wkt,
                         "px_m": 2.0,
                         "native_bounds": _b(x0, x1, y0, y1),
                         "wgs84_bounds": _b(min(p[0] for p in pts),
                                            max(p[0] for p in pts),
                                            min(p[1] for p in pts),
                                            max(p[1] for p in pts)),
                         "size": 1, "mtime_ns": 1, "driver": "GTiff",
                         "compression": "LZW", "dtype": "Float32",
                         "bands": 1}
                infos.append(ti.entry_info(f"/lv95/{e}_{n}.tif", entry))
        return infos

    def test_covered_inside_not_at_a_hole_nor_past_the_edge(self):
        def holds(t):
            b = mct._tile_bbox_of(2, t)
            return (b["west"] < 8.62 < b["east"]
                    and b["south"] < 47.22 < b["north"])
        expected = [(r, t) for r, t in mct._enumerate_tiles(mct._snap_bbox(
            {"south": 47.21, "north": 47.24, "west": 8.61, "east": 8.64},
            [2]), [2]) if holds(t)]
        self.assertEqual(len(expected), 1)
        name = expected[0][1]["filename"]
        tb = mct._tile_bbox_of(2, expected[0][1])
        with real_gdal():
            es, ns = self._km_range(tb)
            mid = (es[len(es) // 2], ns[len(ns) // 2])
            runs = {
                "full": self._infos(es, ns),
                "hole": self._infos(es, ns, skip={mid}),
                # Two km short in the east: the tile's own ground is cut.
                "short": self._infos(range(es.start, es.stop - 4), ns),
            }
            pool = {}
            for label, infos in runs.items():
                pool[label] = {t["filename"] for _r, t in
                               mct._pool_estimate_tiles(
                                   expected, [{"kind": "files",
                                               "infos": infos},
                                              {"kind": "xyz", "uri": _XYZ}])}
        self.assertEqual(pool, {"full": set(), "hole": {name},
                                "short": {name}})


# ---------------------------------------------------------------------------
# One listing for the download and the Site Analysis stale check
# ---------------------------------------------------------------------------

def _sparse_abt(path, size_px):
    """A whole-length .abt (header + size x stride, sparse) for *size_px*."""
    import struct
    stride = (size_px * 2 + 255) & ~255
    with open(path, "wb") as fh:
        fh.write(b"AETH" + struct.pack("<HH", 1, size_px)
                 + struct.pack("<dddd", 47.0, 8.0, 1e-4, 1e-4)
                 + struct.pack("<hH", 0, stride))
        fh.truncate(ta._abt_whole_bytes(size_px))


class PoolListingEverywhere(unittest.TestCase):

    def _no_header_reads(self):
        boom = AssertionError("per-tile header read")
        return (mock.patch.object(ta, "_tile_is_whole", side_effect=boom),
                mock.patch.object(ta, "_tile_ready", side_effect=boom))

    def test_ensure_pool_tiles_lists_instead_of_reading_headers(self):
        specs = [{"filename": f"t{k}.abt", "size_px": 64, "ul_lat": 47.0,
                  "ul_lon": 8.0 + k * 0.1, "exact_res_m": 30.0}
                 for k in range(5)]
        asked = []

        def fake_download(uri, pool_dir, _x, res, todo, *_a, **_kw):
            asked.extend(t["filename"] for t in todo)
            for t in todo:
                _sparse_abt(os.path.join(pool_dir, t["filename"]), 64)
            return mock.Mock(detail="")

        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(ta, "get_cache_dir", return_value=root):
            pool = ta._pool_dir(_XYZ)
            os.makedirs(pool, exist_ok=True)
            for t in specs[:3]:
                _sparse_abt(os.path.join(pool, t["filename"]), 64)
            a, b = self._no_header_reads()
            with a, b, mock.patch.object(ta, "_try_rust_download",
                                         side_effect=fake_download), \
                 mock.patch.object(ta.os, "scandir",
                                   wraps=os.scandir) as scan:
                mapping = ta.ensure_pool_tiles(_XYZ, specs, 30,
                                               binary_manager=mock.Mock())
        self.assertEqual(asked, ["t3.abt", "t4.abt"])
        self.assertEqual(sorted(mapping), [t["filename"] for t in specs])
        self.assertEqual(scan.call_count, 2, "one listing before, one after")

    def test_site_analysis_stale_check_lists_instead_of_reading_headers(self):
        lat, lon, rng, res = 47.17, 8.22, 3.0, 30
        infos = _grid_infos(_b(8.15, 8.3, 47.12, 47.22), step=0.1,
                            halo=0.001)
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(ta, "get_cache_dir", return_value=root), \
             mock.patch.object(ta, "_gather_source_infos",
                               return_value=(infos, False)), \
             mock.patch.object(ta, "classify_raster_layer",
                               return_value="dem"), \
             mock.patch.object(ta, "_sync_view", return_value=[]), \
             mock.patch.object(ta, "_run_converter") as conv:
            layer = mock.Mock()
            layer.source.return_value = os.path.join(root, "dem.tif")
            bbox = ta._compute_sector_bbox(lat, lon, rng, 0.0, 360.0)
            subtiles = ta._compute_subtiles(bbox, res, lat, lon, rng,
                                            0.0, 360.0)
            pool = ta._pool_dir(layer.source(),
                                ta.buildings_identity(None, False))
            os.makedirs(pool, exist_ok=True)
            fidx = ta.FootprintIndex(infos)
            for t in subtiles:
                _sparse_abt(os.path.join(pool, t["filename"]), t["size_px"])
                with open(os.path.join(pool, t["filename"] + ".src"),
                          "w") as fh:
                    fh.write(ta.tile_source_fingerprint(
                        infos, ta._subtile_bbox(t), t["exact_res_m"],
                        index=fidx))
            a, b = self._no_header_reads()
            with a, b:
                ta.prepare_terrain(layer, lat, lon, rng, res, mock.Mock())
        conv.assert_not_called()


# ---------------------------------------------------------------------------
# The worker: index once, the status says what it does, the skip is logged
# ---------------------------------------------------------------------------

_RAW = {"south": 47.1, "north": 47.25, "west": 8.1, "east": 8.35}


class WorkerMatchingStatus(unittest.TestCase):

    def _run(self, tmp, resolved, layers):
        worker = _MapConverterWorker(layers, os.path.join(tmp, "out"), [2],
                                     [e.source_path for e in layers],
                                     overwrite=True)
        status, logs, built = [], [], {}
        worker.status.connect(status.append)
        worker.log_line.connect(logs.append)

        def fake_build(expected, out_dir, overwrite, entries, *_a, **_kw):
            built.setdefault("entries", entries)
            jobs = [{"output_path": os.path.join(out_dir, t["filename"]),
                     "size_px": t["size_px"], "sources": []}
                    for _r, t in expected]
            return jobs, []

        def fake_runner(exe, args, on_line, on_start=None, env=None):
            # Every tile complete from the files alone.
            for job in json.load(open(args[-1], encoding="utf-8")):
                with open(job["output_path"], "wb") as fh:
                    fh.truncate(ta._abt_whole_bytes(job["size_px"]))
                px = job["size_px"] ** 2
                on_line(f"[Tile] {os.path.basename(job['output_path'])} "
                        f"covered={px}/{px}")
            return 0

        def fake_pool(uri, specs, res, **_kw):
            built.setdefault("asked", []).extend(t["filename"] for t in specs)
            return {t["filename"]: f"/pool/{t['filename']}" for t in specs}

        with mock.patch.object(_MapConverterWorker, "_resolve_raster",
                               side_effect=lambda e: resolved[
                                   e["source_path"]]), \
             mock.patch.object(_MapConverterWorker, "_resolve_buildings",
                               return_value=None), \
             mock.patch.object(mct, "find_binary", return_value="/bin/conv"), \
             mock.patch.object(mct, "_plan_cross_check"), \
             mock.patch.object(mct, "_converter_reports_tiles",
                               return_value=True), \
             mock.patch.object(ta, "preflight_ingest_jobs"), \
             mock.patch.object(ta, "get_cache_dir", return_value=tmp), \
             mock.patch.object(ta, "ensure_pool_tiles",
                               side_effect=fake_pool), \
             mock.patch.object(ta, "run_converter_streaming",
                               side_effect=fake_runner), \
             mock.patch.object(mct, "_build_tile_jobs",
                               side_effect=fake_build):
            worker.run()
        return status, logs, built

    def test_status_index_and_phase2_log(self):
        infos = _grid_infos()
        resolved = {"/dem": {"kind": "files", "infos": infos},
                    "xyz": {"kind": "xyz", "uri": _XYZ, "extent": None}}
        layers = [_LayerEntry(layer_type="raster", source_path="/dem",
                              extent=dict(_RAW), target_resolutions=[2]),
                  _LayerEntry(layer_type="raster", source_path="xyz",
                              extent=dict(_RAW), target_resolutions=[2])]
        with tempfile.TemporaryDirectory() as tmp:
            status, logs, built = self._run(tmp, resolved, layers)
        # The worker's grid (an unknown converter: the bare snap, 12 tiles)
        # without the tiles outside every source (2026-11-07): its stray
        # row and column lie wholly outside _RAW — 6 tiles.
        n_tiles = len(mct._tiles_within_sources(mct._enumerate_tiles(
            mct._snap_bbox(_RAW, [2]), [2]), layers)[0])
        self.assertEqual(n_tiles, 6)
        self.assertIn(f"Matching {len(infos):,} files to {n_tiles:,} "
                      f"tiles...", status)
        self.assertLess(status.index(f"Matching {len(infos):,} files to "
                                     f"{n_tiles:,} tiles..."),
                        status.index(f"Converting {n_tiles} tiles from "
                                     f"local files..."))
        self.assertEqual(status[-1], "Done.")
        files_ent = built["entries"][0]
        self.assertIsInstance(files_ent["index"], ta.FootprintIndex)
        self.assertIs(files_ent["index"].infos, infos)
        self.assertNotIn("asked", built, "complete tiles were downloaded")
        self.assertIn(f"Phase 2: {n_tiles} tiles from local files — "
                      f"{n_tiles} complete, 0 need the pool", logs)


if __name__ == "__main__":
    unittest.main()
