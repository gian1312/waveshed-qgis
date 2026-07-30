"""Unit tests for the OpenFreeMap building-tile source.

Pure tile maths, URL handling and the download guard. No network access —
the one download test stubs urlopen.

QGIS stubs provided by conftest.py.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import io
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from waveshed.core import openfreemap as ofm


def bbox(n, s, e, w):
    return {"north": n, "south": s, "east": e, "west": w}


class TestTileMaths(unittest.TestCase):
    def test_known_zurich_tile_at_z14(self):
        # 47.37 N, 8.54 E at z14 — against the standard slippy formula:
        #   x = floor((lon + 180) / 360 * 2^z)
        #   y = floor((1 - asinh(tan(lat)) / pi) / 2 * 2^z)
        self.assertEqual(ofm.lon_to_tile_x(8.54, 14), 8580)
        self.assertEqual(ofm.lat_to_tile_y(47.37, 14), 5737)

    def test_origin_and_antimeridian_clamp(self):
        self.assertEqual(ofm.lon_to_tile_x(-180.0, 14), 0)
        # +180 lands exactly on the wrap; it must clamp inside the grid.
        self.assertEqual(ofm.lon_to_tile_x(180.0, 14), (1 << 14) - 1)

    def test_poles_clamp_into_range(self):
        n = 1 << 14
        self.assertEqual(ofm.lat_to_tile_y(90.0, 14), 0)
        self.assertEqual(ofm.lat_to_tile_y(-90.0, 14), n - 1)

    def test_y_increases_southwards(self):
        north = ofm.lat_to_tile_y(48.0, 14)
        south = ofm.lat_to_tile_y(47.0, 14)
        self.assertLess(north, south)


class TestTilesForBbox(unittest.TestCase):
    def test_single_tile_bbox(self):
        tiles = ofm.tiles_for_bbox(bbox(47.3701, 47.3700, 8.5401, 8.5400))
        self.assertEqual(len(tiles), 1)
        self.assertEqual(tiles[0][0], ofm.BUILDING_ZOOM)

    def test_covers_the_whole_box(self):
        bb = bbox(47.40, 47.30, 8.60, 8.50)
        tiles = ofm.tiles_for_bbox(bb)
        xs = {x for _, x, _ in tiles}
        ys = {y for _, _, y in tiles}
        self.assertIn(ofm.lon_to_tile_x(bb["west"], 14), xs)
        self.assertIn(ofm.lon_to_tile_x(bb["east"], 14), xs)
        self.assertIn(ofm.lat_to_tile_y(bb["north"], 14), ys)
        self.assertIn(ofm.lat_to_tile_y(bb["south"], 14), ys)

    def test_count_matches_the_estimate(self):
        bb = bbox(47.5, 47.2, 8.7, 8.3)
        self.assertEqual(len(ofm.tiles_for_bbox(bb)), ofm.estimate_tile_count(bb))

    def test_no_duplicate_tiles(self):
        tiles = ofm.tiles_for_bbox(bbox(47.5, 47.2, 8.7, 8.3))
        self.assertEqual(len(tiles), len(set(tiles)))


class TestUrlAndNaming(unittest.TestCase):
    def test_template_substitution(self):
        got = ofm.tile_url("https://h/planet/20260101_000/{z}/{x}/{y}.pbf", 14, 8579, 5807)
        self.assertEqual(got, "https://h/planet/20260101_000/14/8579/5807.pbf")

    def test_filename_matches_the_converter_parser(self):
        # aether_converter parses `{z}_{x}_{y}.pbf`; drifting here silently
        # yields zero buildings because every file is skipped.
        self.assertEqual(ofm.tile_filename(14, 8579, 5807), "14_8579_5807.pbf")

    def test_tilejson_template_is_used_when_available(self):
        ofm._cached_template = None
        payload = b'{"tiles":["https://t/planet/SNAP/{z}/{x}/{y}.pbf"]}'
        with mock.patch.object(ofm.urllib.request, "urlopen") as uo:
            uo.return_value.__enter__.return_value = io.BytesIO(payload)
            self.assertEqual(
                ofm.resolve_tile_template(), "https://t/planet/SNAP/{z}/{x}/{y}.pbf")
        ofm._cached_template = None

    def test_unreachable_tilejson_falls_back(self):
        ofm._cached_template = None
        with mock.patch.object(ofm.urllib.request, "urlopen",
                               side_effect=OSError("no network")):
            self.assertEqual(ofm.resolve_tile_template(), ofm.FALLBACK_TEMPLATE)
        ofm._cached_template = None


class TestDownloadGuard(unittest.TestCase):
    def test_oversized_area_is_refused_before_downloading(self):
        # Buildings only exist at z14, so a big analysis area is an enormous
        # download. Refusing beats starting something that runs for hours.
        huge = bbox(50.0, 45.0, 12.0, 6.0)
        self.assertGreater(ofm.estimate_tile_count(huge), ofm.MAX_TILES)
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError) as cm:
                ofm.download_building_tiles(huge, d)
            self.assertIn("limit", str(cm.exception))

    def test_attribution_names_the_real_rightsholders(self):
        # ODbL obligation; the web app credits the wrong dataset, so this is
        # pinned here to keep the plugin honest.
        self.assertIn("OpenStreetMap", ofm.ATTRIBUTION)
        self.assertIn("OpenMapTiles", ofm.ATTRIBUTION)
        self.assertIn("OpenFreeMap", ofm.ATTRIBUTION)
        self.assertIn("ODbL", ofm.ATTRIBUTION)
        self.assertNotIn("Microsoft", ofm.ATTRIBUTION)


class TestDownload(unittest.TestCase):
    """A 404 means 'no buildings here' and must not fail the run."""

    def _run(self, responder):
        ofm._cached_template = ofm.FALLBACK_TEMPLATE
        bb = bbox(47.3705, 47.3700, 8.5405, 8.5400)
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(ofm.urllib.request, "urlopen",
                                   side_effect=responder):
                ok, missing = ofm.download_building_tiles(bb, d, max_workers=2)
            files = sorted(os.listdir(d))
        ofm._cached_template = None
        return ok, missing, files

    def test_successful_tiles_are_written(self):
        def responder(req, timeout=None):
            m = mock.MagicMock()
            m.__enter__.return_value.read.return_value = b"PBFDATA"
            return m
        ok, missing, files = self._run(responder)
        self.assertGreater(ok, 0)
        self.assertEqual(missing, 0)
        self.assertTrue(all(f.endswith(".pbf") for f in files))
        # No leftover partial files.
        self.assertFalse(any(f.endswith(".part") for f in files))

    def test_404_is_counted_as_empty_not_failed(self):
        def responder(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "no tile", {}, None)
        ok, missing, files = self._run(responder)
        self.assertEqual(ok, 0)
        self.assertGreater(missing, 0)
        self.assertEqual(files, [])

    def test_network_error_does_not_raise(self):
        def responder(req, timeout=None):
            raise OSError("connection reset")
        ok, missing, files = self._run(responder)
        self.assertEqual(ok, 0)
        self.assertGreater(missing, 0)


if __name__ == "__main__":
    unittest.main()
