"""Unit tests for the OpenFreeMap building-tile source.

Pure tile maths, URL handling, the tile check and the strict fetch (the ONE
fetch of both tabs, through core/osm_buildings). No network access — a
local stand-in server (``_ofm_server``).

QGIS stubs provided by conftest.py.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import os
import tempfile
import time
import unittest
from unittest import mock

from waveshed.core import openfreemap as ofm
from _ofm_server import OfmServer, building_tile


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

class TestAttribution(unittest.TestCase):
    def test_attribution_names_the_real_rightsholders(self):
        # ODbL obligation; the web app credits the wrong dataset, so this is
        # pinned here to keep the plugin honest.
        self.assertIn("OpenStreetMap", ofm.ATTRIBUTION)
        self.assertIn("OpenMapTiles", ofm.ATTRIBUTION)
        self.assertIn("OpenFreeMap", ofm.ATTRIBUTION)
        self.assertIn("ODbL", ofm.ATTRIBUTION)
        self.assertNotIn("Microsoft", ofm.ATTRIBUTION)


class TestOneFetch(unittest.TestCase):
    """2026-10-23: Site Analysis's lenient fetch (8 workers, one attempt, a
    4096-tile cap, a guessed fallback URL, failures logged and dropped) is
    gone — every caller goes through the strict fetch."""

    def test_the_lenient_path_is_gone(self):
        for name in ("download_building_tiles", "MAX_TILES",
                     "resolve_tile_template", "FALLBACK_TEMPLATE"):
            self.assertFalse(hasattr(ofm, name), name)


# ---------------------------------------------------------------------------
# The strict fetch (the Map Converter's OSM buildings row, 2026-10-22)
# ---------------------------------------------------------------------------

#: Six z14 tiles over Zurich (3 columns x 2 rows).
_TILES = [(14, x, y) for y in (5737, 5738) for x in (8580, 8581, 8582)]
_HOUSE = building_tile([(1500, 1500, 2600, 2600, 40.0)])
_FAST = mock.patch.object(ofm, "RETRY_BACKOFF_S", (0.0, 0.0, 0.0))


class TestTileCheck(unittest.TestCase):
    """What enters the store must be a tile the converter can decode."""

    def test_vector_tiles_pass_plain_gzipped_and_empty(self):
        import gzip
        self.assertIsNone(ofm.tile_problem(_HOUSE))
        self.assertIsNone(ofm.tile_problem(gzip.compress(_HOUSE)))
        self.assertIsNone(ofm.tile_problem(building_tile()))
        self.assertIsNone(ofm.tile_problem(b""))

    def test_error_pages_cut_off_bodies_and_bad_gzip_fail(self):
        import gzip
        gz = gzip.compress(_HOUSE)
        cases = {
            "html": b"<html><body>502 Bad Gateway</body></html>",
            "json": b'{"error": "rate limited"}',
            "cut": _HOUSE[:len(_HOUSE) // 2],
            "cut gzip": gz[:len(gz) // 2],
            "bad crc": gz[:-8] + b"\0" * 8,
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.assertIsNotNone(ofm.tile_problem(data), name)


class TestStrictTemplate(unittest.TestCase):

    def test_tilejson_is_read_on_every_call(self):
        # No session reuse: a snapshot path gone stale in a long session
        # answers every tile with "no data".
        with OfmServer() as srv, \
                mock.patch.object(ofm, "TILEJSON_URL", srv.tilejson_url):
            self.assertEqual(ofm.resolve_tile_template_strict(),
                             srv.template)
            self.assertEqual(ofm.resolve_tile_template_strict(),
                             srv.template)
            self.assertEqual(srv.tilejson_requests, 2)

    def test_an_unreadable_tilejson_is_an_error_not_the_fallback(self):
        with OfmServer(tilejson_status=503) as srv, \
                mock.patch.object(ofm, "TILEJSON_URL", srv.tilejson_url):
            with self.assertRaises(ofm.FetchFailed) as cm:
                ofm.resolve_tile_template_strict(attempts=2)
            self.assertEqual(srv.tilejson_requests, 2)
        self.assertIn("HTTP 503", str(cm.exception))
        self.assertIn("TileJSON", str(cm.exception))

    def test_stop_is_seen_while_tilejson_hangs(self):
        t0 = time.monotonic()
        with OfmServer(tilejson_delay_s=3.0) as srv, \
                mock.patch.object(ofm, "TILEJSON_URL", srv.tilejson_url):
            with self.assertRaises(ofm.FetchCancelled):
                ofm.resolve_tile_template_strict(
                    should_cancel=lambda: time.monotonic() - t0 > 0.2)
            took = time.monotonic() - t0
        self.assertLess(took, 1.0)


class TestStrictFetch(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = os.path.join(self._tmp.name, "store")

    def tearDown(self):
        self._tmp.cleanup()

    def fetch(self, srv, tiles=_TILES, **kw):
        return ofm.fetch_building_tiles(tiles, self.store, srv.template, **kw)

    def test_every_tile_is_fetched_checked_and_stored(self):
        starts, progress = [], []
        with OfmServer(default=_HOUSE) as srv:
            r = self.fetch(srv, workers=4,
                           on_start=lambda c, t: starts.append((c, t)),
                           on_progress=lambda d, t, b: progress.append(d))
            asked = srv.requested()
        names = sorted(ofm.tile_filename(*t) for t in _TILES)
        self.assertEqual(asked, {(x, y) for _z, x, y in _TILES})
        self.assertEqual((r.total, r.cached, r.fetched, r.empty),
                         (6, 0, 6, 0))
        self.assertEqual(list(r.present), names)
        self.assertEqual(sorted(os.listdir(self.store)), names)
        self.assertEqual(starts, [(0, 6)])
        self.assertEqual(progress, [1, 2, 3, 4, 5, 6])
        # Stored as served (gzip, which the converter un-gzips), decodable.
        for name in names:
            with open(os.path.join(self.store, name), "rb") as fh:
                data = fh.read()
            self.assertEqual(data[:2], b"\x1f\x8b")
            self.assertIsNone(ofm.tile_problem(data))
        self.assertEqual(r.nbytes, sum(os.path.getsize(
            os.path.join(self.store, n)) for n in names))

    def test_one_kept_alive_connection_per_worker(self):
        # OpenFreeMap accepts only ~15-20 NEW connections per second from
        # one address: a connection per tile capped the fetch there.
        tiles = [(14, 8500 + i, 5700) for i in range(40)]
        with OfmServer(default=_HOUSE) as srv:
            r = self.fetch(srv, tiles=tiles, workers=3)
            connections = srv.connections
        self.assertEqual(r.fetched, 40)
        self.assertLessEqual(connections, 3)

    def test_a_dropped_idle_connection_is_reopened(self):
        tiles = [(14, 8500 + i, 5700) for i in range(10)]
        with OfmServer(default=_HOUSE, drop_idle=True) as srv:
            r = self.fetch(srv, tiles=tiles, workers=2)
            asked = len(srv.requests)
        self.assertEqual(r.fetched, 10)
        self.assertEqual(asked, 10)

    def test_tiles_in_the_store_are_not_requested_again(self):
        os.makedirs(self.store)
        ofm._write_tile(self.store, ofm.tile_filename(*_TILES[0]), _HOUSE)
        with OfmServer(default=_HOUSE) as srv:
            r = self.fetch(srv)
            asked = srv.requested()
        self.assertNotIn(_TILES[0][1:], asked)
        self.assertEqual(len(asked), 5)
        self.assertEqual((r.cached, r.fetched), (1, 5))

    def test_a_tile_without_data_is_fine_no_tile_with_data_is_not(self):
        one = {_TILES[0][1:]: _HOUSE}
        with OfmServer(tiles=one, default=None) as srv:   # empty 200s
            r = self.fetch(srv)
        self.assertEqual((r.fetched, r.empty), (1, 5))
        self.assertEqual(list(os.listdir(self.store)),
                         [ofm.tile_filename(*_TILES[0])])
        with OfmServer(default=None) as srv:
            with self.assertRaises(ofm.FetchFailed) as cm:
                ofm.fetch_building_tiles(_TILES, os.path.join(
                    self._tmp.name, "other"), srv.template)
        self.assertIn("none of the 6 building tiles has data", str(cm.exception))
        self.assertEqual(cm.exception.missing, 6)

    def test_no_data_anywhere_can_be_left_to_the_caller(self):
        # Site Analysis: an area without any building data is a warning
        # there, not a failed fetch (require_data=False).
        with OfmServer(default=None) as srv:
            r = self.fetch(srv, require_data=False)
        self.assertEqual((r.total, r.fetched, r.empty), (6, 0, 6))
        self.assertEqual(r.present, ())

    def test_404_means_no_data_there(self):
        fail = {t[1:]: 404 for t in _TILES[1:]}
        with OfmServer(default=_HOUSE, fail=fail) as srv:
            r = self.fetch(srv)
        self.assertEqual((r.fetched, r.empty), (1, 5))

    def test_a_transient_error_is_retried(self):
        fail = {_TILES[2][1:]: 503}
        with _FAST, OfmServer(default=_HOUSE, fail=fail, fail_times=2) as srv:
            r = self.fetch(srv)
            attempts = sum(1 for _t, x, y in srv.requests
                           if (x, y) == _TILES[2][1:])
        self.assertEqual(r.fetched, 6)
        self.assertEqual(attempts, 3)

    def test_a_tile_failing_every_attempt_ends_the_fetch(self):
        # 200 tiles, one failing: the fetch ends at it instead of fetching
        # the rest, naming the tile, the cause and what is missing.
        tiles = [(14, 8500 + i % 20, 5700 + i // 20) for i in range(200)]
        bad = tiles[3]
        with _FAST, OfmServer(default=_HOUSE, fail={bad[1:]: 500},
                              delay_s=0.01) as srv:
            with self.assertRaises(ofm.FetchFailed) as cm:
                self.fetch(srv, tiles=tiles, workers=2)
            asked = len(srv.requested())
        err = str(cm.exception)
        self.assertIn(ofm.tile_filename(*bad), err)
        self.assertIn("HTTP 500", err)
        self.assertIn(f"after {ofm.TILE_ATTEMPTS} attempts", err)
        self.assertIn("of 200 building tiles missing", err)
        self.assertGreater(cm.exception.missing, 100)
        self.assertEqual(cm.exception.total, 200)
        self.assertLess(asked, 100, "the fetch went on after the failure")

    def test_an_undecodable_answer_fails_after_its_attempts(self):
        with _FAST, OfmServer(default=_HOUSE,
                              garbage=[_TILES[1][1:]]) as srv:
            with self.assertRaises(ofm.FetchFailed) as cm:
                self.fetch(srv)
        self.assertIn("cannot be decoded", str(cm.exception))
        self.assertIn(ofm.tile_filename(*_TILES[1]), str(cm.exception))
        self.assertNotIn(ofm.tile_filename(*_TILES[1]),
                         os.listdir(self.store))

    def test_a_timeout_is_a_failure_after_its_attempts(self):
        with _FAST, OfmServer(default=_HOUSE, delay_s=1.0) as srv:
            with self.assertRaises(ofm.FetchFailed) as cm:
                self.fetch(srv, tiles=_TILES[:1], timeout=0.2, attempts=2)
        self.assertIn("timed out", str(cm.exception))
        self.assertIn("after 2 attempts", str(cm.exception))

    def test_cancel_stops_the_fetch_promptly(self):
        tiles = [(14, 8500 + i % 20, 5700 + i // 20) for i in range(400)]
        t0 = time.monotonic()
        with OfmServer(default=_HOUSE, delay_s=0.05) as srv:
            with self.assertRaises(ofm.FetchCancelled):
                self.fetch(srv, tiles=tiles, workers=4,
                           should_cancel=lambda: time.monotonic() - t0 > 0.3)
            stopped = time.monotonic()
            asked = len(srv.requested())
            time.sleep(0.3)
            asked_later = len(srv.requested())
            written = len(os.listdir(self.store))
            time.sleep(0.2)
            written_later = len(os.listdir(self.store))
        self.assertLess(stopped - t0, 0.3 + ofm.CANCEL_GRACE_S)
        self.assertLess(asked, 100)
        self.assertEqual(asked_later, asked, "requests went on after Stop")
        self.assertEqual(written_later, written, "tiles written after Stop")
        self.assertFalse(any(n.endswith(".part")
                             for n in os.listdir(self.store)))


if __name__ == "__main__":
    unittest.main()
