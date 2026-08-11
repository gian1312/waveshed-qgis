"""Unit tests for terrain_adapter — sector bbox, subtile filtering, estimates.

These tests do NOT require QGIS or AETHER binaries.  QGIS stubs are
provided by conftest.py.
"""

# Bootstrap stubs before any plugin import.
import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import hashlib
import json
import math
import os
import struct
import tempfile
import unittest
import urllib.parse
from unittest import mock

try:
    import numpy as np
    _HAVE_NUMPY = True
except Exception:  # noqa: BLE001 — numpy is optional here (only the .abt
    # writer below needs it), and a wheel built for another platform raises
    # AttributeError rather than ImportError. Never let that abort collection.
    _HAVE_NUMPY = False

import waveshed.core.terrain_adapter as ta
from waveshed.core.terrain_adapter import (
    _compute_sector_bbox,
    _compute_subtiles,
    _estimate_abt_disk_mb,
    _cache_key,
    _parse_download_completeness,
    _subtile_degrees,
    _tile_params,
    _warn_if_bbox_exceeds_source,
    reset_terrain_warnings,
)


def _abt_row_stride(size_px: int) -> int:
    """Mirror the converter's stride math: size_px*2 aligned up to 256."""
    return (size_px * 2 + 255) & ~255


class TestSubtileStrideFitsU16(unittest.TestCase):
    """Tile extent comes from ABT_EXTENT_DEG, and the stride must fit a u16.

    The .abt writer stores row_stride as a u16, so a tile whose size_px runs
    past ~32000 wraps and shears the terrain into bands.
    """

    def test_extent_comes_from_the_shared_table(self):
        # One ladder drives both terrain paths — this one and the Map
        # Converter tab, which aliases the same dict.
        for res, extent in ta.ABT_EXTENT_DEG.items():
            self.assertEqual(_subtile_degrees(res), extent, f"res={res}")

    def test_fine_resolutions_stay_at_or_below_quarter_degree(self):
        # Tile extent is an engine constraint, not just a file-size one: a
        # 1 deg tile at 5 m is 22224 px -> 0.99 GB per terrain-atlas layer, and
        # a site near a tile corner needs four of them, which blows the
        # solver's ~3.86 GB single-allocation ceiling and rejects the job.
        for res in (2, 5, 10):
            self.assertLessEqual(_subtile_degrees(res), 0.25, f"res={res}")

    def test_off_table_resolution_takes_the_next_entry_up(self):
        # job_builder only offers the table's keys, but the engine accepts any
        # resolution >= 0.1 m, so the ladder must still answer for the rest.
        self.assertEqual(_subtile_degrees(3), ta.ABT_EXTENT_DEG[5])
        self.assertEqual(_subtile_degrees(500), ta.ABT_EXTENT_DEG[250])

    def test_stride_fits_u16_for_all_valid_resolutions(self):
        bbox = {"north": 47.6, "south": 47.0, "east": 8.6, "west": 8.0}
        for res in (2, 5, 10, 30):
            tiles = _compute_subtiles(bbox, res)
            self.assertTrue(tiles, f"no tiles for res={res}")
            for t in tiles:
                stride = _abt_row_stride(t["size_px"])
                self.assertLess(
                    stride, 65536,
                    f"res={res} size_px={t['size_px']} stride={stride} "
                    f"overflows u16",
                )


class TestTileParamsMatchPrepareData(unittest.TestCase):
    """_tile_params must reproduce prepare_data.py's calc_size / exact_res / ul."""

    @staticmethod
    def _prepare_data_formula(sub, lat, lon, res):
        # Verbatim from python/drivers/prepare_data.py:349-358.
        target_deg_per_px = res / 111111.0
        calc_size = int(round(sub / target_deg_per_px))
        calc_size = (calc_size + 3) // 4 * 4
        exact_res_m = (sub / calc_size) * 111111.0
        return calc_size, exact_res_m, lat + sub, lon

    def test_matches_reference_for_all_sub_and_res(self):
        for sub in (0.1, 0.5, 1.0):
            for res in (2, 5, 10, 30):
                cs, exact, ul_lat, ul_lon = self._prepare_data_formula(
                    sub, 47.0, 8.0, res)
                got = _tile_params(sub, 47.0, 8.0, res)
                self.assertEqual(got["size_px"], cs, f"sub={sub} res={res}")
                self.assertAlmostEqual(got["exact_res_m"], exact, places=6)
                self.assertAlmostEqual(got["ul_lat"], ul_lat, places=6)
                self.assertAlmostEqual(got["ul_lon"], ul_lon, places=6)


class TestComputeSectorBbox(unittest.TestCase):
    """Tests for _compute_sector_bbox."""

    def test_full_circle_symmetric(self):
        bb = _compute_sector_bbox(47.0, 8.0, 100.0, 0.0, 360.0)
        self.assertAlmostEqual(bb["north"] - 47.0, 47.0 - bb["south"], places=2)
        self.assertAlmostEqual(bb["east"] - 8.0, 8.0 - bb["west"], places=2)

    def test_full_circle_range(self):
        bb = _compute_sector_bbox(47.0, 8.0, 100.0)
        lat_off = bb["north"] - 47.0
        self.assertAlmostEqual(lat_off, 100_000 / 6_371_000 * 180 / math.pi,
                               delta=0.02)
        lon_off = bb["east"] - 8.0
        self.assertGreater(lon_off, lat_off)

    def test_90_deg_sector_north(self):
        bb = _compute_sector_bbox(47.0, 8.0, 100.0, 315.0, 45.0)
        self.assertGreater(bb["south"], 46.5)
        self.assertGreater(bb["north"], 47.5)

    def test_90_deg_sector_east(self):
        bb = _compute_sector_bbox(47.0, 8.0, 100.0, 45.0, 135.0)
        self.assertGreater(bb["east"], 8.5)
        self.assertGreater(bb["west"], 7.5)

    def test_90_deg_sector_south(self):
        bb = _compute_sector_bbox(47.0, 8.0, 100.0, 135.0, 225.0)
        self.assertLess(bb["south"], 46.5)
        self.assertLess(bb["north"], 47.5)

    def test_wrapping_sector(self):
        bb = _compute_sector_bbox(47.0, 8.0, 50.0, 350.0, 10.0)
        self.assertGreater(bb["north"], 47.3)
        self.assertGreater(bb["south"], 46.9)

    def test_equator(self):
        bb = _compute_sector_bbox(0.0, 0.0, 100.0)
        self.assertAlmostEqual(bb["north"], bb["east"], delta=0.02)

    def test_high_latitude(self):
        bb = _compute_sector_bbox(70.0, 20.0, 100.0)
        lat_off = bb["north"] - 70.0
        lon_off = bb["east"] - 20.0
        self.assertGreater(lon_off / lat_off, 2.5)

    def test_southern_hemisphere(self):
        bb = _compute_sector_bbox(-34.0, 18.5, 50.0)
        self.assertLess(bb["south"], -34.0)
        self.assertGreater(bb["north"], -34.0)

    def test_margin_present(self):
        bb = _compute_sector_bbox(47.0, 8.0, 100.0)
        exact_lat = 100_000 / 6_371_000 * 180 / math.pi
        self.assertGreater(bb["north"] - 47.0, exact_lat)

    def test_zero_range(self):
        bb = _compute_sector_bbox(47.0, 8.0, 0.0)
        self.assertAlmostEqual(bb["north"], 47.0, places=2)
        self.assertAlmostEqual(bb["south"], 47.0, places=2)


class TestComputeSubtiles(unittest.TestCase):

    def _count(self, tx_lat=47.0, tx_lon=8.0, max_range_km=100.0,
               az_start=0.0, az_end=360.0, resolution_m=30):
        bbox = _compute_sector_bbox(tx_lat, tx_lon, max_range_km,
                                    az_start, az_end)
        return len(_compute_subtiles(bbox, resolution_m, tx_lat, tx_lon,
                                     max_range_km, az_start, az_end))

    def test_full_circle_100km(self):
        n = self._count(max_range_km=100.0)
        self.assertGreater(n, 4)
        self.assertLess(n, 30)

    def test_full_circle_500km_fewer_than_square(self):
        # Measure the enclosing rectangle in the same sub-tile grid the tiler
        # uses, so this stays a test of the sector filter rather than of the
        # tile extent.
        res = 30
        sub = _subtile_degrees(res)
        bbox = _compute_sector_bbox(47.0, 8.0, 500.0)
        lat_count = math.ceil(bbox["north"] / sub) - math.floor(bbox["south"] / sub)
        lon_count = math.ceil(bbox["east"] / sub) - math.floor(bbox["west"] / sub)
        square_count = lat_count * lon_count
        filtered = self._count(max_range_km=500.0, resolution_m=res)
        self.assertLess(filtered, square_count)

    def test_90_deg_sector_fewer_than_full(self):
        full = self._count(max_range_km=200.0)
        quarter = self._count(max_range_km=200.0, az_start=0.0, az_end=90.0)
        self.assertLess(quarter, full)
        self.assertLessEqual(quarter, full * 0.6)

    def test_subtile_has_correct_fields(self):
        bbox = _compute_sector_bbox(47.0, 8.0, 50.0)
        tiles = _compute_subtiles(bbox, 30, 47.0, 8.0, 50.0)
        self.assertGreater(len(tiles), 0)
        for key in ("ul_lat", "ul_lon", "size_px", "exact_res_m", "filename"):
            self.assertIn(key, tiles[0])

    def test_size_px_multiple_of_4(self):
        for res in [2, 5, 10, 30, 90]:
            bbox = _compute_sector_bbox(47.0, 8.0, 50.0)
            tiles = _compute_subtiles(bbox, res, 47.0, 8.0, 50.0)
            for t in tiles:
                self.assertEqual(t["size_px"] % 4, 0)

    def test_no_range_keeps_all(self):
        bbox = {"north": 49.0, "south": 47.0, "east": 10.0, "west": 8.0}
        a = _compute_subtiles(bbox, 30)
        b = _compute_subtiles(bbox, 30, tx_lat=48.0, tx_lon=9.0, max_range_km=0.0)
        self.assertEqual(len(a), len(b))

    def test_equator_crossing(self):
        bbox = _compute_sector_bbox(-0.5, 37.0, 100.0)
        tiles = _compute_subtiles(bbox, 30, -0.5, 37.0, 100.0)
        self.assertGreater(len(tiles), 0)
        lats = [t["ul_lat"] for t in tiles]
        self.assertTrue(any(l > 0 for l in lats))
        self.assertTrue(any(l <= 0 for l in lats))


class TestEstimateAbtDiskMb(unittest.TestCase):

    def test_30m_tile(self):
        mb = _estimate_abt_disk_mb([{"size_px": 3704}])
        self.assertGreater(mb, 20)
        self.assertLess(mb, 35)

    def test_10m_tile(self):
        mb = _estimate_abt_disk_mb([{"size_px": 11112}])
        self.assertGreater(mb, 200)
        self.assertLess(mb, 280)

    def test_empty(self):
        self.assertEqual(_estimate_abt_disk_mb([]), 0)

    def test_multiple(self):
        s = _estimate_abt_disk_mb([{"size_px": 3704}])
        d = _estimate_abt_disk_mb([{"size_px": 3704}, {"size_px": 3704}])
        self.assertAlmostEqual(d, s * 2, delta=1)


def _tiles(*names):
    """Minimal sub-tile dicts — _cache_key only reads the filename."""
    return [{"filename": n} for n in names]


class TestCacheKey(unittest.TestCase):
    """The key identifies a tile SET, not the request that produced it."""

    A = _tiles("tile_N48.00E8.00_30m.abt", "tile_N48.00E9.00_30m.abt")
    B = _tiles("tile_N49.00E8.00_30m.abt")

    def test_different_tile_sets(self):
        self.assertNotEqual(_cache_key("s", self.A, 30),
                            _cache_key("s", self.B, 30))

    def test_different_res(self):
        self.assertNotEqual(_cache_key("s", self.A, 10),
                            _cache_key("s", self.A, 30))

    def test_different_source(self):
        self.assertNotEqual(_cache_key("s", self.A, 30),
                            _cache_key("other", self.A, 30))

    def test_deterministic(self):
        self.assertEqual(_cache_key("s", self.A, 30),
                         _cache_key("s", self.A, 30))

    def test_tile_order_does_not_matter(self):
        # _compute_subtiles emits in grid order; a reordering is the same set.
        self.assertEqual(_cache_key("s", self.A, 30),
                         _cache_key("s", list(reversed(self.A)), 30))

    def test_subset_is_a_different_key(self):
        # A narrowed azimuth sector selects fewer tiles. It must NOT hit the
        # full-circle cache, or the run gets terrain with holes in it. This is
        # why the key needs no azimuth term of its own.
        self.assertNotEqual(_cache_key("s", self.A, 30),
                            _cache_key("s", self.A[:1], 30))

    def test_same_tiles_from_different_requests_share_a_key(self):
        # The whole point: two sites a few km apart, or 30 km vs 31 km at one
        # site, resolve to the same tiles and must reuse the same cache.
        # Hashing the raw bbox floats made that a guaranteed miss.
        near = _compute_sector_bbox(47.4, 8.5, 30.0)
        nudged = _compute_sector_bbox(47.4001, 8.5001, 31.0)
        t1 = _compute_subtiles(near, 30, 47.4, 8.5, 30.0)
        t2 = _compute_subtiles(nudged, 30, 47.4001, 8.5001, 31.0)
        self.assertEqual([t["filename"] for t in t1],
                         [t["filename"] for t in t2])
        self.assertEqual(_cache_key("s", t1, 30), _cache_key("s", t2, 30))

    def test_hex_digest(self):
        k = _cache_key("s", self.A, 30)
        self.assertEqual(len(k), 32)
        int(k, 16)


def _write_pool_abt(path, size=64, res_m=None, ul_lat=47.0, ul_lon=8.0):
    """A minimal complete .abt: 44-byte header plus size*stride body bytes.

    Deliberately numpy-free (unlike _write_abt below) so the pool tests run
    everywhere — they care about presence and length, not pixels.

    *res_m* sets the tile's ground resolution (degrees-per-pixel is derived
    from it); the default keeps the historical 1/size degrees per pixel.
    """
    stride = _abt_row_stride(size)
    px_deg = (res_m / 111_111.0) if res_m else (1.0 / size)
    hdr = (b"AETH" + struct.pack("<HH", 1, size)
           + struct.pack("<dddd", ul_lat, ul_lon, px_deg, px_deg)
           + struct.pack("<hH", 0, stride))
    with open(path, "wb") as fh:
        fh.write(hdr)
        fh.write(b"\0" * (size * stride))


_XYZ_SOURCE = ("type=xyz&url=https%3A//example.com/%7Bz%7D/%7Bx%7D/%7By%7D.png"
               "&zmax=15")


class TestTileCompleteness(unittest.TestCase):
    """Membership is per tile, and a short tile is not a member."""

    def test_whole_tile_is_ready(self):
        with tempfile.TemporaryDirectory() as d:
            _write_pool_abt(os.path.join(d, "a.abt"))
            self.assertTrue(ta._tile_ready(d, "a.abt"))

    def test_truncated_tile_is_not_ready(self):
        # A run killed mid-write leaves a valid header over missing rows. The
        # engine would read that as real terrain and the coverage would come
        # out holed, with no error anywhere.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.abt")
            _write_pool_abt(path)
            with open(path, "rb") as fh:
                head = fh.read(44 + 128)
            with open(path, "wb") as fh:
                fh.write(head)
            self.assertFalse(ta._tile_ready(d, "a.abt"))

    def test_missing_tile_is_not_ready(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(ta._tile_ready(d, "nope.abt"))

    def test_rebuild_flag_hides_a_whole_tile(self):
        with tempfile.TemporaryDirectory() as d:
            _write_pool_abt(os.path.join(d, "a.abt"))
            ta._set_rebuild_flags(d, ["a.abt"], {"a.abt"})
            self.assertFalse(ta._tile_ready(d, "a.abt"))
            ta._set_rebuild_flags(d, ["a.abt"])          # cleared on rebuild
            self.assertTrue(ta._tile_ready(d, "a.abt"))


class TestViewDirectory(unittest.TestCase):
    """The engine reads every .abt in the directory it is handed."""

    def test_view_holds_exactly_the_requested_tiles(self):
        # engines/coverage.rs builds relevant_tiles from the whole listing with
        # no bbox filter, so one stray tile enlarges the atlas and the output
        # grid. The view must be pruned to the run's own tiles.
        with tempfile.TemporaryDirectory() as pool, \
                tempfile.TemporaryDirectory() as view:
            for name in ("a.abt", "b.abt", "c.abt"):
                _write_pool_abt(os.path.join(pool, name))
            _write_pool_abt(os.path.join(view, "stale.abt"))
            gone = ta._sync_view(pool, view, ["a.abt", "b.abt"])
            self.assertEqual(gone, [])
            self.assertEqual(sorted(n for n in os.listdir(view)
                                    if n.endswith(".abt")),
                             ["a.abt", "b.abt"])

    def test_missing_pool_tile_is_reported(self):
        with tempfile.TemporaryDirectory() as pool, \
                tempfile.TemporaryDirectory() as view:
            _write_pool_abt(os.path.join(pool, "a.abt"))
            self.assertEqual(ta._sync_view(pool, view, ["a.abt", "b.abt"]),
                             ["b.abt"])


class TestMissingEngineFailsFast(unittest.TestCase):
    """A missing converter must stop the run before the expensive extract."""

    def test_prepare_terrain_raises_before_extracting(self):
        # The fast download path returns False when the converter is absent,
        # which used to drop the run onto the slow QGIS raster path for tens
        # of minutes before failing on the very same missing binary.
        layer = mock.Mock()
        layer.source.return_value = _XYZ_SOURCE
        bm = mock.Mock()
        bm.find_binary.side_effect = RuntimeError(
            "AETHER binary 'aether_converter' not found.")

        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(ta, "get_cache_dir", return_value=root), \
                 mock.patch.object(ta, "_extract_reproject") as extract:
                with self.assertRaises(RuntimeError) as caught:
                    ta.prepare_terrain(layer, 47.4, 8.5, 30.0, 30, bm)
        self.assertIn("aether_converter", str(caught.exception))
        extract.assert_not_called()

    def test_full_cache_hit_needs_no_engine(self):
        # Nothing to build means nothing to build it with.
        layer = mock.Mock()
        layer.source.return_value = _XYZ_SOURCE
        bm = mock.Mock()
        bm.find_binary.side_effect = RuntimeError("not found")

        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(ta, "get_cache_dir", return_value=root):
                pool = ta._pool_dir(_XYZ_SOURCE)
                os.makedirs(pool, exist_ok=True)
                bb = ta._compute_sector_bbox(47.4, 8.5, 30.0)
                for t in ta._compute_subtiles(bb, 30, 47.4, 8.5, 30.0):
                    _write_pool_abt(os.path.join(pool, t["filename"]))
                view = ta.prepare_terrain(layer, 47.4, 8.5, 30.0, 30, bm)
                self.assertTrue(os.path.isdir(view))

    def test_fast_path_logs_why_it_bailed(self):
        # Three different causes used to share one generic message.
        logged = []
        bm = mock.Mock()
        bm.find_binary.side_effect = RuntimeError("binary 'x' not found")
        with mock.patch.object(ta, "_log", side_effect=logged.append):
            ta._try_rust_download("provider=gdal&path=/x.tif", "/tmp/p",
                                  {"north": 1, "south": 0, "east": 1, "west": 0},
                                  30, [], bm)
            ta._try_rust_download("type=xyz&zmax=15", "/tmp/p",
                                  {"north": 1, "south": 0, "east": 1, "west": 0},
                                  30, [], bm)
            ta._try_rust_download(_XYZ_SOURCE, "/tmp/p",
                                  {"north": 1, "south": 0, "east": 1, "west": 0},
                                  30, [], bm)
        self.assertIn("not an XYZ tile service", logged[0])
        self.assertIn("no 'url'", logged[1])
        # The third bail-out (missing engine) is now preceded by the encoding
        # note for a URL that names no known service, so match on the tail.
        self.assertTrue(any("not found" in line for line in logged[2:]), logged)


class TestSourceIdentity(unittest.TestCase):
    """Two spellings of one endpoint must not mean two pools."""

    URL = "https%3A//s3.amazonaws.com/x/%7Bz%7D/%7Bx%7D/%7By%7D.png"

    def test_parameter_order_and_extras_do_not_matter(self):
        a = f"type=xyz&url={self.URL}&zmax=15&zmin=0"
        b = f"zmin=0&url={self.URL}&type=xyz&zmax=15"
        self.assertEqual(ta.source_identity(a), ta.source_identity(b))

    def test_zmax_is_part_of_identity(self):
        # zmax caps the zoom a tile can be built from, so it changes pixels.
        a = f"type=xyz&url={self.URL}&zmax=15"
        b = f"type=xyz&url={self.URL}&zmax=13"
        self.assertNotEqual(ta.source_identity(a), ta.source_identity(b))

    def test_different_services_differ(self):
        a = f"type=xyz&url={self.URL}&zmax=15"
        b = "type=xyz&url=https%3A//other.example/%7Bz%7D.png&zmax=15"
        self.assertNotEqual(ta.source_identity(a), ta.source_identity(b))

    def test_encoding_is_part_of_identity(self):
        a = f"type=xyz&url={self.URL}&zmax=15"
        b = f"type=xyz&url={self.URL}&zmax=15&interpretation=mapboxterrain"
        self.assertNotEqual(ta.source_identity(a), ta.source_identity(b))

    def test_non_xyz_source_passes_through(self):
        self.assertEqual(ta.source_identity("/data/dem.tif"), "/data/dem.tif")

    def test_pool_is_shared_across_uri_spellings(self):
        with mock.patch.object(ta, "get_cache_dir", return_value="/tmp/x"):
            a = ta._pool_dir(f"type=xyz&url={self.URL}&zmax=15&zmin=0")
            b = ta._pool_dir(f"zmin=0&zmax=15&url={self.URL}&type=xyz")
            self.assertEqual(a, b)


class TestTileZoom(unittest.TestCase):
    """Zoom must be a property of the tile, not of the request."""

    def test_finer_resolution_needs_deeper_zoom(self):
        self.assertGreater(ta.tile_zoom(47.0, 5, 20), ta.tile_zoom(47.0, 30, 20))

    def test_capped_at_service_maximum(self):
        self.assertEqual(ta.tile_zoom(47.0, 1, 14), 14)

    def test_higher_latitude_needs_less_zoom(self):
        # Tiles shrink in ground metres towards the poles, so the same target
        # resolution is reached at a shallower zoom.
        self.assertLessEqual(ta.tile_zoom(70.0, 10, 20), ta.tile_zoom(0.0, 10, 20))

    def test_same_tile_gets_one_zoom_regardless_of_request(self):
        # The bug this replaces: zoom came from the request's bbox centre, so
        # a tile shared by an equatorial-centred run and a polar-centred run
        # was built from different source data depending on who fetched first.
        tile = {"ul_lat": 47.25, "ul_lon": 8.5, "size_px": 5556,
                "exact_res_m": 5.0}
        lat = ta._tile_center_lat(tile)
        self.assertEqual(ta.tile_zoom(lat, tile["exact_res_m"], 15),
                         ta.tile_zoom(lat, tile["exact_res_m"], 15))
        self.assertLess(abs(lat - 47.25), 0.3)


class TestTerrainPlan(unittest.TestCase):
    """The pre-run estimate must price the pool, not the whole tile set."""

    def test_everything_pooled_costs_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(ta, "get_cache_dir", return_value=root):
                first = ta.terrain_plan(_XYZ_SOURCE, 47.4, 8.5, 30.0, 30)
                self.assertEqual(first["tiles_cached"], 0)
                self.assertGreater(first["download_mb"], 0)

                pool = ta._pool_dir(_XYZ_SOURCE)
                os.makedirs(pool, exist_ok=True)
                bb = ta._compute_sector_bbox(47.4, 8.5, 30.0)
                for t in ta._compute_subtiles(bb, 30, 47.4, 8.5, 30.0):
                    _write_pool_abt(os.path.join(pool, t["filename"]))

                after = ta.terrain_plan(_XYZ_SOURCE, 47.4, 8.5, 30.0, 30)
        self.assertEqual(after["tiles_missing"], 0)
        self.assertEqual(after["download_mb"], 0)
        self.assertEqual(after["tiles_cached"], after["tiles_total"])
        self.assertGreater(after["total_mb"], 0)   # still reports real size

    def test_buildings_price_separately(self):
        # Buildings are burned into the pixels, so a plain pool cannot pay for
        # a buildings run — the estimate must say so.
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(ta, "get_cache_dir", return_value=root):
                pool = ta._pool_dir(_XYZ_SOURCE)
                os.makedirs(pool, exist_ok=True)
                bb = ta._compute_sector_bbox(47.4, 8.5, 30.0)
                for t in ta._compute_subtiles(bb, 30, 47.4, 8.5, 30.0):
                    _write_pool_abt(os.path.join(pool, t["filename"]))
                with_b = ta.terrain_plan(
                    _XYZ_SOURCE, 47.4, 8.5, 30.0, 30,
                    buildings=ta.buildings_identity(osm_buildings=True))
        self.assertEqual(with_b["tiles_cached"], 0)

    def test_size_warning_mentions_what_is_reused(self):
        msg, _strong = ta.terrain_size_warning(60 * 1024, cached_mb=40 * 1024)
        self.assertIn("still needs", msg)
        self.assertIn("already cached", msg)


class TestBboxCoveredFraction(unittest.TestCase):

    NEED = {"north": 48.0, "south": 47.0, "east": 9.0, "west": 8.0}

    def test_full(self):
        self.assertEqual(ta.bbox_covered_fraction(
            {"north": 49.0, "south": 46.0, "east": 10.0, "west": 7.0},
            self.NEED), 1.0)

    def test_none(self):
        self.assertEqual(ta.bbox_covered_fraction(
            {"north": 60.0, "south": 59.0, "east": 20.0, "west": 19.0},
            self.NEED), 0.0)

    def test_quarter(self):
        self.assertAlmostEqual(ta.bbox_covered_fraction(
            {"north": 47.5, "south": 46.0, "east": 8.5, "west": 7.0},
            self.NEED), 0.25)


class TestPoolReuse(unittest.TestCase):
    """The pool exists so a second, smaller run fetches nothing."""

    def _run(self, layer, fetched, root, range_km):
        def fake_download(source, pool, bbox, res, subtiles, bm, pbf_dir=None):
            fetched.append([t["filename"] for t in subtiles])
            for t in subtiles:
                _write_pool_abt(os.path.join(pool, t["filename"]))
            return True

        with mock.patch.object(ta, "get_cache_dir", return_value=root), \
             mock.patch.object(ta, "_try_rust_download",
                               side_effect=fake_download):
            return ta.prepare_terrain(layer, 47.4, 8.5, range_km, 30,
                                      mock.Mock())

    def test_reducing_the_range_fetches_nothing(self):
        # The reported bug: every run re-downloaded, "even if we just reduced
        # the range". A key over a whole directory — even over its exact tile
        # set — cannot express this, because a smaller range is a SUBSET and
        # set equality calls a subset a miss.
        layer = mock.Mock()
        layer.source.return_value = _XYZ_SOURCE
        fetched = []
        with tempfile.TemporaryDirectory() as root:
            big = self._run(layer, fetched, root, 60.0)
            small = self._run(layer, fetched, root, 20.0)

            self.assertEqual(len(fetched), 1, "the 20 km run fetched again")
            self.assertTrue(fetched[0])
            self.assertNotEqual(big, small, "each run needs its own view dir")
            n_big = len([n for n in os.listdir(big) if n.endswith(".abt")])
            n_small = len([n for n in os.listdir(small) if n.endswith(".abt")])
            self.assertLess(n_small, n_big)

    def test_growing_the_range_fetches_only_the_new_tiles(self):
        layer = mock.Mock()
        layer.source.return_value = _XYZ_SOURCE
        fetched = []
        with tempfile.TemporaryDirectory() as root:
            self._run(layer, fetched, root, 20.0)
            self._run(layer, fetched, root, 60.0)
            self.assertEqual(len(fetched), 2)
            # Nothing from the first run may be fetched a second time.
            self.assertFalse(set(fetched[0]) & set(fetched[1]))


class TestDownloadCompleteness(unittest.TestCase):
    """Unfetchable source tiles must not poison good pooled tiles."""

    BBOX = {"north": 47.5, "south": 47.0, "east": 8.5, "west": 8.0}
    TILE = "tile_N48.00E8.00_30m.abt"

    def _download(self, pool_dir, ok, total, has_gaps):
        subtiles = [{"filename": self.TILE, "ul_lat": 48.0, "ul_lon": 8.0,
                     "size_px": 3704, "exact_res_m": 30.0}]
        bm = mock.Mock()
        bm.find_binary.return_value = "aether_converter"
        _write_pool_abt(os.path.join(pool_dir, self.TILE))
        with mock.patch.object(ta, "_run_converter_download_once",
                               return_value=(0, [f"[Stats] Tiles: {ok}/{total} OK"])), \
             mock.patch.object(ta, "_abt_has_gaps", return_value=has_gaps):
            return ta._try_rust_download(_XYZ_SOURCE, pool_dir, self.BBOX, 30,
                                         subtiles, bm)

    def test_missing_sources_without_gaps_is_a_complete_tile(self):
        # The converter reports a run with a handful of unfetchable source
        # tiles as "100.0% success"; those are usually permanent 404s (ocean,
        # past zmax) that no retry can fix. If no output tile has a hole, that
        # is a COMPLETE result. Treating it as a failure flagged the tiles for
        # rebuild on the very first run, so every later run re-fetched them
        # and landed here again.
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(self._download(d, ok=999, total=1000, has_gaps=False))
            self.assertTrue(ta._tile_ready(d, self.TILE))

    def test_real_gaps_flag_only_the_gapped_tile(self):
        # Gapped terrain beats redoing the whole preparation through the far
        # slower path in this same run, so it is returned — and flagged, so
        # the next run rebuilds that tile and keeps every other one.
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(self._download(d, ok=999, total=1000, has_gaps=True))
            self.assertFalse(ta._tile_ready(d, self.TILE))

    def test_complete_download_clears_a_stale_flag(self):
        with tempfile.TemporaryDirectory() as d:
            ta._set_rebuild_flags(d, [self.TILE], {self.TILE})
            self.assertTrue(self._download(d, ok=1000, total=1000, has_gaps=False))
            self.assertTrue(ta._tile_ready(d, self.TILE))


class TestBuildingsCacheIdentity(unittest.TestCase):
    """A pool keyed "with buildings" must never hold building-free tiles."""

    def test_failed_building_download_flags_the_tiles_for_rebuild(self):
        # Buildings are part of the pool identity, so if the download fails
        # and we build plain terrain anyway, the pool lies about its contents.
        # Left reusable, the next run would reuse those tiles, skip the
        # download entirely and produce building-free coverage in silence.
        from waveshed.core import openfreemap

        layer = mock.Mock()
        layer.source.return_value = _XYZ_SOURCE
        fetched = []

        def fake_download(source, pool, bbox, res, subtiles, bm, pbf_dir=None):
            fetched.append(len(subtiles))
            for t in subtiles:
                _write_pool_abt(os.path.join(pool, t["filename"]))
            return True

        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(ta, "get_cache_dir", return_value=root), \
                 mock.patch.object(openfreemap, "download_building_tiles",
                                   side_effect=RuntimeError("503 from OFM")), \
                 mock.patch.object(ta, "_try_rust_download",
                                   side_effect=fake_download):
                ta.prepare_terrain(layer, 47.4, 8.5, 30.0, 30, mock.Mock(),
                                   osm_buildings=True)
                # Second run must not reuse the building-free tiles.
                ta.prepare_terrain(layer, 47.4, 8.5, 30.0, 30, mock.Mock(),
                                   osm_buildings=True)
        self.assertEqual(len(fetched), 2, "building-free tiles were reused")


class TestSectorBboxMatchesAetherCore(unittest.TestCase):
    """Verify Python bbox matches the Rust aether_core formula."""

    def _reference(self, tx_lat, tx_lon, range_km, az_start, az_end):
        earth_r = 6_371_000.0
        pi_180 = math.pi / 180.0
        range_m = range_km * 1000.0
        cos_tx = math.cos(tx_lat * pi_180)
        rdl = range_m / (earth_r * pi_180)
        rdn = range_m / (earth_r * cos_tx * pi_180)

        def in_arc(a):
            return (a >= az_start and a <= az_end) if az_start <= az_end else (a >= az_start or a <= az_end)

        angles = [az_start, az_end]
        for c in [0.0, 90.0, 180.0, 270.0]:
            if in_arc(c):
                angles.append(c)

        min_dlat = max_dlat = min_dlon = max_dlon = 0.0
        for a in angles:
            rad = a * pi_180
            dlat = rdl * math.cos(rad)
            dlon = rdn * math.sin(rad)
            min_dlat = min(min_dlat, dlat); max_dlat = max(max_dlat, dlat)
            min_dlon = min(min_dlon, dlon); max_dlon = max(max_dlon, dlon)

        ml = rdl * 0.01; mn = rdn * 0.01
        return {"north": tx_lat+max_dlat+ml, "south": tx_lat+min_dlat-ml,
                "east": tx_lon+max_dlon+mn, "west": tx_lon+min_dlon-mn}

    def _check(self, *args):
        ref = self._reference(*args)
        got = _compute_sector_bbox(*args)
        for k in ("north", "south", "east", "west"):
            self.assertAlmostEqual(ref[k], got[k], places=6, msg=k)

    def test_full_circle(self):
        self._check(47.0, 8.0, 500.0, 0.0, 360.0)

    def test_quarter(self):
        self._check(47.0, 8.0, 200.0, 45.0, 135.0)

    def test_wrapping(self):
        self._check(47.0, 8.0, 100.0, 350.0, 10.0)


class TestParseDownloadCompleteness(unittest.TestCase):
    """Tests for parsing the converter's `[Stats] Tiles: X/Y OK` line."""

    def test_complete(self):
        lines = [
            "[Download] 100% (500/500) — 12.3 MB/s, 0 errors, 0 in-flight",
            "[Stats] Tiles: 500/500 OK (100.0% success)",
        ]
        self.assertEqual(_parse_download_completeness(lines), (500, 500))

    def test_incomplete(self):
        lines = [
            "[Stats] Tiles: 697340/697343 OK (100.0% success)",
            "[Stats] ERRORS (3): timeout x3",
        ]
        ok, total = _parse_download_completeness(lines)
        self.assertEqual((ok, total), (697340, 697343))
        self.assertLess(ok, total)  # -> triggers a retry

    def test_missing_stats_line(self):
        self.assertEqual(
            _parse_download_completeness(["[Download] Batch processing..."]),
            (-1, -1),
        )

    def test_last_line_wins(self):
        # A retry pass appends a second stats line; the latest one is authoritative.
        lines = [
            "[Stats] Tiles: 498/500 OK (99.6% success)",
            "[Stats] Tiles: 500/500 OK (100.0% success)",
        ]
        self.assertEqual(_parse_download_completeness(lines), (500, 500))


class TestExtentWarningOncePerRun(unittest.TestCase):
    """The 'range exceeds DEM extent' notice must fire once per run per source."""

    # DEM covering 8.0–8.5 E, 47.0–47.5 N.
    _EXTENT = {"wgs84Extent": {"coordinates": [[
        [8.0, 47.0], [8.5, 47.0], [8.5, 47.5], [8.0, 47.5], [8.0, 47.0]]]}}
    _BBOX_OUT = {"west": 7.0, "south": 47.0, "east": 8.5, "north": 47.5}
    _BBOX_IN = {"west": 8.1, "south": 47.1, "east": 8.4, "north": 47.4}

    @staticmethod
    def _warns(logs):
        return sum(1 for m in logs if "extends beyond the DEM coverage" in m)

    def test_dedup_per_source_and_reset(self):
        reset_terrain_warnings()
        logs = []
        with mock.patch.object(ta.gdal, "Info", create=True,
                               return_value=self._EXTENT), \
                mock.patch.object(ta, "_log", side_effect=logs.append):
            _warn_if_bbox_exceeds_source(None, self._BBOX_OUT, "srcA")
            _warn_if_bbox_exceeds_source(None, self._BBOX_OUT, "srcA")  # dedup
            self.assertEqual(self._warns(logs), 1)
            _warn_if_bbox_exceeds_source(None, self._BBOX_OUT, "srcB")  # new src
            self.assertEqual(self._warns(logs), 2)
        # A fresh run resets the dedup.
        reset_terrain_warnings()
        logs2 = []
        with mock.patch.object(ta.gdal, "Info", create=True,
                               return_value=self._EXTENT), \
                mock.patch.object(ta, "_log", side_effect=logs2.append):
            _warn_if_bbox_exceeds_source(None, self._BBOX_OUT, "srcA")
            self.assertEqual(self._warns(logs2), 1)

    def test_no_warning_when_inside_extent(self):
        reset_terrain_warnings()
        logs = []
        with mock.patch.object(ta.gdal, "Info", create=True,
                               return_value=self._EXTENT), \
                mock.patch.object(ta, "_log", side_effect=logs.append):
            _warn_if_bbox_exceeds_source(None, self._BBOX_IN, "srcA")
            self.assertEqual(self._warns(logs), 0)


def _write_abt(path, size=128, zero_block=None):
    """Write a minimal valid .abt (44-byte header + int16 rows padded to stride).

    zero_block = (row0, col0, n) zeroes an n×n region; else all pixels = 500 m.
    """
    stride = (size * 2 + 255) & ~255
    hdr = (b"AETH" + struct.pack("<HH", 1, size)
           + struct.pack("<dddd", 47.0, 8.0, 1.0 / size, 1.0 / size)
           + struct.pack("<hH", 0, stride))
    assert len(hdr) == 44, len(hdr)
    elev = np.full((size, size), 500, dtype=np.int16)
    if zero_block:
        r0, c0, n = zero_block
        elev[r0:r0 + n, c0:c0 + n] = 0
    body = np.zeros((size, stride), dtype=np.uint8)
    body[:, : size * 2] = np.ascontiguousarray(elev).view(np.uint8).reshape(
        size, size * 2)
    with open(path, "wb") as fh:
        fh.write(hdr)
        fh.write(body.tobytes())


@unittest.skipUnless(_HAVE_NUMPY, "numpy not available")
class TestDownloadGapDetection(unittest.TestCase):
    """_abt_has_gaps flags the zero block a failed XYZ tile leaves."""

    def test_aligned_zero_block_is_a_gap(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.abt")
            _write_abt(p, size=128, zero_block=(64, 64, 64))
            self.assertTrue(ta._abt_has_gaps(p, block=64))

    def test_large_unaligned_zero_region_is_a_gap(self):
        # A real failed tile (~150 px) is bigger than the 64-block, so it always
        # fully contains an aligned block regardless of offset.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.abt")
            _write_abt(p, size=256, zero_block=(30, 30, 130))
            self.assertTrue(ta._abt_has_gaps(p, block=64))

    def test_all_nonzero_is_clean(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.abt")
            _write_abt(p, size=128, zero_block=None)
            self.assertFalse(ta._abt_has_gaps(p, block=64))

    def test_non_abt_is_not_a_gap(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.abt")
            with open(p, "wb") as fh:
                fh.write(b"not an abt file")
            self.assertFalse(ta._abt_has_gaps(p))


@unittest.skipUnless(_HAVE_NUMPY, "numpy not available")
class TestRebuildFlag(unittest.TestCase):
    """A tile flagged for rebuild must not be reused, until the flag clears.

    Supersedes the directory-wide `.incomplete` marker: readiness is a per-tile
    question, so one bad tile no longer condemns the whole pool.
    """

    def test_flag_blocks_reuse_until_cleared(self):
        with tempfile.TemporaryDirectory() as d:
            _write_abt(os.path.join(d, "t.abt"))
            self.assertTrue(ta._tile_ready(d, "t.abt"))
            ta._set_rebuild_flags(d, ["t.abt"], {"t.abt"})
            self.assertFalse(ta._tile_ready(d, "t.abt"))
            ta._set_rebuild_flags(d, ["t.abt"])
            self.assertTrue(ta._tile_ready(d, "t.abt"))

    def test_flag_is_per_tile(self):
        with tempfile.TemporaryDirectory() as d:
            _write_abt(os.path.join(d, "good.abt"))
            _write_abt(os.path.join(d, "bad.abt"))
            ta._set_rebuild_flags(d, ["good.abt", "bad.abt"], {"bad.abt"})
            self.assertTrue(ta._tile_ready(d, "good.abt"))
            self.assertFalse(ta._tile_ready(d, "bad.abt"))

    def test_missing_tile_is_not_ready(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(ta._tile_ready(d, "absent.abt"))


class TestCacheKeyBuildings(unittest.TestCase):
    """Buildings are baked into .abt pixels, so they must key the cache."""

    TILES = _tiles("tile_N48.00E8.00_30m.abt")

    def _key(self, buildings=None):
        return ta._cache_key("src", self.TILES, 30, buildings)

    def test_no_buildings_omits_the_term(self):
        # The buildings term is appended only when there are buildings, so
        # None / "" are the same cache as "no buildings requested".
        names = ",".join(t["filename"] for t in self.TILES)
        plain = hashlib.md5(
            f"{ta._CACHE_SCHEMA}|src|{names}|30".encode()).hexdigest()
        self.assertEqual(self._key(), plain)
        self.assertEqual(self._key(None), plain)
        self.assertEqual(self._key(""), plain)

    def test_schema_version_is_in_the_key(self):
        # Tile geometry changed with the ABT_EXTENT_DEG migration, so caches
        # written by an older plugin must never be mistaken for valid ones.
        names = ",".join(t["filename"] for t in self.TILES)
        unversioned = hashlib.md5(f"src|{names}|30".encode()).hexdigest()
        self.assertNotEqual(self._key(), unversioned)

    def test_buildings_change_the_key(self):
        with tempfile.TemporaryDirectory() as d:
            b = os.path.join(d, "b.fgb")
            open(b, "w").close()
            self.assertNotEqual(self._key(b), self._key())

    def test_different_building_files_differ(self):
        with tempfile.TemporaryDirectory() as d:
            a = os.path.join(d, "a.fgb")
            b = os.path.join(d, "b.fgb")
            open(a, "w").close()
            open(b, "w").close()
            self.assertNotEqual(self._key(a), self._key(b))

    def test_editing_the_file_changes_the_key(self):
        with tempfile.TemporaryDirectory() as d:
            b = os.path.join(d, "b.fgb")
            open(b, "w").close()
            before = self._key(b)
            with open(b, "w") as fh:
                fh.write("more geometry")
            self.assertNotEqual(self._key(b), before)

    def test_directory_contents_change_the_key(self):
        # A directory's own mtime does not move when a file inside is
        # rewritten, so the fingerprint has to walk it.
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, "parts")
            os.makedirs(sub)
            with open(os.path.join(sub, "p1.fgb"), "w") as fh:
                fh.write("a")
            before = self._key(sub)
            with open(os.path.join(sub, "p1.fgb"), "w") as fh:
                fh.write("aa")
            self.assertNotEqual(self._key(sub), before)

    def test_downloaded_source_keys_by_token_not_by_path(self):
        # OpenFreeMap has no file to fingerprint; it identifies itself with an
        # opaque token that must survive into the key unchanged (an earlier
        # version ran it through abspath, making the key depend on the cwd).
        tok = ta._OSM_BUILDINGS_TOKEN
        self.assertEqual(ta.source_fingerprint(tok), tok)
        self.assertNotEqual(self._key(tok), self._key())

    def test_osm_and_file_sources_are_distinguishable(self):
        with tempfile.TemporaryDirectory() as d:
            b = os.path.join(d, "b.fgb")
            open(b, "w").close()
            tok = ta._OSM_BUILDINGS_TOKEN
            combined = f"{ta.source_fingerprint(b)}+{tok}"
            keys = {self._key(), self._key(b), self._key(tok), self._key(combined)}
            # none, file-only, osm-only and both must all be separate caches
            self.assertEqual(len(keys), 4)

    def test_missing_buildings_path_still_keys(self):
        # An unreadable path must not raise — it just keys on the path.
        self.assertNotEqual(self._key("/no/such/buildings.fgb"), self._key())


class TestTerrainSizeWarning(unittest.TestCase):
    """Only genuinely huge runs are worth interrupting the user for."""

    def test_ordinary_size_is_silent(self):
        self.assertIsNone(ta.terrain_size_warning(0))
        self.assertIsNone(ta.terrain_size_warning(1024))          # 1 GB
        self.assertIsNone(ta.terrain_size_warning(49 * 1024))     # 49 GB

    def test_50gb_warns_softly(self):
        got = ta.terrain_size_warning(50 * 1024)
        self.assertIsNotNone(got)
        msg, strong = got
        self.assertFalse(strong)
        self.assertIn("50 GB", msg)

    def test_200gb_warns_strongly(self):
        got = ta.terrain_size_warning(200 * 1024)
        self.assertIsNotNone(got)
        msg, strong = got
        self.assertTrue(strong)
        self.assertIn("extreme", msg)


class TestBboxesIntersect(unittest.TestCase):
    def _bb(self, n, s, e, w):
        return {"north": n, "south": s, "east": e, "west": w}

    def test_overlapping(self):
        self.assertTrue(ta.bboxes_intersect(
            self._bb(48, 47, 9, 8), self._bb(47.5, 46.5, 8.5, 7.5)))

    def test_identical(self):
        a = self._bb(48, 47, 9, 8)
        self.assertTrue(ta.bboxes_intersect(a, dict(a)))

    def test_disjoint_in_longitude(self):
        self.assertFalse(ta.bboxes_intersect(
            self._bb(48, 47, 9, 8), self._bb(48, 47, 12, 11)))

    def test_disjoint_in_latitude(self):
        self.assertFalse(ta.bboxes_intersect(
            self._bb(48, 47, 9, 8), self._bb(51, 50, 9, 8)))

    def test_touching_edges_is_not_an_overlap(self):
        # Sharing only an edge yields no usable terrain pixels.
        self.assertFalse(ta.bboxes_intersect(
            self._bb(48, 47, 9, 8), self._bb(48, 47, 10, 9)))


class TestTerrainCoverageWarning(unittest.TestCase):
    """Fires when the directory cannot cover the analysis area.

    Policy change: partial coverage used to be silent on the grounds that it
    was normal. It is not benign — a terrain directory is the ONLY source once
    selected, with no fall back to the base DEM, so whatever it misses is
    computed over 0 m sea level and looks like real coverage.
    """

    BBOX = {"north": 48.0, "south": 47.0, "east": 9.0, "west": 8.0}

    def test_empty_directory_warns(self):
        with tempfile.TemporaryDirectory() as d:
            msg = ta.terrain_coverage_warning(d, self.BBOX)
            self.assertIsNotNone(msg)
            self.assertIn("No terrain files", msg)

    def test_directory_with_no_terrain_extensions_warns(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "readme.txt"), "w") as fh:
                fh.write("not terrain")
            self.assertIsNotNone(ta.terrain_coverage_warning(d, self.BBOX))

    def _run_with_extent(self, extent):
        """Invoke the check with GDAL mocked to report *extent* as coverage."""
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "a.tif"), "w").close()
            with mock.patch.object(ta.gdal, "BuildVRT", create=True,
                                   return_value=object()), \
                 mock.patch.object(ta.gdal, "_dummy", create=True):
                with mock.patch.object(ta, "_dataset_wgs84_bbox",
                                       return_value=extent):
                    return ta.terrain_coverage_warning(d, self.BBOX)

    def test_full_coverage_is_silent(self):
        self.assertIsNone(self._run_with_extent(
            {"north": 49.0, "south": 46.0, "east": 10.0, "west": 7.0}))

    def test_slight_overhang_is_silent(self):
        # The analysis bbox carries a 1% margin and tiles rarely line up with
        # a DEM edge, so near-total coverage must not nag.
        self.assertIsNone(self._run_with_extent(
            {"north": 47.99, "south": 47.0, "east": 9.0, "west": 8.0}))

    def test_quarter_coverage_warns(self):
        # Covers lat 47.0-47.5 and lon 8.0-8.5 of a 1x1 degree request = 25%.
        msg = self._run_with_extent(
            {"north": 47.5, "south": 46.0, "east": 8.5, "west": 7.0})
        self.assertIsNotNone(msg)
        self.assertIn("25%", msg)
        self.assertIn("0 m", msg)

    def test_sliver_coverage_warns(self):
        msg = self._run_with_extent(
            {"north": 47.1, "south": 47.0, "east": 8.1, "west": 8.0})
        self.assertIsNotNone(msg)
        self.assertIn("1%", msg)

    def test_disjoint_terrain_warns(self):
        msg = self._run_with_extent(
            {"north": 60.0, "south": 59.0, "east": 20.0, "west": 19.0})
        self.assertIsNotNone(msg)
        self.assertIn("does not cover", msg)

    def test_unknown_extent_is_silent(self):
        self.assertIsNone(self._run_with_extent(None))

    def test_missing_directory_warns(self):
        msg = ta.terrain_coverage_warning("/no/such/dir/anywhere", self.BBOX)
        self.assertIsNotNone(msg)
        self.assertIn("does not exist", msg)


class TestEstimateTerrainDiskMb(unittest.TestCase):
    def test_larger_range_needs_more_disk(self):
        small = ta.estimate_terrain_disk_mb(47.0, 8.0, 10.0, 30)
        large = ta.estimate_terrain_disk_mb(47.0, 8.0, 200.0, 30)
        self.assertGreater(large, small)

    def test_finer_resolution_needs_more_disk(self):
        coarse = ta.estimate_terrain_disk_mb(47.0, 8.0, 50.0, 90)
        fine = ta.estimate_terrain_disk_mb(47.0, 8.0, 50.0, 10)
        self.assertGreater(fine, coarse)

    def test_zero_range_needs_nothing(self):
        self.assertEqual(ta.estimate_terrain_disk_mb(47.0, 8.0, 0.0, 30), 0)


if __name__ == "__main__":
    unittest.main()


class TestBuildingsOnTheFastPath(unittest.TestCase):
    """OpenFreeMap buildings are fused during download, not via QGIS export.

    Diverting a buildings run onto the export + ingest path measured 7-17x
    slower (a 50 km run spent ~1560 s of 1687 s inside ``writeRaster``), which
    is what made buildings unusable in practice.
    """

    BBOX = {"north": 47.5, "south": 47.0, "east": 8.5, "west": 8.0}
    TILE = "tile_N48.00E8.00_30m.abt"

    def _run(self, pool_dir, pbf_dir, out_lines, supported=True):
        """Run one download and return the job dict the converter was given.

        *supported* stands in for inspecting the engine binary.
        """
        subtiles = [{"filename": self.TILE, "ul_lat": 48.0, "ul_lon": 8.0,
                     "size_px": 3704, "exact_res_m": 30.0}]
        bm = mock.Mock()
        bm.find_binary.return_value = "aether_converter"
        _write_pool_abt(os.path.join(pool_dir, self.TILE))
        seen = {}

        def fake_run(exe, job_file):
            with open(job_file) as fh:
                seen.update(json.load(fh))
            return 0, out_lines

        with mock.patch.object(ta, "_run_converter_download_once",
                               side_effect=fake_run), \
             mock.patch.object(ta, "_binary_has_buildings_support",
                               return_value=supported), \
             mock.patch.object(ta, "_abt_has_gaps", return_value=False):
            ok = ta._try_rust_download(_XYZ_SOURCE, pool_dir, self.BBOX, 30,
                                       subtiles, bm, pbf_dir=pbf_dir)
        return ok, seen

    def test_pbf_dir_is_passed_to_the_downloader(self):
        with tempfile.TemporaryDirectory() as d:
            ok, job = self._run(
                d, os.path.join(d, "_buildings"),
                ["[Stats] Tiles: 100/100 OK", "[Buildings] 12 pbf tile(s)"],
            )
        self.assertTrue(ok)
        self.assertTrue(job["buildings_pbf_dir"].endswith("_buildings"))

    def test_field_is_absent_when_no_buildings_are_requested(self):
        # An older converter must see exactly the job shape it sees today.
        with tempfile.TemporaryDirectory() as d:
            ok, job = self._run(d, None, ["[Stats] Tiles: 100/100 OK"])
        self.assertTrue(ok)
        self.assertNotIn("buildings_pbf_dir", job)

    def test_engine_without_buildings_support_is_refused(self):
        """The field is additive: an old engine ignores it and writes plain
        terrain, which would then be pooled under a buildings-keyed identity
        and reused forever. Gate on the binary, before anything downloads."""
        with tempfile.TemporaryDirectory() as d:
            ok, job = self._run(d, os.path.join(d, "_buildings"),
                                ["[Stats] Tiles: 100/100 OK"], supported=False)
            self.assertFalse(ok, "old engine was allowed onto the fast path")
            self.assertEqual(job, {}, "download ran despite the refusal")

    def test_unreadable_engine_is_assumed_to_support_it(self):
        """A false negative costs the 7-17x slow path, so do not guess "no"."""
        with tempfile.TemporaryDirectory() as d:
            ok, job = self._run(d, os.path.join(d, "_buildings"),
                                ["[Stats] Tiles: 100/100 OK"], supported=None)
            self.assertTrue(ok)
            self.assertIn("buildings_pbf_dir", job)

    def test_missing_marker_line_does_not_refuse_a_supporting_engine(self):
        """Regression: the old output-scraping gate produced a false negative
        against an engine that provably has the feature, sending a real run
        down the slow path."""
        with tempfile.TemporaryDirectory() as d:
            ok, job = self._run(d, os.path.join(d, "_buildings"),
                                ["[Stats] Tiles: 100/100 OK"])
            self.assertTrue(ok)
            self.assertIn("buildings_pbf_dir", job)
            self.assertTrue(ta._tile_ready(d, self.TILE))

    def test_empty_extent_is_fine(self):
        # "no buildings in extent" is a real answer from a supporting engine
        # (ocean, empty countryside) — not a missing feature.
        with tempfile.TemporaryDirectory() as d:
            ok, _ = self._run(
                d, os.path.join(d, "_buildings"),
                ["[Stats] Tiles: 100/100 OK",
                 "[Buildings] 8 pbf tile(s), no buildings in extent"],
            )
            self.assertTrue(ok)
            self.assertTrue(ta._tile_ready(d, self.TILE))


class TestBuildingsAppliedMarker(unittest.TestCase):
    def test_detects_the_marker(self):
        self.assertTrue(ta._buildings_were_applied(["x", "[Buildings] 3 tiles"]))

    def test_absent_marker(self):
        self.assertFalse(ta._buildings_were_applied(["[Stats] Tiles: 1/1 OK"]))

    def test_empty_output(self):
        self.assertFalse(ta._buildings_were_applied([]))


class TestPrebuiltAbtTiles(unittest.TestCase):
    """A directory of .abt tiles is an engine input, not a source to convert."""

    BBOX = {"north": 48.4, "south": 48.0, "east": 8.4, "west": 8.0}

    def _tile_dir(self, root, names=("tile_N48.00E8.00_30m.abt",)):
        d = os.path.join(root, "prebuilt")
        os.makedirs(d, exist_ok=True)
        for n in names:
            _write_pool_abt(os.path.join(d, n))
        return d

    def test_detects_a_tile_directory(self):
        with tempfile.TemporaryDirectory() as root:
            d = self._tile_dir(root)
            self.assertTrue(ta.is_abt_tile_dir(d))
            self.assertEqual(len(ta.list_abt_tiles(d)), 1)

    def test_a_geotiff_directory_is_not_one(self):
        with tempfile.TemporaryDirectory() as root:
            d = os.path.join(root, "rasters")
            os.makedirs(d)
            open(os.path.join(d, "dem.tif"), "wb").close()
            self.assertFalse(ta.is_abt_tile_dir(d))

    def test_nested_tiles_are_not_found(self):
        # The engine's read_dir is not recursive, so reporting tiles in a
        # sub-folder would promise coverage the run would not have.
        with tempfile.TemporaryDirectory() as root:
            outer = os.path.join(root, "outer")
            self._tile_dir(os.path.join(outer, "inner"))
            os.makedirs(outer, exist_ok=True)
            self.assertFalse(ta.is_abt_tile_dir(outer))

    def test_empty_and_missing_dirs(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(ta.is_abt_tile_dir(root))
        self.assertFalse(ta.is_abt_tile_dir(None))
        self.assertFalse(ta.is_abt_tile_dir("/no/such/dir"))

    def test_prepare_terrain_returns_the_dir_untouched(self):
        """No download, no conversion — the tiles are already the answer."""
        layer = mock.Mock()
        layer.source.return_value = _XYZ_SOURCE
        with tempfile.TemporaryDirectory() as root:
            d = self._tile_dir(root)
            with mock.patch.object(ta, "get_cache_dir", return_value=root), \
                 mock.patch.object(ta, "_try_rust_download") as dl, \
                 mock.patch.object(ta, "_run_converter") as conv:
                out = ta.prepare_terrain(layer, 48.2, 8.2, 20.0, 30,
                                         mock.Mock(), terrain_dir=d)
            self.assertEqual(out, d)
            dl.assert_not_called()
            conv.assert_not_called()


class TestAbtCoverageWarning(unittest.TestCase):
    """GDAL cannot open a .abt, so these are checked by their own headers."""

    BBOX = {"north": 48.4, "south": 48.0, "east": 8.4, "west": 8.0}

    def _dir_with(self, root, name):
        d = os.path.join(root, "t")
        os.makedirs(d, exist_ok=True)
        _write_pool_abt(os.path.join(d, name))
        return d

    def test_no_warning_when_tiles_cover_the_area(self):
        with tempfile.TemporaryDirectory() as root:
            d = self._dir_with(root, "tile_N48.00E8.00_30m.abt")
            from waveshed.core import abt as abt_mod
            hdr = abt_mod.read_header(ta.list_abt_tiles(d)[0])
            span = hdr.size * hdr.pixel_res
            bbox = {"north": hdr.ul_lat - span * 0.1,
                    "south": hdr.ul_lat - span * 0.9,
                    "west": hdr.ul_lon + span * 0.1,
                    "east": hdr.ul_lon + span * 0.9}
            self.assertIsNone(ta.terrain_coverage_warning(d, bbox))

    def test_warns_when_tiles_are_somewhere_else(self):
        with tempfile.TemporaryDirectory() as root:
            d = self._dir_with(root, "tile_N48.00E8.00_30m.abt")
            far = {"north": -10.0, "south": -11.0, "west": 100.0, "east": 101.0}
            msg = ta.terrain_coverage_warning(d, far)
            self.assertIsNotNone(msg)
            self.assertIn("do not overlap", msg)

    def test_unreadable_abt_is_reported(self):
        with tempfile.TemporaryDirectory() as root:
            d = os.path.join(root, "t")
            os.makedirs(d)
            with open(os.path.join(d, "broken.abt"), "wb") as fh:
                fh.write(b"nope")
            msg = ta.terrain_coverage_warning(d, self.BBOX)
            self.assertIsNotNone(msg)
            self.assertIn("could be read", msg)

    def test_empty_dir_message_mentions_abt(self):
        with tempfile.TemporaryDirectory() as root:
            msg = ta.terrain_coverage_warning(root, self.BBOX)
            self.assertIsNotNone(msg)
            self.assertIn(".abt", msg)


class TestPrebuiltAbtSuitability(unittest.TestCase):
    """Pre-built tiles are finished output: the run cannot change their
    resolution and cannot add buildings to them. Both mismatches have to be
    raised, or the result is confidently wrong."""

    def _dir(self, root, res_m, n=1):
        d = os.path.join(root, "prebuilt")
        os.makedirs(d, exist_ok=True)
        for i in range(n):
            _write_pool_abt(os.path.join(d, f"t{i}.abt"), size=64,
                            res_m=res_m, ul_lat=48.0, ul_lon=8.0)
        return d

    def _bbox_inside(self, d):
        from waveshed.core import abt as abt_mod
        h = abt_mod.read_header(ta.list_abt_tiles(d)[0])
        span = h.size * h.pixel_res
        return {"north": h.ul_lat - span * 0.1,
                "south": h.ul_lat - span * 0.9,
                "west": h.ul_lon + span * 0.1,
                "east": h.ul_lon + span * 0.9}

    def test_coarse_tiles_are_refused_for_a_fine_request(self):
        """30 m tiles must not silently serve a 5 m analysis."""
        with tempfile.TemporaryDirectory() as root:
            d = self._dir(root, res_m=30.0)
            msg = ta.terrain_coverage_warning(d, self._bbox_inside(d),
                                              resolution_m=5)
            self.assertIsNotNone(msg, "30 m tiles accepted for a 5 m run")
            self.assertIn("30 m", msg)
            self.assertIn("5 m", msg)

    def test_matching_resolution_is_accepted(self):
        with tempfile.TemporaryDirectory() as root:
            d = self._dir(root, res_m=5.0)
            self.assertIsNone(
                ta.terrain_coverage_warning(d, self._bbox_inside(d),
                                            resolution_m=5))

    def test_exact_resolution_rounding_does_not_warn(self):
        """A nominal 30 m tile stores 29.99757 m — that is not a mismatch."""
        with tempfile.TemporaryDirectory() as root:
            d = self._dir(root, res_m=29.99757019438445)
            self.assertIsNone(
                ta.terrain_coverage_warning(d, self._bbox_inside(d),
                                            resolution_m=30))

    def test_finer_tiles_are_fine(self):
        """Downsampling is honest; only upsampling invents detail."""
        with tempfile.TemporaryDirectory() as root:
            d = self._dir(root, res_m=5.0)
            self.assertIsNone(
                ta.terrain_coverage_warning(d, self._bbox_inside(d),
                                            resolution_m=30))

    def test_buildings_requested_on_prebuilt_tiles_warns(self):
        with tempfile.TemporaryDirectory() as root:
            d = self._dir(root, res_m=5.0)
            msg = ta.terrain_coverage_warning(d, self._bbox_inside(d),
                                              resolution_m=5,
                                              osm_buildings=True)
            self.assertIsNotNone(msg, "buildings checkbox silently ignored")
            self.assertIn("Buildings", msg)

    def test_buildings_file_also_warns(self):
        with tempfile.TemporaryDirectory() as root:
            d = self._dir(root, res_m=5.0)
            msg = ta.terrain_coverage_warning(d, self._bbox_inside(d),
                                              resolution_m=5,
                                              buildings_file="/x/b.fgb")
            self.assertIsNotNone(msg)

    def test_no_buildings_no_warning(self):
        with tempfile.TemporaryDirectory() as root:
            d = self._dir(root, res_m=5.0)
            self.assertIsNone(
                ta.terrain_coverage_warning(d, self._bbox_inside(d),
                                            resolution_m=5,
                                            osm_buildings=False))

    def test_prepare_terrain_logs_the_problem(self):
        """A Processing run never sees the dialog, so the log must carry it."""
        layer = mock.Mock()
        layer.source.return_value = _XYZ_SOURCE
        with tempfile.TemporaryDirectory() as root:
            d = self._dir(root, res_m=30.0)
            logged = []
            with mock.patch.object(ta, "get_cache_dir", return_value=root), \
                 mock.patch.object(ta, "_log", side_effect=logged.append):
                out = ta.prepare_terrain(layer, 48.0, 8.0, 1.0, 5,
                                         mock.Mock(), terrain_dir=d,
                                         osm_buildings=True)
            self.assertEqual(out, d)
            self.assertTrue(any("WARNING" in ln for ln in logged),
                            f"no warning logged; got {logged}")


# ---------------------------------------------------------------------------
# XYZ encoding detection (B2)
# ---------------------------------------------------------------------------

_MAPZEN_URL = ("https%3A//s3.amazonaws.com/elevation-tiles-prod/terrarium/"
               "%7Bz%7D/%7Bx%7D/%7By%7D.png")
_MAPBOX_URL = ("https%3A//api.mapbox.com/v4/mapbox.terrain-rgb/"
               "%7Bz%7D/%7Bx%7D/%7By%7D.pngraw")
_ANON_URL = "https%3A//tiles.example.com/%7Bz%7D/%7Bx%7D/%7By%7D.png"


class TestXyzEncoding(unittest.TestCase):
    """Terrarium and Terrain-RGB are both 8-bit RGB PNGs.

    Nothing in the pixels tells them apart, so decoding a Terrain-RGB tile as
    Terrarium yields terrain around -32000 m — a confident, silent wrong
    answer. The URI used to be read for ``interpretation=mapbox…`` only, so a
    Terrain-RGB layer added by hand (QGIS writes no interpretation) was always
    decoded as Terrarium.
    """

    def test_mapbox_url_without_interpretation_is_mapbox(self):
        src = f"type=xyz&url={_MAPBOX_URL}&zmax=15"
        self.assertEqual(ta.xyz_encoding(src), ta.ENCODING_MAPBOX)

    def test_terrarium_url_without_interpretation_is_terrarium(self):
        src = f"type=xyz&url={_MAPZEN_URL}&zmax=15"
        self.assertEqual(ta.xyz_encoding(src), ta.ENCODING_TERRARIUM)

    def test_explicit_interpretation_beats_the_url(self):
        # QGIS wrote it: it is authoritative, whatever the host name suggests.
        src = f"type=xyz&interpretation=mapboxterrain&url={_MAPZEN_URL}"
        self.assertEqual(ta.xyz_encoding(src), ta.ENCODING_MAPBOX)
        src = f"type=xyz&interpretation=terrarium&url={_MAPBOX_URL}"
        self.assertEqual(ta.xyz_encoding(src), ta.ENCODING_TERRARIUM)

    def test_url_naming_neither_family_is_undecided(self):
        self.assertIsNone(ta.xyz_encoding(f"type=xyz&url={_ANON_URL}"))

    def test_url_naming_both_families_is_undecided(self):
        both = "https%3A//tiles.example.com/mapbox/terrarium/%7Bz%7D.png"
        self.assertIsNone(ta.xyz_encoding(f"type=xyz&url={both}"))


class TestResolveXyzEncoding(unittest.TestCase):

    def test_conflicting_url_requires_an_explicit_choice(self):
        both = "https%3A//tiles.example.com/mapbox/terrarium/%7Bz%7D.png"
        with self.assertRaises(RuntimeError) as caught:
            ta.resolve_xyz_encoding(f"type=xyz&url={both}")
        msg = str(caught.exception)
        self.assertIn("interpretation=terrarium", msg)
        self.assertIn("interpretation=mapboxterrain", msg)

    def test_unknown_url_keeps_the_terrarium_default_and_says_so(self):
        # Every self-hosted Terrarium mirror in the wild relies on this
        # default, so it stays — but it is no longer silent.
        logged = []
        with mock.patch.object(ta, "_log", side_effect=logged.append):
            enc = ta.resolve_xyz_encoding(f"type=xyz&url={_ANON_URL}")
        self.assertEqual(enc, ta.ENCODING_TERRARIUM)
        self.assertTrue(any("Terrarium" in ln for ln in logged), logged)

    def test_decided_urls_log_nothing(self):
        logged = []
        with mock.patch.object(ta, "_log", side_effect=logged.append):
            ta.resolve_xyz_encoding(f"type=xyz&url={_MAPZEN_URL}")
            ta.resolve_xyz_encoding(f"type=xyz&url={_MAPBOX_URL}")
        self.assertEqual(logged, [])


class TestEncodingAndIdentityCannotDrift(unittest.TestCase):
    """One detector feeds both the cache key and the downloader."""

    def test_identity_carries_the_detected_encoding(self):
        for url in (_MAPZEN_URL, _MAPBOX_URL, _ANON_URL):
            src = f"type=xyz&url={url}&zmax=15"
            self.assertIn(f"|{ta.resolve_xyz_encoding(src)}|",
                          ta.source_identity(src))

    def test_terrarium_pools_keep_their_existing_identity(self):
        # The whole point of the constraint: a source that decodes correctly
        # today must not be re-downloaded because of this fix.
        src = f"type=xyz&url={_MAPZEN_URL}&zmax=15"
        self.assertEqual(
            ta.source_identity(src),
            f"xyz|{urllib.parse.unquote(_MAPZEN_URL)}|terrarium|zmax=15")

    def test_undecided_uri_keeps_the_historical_identity(self):
        # So adding interpretation=terrarium later lands on the same pool
        # rather than re-downloading it.
        anon = f"type=xyz&url={_ANON_URL}&zmax=15"
        explicit = f"type=xyz&url={_ANON_URL}&zmax=15&interpretation=terrarium"
        self.assertEqual(ta.source_identity(anon), ta.source_identity(explicit))

    def test_mapbox_pool_identity_moves_off_the_wrong_data(self):
        # Its current tiles decode to about -32000 m, so they must not be
        # reused — a new identity is the point, not a regression.
        src = f"type=xyz&url={_MAPBOX_URL}&zmax=15"
        legacy = f"xyz|{urllib.parse.unquote(_MAPBOX_URL)}|terrarium|zmax=15"
        self.assertNotEqual(ta.source_identity(src), legacy)
        self.assertIn("|mapbox|", ta.source_identity(src))


# ---------------------------------------------------------------------------
# zmax validation (B1)
# ---------------------------------------------------------------------------

class TestResolveZmax(unittest.TestCase):
    """QGIS defaults a hand-added XYZ layer to zmax=18.

    Mapzen/AWS stops at 15, so at 2 m every tile 404s, the converter writes
    those as 0 m, and the run completes over a flat sea-level plane.
    """

    def test_known_service_is_capped_at_its_real_maximum(self):
        z, warning = ta.resolve_zmax(f"type=xyz&url={_MAPZEN_URL}&zmax=18")
        self.assertEqual(z, 15)
        self.assertIsNotNone(warning)
        self.assertIn("z15", warning)

    def test_a_correct_zmax_passes_without_a_warning(self):
        self.assertEqual(ta.resolve_zmax(f"type=xyz&url={_MAPZEN_URL}&zmax=15"),
                         (15, None))

    def test_unknown_service_is_left_alone(self):
        # We have no idea what it publishes; guessing would silently coarsen
        # a service that really does go deeper.
        self.assertEqual(ta.resolve_zmax(f"type=xyz&url={_ANON_URL}&zmax=18"),
                         (18, None))

    def test_absent_zmax_keeps_the_historical_default(self):
        self.assertEqual(ta.resolve_zmax(f"type=xyz&url={_ANON_URL}"), (15, None))

    def test_non_numeric_zmax_does_not_crash_the_run(self):
        # int(params["zmax"]) used to raise straight out of prepare_terrain.
        z, warning = ta.resolve_zmax(f"type=xyz&url={_ANON_URL}&zmax=abc")
        self.assertEqual(z, 15)
        self.assertIn("not a number", warning)

    def test_absurd_zmax_is_capped(self):
        z, warning = ta.resolve_zmax(f"type=xyz&url={_ANON_URL}&zmax=99")
        self.assertEqual(z, ta._XYZ_ZOOM_CEILING)
        self.assertIsNotNone(warning)

    def test_negative_zmax_is_floored(self):
        z, warning = ta.resolve_zmax(f"type=xyz&url={_ANON_URL}&zmax=-3")
        self.assertEqual(z, 0)
        self.assertIsNotNone(warning)

    def test_clamping_does_not_move_the_cache_identity(self):
        # A zmax=18 pool also holds the coarse tiles that were always correct
        # (a 30 m run never asks past z12), so the raw value stays in the key.
        src = f"type=xyz&url={_MAPZEN_URL}&zmax=18"
        self.assertIn("zmax=18", ta.source_identity(src))
        self.assertEqual(ta.resolve_zmax(src)[0], 15)

    def test_capped_zoom_is_what_tiles_are_built_from(self):
        # 2 m at lat 47 wants z16; the cap is what keeps it fetchable.
        z_max, _warning = ta.resolve_zmax(f"type=xyz&url={_MAPZEN_URL}&zmax=18")
        self.assertEqual(ta.tile_zoom(47.0, 2.0, z_max), 15)
        self.assertEqual(ta.tile_zoom(47.0, 2.0, 18), 16)   # the old behaviour


class TestTotalDownloadFailureIsDistinct(unittest.TestCase):
    """"Nothing arrived" must not read like "a few tiles are gapped"."""

    _SPECS = [{"filename": "tile_a.abt"}, {"filename": "tile_b.abt"}]

    def _run_group(self, ok, total, pool):
        logged = []
        calls = []

        def run_pass(specs, conn, zoom):
            calls.append((len(specs), conn, zoom))
            return 0, ok, total

        with mock.patch.object(ta, "_log", side_effect=logged.append):
            result = ta._download_zoom_group(
                pool, self._SPECS, 16, 256, 4, run_pass, lambda: True)
        return result, logged, calls

    def test_zero_tiles_fetched_is_reported_as_a_total_failure(self):
        with tempfile.TemporaryDirectory() as pool:
            result, logged, calls = self._run_group(0, 100, pool)
            flags = sorted(os.listdir(pool))
        self.assertFalse(result)
        text = "\n".join(logged)
        self.assertIn("0 of 100", text)
        self.assertIn("flat 0 m terrain", text)
        # Retrying at half the connections cannot fix a 404 for everything.
        self.assertEqual(len(calls), 1)
        # And none of it may be left cached as a valid tile.
        self.assertEqual(flags, ["tile_a.abt.rebuild", "tile_b.abt.rebuild"])

    def test_a_partial_failure_still_reads_as_partial(self):
        with tempfile.TemporaryDirectory() as pool:
            _result, logged, _calls = self._run_group(97, 100, pool)
        text = "\n".join(logged)
        self.assertNotIn("flat 0 m terrain", text)
        self.assertIn("partial terrain", text)

    def test_a_complete_download_says_nothing_about_failure(self):
        with tempfile.TemporaryDirectory() as pool:
            _result, logged, _calls = self._run_group(100, 100, pool)
        self.assertNotIn("flat 0 m terrain", "\n".join(logged))


# ---------------------------------------------------------------------------
# Buildings download result (B6)
# ---------------------------------------------------------------------------

class TestBuildingDownloadResultIsChecked(unittest.TestCase):
    """A zero-tile buildings fetch used to pass for a successful one.

    The (ok, missing) return was discarded, so buildings_missing stayed False,
    the tiles were pooled under a buildings-keyed identity — and every later
    run hit that cache and quietly produced building-free terrain.
    """

    def _prepare(self, download_result):
        from waveshed.core import openfreemap as ofm

        layer = mock.Mock()
        layer.source.return_value = _XYZ_SOURCE

        logged = []
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(ta, "get_cache_dir", return_value=root), \
                 mock.patch.object(ta, "_log", side_effect=logged.append), \
                 mock.patch.object(ta, "_try_rust_download", return_value=True), \
                 mock.patch.object(ofm, "download_building_tiles",
                                   return_value=download_result), \
                 mock.patch.object(ofm, "tiles_for_bbox",
                                   return_value=[(14, 8000, 5000)]):
                buildings = ta.buildings_identity(osm_buildings=True)
                pool = ta._pool_dir(_XYZ_SOURCE, buildings)
                ta.prepare_terrain(layer, 47.4, 8.5, 5.0, 30, mock.Mock(),
                                   osm_buildings=True)
                flags = sorted(n for n in os.listdir(pool)
                               if n.endswith(".rebuild"))
        return logged, flags

    def test_zero_tiles_fetched_is_a_failed_buildings_run(self):
        logged, flags = self._prepare((0, 42))
        text = "\n".join(logged)
        self.assertIn("no building tiles could be fetched", text)
        self.assertIn("buildings were requested but unavailable", text)
        # Nothing may stay pooled under an identity that claims buildings.
        self.assertTrue(flags, "no tile was flagged for rebuild")

    def test_a_successful_fetch_leaves_the_tiles_pooled(self):
        logged, flags = self._prepare((17, 3))
        text = "\n".join(logged)
        self.assertNotIn("no building tiles could be fetched", text)
        self.assertEqual(flags, [])
