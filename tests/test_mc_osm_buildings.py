"""Map Converter: the OSM buildings row fetches OpenFreeMap z14 tiles
(2026-10-22, replaced the Overpass query).

The owner's run: ONE Overpass query for 16.6 deg² answered empty after
107 s; the worker logged "Downloaded 0 buildings from OSM." and converted
WITHOUT buildings, and Phase 1 had held the terrain download back for those
107 s. Pinned here, against a stand-in OpenFreeMap (``_ofm_server``: real
vector tiles over real HTTP) and the shipped worker:

* the run fetches the z14 tiles its pending output tiles need, into the
  Site Analysis store, and every ingest job those tiles reach carries them
  as ``buildings_pbf_dir`` (a per-batch view of exactly those tiles);
* the tile list and the terrain download do not wait for the buildings —
  only the job build of the first ingest batch does (timestamps);
* a tile the server keeps failing, an undecodable answer, or no tile with
  data at all ENDS THE RUN with the cause and the missing count, and stops
  the other branches (their engine processes are killed);
* Stop cancels the fetch (no request after it, nothing written);
* the converter's own decode warning (a poisoned cache) ends the run before
  a tile is written, and the tile leaves the cache;
* an Overpass-era stack still loads: the same placeholder row runs on
  OpenFreeMap, and tiles an Overpass-era run recorded are rebuilt where the
  buildings reach them;
* with the REAL converter (AETHER_BIN_DIR): the buildings are in the
  output pixels, byte for byte what an ingest of the same job writes, and a
  second run rebuilds and fetches nothing;
* with the REAL converter >= 0.2.12 (``AETHER_CONVERTER_0212``, toolkit
  CONTRACT item 48; skipped without): Phase 2 is launched while its
  building tiles are still being fetched, waits for its view's marker and
  writes the bytes of the sequential path (a >= 0.2.14 binary pinned to
  the view);
* with the REAL converter >= 0.2.14 (``AETHER_CONVERTER_0214``, else
  AETHER_BIN_DIR's / PATH's when it is one; ``buildings_pbf_list``,
  CONTRACT changelog 51; skipped without, a failure when the variable
  names anything else): Phase 2 reads THE store through its list — no
  view made — and writes the bytes of the view mode.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

import waveshed.core.terrain_adapter as ta
from waveshed.core import openfreemap as ofm
from waveshed.core import output_estimate as oe
import waveshed.gui.map_converter_tab as mct
from waveshed.gui.map_converter_tab import _LayerEntry, _MapConverterWorker

from _ofm_server import OfmServer, building_tile, tile_lonlat
from _real_gdal import HAVE_GDAL, real_gdal, write_tif
from test_two_phase_convert import (_Converter, _EXT, _XYZ, _HUGE, _b, _files,
                                    _find_real_converter, _info, _surface,
                                    _terrarium_png, _tiles)

#: The OSM row's extent: inside tile A (47.0-47.5 N, 8.0-8.5 E) of _EXT.
OSM_EXT = {"south": 47.30, "north": 47.34, "west": 8.40, "east": 8.44}
#: The Overpass-era placeholder, spelled as that code spelled it.
OVERPASS_ERA_SOURCE = "[OSM Download — set extent, download at convert]"
HOUSE = (1500, 1500, 2600, 2600, 40.0)


def _osm_tiles(extent=OSM_EXT):
    return {(x, y) for _z, x, y in ofm.tiles_for_bbox(extent)}


def _ingest_ready_converter():
    """An aether_converter >= 0.2.12 (an INGEST that waits for
    ``buildings_pbf_ready``, toolkit CONTRACT item 48): the one
    ``AETHER_CONVERTER_0212`` names, else AETHER_BIN_DIR's / PATH's when it
    is one; None."""
    from waveshed.core import binary_manager
    for exe in (os.environ.get("AETHER_CONVERTER_0212"),
                _find_real_converter()):
        if (exe and os.path.isfile(exe) and binary_manager.
                converter_supports_ingest_buildings_ready(exe)):
            return exe
    return None


class _PbfConverter(_Converter):
    """The two-phase fake converter, plus the lines a real one prints for a
    batch whose jobs carry ``buildings_pbf_dir`` (decode line, one
    ``[Info]`` line per such job). *warn*: a tile name the "converter"
    cannot decode; *info* False: an engine that ignores the field."""

    def __init__(self, warn=None, info=True, **kw):
        super().__init__(**kw)
        self.warn = warn
        self.info = info
        self.times = []

    def __call__(self, exe, args, on_line, on_start=None, env=None):
        with open(args[-1], encoding="utf-8") as fh:
            jobs = json.load(fh)
        self.times.append(time.monotonic())
        for d in sorted({j["buildings_pbf_dir"] for j in jobs
                         if j.get("buildings_pbf_dir")}):
            on_line(f'[Rust] Decoding building tiles in "{d}" (once for '
                    f'the run)...')
            if self.warn:
                on_line(f"[Warn] buildings {self.warn}: PBF decode error: "
                        f"failed to decode Protobuf message: unexpected end "
                        f"group tag")
        if self.info:
            for j in jobs:
                d = j.get("buildings_pbf_dir")
                if d:
                    n = len(os.listdir(d))
                    on_line(f"[Info] buildings_pbf_dir: {n} tile(s), {n} "
                            f"building(s) ({n} after dedup), {n} drawn, "
                            f"{100 * n} pixel(s) raised")
        return super().__call__(exe, args, on_line, on_start, env)


class _OsmRun:
    """One shipped-worker run over [files, xyz] (or *entries*) plus an OSM
    buildings row, against the stand-in server *srv*; fakes for the engine,
    the terrain pool and the free space (as tests/test_two_phase_convert)."""

    def __init__(self, case, srv, osm_extent=OSM_EXT, entries=None,
                 converter=None, overwrite=False,
                 osm_source=mct._OSM_PLACEHOLDER, pool=None):
        self.case, self.srv = case, srv
        self.osm_extent = osm_extent
        self.entries = entries if entries is not None else [
            {"kind": "files", "infos": _files()},
            {"kind": "xyz", "uri": _XYZ, "extent": dict(_EXT)}]
        self.converter = converter or _PbfConverter()
        self.overwrite = overwrite
        self.osm_source = osm_source
        self.pool_fn = pool
        self.pool_starts = []
        self.plan_times = []
        self.status, self.logs, self.errs, self.oks = [], [], [], []
        self.log_times = []
        self.urls = []
        self._lock = threading.Lock()

    def pool(self, uri, specs, res, **kw):
        with self._lock:
            self.pool_starts.append(time.monotonic())
        if self.pool_fn is not None:
            return self.pool_fn(uri, specs, res, **kw)
        return {t["filename"]: f"/pool/{t['filename']}" for t in specs}

    def log(self, msg):
        with self._lock:
            self.logs.append(msg)
            self.log_times.append((time.monotonic(), msg))

    def run(self, worker_hook=None):
        layers = [_LayerEntry(layer_type="raster", source_path=f"/src{k}",
                              extent=dict(_EXT))
                  for k in range(len(self.entries))]
        layers.append(_LayerEntry(layer_type="buildings",
                                  source_path=self.osm_source,
                                  crs_authid="EPSG:4326",
                                  extent=(dict(self.osm_extent)
                                          if self.osm_extent else None)))
        resolved = {f"/src{k}": e for k, e in enumerate(self.entries)}
        worker = _MapConverterWorker(layers, self.case.out, [30],
                                     [e.source_path for e in layers],
                                     overwrite=self.overwrite)
        self.worker = worker
        worker.status.connect(self.status.append)
        worker.log_line.connect(self.log)
        worker.finished_err.connect(self.errs.append)
        worker.finished_ok.connect(self.oks.append)
        real_urlopen = urllib.request.urlopen

        def urlopen(req, *a, **kw):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            with self._lock:
                self.urls.append(url)
            return real_urlopen(req, *a, **kw)

        def plan(*_a, **_kw):
            self.plan_times.append(time.monotonic())

        if worker_hook is not None:
            worker_hook(self, worker)
        with mock.patch.object(_MapConverterWorker, "_resolve_raster",
                               side_effect=lambda e: resolved[
                                   e["source_path"]]), \
             mock.patch.object(mct, "find_binary", return_value="/bin/conv"), \
             mock.patch.object(mct, "_plan_cross_check", side_effect=plan), \
             mock.patch.object(mct, "_converter_reports_tiles",
                               return_value=True), \
             mock.patch.object(ta, "preflight_ingest_jobs"), \
             mock.patch.object(ta, "get_cache_dir",
                               return_value=self.case.cache), \
             mock.patch.object(ta, "ensure_pool_tiles",
                               side_effect=self.pool), \
             mock.patch.object(oe, "free_bytes", return_value=_HUGE), \
             mock.patch.object(ta, "run_converter_streaming",
                               side_effect=self.converter), \
             mock.patch.object(ofm, "TILEJSON_URL", self.srv.tilejson_url), \
             mock.patch.object(ofm, "RETRY_BACKOFF_S", (0.0, 0.0, 0.0)), \
             mock.patch.object(ofm.urllib.request, "urlopen",
                               side_effect=urlopen):
            worker.run()
        return self

    def logged(self, prefix):
        return [m for m in self.logs if m.startswith(prefix)]

    def log_time(self, prefix):
        return next(t for t, m in self.log_times if m.startswith(prefix))


class _Case(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.out = os.path.join(self.root, "out")
        self.cache = os.path.join(self.root, "cache")
        os.makedirs(self.cache)
        self.expected, self.n = _tiles()
        ofm._cached_template = None

    def tearDown(self):
        ofm._cached_template = None
        self._tmp.cleanup()

    def store(self):
        with mock.patch.object(ta, "get_cache_dir", return_value=self.cache):
            return ta.osm_buildings_store_dir()

    def store_names(self):
        try:
            return set(os.listdir(self.store()))
        except OSError:
            return set()

    @staticmethod
    def names(tiles):
        return {ofm.tile_filename(14, x, y) for x, y in tiles}


# ---------------------------------------------------------------------------
# The fetch and the jobs
# ---------------------------------------------------------------------------

class FetchAndJobs(_Case):

    def test_the_run_fetches_the_extent_and_its_jobs_carry_the_tiles(self):
        n = self.n
        with OfmServer(default=building_tile([HOUSE])) as srv:
            r = _OsmRun(self, srv).run()
            asked = srv.requested()
        self.assertEqual(r.errs, [])
        self.assertEqual(r.oks, [self.out])
        cover = _osm_tiles()
        # Exactly the z14 tiles of the row's extent (tile A needs them all;
        # B, C, D are out of their reach), each once, from OpenFreeMap
        # only — TileJSON first.
        self.assertEqual(asked, cover)
        self.assertEqual(len(srv.requests), len(cover))
        self.assertTrue(all(u.startswith(f"http://127.0.0.1:{srv.port}/")
                            for u in r.urls), r.urls)
        self.assertTrue(r.urls[0].endswith("/planet"))
        # In the Site Analysis store.
        self.assertEqual(self.store_names(), self.names(cover))
        # Phase 2 (A B C from files) and Phase 3 (B C D): every job whose
        # tile the buildings reach — A, built in Phase 2 — carries the
        # batch's view, holding exactly the fetched tiles.
        p2, p3 = r.converter.calls
        with_pbf = [j for call in (p2, p3) for j in call["jobs"]
                    if j.get("buildings_pbf_dir")]
        self.assertEqual([os.path.basename(j["output_path"])
                          for j in with_pbf], [n["A"]])
        view = with_pbf[0]["buildings_pbf_dir"]
        self.assertEqual(set(os.listdir(view)), self.names(cover))
        self.assertTrue(view.startswith(os.path.join(self.cache, "views")))
        for j in p2["jobs"] + p3["jobs"]:
            self.assertNotIn("buildings_file", j)
        # The log the owner reads.
        self.assertEqual(
            r.logged("Downloading buildings (OpenFreeMap z14): "),
            [f"Downloading buildings (OpenFreeMap z14): {len(cover)} tiles "
             f"for S=47.3000 N=47.3400 W=8.4000 E=8.4400"])
        ready = r.logged("Buildings ready: ")
        self.assertEqual(len(ready), 1)
        self.assertRegex(ready[0], rf"^Buildings ready: {len(cover)} tiles "
                                   r"\(\d+ MB\) in \d+ s$")
        self.assertIn("  buildings: 100% ", "\n".join(r.logs))
        self.assertIn(f"  buildings: burned onto 1 of 3 tile(s) — "
                      f"{len(cover)} building(s) drawn, "
                      f"{100 * len(cover):,} px raised", r.logs)
        # One progress model: the buildings slot was sized by the tiles to
        # fetch and is done.
        model = r.worker.progress_model
        self.assertEqual(model.work(mct._BUILDINGS_SLOT), float(len(cover)))
        self.assertEqual(model.done(mct._BUILDINGS_SLOT), float(len(cover)))
        self.assertEqual(model.percent(), 100)

    def test_manifest_records_the_buildings(self):
        # Tile A was built with the OpenFreeMap tiles: its record is the
        # fingerprint WITH them; B, C, D (out of reach) keep the plain one.
        n = self.n
        with OfmServer(default=building_tile([HOUSE])) as srv:
            r = _OsmRun(self, srv).run()
        self.assertEqual(r.errs, [])
        osm = mct._OsmBuildings([OSM_EXT])
        spec = mct._RunBuildings(osm=osm)
        entries = [{"kind": "files", "infos": _files()},
                   {"kind": "xyz", "uri": _XYZ, "extent": dict(_EXT)}]
        files_with, files_without = {}, {}
        with_b = mct._stack_tile_fingerprints(self.expected, entries, spec,
                                              files_only=files_with)
        without = mct._stack_tile_fingerprints(self.expected, entries, None,
                                               files_only=files_without)
        manifest = mct._read_output_manifest(self.out)
        self.assertEqual(manifest[n["A"]], files_with[n["A"]])   # Phase 2
        self.assertNotEqual(files_with[n["A"]], files_without[n["A"]])
        for k in "BCD":
            self.assertEqual(with_b[n[k]], without[n[k]], k)
        self.assertTrue(osm.tag(*next(rt for rt in self.expected
                                      if rt[1]["filename"] == n["A"]))
                        .startswith("openfreemap:z14@"))

    def test_tiles_in_the_store_are_not_fetched_again(self):
        cover = sorted(_osm_tiles())
        os.makedirs(self.store())
        for x, y in cover[:5]:
            ofm._write_tile(self.store(), ofm.tile_filename(14, x, y),
                            building_tile([HOUSE]))
        with OfmServer(default=building_tile([HOUSE])) as srv:
            r = _OsmRun(self, srv).run()
            asked = srv.requested()
        self.assertEqual(r.errs, [])
        self.assertEqual(asked, set(cover[5:]))
        self.assertIn(f"  buildings: 5 tile(s) already in the cache, "
                      f"{len(cover) - 5} to fetch", "\n".join(r.logs))

    def test_a_tile_without_data_is_fine(self):
        cover = sorted(_osm_tiles())
        tiles = {cover[0]: building_tile([HOUSE])}
        with OfmServer(tiles=tiles, default=None) as srv:     # empty 200s
            r = _OsmRun(self, srv).run()
        self.assertEqual(r.errs, [])
        self.assertEqual(self.store_names(), self.names(cover[:1]))
        self.assertRegex(r.logged("Buildings ready: ")[0],
                         rf"\({len(cover) - 1} without data\)$")

    def test_nothing_is_fetched_when_every_tile_exists(self):
        with OfmServer(default=building_tile([HOUSE])) as srv:
            first = _OsmRun(self, srv).run()
            n_first = len(srv.requests)
            second = _OsmRun(self, srv).run()
            n_second = len(srv.requests)
        self.assertEqual(first.errs + second.errs, [])
        self.assertEqual(n_second, n_first)
        self.assertEqual(second.status[-1], "All tiles already exist.")
        self.assertEqual(second.converter.calls, [])

    def test_a_row_without_extent_is_refused(self):
        with OfmServer(default=building_tile([HOUSE])) as srv:
            r = _OsmRun(self, srv, osm_extent=None).run()
            asked = srv.requested()
        self.assertEqual(len(r.errs), 1)
        self.assertIn("has no extent", r.errs[0])
        self.assertEqual(r.oks, [])
        self.assertEqual(asked, set())
        self.assertEqual(r.converter.calls, [])
        self.assertEqual(r.pool_starts, [])

    def test_a_row_off_the_terrain_is_refused(self):
        off = {"south": 40.0, "north": 40.1, "west": 2.0, "east": 2.1}
        with OfmServer(default=building_tile([HOUSE])) as srv:
            r = _OsmRun(self, srv, osm_extent=off).run()
        self.assertEqual(len(r.errs), 1)
        self.assertIn("outside the terrain extent", r.errs[0])
        self.assertEqual(r.converter.calls, [])


# ---------------------------------------------------------------------------
# Concurrency: nothing waits for the buildings but the job build
# ---------------------------------------------------------------------------

class NothingWaitsForTheBuildings(_Case):

    def test_tile_list_and_terrain_download_start_before_the_buildings(self):
        # Every building tile takes 0.6 s; the tile list (plan cross-check)
        # and the terrain download must start long before the buildings are
        # ready, the first ingest batch only after.
        with OfmServer(default=building_tile([HOUSE]), delay_s=0.6) as srv:
            r = _OsmRun(self, srv).run()
            first_request = min(t for t, _x, _y in srv.requests)
        self.assertEqual(r.errs, [])
        ready = r.log_time("Buildings ready: ")
        self.assertEqual(len(r.plan_times), 1)
        self.assertLess(r.plan_times[0], first_request)
        self.assertTrue(r.pool_starts)
        self.assertLess(r.pool_starts[0], ready - 0.3,
                        "the terrain download waited for the buildings")
        self.assertLess(r.pool_starts[0], first_request + 0.3)
        self.assertGreater(min(r.converter.times), ready,
                           "an ingest batch ran before the buildings")
        self.assertIn("  waiting for the building tiles (OpenFreeMap z14) "
                      "before converting...", r.logs)

    def test_the_label_names_the_building_download_next_to_the_terrain(self):
        labels = []

        def slow_pool(uri, specs, res, **kw):
            # The terrain download runs (and reports, as the downloader
            # does) while the buildings come in.
            kw["progress_cb"].download_event({
                "event": "pass_start", "zoom": 12, "total_units": 10.0,
                "done_units": 1.0, "pass_done": 1, "pass_total": 10})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                label = r.worker.progress_model.label()
                labels.append(label)
                if ("Downloading buildings (OpenFreeMap z14): " in label
                        and "Downloading terrain" in label):
                    break
                time.sleep(0.02)
            return {t["filename"]: f"/pool/{t['filename']}" for t in specs}

        with OfmServer(default=building_tile([HOUSE]), delay_s=0.4) as srv:
            r = _OsmRun(self, srv, pool=slow_pool)
            r.run()
        self.assertEqual(r.errs, [])
        both = [lb for lb in labels
                if "Downloading buildings (OpenFreeMap z14): " in lb
                and "Downloading terrain" in lb]
        self.assertTrue(both, labels[-3:])
        self.assertRegex(both[-1], r"Downloading buildings \(OpenFreeMap "
                                   r"z14\): \d+/\d+ tiles")


# ---------------------------------------------------------------------------
# Failure ends the run — loudly, and stops the other branches
# ---------------------------------------------------------------------------

def _sleeper():
    return subprocess.Popen([sys.executable, "-c",
                             "import time; time.sleep(60)"])


class FailureEndsTheRun(_Case):

    def _run_with_live_download(self, srv, **kw):
        """A run whose terrain download is a real (sleeping) process,
        registered like the converter's — the Stop contract kills it."""
        procs = []

        def pool(uri, specs, res, **pkw):
            proc = _sleeper()
            procs.append(proc)
            pkw["on_start"](proc)
            proc.wait(timeout=30)
            raise RuntimeError("Terrain download failed: the engine was "
                               "killed")

        t0 = time.monotonic()
        r = _OsmRun(self, srv, pool=pool, **kw).run()
        return r, procs, time.monotonic() - t0

    def test_a_server_failure_ends_the_run_and_kills_the_download(self):
        cover = sorted(_osm_tiles())
        bad = cover[2]
        with OfmServer(default=building_tile([HOUSE]),
                       fail={bad: 503}) as srv:
            r, procs, took = self._run_with_live_download(srv)
            attempts = sum(1 for _t, x, y in srv.requests if (x, y) == bad)
        self.assertEqual(r.oks, [])
        self.assertEqual(len(r.errs), 1)
        err = r.errs[0]
        self.assertTrue(err.startswith(
            "Buildings download failed (OpenFreeMap z14): "), err)
        self.assertIn(f"tile {ofm.tile_filename(14, *bad)}: HTTP 503", err)
        self.assertIn(f"after {ofm.TILE_ATTEMPTS} attempts", err)
        self.assertRegex(err, rf"\d+ of {len(cover)} building tiles missing")
        self.assertEqual(attempts, ofm.TILE_ATTEMPTS)
        # The other branch: its engine process was killed, at once.
        self.assertEqual(len(procs), 1)
        self.assertIsNotNone(procs[0].poll())
        self.assertLess(took, 20)
        # Nothing converted, nothing kept, the error logged.
        self.assertEqual(r.converter.calls, [])
        self.assertFalse(any(name.endswith(".abt")
                             for name in os.listdir(self.out)))
        self.assertTrue(any(m.startswith("ERROR: Buildings download failed")
                            for m in r.logs))
        self.assertNotIn("Cancelled.", r.status)

    def test_an_undecodable_answer_ends_the_run(self):
        cover = sorted(_osm_tiles())
        with OfmServer(default=building_tile([HOUSE]),
                       garbage=[cover[0]]) as srv:
            r = _OsmRun(self, srv).run()
        self.assertEqual(len(r.errs), 1)
        self.assertIn("cannot be decoded: not a vector tile", r.errs[0])
        self.assertIn(ofm.tile_filename(14, *cover[0]), r.errs[0])
        self.assertNotIn(ofm.tile_filename(14, *cover[0]),
                         self.store_names())
        self.assertEqual(r.converter.calls, [])

    def test_no_tile_with_data_is_not_zero_buildings(self):
        # The owner's case: "Downloaded 0 buildings" was a success. Here
        # every tile answers "no data" — the run ends with that cause.
        with OfmServer(default=None) as srv:
            r = _OsmRun(self, srv).run()
        self.assertEqual(r.oks, [])
        self.assertEqual(len(r.errs), 1)
        self.assertIn("none of the", r.errs[0])
        self.assertIn("building tiles has data", r.errs[0])
        self.assertEqual(r.converter.calls, [])

    def test_an_unreadable_tilejson_ends_the_run(self):
        with OfmServer(default=building_tile([HOUSE]),
                       tilejson_status=500) as srv, \
                mock.patch.object(ofm, "TILEJSON_ATTEMPTS", 1):
            r = _OsmRun(self, srv).run()
            asked = srv.requested()
        self.assertEqual(len(r.errs), 1)
        self.assertIn("TileJSON", r.errs[0])
        self.assertIn("HTTP 500", r.errs[0])
        self.assertEqual(asked, set(), "fell back to a guessed tile URL")

    def test_a_terrain_failure_stops_the_building_fetch(self):
        # The other direction: the download fails, the fetch (slow server)
        # is stopped and the run reports the DOWNLOAD's error.
        def pool(uri, specs, res, **kw):
            time.sleep(0.3)
            raise RuntimeError("Terrain download failed: HTTP 403")

        with OfmServer(default=building_tile([HOUSE]), delay_s=0.3) as srv, \
                mock.patch.object(ofm, "FETCH_WORKERS", 1):
            r = _OsmRun(self, srv, pool=pool).run()
            asked = len(srv.requests)
            time.sleep(0.5)
            asked_later = len(srv.requests)
        self.assertEqual(r.errs, ["Terrain download failed: HTTP 403"])
        self.assertLess(asked, len(_osm_tiles()))
        self.assertEqual(asked_later, asked)
        self.assertFalse(r.logged("Buildings ready"))


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------

class StopCancelsTheFetch(_Case):

    def test_stop_cancels_the_buildings_fetch_and_the_download(self):
        procs = []

        def pool(uri, specs, res, **kw):
            proc = _sleeper()
            procs.append(proc)
            kw["on_start"](proc)
            proc.wait(timeout=30)
            raise RuntimeError("download failed")

        stopped = {}

        def hook(run, worker):
            def stopper():
                deadline = time.monotonic() + 20
                while len(run.srv.requests) < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
                stopped["t"] = time.monotonic()
                worker.cancel()
            threading.Thread(target=stopper, daemon=True).start()

        with OfmServer(default=building_tile([HOUSE]), delay_s=0.2) as srv, \
                mock.patch.object(ofm, "FETCH_WORKERS", 2):
            r = _OsmRun(self, srv, pool=pool).run(worker_hook=hook)
            ended = time.monotonic()
            asked = len(srv.requests)
            time.sleep(0.6)
            asked_later = len(srv.requests)
            written = self.store_names()
        cover = _osm_tiles()
        self.assertEqual(r.errs, [])
        self.assertEqual(r.oks, [])
        self.assertEqual(r.status[-1], "Cancelled.")
        self.assertIn("Cancelled by user.", r.logs)
        self.assertLess(ended - stopped["t"], 1.0 + ofm.CANCEL_GRACE_S)
        self.assertLess(asked, len(cover), "the fetch ran to the end")
        self.assertEqual(asked_later, asked, "requests after Stop")
        self.assertLess(len(written), len(cover))
        self.assertEqual(len(procs), 1)
        self.assertIsNotNone(procs[0].poll(), "the download kept running")
        self.assertEqual(r.converter.calls, [])
        self.assertFalse(r.logged("Buildings ready"))
        self.assertFalse(any(n.endswith(".part") for n in written))


# ---------------------------------------------------------------------------
# The converter's own decode check (a poisoned cache)
# ---------------------------------------------------------------------------

class ConverterDecodeCheck(_Case):

    def test_a_tile_the_converter_cannot_decode_ends_the_run(self):
        cover = sorted(_osm_tiles())
        bad = ofm.tile_filename(14, *cover[0])
        conv = _PbfConverter(warn=bad)
        with OfmServer(default=building_tile([HOUSE])) as srv:
            r = _OsmRun(self, srv, converter=conv).run()
        self.assertEqual(r.oks, [])
        self.assertEqual(len(r.errs), 1)
        self.assertIn("the converter could not decode 1 OpenFreeMap tile(s)",
                      r.errs[0])
        self.assertIn(bad, r.errs[0])
        # Stopped at the line after the warning, before its first tile
        # (Phase 2's batch): nothing written, nothing promoted.
        self.assertEqual(len(conv.times), 1)
        self.assertEqual(conv.calls, [])
        self.assertFalse(any(name.endswith(".abt")
                             for name in os.listdir(self.out)))
        # Out of the store and of the view: the next run fetches it anew.
        self.assertNotIn(bad, self.store_names())
        views = os.path.join(self.cache, "views")
        for v in os.listdir(views):
            self.assertNotIn(bad, os.listdir(os.path.join(views, v)))
        with OfmServer(default=building_tile([HOUSE])) as srv:
            again = _OsmRun(self, srv).run()
            asked = srv.requested()
        self.assertEqual(again.errs, [])
        self.assertEqual(asked, {cover[0]})

    def test_a_phase3_tile_written_without_verified_buildings_is_rebuilt(self):
        # Buildings reaching D (Phase 3 only, written straight into the
        # output dir) and an engine that reports no burn: the run fails and
        # D's record makes the next run rebuild it ("no record" keeps).
        n = self.n
        ext_d = {"south": 47.70, "north": 47.72, "west": 8.70, "east": 8.72}
        conv = _PbfConverter(info=False)
        with OfmServer(default=building_tile([HOUSE])) as srv:
            r = _OsmRun(self, srv, osm_extent=ext_d, converter=conv).run()
        self.assertEqual(len(r.errs), 1)
        self.assertIn("does not apply buildings_pbf_dir", r.errs[0])
        manifest = mct._read_output_manifest(self.out)
        self.assertEqual(manifest.get(n["D"]), mct._BUILDINGS_UNVERIFIED)
        with OfmServer(default=building_tile([HOUSE])) as srv:
            again = _OsmRun(self, srv, osm_extent=ext_d).run()
        self.assertEqual(again.errs, [])
        rebuilt = {os.path.basename(j["output_path"])
                   for call in again.converter.calls for j in call["jobs"]}
        self.assertIn(n["D"], rebuilt)


# ---------------------------------------------------------------------------
# An Overpass-era stack still loads
# ---------------------------------------------------------------------------

class OverpassEraStack(_Case):

    def test_the_old_placeholder_row_runs_on_openfreemap(self):
        self.assertEqual(mct._OSM_PLACEHOLDER, OVERPASS_ERA_SOURCE)
        with OfmServer(default=building_tile([HOUSE])) as srv:
            r = _OsmRun(self, srv, osm_source=OVERPASS_ERA_SOURCE).run()
            asked = srv.requested()
        self.assertEqual(r.errs, [])
        self.assertEqual(asked, _osm_tiles())
        self.assertFalse(any("overpass" in u for u in r.urls))

    def test_tiles_an_overpass_era_run_recorded_are_rebuilt_where_reached(
            self):
        # The owner's failed run recorded its tiles WITHOUT buildings
        # ("Downloaded 0 buildings" → no buildings file). With the
        # OpenFreeMap row, tile A — the only one its tiles reach — is
        # rebuilt (with them); B, C, D keep their records and are kept.
        n = self.n
        entries = [{"kind": "files", "infos": _files()},
                   {"kind": "xyz", "uri": _XYZ, "extent": dict(_EXT)}]
        files_only = {}
        old = mct._stack_tile_fingerprints(self.expected, entries, None,
                                           files_only=files_only)
        os.makedirs(self.out)
        records = {}
        for res, tile in self.expected:
            name = tile["filename"]
            job = {"output_path": os.path.join(self.out, name),
                   "size_px": tile["size_px"], "ul_lat": tile["ul_lat"],
                   "ul_lon": tile["ul_lon"]}
            from test_two_phase_convert import _write_tile
            _write_tile(job)
            records[name] = files_only[name] if name != n["D"] else old[name]
        mct._write_output_manifest(self.out, records)
        with OfmServer(default=building_tile([HOUSE])) as srv:
            r = _OsmRun(self, srv, osm_source=OVERPASS_ERA_SOURCE).run()
        self.assertEqual(r.errs, [])
        built = [os.path.basename(j["output_path"])
                 for call in r.converter.calls for j in call["jobs"]]
        self.assertEqual(built, [n["A"]])
        self.assertTrue(all(j.get("buildings_pbf_dir")
                            for call in r.converter.calls
                            for j in call["jobs"]))
        self.assertIn(f"  1 existing tile(s) are rebuilt — the files or "
                      f"buildings that feed them changed since they were "
                      f"built (first: {n['A']})", r.logs)


# ---------------------------------------------------------------------------
# Up-front estimate and the tab's own check
# ---------------------------------------------------------------------------

class EstimateAndRowChecks(unittest.TestCase):

    def test_the_run_estimate_registers_the_buildings_slot(self):
        from waveshed.core import run_progress
        model = run_progress.RunProgress()
        layers = [_LayerEntry(layer_type="raster", source_path="/a.tif",
                              extent=dict(_EXT)),
                  _LayerEntry(layer_type="buildings",
                              source_path=mct._OSM_PLACEHOLDER,
                              extent=dict(OSM_EXT))]
        with mock.patch.object(mct, "_entry_kind",
                               side_effect=lambda e: "buildings"
                               if e.layer_type == "buildings" else "file"):
            mct._estimate_run_work(model, layers, [])
        self.assertEqual(model.work(mct._BUILDINGS_SLOT),
                         float(ofm.estimate_tile_count(OSM_EXT)))

    def test_row_problems(self):
        raster = _LayerEntry(layer_type="raster", source_path="/a.tif",
                             extent=dict(_EXT))
        row = _LayerEntry(layer_type="buildings",
                          source_path=mct._OSM_PLACEHOLDER)
        self.assertIn("has no extent", mct._osm_rows_problem([raster, row]))
        row.extent = dict(OSM_EXT)
        self.assertIsNone(mct._osm_rows_problem([raster, row]))
        row.extent = {"south": 1.0, "north": 1.1, "west": 1.0, "east": 1.1}
        self.assertIn("outside the terrain extent",
                      mct._osm_rows_problem([raster, row]))
        # A building FILE row is not an OSM row.
        self.assertIsNone(mct._osm_rows_problem([raster, _LayerEntry(
            layer_type="buildings", source_path="/b.fgb")]))

    def test_moving_the_row_changes_only_the_tiles_it_reaches(self):
        expected, n = _tiles()
        a = mct._OsmBuildings([OSM_EXT])
        moved = mct._OsmBuildings([{"south": 47.30, "north": 47.34,
                                    "west": 8.40, "east": 8.46}])
        by = {t["filename"]: (r, t) for r, t in expected}
        self.assertNotEqual(a.tag(*by[n["A"]]), moved.tag(*by[n["A"]]))
        for k in "BCD":
            self.assertEqual(a.tag(*by[n[k]]), "")
            self.assertEqual(moved.tag(*by[n[k]]), "")


# ---------------------------------------------------------------------------
# The real converter
# ---------------------------------------------------------------------------

#: One 30 m output tile (A of _EXT): 47.0-47.5 N, 8.0-8.5 E.
_EXT_A = {"south": 47.05, "north": 47.45, "west": 8.05, "east": 8.45}


class RealConverterBuildings(_Case):
    """The shipped worker, the REAL aether_converter (AETHER_BIN_DIR): the
    terrain downloaded from a local terrarium server, the buildings from
    the stand-in OpenFreeMap — a house in every z14 tile of the row."""

    def setUp(self):
        super().setUp()
        self.exe = _find_real_converter()
        if not self.exe:
            self.skipTest("aether_converter not provided via AETHER_BIN_DIR "
                          "or PATH")

    def _server(self, **kw):
        return OfmServer(default=building_tile([HOUSE]),
                         terrain=lambda z, x, y: _terrarium_png(z, x, y,
                                                                900.0), **kw)

    def _run(self, srv):
        from waveshed.core import binary_manager
        uri = srv.terrain_uri(zmax=12)
        layers = [_LayerEntry(layer_type="raster", source_path=uri,
                              extent=dict(_EXT_A)),
                  _LayerEntry(layer_type="buildings",
                              source_path=mct._OSM_PLACEHOLDER,
                              crs_authid="EPSG:4326", extent=dict(OSM_EXT))]
        # The test re-ingests the batch's job afterwards, which reads the
        # pool tile: keep it ("Keep pool tiles" on; off removes the pool
        # tiles a run downloaded once it has succeeded).
        worker = _MapConverterWorker(layers, self.out, [30],
                                     [e.source_path for e in layers],
                                     keep_pool_tiles=True)
        rec = {"log": [], "err": [], "ok": [], "jobs": []}
        worker.log_line.connect(rec["log"].append)
        worker.finished_err.connect(rec["err"].append)
        worker.finished_ok.connect(rec["ok"].append)
        real_run = ta.run_converter_streaming

        def launch(exe_, args, on_line, **kw):
            if args[0] == "ingest":
                with open(args[-1], encoding="utf-8") as fh:
                    rec["jobs"].extend(json.load(fh))
            return real_run(exe_, args, on_line, **kw)

        with mock.patch.object(_MapConverterWorker, "_resolve_raster",
                               side_effect=lambda e: {
                                   "kind": "xyz", "uri": uri,
                                   "extent": dict(_EXT_A)}), \
             mock.patch.object(mct, "find_binary", return_value=self.exe), \
             mock.patch.object(binary_manager, "find_binary",
                               return_value=self.exe), \
             mock.patch.object(ta, "get_cache_dir", return_value=self.cache), \
             mock.patch.object(ta, "run_converter_streaming",
                               side_effect=launch), \
             mock.patch.object(ofm, "TILEJSON_URL", srv.tilejson_url):
            worker.run()
        return rec

    def _ingest(self, job, out_name):
        job = dict(job, output_path=os.path.join(self.root, out_name))
        path = os.path.join(self.root, out_name + ".json")
        with open(path, "w") as fh:
            json.dump([job], fh)
        lines = []
        rc = ta.run_converter_streaming(self.exe, ["ingest", "--job-file",
                                                   path], lines.append)
        self.assertEqual(rc, 0, lines[-5:])
        return job["output_path"]

    @staticmethod
    def _grid(path):
        from waveshed.core import abt
        header = abt.read_header(path)
        return header, abt.read_tile(header)

    def test_buildings_are_burned_into_the_output(self):
        import numpy as np
        with self._server() as srv:
            first = self._run(srv)
            n_bld, n_ter = len(srv.requests), len(srv.terrain_requests)
            second = self._run(srv)
            n_bld2, n_ter2 = len(srv.requests), len(srv.terrain_requests)
        self.assertEqual(first["err"], [], first["log"][-12:])
        self.assertEqual(first["ok"], [self.out])
        cover = _osm_tiles()
        self.assertEqual(n_bld, len(cover))
        self.assertTrue(any(m.startswith(f"Buildings ready: {len(cover)} "
                                         f"tiles") for m in first["log"]))
        # One output tile, one job, carrying exactly the fetched tiles —
        # plus, on a converter that waits for its building tiles (>= 0.2.12,
        # the batch launched before them), the marker the fetch wrote into
        # the view once they were in.
        self.assertEqual(len(first["jobs"]), 1)
        job = first["jobs"][0]
        from waveshed.core import binary_manager, osm_buildings
        expected_view = set(self.names(cover))
        if binary_manager.converter_supports_buildings_list(self.exe):
            # >= 0.2.14 (CONTRACT changelog 51): THE store and the list of
            # exactly those tiles — no view.
            self.assertEqual(job["buildings_pbf_dir"],
                             os.path.abspath(self.store()))
            with open(job["buildings_pbf_list"], encoding="utf-8") as fh:
                self.assertEqual(set(fh.read().split()), expected_view)
        elif binary_manager.converter_supports_ingest_buildings_ready(
                self.exe):
            expected_view.add(osm_buildings.READY_MARKER)
        self.assertEqual(set(os.listdir(job["buildings_pbf_dir"])),
                         expected_view)
        out_path = job["output_path"]
        with open(out_path, "rb") as fh:
            built = fh.read()
        # Byte for byte what the converter writes for that job ...
        with open(self._ingest(job, "again.abt"), "rb") as fh:
            self.assertEqual(fh.read(), built)
        # ... and the buildings are in it: against the same job without
        # them, raised pixels exactly inside the houses, by their height.
        plain = dict(job)
        del plain["buildings_pbf_dir"]
        for key in ("buildings_pbf_ready", "buildings_pbf_list",
                    "buildings_pbf_wait_s", "buildings_hold_mb"):
            plain.pop(key, None)        # the marker fields go with the dir
        header, with_b = self._grid(out_path)
        _h, without = self._grid(self._ingest(plain, "plain.abt"))
        raised = with_b.astype(np.int32) - without.astype(np.int32)
        self.assertTrue((raised >= 0).all())
        rows, cols = np.nonzero(raised)
        self.assertGreater(len(rows), 50 * len(cover))
        lat = header.ul_lat - (rows + 0.5) * header.pixel_res
        lon = header.ul_lon + (cols + 0.5) * header.pixel_res
        inside = np.zeros(len(rows), dtype=bool)
        slack = header.pixel_res
        for x, y in cover:
            w, nn = tile_lonlat(14, x, y, HOUSE[0], HOUSE[1])
            e, s = tile_lonlat(14, x, y, HOUSE[2], HOUSE[3])
            inside |= ((lon >= w - slack) & (lon <= e + slack)
                       & (lat >= s - slack) & (lat <= nn + slack))
        self.assertTrue(inside.all(), "a pixel was raised outside a house")
        # A flat roof 40 m above the lowest ground under the house: the
        # largest raise is about the height (terrain slopes under it).
        self.assertGreaterEqual(raised.max(), 2 * 40)
        self.assertLessEqual(raised.max(), 2 * 40 + 2 * 60)
        self.assertTrue(any(m.startswith("  buildings: burned onto 1 of 1 "
                                         "tile(s) — ") for m in first["log"]))
        # Second run: everything recorded (with its buildings), nothing
        # fetched, nothing converted.
        self.assertEqual(second["err"], [])
        self.assertEqual(second["jobs"], [])
        self.assertEqual((n_bld2, n_ter2), (n_bld, n_ter))

    def test_a_poisoned_cache_tile_ends_the_run_and_is_fetched_anew(self):
        # A store tile no plugin check ever saw (an older plugin wrote it):
        # the real converter only warns and would burn the rest; the run
        # ends before a tile is written, and the next run fetches it anew.
        cover = sorted(_osm_tiles())
        bad = ofm.tile_filename(14, *cover[3])
        os.makedirs(self.store())
        with open(os.path.join(self.store(), bad), "wb") as fh:
            fh.write(b"<html>old cache entry</html>")
        with self._server() as srv:
            first = self._run(srv)
            asked_first = srv.requested()
            out = first["jobs"][0]["output_path"]
            written = os.path.exists(out)
            record = mct._read_output_manifest(self.out).get(
                os.path.basename(out))
            second = self._run(srv)
            asked_second = srv.requested() - asked_first
        self.assertEqual(len(first["err"]), 1, first["log"][-8:])
        self.assertIn("the converter could not decode 1 OpenFreeMap tile(s) "
                      f"(first: {bad}: PBF decode error", first["err"][0])
        self.assertNotIn(cover[3], asked_first)       # it was "cached"
        # Killed at the line after the warning — before the tile is
        # committed; should the commit win the race, its record makes the
        # next run rebuild it.
        if written:
            self.assertEqual(record, mct._BUILDINGS_UNVERIFIED)
        else:
            self.assertIsNone(record)
        self.assertEqual(second["err"], [], second["log"][-8:])
        self.assertEqual([j["output_path"] for j in second["jobs"]], [out])
        self.assertEqual(asked_second, {cover[3]})
        self.assertTrue(os.path.exists(second["jobs"][0]["output_path"]))


#: Scratch of the real-converter runs below: the project's gitignored .tmp.
_SCRATCH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(
    __file__))), ".tmp", "test_mc_osm_buildings")


def _list_converter(test):
    """The aether_converter >= 0.2.14 (``buildings_pbf_list``, toolkit
    CONTRACT changelog 51): the one ``AETHER_CONVERTER_0214`` names, else
    AETHER_BIN_DIR's / PATH's when it is one (as the 0.2.10-0.2.12 gates
    resolve theirs — the release check's ``--bin-dir`` sets only
    AETHER_BIN_DIR). Neither: the test is skipped. The variable set to a
    file that is not there or to an older build: a FAILURE, never a skip —
    a run meant to check the list mode must not pass without it."""
    from waveshed.core import binary_manager
    exe = os.environ.get("AETHER_CONVERTER_0214")
    if not exe:
        found = _find_real_converter()
        if found and binary_manager.converter_supports_buildings_list(found):
            return found
        seen = (f"{found} answers --version "
                f"{binary_manager.probe_converter_version(found)!r}"
                if found else "none there")
        test.skipTest(f"AETHER_CONVERTER_0214 is not set and AETHER_BIN_DIR "
                      f"/ PATH has no aether_converter >= 0.2.14 ({seen}) — "
                      f"the list mode needs one (buildings_pbf_list, toolkit "
                      f"CONTRACT changelog 51)")
    test.assertTrue(os.path.isfile(exe),
                    f"AETHER_CONVERTER_0214={exe}: no such file")
    test.assertTrue(binary_manager.converter_supports_buildings_list(exe),
                    f"AETHER_CONVERTER_0214={exe} answers --version "
                    f"{binary_manager.probe_converter_version(exe)!r}, not "
                    f">= 0.2.14")
    return exe


#: The converter's line when it starts waiting for its building tiles (the
#: Map Converter logs it as printed; Site Analysis indents it by 4).
_WAITING = "[Buildings] waiting for the building tiles ("
#: How long a held tile waits for that line before it is answered anyway
#: and the test fails — far beyond the seconds a converter needs.
_HOLD_S = 120.0
#: A "waits" test's tiles are answered this long after the line, so the
#: converter's own figure (``ready after S s``, one decimal) is never 0.0.
_AFTER_LINE_S = 1.0


class _HeldTiles:
    """A stand-in OpenFreeMap's ``delay_fn``: the building tiles *tiles*
    (None: every one) are answered once the converter has logged that it
    waits for them — the test's own log line *line* (:meth:`saw` gets every
    logged line) — and *after_s* later. A fixed 3-6 s delay used to give the
    converter its head start (to wait for its tiles, or to be killed while
    it waits); a loaded machine outlasted it (the converter had not reached
    its line: no wait, or the fetch failed first). The server calls this on
    the request's own thread: other tiles, terrain and TileJSON never wait.

    One run at a time: :meth:`check` after it (every held request answered
    after the line — not on the :data:`_HOLD_S` cap, not left unanswered);
    :meth:`rearm` before another held run, :meth:`disarm` before a run that
    is not held (a sequential run fetches before launching the converter,
    whose line would never come). :meth:`client` lets the fetch wait that
    long for a held answer (its 20 s socket timeout and 90 s answer
    deadline would turn a slow converter into a fetch failure)."""

    def __init__(self, test, line, tiles=None, after_s=0.0):
        self.test, self.line, self.after_s = test, line, after_s
        self.tiles = None if tiles is None else set(tiles)
        self._arm()
        test.addCleanup(self.disarm)        # no server thread left held

    def _arm(self):
        self.waiting = threading.Event()
        self.seen_at = None
        self.asked, self.held = [], []
        self.armed = True

    def rearm(self):
        self.disarm()
        self._arm()

    def disarm(self):
        self.armed = False
        self.waiting.set()

    def saw(self, line):
        if self.armed and self.seen_at is None and line.startswith(self.line):
            self.seen_at = time.monotonic()
            self.waiting.set()

    def __call__(self, x, y):
        if not self.armed or (self.tiles is not None
                              and (x, y) not in self.tiles):
            return 0.0
        event, asked, held = self.waiting, self.asked, self.held
        asked.append((x, y))
        came = event.wait(_HOLD_S)
        held.append(came)
        seen = self.seen_at
        return (max(0.0, seen + self.after_s - time.monotonic())
                if came and seen is not None else 0.0)

    @staticmethod
    def client():
        return mock.patch.multiple(ofm, TILE_TIMEOUT_S=_HOLD_S + 60.0,
                                   TILE_DEADLINE_S=_HOLD_S + 60.0)

    def check(self, text):
        asked, held = list(self.asked), list(self.held)
        if not asked:
            why = "no held building tile was requested"
        elif len(held) < len(asked):
            why = (f"{len(asked) - len(held)} of {len(asked)} held building "
                   f"tile(s) still unanswered when the run ended")
        elif not all(held):
            why = (f"{held.count(False)} of {len(held)} held building "
                   f"tile(s) answered after {_HOLD_S:.0f} s without it")
        else:
            return
        came = "came" if self.seen_at is not None else "never came"
        self.test.fail(f"{why}; the converter's {self.line.strip()!r}… "
                       f"line {came}: {text[-3000:]}")


class _RealPhase2:
    """The real-converter Phase-2 fixture (no tests of its own): [a real
    GeoTIFF covering tile A and its halo, an XYZ entry] plus the OSM row,
    run through the shipped worker; :meth:`_converter` names the binary."""

    def _converter(self):
        raise NotImplementedError

    def setUp(self):
        self.exe = self._converter()
        import shutil
        import numpy as np
        os.makedirs(_SCRATCH, exist_ok=True)
        self.root = tempfile.mkdtemp(prefix="t_", dir=_SCRATCH)
        self.addCleanup(shutil.rmtree, self.root, True)
        _expected, self.n = _tiles()
        ofm._cached_template = None
        self.addCleanup(setattr, ofm, "_cached_template", None)
        self.dem = os.path.join(self.root, "dem")
        os.makedirs(self.dem)
        box = _b(7.99, 8.51, 46.99, 47.51)
        px = 520                        # 0.001 deg pixels, halo 0.004 deg
        r, c = np.mgrid[0:px, 0:px]
        arr = _surface(box["north"] - (r + 0.5) * 0.001,
                       box["west"] + (c + 0.5) * 0.001,
                       1000.0).astype(np.float32)
        path = os.path.join(self.dem, "a.tif")
        with real_gdal():
            write_tif(path, box, size=(px, px), array=arr, nodata=-9999.0)
        self.infos = [_info(path, box, fp=f"{os.path.getsize(path)}:1",
                            halo_deg=0.004)]

    def _run(self, srv, tag, sequential=False, lists=False, on_line=None):
        """One run into ``out_<tag>`` / ``cache_<tag>``: *sequential* —
        the < 0.2.12 path; else the 0.2.12 view and its marker, or with
        *lists* the >= 0.2.14 list (the same binary throughout). *on_line*:
        also called with every logged line."""
        from waveshed.core import binary_manager
        out = os.path.join(self.root, f"out_{tag}")
        cache = os.path.join(self.root, f"cache_{tag}")
        os.makedirs(cache)
        uri = srv.terrain_uri(zmax=12)
        layers = [_LayerEntry(layer_type="raster", source_path=self.dem,
                              extent=dict(_EXT_A)),
                  _LayerEntry(layer_type="raster", source_path=uri,
                              extent=dict(_EXT_A)),
                  _LayerEntry(layer_type="buildings",
                              source_path=mct._OSM_PLACEHOLDER,
                              crs_authid="EPSG:4326", extent=dict(OSM_EXT))]
        resolved = {self.dem: {"kind": "files", "infos": self.infos},
                    uri: {"kind": "xyz", "uri": uri, "extent": dict(_EXT_A)}}
        worker = _MapConverterWorker(layers, out, [30],
                                     [e.source_path for e in layers])
        rec = {"log": [], "err": [], "ok": [], "jobs": [], "out": out}
        worker.log_line.connect(rec["log"].append)
        if on_line is not None:
            worker.log_line.connect(on_line)
        worker.finished_err.connect(rec["err"].append)
        worker.finished_ok.connect(rec["ok"].append)
        real_run = ta.run_converter_streaming

        def launch(exe_, args, on_line, **kw):
            if args[0] == "ingest":
                with open(args[-1], encoding="utf-8") as fh:
                    rec["jobs"].extend(json.load(fh))
            return real_run(exe_, args, on_line, **kw)

        # sequential: the same binary through the < 0.2.12 path; without
        # *lists* a >= 0.2.14 binary takes the 0.2.12 view (pinned).
        gate = (mock.patch.object(mct, "_converter_waits_for_buildings",
                                  return_value=False) if sequential
                else contextlib.nullcontext())
        views = (contextlib.nullcontext() if lists
                 else mock.patch.object(mct, "_converter_lists_buildings",
                                        return_value=False))
        with real_gdal(), views, \
             mock.patch.object(_MapConverterWorker, "_resolve_raster",
                               side_effect=lambda e: resolved[
                                   e["source_path"]]), \
             mock.patch.object(mct, "find_binary", return_value=self.exe), \
             mock.patch.object(binary_manager, "find_binary",
                               return_value=self.exe), \
             mock.patch.object(ta, "get_cache_dir", return_value=cache), \
             mock.patch.object(ta, "run_converter_streaming",
                               side_effect=launch), \
             mock.patch.object(ofm, "TILEJSON_URL", srv.tilejson_url), gate:
            worker.run()
        return rec


@unittest.skipUnless(HAVE_GDAL, "needs the real GDAL")
class RealConverterPhase2BesideItsBuildings(_RealPhase2, unittest.TestCase):
    """The shipped worker and the REAL aether_converter >= 0.2.12
    (``AETHER_CONVERTER_0212``, toolkit CONTRACT item 48) over [a real
    GeoTIFF covering tile A and its halo, an XYZ entry] plus the OSM row:
    Phase 2 converts A from the file, launched while A's building tiles
    are still being fetched (held until it waits, :class:`_HeldTiles`) — it
    waits for its view's marker and writes the bytes the sequential path
    (Phase 2 after the tiles) writes. (A >= 0.2.14 binary is pinned to the
    view here; :class:`RealConverterPhase2ListsItsBuildings` runs its list.)"""

    def _converter(self):
        exe = _ingest_ready_converter()
        if not exe:
            self.skipTest("no aether_converter >= 0.2.12 (set "
                          "AETHER_CONVERTER_0212)")
        return exe

    def test_phase2_waits_for_its_marker_and_writes_the_sequential_bytes(
            self):
        terrain = (lambda z, x, y: _terrarium_png(z, x, y, 900.0))
        # Every building tile is answered once Phase 2 waits for it (+1 s);
        # the sequential run fetches before its converter: never held.
        hold = _HeldTiles(self, _WAITING, after_s=_AFTER_LINE_S)
        with OfmServer(default=building_tile([HOUSE]), terrain=terrain,
                       delay_fn=hold) as srv, hold.client():
            beside = self._run(srv, "beside", on_line=hold.saw)
            hold.check("\n".join(beside["log"]))
            hold.disarm()
            first = self._run(srv, "first", sequential=True)
        log, log0 = beside["log"], first["log"]
        self.assertEqual(beside["err"], [], log[-12:])
        self.assertEqual(first["err"], [], log0[-12:])
        name = self.n["A"]
        for rec in (beside, first):
            self.assertEqual(rec["ok"], [rec["out"]])
            self.assertIn("Phase 2: 1 tiles from local files — 1 complete, "
                          "0 need the pool", rec["log"])
            (job,) = rec["jobs"]                  # Phase 2's, tile A
            self.assertEqual(os.path.basename(job["output_path"]), name)
            self.assertTrue(job.get("buildings_pbf_dir"))
        self.assertEqual(beside["jobs"][0]["buildings_pbf_ready"], "_ready")
        self.assertNotIn("buildings_pbf_ready", first["jobs"][0])
        # Launched before the building tiles were in: it waited for them.
        waiting = next((i for i, m in enumerate(log) if m.startswith(
            "[Buildings] waiting for the building tiles (")), None)
        ready = next(i for i, m in enumerate(log)
                     if m.startswith("Buildings ready: "))
        self.assertIsNotNone(waiting, log[-20:])
        self.assertLess(waiting, ready, log[-20:])
        end = [m for m in log if m.startswith("  buildings: burned onto ")]
        self.assertEqual(len(end), 1, log[-20:])
        waited = re.search(r" — the converter waited (\d+\.\d) s for them$",
                           end[0])
        self.assertIsNotNone(waited, end[0])
        self.assertGreater(float(waited.group(1)), 0.0)
        self.assertFalse([m for m in log0 if "the converter waited" in m
                          or "[Buildings] waiting" in m])
        # The same burn, the same bytes.
        end0 = [m for m in log0 if m.startswith("  buildings: burned onto ")]
        self.assertEqual(end[0].split(" — the converter waited")[0], end0[0])
        with open(os.path.join(beside["out"], name), "rb") as a, \
                open(os.path.join(first["out"], name), "rb") as b:
            self.assertEqual(a.read(), b.read())


@unittest.skipUnless(HAVE_GDAL, "needs the real GDAL")
class RealConverterPhase2ListsItsBuildings(_RealPhase2, unittest.TestCase):
    """The REAL aether_converter >= 0.2.14 (``AETHER_CONVERTER_0214``, else
    AETHER_BIN_DIR's / PATH's; ``buildings_pbf_list``, toolkit CONTRACT
    changelog 51; skipped without one) on the stack of
    :class:`RealConverterPhase2BesideItsBuildings`:
    Phase 2 is launched while A's building tiles are still being fetched,
    its job carries THE store and its list — nothing is linked, no view
    made — it waits for the list and writes the bytes the view (0.2.12
    marker) mode of the same binary writes."""

    def _converter(self):
        return _list_converter(self)

    def test_phase2_reads_its_list_and_writes_the_view_bytes(self):
        terrain = (lambda z, x, y: _terrarium_png(z, x, y, 900.0))
        # Every building tile is answered once Phase 2 waits for it (+1 s),
        # in both runs (each has its own cache, so fetches anew).
        hold = _HeldTiles(self, _WAITING, after_s=_AFTER_LINE_S)
        with OfmServer(default=building_tile([HOUSE]), terrain=terrain,
                       delay_fn=hold) as srv, hold.client():
            listed = self._run(srv, "list", lists=True, on_line=hold.saw)
            hold.check("\n".join(listed["log"]))
            hold.rearm()
            viewed = self._run(srv, "view", on_line=hold.saw)
            hold.check("\n".join(viewed["log"]))
        log, log0 = listed["log"], viewed["log"]
        self.assertEqual(listed["err"], [], log[-12:])
        self.assertEqual(viewed["err"], [], log0[-12:])
        name = self.n["A"]
        for rec in (listed, viewed):
            self.assertEqual(rec["ok"], [rec["out"]])
            (job,) = rec["jobs"]                  # Phase 2's, tile A
            self.assertEqual(os.path.basename(job["output_path"]), name)
        (job,), (job0,) = listed["jobs"], viewed["jobs"]
        cache = os.path.join(self.root, "cache_list")
        with mock.patch.object(ta, "get_cache_dir", return_value=cache):
            store = ta.osm_buildings_store_dir()
            lists = ta.osm_buildings_list_dir()
        self.assertEqual(job["buildings_pbf_dir"], os.path.abspath(store))
        path = job["buildings_pbf_list"]
        self.assertEqual(os.path.dirname(path), lists)
        self.assertNotIn("buildings_pbf_ready", job)
        self.assertEqual(job["buildings_pbf_wait_s"], 14400)
        self.assertEqual(job0["buildings_pbf_ready"], "_ready")
        self.assertNotIn("buildings_pbf_list", job0)
        # The list names the tiles the view of the other run holds; the
        # list run made no view (the lists' directory only).
        with open(path, encoding="utf-8") as fh:
            names = [n for n in fh.read().split("\n") if n]
        self.assertEqual(sorted(names), sorted(
            n for n in os.listdir(job0["buildings_pbf_dir"])
            if n.endswith(".pbf")))
        self.assertEqual(os.listdir(os.path.join(cache, "views")),
                         [os.path.basename(lists)])
        # Launched before the building tiles were in: it waited for its
        # list, then decoded the listed tiles from the store.
        waiting = log.index(f"[Buildings] waiting for the building tiles "
                            f"({path})")
        ready = next(i for i, m in enumerate(log)
                     if m.startswith("Buildings ready: "))
        self.assertLess(waiting, ready, log[-20:])
        self.assertTrue(any(m.startswith(f"[Rust] Decoding {len(names)} "
                                         f"listed building tiles from ")
                            for m in log), log[-20:])
        self.assertIn(f"  buildings: {len(names)} OpenFreeMap tile(s) listed "
                      f"for the converter in {path} — the converter burns "
                      f"them now", log)
        end = [m for m in log if m.startswith("  buildings: burned onto ")]
        end0 = [m for m in log0 if m.startswith("  buildings: burned onto ")]
        self.assertEqual(len(end), 1, log[-20:])
        waited = re.search(r" — the converter waited (\d+\.\d) s for them$",
                           end[0])
        self.assertIsNotNone(waited, end[0])
        self.assertGreater(float(waited.group(1)), 0.0)
        # The same burn, the same bytes.
        self.assertEqual(end[0].split(" — the converter waited")[0],
                         end0[0].split(" — the converter waited")[0])
        with open(os.path.join(listed["out"], name), "rb") as a, \
                open(os.path.join(viewed["out"], name), "rb") as b:
            self.assertEqual(a.read(), b.read())


if __name__ == "__main__":
    unittest.main()
