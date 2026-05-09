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

from aether_qgis.gui.map_converter_tab import (
    _estimate_tile_count_and_mb,
    _abt_size_px,
    _ABT_EXTENT_DEG,
    _LayerEntry,
    _MapConverterWorker,
    _list_terrain_files,
    _detect_xyz_resolution,
)


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
        for r in [2, 5, 10, 30, 90]:
            self.assertIn(r, _ABT_EXTENT_DEG)

    def test_monotonic(self):
        self.assertLessEqual(_ABT_EXTENT_DEG[2], _ABT_EXTENT_DEG[5])
        self.assertLessEqual(_ABT_EXTENT_DEG[5], _ABT_EXTENT_DEG[30])
        self.assertLessEqual(_ABT_EXTENT_DEG[30], _ABT_EXTENT_DEG[90])


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
        from aether_qgis.gui.map_converter_tab import MapConverterTab
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
        from aether_qgis.gui.map_converter_tab import MapConverterTab
        geojson = MapConverterTab._overpass_to_geojson({"elements": []})
        self.assertEqual(len(geojson["features"]), 0)


if __name__ == "__main__":
    unittest.main()
