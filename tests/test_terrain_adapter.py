"""Unit tests for terrain_adapter — sector bbox, subtile filtering, estimates.

These tests do NOT require QGIS or AETHER binaries.  QGIS stubs are
provided by conftest.py.
"""

# Bootstrap stubs before any plugin import.
import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import math
import unittest

from aether_qgis.core.terrain_adapter import (
    _compute_sector_bbox,
    _compute_subtiles,
    _estimate_abt_disk_mb,
    _cache_key,
)


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
        bbox = _compute_sector_bbox(47.0, 8.0, 500.0)
        lat_count = math.ceil(bbox["north"]) - math.floor(bbox["south"])
        lon_count = math.ceil(bbox["east"]) - math.floor(bbox["west"])
        square_count = lat_count * lon_count
        filtered = self._count(max_range_km=500.0)
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


class TestCacheKey(unittest.TestCase):

    def test_different_bbox(self):
        bb1 = {"north": 48, "south": 46, "east": 9, "west": 7}
        bb2 = {"north": 49, "south": 47, "east": 10, "west": 8}
        self.assertNotEqual(_cache_key("s", bb1, 30), _cache_key("s", bb2, 30))

    def test_different_res(self):
        bb = {"north": 48, "south": 46, "east": 9, "west": 7}
        self.assertNotEqual(_cache_key("s", bb, 10), _cache_key("s", bb, 30))

    def test_deterministic(self):
        bb = {"north": 48, "south": 46, "east": 9, "west": 7}
        self.assertEqual(_cache_key("s", bb, 30), _cache_key("s", bb, 30))

    def test_hex_digest(self):
        k = _cache_key("s", {"north": 48, "south": 46, "east": 9, "west": 7}, 30)
        self.assertEqual(len(k), 32)
        int(k, 16)


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


if __name__ == "__main__":
    unittest.main()
