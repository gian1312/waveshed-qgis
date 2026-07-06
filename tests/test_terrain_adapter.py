"""Unit tests for terrain_adapter — sector bbox, subtile filtering, estimates.

These tests do NOT require QGIS or AETHER binaries.  QGIS stubs are
provided by conftest.py.
"""

# Bootstrap stubs before any plugin import.
import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import math
import os
import struct
import tempfile
import unittest
from unittest import mock

import numpy as np

import aether_qgis.core.terrain_adapter as ta
from aether_qgis.core.terrain_adapter import (
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
    """The .abt writer stores row_stride as u16; sub-tiles must keep it < 65536.

    A 1-degree tile at 2 m overflows (stride 111360 -> wraps), shearing the
    terrain into bands, so _subtile_degrees shrinks the tile at fine res.
    """

    def test_two_metre_base_dem_shrinks_for_u16(self):
        # Base-DEM path: 1 deg would overflow u16 at 2 m, so it drops to 0.5.
        self.assertEqual(_subtile_degrees(2), 0.5)

    def test_coarse_base_dem_uses_full_degree(self):
        for res in (5, 10, 30):
            self.assertEqual(_subtile_degrees(res), 1.0)

    def test_high_res_matches_prepare_data_sub_size_macro(self):
        # Mirror prepare_data.py: 0.1 deg at <=3 m, else 0.5 deg, with Swiss data.
        self.assertEqual(_subtile_degrees(2, high_res=True), 0.1)
        self.assertEqual(_subtile_degrees(3, high_res=True), 0.1)
        self.assertEqual(_subtile_degrees(5, high_res=True), 0.5)
        self.assertEqual(_subtile_degrees(10, high_res=True), 0.5)
        self.assertEqual(_subtile_degrees(30, high_res=True), 0.5)

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


if __name__ == "__main__":
    unittest.main()
