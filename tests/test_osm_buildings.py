"""core/osm_buildings — the ONE OpenFreeMap buildings path (2026-10-23).

Pinned here:

* :class:`BuildingCover`: the cover of a list of extents, the reach rule
  per output tile (members / needed / view_names), and that the Map
  Converter's ``_OsmBuildings`` IS this class;
* :class:`BurnMonitor` over the exact lines aether_converter 0.2.7 prints
  (captured from the frozen binary: ingest ``[Info] buildings_pbf_dir:``,
  download ``[Buildings] … pbf tile(s)``, the decode warning): kill at the
  line after a decode warning, a converter run that owes a report and
  prints none, "no buildings in extent" everywhere (empty, no error),
  buildings decoded and none drawn over the whole run (error — one batch
  drawing none is not);
* :func:`batch_view` (ONE linker), :func:`ensure_tiles` (the strict fetch,
  stand-in server), :func:`drop_undecodable`; a view named before the
  fetch and its ready marker (:func:`remove_ready_marker` /
  :func:`write_ready_marker`, :func:`ready_view` / :func:`complete_view`),
  the wait lines (no report; the seconds per converter run);
* :class:`BuildingsFetch` — the fetch thread both tabs run (the Map
  Converter's ``_BuildingsBranch`` IS one): groups in order, each announced
  when in, the last after ``on_fetched``; a failure in the whole fetch's
  numbers before ``on_failed``; a failing hook in its own words; Stop
  quiet; no data anywhere strict or the caller's call; a view released
  when its names are in (:meth:`link_when_ready`: at once, or on the fetch
  thread with the marker after the links; never after a failure; a link
  failure is the fetch's error in its own words);
* the LIST of aether_converter >= 0.2.14 (``buildings_pbf_list``, toolkit
  CONTRACT changelog 51): its path (own key, one md5 directory under
  ``<cache>/views`` — Clear Terrain Cache takes it), :func:`ready_list`
  (a stale list removed; cannot → None) / :func:`complete_list` (exactly
  the names, atomic: temp + ``replace_with_retry``), released by
  :meth:`list_when_ready` by the same rule as the view (nothing linked;
  never after a failure or Stop; a write failure is the fetch's error);
  the 0.2.14 decode header is a "decoding" line and moves no bar;
* the output manifest, schema 2 (``terrain_adapter.read_tiles_manifest`` /
  ``write_tiles_manifest``): round trip, schema 1 read (buildings unknown),
  ``_tiles_to_build`` judging the fingerprint only; the Map Converter's
  records (``openfreemap:z14`` / ``none`` / ``unverified``) and its
  run-level zero-drawn rule, through the shipped worker.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import gzip
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

import waveshed.core.terrain_adapter as ta
import waveshed.gui.map_converter_tab as mct
from waveshed.core import openfreemap as ofm
from waveshed.core import osm_buildings as ob

from _ofm_server import OfmServer, building_tile
from test_mc_osm_buildings import _Case, _OsmRun, _PbfConverter, HOUSE

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: Scratch inside the project's gitignored .tmp (never the container drive).
_SCRATCH = os.path.join(_REPO, ".tmp", "test_osm_buildings")


def _scratch(test):
    os.makedirs(_SCRATCH, exist_ok=True)
    d = tempfile.mkdtemp(prefix="t_", dir=_SCRATCH)
    test.addCleanup(shutil.rmtree, d, True)
    return d


#: The lines the frozen aether_converter 0.2.7 printed for one 30 m tile
#: over twelve z14 tiles with a house each (.tmp/adv2 probe, 2026-10-23).
DECODING = ('[Rust] Decoding building tiles in "/c/views/0f3a" (once for '
            'the run)...')
INGEST_DRAWN = ("[Info] buildings_pbf_dir: 12 tile(s), 12 building(s) "
                "(12 after dedup), 12 drawn, 3835 pixel(s) raised")
INGEST_EMPTY = "[Info] buildings_pbf_dir: 2 tile(s), no buildings in extent"
INGEST_NONE_DRAWN = ("[Info] buildings_pbf_dir: 12 tile(s), 12 building(s) "
                     "(12 after dedup), 0 drawn, 0 pixel(s) raised")
DOWNLOAD_DRAWN = ("[Buildings] 12 pbf tile(s) → 12 building(s) (0 dup "
                  "dropped) → 1/1 abt tile(s), 3835 px in 0.2s")
DOWNLOAD_EMPTY = "[Buildings] 2 pbf tile(s), no buildings in extent"
DOWNLOAD_NONE_DRAWN = ("[Buildings] 12 pbf tile(s) → 12 building(s) (0 dup "
                       "dropped) → 0/1 abt tile(s), 0 px in 0.1s")
WARN = ("[Warn] buildings 14_0_0.pbf: PBF decode error: failed to decode "
        "Protobuf message: unexpected end group tag")
TILE_LINE = "[Tile] ing.abt covered=3429904/3429904"
STATS = "[Stats] Tiles: 9/9 OK (100.0% success)"
#: A buildings FILE burn (ingest.rs apply_buildings) — not a pbf report.
FILE_BURN = ("[Buildings] /b/houses.fgb: 3 polygon feature(s) → 3 "
             "footprint(s), 3 with a height from the data (0 at the 10 m "
             "default), 3 drawn, 40 px raised")
#: aether_converter 0.2.14's decode header for jobs with
#: ``buildings_pbf_list`` (toolkit CONTRACT changelog 51, as specified).
LISTED_DECODING = ('[Rust] Decoding 21012 listed building tiles from '
                   '"/c/pool/5f2c" (once for the run)...')


def read_building_list(path):
    """The names of a ``buildings_pbf_list`` file as the converter reads
    them (CONTRACT changelog 51): one per line, ``\\n`` or ``\\r\\n``, blank
    lines and ``#`` lines ignored, a duplicate once."""
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    names = []
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and line not in names:
            names.append(line)
    return names


# ---------------------------------------------------------------------------
# BuildingCover
# ---------------------------------------------------------------------------

class Cover(unittest.TestCase):

    EXT = {"south": 47.30, "north": 47.34, "west": 8.40, "east": 8.44}

    def test_the_cover_is_the_union_of_the_extents_tiles(self):
        other = {"south": 47.36, "north": 47.37, "west": 8.50, "east": 8.51}
        cover = ob.BuildingCover([self.EXT, other, None])
        want = {(x, y) for e in (self.EXT, other)
                for _z, x, y in ofm.tiles_for_bbox(e)}
        self.assertEqual(cover.cover, want)
        self.assertEqual(cover.bbox(), {"south": 47.30, "north": 47.37,
                                        "west": 8.40, "east": 8.51})
        # The whole cover, north to south and west to east, at z14.
        needed = cover.needed()
        self.assertEqual({(x, y) for _z, x, y in needed}, want)
        self.assertEqual(needed, sorted(needed, key=lambda t: (t[2], t[1])))
        self.assertTrue(all(z == ofm.BUILDING_ZOOM for z, _x, _y in needed))

    def test_members_follow_the_reach_rule(self):
        # An output tile reaches the cover tiles of its bbox grown by
        # REACH_DEG (1/16 of a z14 tile) — no more, no less.
        expected = mct._enumerate_tiles(
            mct._snap_bbox({"south": 47.0, "north": 47.5, "west": 8.0,
                            "east": 8.5}, [30]), [30])
        res, tile = expected[0]
        wide = ob.BuildingCover([{"south": 46.9, "north": 47.6,
                                  "west": 7.9, "east": 8.6}])
        reach = ta._expand_bbox(ob._output_tile_bbox(res, tile),
                                ob.REACH_DEG)
        want = {(x, y) for _z, x, y in ofm.tiles_for_bbox(reach)}
        self.assertEqual(set(wide.members(res, tile)), want)
        inside = {(x, y) for _z, x, y in ofm.tiles_for_bbox(
            ob._output_tile_bbox(res, tile))}
        self.assertTrue(inside <= want)
        # A z14 tile just past the west edge, within the reach, is a member.
        z = ofm.BUILDING_ZOOM
        edge = ofm.lon_to_tile_x(tile["ul_lon"] - ob.REACH_DEG / 2, z)
        row = ofm.lat_to_tile_y(tile["ul_lat"] - 0.25, z)
        self.assertIn((edge, row), set(wide.members(res, tile)))
        # Restricted to the cover; needed() of the tile = its members.
        cover = ob.BuildingCover([self.EXT])
        self.assertEqual(set(cover.members(res, tile)), cover.cover)
        self.assertEqual({(x, y) for _z, x, y in cover.needed([(res, tile)])},
                         cover.cover)
        self.assertTrue(cover.tag(res, tile).startswith("openfreemap:z14@"))

    def test_view_names_are_the_needed_tiles_the_store_holds(self):
        cover = ob.BuildingCover([self.EXT])
        names = [ofm.tile_filename(*t) for t in cover.needed()]
        self.assertEqual(cover.view_names(), [])
        cover.present = set(names[:3]) | {"14_1_1.pbf"}
        self.assertEqual(cover.view_names(), sorted(names[:3]))

    def test_the_map_converter_uses_this_cover(self):
        self.assertIs(mct._OsmBuildings, ob.BuildingCover)
        self.assertEqual(mct._OSM_REACH_DEG, ob.REACH_DEG)


# ---------------------------------------------------------------------------
# BurnMonitor — the converter's own lines
# ---------------------------------------------------------------------------

class Monitor(unittest.TestCase):

    def feed(self, mon, lines):
        return [mon.feed(ln) for ln in lines]

    def test_a_clean_ingest_burn(self):
        mon = ob.BurnMonitor(1)
        self.assertEqual(self.feed(mon, [DECODING, INGEST_DRAWN, TILE_LINE]),
                         [False, False, False])
        self.assertIsNone(mon.end(tiles=1))
        self.assertIsNone(mon.verdict())
        self.assertFalse(mon.empty)
        self.assertEqual((mon.reports, mon.decoded, mon.drawn, mon.raised,
                          mon.pbf_tiles), (1, 12, 12, 3835, 12))
        self.assertEqual((mon.batch_reports, mon.batch_drawn,
                          mon.batch_raised), (1, 12, 3835))
        self.assertEqual(mon.summary(),
                         "12 building(s) decoded, 12 building(s) drawn, "
                         "3,835 px raised")

    def test_line_kinds(self):
        mon = ob.BurnMonitor(1)
        kinds = []
        for line in (DECODING, INGEST_DRAWN, TILE_LINE, FILE_BURN):
            mon.feed(line)
            kinds.append(mon.last_kind)
        self.assertEqual(kinds, ["decoding", "report", None, None])
        self.assertEqual(mon.reports, 1, "a buildings FILE line is no pbf "
                                         "report")

    def test_a_decode_warning_kills_at_the_next_line(self):
        mon = ob.BurnMonitor(2)
        got = self.feed(mon, [DECODING, WARN, WARN.replace("14_0_0",
                                                           "14_0_1"),
                              INGEST_DRAWN])
        # Every warned tile is known before the kill, at the line after.
        self.assertEqual(got, [False, False, False, True])
        self.assertEqual([n for n, _e in mon.bad_tiles],
                         ["14_0_0.pbf", "14_0_1.pbf"])
        self.assertEqual(mon.bad_tiles[0][1], "PBF decode error: failed to "
                         "decode Protobuf message: unexpected end group tag")
        self.assertEqual(mon.reports, 0, "the line after the warning is "
                                         "not counted")
        self.assertIsNone(mon.end(tiles=2), "the decode failure is the cause")
        self.assertEqual(mon.verdict(),
                         "Buildings: the converter could not decode 2 "
                         "OpenFreeMap tile(s) (first: 14_0_0.pbf: PBF decode "
                         "error: failed to decode Protobuf message: "
                         "unexpected end group tag).")

    def test_an_ingest_without_a_report_is_an_engine_ignoring_the_field(self):
        mon = ob.BurnMonitor(2)
        self.feed(mon, [TILE_LINE, "[Rust] Progress: 2/2"])
        msg = mon.end(tiles=3)
        self.assertEqual(
            msg, "aether_converter wrote 3 tile(s) without reporting a burn "
                 "of the building tiles it was given (no '[Info] "
                 "buildings_pbf_dir:' line) — this engine does not apply "
                 "buildings_pbf_dir. Update the engine binaries (Settings → "
                 "Download Binaries).")
        self.assertEqual(mon.verdict(), msg)
        # A converter run that failed owes nothing; one without the field
        # neither.
        other = ob.BurnMonitor(2)
        self.assertIsNone(other.end(ok=False))
        self.assertIsNone(ob.BurnMonitor(0).end())

    def test_no_buildings_in_extent_everywhere_is_empty_not_an_error(self):
        mon = ob.BurnMonitor(3)
        self.feed(mon, [DECODING, INGEST_EMPTY, INGEST_EMPTY, INGEST_EMPTY])
        self.assertIsNone(mon.end())
        self.assertTrue(mon.empty)
        self.assertIsNone(mon.verdict())
        mon.begin(1, what="download")
        self.feed(mon, [DOWNLOAD_EMPTY, STATS])
        self.assertIsNone(mon.end())
        self.assertTrue(mon.empty)
        self.assertIsNone(mon.verdict())
        self.assertFalse(ob.BurnMonitor().empty, "no report is not empty")

    def test_decoded_but_none_drawn_over_the_whole_run_is_an_error(self):
        mon = ob.BurnMonitor(1)
        self.feed(mon, [DECODING, INGEST_NONE_DRAWN])
        self.assertIsNone(mon.end(), "one batch drawing none is no error")
        problem = mon.verdict()
        self.assertTrue(problem.startswith(
            "Buildings: the converter decoded 12 building(s) from the 12 "
            "OpenFreeMap tile(s) it was given but drew none onto the 1 "
            "terrain tile(s) it wrote"), problem)
        self.assertEqual(mon.nothing_drawn(), problem)
        # A later batch of the same run draws: the run is fine.
        mon.begin(1)
        self.feed(mon, [DECODING, INGEST_DRAWN])
        mon.end()
        self.assertIsNone(mon.verdict())

    def test_download_pass_lines(self):
        mon = ob.BurnMonitor(1, what="download")
        self.feed(mon, ["[Download] 100% (9/9) — 7.4 MB/s, 0 errors, 0 "
                        "in-flight, window 8", DOWNLOAD_DRAWN, STATS])
        self.assertIsNone(mon.end())
        self.assertIsNone(mon.verdict())
        self.assertEqual((mon.decoded, mon.raised, mon.tiles), (12, 3835, 1))
        none = ob.BurnMonitor(1, what="download")
        self.feed(none, [DOWNLOAD_NONE_DRAWN, STATS])
        none.end()
        self.assertIn("drew none onto the 1 terrain tile(s)", none.verdict())
        # The undecodable tile of a download: the warning comes in the
        # post-pass, right before the burn line.
        bad = ob.BurnMonitor(1, what="download")
        self.assertEqual(self.feed(bad, [WARN, DOWNLOAD_DRAWN]),
                         [False, True])
        self.assertEqual(bad.bad_tiles[0][0], "14_0_0.pbf")

    def test_a_silent_download_pass_and_a_repair_pass(self):
        mon = ob.BurnMonitor(1, what="download")
        self.feed(mon, [STATS])
        msg = mon.end(ok=True)
        self.assertIn("no '[Buildings] … pbf tile(s)' line", msg)
        self.assertIn("does not apply buildings_pbf_dir to downloads", msg)
        self.assertEqual(mon.verdict(), msg)
        # A repair pass reports only when it patched pixels.
        repair = ob.BurnMonitor(0, what="download")
        self.feed(repair, [STATS])
        self.assertIsNone(repair.end())
        repair.begin(0, what="download")
        repair.feed("[Buildings] repair: 12 building(s), 40 repaired px raised")
        self.assertEqual((repair.reports, repair.raised), (1, 40))

    def test_a_report_this_monitor_cannot_read_is_never_judged(self):
        mon = ob.BurnMonitor(1)
        mon.feed("[Info] buildings_pbf_dir: 12 tile(s), 12 building(s), "
                 "a format of a later converter")
        self.assertIsNone(mon.end())
        self.assertEqual(mon.unreadable, 1)
        self.assertIsNone(mon.verdict())

    def test_the_wait_for_the_marker_is_no_burn_report(self):
        # aether_converter 0.2.10, a job with buildings_pbf_ready whose
        # marker came 1.8 s after the terrain (probed, 2026-10-23).
        mon = ob.BurnMonitor(1, what="download")
        wait = ("[Buildings] waiting for the building tiles "
                "(/c/views/103e/_ready)")
        ready = "[Buildings] building tiles ready after 1.8 s"
        self.assertEqual(self.feed(mon, [wait, ready]), [False, False])
        self.assertEqual((mon.reports, mon.waits, mon.waited_s),
                         (0, 1, 1.8))
        self.assertEqual(mon.last_kind, "wait")
        # Without the burn line that follows, the pass is still silent.
        self.assertIn("does not apply buildings_pbf_dir", mon.end())
        mon.begin(1, what="download")
        self.feed(mon, [wait, ready, DOWNLOAD_DRAWN, STATS])
        self.assertIsNone(mon.end())
        self.assertEqual((mon.reports, mon.waited_s), (1, 3.6))

    def test_the_listed_decode_header_is_a_decoding_line(self):
        # aether_converter 0.2.14, jobs with buildings_pbf_list (CONTRACT
        # changelog 51): the wait names the LIST, the decode header names
        # the listed count; the progress and end lines are unchanged.
        from waveshed.core import run_progress as rp
        mon = ob.BurnMonitor(1)
        wait = ("[Buildings] waiting for the building tiles "
                "(/c/views/77ab/3f0c.txt)")
        ready = "[Buildings] building tiles ready after 4.2 s"
        step = "[Rust] Decoding building tiles: 1000/1500"
        decoded = ("[Rust] Building tiles decoded: 1500 tile(s), 12 "
                   "building(s) in 0.4 s")
        kinds = []
        for line in (wait, ready, LISTED_DECODING, step, decoded,
                     INGEST_DRAWN, TILE_LINE):
            self.assertFalse(mon.feed(line), line)
            kinds.append(mon.last_kind)
        self.assertEqual(kinds, ["wait", "wait", "decoding", "decoding",
                                 None, "report", None])
        self.assertEqual((mon.waits, mon.batch_waited_s, mon.reports),
                         (1, 4.2, 1))
        self.assertIsNone(mon.end(tiles=1))
        self.assertTrue(ob.LISTED_DECODING_RE.match(
            '[Rust] Decoding 3 listed building tiles from "/s" (once for '
            'the run)...'))
        # The header moves no bar (the Map Converter's decode share is
        # moved by the n/m lines and the end line, read as before).
        self.assertIsNone(rp.parse_building_decode(LISTED_DECODING))
        self.assertIsNone(rp.parse_buildings_decoded(LISTED_DECODING))
        self.assertEqual(rp.parse_building_decode(step), (1000, 1500))
        self.assertEqual(rp.parse_buildings_decoded(decoded), (1500, 12, 0.4))
        # A decode warning after it still kills at the next line.
        mon.begin(1)
        self.assertEqual(self.feed(mon, [LISTED_DECODING, WARN, step,
                                         INGEST_DRAWN]),
                         [False, False, False, True])

    def test_the_seconds_the_current_run_waited(self):
        # An ingest (aether_converter 0.2.12, CONTRACT item 48) waits like a
        # download: the Map Converter's end-of-batch line says how long
        # THIS batch waited (batch_waited_s), the run sums them (waited_s).
        mon = ob.BurnMonitor(1)
        self.assertEqual(mon.batch_waited_s, 0.0)
        self.feed(mon, ["[Buildings] waiting for the building tiles "
                        "(/c/views/9a/_ready)",
                        "[Buildings] building tiles ready after 2.5 s",
                        INGEST_DRAWN])
        self.assertIsNone(mon.end())
        self.assertEqual((mon.batch_waited_s, mon.waited_s), (2.5, 2.5))
        mon.begin(1)                        # the next batch: no wait
        self.assertEqual(mon.batch_waited_s, 0.0)
        self.feed(mon, [INGEST_DRAWN])
        self.assertIsNone(mon.end())
        self.assertEqual((mon.batch_waited_s, mon.waited_s), (0.0, 2.5))
        mon.begin(1)
        self.feed(mon, ["[Buildings] building tiles ready after 0.7 s",
                        INGEST_DRAWN])
        self.assertEqual((mon.batch_waited_s, mon.waited_s), (0.7, 3.2))
        self.assertEqual(mon.reports, 3)


# ---------------------------------------------------------------------------
# The store, the strict fetch, the view
# ---------------------------------------------------------------------------

class StoreFetchView(unittest.TestCase):

    def setUp(self):
        self.root = _scratch(self)
        patch = mock.patch.object(ta, "get_cache_dir", return_value=self.root)
        patch.start()
        self.addCleanup(patch.stop)
        self.store = ta.osm_buildings_store_dir()
        self.tiles = [(14, x, 5737) for x in (8580, 8581, 8582)]

    def test_ensure_tiles_fetches_into_the_store_and_says_so(self):
        logged = []
        with OfmServer(default=building_tile([HOUSE])) as srv, \
                mock.patch.object(ofm, "TILEJSON_URL", srv.tilejson_url):
            res = ob.ensure_tiles(self.tiles, log=logged.append)
            again = ob.ensure_tiles(self.tiles)
            asked = len(srv.requests)
        names = sorted(ofm.tile_filename(*t) for t in self.tiles)
        self.assertEqual(list(res.present), names)
        self.assertEqual(sorted(os.listdir(self.store)), names)
        self.assertEqual((again.cached, again.fetched), (3, 0))
        self.assertEqual(asked, 3, "a stored tile was fetched again")
        text = "\n".join(logged)
        self.assertIn("  buildings: http://127.0.0.1:", text)   # TileJSON
        self.assertIn("  buildings: 0 tile(s) already in the cache, 3 to "
                      "fetch (32 at a time) → " + self.store, text)
        self.assertIn("  buildings: 100% (3/3 tiles, ", text)

    def test_no_data_anywhere_raises_unless_the_caller_decides(self):
        with OfmServer(default=None) as srv, \
                mock.patch.object(ofm, "TILEJSON_URL", srv.tilejson_url):
            with self.assertRaises(ofm.FetchFailed) as cm:
                ob.ensure_tiles(self.tiles)
            self.assertEqual(cm.exception.cause, "no tile with data")
            res = ob.ensure_tiles(self.tiles, require_data=False)
        self.assertEqual((res.present, res.empty), ((), 3))

    def test_a_failing_tile_raises_with_the_cause(self):
        bad = self.tiles[1]
        with OfmServer(default=building_tile([HOUSE]),
                       fail={bad[1:]: 500}) as srv, \
                mock.patch.object(ofm, "TILEJSON_URL", srv.tilejson_url), \
                mock.patch.object(ofm, "RETRY_BACKOFF_S", (0.0, 0.0, 0.0)):
            with self.assertRaises(ofm.FetchFailed) as cm:
                ob.ensure_tiles(self.tiles)
        self.assertIn(f"tile {ofm.tile_filename(*bad)}: HTTP 500",
                      str(cm.exception))

    def test_batch_view_holds_exactly_the_names(self):
        os.makedirs(self.store)
        names = [ofm.tile_filename(*t) for t in self.tiles]
        for n in names:
            ofm._write_tile(self.store, n, gzip.compress(building_tile([HOUSE])))
        self.assertIsNone(ob.batch_view([]))
        view = ob.batch_view(names[:2])
        self.assertEqual(view, ta.osm_buildings_view_dir(names[:2]))
        self.assertTrue(view.startswith(os.path.join(self.root, "views")))
        self.assertEqual(sorted(os.listdir(view)), sorted(names[:2]))
        for n in names[:2]:
            self.assertTrue(os.path.samefile(os.path.join(view, n),
                                             os.path.join(self.store, n)))
        self.assertEqual(ob.batch_view(list(reversed(names[:2]))), view)
        with self.assertRaises(RuntimeError):
            ob.batch_view(["14_1_1.pbf"])      # not in the store

    def test_a_view_named_before_the_fetch_and_its_marker(self):
        # Site Analysis's download view: named from the tiles NEEDED (its
        # own key), filled when the fetch is done, the marker LAST.
        os.makedirs(self.store)
        names = [ofm.tile_filename(*t) for t in self.tiles]
        for n in names[:2]:
            ofm._write_tile(self.store, n, b"x")
        view = ta.osm_buildings_view_dir(names, marker=ob.READY_MARKER)
        self.assertNotEqual(view, ta.osm_buildings_view_dir(names))
        os.makedirs(view)
        with open(os.path.join(view, ob.READY_MARKER), "w") as fh:
            fh.write("left by an earlier run")
        self.assertTrue(ob.remove_ready_marker(view))
        self.assertFalse(os.path.lexists(os.path.join(view, ob.READY_MARKER)))
        self.assertTrue(ob.remove_ready_marker(view))        # none: fine
        self.assertEqual(ob.batch_view(names[:2], view=view), view)
        self.assertEqual(sorted(os.listdir(view)), sorted(names[:2]))
        marker = ob.write_ready_marker(view)
        self.assertEqual(marker, os.path.join(view, "_ready"))
        self.assertTrue(os.path.isfile(marker))
        self.assertEqual(sorted(os.listdir(view)),
                         sorted(names[:2] + ["_ready"]))   # no temp file
        self.assertIsNone(ob.UNDECODABLE_RE.search(ob.READY_MARKER))
        # A relink makes the view hold exactly the names: the marker too
        # goes (a stranger) — the caller writes it after the last link.
        self.assertEqual(ob.batch_view([], view=view), view)
        self.assertEqual(os.listdir(view), [])

    def test_ready_view_and_complete_view(self):
        # The shared pair both tabs launch a waiting converter with: the
        # view named from the tiles NEEDED, made, an old marker removed —
        # then exactly the tiles with data linked and the marker LAST.
        os.makedirs(self.store)
        names = [ofm.tile_filename(*t) for t in self.tiles]
        for n in names[:2]:
            ofm._write_tile(self.store, n, b"x")
        expected = ta.osm_buildings_view_dir(names, marker=ob.READY_MARKER)
        os.makedirs(expected)
        with open(os.path.join(expected, ob.READY_MARKER), "w") as fh:
            fh.write("left by an earlier run")
        view = ob.ready_view(names)
        self.assertEqual(view, expected)
        self.assertEqual(os.listdir(view), [])
        seen = []
        real_write = ob.write_ready_marker

        def write(v, marker=ob.READY_MARKER):
            seen.append(sorted(os.listdir(v)))      # what is there before it
            return real_write(v, marker)

        with mock.patch.object(ob, "write_ready_marker", side_effect=write):
            marker = ob.complete_view(view, names[:2])
        self.assertEqual(marker, os.path.join(view, ob.READY_MARKER))
        self.assertEqual(seen, [sorted(names[:2])])
        self.assertEqual(sorted(os.listdir(view)),
                         sorted(names[:2] + [ob.READY_MARKER]))
        # A marker that cannot be removed: no view to launch with.
        with mock.patch.object(ob, "remove_ready_marker", return_value=False):
            self.assertIsNone(ob.ready_view(names))
        with mock.patch.object(ta, "ensure_engine_dirs",
                               side_effect=RuntimeError("no dir")):
            self.assertIsNone(ob.ready_view(names))
        with self.assertRaises(RuntimeError):
            ob.complete_view(view, ["14_1_1.pbf"])     # not in the store

    def test_drop_undecodable_leaves_store_and_views(self):
        os.makedirs(self.store)
        names = [ofm.tile_filename(*t) for t in self.tiles]
        for n in names:
            ofm._write_tile(self.store, n, b"x")
        view = ob.batch_view(names)
        msg = ob.drop_undecodable([(names[0], "PBF decode error: x")])
        self.assertEqual(msg, f"Buildings: the converter could not decode 1 "
                              f"OpenFreeMap tile(s) (first: {names[0]}: PBF "
                              f"decode error: x). They were removed from the "
                              f"cache — run again to download them anew.")
        self.assertNotIn(names[0], os.listdir(self.store))
        self.assertNotIn(names[0], os.listdir(view))
        self.assertIn(names[1], os.listdir(view))

    # -- the list (aether_converter >= 0.2.14, CONTRACT changelog 51) ------
    def test_the_list_path_is_its_own_key_in_a_cache_entry(self):
        # Named from the tiles a run NEEDS (order-free), never a view
        # directory's name, in ONE md5-named directory under <cache>/views.
        names = [ofm.tile_filename(*t) for t in self.tiles]
        path = ta.osm_buildings_list_path(names)
        self.assertTrue(os.path.isabs(path))
        self.assertEqual(path, ta.osm_buildings_list_path(names[::-1]))
        self.assertNotEqual(path, ta.osm_buildings_list_path(names[:2]))
        lists = ta.osm_buildings_list_dir()
        self.assertEqual(os.path.dirname(path), lists)
        self.assertEqual(os.path.dirname(lists),
                         os.path.join(self.root, "views"))
        self.assertRegex(os.path.basename(lists), r"^[0-9a-f]{32}$")
        stem, ext = os.path.splitext(os.path.basename(path))
        self.assertEqual(ext, ".txt")
        self.assertRegex(stem, r"^[0-9a-f]{32}$")
        views = {os.path.basename(ta.osm_buildings_view_dir(names)),
                 os.path.basename(ta.osm_buildings_view_dir(
                     names, marker=ob.READY_MARKER))}
        self.assertFalse({stem, os.path.basename(lists)} & views)

    def test_ready_list_and_complete_list(self):
        # ready_list: the directory made, a list an earlier run left
        # removed (it is the ready signal); complete_list: exactly the
        # names, sorted, each once, "\n", written to a temp file next to it
        # and renamed onto the name — the converter never sees half a list.
        names = [ofm.tile_filename(*t) for t in self.tiles]
        path = ta.osm_buildings_list_path(names)
        lists = os.path.dirname(path)
        os.makedirs(lists)
        with open(path, "w") as fh:
            fh.write("14_1_1.pbf\n")
        self.assertFalse(os.path.isdir(self.store))
        self.assertEqual(ob.ready_list(names), path)
        self.assertFalse(os.path.lexists(path))
        # THE store too: the job's buildings_pbf_dir, there at the launch.
        self.assertTrue(os.path.isdir(self.store))
        self.assertEqual(ob.ready_list(names), path)        # none: fine
        renames = []
        real_replace = ta.replace_with_retry

        def replace(src, dst, *a, **kw):
            with open(src, "rb") as fh:
                renames.append((os.path.dirname(src),
                                os.path.basename(src), dst, fh.read(),
                                os.path.lexists(dst)))
            return real_replace(src, dst, *a, **kw)

        with mock.patch.object(ta, "replace_with_retry", side_effect=replace):
            self.assertEqual(ob.complete_list(
                path, [names[1], names[0], names[1]]), path)
        want = "".join(f"{n}\n" for n in sorted(names[:2])).encode()
        (renamed,) = renames
        self.assertEqual(renamed[0], lists)
        self.assertRegex(renamed[1], r"^\..*\.tmp$")
        self.assertEqual(renamed[2:], (path, want, False))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), want)
        self.assertEqual(read_building_list(path), sorted(names[:2]))
        self.assertEqual(os.listdir(lists), [os.path.basename(path)])
        # No tile with data: an empty list (the converter burns nothing).
        ob.complete_list(path, [])
        self.assertEqual(os.path.getsize(path), 0)
        # A list that cannot be written: RuntimeError naming it, no temp.
        with mock.patch.object(ta, "replace_with_retry",
                               side_effect=PermissionError(
                                   13, "Permission denied")):
            with self.assertRaises(RuntimeError) as cm:
                ob.complete_list(path, names)
        self.assertEqual(str(cm.exception),
                         f"Building tile list {path} could not be written "
                         f"([Errno 13] Permission denied)")
        self.assertEqual(os.listdir(lists), [os.path.basename(path)])
        # A directory that cannot be made: no list to launch with.
        with mock.patch.object(ta, "ensure_engine_dirs",
                               side_effect=RuntimeError("no dir")):
            self.assertIsNone(ob.ready_list(names))

    def test_a_list_that_cannot_be_removed_is_none(self):
        # Something at the list's path os.remove refuses (a directory
        # here): no list to launch with — the caller takes the view.
        names = [ofm.tile_filename(*t) for t in self.tiles]
        path = ta.osm_buildings_list_path(names)
        os.makedirs(path)
        self.assertFalse(ob.remove_building_list(path))
        self.assertIsNone(ob.ready_list(names))
        self.assertTrue(ob.remove_building_list(path + ".none"))

    def test_clear_terrain_cache_takes_the_lists(self):
        # Measured, not assumed (2026-11-07): cache_entries lists md5-named
        # DIRECTORIES only — a list file straight in <cache>/views survived
        # clear_cache; the lists' one md5 directory is an entry.
        names = [ofm.tile_filename(*t) for t in self.tiles]
        path = ob.complete_list(ob.ready_list(names), names)
        self.assertIn(ta.osm_buildings_list_dir(), ta.cache_entries())
        ta.clear_cache()
        self.assertFalse(os.path.lexists(path))
        self.assertFalse(os.path.lexists(ta.osm_buildings_list_dir()))

    def test_drop_undecodable_with_lists_present(self):
        # The store loses the tile; a list needs no change (a run removes
        # its list before it writes it anew from the store's tiles).
        os.makedirs(self.store)
        names = [ofm.tile_filename(*t) for t in self.tiles]
        for n in names:
            ofm._write_tile(self.store, n, b"x")
        path = ob.complete_list(ob.ready_list(names), names)
        view = ob.batch_view(names)
        ob.drop_undecodable([(names[0], "PBF decode error: x")])
        self.assertNotIn(names[0], os.listdir(self.store))
        self.assertNotIn(names[0], os.listdir(view))
        self.assertEqual(read_building_list(path), sorted(names))


# ---------------------------------------------------------------------------
# The fetch thread both tabs run (BuildingsFetch)
# ---------------------------------------------------------------------------

class _HoldFetch:
    """``openfreemap.fetch_building_tiles`` stand-in: a call's tiles land
    in the store (with data where *data(name)*); a call naming *fail*
    raises the strict fetch's FetchFailed for it; one naming a tile of
    *hold* waits for :attr:`release` (Stop seen)."""

    def __init__(self, hold=(), fail=None, data=lambda _n: True):
        self.hold = set(hold)
        self.fail = fail
        self.data = data
        self.release = threading.Event()
        self.holding = threading.Event()
        self.calls = []

    def __call__(self, tiles, store, template, should_cancel=None,
                 on_start=None, on_progress=None, **_kw):
        names = [ofm.tile_filename(*t) for t in tiles]
        self.calls.append(names)
        if self.hold & set(names):
            self.holding.set()
            while not self.release.wait(0.02):
                if should_cancel is not None and should_cancel():
                    raise ofm.FetchCancelled()
        if self.fail in names:
            raise ofm.FetchFailed(
                f"1 of {len(names)} building tiles missing — tile "
                f"{self.fail}: HTTP 500", missing=1, total=len(names),
                cause=f"{self.fail}: HTTP 500")
        os.makedirs(store, exist_ok=True)
        present = []
        for k, name in enumerate(names, start=1):
            if self.data(name):
                ofm._write_tile(store, name, b"\x1f\x8bpbf")
                present.append(name)
            if on_progress is not None:
                on_progress(k, len(names), 5 * k)
        return ofm.FetchResult(len(names), 0, len(present),
                               len(names) - len(present), 5 * len(present),
                               0.0, tuple(sorted(present)))


class FetchThread(unittest.TestCase):
    """:class:`osm_buildings.BuildingsFetch`, GUI-free: the Map Converter's
    branch and Site Analysis's download run it."""

    def setUp(self):
        self.root = _scratch(self)
        for patch in (mock.patch.object(ta, "get_cache_dir",
                                        return_value=self.root),
                      mock.patch.object(ofm, "resolve_tile_template_strict",
                                        return_value="http://x/{z}/{x}/{y}"
                                                     ".pbf")):
            patch.start()
            self.addCleanup(patch.stop)
        self.a = [(14, x, 5737) for x in (8580, 8581)]
        self.b = [(14, x, 5738) for x in (8580, 8581)]
        self.names_a = [ofm.tile_filename(*t) for t in self.a]
        self.names_b = [ofm.tile_filename(*t) for t in self.b]

    def fetch(self, fake, groups, **kw):
        patch = mock.patch.object(ofm, "fetch_building_tiles",
                                  side_effect=fake)
        patch.start()
        self.addCleanup(patch.stop)
        f = ob.BuildingsFetch(groups, **kw)
        self.addCleanup(lambda: f.alive() and f.join(10))
        return f

    def test_the_map_converter_branch_is_this_thread(self):
        self.assertTrue(issubclass(mct._BuildingsBranch, ob.BuildingsFetch))

    def test_groups_in_order_each_announced_when_it_is_in(self):
        fake = _HoldFetch(hold=self.names_b)
        logged, progress = [], []
        f = self.fetch(fake, [self.a, self.b + self.a], log=logged.append,
                       on_progress=lambda *a: progress.append(a[:3]))
        self.assertEqual(f.tiles, self.a + self.b)   # with its first group
        f.start()
        self.assertTrue(fake.holding.wait(10))
        f.wait_for(self.names_a)                     # A's are in: no wait
        self.assertTrue(f.alive())
        self.assertFalse(f.ready_for(self.names_b))
        waiter = threading.Thread(target=f.wait_for, args=(self.names_b,))
        waiter.start()
        time.sleep(0.2)
        self.assertTrue(waiter.is_alive(), "B did not wait for its tiles")
        fake.release.set()
        waiter.join(10)
        f.join(10)
        self.assertFalse(waiter.is_alive() or f.alive())
        self.assertEqual((f.error, f.cancelled), (None, False))
        self.assertEqual(fake.calls, [self.names_a, self.names_b])
        self.assertEqual(f.result.present,
                         tuple(sorted(self.names_a + self.names_b)))
        # The whole fetch's counts, also across the groups.
        self.assertEqual(progress, [(1, 4, 5), (2, 4, 10), (3, 4, 15),
                                    (4, 4, 20)])
        text = "\n".join(logged)
        self.assertIn("  buildings: http://x/{z}/{x}/{y}.pbf", text)
        self.assertIn("  buildings: 0 tile(s) already in the cache, 4 to "
                      "fetch (32 at a time) → ", text)
        self.assertIn("  buildings: 100% (4/4 tiles, ", text)
        # Stored tiles cost nothing the next time.
        again = self.fetch(_HoldFetch(), [self.a, self.b])
        again.start()
        again.join(10)
        self.assertEqual((again.result.cached, again.result.fetched), (4, 0))

    def test_on_fetched_comes_before_the_last_names_and_on_end_last(self):
        events = []
        f = self.fetch(
            _HoldFetch(), [self.a, self.b],
            on_begin=lambda: events.append("begin"),
            on_start=lambda cached, todo: events.append(("start", todo)),
            on_fetched=lambda res: events.append(
                ("fetched", len(res.present), f.ready_for(self.names_b),
                 f.ready_for(self.names_a))),
            on_end=lambda: events.append(("end",
                                          f.ready_for(self.names_b))))
        f.start()
        f.join(10)
        self.assertEqual(events, ["begin", ("start", 4),
                                  ("fetched", 4, False, True), ("end", True)])
        self.assertIsNotNone(f.started)
        self.assertGreaterEqual(f.ended, f.started)

    def test_a_failure_is_the_error_in_the_whole_fetchs_numbers(self):
        store = ta.osm_buildings_store_dir()
        for fail, missing in ((self.names_b[0], 1), (self.names_a[0], 3)):
            shutil.rmtree(store, ignore_errors=True)    # nothing cached
            seen = []
            f = self.fetch(_HoldFetch(fail=fail), [self.a, self.b],
                           failure_text=lambda c: f"failed: {c}",
                           on_failed=lambda c: seen.append((c, f.error)))
            f.start()
            f.join(10)
            cause = (f"{missing} of 4 building tiles missing — tile {fail}: "
                     f"HTTP 500")
            self.assertEqual((f.cause, f.error), (cause, f"failed: {cause}"))
            self.assertEqual(seen, [(cause, f"failed: {cause}")])
            self.assertTrue(f.ready_for({"other.pbf"}))   # waiters released
            self.assertFalse(f.cancelled)
        shutil.rmtree(store, ignore_errors=True)
        default = self.fetch(_HoldFetch(fail=self.names_a[0]), [self.a])
        default.start()
        default.join(10)
        self.assertEqual(default.error,
                         f"Buildings download failed (OpenFreeMap z14): 1 of "
                         f"2 building tiles missing — tile {self.names_a[0]}: "
                         f"HTTP 500.")

    def test_a_failing_on_fetched_fails_the_fetch_in_its_own_words(self):
        seen = []

        def fetched(_res):
            raise RuntimeError("Buildings: 14_8580_5737.pbf could not be "
                               "linked")
        f = self.fetch(_HoldFetch(), [self.a], on_fetched=fetched,
                       on_failed=seen.append)
        f.start()
        f.join(10)
        self.assertEqual(f.error, "Buildings: 14_8580_5737.pbf could not be "
                                  "linked")
        self.assertEqual(seen, [f.error])

    def test_stop_is_quiet(self):
        stop = threading.Event()
        failed = []
        fake = _HoldFetch(hold=self.names_b)
        f = self.fetch(fake, [self.a, self.b], should_cancel=stop.is_set,
                       on_failed=failed.append)
        f.start()
        self.assertTrue(fake.holding.wait(10))
        waiter = threading.Thread(target=f.wait_for, args=(self.names_b,))
        waiter.start()
        stop.set()
        f.join(10)
        waiter.join(10)
        self.assertFalse(waiter.is_alive() or f.alive())
        self.assertEqual((f.cancelled, f.error, failed), (True, None, []))
        self.assertIsNone(f.result)

    def test_no_data_anywhere(self):
        none = _HoldFetch(data=lambda _n: False)
        strict = self.fetch(none, [self.a, self.b])
        strict.start()
        strict.join(10)
        self.assertIn("none of the 4 building tiles has data", strict.error)
        # Site Analysis decides itself (a warning, the download released).
        lenient = self.fetch(none, [self.a, self.b], require_data=False)
        self.assertFalse(lenient.ready_for(self.names_a))
        lenient.start()
        lenient.join(10)
        self.assertIsNone(lenient.error)
        self.assertEqual(lenient.result.present, ())
        self.assertTrue(lenient.ready_for(self.names_a))

    # -- releasing a converter that waits for its view's marker ------------
    def _view(self, names):
        view = ob.ready_view(sorted(names))
        self.assertIsNotNone(view)
        return view

    def _record_marker_writes(self):
        """Patch write_ready_marker: (thread name, view listing) per write."""
        seen = []
        real_write = ob.write_ready_marker

        def write(v, marker=ob.READY_MARKER):
            seen.append((threading.current_thread().name,
                         sorted(os.listdir(v))))
            return real_write(v, marker)

        patch = mock.patch.object(ob, "write_ready_marker", side_effect=write)
        patch.start()
        self.addCleanup(patch.stop)
        return seen

    def test_a_view_whose_names_are_in_is_linked_and_marked_at_once(self):
        f = self.fetch(_HoldFetch(), [self.a, self.b])
        f.start()
        f.join(10)
        seen = self._record_marker_writes()
        view = self._view(self.names_a)
        linked = []
        now = f.link_when_ready(self.names_a, view, lambda: self.names_a,
                                on_linked=lambda v, n: linked.append(
                                    (v, n, threading.current_thread().name)))
        self.assertTrue(now)
        self.assertEqual(seen, [(threading.current_thread().name,
                                 sorted(self.names_a))])
        self.assertEqual(linked, [(view, 2, threading.current_thread().name)])
        self.assertEqual(sorted(os.listdir(view)),
                         sorted(self.names_a + [ob.READY_MARKER]))

    def test_a_pending_view_is_completed_on_the_fetch_thread(self):
        # B's tiles are still being fetched: the registration returns at
        # once, the fetch thread links B's tiles right after they are in and
        # THEN writes the marker — a name the fetch never had waits for the
        # fetch's end.
        fake = _HoldFetch(hold=self.names_b)
        f = self.fetch(fake, [self.a, self.b])
        seen = self._record_marker_writes()
        f.start()
        self.assertTrue(fake.holding.wait(10))
        view_b = self._view(self.names_b)
        view_x = self._view(self.names_a + ["14_1_1.pbf"])
        linked = []

        def on_linked(v, n):
            linked.append((v, n, threading.current_thread().name))

        self.assertFalse(f.link_when_ready(self.names_b, view_b,
                                           lambda: self.names_b,
                                           on_linked=on_linked))
        self.assertFalse(f.link_when_ready(
            self.names_a + ["14_1_1.pbf"], view_x, lambda: self.names_a,
            on_linked=on_linked))
        time.sleep(0.2)
        self.assertEqual(seen, [])
        self.assertFalse(os.path.lexists(os.path.join(view_b,
                                                      ob.READY_MARKER)))
        fake.release.set()
        f.join(10)
        self.assertIsNone(f.error)
        self.assertEqual(seen, [("waveshed-buildings", sorted(self.names_b)),
                                ("waveshed-buildings", sorted(self.names_a))])
        self.assertEqual(linked, [(view_b, 2, "waveshed-buildings"),
                                  (view_x, 2, "waveshed-buildings")])
        self.assertTrue(os.path.isfile(os.path.join(view_b, ob.READY_MARKER)))

    def test_with_require_data_a_view_waits_until_some_tile_has_data(self):
        # A's tiles have no data: its view is not released before B's (with
        # data) are in — a fetch where none has data must fail first.
        fake = _HoldFetch(hold=self.names_b,
                          data=lambda n: n in self.names_b)
        f = self.fetch(fake, [self.a, self.b])
        f.start()
        self.assertTrue(fake.holding.wait(10))
        view_a = self._view(self.names_a)
        self.assertFalse(f.link_when_ready(self.names_a, view_a, lambda: []))
        time.sleep(0.2)
        self.assertFalse(os.path.lexists(os.path.join(view_a,
                                                      ob.READY_MARKER)))
        fake.release.set()
        f.join(10)
        self.assertIsNone(f.error)
        self.assertEqual(os.listdir(view_a), [ob.READY_MARKER])

    def test_a_failed_or_stopped_fetch_never_writes_the_marker(self):
        fake = _HoldFetch(hold=self.names_b, fail=self.names_b[0])
        f = self.fetch(fake, [self.a, self.b])
        seen = self._record_marker_writes()
        f.start()
        self.assertTrue(fake.holding.wait(10))
        view_b = self._view(self.names_b)
        self.assertFalse(f.link_when_ready(self.names_b, view_b,
                                           lambda: self.names_b))
        fake.release.set()
        f.join(10)
        self.assertIsNotNone(f.error)
        ran = []
        self.assertFalse(f.when_ready(self.names_a, lambda: ran.append(1)))
        self.assertEqual((seen, ran), ([], []))
        self.assertEqual(os.listdir(view_b), [])
        # Stopped: the same.
        stop = threading.Event()
        fake2 = _HoldFetch(hold=self.names_b)
        g = self.fetch(fake2, [self.a, self.b], should_cancel=stop.is_set)
        g.start()
        self.assertTrue(fake2.holding.wait(10))
        self.assertFalse(g.link_when_ready(self.names_b, view_b,
                                           lambda: self.names_b))
        stop.set()
        g.join(10)
        self.assertTrue(g.cancelled)
        self.assertEqual(seen, [])
        self.assertEqual(os.listdir(view_b), [])

    def test_a_view_that_cannot_be_completed_fails_the_fetch(self):
        # On the fetch thread: the fetch's error in its own words, on_failed
        # told; in the caller's thread: raised.
        failed = []
        fake = _HoldFetch(hold=self.names_b)
        f = self.fetch(fake, [self.a, self.b], on_failed=failed.append)
        f.start()
        self.assertTrue(fake.holding.wait(10))
        view = self._view(self.names_b)
        self.assertFalse(f.link_when_ready(self.names_b, view,
                                           lambda: ["14_1_1.pbf"]))
        fake.release.set()
        f.join(10)
        self.assertTrue(f.error.startswith("Buildings: Building tile "
                                           "14_1_1.pbf could not be linked "
                                           "into "), f.error)
        self.assertEqual(failed, [f.error])
        self.assertFalse(os.path.lexists(os.path.join(view, ob.READY_MARKER)))
        done = self.fetch(_HoldFetch(), [self.a])
        done.start()
        done.join(10)
        with self.assertRaises(RuntimeError) as cm:
            done.link_when_ready(self.names_a, self._view(self.names_a),
                                 lambda: ["14_1_1.pbf"])
        self.assertTrue(str(cm.exception).startswith("Buildings: "))

    # -- releasing a converter that waits for its LIST (>= 0.2.14) ---------
    def _list(self, names):
        path = ob.ready_list(sorted(names))
        self.assertIsNotNone(path)
        return path

    def _record_list_writes(self):
        """Patch write_building_list: (thread name, names, the list there
        before) per write."""
        seen = []
        real_write = ob.write_building_list

        def write(path, names):
            names = list(names)
            seen.append((threading.current_thread().name, names,
                         os.path.lexists(path)))
            return real_write(path, names)

        patch = mock.patch.object(ob, "write_building_list",
                                  side_effect=write)
        patch.start()
        self.addCleanup(patch.stop)
        return seen

    def _record_links(self):
        """Patch os.link: every destination (nothing may be linked)."""
        links = []
        real_link = os.link

        def link(src, dst, *a, **kw):
            links.append(dst)
            return real_link(src, dst, *a, **kw)

        patch = mock.patch.object(os, "link", side_effect=link)
        patch.start()
        self.addCleanup(patch.stop)
        return links

    def test_a_list_whose_names_are_in_is_written_at_once(self):
        f = self.fetch(_HoldFetch(), [self.a, self.b])
        f.start()
        f.join(10)
        seen = self._record_list_writes()
        links = self._record_links()
        path = self._list(self.names_a)
        listed = []
        now = f.list_when_ready(self.names_a, path, lambda: self.names_a,
                                on_listed=lambda p, n: listed.append(
                                    (p, n, threading.current_thread().name)))
        me = threading.current_thread().name
        self.assertTrue(now)
        self.assertEqual(seen, [(me, sorted(self.names_a), False)])
        self.assertEqual(listed, [(path, 2, me)])
        self.assertEqual(read_building_list(path), sorted(self.names_a))
        self.assertEqual(links, [])
        self.assertEqual(os.listdir(os.path.join(self.root, "views")),
                         [os.path.basename(os.path.dirname(path))])

    def test_a_pending_list_is_written_on_the_fetch_thread(self):
        # B's tiles are still being fetched: the registration returns at
        # once and the fetch thread writes B's list right after they are
        # in — a name the fetch never had waits for the fetch's end.
        fake = _HoldFetch(hold=self.names_b)
        f = self.fetch(fake, [self.a, self.b])
        seen = self._record_list_writes()
        links = self._record_links()
        f.start()
        self.assertTrue(fake.holding.wait(10))
        path_b = self._list(self.names_b)
        path_x = self._list(self.names_a + ["14_1_1.pbf"])
        listed = []

        def on_listed(p, n):
            listed.append((p, n, threading.current_thread().name))

        self.assertFalse(f.list_when_ready(self.names_b, path_b,
                                           lambda: self.names_b,
                                           on_listed=on_listed))
        self.assertFalse(f.list_when_ready(
            self.names_a + ["14_1_1.pbf"], path_x, lambda: self.names_a,
            on_listed=on_listed))
        time.sleep(0.2)
        self.assertEqual(seen, [])
        self.assertFalse(os.path.lexists(path_b))
        fake.release.set()
        f.join(10)
        self.assertIsNone(f.error)
        self.assertEqual(seen, [("waveshed-buildings", sorted(self.names_b),
                                 False),
                                ("waveshed-buildings", sorted(self.names_a),
                                 False)])
        self.assertEqual(listed, [(path_b, 2, "waveshed-buildings"),
                                  (path_x, 2, "waveshed-buildings")])
        self.assertEqual(read_building_list(path_b), sorted(self.names_b))
        self.assertEqual(read_building_list(path_x), sorted(self.names_a))
        self.assertEqual(links, [])

    def test_with_require_data_a_list_waits_until_some_tile_has_data(self):
        fake = _HoldFetch(hold=self.names_b,
                          data=lambda n: n in self.names_b)
        f = self.fetch(fake, [self.a, self.b])
        f.start()
        self.assertTrue(fake.holding.wait(10))
        path_a = self._list(self.names_a)
        self.assertFalse(f.list_when_ready(self.names_a, path_a, lambda: []))
        time.sleep(0.2)
        self.assertFalse(os.path.lexists(path_a))
        fake.release.set()
        f.join(10)
        self.assertIsNone(f.error)
        self.assertEqual(read_building_list(path_a), [])     # none has data

    def test_a_failed_or_stopped_fetch_never_writes_the_list(self):
        fake = _HoldFetch(hold=self.names_b, fail=self.names_b[0])
        f = self.fetch(fake, [self.a, self.b])
        seen = self._record_list_writes()
        f.start()
        self.assertTrue(fake.holding.wait(10))
        path_b = self._list(self.names_b)
        self.assertFalse(f.list_when_ready(self.names_b, path_b,
                                           lambda: self.names_b))
        fake.release.set()
        f.join(10)
        self.assertIsNotNone(f.error)
        self.assertEqual(seen, [])
        self.assertFalse(os.path.lexists(path_b))
        # Registered after the failure: dropped too.
        self.assertFalse(f.list_when_ready(self.names_a,
                                           self._list(self.names_a),
                                           lambda: self.names_a))
        self.assertEqual(seen, [])
        # Stopped: the same.
        stop = threading.Event()
        fake2 = _HoldFetch(hold=self.names_b)
        g = self.fetch(fake2, [self.a, self.b], should_cancel=stop.is_set)
        g.start()
        self.assertTrue(fake2.holding.wait(10))
        self.assertFalse(g.list_when_ready(self.names_b, path_b,
                                           lambda: self.names_b))
        stop.set()
        g.join(10)
        self.assertTrue(g.cancelled)
        self.assertEqual(seen, [])
        self.assertFalse(os.path.lexists(path_b))

    def test_a_list_that_cannot_be_written_fails_the_fetch(self):
        # On the fetch thread: the fetch's error in its own words, on_failed
        # told, no temp file left; in the caller's thread: raised.
        failed = []
        fake = _HoldFetch(hold=self.names_b)
        f = self.fetch(fake, [self.a, self.b], on_failed=failed.append)
        f.start()
        self.assertTrue(fake.holding.wait(10))
        path = self._list(self.names_b)
        refused = PermissionError(13, "Permission denied")
        with mock.patch.object(ta, "replace_with_retry", side_effect=refused):
            self.assertFalse(f.list_when_ready(self.names_b, path,
                                               lambda: self.names_b))
            fake.release.set()
            f.join(10)
        self.assertEqual(f.error, f"Buildings: Building tile list {path} "
                                  f"could not be written ([Errno 13] "
                                  f"Permission denied)")
        self.assertEqual(failed, [f.error])
        self.assertEqual(os.listdir(os.path.dirname(path)), [])
        done = self.fetch(_HoldFetch(), [self.a])
        done.start()
        done.join(10)
        with mock.patch.object(ta, "replace_with_retry",
                               side_effect=refused), \
                self.assertRaises(RuntimeError) as cm:
            done.list_when_ready(self.names_a, self._list(self.names_a),
                                 lambda: self.names_a)
        self.assertTrue(str(cm.exception).startswith(
            "Buildings: Building tile list "), str(cm.exception))


# ---------------------------------------------------------------------------
# The output manifest, schema 2
# ---------------------------------------------------------------------------

class ManifestSchema2(unittest.TestCase):

    def setUp(self):
        self.dir = _scratch(self)
        self.path = os.path.join(self.dir, ta.TILES_MANIFEST)

    def test_round_trip(self):
        ta.write_tiles_manifest(self.dir, {"a.abt": "fp-a", "b.abt": "fp-b",
                                           "c.abt": "fp-c"},
                                {"a.abt": ta.BUILDINGS_OSM,
                                 "b.abt": ta.BUILDINGS_NONE})
        with open(self.path, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc, {"schema": 2,
                               "tiles": {"a.abt": "fp-a", "b.abt": "fp-b",
                                         "c.abt": "fp-c"},
                               "buildings": {"a.abt": "openfreemap:z14",
                                             "b.abt": "none"}})
        man = ta.read_tiles_manifest(self.dir)
        self.assertEqual(man.schema, 2)
        self.assertEqual(man.tiles, doc["tiles"])
        self.assertEqual(man.buildings, doc["buildings"])
        # The Map Converter's reader: the fingerprints, as ever.
        self.assertEqual(mct._read_output_manifest(self.dir), doc["tiles"])
        # A record dropped takes its buildings record along; a rewrite
        # without one leaves the tile "unknown".
        mct._write_output_manifest(self.dir, {"a.abt": None, "b.abt": "fp-b2"})
        man = ta.read_tiles_manifest(self.dir)
        self.assertEqual(man.tiles, {"b.abt": "fp-b2", "c.abt": "fp-c"})
        self.assertEqual(man.buildings, {})

    def test_schema_1_reads_with_buildings_unknown(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"schema": 1, "tiles": {"a.abt": "fp-a"},
                       "buildings": {"a.abt": "openfreemap:z14"}}, fh)
        man = ta.read_tiles_manifest(self.dir)
        self.assertEqual((man.schema, man.tiles, man.buildings),
                         (1, {"a.abt": "fp-a"}, {}))
        self.assertEqual(mct._read_output_manifest(self.dir),
                         {"a.abt": "fp-a"})
        state = ta.prepared_buildings(self.dir, ["a.abt"])
        self.assertEqual(state.state, ta.PREPARED_UNKNOWN)
        # Written on top: schema 2, the old record kept (still unknown).
        ta.write_tiles_manifest(self.dir, {"b.abt": "fp-b"},
                                {"b.abt": ta.BUILDINGS_FILE})
        man = ta.read_tiles_manifest(self.dir)
        self.assertEqual(man.schema, 2)
        self.assertEqual(man.tiles, {"a.abt": "fp-a", "b.abt": "fp-b"})
        self.assertEqual(man.buildings, {"b.abt": "file"})

    def test_no_manifest_and_a_broken_one(self):
        self.assertEqual(ta.read_tiles_manifest(self.dir),
                         ta.TilesManifest(0, {}, {}))
        with open(self.path, "w") as fh:
            fh.write("{not json")
        self.assertEqual(ta.read_tiles_manifest(self.dir).schema, 0)

    def test_tiles_to_build_judges_the_fingerprint_only(self):
        expected = mct._enumerate_tiles(
            mct._snap_bbox({"south": 47.0, "north": 47.5, "west": 8.0,
                            "east": 8.5}, [30]), [30])
        name = expected[0][1]["filename"]
        fps = {name: "fp-1"}
        with mock.patch.object(mct, "_output_tile_whole", return_value=True):
            for rec in (ta.BUILDINGS_OSM, ta.BUILDINGS_NONE,
                        ta.BUILDINGS_UNVERIFIED, None):
                ta.write_tiles_manifest(self.dir, fps, {name: rec})
                pending, rebuild, _unknown = mct._tiles_to_build(
                    expected, self.dir, False, fps)
                self.assertEqual((pending, rebuild), ([], set()), rec)
            ta.write_tiles_manifest(self.dir, {name: "fp-0"},
                                    {name: ta.BUILDINGS_OSM})
            pending, rebuild, _unknown = mct._tiles_to_build(
                expected, self.dir, False, fps)
        self.assertEqual(rebuild, {name})


class PreparedDetection(unittest.TestCase):

    def setUp(self):
        self.dir = _scratch(self)

    def state(self, records, names=None):
        fps = {n: f"fp-{n}" for n in records}
        ta.write_tiles_manifest(self.dir, fps, records)
        return ta.prepared_buildings(self.dir, names or sorted(records))

    def test_all_none_mixed_unknown(self):
        s = self.state({"a.abt": ta.BUILDINGS_OSM,
                        "b.abt": "file+openfreemap:z14"})
        self.assertEqual((s.state, s.with_buildings, s.sources),
                         (ta.PREPARED_ALL, 2, ("file", "openfreemap:z14")))
        self.assertEqual(s.counts(), "2 with buildings (file, "
                                     "openfreemap:z14) — of 2 tile(s)")
        s = self.state({"a.abt": ta.BUILDINGS_NONE, "b.abt": "none"})
        self.assertEqual((s.state, s.without), (ta.PREPARED_NONE, 2))
        s = self.state({"a.abt": ta.BUILDINGS_OSM, "b.abt": ta.BUILDINGS_NONE},
                       ["/x/a.abt", "/x/b.abt", "/x/c.abt"])
        self.assertEqual((s.state, s.with_buildings, s.without, s.unknown),
                         (ta.PREPARED_MIXED, 1, 1, 1))
        self.assertEqual(s.counts(), "1 with buildings (openfreemap:z14), 1 "
                                     "without, 1 not recorded — of 3 tile(s)")
        s = self.state({"a.abt": ta.BUILDINGS_UNVERIFIED})
        self.assertEqual(s.state, ta.PREPARED_UNKNOWN)
        self.assertEqual(ta.prepared_buildings(self.dir, []).state,
                         ta.PREPARED_UNKNOWN)

    def test_the_log_lines(self):
        all_on = self.state({"a.abt": ta.BUILDINGS_OSM})
        self.assertEqual(ta.prepared_buildings_line(all_on, True),
                         "buildings: the prepared terrain already carries "
                         "buildings (1 with buildings (openfreemap:z14) — of "
                         "1 tile(s)) — nothing is added")
        self.assertIn("although none were requested",
                      ta.prepared_buildings_line(all_on, False))
        none = self.state({"a.abt": ta.BUILDINGS_NONE})
        self.assertTrue(ta.prepared_buildings_line(none, True).startswith(
            "WARNING: buildings were requested, but the prepared terrain "
            "carries none"))
        self.assertIsNone(ta.prepared_buildings_line(none, False))
        unknown = ta.prepared_buildings(self.dir, ["z.abt"])
        self.assertIn("is not recorded",
                      ta.prepared_buildings_line(unknown, True))


# ---------------------------------------------------------------------------
# The Map Converter: records and the run-level zero-drawn rule
# ---------------------------------------------------------------------------

#: Around the corner of the four 30 m tiles (47.5 N, 8.5 E): the row's
#: buildings reach A, B, C and D.
CORNER = {"south": 47.49, "north": 47.51, "west": 8.49, "east": 8.51}


class _NoneDrawnConverter(_PbfConverter):
    """Decodes the buildings it is given and draws none of them."""

    def __init__(self, **kw):
        super().__init__(info=False, **kw)

    def __call__(self, exe, args, on_line, on_start=None, env=None):
        with open(args[-1], encoding="utf-8") as fh:
            jobs = json.load(fh)
        for j in jobs:
            d = j.get("buildings_pbf_dir")
            if d:
                n = len(os.listdir(d))
                on_line(f"[Info] buildings_pbf_dir: {n} tile(s), {n} "
                        f"building(s) ({n} after dedup), 0 drawn, 0 pixel(s) "
                        f"raised")
        return super().__call__(exe, args, on_line, on_start, env)


class MapConverterRecords(_Case):

    def records(self):
        return ta.read_tiles_manifest(self.out).buildings

    def test_built_tiles_record_their_buildings(self):
        n = self.n
        with OfmServer(default=building_tile([HOUSE])) as srv:
            r = _OsmRun(self, srv).run()
        self.assertEqual(r.errs, [])
        man = ta.read_tiles_manifest(self.out)
        self.assertEqual(man.schema, 2)
        # A (Phase 2, the row's buildings reach it) and B, C, D (Phase 3,
        # out of reach).
        self.assertEqual(man.buildings, {
            n["A"]: "openfreemap:z14", n["B"]: "none", n["C"]: "none",
            n["D"]: "none"})
        self.assertEqual(set(man.tiles), set(n.values()))
        self.assertEqual(ta.prepared_buildings(self.out, [n["A"]]).state,
                         ta.PREPARED_ALL)
        self.assertEqual(ta.prepared_buildings(self.out, list(n.values()))
                         .state, ta.PREPARED_MIXED)

    def test_an_unverified_batch_records_unverified(self):
        n = self.n
        ext_d = {"south": 47.70, "north": 47.72, "west": 8.70, "east": 8.72}
        with OfmServer(default=building_tile([HOUSE])) as srv:
            r = _OsmRun(self, srv, osm_extent=ext_d,
                        converter=_PbfConverter(info=False)).run()
        self.assertEqual(len(r.errs), 1)
        self.assertEqual(mct._read_output_manifest(self.out)[n["D"]],
                         mct._BUILDINGS_UNVERIFIED)
        self.assertEqual(self.records()[n["D"]], ta.BUILDINGS_UNVERIFIED)

    def test_decoded_and_never_drawn_fails_the_run(self):
        n = self.n
        with OfmServer(default=building_tile([HOUSE])) as srv:
            r = _OsmRun(self, srv, osm_extent=CORNER,
                        converter=_NoneDrawnConverter()).run()
        self.assertEqual(r.oks, [])
        self.assertEqual(len(r.errs), 1, r.logs[-6:])
        self.assertIn("but drew none onto the", r.errs[0])
        self.assertIn("Move the Download Buildings… row's extent", r.errs[0])
        # Every tile it recorded with OpenFreeMap buildings is unverified:
        # the next run rebuilds them.
        fps = mct._read_output_manifest(self.out)
        for k in "ABCD":
            self.assertEqual(fps[n[k]], mct._BUILDINGS_UNVERIFIED, k)
            self.assertEqual(self.records()[n[k]], ta.BUILDINGS_UNVERIFIED)
        with OfmServer(default=building_tile([HOUSE])) as srv:
            again = _OsmRun(self, srv, osm_extent=CORNER).run()
        self.assertEqual(again.errs, [])
        rebuilt = {os.path.basename(j["output_path"])
                   for call in again.converter.calls for j in call["jobs"]}
        self.assertEqual(rebuilt, set(n.values()))

    def test_one_edge_tile_rebuilt_alone_is_not_judged(self):
        # D rebuilt alone, next to A B C on which an earlier run drew the
        # buildings: D's batch decodes them and draws none — no evidence.
        n = self.n
        with OfmServer(default=building_tile([HOUSE])) as srv:
            first = _OsmRun(self, srv, osm_extent=CORNER).run()
            self.assertEqual(first.errs, [])
            os.remove(os.path.join(self.out, n["D"]))
            r = _OsmRun(self, srv, osm_extent=CORNER,
                        converter=_NoneDrawnConverter()).run()
        self.assertEqual(r.errs, [], r.logs[-6:])
        built = {os.path.basename(j["output_path"])
                 for call in r.converter.calls for j in call["jobs"]}
        self.assertEqual(built, {n["D"]})
        self.assertTrue(any(m.startswith("  buildings: none drawn on this "
                                         "run's tiles — not judged: 3 "
                                         "tile(s)") for m in r.logs), r.logs)
        self.assertEqual(self.records()[n["D"]], "openfreemap:z14")

    def test_no_buildings_in_extent_everywhere_is_a_warning(self):
        class Empty(_PbfConverter):
            def __init__(self):
                super().__init__(info=False)

            def __call__(self, exe, args, on_line, on_start=None, env=None):
                with open(args[-1], encoding="utf-8") as fh:
                    jobs = json.load(fh)
                for j in jobs:
                    if j.get("buildings_pbf_dir"):
                        on_line("[Info] buildings_pbf_dir: 3 tile(s), no "
                                "buildings in extent")
                return super().__call__(exe, args, on_line, on_start, env)

        with OfmServer(default=building_tile([HOUSE])) as srv:
            r = _OsmRun(self, srv, converter=Empty()).run()
        self.assertEqual(r.errs, [])
        self.assertTrue(any("hold no building" in m and "WARNING" in m
                            for m in r.logs), r.logs[-6:])


if __name__ == "__main__":
    unittest.main()
