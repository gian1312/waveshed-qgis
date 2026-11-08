"""Map Converter pipeline (2026-10-23): the terrain download per latitude
band, a Phase-3 batch per band on one ingest queue, buildings per batch.

Pinned here with the shipped worker and fakes for the engine, the pool
download and the free space (as ``tests/test_two_phase_convert``):

* bands: height = the coarsest output tile extent of the run (at least
  0.25 deg), north to south; per pool ONE download job per band and zoom,
  finest zoom first; the hole pass runs right after the band that is
  downloading when Phase 2 ends;
* a band's tiles are converted while the next band downloads; never two
  converter ingests at once (Phase 2 first); one job file, progress slot
  and set of manifest records per batch;
* the building fetch runs in batch order and a batch waits only for ITS
  tiles (``_pbf_view`` returns while the fetch goes on); on aether_converter
  >= 0.2.12 (toolkit CONTRACT item 48) no batch waits: Phase 2 and every
  band batch launch with their view's marker fields, the fetch links their
  tiles and writes the marker as soon as they are in (at once when they
  are), the slot says "waiting for building tiles" while the converter
  waits, the end-of-batch line says how long; a fetch failure kills the
  waiting batch and ends the run once; an older converter waits as before;
  on >= 0.2.14 (CONTRACT changelog 51) the jobs carry THE store and the
  batch's list instead — nothing linked, no view made, the list written
  atomically once the batch's tiles are in (at once when they are), a
  stale one removed first (cannot → the view), none after a failure or
  Stop; 0.2.13 keeps the view;
* Stop during band 2 kills every process and leaves no scratch, job file
  or tile; a failure in band 2 keeps band 1's finished tiles;
* the epsilon snap of converter >= 0.2.9 (``_snap_bbox``: epsilon + edge
  rounded to the grid line; ``_enumerate_tiles``: epsilon start),
  version-gated;
* tiles outside every source (owner, 2026-11-07): after the grid
  cross-check, a tile whose bbox intersects no terrain row's extent is
  skipped with one log line — never downloaded, written or recorded; the
  pre-run estimate counts what the run builds.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import json
import os
import re
import tempfile
import threading
import time
import unittest
from collections import Counter
from unittest import mock

import waveshed.core.terrain_adapter as ta
from waveshed.core import binary_manager as bmod
from waveshed.core import openfreemap as ofm
from waveshed.core import osm_buildings as ob
from waveshed.core import output_estimate as oe
from waveshed.core import run_progress as rp
import waveshed.gui.map_converter_tab as mct
from waveshed.gui.map_converter_tab import _LayerEntry, _MapConverterWorker

from test_osm_buildings import read_building_list
from test_two_phase_convert import (_EXT, _HUGE, _XYZ, _Converter, _Run,
                                    _b, _files, _info, _sleeper, _tab,
                                    _tiles, _wait_for)


def _xyz_only():
    """An XYZ-only stack over _EXT (fresh: the worker fills its entries):
    at 30 m two 0.5 deg bands, C D (north) and A B (south) — every tile
    downloaded."""
    return [{"kind": "xyz", "uri": _XYZ, "extent": dict(_EXT)}]


def _names(jobs):
    return sorted(os.path.basename(j["output_path"]) for j in jobs)


def _files_a():
    """A file whose rectangle covers tile A (south band) plus its halo
    only: A is Phase 2's, B C D are downloaded (bands C D, then B)."""
    return [_info("/dem/a.tif", _b(7.99, 8.51, 46.99, 47.51))]


class _BandRun(_Run):
    """:class:`test_two_phase_convert._Run` over any resolutions and
    extent, optionally with a Download Buildings… row."""

    def __init__(self, test, out_dir, cache_dir, resolutions=(30,),
                 extent=None, osm_extents=(), **kw):
        super().__init__(test, out_dir, cache_dir, **kw)
        self.resolutions = list(resolutions)
        self.extent = dict(extent or _EXT)
        self.osm_extents = [dict(e) for e in osm_extents]

    def run(self):
        layers = [_LayerEntry(layer_type="raster", source_path=f"/src{k}",
                              extent=dict(self.extent))
                  for k in range(len(self.entries))]
        for ext in self.osm_extents:
            layers.append(_LayerEntry(
                layer_type="buildings", source_path=mct._OSM_PLACEHOLDER,
                crs_authid="EPSG:4326", extent=dict(ext)))
        resolved = {f"/src{k}": e for k, e in enumerate(self.entries)}
        worker = _MapConverterWorker(layers, self.out_dir, self.resolutions,
                                     [e.source_path for e in layers],
                                     overwrite=self.overwrite)
        self.worker = worker
        worker.status.connect(self.status.append)
        worker.log_line.connect(self.logs.append)
        worker.finished_err.connect(self.errs.append)
        worker.finished_ok.connect(self.oks.append)
        with mock.patch.object(_MapConverterWorker, "_resolve_raster",
                               side_effect=lambda e: resolved[
                                   e["source_path"]]), \
             mock.patch.object(_MapConverterWorker, "_resolve_buildings",
                               return_value=None), \
             mock.patch.object(mct, "find_binary", return_value="/bin/conv"), \
             mock.patch.object(mct, "_plan_cross_check"), \
             mock.patch.object(mct, "_converter_reports_tiles",
                               return_value=self.reports_tiles), \
             mock.patch.object(ta, "preflight_ingest_jobs"), \
             mock.patch.object(ta, "get_cache_dir",
                               return_value=self.cache_dir), \
             mock.patch.object(ta, "ensure_pool_tiles",
                               side_effect=self.pool), \
             mock.patch.object(oe, "free_bytes",
                               side_effect=lambda _p: self.free["v"]), \
             mock.patch.object(ta, "run_converter_streaming",
                               side_effect=self.converter):
            worker.run()
        return self


class _Case(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.out = os.path.join(self.root, "out")
        self.cache = os.path.join(self.root, "cache")
        self.temp = os.path.join(self.root, "temp")
        os.makedirs(self.cache)
        os.makedirs(self.temp)
        self.expected, self.n = _tiles()

    def tearDown(self):
        self._tmp.cleanup()

    def run_(self, **kw):
        with mock.patch.object(tempfile, "tempdir", self.temp):
            return _BandRun(self, self.out, self.cache, **kw).run()

    def outputs(self):
        if not os.path.isdir(self.out):
            return set()
        return {n for n in os.listdir(self.out) if n.endswith(".abt")}

    def leftovers(self):
        """Scratch a run must never leave: Phase-2 dirs, manifest temps in
        the output dir, job files in %TEMP%."""
        out = [n for n in (os.listdir(self.out) if os.path.isdir(self.out)
                           else ())
               if n.startswith(mct._PHASE2_SCRATCH_PREFIX)
               or n.endswith(".tmp")]
        return out + sorted(os.listdir(self.temp))


# ---------------------------------------------------------------------------
# Bands
# ---------------------------------------------------------------------------

class Bands(_Case):

    def test_band_height_is_the_coarsest_tile_extent_at_least_a_quarter(self):
        self.assertEqual(mct._band_height([2]), 0.25)
        self.assertEqual(mct._band_height([5, 10]), 0.25)
        self.assertEqual(mct._band_height([2, 30]), 0.5)
        self.assertEqual(mct._band_height([2, 5, 10, 30, 90]), 1.0)
        self.assertEqual(mct._band_height([250]), 2.0)

    def test_band_of_is_the_row_of_the_tile_centre(self):
        h = mct._band_height([2, 30])
        for res, tile in mct._enumerate_tiles(mct._snap_bbox(
                _EXT, [2, 30]), [2, 30]):
            band = mct._band_of(res, tile, h)
            centre = tile["ul_lat"] - mct._subtile_degrees(res) / 2
            self.assertLessEqual(band * h, centre)
            self.assertLess(centre, (band + 1) * h)
            # A 30 m tile is a whole band; 2 m tiles lie inside one.
            self.assertGreaterEqual(tile["ul_lat"] - mct._subtile_degrees(res),
                                    band * h - 1e-9)
            self.assertLessEqual(tile["ul_lat"], (band + 1) * h + 1e-9)

    def test_calls_split_north_to_south_finest_zoom_first(self):
        expected = mct._enumerate_tiles(mct._snap_bbox(_EXT, [2, 30]),
                                        [2, 30])
        entries = _xyz_only()
        plan = mct._xyz_download_plan(entries, expected)
        # The plan itself stays zoom-ascending (one call per zoom).
        self.assertEqual([z for _k, _e, z, _g in plan], [12, 15])
        bands = mct._band_calls(mct._pool_calls(plan), 0.5)
        self.assertEqual([b for b, _c in bands], [95, 94])     # N, then S
        for band, calls in bands:
            self.assertEqual([z for (_k, _e, z, _g), _m in calls], [15, 12])
            for (_k, _e, _z, group), members in calls:
                for res, tile in group:
                    self.assertEqual(mct._band_of(res, tile, 0.5), band)
                self.assertEqual(members, [(entries[0], group)])
        # Every tile once, in exactly one band.
        flat = [t["filename"] for _b2, calls in bands
                for (_k, _e, _z, g), _m in calls for _r, t in g]
        self.assertEqual(sorted(flat), sorted(t["filename"]
                                              for _r, t in expected))

    def test_the_worker_downloads_band_by_band_finest_zoom_first(self):
        # 2 m + 30 m over 47-48 N, 8.0-8.1 E: per 0.5 deg band one z15 job
        # (five 2 m tiles), then one z12 job (one 30 m tile). One lane
        # (WAVESHED_SERIAL_ZOOMS=1) — the order before the two lanes, call
        # for call; the two lanes: tests/test_download_lanes.py.
        ext = {"south": 47.0, "north": 48.0, "west": 8.0, "east": 8.1}
        entries = [{"kind": "xyz", "uri": _XYZ, "extent": dict(ext)}]
        with mock.patch.dict(os.environ, {mct.SERIAL_ZOOMS_ENV: "1"}):
            r = self.run_(entries=entries, resolutions=(2, 30), extent=ext)
        self.assertEqual(r.errs, [])
        zooms, lats = [], []
        for call in r.pool_calls:
            res = {int(n.rsplit("_", 1)[1][:-5]) for n in call}
            zooms.append(15 if res == {2} else 12 if res == {30} else res)
            lats.append({float(n.split("_")[1][1:6]) for n in call})
        self.assertEqual(zooms, [15, 12, 15, 12])
        self.assertEqual([len(c) for c in r.pool_calls], [5, 1, 5, 1])
        # North band first: every tile of the first two calls lies north
        # of every tile of the last two.
        self.assertGreater(min(lats[0] | lats[1]), max(lats[2] | lats[3]))
        self.assertIn("Downloading terrain for 12 tile(s) from 1 XYZ "
                      "entr(ies) via the shared pool: 2 band(s) × z15, z12 "
                      "— per pool one job per band and zoom, finest zoom "
                      "first, the 0.5° bands north to south", r.logs)
        # One Phase-3 batch per band.
        self.assertEqual([len(c["jobs"]) for c in r.converter.calls], [6, 6])
        self.assertEqual(r.oks, [self.out])

    def test_the_hole_pass_runs_after_the_band_downloading_now(self):
        # Phase 2 converts A (a hole) while band 1 (C D) downloads; band 1
        # holds until the hole unit is queued on the pool's thread: the
        # hole's download comes next, before band 2 (B).
        n = self.n
        holder = {}

        def pool(call_no, specs, kw):
            if call_no == 1:
                branch = holder["r"].worker._branches[0]
                self.assertTrue(_wait_for(lambda: any(
                    key == ("holes",) for key, _c in list(branch._units))),
                    "the hole unit was never queued")

        r = _BandRun(self, self.out, self.cache,
                     entries=[{"kind": "files", "infos": _files_a()},
                              {"kind": "xyz", "uri": _XYZ,
                               "extent": dict(_EXT)}],
                     converter=_Converter(coverage={n["A"]: (1, 2)}))
        holder["r"] = r
        r.on_pool = pool
        with mock.patch.object(tempfile, "tempdir", self.temp):
            r.run()
        self.assertEqual(r.errs, [])
        self.assertEqual(r.pool_calls, [sorted([n["C"], n["D"]]), [n["A"]],
                                        [n["B"]]])
        self.assertEqual(self.outputs(), set(n.values()))


# ---------------------------------------------------------------------------
# The ingest queue
# ---------------------------------------------------------------------------

class _Recorder(_Converter):
    """The fake converter, plus: job files seen (existing at launch), and
    the largest number of ingests running at once (each takes *hold_s*).
    *on_batch(job_file)* runs first; a value it returns is the exit code
    (nothing written — a killed batch)."""

    def __init__(self, hold_s=0.0, on_batch=None, **kw):
        super().__init__(**kw)
        self.hold_s = hold_s
        self.on_batch = on_batch
        self.job_files = []
        self.running = 0
        self.most = 0
        self._lock = threading.Lock()

    def __call__(self, exe, args, on_line, on_start=None, env=None):
        with self._lock:
            self.running += 1
            self.most = max(self.most, self.running)
            self.job_files.append((args[-1], os.path.isfile(args[-1])))
        try:
            self.on_start = on_start
            if self.on_batch is not None:
                rc = self.on_batch(args[-1])
                if rc is not None:
                    return rc
            time.sleep(self.hold_s)
            with open(args[-1], encoding="utf-8") as fh:
                jobs = json.load(fh)
            for job in jobs:           # the burn report a real one prints
                d = job.get("buildings_pbf_dir")
                if d:
                    k = len(os.listdir(d))
                    on_line(f"[Info] buildings_pbf_dir: {k} tile(s), {k} "
                            f"building(s) ({k} after dedup), {k} drawn, "
                            f"{k} pixel(s) raised")
            return super().__call__(exe, args, on_line, on_start, env)
        finally:
            with self._lock:
                self.running -= 1


class IngestQueue(_Case):

    def test_a_band_converts_while_the_next_band_downloads(self):
        n = self.n
        band2_reported = threading.Event()
        band1_converting = threading.Event()
        seen = {}

        def on_batch(job_file):
            if not band1_converting.is_set():
                # Band 1's batch: the download of band 2 is under way.
                seen["band2_reported"] = band2_reported.wait(10)
                seen["label"] = r.worker.progress_model.label()
                band1_converting.set()

        def pool(call_no, specs, kw):
            if call_no == 2:
                kw["progress_cb"].download_event({
                    "event": "progress", "done_units": 5, "total_units": 10,
                    "ceiling_units": 5, "pass": 1, "max_passes": 3,
                    "zoom": 12, "pass_done": 5, "pass_total": 10,
                    "mbps": 2.5, "errors": 0})
                band2_reported.set()
                # Only a batch converting NOW can release this download.
                seen["converted_during_band2"] = band1_converting.wait(10)

        conv = _Recorder(on_batch=on_batch)
        r = _BandRun(self, self.out, self.cache, entries=_xyz_only(),
                     converter=conv)
        r.on_pool = pool
        with mock.patch.object(tempfile, "tempdir", self.temp):
            r.run()
        self.assertEqual(r.errs, [])
        self.assertTrue(seen["band2_reported"])
        self.assertTrue(seen["converted_during_band2"],
                        "band 1 was not converted while band 2 downloaded")
        self.assertEqual(r.pool_calls, [sorted([n["C"], n["D"]]),
                                        sorted([n["A"], n["B"]])])
        self.assertEqual([_names(c["jobs"]) for c in conv.calls],
                         [sorted([n["C"], n["D"]]),
                          sorted([n["A"], n["B"]])])
        # The one bar's label names the batch and the band downloading.
        self.assertIn("Converting band 1/2: 0/2 tiles", seen["label"])
        self.assertIn("Downloading terrain z12 (30 m) band 2/2: 5/10 source "
                      "tiles", seen["label"])
        self.assertIn("Converting band 2/2: 2 .abt tiles...", r.status)
        self.assertEqual(self.outputs(), set(n.values()))
        self.assertEqual(self.leftovers(), [])

    def test_one_ingest_at_a_time_phase2_first_and_unique_job_files(self):
        # Phase 2 (A B C, holes in B and C), D's band, the holes: three
        # ingests, each held 0.2 s — never two at once, Phase 2 first.
        n = self.n
        conv = _Recorder(hold_s=0.2,
                         coverage={n["B"]: (1, 2), n["C"]: (3, 4)})
        r = self.run_(converter=conv)
        self.assertEqual(r.errs, [])
        self.assertEqual(conv.most, 1, "two converter ingests at once")
        self.assertEqual([c["phase2"] for c in conv.calls],
                         [True, False, False])
        self.assertEqual(sorted(_names(c["jobs"]) for c in conv.calls[1:]),
                         [sorted([n["B"], n["C"]]), [n["D"]]])
        # One job file per batch — Phase 2's and each batch's own, all
        # there at launch, all gone after the run.
        pid = os.getpid()
        files = [os.path.basename(p) for p, _there in conv.job_files]
        self.assertEqual(files[0], f"aether_conv_{pid}_files.json")
        self.assertEqual(sorted(files[1:]), [f"aether_conv_{pid}_1.json",
                                             f"aether_conv_{pid}_2.json"])
        self.assertEqual(len(set(files)), 3)
        self.assertTrue(all(there for _p, there in conv.job_files))
        self.assertEqual([os.path.dirname(p) for p, _t in conv.job_files],
                         [self.temp] * 3)
        self.assertEqual(self.leftovers(), [])
        self.assertIn("Phase 3: 3 tile(s) converted with the whole stack in "
                      "2 batch(es).", r.logs)
        self.assertIn("Conversion complete: 4 tiles.", r.logs)

    def test_manifest_holds_the_records_of_every_batch(self):
        r = self.run_(entries=_xyz_only())
        self.assertEqual(r.errs, [])
        self.assertEqual(len(r.converter.calls), 2)
        full = mct._stack_tile_fingerprints(self.expected, _xyz_only(), None)
        self.assertEqual(mct._read_output_manifest(self.out), full)
        # A second run rebuilds nothing.
        again = self.run_(entries=_xyz_only())
        self.assertEqual(again.converter.calls, [])
        self.assertEqual(again.status[-1], "All tiles already exist.")

    def test_manifest_writes_never_lose_each_others_records(self):
        os.makedirs(self.out)

        def writer(k):
            for i in range(40):
                mct._write_output_manifest(self.out, {f"t{k}_{i}": f"fp{k}"})

        threads = [threading.Thread(target=writer, args=(k,))
                   for k in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        tiles = mct._read_output_manifest(self.out)
        self.assertEqual(len(tiles), 8 * 40)
        self.assertEqual(tiles["t3_39"], "fp3")
        self.assertEqual(os.listdir(self.out), [mct._OUTPUT_MANIFEST])

    def test_each_batch_gets_its_own_progress_slot(self):
        r = self.run_(entries=_xyz_only())
        model = r.worker.progress_model
        tile = {t["filename"]: t for _r, t in self.expected}
        n = self.n
        self.assertEqual(model.work(("convert", 1)), mct._convert_work(
            [tile[n["C"]], tile[n["D"]]]))
        self.assertEqual(model.work(("convert", 2)), mct._convert_work(
            [tile[n["A"]], tile[n["B"]]]))
        self.assertEqual(model.work(mct._CONVERT_PENDING), 0.0)
        self.assertEqual(model.percent(), 100)


class SlotLedger(unittest.TestCase):
    """One download slot, served by one call per band: the bar never goes
    back and the slot is complete after the last band only."""

    def test_two_band_calls_report_into_one_slot(self):
        model = rp.RunProgress()
        expected = mct._enumerate_tiles(mct._snap_bbox(_EXT, [30]), [30])
        calls = mct._pool_calls(mct._xyz_download_plan(
            [{"kind": "xyz", "uri": _XYZ, "extent": None}], expected))
        bands = mct._band_calls(calls, 0.5)
        key = ("download", 0, 12)
        ledger = mct._SlotLedger(model)
        for band, _c in bands:
            ledger.add(key, band, 100.0)
        model.set_work(key, ledger.total(key))
        model.set_work(("convert",), 50.0)
        fracs = []

        def fake_ensure(uri, specs, res, progress_cb=None, **_kw):
            for d in (0, 40, 120):      # the band's real total is 120
                progress_cb.download_event({
                    "event": "progress", "done_units": d,
                    "total_units": 120, "ceiling_units": d, "pass": 1,
                    "max_passes": 3, "zoom": 12, "pass_done": d,
                    "pass_total": 120})
                fracs.append(model.fraction())
            return {t["filename"]: "/p" for t in specs}

        with mock.patch.object(ta, "ensure_pool_tiles",
                               side_effect=fake_ensure):
            for i, (band, band_calls) in enumerate(bands):
                mct._run_pool_calls(band_calls, progress_model=model,
                                    ledger=ledger, band=band,
                                    tag=f" band {i + 1}/2")
                if i == 0:
                    self.assertEqual(model.done(key), 120.0)
                    self.assertEqual(model.work(key), 220.0)
                    self.assertLess(model.fraction(), 1.0)
        self.assertEqual(fracs, sorted(fracs))
        self.assertEqual(model.done(key), 240.0)
        self.assertEqual(model.work(key), 240.0)
        self.assertIn("Downloading terrain z12 (30 m) band 2/2",
                      model.label())
        model.finish(("convert",))
        self.assertEqual(model.fraction(), 1.0)


# ---------------------------------------------------------------------------
# Buildings per batch
# ---------------------------------------------------------------------------

#: OSM extents inside tile A (Phase 2's) and tile D (band 1's).
_OSM_A = {"south": 47.30, "north": 47.31, "west": 8.40, "east": 8.41}
_OSM_D = {"south": 47.70, "north": 47.71, "west": 8.70, "east": 8.71}


class _FakeFetch:
    """``openfreemap.fetch_building_tiles`` stand-in: writes each tile into
    the store (a tile with data) and records each call's names; a call
    holding a name of *hold* waits for :attr:`release`."""

    def __init__(self, hold=()):
        self.hold = set(hold)
        self.release = threading.Event()
        self.calls = []
        self.holding = threading.Event()

    def __call__(self, tiles, store, template, should_cancel=None,
                 on_start=None, on_progress=None, **_kw):
        names = [ofm.tile_filename(*t) for t in tiles]
        self.calls.append(sorted(names))
        if self.hold & set(names):
            self.holding.set()
            while not self.release.wait(0.05):
                if should_cancel is not None and should_cancel():
                    raise ofm.FetchCancelled()
        os.makedirs(store, exist_ok=True)
        for k, name in enumerate(names, start=1):
            with open(os.path.join(store, name), "wb") as fh:
                fh.write(b"\x1f\x8bpbf")
            if on_progress is not None:
                on_progress(k, len(names), 5 * k)
        return ofm.FetchResult(len(names), 0, len(names), 0, 5 * len(names),
                               0.0, tuple(sorted(names)))


class BuildingsPerBatch(_Case):

    def osm(self):
        return mct._OsmBuildings([_OSM_A, _OSM_D])

    def tile(self, key):
        return [rt for rt in self.expected
                if rt[1]["filename"] == self.n[key]]

    def worker_with_branch(self, groups):
        worker = _MapConverterWorker([], self.out, [30], [])
        osm = self.osm()
        worker._buildings = mct._RunBuildings(osm=osm)
        worker._bld_branch = mct._BuildingsBranch(
            worker, osm, [osm.needed(g) for g in groups])
        return worker, osm

    def test_pbf_view_returns_once_its_own_tiles_are_in(self):
        osm = self.osm()
        names_d = {ofm.tile_filename(*t) for t in osm.needed(self.tile("D"))}
        fetch = _FakeFetch(hold=names_d)
        logs = []
        with mock.patch.object(ta, "get_cache_dir", return_value=self.cache), \
             mock.patch.object(ofm, "resolve_tile_template_strict",
                               return_value="http://x/{z}/{x}/{y}.pbf"), \
             mock.patch.object(ofm, "fetch_building_tiles",
                               side_effect=fetch):
            worker, osm = self.worker_with_branch(
                [self.tile("A"), self.tile("D")])
            worker.log_line.connect(logs.append)
            branch = worker._bld_branch
            branch.start()
            self.assertTrue(fetch.holding.wait(10))
            # A's tiles are in, D's are still being fetched: A's view now.
            view_a = worker._pbf_view(self.tile("A"))
            self.assertTrue(branch.alive(), "the fetch was over")
            self.assertEqual(sorted(os.listdir(view_a)), sorted(
                ofm.tile_filename(*t) for t in osm.needed(self.tile("A"))))
            # D's batch waits — until the fetch delivers its tiles.
            got = {}
            waiter = threading.Thread(target=lambda: got.setdefault(
                "view", worker._pbf_view(self.tile("D"))))
            waiter.start()
            time.sleep(0.3)
            self.assertTrue(waiter.is_alive(), "D did not wait for its tiles")
            fetch.release.set()
            waiter.join(10)
            branch.join()
        self.assertFalse(waiter.is_alive())
        self.assertEqual(sorted(os.listdir(got["view"])), sorted(names_d))
        self.assertIsNone(branch.error)
        self.assertEqual(fetch.calls, [sorted(
            ofm.tile_filename(*t) for t in osm.needed(self.tile("A"))),
            sorted(names_d)])
        self.assertIn("  waiting for the building tiles (OpenFreeMap z14) "
                      "before converting...", logs)
        self.assertEqual(len([m for m in logs
                              if m.startswith("Buildings ready: ")]), 1)

    def test_a_batch_waits_until_some_tile_has_data(self):
        # A group without data says nothing yet: a fetch where NO tile has
        # data fails — no batch may run before that is ruled out.
        with mock.patch.object(ta, "get_cache_dir", return_value=self.cache):
            worker, osm = self.worker_with_branch([self.tile("A")])
            branch = worker._bld_branch
            names = {ofm.tile_filename(*t) for t in osm.needed(self.tile("A"))}
            branch._mark(names, data=False)
            self.assertFalse(branch.ready_for(names))
            branch._mark((), data=True)
            self.assertTrue(branch.ready_for(names))
            branch.error = "Buildings download failed: x"
            self.assertTrue(branch.ready_for({"other.pbf"}))

    def test_the_run_fetches_phase2_tiles_first_and_does_not_wait_for_all(
            self):
        # Phase 2 (A B C) needs A's building tiles, band 1 (D) needs D's:
        # the fetch asks A's first; Phase 2 converts while D's are still
        # being fetched; D's batch waits for D's.
        n = self.n
        osm = self.osm()
        names_a = sorted(ofm.tile_filename(*t)
                         for t in osm.needed(self.tile("A")))
        names_d = sorted(ofm.tile_filename(*t)
                         for t in osm.needed(self.tile("D")))
        fetch = _FakeFetch(hold=names_d)
        seen = []

        def on_batch(job_file):
            seen.append(fetch.release.is_set())
            if len(seen) == 1:
                fetch.release.set()      # Phase 2 ran; now D's tiles come

        conv = _Recorder(on_batch=on_batch)
        from test_two_phase_convert import _files
        r = _BandRun(self, self.out, self.cache, converter=conv,
                     osm_extents=[_OSM_A, _OSM_D],
                     entries=[{"kind": "files", "infos": _files()},
                              {"kind": "xyz", "uri": _XYZ,
                               "extent": dict(_EXT)}])
        with mock.patch.object(tempfile, "tempdir", self.temp), \
             mock.patch.object(ofm, "resolve_tile_template_strict",
                               return_value="http://x/{z}/{x}/{y}.pbf"), \
             mock.patch.object(ofm, "fetch_building_tiles",
                               side_effect=fetch):
            r.run()
        self.assertEqual(r.errs, [])
        self.assertEqual(fetch.calls, [names_a, names_d])
        # Phase 2 ran before D's building tiles were fetched; D's batch
        # after.
        self.assertEqual(seen, [False, True])
        self.assertEqual([c["phase2"] for c in conv.calls], [True, False])
        with_pbf = {os.path.basename(j["output_path"]): j["buildings_pbf_dir"]
                    for c in conv.calls for j in c["jobs"]
                    if j.get("buildings_pbf_dir")}
        self.assertEqual(sorted(with_pbf), sorted([n["A"], n["D"]]))
        self.assertEqual(sorted(os.listdir(with_pbf[n["D"]])), names_d)
        self.assertEqual(sorted(os.listdir(with_pbf[n["A"]])), names_a)


# ---------------------------------------------------------------------------
# Batches launched before their building tiles (aether_converter >= 0.2.12)
# ---------------------------------------------------------------------------

class _MarkerConverter(_Converter):
    """The fake converter as aether_converter 0.2.12 runs a batch whose jobs
    carry ``buildings_pbf_ready`` (CONTRACT item 48), over a REAL process (a
    sleeper registered through on_start — what a halt or Stop kills): a
    marker missing → the wait line once (*on_wait(jobs, proc)* then), the
    markers polled every 20 ms, the ready line; then the view read ONCE
    (:attr:`read`, its ``.pbf`` names) and per job carrying it the burn
    report; then the tiles, as :class:`_Converter`. Jobs without the field
    (an older converter) read the view at once. Killed while waiting: its
    exit code, nothing written (:attr:`killed`)."""

    def __init__(self, test, on_wait=None, **kw):
        super().__init__(**kw)
        self.test = test
        self.on_wait = on_wait
        self.launched, self.marker_at_launch = [], []
        self.read, self.killed = [], []

    def __call__(self, exe, args, on_line, on_start=None, env=None):
        with open(args[-1], encoding="utf-8") as fh:
            jobs = json.load(fh)
        self.launched.append(time.monotonic())
        markers = sorted({os.path.join(j["buildings_pbf_dir"],
                                       j["buildings_pbf_ready"])
                          for j in jobs if j.get("buildings_pbf_ready")})
        self.marker_at_launch.append(any(os.path.lexists(m)
                                         for m in markers))
        proc = _sleeper(self.test)
        on_start(proc)
        try:
            missing = [m for m in markers if not os.path.isfile(m)]
            if missing:
                on_line(f"[Buildings] waiting for the building tiles "
                        f"({missing[0]})")
                if self.on_wait is not None:
                    self.on_wait(jobs, proc)
                t0 = time.monotonic()
                while not all(os.path.isfile(m) for m in markers):
                    if proc.poll() is not None:
                        self.killed.append(proc.returncode)
                        return proc.returncode
                    time.sleep(0.02)
                on_line(f"[Buildings] building tiles ready after "
                        f"{time.monotonic() - t0:.1f} s")
            for d in sorted({j["buildings_pbf_dir"] for j in jobs
                             if j.get("buildings_pbf_dir")}):
                self.read.append(sorted(n for n in os.listdir(d)
                                        if n.endswith(".pbf")))
            for job in jobs:
                d = job.get("buildings_pbf_dir")
                if d:
                    k = sum(1 for n in os.listdir(d) if n.endswith(".pbf"))
                    on_line(f"[Info] buildings_pbf_dir: {k} tile(s), {k} "
                            f"building(s) ({k} after dedup), {k} drawn, "
                            f"{k} pixel(s) raised")
            return super().__call__(exe, args, on_line, on_start, env)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


class _FailingFetch(_FakeFetch):
    """:class:`_FakeFetch` whose call naming a tile of *fail* waits for
    *gate* (a batch's converter is waiting by then) and then fails as the
    strict fetch does."""

    def __init__(self, fail, gate):
        super().__init__()
        self.fail = set(fail)
        self.gate = gate

    def __call__(self, tiles, store, template, should_cancel=None, **kw):
        names = [ofm.tile_filename(*t) for t in tiles]
        if self.fail & set(names):
            self.calls.append(sorted(names))
            while not self.gate.wait(0.05):
                if should_cancel is not None and should_cancel():
                    raise ofm.FetchCancelled()
            raise ofm.FetchFailed(
                f"1 of {len(names)} building tiles missing — tile "
                f"{names[0]}: HTTP 500", missing=1, total=len(names),
                cause=f"{names[0]}: HTTP 500")
        return super().__call__(tiles, store, template,
                                should_cancel=should_cancel, **kw)


class _ListConverter(_MarkerConverter):
    """The fake converter as aether_converter 0.2.14 runs a batch whose jobs
    carry ``buildings_pbf_list`` (toolkit CONTRACT changelog 51), over a
    REAL process: the list with ``buildings_pbf_ready`` → exit 1; a list
    missing → the wait line once (*on_wait(jobs, proc)* then), the lists
    polled every 20 ms, the ready line; then each list read ONCE — its
    names opened in ``buildings_pbf_dir``, never a listing of it
    (:attr:`read`); a listed name not there → the contract's error, exit 1,
    nothing written (:attr:`errors`); the 0.2.14 decode lines and per job
    carrying it the burn report; then the tiles, as :class:`_Converter`.
    Killed while waiting: its exit code, nothing written. A batch without
    the list (it took the view) is :class:`_MarkerConverter`'s."""

    def __init__(self, test, on_wait=None, **kw):
        super().__init__(test, on_wait=on_wait, **kw)
        self.list_at_launch, self.errors = [], []
        #: The jobs of every batch carrying a list, at its launch, and
        #: whether its buildings_pbf_dir (THE store) was there then.
        self.batches, self.dir_at_launch = [], []

    def __call__(self, exe, args, on_line, on_start=None, env=None):
        with open(args[-1], encoding="utf-8") as fh:
            jobs = json.load(fh)
        if not any(j.get("buildings_pbf_list") for j in jobs):
            return super().__call__(exe, args, on_line, on_start, env)
        self.launched.append(time.monotonic())
        self.batches.append(jobs)
        if any(j.get("buildings_pbf_ready") for j in jobs):
            self.errors.append("buildings_pbf_ready with buildings_pbf_list")
            on_line("Error: buildings_pbf_list and buildings_pbf_ready are "
                    "exclusive")
            return 1
        lists = sorted({(j["buildings_pbf_dir"], j["buildings_pbf_list"])
                        for j in jobs if j.get("buildings_pbf_list")})
        self.list_at_launch.append(any(os.path.lexists(p)
                                       for _d, p in lists))
        self.dir_at_launch.append(all(os.path.isdir(d) for d, _p in lists))
        proc = _sleeper(self.test)
        on_start(proc)
        try:
            missing = [p for _d, p in lists if not os.path.isfile(p)]
            if missing:
                on_line(f"[Buildings] waiting for the building tiles "
                        f"({missing[0]})")
                if self.on_wait is not None:
                    self.on_wait(jobs, proc)
                t0 = time.monotonic()
                while not all(os.path.isfile(p) for _d, p in lists):
                    if proc.poll() is not None:
                        self.killed.append(proc.returncode)
                        return proc.returncode
                    time.sleep(0.02)
                on_line(f"[Buildings] building tiles ready after "
                        f"{time.monotonic() - t0:.1f} s")
            counts = {}
            for d, p in lists:
                names = read_building_list(p)
                for name in names:
                    if not os.path.isfile(os.path.join(d, name)):
                        self.errors.append(name)
                        on_line(f"Error: Building tile {name} listed in {p} "
                                f"is not in {d}")
                        return 1
                self.read.append(sorted(names))
                counts[p] = k = len(names)
                on_line(f'[Rust] Decoding {k} listed building tiles from '
                        f'"{d}" (once for the run)...')
                on_line(f"[Rust] Building tiles decoded: {k} tile(s), {k} "
                        f"building(s) in 0.0 s")
            for job in jobs:
                p = job.get("buildings_pbf_list")
                if p:
                    k = counts[p]
                    on_line(f"[Info] buildings_pbf_dir: {k} tile(s), {k} "
                            f"building(s) ({k} after dedup), {k} drawn, "
                            f"{k} pixel(s) raised")
            return _Converter.__call__(self, exe, args, on_line, on_start,
                                       env)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


_END_OF_BATCH_RE = (r"^  buildings: burned onto \d+ of \d+ tile\(s\) — \d+ "
                    r"building\(s\) drawn, \d+ px raised")


class BatchesLaunchBeforeTheirBuildings(_Case):
    """aether_converter >= 0.2.12 (toolkit CONTRACT item 48): Phase 2 and
    every band batch are launched at once with their view's marker fields;
    the run's fetch links their tiles and writes the marker as soon as they
    are in; Phase 2 converts A (with A's buildings), band 1 converts D (with
    D's). On >= 0.2.14 (CONTRACT changelog 51, the ``test_list_mode_…``
    tests) no view at all: the jobs carry THE store and their list, written
    atomically by the fetch once the batch's tiles are in — nothing is
    linked."""

    def store(self):
        with mock.patch.object(ta, "get_cache_dir", return_value=self.cache):
            return ta.osm_buildings_store_dir()

    def list_path(self, names):
        with mock.patch.object(ta, "get_cache_dir", return_value=self.cache):
            return ta.osm_buildings_list_path(names)

    def record_links(self):
        """Every ``os.link`` destination while the test runs (the list
        mode links nothing)."""
        links = []
        real_link = os.link

        def link(src, dst, *a, **kw):
            links.append(dst)
            return real_link(src, dst, *a, **kw)

        patch = mock.patch.object(os, "link", side_effect=link)
        patch.start()
        self.addCleanup(patch.stop)
        return links

    def views(self):
        views = os.path.join(self.cache, "views")
        return sorted(os.listdir(views)) if os.path.isdir(views) else []

    def release_later(self, fetch, after=10.0):
        """A held fetch released anyway after *after* s: a regression that
        never waits for its list fails its assertions instead of hanging."""
        timer = threading.Timer(after, fetch.release.set)
        timer.daemon = True
        timer.start()
        self.addCleanup(timer.cancel)

    def names(self, key):
        osm = mct._OsmBuildings([_OSM_A, _OSM_D])
        return sorted(ofm.tile_filename(*t) for t in osm.needed(
            [rt for rt in self.expected if rt[1]["filename"] == self.n[key]]))

    def view(self, names, marker=True):
        with mock.patch.object(ta, "get_cache_dir", return_value=self.cache):
            return ta.osm_buildings_view_dir(
                names, marker=ob.READY_MARKER if marker else None)

    def run_(self, version, conv, fetch):
        r = _BandRun(self, self.out, self.cache, converter=conv,
                     osm_extents=[_OSM_A, _OSM_D],
                     entries=[{"kind": "files", "infos": _files()},
                              {"kind": "xyz", "uri": _XYZ,
                               "extent": dict(_EXT)}])
        self.r = r
        with mock.patch.object(tempfile, "tempdir", self.temp), \
             mock.patch.object(bmod, "probe_converter_version",
                               return_value=version), \
             mock.patch.object(ofm, "resolve_tile_template_strict",
                               return_value="http://x/{z}/{x}/{y}.pbf"), \
             mock.patch.object(ofm, "fetch_building_tiles",
                               side_effect=fetch):
            r.run()
        return r

    def jobs_of(self, conv, key):
        return [j for c in conv.calls for j in c["jobs"]
                if os.path.basename(j["output_path"]) == self.n[key]]

    def test_phase2_launches_before_its_tiles_are_fetched(self):
        names_a, names_d = self.names("A"), self.names("D")
        fetch = _FakeFetch(hold=names_a)
        seen = {}

        def on_wait(jobs, proc):
            # Phase 2's converter waits for A's tiles, still being fetched.
            seen.setdefault("held", not fetch.release.is_set())
            seen.setdefault("label", self.r.worker.progress_model.label())
            seen.setdefault("status", list(self.r.status))
            fetch.release.set()

        conv = _MarkerConverter(self, on_wait=on_wait)
        r = self.run_("0.2.12", conv, fetch)
        self.assertEqual(r.errs, [])
        self.assertEqual(r.oks, [self.out])
        self.assertTrue(seen["held"], "Phase 2 waited for its tiles")
        self.assertEqual([c["phase2"] for c in conv.calls], [True, False])
        self.assertEqual(conv.marker_at_launch[0], False)
        self.assertEqual(fetch.calls, [names_a, names_d])
        (job_a,) = self.jobs_of(conv, "A")
        self.assertEqual(job_a["buildings_pbf_dir"],
                         os.path.abspath(self.view(names_a)))
        self.assertEqual((job_a["buildings_pbf_ready"],
                          job_a["buildings_pbf_wait_s"]), ("_ready", 14400))
        # 0.2.12 has no list (0.2.14, CONTRACT changelog 51): none written.
        self.assertNotIn("buildings_pbf_list", job_a)
        self.assertNotIn(os.path.basename(os.path.dirname(
            self.list_path(names_a))), self.views())
        for key in "BC":                # their tiles reach no building
            (job,) = self.jobs_of(conv, key)
            self.assertFalse({"buildings_pbf_dir", "buildings_pbf_ready",
                              "buildings_pbf_wait_s"} & set(job), key)
        # The converter read exactly A's tiles, after the marker.
        self.assertEqual(conv.read[0], names_a)
        self.assertTrue(os.path.isfile(os.path.join(self.view(names_a),
                                                    ob.READY_MARKER)))
        self.assertIn("Converting tiles from local files: waiting for "
                      "building tiles", seen["label"])
        self.assertEqual(seen["status"][-1],
                         "Waiting for the building tiles...")
        logs = r.logs
        self.assertEqual(logs.count(
            "  buildings: every batch is launched at once — aether_converter "
            ">= 0.2.12 waits for its building tiles (_ready in its view)"), 1)
        self.assertIn(f"  buildings: {len(names_a)} OpenFreeMap tile(s) "
                      f"linked into {self.view(names_a)}, _ready written — "
                      f"the converter burns them now", logs)
        self.assertNotIn("  waiting for the building tiles (OpenFreeMap z14) "
                         "before converting...", logs)
        self.assertTrue(any(m.startswith("[Buildings] waiting for the "
                                         "building tiles (") for m in logs))
        ends = [m for m in logs if re.match(_END_OF_BATCH_RE, m)]
        self.assertEqual(len(ends), 2, ends)
        self.assertRegex(ends[0], _END_OF_BATCH_RE + r" — the converter "
                                  r"waited \d+\.\d s for them$")
        self.assertEqual(self.outputs(), set(self.n.values()))
        self.assertEqual(self.leftovers(), [])

    def test_a_band_batch_launches_before_its_tiles_are_fetched(self):
        names_d = self.names("D")
        fetch = _FakeFetch(hold=names_d)
        seen = {}

        def on_wait(jobs, proc):
            if any(os.path.basename(j["output_path"]) == self.n["D"]
                   for j in jobs):
                seen["held"] = not fetch.release.is_set()
                seen["label"] = self.r.worker.progress_model.label()
                fetch.release.set()

        conv = _MarkerConverter(self, on_wait=on_wait)
        r = self.run_("0.2.12", conv, fetch)
        self.assertEqual(r.errs, [])
        self.assertTrue(seen["held"], "band 1 waited for its tiles")
        (job_d,) = self.jobs_of(conv, "D")
        view_d = self.view(names_d)
        self.assertEqual(job_d["buildings_pbf_dir"], os.path.abspath(view_d))
        self.assertEqual(job_d["buildings_pbf_ready"], "_ready")
        self.assertEqual(conv.read[-1], names_d)
        self.assertTrue(os.path.isfile(os.path.join(view_d,
                                                    ob.READY_MARKER)))
        self.assertIn("Converting band 1/1: waiting for building tiles",
                      seen["label"])
        # The batch's "building view" bucket is the registration only.
        (timing,) = [m for m in r.logs
                     if m.startswith("  timing: band 1/1 ready at ")]
        view_s = float(re.search(r"building view (\d+\.\d) s", timing)
                       .group(1))
        self.assertLess(view_s, 0.3, timing)
        ends = [m for m in r.logs if re.match(_END_OF_BATCH_RE, m)]
        self.assertRegex(ends[-1], r" — the converter waited \d+\.\d s for "
                                   r"them$")
        self.assertEqual(self.outputs(), set(self.n.values()))

    def test_the_view_is_released_at_once_or_when_its_tiles_are_in(self):
        names_a, names_d = self.names("A"), self.names("D")
        fetch = _FakeFetch(hold=names_d)
        logs = []
        with mock.patch.object(ta, "get_cache_dir", return_value=self.cache), \
             mock.patch.object(ofm, "resolve_tile_template_strict",
                               return_value="http://x/{z}/{x}/{y}.pbf"), \
             mock.patch.object(ofm, "fetch_building_tiles",
                               side_effect=fetch):
            worker = _MapConverterWorker([], self.out, [30], [])
            osm = mct._OsmBuildings([_OSM_A, _OSM_D])
            worker._buildings = mct._RunBuildings(osm=osm)
            tiles = {k: [rt for rt in self.expected
                         if rt[1]["filename"] == self.n[k]] for k in "AD"}
            worker._bld_branch = mct._BuildingsBranch(
                worker, osm, [osm.needed(tiles["A"]), osm.needed(tiles["D"])])
            worker._pbf_marker = True
            worker.log_line.connect(logs.append)
            branch = worker._bld_branch
            branch.start()
            self.assertTrue(fetch.holding.wait(10))
            # A's tiles are in: linked and marked at once, in this thread.
            view_a = worker._pbf_view(tiles["A"])
            self.assertEqual(view_a, self.view(names_a))
            self.assertEqual(sorted(os.listdir(view_a)),
                             sorted(names_a + [ob.READY_MARKER]))
            # D's are not: the view comes back at once, without its marker.
            t0 = time.monotonic()
            view_d = worker._pbf_view(tiles["D"])
            self.assertLess(time.monotonic() - t0, 0.5)
            self.assertEqual(os.listdir(view_d), [])
            self.assertTrue(branch.alive())
            fetch.release.set()
            branch.join(10)
        self.assertIsNone(branch.error)
        self.assertEqual(sorted(os.listdir(view_d)),
                         sorted(names_d + [ob.READY_MARKER]))
        fields = {"buildings_pbf_ready": "_ready", "buildings_pbf_wait_s": 14400}
        self.assertEqual(worker._ready_fields(view_a), fields)
        self.assertEqual(worker._ready_fields(view_d), fields)
        self.assertIsNone(worker._ready_fields(self.view(names_a,
                                                         marker=False)))
        self.assertIsNone(worker._ready_fields(None))
        linked = [m for m in logs if "_ready written — the converter burns "
                                     "them now" in m]
        self.assertEqual(len(linked), 2, logs)

    def test_a_fetch_failure_kills_the_waiting_batch_and_ends_the_run_once(
            self):
        names_d = self.names("D")
        waiting = threading.Event()
        procs = []

        def on_wait(jobs, proc):
            if any(os.path.basename(j["output_path"]) == self.n["D"]
                   for j in jobs):
                procs.append(proc)
                waiting.set()           # D's converter waits: now it fails

        conv = _MarkerConverter(self, on_wait=on_wait)
        t0 = time.monotonic()
        r = self.run_("0.2.12", conv, _FailingFetch(names_d, waiting))
        self.assertLess(time.monotonic() - t0, 20)
        self.assertEqual(r.oks, [])
        self.assertEqual(len(r.errs), 1, r.errs)
        # The whole fetch's numbers (A's tiles and D's).
        total = len(self.names("A")) + len(names_d)
        m = re.match(rf"Buildings download failed \(OpenFreeMap z14\): 1 of "
                     rf"{total} building tiles missing — tile "
                     rf"(14_\d+_\d+\.pbf): HTTP 500\.", r.errs[0])
        self.assertIsNotNone(m, r.errs[0])
        self.assertIn(m.group(1), names_d)
        self.assertEqual(len([m for m in r.logs if m.startswith(
            "ERROR: Buildings download failed")]), 1, r.logs)
        self.assertNotIn("Cancelled.", r.status)
        # The waiting batch was killed; D is neither written nor recorded.
        self.assertEqual(len(procs), 1)
        self.assertIsNotNone(procs[0].poll())
        self.assertEqual(len(conv.killed), 1)
        self.assertNotIn(self.n["D"], self.outputs())
        self.assertNotIn(self.n["D"], mct._read_output_manifest(self.out))
        self.assertFalse(os.path.lexists(os.path.join(self.view(names_d),
                                                      ob.READY_MARKER)))
        self.assertEqual(self.leftovers(), [])

    def test_an_older_converter_waits_for_the_tiles_as_before(self):
        for i, version in enumerate(("0.2.11", None)):
            self.out = os.path.join(self.root, f"out_{i}")
            self.cache = os.path.join(self.root, f"cache_{i}")
            os.makedirs(self.cache)
            names_a, names_d = self.names("A"), self.names("D")
            fetch = _FakeFetch(hold=names_a)
            released = {}

            def release(fetch=fetch, released=released):
                time.sleep(0.5)
                released["t"] = time.monotonic()
                fetch.release.set()

            threading.Thread(target=release, daemon=True).start()
            conv = _MarkerConverter(self)
            r = self.run_(version, conv, fetch)
            self.assertEqual(r.errs, [], version)
            # Phase 2's job build waited for A's tiles, as before.
            self.assertGreater(conv.launched[0], released["t"], version)
            for call in conv.calls:
                for job in call["jobs"]:
                    self.assertFalse({"buildings_pbf_ready",
                                      "buildings_pbf_list",
                                      "buildings_pbf_wait_s"} & set(job))
            (job_a,) = self.jobs_of(conv, "A")
            self.assertEqual(job_a["buildings_pbf_dir"],
                             self.view(names_a, marker=False))
            (job_d,) = self.jobs_of(conv, "D")
            self.assertEqual(job_d["buildings_pbf_dir"],
                             self.view(names_d, marker=False))
            self.assertIn("  waiting for the building tiles (OpenFreeMap "
                          "z14) before converting...", r.logs)
            self.assertFalse([m for m in r.logs if "the converter waited" in m
                              or "launched at once" in m or "_ready" in m])
            ends = [m for m in r.logs if re.match(_END_OF_BATCH_RE, m)]
            self.assertEqual(len(ends), 2)
            self.assertTrue(all(m.endswith(" px raised") for m in ends), ends)
            views = os.path.join(self.cache, "views")
            self.assertFalse([v for v in os.listdir(views) if os.path.lexists(
                os.path.join(views, v, ob.READY_MARKER))])
            self.assertNotIn(os.path.basename(os.path.dirname(
                self.list_path(names_a))), self.views())
            self.assertEqual(self.outputs(), set(self.n.values()))

    # -- aether_converter >= 0.2.14: the list, no view (changelog 51) -------
    def test_list_mode_phase2_reads_its_list_and_nothing_is_linked(self):
        names_a, names_d = self.names("A"), self.names("D")
        fetch = _FakeFetch(hold=names_a)
        self.release_later(fetch)
        seen = {}

        def on_wait(jobs, proc):
            # Phase 2's converter waits for A's list, its tiles still being
            # fetched.
            seen.setdefault("held", not fetch.release.is_set())
            seen.setdefault("listed", os.path.lexists(list_a))
            seen.setdefault("label", self.r.worker.progress_model.label())
            seen.setdefault("status", list(self.r.status))
            fetch.release.set()

        list_a, store = self.list_path(names_a), self.store()
        conv = _ListConverter(self, on_wait=on_wait)
        links = self.record_links()
        r = self.run_("0.2.14", conv, fetch)
        self.assertEqual(r.errs, [])
        self.assertEqual(r.oks, [self.out])
        self.assertEqual(conv.errors, [])
        self.assertTrue(seen["held"], "Phase 2 waited for its tiles")
        self.assertFalse(seen["listed"], "the list came before its tiles")
        self.assertEqual(conv.list_at_launch[0], False)
        # THE store was there at the launch (a fresh cache: made by
        # ready_list — the fetch had written nothing yet).
        self.assertEqual(conv.dir_at_launch[0], True)
        self.assertEqual([c["phase2"] for c in conv.calls], [True, False])
        self.assertEqual(fetch.calls, [names_a, names_d])
        # The job: THE store + the list (absolute, under <cache>/views) +
        # the wait; never the marker.
        (job_a,) = self.jobs_of(conv, "A")
        self.assertEqual(job_a["buildings_pbf_dir"], os.path.abspath(store))
        self.assertEqual(job_a["buildings_pbf_list"], list_a)
        self.assertTrue(os.path.isabs(list_a))
        self.assertTrue(list_a.startswith(os.path.join(self.cache, "views")
                                          + os.sep), list_a)
        self.assertEqual(job_a["buildings_pbf_wait_s"], 14400)
        self.assertNotIn("buildings_pbf_ready", job_a)
        for key in "BC":                # their tiles reach no building
            (job,) = self.jobs_of(conv, key)
            self.assertFalse({"buildings_pbf_dir", "buildings_pbf_list",
                              "buildings_pbf_ready",
                              "buildings_pbf_wait_s"} & set(job), key)
        # The converter read exactly A's tiles, named by the list; nothing
        # was linked, no view directory made (the lists' one directory).
        self.assertEqual(conv.read, [names_a, names_d])
        self.assertEqual(read_building_list(list_a), names_a)
        self.assertEqual(links, [])
        self.assertEqual(self.views(),
                         [os.path.basename(os.path.dirname(list_a))])
        self.assertIn("Converting tiles from local files: waiting for "
                      "building tiles", seen["label"])
        self.assertEqual(seen["status"][-1],
                         "Waiting for the building tiles...")
        logs = r.logs
        self.assertEqual(logs.count(
            f"  buildings: every batch is launched at once — "
            f"aether_converter >= 0.2.14 waits for the list of its building "
            f"tiles and reads them from {store}"), 1)
        self.assertIn(f"  buildings: {len(names_a)} OpenFreeMap tile(s) for "
                      f"3 output tile(s) — converted now, the converter "
                      f"waits for their list {list_a}", logs)
        self.assertIn(f"  buildings: {len(names_a)} OpenFreeMap tile(s) "
                      f"listed for the converter in {list_a} — the converter "
                      f"burns them now", logs)
        self.assertIn(f"[Buildings] waiting for the building tiles "
                      f"({list_a})", logs)
        self.assertIn(f'[Rust] Decoding {len(names_a)} listed building tiles '
                      f'from "{os.path.abspath(store)}" (once for the run)...',
                      logs)
        self.assertFalse([m for m in logs if "linked into" in m
                          or "_ready" in m], logs)
        ends = [m for m in logs if re.match(_END_OF_BATCH_RE, m)]
        self.assertEqual(len(ends), 2, ends)
        self.assertRegex(ends[0], _END_OF_BATCH_RE + r" — the converter "
                                  r"waited \d+\.\d s for them$")
        self.assertEqual(self.outputs(), set(self.n.values()))
        self.assertEqual(self.leftovers(), [])

    def test_list_mode_a_band_batch_launches_before_its_tiles(self):
        names_d = self.names("D")
        list_d = self.list_path(names_d)
        fetch = _FakeFetch(hold=names_d)
        self.release_later(fetch)
        seen = {}

        def on_wait(jobs, proc):
            if any(os.path.basename(j["output_path"]) == self.n["D"]
                   for j in jobs):
                seen["held"] = not fetch.release.is_set()
                seen["listed"] = os.path.lexists(list_d)
                seen["label"] = self.r.worker.progress_model.label()
                fetch.release.set()

        conv = _ListConverter(self, on_wait=on_wait)
        r = self.run_("0.2.14", conv, fetch)
        self.assertEqual(r.errs, [])
        self.assertTrue(seen["held"], "band 1 waited for its tiles")
        self.assertFalse(seen["listed"], "the list came before its tiles")
        (job_d,) = self.jobs_of(conv, "D")
        self.assertEqual(job_d["buildings_pbf_dir"],
                         os.path.abspath(self.store()))
        self.assertEqual(job_d["buildings_pbf_list"], list_d)
        self.assertNotIn("buildings_pbf_ready", job_d)
        self.assertEqual(conv.read[-1], names_d)
        self.assertEqual(read_building_list(list_d), names_d)
        self.assertIn("Converting band 1/1: waiting for building tiles",
                      seen["label"])
        # The batch's "building view" bucket is the list's registration.
        (timing,) = [m for m in r.logs
                     if m.startswith("  timing: band 1/1 ready at ")]
        view_s = float(re.search(r"building view (\d+\.\d) s", timing)
                       .group(1))
        self.assertLess(view_s, 0.3, timing)
        ends = [m for m in r.logs if re.match(_END_OF_BATCH_RE, m)]
        self.assertRegex(ends[-1], r" — the converter waited \d+\.\d s for "
                                   r"them$")
        self.assertEqual(self.outputs(), set(self.n.values()))

    def test_list_mode_the_list_is_written_at_once_or_when_its_tiles_are_in(
            self):
        names_a, names_d = self.names("A"), self.names("D")
        fetch = _FakeFetch(hold=names_d)
        logs, renames = [], []
        real_replace = ta.replace_with_retry

        def replace(src, dst, *a, **kw):
            # Atomic: a complete temp file next to the list, renamed onto it.
            with open(src, encoding="utf-8") as fh:
                renames.append((os.path.dirname(src) == os.path.dirname(dst),
                                os.path.lexists(dst), dst, fh.read(),
                                threading.current_thread().name))
            return real_replace(src, dst, *a, **kw)

        links = self.record_links()
        with mock.patch.object(ta, "get_cache_dir", return_value=self.cache), \
             mock.patch.object(ta, "replace_with_retry",
                               side_effect=replace), \
             mock.patch.object(ofm, "resolve_tile_template_strict",
                               return_value="http://x/{z}/{x}/{y}.pbf"), \
             mock.patch.object(ofm, "fetch_building_tiles",
                               side_effect=fetch):
            worker = _MapConverterWorker([], self.out, [30], [])
            osm = mct._OsmBuildings([_OSM_A, _OSM_D])
            worker._buildings = mct._RunBuildings(osm=osm)
            tiles = {k: [rt for rt in self.expected
                         if rt[1]["filename"] == self.n[k]] for k in "AD"}
            worker._bld_branch = mct._BuildingsBranch(
                worker, osm, [osm.needed(tiles["A"]), osm.needed(tiles["D"])])
            worker._pbf_marker = worker._pbf_list = True
            worker.log_line.connect(logs.append)
            branch = worker._bld_branch
            store = ta.osm_buildings_store_dir()
            list_a = ta.osm_buildings_list_path(names_a)
            list_d = ta.osm_buildings_list_path(names_d)
            branch.start()
            self.assertTrue(fetch.holding.wait(10))
            # A's tiles are in: its list written at once, in this thread.
            self.assertEqual(worker._pbf_batch(tiles["A"]), (store, {
                "buildings_pbf_list": list_a, "buildings_pbf_wait_s": 14400}))
            self.assertEqual(read_building_list(list_a), names_a)
            # D's are not: its fields come back at once, its list later.
            t0 = time.monotonic()
            self.assertEqual(worker._pbf_batch(tiles["D"]), (store, {
                "buildings_pbf_list": list_d, "buildings_pbf_wait_s": 14400}))
            self.assertLess(time.monotonic() - t0, 0.5)
            self.assertFalse(os.path.lexists(list_d))
            self.assertTrue(branch.alive())
            fetch.release.set()
            branch.join(10)
        self.assertIsNone(branch.error)
        self.assertEqual(read_building_list(list_d), names_d)
        me = threading.current_thread().name
        self.assertEqual(renames, [
            (True, False, list_a, "".join(f"{n}\n" for n in names_a), me),
            (True, False, list_d, "".join(f"{n}\n" for n in names_d),
             "waveshed-buildings")])
        self.assertEqual(links, [])
        self.assertEqual(self.views(),
                         [os.path.basename(os.path.dirname(list_a))])
        self.assertEqual([m for m in logs if " listed for the converter in "
                          in m],
                         [f"  buildings: {len(names_a)} OpenFreeMap tile(s) "
                          f"listed for the converter in {list_a} — the "
                          f"converter burns them now",
                          f"  buildings: {len(names_d)} OpenFreeMap tile(s) "
                          f"listed for the converter in {list_d} — the "
                          f"converter burns them now"])
        self.assertFalse([m for m in logs if "linked into" in m], logs)

    def test_list_mode_a_stale_list_is_removed_before_the_launch(self):
        names_a = self.names("A")
        list_a = self.list_path(names_a)
        os.makedirs(os.path.dirname(list_a))
        with open(list_a, "w", encoding="utf-8") as fh:
            fh.write("14_1_1.pbf\n")    # an earlier run's: not in this store
        fetch = _FakeFetch(hold=names_a)
        self.release_later(fetch)
        conv = _ListConverter(self,
                              on_wait=lambda _j, _p: fetch.release.set())
        r = self.run_("0.2.14", conv, fetch)
        self.assertEqual(r.errs, [])
        self.assertEqual(conv.errors, [])
        self.assertEqual(conv.list_at_launch[0], False)
        self.assertEqual(conv.read[0], names_a)
        self.assertEqual(read_building_list(list_a), names_a)

    def test_list_mode_a_list_that_cannot_be_removed_takes_the_view(self):
        # Something at A's list path os.remove refuses: Phase 2 takes the
        # 0.2.12 view and its marker; band 1 (D) still its list.
        names_a, names_d = self.names("A"), self.names("D")
        list_a = self.list_path(names_a)
        os.makedirs(list_a)
        conv = _ListConverter(self)
        r = self.run_("0.2.14", conv, _FakeFetch())
        self.assertEqual(r.errs, [])
        (job_a,) = self.jobs_of(conv, "A")
        self.assertEqual(job_a["buildings_pbf_dir"],
                         os.path.abspath(self.view(names_a)))
        self.assertEqual(job_a["buildings_pbf_ready"], "_ready")
        self.assertNotIn("buildings_pbf_list", job_a)
        (job_d,) = self.jobs_of(conv, "D")
        self.assertEqual(job_d["buildings_pbf_list"],
                         self.list_path(names_d))
        self.assertNotIn("buildings_pbf_ready", job_d)
        self.assertEqual(conv.read, [names_a, names_d])
        self.assertIn(f"  WARNING: buildings: {list_a} (left by an earlier "
                      f"run) could not be removed — this batch's building "
                      f"tiles are linked into a view instead", r.logs)
        self.assertEqual(self.outputs(), set(self.n.values()))

    def test_list_mode_a_fetch_failure_kills_the_waiting_batch(self):
        names_d = self.names("D")
        waiting = threading.Event()
        procs = []

        def on_wait(jobs, proc):
            if any(os.path.basename(j["output_path"]) == self.n["D"]
                   for j in jobs):
                procs.append(proc)
                waiting.set()           # D's converter waits: now it fails

        conv = _ListConverter(self, on_wait=on_wait)
        t0 = time.monotonic()
        r = self.run_("0.2.14", conv, _FailingFetch(names_d, waiting))
        self.assertLess(time.monotonic() - t0, 20)
        self.assertEqual(r.oks, [])
        self.assertEqual(len(r.errs), 1, r.errs)
        self.assertTrue(r.errs[0].startswith(
            "Buildings download failed (OpenFreeMap z14): 1 of "), r.errs[0])
        self.assertEqual(len([m for m in r.logs if m.startswith(
            "ERROR: Buildings download failed")]), 1, r.logs)
        # The waiting batch (it carried D's list) was killed; D neither
        # written nor recorded; its list never written (Phase 2's was).
        self.assertEqual([j["buildings_pbf_list"] for b in conv.batches
                          for j in b if os.path.basename(j["output_path"])
                          == self.n["D"]], [self.list_path(names_d)])
        self.assertEqual(len(procs), 1)
        self.assertIsNotNone(procs[0].poll())
        self.assertEqual(len(conv.killed), 1)
        self.assertNotIn(self.n["D"], self.outputs())
        self.assertNotIn(self.n["D"], mct._read_output_manifest(self.out))
        self.assertFalse(os.path.lexists(self.list_path(names_d)))
        self.assertTrue(os.path.isfile(self.list_path(self.names("A"))))
        self.assertEqual(self.leftovers(), [])

    def test_list_mode_stop_while_a_batch_waits_writes_no_list(self):
        names_d = self.names("D")
        fetch = _FakeFetch(hold=names_d)
        self.release_later(fetch)
        procs = []

        def on_wait(jobs, proc):
            if any(os.path.basename(j["output_path"]) == self.n["D"]
                   for j in jobs):
                procs.append(proc)
                self.r.worker.cancel()  # ONE Stop, while D's converter waits

        conv = _ListConverter(self, on_wait=on_wait)
        r = self.run_("0.2.14", conv, fetch)
        self.assertEqual((r.errs, r.oks), ([], []))
        self.assertEqual(r.status[-1], "Cancelled.")
        # The batch Stop killed carried D's list, which was never written.
        self.assertEqual([j["buildings_pbf_list"] for b in conv.batches
                          for j in b if os.path.basename(j["output_path"])
                          == self.n["D"]], [self.list_path(names_d)])
        self.assertTrue(fetch.calls and names_d in fetch.calls)
        self.assertEqual(len(procs), 1)
        self.assertIsNotNone(procs[0].poll())
        self.assertEqual(len(conv.killed), 1)
        self.assertNotIn(self.n["D"], self.outputs())
        self.assertFalse(os.path.lexists(self.list_path(names_d)))
        self.assertEqual(self.leftovers(), [])

    def test_0213_still_takes_the_view_and_its_marker(self):
        # The gate: buildings_pbf_list from 0.2.14 only.
        names_a = self.names("A")
        conv = _ListConverter(self)
        r = self.run_("0.2.13", conv, _FakeFetch())
        self.assertEqual(r.errs, [])
        for call in conv.calls:
            for job in call["jobs"]:
                self.assertNotIn("buildings_pbf_list", job)
        (job_a,) = self.jobs_of(conv, "A")
        self.assertEqual(job_a["buildings_pbf_ready"], "_ready")
        self.assertEqual(job_a["buildings_pbf_dir"],
                         os.path.abspath(self.view(names_a)))
        self.assertEqual(conv.list_at_launch, [])
        self.assertNotIn(os.path.basename(os.path.dirname(
            self.list_path(names_a))), self.views())


# ---------------------------------------------------------------------------
# Stop and failure
# ---------------------------------------------------------------------------

class StopAndFailure(_Case):

    def _hold_band2(self, r, procs):
        """Band 2's download is a real process, registered like the
        converter's — it runs until something kills it."""
        def pool(call_no, specs, kw):
            if call_no == 2:
                proc = procs["band2"] = _sleeper(self)
                kw["on_start"](proc)
                proc.wait(30)
                if kw["should_cancel"]():
                    raise ta.ConverterCancelled()
                raise RuntimeError("Terrain download failed: killed")
        r.on_pool = pool

    def _stop_when(self, r, ready):
        runner = threading.Thread(target=lambda: self._run_bg(r))
        runner.start()
        self.assertTrue(_wait_for(ready, timeout=20), "never got there")
        r.worker.cancel()                  # ONE Stop
        runner.join(30)
        self.assertFalse(runner.is_alive(), "the worker did not end")

    def _run_bg(self, r):
        with mock.patch.object(tempfile, "tempdir", self.temp):
            r.run()

    def test_stop_during_band2_while_phase2_converts(self):
        # Phase 2 (A) is a live ingest, band 1 (C D) has landed, band 2 (B)
        # is a live download: one Stop kills both, nothing is left.
        procs = {}

        def phase2(jobs, on_line):
            proc = procs["phase2"] = _sleeper(self)
            conv.on_start(proc)
            return proc.wait(30)

        conv = _Converter(on_phase2=phase2)
        r = _BandRun(self, self.out, self.cache, converter=conv,
                     entries=[{"kind": "files", "infos": _files_a()},
                              {"kind": "xyz", "uri": _XYZ,
                               "extent": dict(_EXT)}])
        self._hold_band2(r, procs)
        scratch = os.path.join(self.out,
                               f"{mct._PHASE2_SCRATCH_PREFIX}{os.getpid()}")
        self._stop_when(r, lambda: len(procs) == 2
                        and os.path.isdir(scratch)
                        and r.worker.progress_model.work(("convert", 1)) > 0)
        for name, proc in procs.items():
            self.assertIsNotNone(proc.poll(), f"{name} outlived Stop")
        self.assertEqual(r.errs, [])
        self.assertEqual(r.oks, [])
        self.assertEqual(r.status[-1], "Cancelled.")
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.outputs(), set())
        self.assertEqual(mct._read_output_manifest(self.out), {})

    def test_stop_during_band2_while_band1_converts(self):
        # XYZ only: band 1's batch is a live ingest, band 2 a live download.
        procs = {}

        def on_batch(job_file):
            proc = procs["band1"] = _sleeper(self)
            conv.on_start(proc)
            return proc.wait(30)

        conv = _Recorder(on_batch=on_batch)
        r = _BandRun(self, self.out, self.cache, converter=conv,
                     entries=_xyz_only())
        self._hold_band2(r, procs)
        self._stop_when(r, lambda: len(procs) == 2)
        for name, proc in procs.items():
            self.assertIsNotNone(proc.poll(), f"{name} outlived Stop")
        self.assertEqual(r.errs, [])
        self.assertEqual(r.status[-1], "Cancelled.")
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(mct._read_output_manifest(self.out), {})
        self.assertEqual(r.worker.progress_model.activities(), [])

    def test_a_failed_band2_keeps_band1_and_is_the_error(self):
        n = self.n
        r = _BandRun(self, self.out, self.cache, entries=_xyz_only())

        def pool(call_no, specs, kw):
            if call_no == 2:
                # Band 1's batch converted and recorded first.
                self.assertTrue(_wait_for(lambda: n["C"] in
                                          mct._read_output_manifest(self.out)))
                raise RuntimeError("Terrain download failed: HTTP 403")

        r.on_pool = pool
        with mock.patch.object(tempfile, "tempdir", self.temp):
            r.run()
        self.assertEqual(r.errs, ["Terrain download failed: HTTP 403"])
        self.assertNotIn("Cancelled.", r.status)
        self.assertEqual(self.outputs(), {n["C"], n["D"]})
        manifest = mct._read_output_manifest(self.out)
        full = mct._stack_tile_fingerprints(self.expected, _xyz_only(), None)
        self.assertEqual(manifest, {k: full[k] for k in (n["C"], n["D"])})
        self.assertEqual(self.leftovers(), [])

    def test_a_failed_batch_stops_the_downloads_and_is_the_error(self):
        procs = {}

        def on_batch(job_file):
            _wait_for(lambda: "band2" in procs)

        class Failing(_Recorder):
            def __call__(self, exe, args, on_line, on_start=None, env=None):
                super().__call__(exe, args, on_line, on_start, env)
                return 1

        r = _BandRun(self, self.out, self.cache, entries=_xyz_only(),
                     converter=Failing(on_batch=on_batch))
        self._hold_band2(r, procs)
        t0 = time.monotonic()
        with mock.patch.object(tempfile, "tempdir", self.temp):
            r.run()
        self.assertLess(time.monotonic() - t0, 20, "band 2 was not stopped")
        self.assertEqual(len(r.errs), 1)
        self.assertIn("exit code 1", r.errs[0])
        self.assertIsNotNone(procs["band2"].poll(), "band 2 lives on")
        self.assertEqual(self.leftovers(), [])


# ---------------------------------------------------------------------------
# The real converter: two bands, two downloads, two batches = the one shot
# ---------------------------------------------------------------------------

class RealConverterBands(unittest.TestCase):
    """The shipped worker with the real aether_converter (AETHER_BIN_DIR)
    and a local terrarium server over an XYZ-only stack: two 0.5 deg bands
    are two ``download`` jobs and two ingest batches, and every output tile
    is byte for byte the one-shot build (all four pool tiles in ONE job,
    one ingest)."""

    def test_banded_run_is_byte_identical_to_the_one_shot_build(self):
        from test_two_phase_convert import (_find_real_converter,
                                            _terrarium_png)
        exe = _find_real_converter()
        if not exe:
            self.skipTest("aether_converter not provided via "
                          "AETHER_BIN_DIR or PATH")
        import functools
        import http.server
        import json as _json
        from waveshed.core import binary_manager

        expected, n = _tiles()
        with tempfile.TemporaryDirectory() as root:
            tiles_dir = os.path.join(root, "tiles")
            for x in range(2137, 2153):             # z12 over 8..9 E
                os.makedirs(os.path.join(tiles_dir, "12", str(x)))
                for y in range(1423, 1449):          # z12 over 47..48 N
                    with open(os.path.join(tiles_dir, "12", str(x),
                                           f"{y}.png"), "wb") as fh:
                        fh.write(_terrarium_png(12, x, y, 700.0))

            class Quiet(http.server.SimpleHTTPRequestHandler):
                def log_message(self, *_a):
                    pass

            class Server(http.server.ThreadingHTTPServer):
                request_queue_size = 512

            server = Server(("127.0.0.1", 0),
                            functools.partial(Quiet, directory=tiles_dir))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            uri = ("type=xyz&url=http%3A//127.0.0.1%3A"
                   f"{server.server_address[1]}/%7Bz%7D/%7Bx%7D/%7By%7D.png"
                   "&zmax=12&interpretation=terrarium")
            out = os.path.join(root, "out")
            cache = os.path.join(root, "cache")
            layers = [_LayerEntry(layer_type="raster", source_path=uri,
                                  extent=dict(_EXT))]
            worker = _MapConverterWorker(layers, out, [30], [uri])
            errs, oks, launches, pools = [], [], [], []
            worker.finished_err.connect(errs.append)
            worker.finished_ok.connect(oks.append)
            real_run = ta.run_converter_streaming
            real_pool = ta.ensure_pool_tiles

            def launch(exe_, args, on_line, **kw):
                launches.append(args[0])
                return real_run(exe_, args, on_line, **kw)

            def pool(u, specs, res, **kw):
                pools.append(sorted(t["filename"] for t in specs))
                return real_pool(u, specs, res, **kw)

            with mock.patch.object(
                    _MapConverterWorker, "_resolve_raster",
                    return_value={"kind": "xyz", "uri": uri,
                                  "extent": dict(_EXT)}), \
                 mock.patch.object(_MapConverterWorker, "_resolve_buildings",
                                   return_value=None), \
                 mock.patch.object(mct, "find_binary", return_value=exe), \
                 mock.patch.object(binary_manager, "find_binary",
                                   return_value=exe), \
                 mock.patch.object(ta, "get_cache_dir", return_value=cache), \
                 mock.patch.object(ta, "ensure_pool_tiles",
                                   side_effect=pool), \
                 mock.patch.object(ta, "run_converter_streaming",
                                   side_effect=launch):
                worker.run()
            self.assertEqual(errs, [])
            self.assertEqual(oks, [out])
            self.assertEqual(pools, [sorted([n["C"], n["D"]]),
                                     sorted([n["A"], n["B"]])])
            self.assertEqual(sorted(launches), ["download", "download",
                                                "ingest", "ingest"])
            self.assertEqual(launches[0], "download")
            built = {name: open(os.path.join(out, name), "rb").read()
                     for name in n.values()}

            # The one-shot build: one download job, one ingest.
            ref = os.path.join(root, "ref")
            os.makedirs(ref)
            with mock.patch.object(ta, "get_cache_dir",
                                   return_value=os.path.join(root, "rc")), \
                 mock.patch.object(binary_manager, "find_binary",
                                   return_value=exe):
                pool_paths = real_pool(uri, [t for _r, t in expected], 30)
                jobs, _temps = mct._build_tile_jobs(
                    expected, ref, True,
                    [{"kind": "xyz", "pool": {30: pool_paths}}], None, root)
                job_file = os.path.join(root, "one_shot.json")
                with open(job_file, "w") as fh:
                    _json.dump(jobs, fh)
                rc = real_run(exe, ["ingest", "--job-file", job_file],
                              lambda _line: None)
            self.assertEqual(rc, 0)
            for name in n.values():
                one_shot = open(os.path.join(ref, name), "rb").read()
                self.assertGreater(len(built[name]), 44, name)
                self.assertEqual(built[name], one_shot,
                                 f"{name} differs from the one-shot build")


# ---------------------------------------------------------------------------
# The epsilon snap (converter >= 0.2.9), version-gated
# ---------------------------------------------------------------------------

class EpsilonSnap(unittest.TestCase):

    #: S 47.3 is ON the 0.1 deg grid, but 47.3 / 0.1 = 472.99999999999994:
    #: the bare floor adds a row of 2 m tiles (N47.30) wholly south of it.
    ON_GRID = {"south": 47.3, "north": 47.6, "west": 8.3, "east": 8.6}
    #: The owner's run (2026-10-23).
    OWNER = {"south": 45.7161, "north": 48.1913, "west": 4.9482,
             "east": 11.6466}
    OWNER_RES = [2, 5, 10, 30, 90]

    @staticmethod
    def _tiles(bbox, res, epsilon):
        return mct._enumerate_tiles(mct._snap_bbox(bbox, res, epsilon=epsilon),
                                    res, epsilon=epsilon)

    def _rows(self, bbox, res, epsilon):
        return sorted({t["ul_lat"] for _r, t in self._tiles(bbox, res, epsilon)})

    def test_the_epsilon_keeps_an_on_grid_edge_where_it_is(self):
        self.assertEqual(self._rows(self.ON_GRID, [2], False),
                         [47.3, 47.4, 47.5, 47.6])
        self.assertEqual(self._rows(self.ON_GRID, [2], True),
                         [47.4, 47.5, 47.6])
        # Off the grid both snap to the same lines (the epsilon snap's edge
        # is the line itself: 47.3, not the product 47.300000000000004) and
        # give the same tiles.
        off = {"south": 47.31, "north": 47.59, "west": 8.31, "east": 8.59}
        bare, eps = mct._snap_bbox(off, [2]), mct._snap_bbox(off, [2], epsilon=True)
        self.assertEqual(eps, {"south": 47.3, "north": 47.6, "west": 8.3, "east": 8.6})
        self.assertEqual({k: round(v, 9) for k, v in bare.items()}, eps)
        self.assertEqual([t["filename"] for _r, t in self._tiles(off, [2], False)],
                         [t["filename"] for _r, t in self._tiles(off, [2], True)])

    def test_the_owner_bbox_loses_its_69th_column_from_0_2_9(self):
        """E 11.6466 snaps to ceil(116.466) = 117 grid steps. The bare
        product 117 * 0.1 = 11.700000000000001 lies past the 11.7 the
        enumeration's 6-decimal steps arrive at, so ``lon < east`` held for
        a 69th 2 m column (E11.70, wholly east of the area): 2,463 tiles —
        still so for converters <= 0.2.8, whose ``plan`` does the same. From
        0.2.9 the edge is the grid line itself, round(117 * 0.1, 9) = 11.7:
        68 columns, 2,438 tiles (1,700 + 308 + 308 + 90 + 32)."""
        old = self._tiles(self.OWNER, self.OWNER_RES, False)
        self.assertEqual(len(old), 2463)
        self.assertEqual(len({t["ul_lon"] for r, t in old if r == 2}), 69)
        self.assertEqual(mct._snap_bbox(self.OWNER, self.OWNER_RES)["east"],
                         11.700000000000001)
        new = self._tiles(self.OWNER, self.OWNER_RES, True)
        self.assertEqual(len(new), 2438)
        self.assertEqual({r: sum(1 for rr, _t in new if rr == r)
                          for r in self.OWNER_RES},
                         {2: 1700, 5: 308, 10: 308, 30: 90, 90: 32})
        cols = sorted({t["ul_lon"] for r, t in new if r == 2})
        self.assertEqual((len(cols), cols[0], cols[-1]), (68, 4.9, 11.6))
        self.assertEqual(mct._snap_bbox(self.OWNER, self.OWNER_RES, epsilon=True),
                         {"south": 45.7, "north": 48.2, "west": 4.9, "east": 11.7})
        # E given as 11.7 itself (11.7 / 0.1 = 116.99999999999999): no extra
        # column either — the bare snap gives the 69th here too.
        on_line = dict(self.OWNER, east=11.7)
        self.assertEqual(len(self._tiles(on_line, self.OWNER_RES, True)), 2438)
        self.assertEqual(len(self._tiles(on_line, self.OWNER_RES, False)), 2463)
        # Every tile of the new grid is one of the old grid's (an upper
        # bound for the GUI estimate, which keeps the bare snap).
        self.assertLessEqual({t["filename"] for _r, t in new},
                             {t["filename"] for _r, t in old})

    def test_the_epsilon_start_keeps_the_rounded_edge_on_its_line(self):
        """The rounded edge IS the grid line (47.3), and the bare start
        floor(47.3 / 0.1) * 0.1 = 47.2 would put the stray row back; the
        epsilon enumeration starts at the line, as plan.rs does."""
        edge = mct._snap_bbox(self.ON_GRID, [2], epsilon=True)
        self.assertEqual(edge, self.ON_GRID)
        self.assertEqual(sorted({t["ul_lat"] for _r, t in mct._enumerate_tiles(
            edge, [2])}), [47.3, 47.4, 47.5, 47.6])
        self.assertEqual(self._rows(self.ON_GRID, [2], True), [47.4, 47.5, 47.6])

    def _worker_expectation(self, version, bbox, res):
        seen = {}

        def spy_check(exe, raw, resolutions, expected):
            seen["raw"], seen["expected"] = dict(raw), expected
            raise RuntimeError("stop before any job is built")

        entry = _LayerEntry(layer_type="raster", source_path="/a.tif",
                            extent=dict(bbox))
        from waveshed.core import binary_manager
        with tempfile.TemporaryDirectory() as out:
            worker = _MapConverterWorker([entry], out, res, ["/a.tif"])
            with mock.patch.object(
                    _MapConverterWorker, "_resolve_raster",
                    return_value={"kind": "files", "infos": []}), \
                 mock.patch.object(_MapConverterWorker, "_resolve_buildings",
                                   return_value=None), \
                 mock.patch.object(mct, "find_binary",
                                   return_value="/bin/conv"), \
                 mock.patch.object(binary_manager, "probe_converter_version",
                                   return_value=version), \
                 mock.patch.object(mct, "_plan_cross_check",
                                   side_effect=spy_check):
                worker.run()
        return seen

    def test_the_worker_snaps_with_the_epsilon_from_0_2_9(self):
        new = self._worker_expectation("0.2.9", self.ON_GRID, [2])
        self.assertEqual(new["raw"], self.ON_GRID)      # always RAW
        self.assertEqual(
            [t["filename"] for _r, t in new["expected"]],
            [t["filename"] for _r, t in self._tiles(self.ON_GRID, [2], True)])
        self.assertEqual(len(new["expected"]), 9)
        for version in ("0.2.8", "0.2.7", None):
            old = self._worker_expectation(version, self.ON_GRID, [2])
            self.assertEqual(len(old["expected"]), 12, version)
            owner = self._worker_expectation(version, self.OWNER, self.OWNER_RES)
            self.assertEqual(len(owner["expected"]), 2463, version)
        owner = self._worker_expectation("0.2.9", self.OWNER, self.OWNER_RES)
        self.assertEqual(len(owner["expected"]), 2438)
        self.assertEqual(owner["raw"], self.OWNER)

    def test_the_gate_reads_the_converter_version(self):
        from waveshed.core import binary_manager
        for version, want in (("0.2.9", True), ("0.3.0", True),
                              ("0.2.8", False), (None, False)):
            with mock.patch.object(binary_manager, "probe_converter_version",
                                   return_value=version):
                self.assertEqual(mct._converter_snaps_with_epsilon("/c"),
                                 want, version)
        with mock.patch.object(binary_manager, "probe_converter_version",
                               side_effect=OSError("boom")):
            self.assertFalse(mct._converter_snaps_with_epsilon("/c"))


# ---------------------------------------------------------------------------
# Tiles outside every source (owner, 2026-11-07)
# ---------------------------------------------------------------------------

def _epsilon_grid(bbox, res):
    """The worker's grid on a converter >= 0.2.9 (the cross-checked one)."""
    return mct._enumerate_tiles(mct._snap_bbox(bbox, list(res), epsilon=True),
                                list(res), epsilon=True)


class TilesWithinSources(unittest.TestCase):
    """``_tiles_within_sources``: a tile whose bbox intersects no TERRAIN
    row's extent is dropped; a row without an extent is the whole service;
    building rows never count."""

    #: The owner's run of 2026-11-05: an XYZ (Mapzen) row and a swisstopo
    #: GeoTIFF folder row, both over this extent.
    OWNER = {"south": 45.7648, "north": 48.1226, "west": 4.8094,
             "east": 11.5693}
    OWNER_RES = [2, 5, 10, 30, 90]

    def _owner_rows(self):
        return [_LayerEntry(layer_type="raster", source_path=_XYZ,
                            extent=dict(self.OWNER)),
                _LayerEntry(layer_type="raster", source_path="/swisstopo",
                            extent=dict(self.OWNER))]

    def test_the_owner_run_skips_the_56_tiles_south_of_its_extent(self):
        """The 0.1 deg grid snaps S to 45.7; the 0.25 deg grids (5 m,
        10 m) start a row lower, 45.5-45.75 N — wholly south of
        S 45.7648. The 30 m (45.5-46) and 90 m (45-46) rows reach into
        the extent and stay."""
        tiles = _epsilon_grid(self.OWNER, self.OWNER_RES)
        self.assertEqual(len(tiles), 2438)
        kept, dropped = mct._tiles_within_sources(tiles, self._owner_rows())
        self.assertEqual(len(dropped), 56)
        self.assertEqual(Counter(r for r, _t in dropped), {5: 28, 10: 28})
        self.assertEqual({t["ul_lat"] for _r, t in dropped}, {45.75})
        # The 2,382 kept are everything else — in order, the very tuples.
        out = {t["filename"] for _r, t in dropped}
        rest = [rt for rt in tiles if rt[1]["filename"] not in out]
        self.assertEqual(len(kept), 2382)
        self.assertEqual(len(rest), 2382)
        self.assertTrue(all(k is rt for k, rt in zip(kept, rest)))
        names = {t["filename"] for _r, t in kept}
        self.assertIn("tile_N46.00E4.50_30m.abt", names)    # partial: stays
        self.assertIn("tile_N46.00E4.00_90m.abt", names)
        # The worker's row dicts say the same.
        dicts = [{"layer_type": e.layer_type, "extent": dict(e.extent)}
                 for e in self._owner_rows()]
        self.assertEqual(mct._tiles_within_sources(tiles, dicts),
                         (kept, dropped))
        self.assertEqual(
            mct._skipped_tiles_line(dropped),
            "  56 tile(s) outside every source skipped (the 5 m, 10 m grids "
            "reach past the layers' extents): 28 × 5 m, 28 × 10 m")

    def test_nothing_is_dropped_where_every_tile_overlaps(self):
        # 47-48 N, 8-9 E lies on every grid line: each tile reaches in.
        tiles = _epsilon_grid(_EXT, self.OWNER_RES)
        rows = [_LayerEntry(layer_type="raster", source_path=_XYZ,
                            extent=dict(_EXT))]
        kept, dropped = mct._tiles_within_sources(tiles, rows)
        self.assertEqual(dropped, [])
        self.assertEqual(kept, tiles)

    def test_a_terrain_row_without_an_extent_keeps_every_tile(self):
        tiles = _epsilon_grid(self.OWNER, self.OWNER_RES)
        for whole in (_LayerEntry(layer_type="raster", source_path=_XYZ),
                      {"layer_type": "raster", "extent": None}):
            for rows in (self._owner_rows() + [whole],
                         [whole] + self._owner_rows()):
                self.assertEqual(mct._tiles_within_sources(tiles, rows),
                                 (tiles, []))

    def test_a_buildings_extent_keeps_no_tile(self):
        tiles = _epsilon_grid(self.OWNER, self.OWNER_RES)
        everywhere = {"south": 45.0, "north": 49.0, "west": 4.0, "east": 12.0}
        buildings = [
            _LayerEntry(layer_type="buildings",
                        source_path=mct._OSM_PLACEHOLDER,
                        extent=dict(everywhere)),
            _LayerEntry(layer_type="buildings", source_path="/b.fgb")]
        kept, dropped = mct._tiles_within_sources(
            tiles, self._owner_rows() + buildings)
        self.assertEqual(len(dropped), 56)
        self.assertEqual(len(kept), 2382)
        self.assertEqual(mct._tiles_within_sources(tiles, buildings),
                         ([], tiles))

    def test_each_row_keeps_the_tiles_of_its_own_extent(self):
        # Two rows apart: the 2 m tiles between them are in the grid of
        # their union (47-48 N, 8-9 E) and in neither extent.
        a = {"south": 47.0, "north": 47.4, "west": 8.0, "east": 8.4}
        b = {"south": 47.6, "north": 48.0, "west": 8.6, "east": 9.0}
        rows = [_LayerEntry(layer_type="raster", source_path="/a",
                            extent=dict(a)),
                _LayerEntry(layer_type="raster", source_path="/b",
                            extent=dict(b))]
        self.assertEqual(mct._union_extent(rows), _EXT)
        tiles = _epsilon_grid(_EXT, [2])
        kept, dropped = mct._tiles_within_sources(tiles, rows)
        self.assertEqual((len(tiles), len(kept), len(dropped)), (100, 32, 68))
        for res, tile in kept:
            box = mct._tile_bbox_of(res, tile)
            self.assertTrue(ta.bboxes_intersect(box, a)
                            or ta.bboxes_intersect(box, b), tile["filename"])
        self.assertEqual(
            mct._skipped_tiles_line(dropped),
            "  68 tile(s) outside every source skipped (the 2 m grid reaches "
            "past the layers' extents): 68 × 2 m")


class TilesOutsideEverySource(_Case):
    """The shipped worker over an XYZ extent whose 0.25 deg grids reach
    past it: those tiles are skipped with one log line — never downloaded,
    written or recorded, not in ``Conversion complete`` — and the pre-run
    estimate counts what the run builds."""

    #: 2 / 5 / 10 m over 47.27-47.5 N, 8.0-8.3 E: the 0.1 deg grid snaps
    #: S to 47.2, the 0.25 deg grids (5 m, 10 m) start at 47.0 — their row
    #: 47.0-47.25 N lies wholly south of the extent. 17 tiles, 13 built.
    EXT = {"south": 47.27, "north": 47.5, "west": 8.0, "east": 8.3}
    RES = (2, 5, 10)
    OUTSIDE = ["tile_N47.25E8.00_5m.abt", "tile_N47.25E8.25_5m.abt",
               "tile_N47.25E8.00_10m.abt", "tile_N47.25E8.25_10m.abt"]
    LINE = ("  4 tile(s) outside every source skipped (the 5 m, 10 m grids "
            "reach past the layers' extents): 2 × 5 m, 2 × 10 m")

    def setUp(self):
        super().setUp()
        self.grid = _epsilon_grid(self.EXT, self.RES)
        self.kept = [(r, t) for r, t in self.grid
                     if t["filename"] not in self.OUTSIDE]

    def _run(self, **kw):
        return self.run_(entries=[{"kind": "xyz", "uri": _XYZ,
                                   "extent": dict(self.EXT)}],
                         resolutions=self.RES, extent=self.EXT, **kw)

    def _layer(self):
        """The tab's row for that XYZ service."""
        return _LayerEntry(layer_type="raster", source_path=_XYZ,
                           extent=dict(self.EXT))

    def test_the_outside_tiles_are_never_downloaded_written_or_recorded(self):
        self.assertEqual(len(self.grid), 17)
        self.assertEqual(sorted(self.OUTSIDE), sorted(
            t["filename"] for res, t in self.grid
            if not mct._jobs_overlapping_extent([(res, t)], self.EXT)))
        kept = {t["filename"] for _r, t in self.kept}
        r = self._run()
        self.assertEqual(r.errs, [])
        self.assertEqual(r.oks, [self.out])
        self.assertEqual([m for m in r.logs if "outside every source" in m],
                         [self.LINE])
        self.assertEqual(set(r.asked), kept)
        jobs = [j for c in r.converter.calls for j in c["jobs"]]
        self.assertEqual(sorted(os.path.basename(j["output_path"])
                                for j in jobs), sorted(kept))
        self.assertTrue(all(j["sources"] for j in jobs), "an empty tile")
        self.assertEqual(self.outputs(), kept)
        self.assertEqual(set(mct._read_output_manifest(self.out)), kept)
        self.assertIn("Conversion complete: 13 tiles.", r.logs)
        # The progress was sized for the tiles built, no more.
        model = r.worker.progress_model
        self.assertAlmostEqual(
            sum(model.work(k) for k in model.keys()
                if isinstance(k, tuple) and k[:1] == ("convert",)),
            mct._convert_work([t for _r, t in self.kept]))
        self.assertEqual(model.percent(), 100)

    def test_the_estimate_counts_what_the_run_builds(self):
        layer = self._layer()
        fresh = os.path.join(self.root, "fresh")
        res = list(self.RES)
        with mock.patch.object(ta, "get_cache_dir", return_value=self.cache):
            count, mb = mct._estimate_tile_count_and_mb(self.EXT, res,
                                                        entries=[layer])
            n_disk, needs = mct._estimate_disk_need(
                [layer], res, fresh, False, tmp_dir=self.temp, cache={})
            with mock.patch.object(mct, "_tiles_within_sources",
                                   side_effect=lambda tiles, _rows: (
                                       list(tiles), [])):
                n_all, needs_all = mct._estimate_disk_need(
                    [layer], res, fresh, False, tmp_dir=self.temp, cache={})
            _combined, render_jobs = mct._pending_render_jobs(
                [layer], res, fresh, False)
            model = rp.RunProgress()
            mct._estimate_run_work(model, [layer], render_jobs)
            tab = _tab(fresh, [layer])
            tab._get_output_resolutions = lambda: res
            with mock.patch.object(oe, "free_bytes", return_value=_HUGE):
                tab._update_estimate()
                tab.flush_estimate()     # the estimate thread's work, inline
        r = self._run()
        self.assertEqual(r.errs, [])
        built = sorted(self.outputs())
        self.assertEqual(len(built), 13)
        self.assertEqual((count, n_disk, len(render_jobs)), (13, 13, 13))
        self.assertEqual(sorted(t["filename"] for _r, t in render_jobs), built)
        self.assertEqual(mb, ta._estimate_abt_disk_mb(
            [t for _r, t in self.kept]))
        self.assertIn(".abt tiles: 13 |", tab._lbl_estimate.text)
        # The disk need is the one of the tiles built: without the rule it
        # was 17 tiles, the 4 outside ones' bytes more (no pool or raw
        # tile of theirs — no download feeds them).
        self.assertEqual(n_all, 17)
        self.assertEqual(
            sum(n.need for n in needs_all) - sum(n.need for n in needs),
            sum(ta._abt_whole_bytes(t["size_px"]) for _r, t in self.grid
                if t["filename"] in self.OUTSIDE))
        self.assertAlmostEqual(model.work(mct._CONVERT_PENDING),
                               mct._convert_work([t for _r, t in self.kept]))

    def test_tiles_an_earlier_run_wrote_there_are_left_alone(self):
        # An earlier run converted the outside tiles empty: even "Rebuild
        # existing tiles" neither rebuilds nor deletes them, and their
        # records stay as they were.
        os.makedirs(self.out)
        size = {t["filename"]: t["size_px"] for _r, t in self.grid}
        for name in self.OUTSIDE:
            with open(os.path.join(self.out, name), "wb") as fh:
                fh.truncate(ta._abt_whole_bytes(size[name]))
        mct._write_output_manifest(self.out, {n: "earlier"
                                              for n in self.OUTSIDE})
        stamps = {n: os.stat(os.path.join(self.out, n)).st_mtime_ns
                  for n in self.OUTSIDE}
        r = self._run(overwrite=True)
        self.assertEqual(r.errs, [])
        self.assertEqual([m for m in r.logs if "outside every source" in m],
                         [self.LINE])
        written = {os.path.basename(j["output_path"])
                   for c in r.converter.calls for j in c["jobs"]}
        self.assertEqual(written, {t["filename"] for _r, t in self.kept})
        self.assertEqual({n: os.stat(os.path.join(self.out, n)).st_mtime_ns
                          for n in self.OUTSIDE}, stamps)
        manifest = mct._read_output_manifest(self.out)
        self.assertEqual({n: manifest.get(n) for n in self.OUTSIDE},
                         {n: "earlier" for n in self.OUTSIDE})
        self.assertIn("Conversion complete: 13 tiles.", r.logs)


if __name__ == "__main__":
    unittest.main()
