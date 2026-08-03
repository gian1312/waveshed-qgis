"""Unit tests for terrain_adapter — sector bbox, subtile filtering, estimates.

These tests do NOT require QGIS or AETHER binaries.  QGIS stubs are
provided by conftest.py.
"""

# Bootstrap stubs before any plugin import.
import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import hashlib
import math
import os
import struct
import tempfile
import unittest
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


def _write_pool_abt(path, size=64):
    """A minimal complete .abt: 44-byte header plus size*stride body bytes.

    Deliberately numpy-free (unlike _write_abt below) so the pool tests run
    everywhere — they care about presence and length, not pixels.
    """
    stride = _abt_row_stride(size)
    hdr = (b"AETH" + struct.pack("<HH", 1, size)
           + struct.pack("<dddd", 47.0, 8.0, 1.0 / size, 1.0 / size)
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
        self.assertIn("not found", logged[2])


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
        def fake_download(source, pool, bbox, res, subtiles, bm):
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

        def fake_download(source, pool, bbox, res, subtiles, bm):
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
class TestIncompleteMarker(unittest.TestCase):
    """An incomplete download must not be reused as a valid cache."""

    def test_marker_blocks_cache_hit_until_cleared(self):
        with tempfile.TemporaryDirectory() as d:
            _write_abt(os.path.join(d, "t.abt"))
            self.assertTrue(ta._cache_hit(d))
            ta._mark_incomplete(d)
            self.assertFalse(ta._cache_hit(d))
            ta._clear_incomplete(d)
            self.assertTrue(ta._cache_hit(d))


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
