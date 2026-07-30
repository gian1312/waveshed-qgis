"""Unit tests for map_converter_tab — estimation, job building, helpers.

QGIS stubs provided by conftest.py.
"""

# Bootstrap stubs before any plugin import.
import conftest  # noqa: F401

import math
import os
import re
import tempfile
import unittest

from waveshed.gui.map_converter_tab import (
    _estimate_tile_count_and_mb,
    _abt_size_px,
    _ABT_EXTENT_DEG,
    _LayerEntry,
    _MapConverterWorker,
    _fgb_cache_name,
    _fgb_translate_options,
    _list_terrain_files,
    _detect_xyz_resolution,
)


class TestFgbCacheName(unittest.TestCase):
    """The conversion cache lives in a shared, session-spanning temp dir, so
    its file name must pin everything that determines the output."""

    def _write(self, d, name, content="x"):
        p = os.path.join(d, name)
        with open(p, "w") as fh:
            fh.write(content)
        return p

    def test_same_basename_from_different_dirs_do_not_collide(self):
        # Regression: `a/buildings.shp` and `b/buildings.shp` used to map onto
        # one cached .fgb, so the second silently got the first's geometry.
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "a"))
            os.makedirs(os.path.join(d, "b"))
            p1 = self._write(os.path.join(d, "a"), "buildings.shp", "one")
            p2 = self._write(os.path.join(d, "b"), "buildings.shp", "two")
            self.assertNotEqual(_fgb_cache_name(p1), _fgb_cache_name(p2))

    def test_declared_crs_changes_the_name(self):
        # Regression: the cache was consulted before src_crs, so re-declaring a
        # source's CRS reused the conversion made under the old one.
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "b.fgb")
            self.assertNotEqual(
                _fgb_cache_name(p, "EPSG:2056"), _fgb_cache_name(p, "EPSG:32632"))
            self.assertNotEqual(_fgb_cache_name(p, ""), _fgb_cache_name(p, "EPSG:2056"))

    def test_editing_the_source_changes_the_name(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "b.fgb", "short")
            before = _fgb_cache_name(p, "EPSG:2056")
            with open(p, "w") as fh:
                fh.write("a much longer body")
            self.assertNotEqual(_fgb_cache_name(p, "EPSG:2056"), before)

    def test_name_is_stable_for_an_unchanged_source(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "b.fgb")
            self.assertEqual(
                _fgb_cache_name(p, "EPSG:2056"), _fgb_cache_name(p, "EPSG:2056"))

    def test_pre_fix_cache_entries_are_unreachable(self):
        # Conversions cached before -s_srs was honoured were plain
        # "<basename>.fgb". Those must no longer be returned, or the fix would
        # never apply to anyone who already has one.
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "buildings.shp")
            self.assertNotEqual(_fgb_cache_name(p, "EPSG:2056"), "buildings.fgb")

    def test_name_stays_recognisable_and_is_an_fgb(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "buildings.shp")
            name = _fgb_cache_name(p, "EPSG:2056")
            self.assertTrue(name.startswith("buildings_"), name)
            self.assertTrue(name.endswith(".fgb"), name)


class TestFgbTranslateOptions(unittest.TestCase):
    """Buildings must always reach the converter in WGS84."""

    def test_always_targets_wgs84_flatgeobuf(self):
        opts = _fgb_translate_options()
        self.assertIn("-t_srs", opts)
        self.assertEqual(opts[opts.index("-t_srs") + 1], "EPSG:4326")
        self.assertEqual(opts[opts.index("-f") + 1], "FlatGeobuf")

    def test_no_source_crs_leaves_s_srs_off(self):
        # GDAL should use the file's embedded CRS when we don't know better.
        self.assertNotIn("-s_srs", _fgb_translate_options(""))

    def test_known_source_crs_is_declared(self):
        # Regression: src_crs used to be accepted and then ignored, so a file
        # without an embedded CRS was reprojected from the wrong origin.
        opts = _fgb_translate_options("EPSG:2056")
        self.assertIn("-s_srs", opts)
        self.assertEqual(opts[opts.index("-s_srs") + 1], "EPSG:2056")


class TestEstimateTileCountAndMb(unittest.TestCase):

    def test_single_res_small_bbox(self):
        bbox = {"north": 48.0, "south": 47.0, "east": 9.0, "west": 8.0}
        tiles, mb = _estimate_tile_count_and_mb(bbox, [30])
        # 0.5 deg tiles: 2x2 = 4
        self.assertEqual(tiles, 4)
        self.assertGreater(mb, 0)

    def test_multiple_res(self):
        bbox = {"north": 48.0, "south": 47.0, "east": 9.0, "west": 8.0}
        _, mb1 = _estimate_tile_count_and_mb(bbox, [30])
        _, mb2 = _estimate_tile_count_and_mb(bbox, [10, 30])
        self.assertGreater(mb2, mb1)

    def test_90m_1deg_tiles(self):
        bbox = {"north": 48.0, "south": 47.0, "east": 9.0, "west": 8.0}
        tiles, _ = _estimate_tile_count_and_mb(bbox, [90])
        self.assertEqual(tiles, 1)

    def test_250m_2deg_tiles(self):
        bbox = {"north": 48.0, "south": 47.0, "east": 9.0, "west": 8.0}
        tiles, _ = _estimate_tile_count_and_mb(bbox, [250])
        self.assertEqual(tiles, 1)

    def test_2m_01deg_tiles(self):
        bbox = {"north": 47.1, "south": 47.0, "east": 8.1, "west": 8.0}
        tiles, _ = _estimate_tile_count_and_mb(bbox, [2])
        self.assertEqual(tiles, 1)

    def test_empty(self):
        bbox = {"north": 48.0, "south": 47.0, "east": 9.0, "west": 8.0}
        self.assertEqual(_estimate_tile_count_and_mb(bbox, []), (0, 0))


class TestAbtSizePx(unittest.TestCase):

    def test_30m_1deg(self):
        sz = _abt_size_px(30, 1.0)
        self.assertEqual(sz % 4, 0)
        self.assertAlmostEqual(sz, 3704, delta=4)

    def test_always_multiple_of_4(self):
        for res, ext in _ABT_EXTENT_DEG.items():
            sz = _abt_size_px(res, ext)
            self.assertEqual(sz % 4, 0, f"res={res}")
            self.assertGreater(sz, 0)

    def test_tile_file_size_under_500mb(self):
        for res, ext in _ABT_EXTENT_DEG.items():
            sz = _abt_size_px(res, ext)
            stride = (sz * 2 + 255) & ~255
            mb = (44 + stride * sz) / (1024 * 1024)
            self.assertLess(mb, 500, f"res={res}m: {mb:.0f} MB")


class TestAbtExtentMapping(unittest.TestCase):

    def test_all_resolutions_present(self):
        for r in [2, 5, 10, 30, 90, 250]:
            self.assertIn(r, _ABT_EXTENT_DEG)

    def test_monotonic(self):
        self.assertLessEqual(_ABT_EXTENT_DEG[2], _ABT_EXTENT_DEG[5])
        self.assertLessEqual(_ABT_EXTENT_DEG[5], _ABT_EXTENT_DEG[30])
        self.assertLessEqual(_ABT_EXTENT_DEG[30], _ABT_EXTENT_DEG[90])
        self.assertLessEqual(_ABT_EXTENT_DEG[90], _ABT_EXTENT_DEG[250])


class TestLayerEntry(unittest.TestCase):

    def test_defaults(self):
        e = _LayerEntry()
        self.assertEqual(e.layer_type, "raster")
        self.assertEqual(e.target_resolutions, [30])
        self.assertIsNone(e.extent)

    def test_buildings(self):
        e = _LayerEntry(layer_type="buildings", source_path="/b.fgb")
        self.assertEqual(e.layer_type, "buildings")


class TestWorkerProgress(unittest.TestCase):

    def test_parse_line(self):
        self.assertEqual(
            _MapConverterWorker._parse_progress("[Rust] Progress: 10/47"),
            (10, 47),
        )

    def test_parse_final(self):
        self.assertEqual(
            _MapConverterWorker._parse_progress("[Rust] Progress: 47/47"),
            (47, 47),
        )

    def test_no_match(self):
        self.assertIsNone(
            _MapConverterWorker._parse_progress("[Rust] Batch processing...")
        )


class TestListTerrainFiles(unittest.TestCase):

    def test_finds_terrain(self):
        d = tempfile.mkdtemp()
        for name in ["a.tif", "b.tiff", "c.dem", "d.hgt", "e.txt", "f.png"]:
            open(os.path.join(d, name), "w").close()
        files = _list_terrain_files(d)
        names = [os.path.basename(f) for f in files]
        self.assertIn("a.tif", names)
        self.assertIn("c.dem", names)
        self.assertNotIn("e.txt", names)

    def test_empty(self):
        d = tempfile.mkdtemp()
        self.assertEqual(_list_terrain_files(d), [])


class TestDetectXyzResolution(unittest.TestCase):

    def test_z15(self):
        src = "type=xyz&url=http://example.com/{z}/{x}/{y}.png&zmax=15"
        res = _detect_xyz_resolution(src)
        self.assertIsNotNone(res)
        # z15 at equator: ~4.8m
        self.assertAlmostEqual(res, 4.8, delta=0.5)

    def test_z10(self):
        src = "type=xyz&url=http://example.com/{z}/{x}/{y}.png&zmax=10"
        res = _detect_xyz_resolution(src)
        self.assertIsNotNone(res)
        # z10: ~152m
        self.assertAlmostEqual(res, 152.9, delta=5)

    def test_default_z15(self):
        src = "type=xyz&url=http://example.com/{z}/{x}/{y}.png"
        res = _detect_xyz_resolution(src)
        self.assertIsNotNone(res)


class TestOverpassToGeojson(unittest.TestCase):

    def test_basic_conversion(self):
        from waveshed.gui.map_converter_tab import MapConverterTab
        data = {
            "elements": [
                {"type": "node", "id": 1, "lat": 47.0, "lon": 8.0},
                {"type": "node", "id": 2, "lat": 47.0, "lon": 8.001},
                {"type": "node", "id": 3, "lat": 47.001, "lon": 8.001},
                {"type": "node", "id": 4, "lat": 47.001, "lon": 8.0},
                {
                    "type": "way", "id": 100,
                    "nodes": [1, 2, 3, 4, 1],
                    "tags": {"building": "yes"},
                },
            ]
        }
        geojson = MapConverterTab._overpass_to_geojson(data)
        self.assertEqual(geojson["type"], "FeatureCollection")
        self.assertEqual(len(geojson["features"]), 1)
        self.assertEqual(
            geojson["features"][0]["geometry"]["type"], "Polygon"
        )

    def test_empty(self):
        from waveshed.gui.map_converter_tab import MapConverterTab
        geojson = MapConverterTab._overpass_to_geojson({"elements": []})
        self.assertEqual(len(geojson["features"]), 0)


if __name__ == "__main__":
    unittest.main()
