"""Map Converter: two-phase conversion and disk safety (2026-10-21).

Two-phase (replaces the 77 s per-file pixel scan the owner rejected), with
the download running from t = 0:

* the pending tiles an XYZ entry overlaps are partitioned: PROVEN — the
  RECTANGLES of the files above the first XYZ entry cover the tile plus its
  halo — go to Phase 2, every other one's pool download starts at once, in
  its own thread, WHILE Phase 2 converts (one converter job per pool and
  zoom: the same XYZ service twice in a stack is one pool, one job);
* Phase 2 converts the proven tiles from those files ALONE, into
  ``<output>/.phase1-<pid>/``; the converter's ``[Tile] <name>
  covered=a/b`` line (aether_converter 0.2.7) decides: complete → moved
  into the output dir (manifest = the files-only fingerprint), a hole or no
  line → deleted and queued for a second, small download pass;
* Phase 3 converts every pool-needing tile with the whole stack — its job
  is byte for byte the one-shot full-stack job;
* an older converter → no Phase 2 (download for every tile, as before);
* Stop kills every process (Phase 2 and each download); a failure in any
  branch ends the run with that error after the others are stopped;
* cancel / failure never leaves the scratch dir or an unpromoted tile; a
  promotion Windows keeps refusing queues the tile for Phase 3 instead;
* a second run over unchanged inputs rebuilds nothing.

Disk: per drive (output dir, pool, %TEMP%) output tiles + pool tiles still
to fetch + window copies, refused when need > free − 2 GB — in the label,
in ``_on_run``, and re-checked by the worker before the download and before
Phase 3; a short output file is rebuilt, not kept. Site Analysis refuses the
same way for coverage rasters + pool.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import waveshed.core.terrain_adapter as ta
from waveshed.core import output_estimate as oe
import waveshed.gui.map_converter_tab as mct
from waveshed.gui.map_converter_tab import _LayerEntry, _MapConverterWorker

_XYZ = ("type=xyz&url=https%3A//example.com/%7Bz%7D/%7Bx%7D/%7By%7D.png"
        "&zmax=15")
#: 30 m: four 0.5 deg tiles, A B (south row) and C D (north row).
_EXT = {"south": 47.0, "north": 48.0, "west": 8.0, "east": 9.0}
GB = 1024 ** 3
_HUGE = 10 ** 15


def _b(w, e, s, n):
    return {"west": w, "east": e, "south": s, "north": n}


def _info(path, box, fp="1:2", **extra):
    info = {"path": path, "crs": None, "crs_authid": "EPSG:4326",
            "native_bounds": dict(box), "wgs84_bounds": dict(box),
            "halo_deg": 0.001, "fp": fp, "materialize": None}
    info.update(extra)
    return info


def _tiles():
    expected = mct._enumerate_tiles(mct._snap_bbox(_EXT, [30]), [30])
    by_name = {t["filename"]: (r, t) for r, t in expected}
    names = {"A": "tile_N47.50E8.00_30m.abt", "B": "tile_N47.50E8.50_30m.abt",
             "C": "tile_N48.00E8.00_30m.abt", "D": "tile_N48.00E8.50_30m.abt"}
    assert set(by_name) == set(names.values()), sorted(by_name)
    return expected, names


def _files(fp_a2="1:2"):
    """Files whose RECTANGLES cover A, B and C plus their halo (each
    0.01 deg wider than its tile, so a, b and c also reach a sliver of the
    neighbours); D has no file of its own — only those slivers, so its
    rectangles do not cover it (unproven: downloaded at once). ``a2`` lies
    inside A alone (no halo reaches a neighbour): its fingerprint moves A
    only."""
    return [_info("/dem/a.tif", _b(7.99, 8.51, 46.99, 47.51)),
            _info("/dem/b.tif", _b(8.49, 9.01, 46.99, 47.51)),
            _info("/dem/c.tif", _b(7.99, 8.51, 47.49, 48.01)),
            _info("/dem/a2.tif", _b(8.05, 8.45, 47.05, 47.45), fp=fp_a2)]


def _write_tile(job, short_by=0):
    """What the converter leaves: a real ``.abt`` header over a full-length
    (sparse) body — ``_tile_is_whole`` reads it."""
    size = job["size_px"]
    stride = (size * 2 + 255) & ~255
    px = 1.0 / size
    with open(job["output_path"], "wb") as fh:
        fh.write(b"AETH" + struct.pack("<HH", 1, size)
                 + struct.pack("<dddd", job["ul_lat"], job["ul_lon"], px, px)
                 + struct.pack("<hH", 0, stride))
        fh.truncate(ta._abt_whole_bytes(size) - short_by)


class _Converter:
    """Fake ``run_converter_streaming``: writes every job's output whole and
    prints ``[Tile]`` lines from *coverage* ({name: (covered, total)} or
    None = no line) for a Phase-2 batch; Phase 3 tiles are complete.
    *before_phase2()* runs first in a Phase-2 call (a test's sync point)."""

    def __init__(self, coverage=None, tile_lines=True, on_phase2=None,
                 before_phase2=None):
        self.coverage = coverage or {}
        self.tile_lines = tile_lines
        self.on_phase2 = on_phase2
        self.before_phase2 = before_phase2
        self.calls = []

    def __call__(self, exe, args, on_line, on_start=None, env=None):
        jobs = json.load(open(args[-1], encoding="utf-8"))
        phase2 = any(mct._PHASE2_SCRATCH_PREFIX in j["output_path"]
                     for j in jobs)
        self.calls.append({"phase2": phase2, "jobs": jobs})
        self.on_start = on_start            # for hooks that start a process
        if phase2 and self.before_phase2 is not None:
            self.before_phase2()
        if phase2 and self.on_phase2 is not None:
            return self.on_phase2(jobs, on_line)
        for job in jobs:
            _write_tile(job)
            name = os.path.basename(job["output_path"])
            px = job["size_px"] ** 2
            cov = self.coverage.get(name, (px, px)) if phase2 else (px, px)
            if self.tile_lines and cov is not None:
                on_line(f"[Tile] {name} covered={cov[0]}/{cov[1]}")
        on_line(f"[Rust] Progress: {len(jobs)}/{len(jobs)}")
        return 0


class _Run:
    """One worker run over [files, xyz] (or a given stack) with fakes for
    the engine, the pool download and the free space."""

    def __init__(self, test, out_dir, cache_dir, entries=None, overwrite=False,
                 converter=None, reports_tiles=True, free=None):
        self.out_dir, self.cache_dir = out_dir, cache_dir
        self.entries = entries if entries is not None else [
            {"kind": "files", "infos": _files()},
            {"kind": "xyz", "uri": _XYZ, "extent": dict(_EXT)}]
        self.converter = converter or _Converter()
        self.reports_tiles = reports_tiles
        self.free = free if free is not None else {"v": _HUGE}
        self.asked = []
        self.pool_calls = []        # the specs of each download call, in order
        self._lock = threading.Lock()
        self.status, self.logs, self.errs, self.oks = [], [], [], []
        self.overwrite = overwrite
        self.test = test

    def pool(self, uri, specs, res, **kw):
        # Called from the worker's download threads too.
        with self._lock:
            self.asked.extend(t["filename"] for t in specs)
            self.pool_calls.append(sorted(t["filename"] for t in specs))
            call_no = len(self.pool_calls)
        self.on_pool(call_no, specs, kw)
        return {t["filename"]: f"/pool/{t['filename']}" for t in specs}

    def on_pool(self, call_no, specs, kw):
        pass

    def run(self):
        layers = [_LayerEntry(layer_type="raster", source_path=f"/src{k}",
                              extent=dict(_EXT))
                  for k in range(len(self.entries))]
        resolved = {f"/src{k}": e for k, e in enumerate(self.entries)}
        worker = _MapConverterWorker(layers, self.out_dir, [30],
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
        os.makedirs(self.cache)
        self.expected, self.n = _tiles()

    def tearDown(self):
        self._tmp.cleanup()

    def run_(self, **kw):
        return _Run(self, self.out, self.cache, **kw).run()

    def outputs(self):
        if not os.path.isdir(self.out):
            return set()
        return {n for n in os.listdir(self.out) if n.endswith(".abt")}

    def scratch_dirs(self):
        if not os.path.isdir(self.out):
            return []
        return [n for n in os.listdir(self.out)
                if n.startswith(mct._PHASE2_SCRATCH_PREFIX)]


# ---------------------------------------------------------------------------
# The [Tile] line
# ---------------------------------------------------------------------------

class TileLineParser(unittest.TestCase):

    def test_parses_name_and_counts(self):
        self.assertEqual(
            ta.parse_tile_line("[Tile] tile_N47.50E8.00_30m.abt "
                               "covered=3429904/3429904"),
            ("tile_N47.50E8.00_30m.abt", 3429904, 3429904))
        self.assertEqual(ta.parse_tile_line("[Tile] t.abt covered=0/16"),
                         ("t.abt", 0, 16))

    def test_a_name_with_spaces_and_a_prefix(self):
        self.assertEqual(ta.parse_tile_line(
            "stdout: [Tile] my tile.abt covered=5/9  "), ("my tile.abt", 5, 9))

    def test_other_lines_are_not_tile_lines(self):
        for line in ("[Rust] Progress: 10/40", "[Tile] t.abt covered=",
                     "[Tile] covered=1/2", "[Error] Failed to process tile",
                     "", None):
            self.assertIsNone(ta.parse_tile_line(line), line)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

class TwoPhaseRouting(_Case):

    def test_complete_holed_and_unknown_tiles_are_routed(self):
        n = self.n
        conv = _Converter(coverage={n["B"]: (100, 3429904), n["C"]: None})
        r = self.run_(converter=conv)
        self.assertEqual(r.errs, [])
        self.assertEqual(r.oks, [self.out])
        # Phase 2: A B C, the tiles the files cover (D only by slivers);
        # scratch in the output dir, named by the PID.
        p2, *p3 = conv.calls
        self.assertTrue(p2["phase2"])
        scratch = os.path.join(self.out, f".phase1-{os.getpid()}")
        self.assertEqual(
            sorted(j["output_path"] for j in p2["jobs"]),
            sorted(os.path.join(os.path.abspath(scratch), n[k])
                   for k in "ABC"))
        for job in p2["jobs"]:
            self.assertFalse(any(s["path"].startswith(os.path.abspath(
                "/pool")) for s in job["sources"]), "Phase 2 read the pool")
        # D's pool download starts with Phase 2; the holes Phase 2 left get
        # the second pass; nothing else is ever downloaded.
        self.assertEqual(r.pool_calls, [[n["D"]], sorted([n["B"], n["C"]])])
        # Phase 3 batch by batch (2026-10-23): D's band once its download
        # landed, the holes once theirs did.
        self.assertFalse(any(c["phase2"] for c in p3))
        self.assertEqual(sorted(sorted(os.path.basename(j["output_path"])
                                       for j in c["jobs"]) for c in p3),
                         [sorted([n["B"], n["C"]]), [n["D"]]])
        for job in [j for c in p3 for j in c["jobs"]]:
            self.assertEqual(os.path.dirname(job["output_path"]),
                             os.path.abspath(self.out))
        self.assertEqual(self.outputs(), set(n.values()))
        self.assertEqual(self.scratch_dirs(), [])
        self.assertIn("Phase 2: 3 tiles from local files — 1 complete, "
                      "2 need the pool", r.logs)
        self.assertIn("  3 tile(s) covered by the local files → Phase 2; 1 "
                      "tile(s) need the pool → downloaded now", r.logs)
        # The status names what runs at the same time.
        self.assertIn("Converting 3 tiles from local files; downloading "
                      "terrain for 1 tile(s) (z12)", r.status)
        self.assertIn("Converting band 1/1: 1 .abt tiles...", r.status)
        self.assertIn("Converting Phase-2 holes: 2 .abt tiles...", r.status)
        self.assertIn("Phase 3: 3 tile(s) converted with the whole stack in "
                      "2 batch(es).", r.logs)
        self.assertIn("Conversion complete: 4 tiles.", r.logs)
        self.assertEqual(r.status[-1], "Done.")

    def test_manifest_files_only_for_phase2_full_stack_for_phase3(self):
        n = self.n
        r = self.run_(converter=_Converter(coverage={n["B"]: (1, 2)}))
        self.assertEqual(r.errs, [])
        files_only = {}
        full = mct._stack_tile_fingerprints(
            self.expected, [{"kind": "files", "infos": _files()},
                            {"kind": "xyz", "uri": _XYZ,
                             "extent": dict(_EXT)}],
            None, files_only=files_only)
        manifest = mct._read_output_manifest(self.out)
        for k in "AC":                 # complete in Phase 2
            self.assertEqual(manifest[n[k]], files_only[n[k]], k)
            self.assertNotEqual(files_only[n[k]], full[n[k]], k)
        for k in "BD":                 # Phase 3, whole stack
            self.assertEqual(manifest[n[k]], full[n[k]], k)

    def test_old_converter_keeps_the_one_pass_flow(self):
        conv = _Converter()
        r = self.run_(converter=conv, reports_tiles=False)
        self.assertEqual(r.errs, [])
        # No Phase 2; every tile downloaded, then converted with the whole
        # stack — one batch per 0.5 deg band, the north band first.
        self.assertEqual([c["phase2"] for c in conv.calls], [False, False])
        self.assertEqual(sorted(r.asked), sorted(self.n.values()))
        self.assertEqual([sorted(os.path.basename(j["output_path"])
                                 for j in c["jobs"]) for c in conv.calls],
                         [sorted(self.n[k] for k in "CD"),
                          sorted(self.n[k] for k in "AB")])
        self.assertTrue(any(line.startswith("  Phase 2 skipped") and "0.2.7"
                            in line for line in r.logs), r.logs)

    def test_no_xyz_entry_no_phase2(self):
        conv = _Converter()
        r = self.run_(converter=conv,
                      entries=[{"kind": "files", "infos": _files()}])
        self.assertEqual(r.errs, [])
        self.assertEqual([c["phase2"] for c in conv.calls], [False])
        self.assertEqual(r.asked, [])

    def test_files_below_the_xyz_entry_are_not_phase2_sources(self):
        # The pool outranks them: a files-only build there would differ
        # from the full stack, so no tile goes through Phase 2 (one Phase-3
        # batch per band).
        conv = _Converter()
        r = self.run_(converter=conv, entries=[
            {"kind": "xyz", "uri": _XYZ, "extent": dict(_EXT)},
            {"kind": "files", "infos": _files()}])
        self.assertEqual([c["phase2"] for c in conv.calls], [False, False])
        self.assertEqual(sorted(r.asked), sorted(self.n.values()))

    def test_a_converter_without_tile_lines_queues_everything(self):
        # 0.2.7 claimed, no [Tile] line seen: coverage unknown = queued.
        conv = _Converter(tile_lines=False)
        r = self.run_(converter=conv)
        self.assertEqual(r.errs, [])
        self.assertEqual(r.pool_calls,
                         [[self.n["D"]], sorted(self.n[k] for k in "ABC")])
        self.assertIn("Phase 2: 3 tiles from local files — 0 complete, "
                      "3 need the pool", r.logs)
        self.assertEqual(self.outputs(), set(self.n.values()))

    def test_phase3_job_equals_the_one_shot_full_stack_job(self):
        n = self.n
        conv = _Converter(coverage={n["B"]: (7, 9)})
        r = self.run_(converter=conv)
        phase3 = {os.path.basename(j["output_path"]): j
                  for c in conv.calls if not c["phase2"] for j in c["jobs"]}
        # One shot: the whole stack, the pool tiles the download returned.
        entries = [{"kind": "files", "infos": _files()},
                   {"kind": "xyz", "uri": _XYZ, "extent": dict(_EXT),
                    "pool": {30: {t["filename"]: f"/pool/{t['filename']}"
                                  for _r, t in self.expected}}}]
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(mct.tempfile, "gettempdir", return_value=tmp):
            one_shot, _t = mct._build_tile_jobs(
                self.expected, self.out, True, entries, None,
                tempfile.gettempdir())
        one_shot = {os.path.basename(j["output_path"]): j for j in one_shot}
        self.assertEqual(sorted(phase3), sorted(n[k] for k in "BD"))
        for name, job in phase3.items():
            self.assertEqual(job, one_shot[name], name)
        self.assertEqual(r.errs, [])


class TwoPhaseFingerprints(_Case):

    def test_a_second_run_rebuilds_nothing(self):
        n = self.n
        first = self.run_(converter=_Converter(coverage={n["B"]: (1, 2)}))
        self.assertEqual(first.errs, [])
        again = _Converter()
        second = self.run_(converter=again)
        self.assertEqual(second.errs, [])
        self.assertEqual(again.calls, [], "unchanged tiles were rebuilt")
        self.assertEqual(second.asked, [])
        self.assertEqual(second.status[-1], "All tiles already exist.")

    def test_a_changed_file_rebuilds_its_tile_only(self):
        n = self.n
        self.run_(converter=_Converter(coverage={n["B"]: (1, 2)}))
        conv = _Converter()
        changed = [{"kind": "files", "infos": _files(fp_a2="9:9")},
                   {"kind": "xyz", "uri": _XYZ, "extent": dict(_EXT)}]
        r = self.run_(converter=conv, entries=changed)
        self.assertEqual(r.errs, [])
        self.assertEqual([[os.path.basename(j["output_path"])
                           for j in c["jobs"]] for c in conv.calls],
                         [[n["A"]]])
        self.assertEqual(r.asked, [])
        self.assertTrue(any("1 existing tile(s) are rebuilt" in line
                            for line in r.logs), r.logs)

    def test_a_files_only_stack_record_does_not_hide_a_new_pool(self):
        # Built from files alone with no XYZ entry (holes unproven), then an
        # XYZ entry is added below: the tiles must be looked at again.
        n = self.n
        self.run_(converter=_Converter(),
                  entries=[{"kind": "files", "infos": _files()}])
        conv = _Converter(coverage={n["B"]: (1, 2)})
        r = self.run_(converter=conv)
        self.assertEqual(r.errs, [])
        self.assertTrue(conv.calls[0]["phase2"])
        self.assertEqual(sorted(r.asked), sorted(n[k] for k in "BD"))
        self.assertTrue(any("4 existing tile(s) are rebuilt" in line
                            for line in r.logs), r.logs)


class TwoPhaseCancelAndFailure(_Case):

    def test_cancel_mid_phase2_leaves_no_scratch_and_no_tile(self):
        run = {}

        def phase2(jobs, on_line):
            # One tile is written (atomically) before Stop lands.
            with open(jobs[0]["output_path"], "wb") as fh:
                fh.truncate(ta._abt_whole_bytes(jobs[0]["size_px"]))
            on_line(f"[Tile] {os.path.basename(jobs[0]['output_path'])} "
                    f"covered=4/4")
            run["r"].worker.cancel()
            raise ta.ConverterCancelled()

        r = _Run(self, self.out, self.cache,
                 converter=_Converter(on_phase2=phase2))
        run["r"] = r
        r.run()
        self.assertEqual(r.errs, [])
        self.assertEqual(r.oks, [])
        self.assertIn("Cancelled.", r.status)
        self.assertEqual(self.scratch_dirs(), [])
        self.assertEqual(self.outputs(), set())
        self.assertEqual(mct._read_output_manifest(self.out), {})
        # Only D's download (started with Phase 2), never a second pass.
        self.assertLessEqual(set(r.asked), {self.n["D"]})

    def test_failed_phase2_is_an_error_and_leaves_no_scratch(self):
        def phase2(jobs, on_line):
            for job in jobs:
                with open(job["output_path"], "wb") as fh:
                    fh.truncate(ta._abt_whole_bytes(job["size_px"]))
            return 1

        r = self.run_(converter=_Converter(on_phase2=phase2))
        self.assertEqual(len(r.errs), 1)
        self.assertIn("exit code 1", r.errs[0])
        self.assertEqual(self.scratch_dirs(), [])
        self.assertEqual(self.outputs(), set())
        self.assertLessEqual(set(r.asked), {self.n["D"]})

    def test_a_stale_scratch_dir_is_removed(self):
        stale = os.path.join(self.out, ".phase1-999999")
        os.makedirs(stale)
        open(os.path.join(stale, self.n["A"]), "w").close()
        r = self.run_(converter=_Converter())
        self.assertEqual(r.errs, [])
        self.assertEqual(self.scratch_dirs(), [])


# ---------------------------------------------------------------------------
# The partition and the concurrency (2026-10-21)
# ---------------------------------------------------------------------------

_XYZ2 = ("type=xyz&url=https%3A//other.example.org/%7Bz%7D/%7Bx%7D/%7By%7D"
         ".png&zmax=15")


def _sleeper(test):
    """A real process standing in for a converter run: killed by Stop."""
    proc = subprocess.Popen([sys.executable, "-c",
                             "import time; time.sleep(60)"])
    test.addCleanup(lambda: proc.poll() is None and proc.kill())
    return proc


def _wait_for(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _first_band_landed(run):
    """A ``before_phase2`` sync: Phase 2's ingest waits until the first
    band's download has landed and its batch is queued (progress slot
    ``("convert", 1)``) — the state the old flow's join of every download
    guaranteed when Phase 2 ended, which the disk re-check tests price."""
    def wait():
        assert _wait_for(lambda: run.worker.progress_model.work(
            ("convert", 1)) > 0), "the first band never landed"
    return wait


class Partition(_Case):

    def _names(self, tiles):
        return sorted(t["filename"] for _r, t in tiles)

    def test_proven_are_the_tiles_the_files_cover_by_rectangles(self):
        n = self.n
        proven, download = mct._partition_pending(
            self.expected, [{"kind": "files", "infos": _files()},
                            {"kind": "xyz", "uri": _XYZ}])
        self.assertEqual(self._names(proven), sorted(n[k] for k in "ABC"))
        # D has file contributors (slivers of a, b, c) but they do not
        # cover it: its pool tile is needed, downloaded at once.
        tile_d = [t for _r, t in self.expected if t["filename"] == n["D"]][0]
        self.assertEqual(
            len(ta._tile_contributors(_files(), mct._tile_bbox_of(30, tile_d),
                                      tile_d["exact_res_m"])), 3)
        self.assertEqual(self._names(download), [n["D"]])

    def test_files_below_the_first_xyz_entry_prove_nothing(self):
        proven, download = mct._partition_pending(
            self.expected, [{"kind": "xyz", "uri": _XYZ},
                            {"kind": "files", "infos": _files()}])
        self.assertEqual(proven, [])
        self.assertEqual(self._names(download), sorted(self.n.values()))

    def test_no_xyz_entry_no_partition(self):
        self.assertEqual(mct._partition_pending(
            self.expected, [{"kind": "files", "infos": _files()}]), ([], []))

    def test_the_estimates_use_the_same_partition(self):
        entries = [{"kind": "files", "infos": _files()},
                   {"kind": "xyz", "uri": _XYZ}]
        self.assertEqual(
            self._names(mct._pool_estimate_tiles(self.expected, entries)),
            [self.n["D"]])
        model = mct.run_progress.RunProgress()
        with mock.patch.object(ta, "get_cache_dir", return_value=self.cache):
            mct._estimate_download_work(
                model, [entries[1]],
                mct._partition_pending(self.expected, entries)[1],
                pool_check=True)
            items = mct._disk_items(
                [], entries, self.out, self.cache,
                download=mct._pool_estimate_tiles(self.expected, entries))
        self.assertEqual(model.keys(), [("download", 0, 12)])
        size = dict((t["filename"], t["size_px"]) for _r, t in self.expected)
        self.assertEqual([i[2] for i in items if i[0] == mct._NEED_POOL],
                         [ta._abt_whole_bytes(size[self.n["D"]])])


class Concurrency(_Case):
    """The download of the unproven tiles runs WHILE Phase 2 converts."""

    def test_phase2_and_the_download_run_at_the_same_time(self):
        p2_started, dl_started = threading.Event(), threading.Event()
        seen = {}

        def phase2(jobs, on_line):
            p2_started.set()
            # Only a concurrent download can set this while Phase 2 waits.
            seen["download_during_phase2"] = dl_started.wait(10)
            seen["label"] = r.worker.progress_model.label()
            for job in jobs:
                _write_tile(job)
                px = job["size_px"] ** 2
                on_line(f"[Tile] {os.path.basename(job['output_path'])} "
                        f"covered={px}/{px}")
            return 0

        r = _Run(self, self.out, self.cache,
                 converter=_Converter(on_phase2=phase2))

        def pool(call_no, specs, kw):
            kw["progress_cb"].download_event({
                "event": "progress", "done_units": 5, "total_units": 10,
                "ceiling_units": 5, "pass": 1, "max_passes": 3, "zoom": 12,
                "pass_done": 5, "pass_total": 10, "mbps": 2.5, "errors": 0})
            dl_started.set()
            # ...and only a concurrent Phase 2 can set this.
            seen["phase2_during_download"] = p2_started.wait(10)
            _wait_for(lambda: "label" in seen)

        r.on_pool = pool
        r.run()
        self.assertEqual(r.errs, [])
        self.assertTrue(seen["download_during_phase2"],
                        "the download had not started while Phase 2 ran")
        self.assertTrue(seen["phase2_during_download"],
                        "Phase 2 had not started while the download ran")
        # The one bar's label names both activities.
        self.assertIn("Converting tiles from local files", seen["label"])
        self.assertIn("Downloading terrain z12 (30 m)", seen["label"])
        self.assertIn("5/10 source tiles", seen["label"])
        self.assertEqual(r.oks, [self.out])
        self.assertEqual(self.outputs(), set(self.n.values()))
        self.assertEqual(r.worker.progress_model.activities(), [])

    def test_two_xyz_entries_download_at_the_same_time(self):
        started = {_XYZ: threading.Event(), _XYZ2: threading.Event()}
        both = []
        r = _Run(self, self.out, self.cache, entries=[
            {"kind": "files", "infos": _files()},
            {"kind": "xyz", "uri": _XYZ, "extent": dict(_EXT)},
            {"kind": "xyz", "uri": _XYZ2, "extent": dict(_EXT)}])
        orig = r.pool

        def pool(uri, specs, res, **kw):
            started[uri].set()
            both.append(all(e.wait(10) for e in started.values()))
            return orig(uri, specs, res, **kw)

        with mock.patch.object(r, "pool", side_effect=pool):
            r.run()
        self.assertEqual(r.errs, [])
        self.assertEqual(both, [True, True])
        self.assertEqual(r.asked, [self.n["D"], self.n["D"]])

    def test_a_failed_download_stops_phase2_and_is_the_error(self):
        procs = {}

        def phase2(jobs, on_line):
            proc = procs["p2"] = _sleeper(self)
            conv.on_start(proc)            # registered like a real ingest
            return proc.wait(20)           # until something kills it

        conv = _Converter(on_phase2=phase2)
        r = _Run(self, self.out, self.cache, converter=conv)

        def pool(call_no, specs, kw):
            _wait_for(lambda: "p2" in procs)
            raise RuntimeError("Terrain download failed: service down")

        r.on_pool = pool
        t0 = time.monotonic()
        r.run()
        self.assertLess(time.monotonic() - t0, 15, "Phase 2 was not stopped")
        self.assertEqual(r.errs, ["Terrain download failed: service down"])
        self.assertIsNotNone(procs["p2"].poll(), "Phase 2's process lives")
        self.assertNotIn("Cancelled.", r.status)
        self.assertEqual(r.oks, [])
        self.assertEqual(self.outputs(), set())
        self.assertEqual(self.scratch_dirs(), [])

    def test_a_failed_phase2_stops_the_download_and_is_the_error(self):
        procs = {}

        def phase2(jobs, on_line):
            _wait_for(lambda: "dl" in procs)
            return 1

        r = _Run(self, self.out, self.cache,
                 converter=_Converter(on_phase2=phase2))

        def pool(call_no, specs, kw):
            proc = procs["dl"] = _sleeper(self)
            kw["on_start"](proc)
            proc.wait(20)
            if kw["should_cancel"]():      # as the real download pass does
                raise ta.ConverterCancelled()

        r.on_pool = pool
        t0 = time.monotonic()
        r.run()
        self.assertLess(time.monotonic() - t0, 15, "the download was not "
                        "stopped")
        self.assertEqual(len(r.errs), 1)
        self.assertIn("exit code 1", r.errs[0])
        self.assertIsNotNone(procs["dl"].poll(), "the download lives on")
        self.assertEqual(self.scratch_dirs(), [])

    def test_stop_kills_every_process(self):
        procs = {}

        def phase2(jobs, on_line):
            proc = procs["p2"] = _sleeper(self)
            conv.on_start(proc)
            return proc.wait(20)

        conv = _Converter(on_phase2=phase2)
        r = _Run(self, self.out, self.cache, converter=conv)

        def pool(call_no, specs, kw):
            proc = procs["dl"] = _sleeper(self)
            kw["on_start"](proc)
            proc.wait(20)
            if kw["should_cancel"]():
                raise ta.ConverterCancelled()

        r.on_pool = pool
        runner = threading.Thread(target=r.run)
        runner.start()
        self.assertTrue(_wait_for(lambda: len(procs) == 2),
                        "Phase 2 and the download never ran together")
        self.assertEqual({p.poll() for p in procs.values()}, {None})
        r.worker.cancel()                  # ONE Stop
        runner.join(20)
        self.assertFalse(runner.is_alive(), "the worker did not end")
        self.assertIsNotNone(procs["p2"].poll())
        self.assertIsNotNone(procs["dl"].poll())
        self.assertEqual(r.errs, [])
        self.assertEqual(r.oks, [])
        self.assertEqual(r.status[-1], "Cancelled.")
        self.assertEqual(self.scratch_dirs(), [])
        self.assertEqual(self.outputs(), set())

    def test_a_process_started_after_stop_is_killed(self):
        with tempfile.TemporaryDirectory() as out:
            worker = _MapConverterWorker([], out, [30], [])
        first, late = _sleeper(self), _sleeper(self)
        worker._register_proc(first)
        worker.cancel()
        worker._register_proc(late)        # a branch launching after Stop
        self.assertIsNotNone(first.wait(10))
        self.assertIsNotNone(late.wait(10))


class RefusedPromotion(_Case):
    """Phase 2's move into the output dir, refused by Windows (the target
    held open by another process: Defender's on-close scan, a viewer):
    retried; still refused → the tile is queued like a hole — downloaded
    and converted in Phase 3 — never an "Unexpected error" ending the run."""

    def run_refusing(self, name, refusals=None):
        """A run whose rename onto ``<output>/<name>`` is refused *refusals*
        times (None = every time)."""
        target = os.path.abspath(os.path.join(self.out, name))
        real, tries = os.replace, []

        def fake(src, dst):
            if os.path.abspath(dst) == target:
                tries.append(src)
                if refusals is None or len(tries) <= refusals:
                    raise PermissionError(
                        13, "The process cannot access the file because it "
                        "is being used by another process", src, dst)
            return real(src, dst)
        with mock.patch.object(ta.os, "replace", side_effect=fake), \
             mock.patch.object(ta.time, "sleep"):
            r = self.run_()
        return r, tries

    def test_a_refusal_that_persists_sends_the_tile_to_phase3(self):
        n = self.n
        r, tries = self.run_refusing(n["A"])
        self.assertEqual(r.errs, [])
        self.assertEqual(r.oks, [self.out])
        self.assertEqual(len(tries), 5, "five attempts")
        self.assertTrue(any(f"could not move {n['A']} into the output folder"
                            in m for m in r.logs), r.logs)
        self.assertIn("Phase 2: 3 tiles from local files — 2 complete, "
                      "1 need the pool", r.logs)
        # Downloaded in the second pass, converted with the whole stack (in
        # the holes' batch; D in its band's).
        self.assertEqual(r.pool_calls, [[n["D"]], [n["A"]]])
        p3 = [c for c in r.converter.calls if not c["phase2"]]
        self.assertEqual(sorted(os.path.basename(j["output_path"])
                                for c in p3 for j in c["jobs"]),
                         sorted([n["A"], n["D"]]))
        self.assertEqual(self.outputs(), set(n.values()))
        full = mct._stack_tile_fingerprints(
            self.expected, [{"kind": "files", "infos": _files()},
                            {"kind": "xyz", "uri": _XYZ,
                             "extent": dict(_EXT)}], None, files_only={})
        self.assertEqual(mct._read_output_manifest(self.out)[n["A"]],
                         full[n["A"]])
        self.assertEqual(self.scratch_dirs(), [])

    def test_a_transient_refusal_still_promotes_the_tile(self):
        n = self.n
        r, tries = self.run_refusing(n["A"], refusals=2)
        self.assertEqual(r.errs, [])
        self.assertEqual(len(tries), 3)
        self.assertIn("Phase 2: 3 tiles from local files — 3 complete, "
                      "0 need the pool", r.logs)
        self.assertEqual(r.pool_calls, [[n["D"]]])
        files_only = {}
        mct._stack_tile_fingerprints(
            self.expected, [{"kind": "files", "infos": _files()},
                            {"kind": "xyz", "uri": _XYZ,
                             "extent": dict(_EXT)}], None,
            files_only=files_only)
        self.assertEqual(mct._read_output_manifest(self.out)[n["A"]],
                         files_only[n["A"]])
        self.assertFalse(any("could not move" in m for m in r.logs))


class OnePoolOneDownload(_Case):
    """The same XYZ layer twice in one stack — or two spellings of one
    service — is ONE pool: one branch and one converter job per zoom serve
    every entry (two concurrent jobs wrote the same pool tiles), and each
    entry still contributes only its own tiles."""

    #: _XYZ with its parameters in another order: the same pool.
    RESPELLED = ("zmax=15&type=xyz&url=https%3A//example.com/%7Bz%7D/%7Bx%7D/"
                 "%7By%7D.png")
    SOUTH = {"south": 47.05, "north": 47.45, "west": 8.05, "east": 8.95}
    NORTH = {"south": 47.55, "north": 47.95, "west": 8.05, "east": 8.95}

    def run_entries(self, entries):
        r = _Run(self, self.out, self.cache, entries=entries)
        orig, lock, active, overlapped = r.pool, threading.Lock(), [], []

        def pool(uri, specs, res, **kw):
            with lock:
                active.append(uri)
                overlapped.append(len(active) > 1)
            try:
                time.sleep(0.05)          # room for a second job to start
                return orig(uri, specs, res, **kw)
            finally:
                with lock:
                    active.remove(uri)
        with mock.patch.object(r, "pool", side_effect=pool):
            r.run()
        self.assertEqual(r.errs, [])
        self.assertNotIn(True, overlapped, "two downloads into one pool at once")
        return r

    def pool_sources(self, job):
        return [s["path"] for s in job["sources"]
                if s["path"].startswith(os.path.abspath("/pool"))]

    def test_two_entries_of_one_service_share_one_job(self):
        n = self.n
        self.assertEqual(ta._pool_dir(_XYZ), ta._pool_dir(self.RESPELLED))
        entries = [{"kind": "xyz", "uri": _XYZ, "extent": dict(self.SOUTH)},
                   {"kind": "xyz", "uri": self.RESPELLED,
                    "extent": dict(self.NORTH)}]
        r = self.run_entries(entries)
        self.assertEqual(len(r.worker._branches), 1, "one branch per pool")
        # One job per latitude band (north first, 2026-10-23) for both
        # entries' tiles — never one per entry.
        self.assertEqual(r.pool_calls, [sorted([n["C"], n["D"]]),
                                        sorted([n["A"], n["B"]])],
                         "one job per band for both entries' tiles")
        self.assertIn("  XYZ entries 1, 2 use one terrain pool (the same "
                      "service) — one download serves them all", r.logs)
        # Each entry keeps its own tiles: its extent still decides.
        self.assertEqual(sorted(entries[0]["pool"][30]), sorted([n["A"], n["B"]]))
        self.assertEqual(sorted(entries[1]["pool"][30]), sorted([n["C"], n["D"]]))
        jobs = [j for c in r.converter.calls for j in c["jobs"]]
        self.assertEqual(len(jobs), 4)
        for job in jobs:
            name = os.path.basename(job["output_path"])
            self.assertEqual(self.pool_sources(job),
                             [os.path.abspath(f"/pool/{name}")], name)
        model = r.worker.progress_model
        self.assertGreater(model.work(("download", 0, 12)), 0)
        self.assertEqual(model.work(("download", 1, 12)), 0.0,
                         "the shared job is sized once, under entry 1")
        self.assertEqual(model.percent(), 100)

    def test_the_same_layer_twice_downloads_each_tile_once(self):
        n = self.n
        entries = [{"kind": "xyz", "uri": _XYZ, "extent": dict(_EXT)},
                   {"kind": "xyz", "uri": _XYZ, "extent": dict(_EXT)}]
        r = self.run_entries(entries)
        self.assertEqual(r.pool_calls, [sorted([n["C"], n["D"]]),
                                        sorted([n["A"], n["B"]])])
        self.assertEqual(sorted(r.asked), sorted(n.values()), "each tile once")
        for ent in entries:
            self.assertEqual(sorted(ent["pool"][30]), sorted(n.values()))
        for job in [j for c in r.converter.calls for j in c["jobs"]]:
            # both entries, as the stack says
            self.assertEqual(len(self.pool_sources(job)), 2)

    def test_the_merged_calls_and_the_estimate_agree(self):
        n = self.n
        entries = [{"kind": "xyz", "uri": _XYZ, "extent": dict(self.SOUTH)},
                   {"kind": "xyz", "uri": _XYZ2, "extent": dict(_EXT)},
                   {"kind": "xyz", "uri": self.RESPELLED,
                    "extent": dict(self.NORTH)}]
        plan = mct._xyz_download_plan(entries, self.expected)
        calls = mct._pool_calls(plan)
        self.assertEqual([(k, z, sorted(t["filename"] for _r, t in g))
                          for (k, _e, z, g), _m in calls],
                         [(0, 12, sorted(n.values())),
                          (1, 12, sorted(n.values()))])
        (_lead, members), _other = calls
        self.assertEqual([m is e for (m, _g), e in zip(members, entries[::2])],
                         [True, True])
        model = mct.run_progress.RunProgress()
        with mock.patch.object(ta, "get_cache_dir", return_value=self.cache):
            mct._estimate_download_work(model, entries, self.expected,
                                        pool_check=False)
        self.assertEqual(sorted(model.keys()), [("download", 0, 12),
                                                ("download", 1, 12)])


#: A converter stand-in: every subcommand announces itself in a marker file,
#: then runs until it is killed — two of them must be alive at once.
_STAND_IN = r'''
import os, sys, time
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd in ("--version", "-V"):
    print("aether_converter 0.2.7")
    sys.exit(0)
marks = os.environ["WAVESHED_STAND_IN_DIR"]
open(os.path.join(marks, "%s.%d" % (cmd, os.getpid())), "w").close()
print("[Stand-in] %s running" % cmd, flush=True)
time.sleep(120)
'''


def _stand_in_converter(test, root):
    """The stand-in's script — the path the test hands the plugin as its
    converter. :func:`_launching_stand_in` has the plugin's one runner start
    it as ``[sys.executable, script, …]`` on every OS, so the interpreter
    itself is the engine process Stop kills. (A Windows .bat wrapper was
    not: Stop killed cmd.exe, the Python grandchild ran on and held the
    pipe open; ``os.execv`` would not help — on Windows it spawns a new
    process too.)"""
    script = os.path.join(root, "aether_converter_stand_in.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(_STAND_IN)
    return script


def _launching_stand_in(script):
    """A ``run_converter_streaming`` replacement: a launch of *script* goes
    through the REAL runner (its Popen, pipes, flags, environment and
    ``on_start``) as ``sys.executable script *args``; every other launch is
    passed through unchanged."""
    real = ta.run_converter_streaming

    def runner(exe, args, on_line, on_start=None, env=None):
        if exe == script:
            exe, args = sys.executable, [script, *args]
        return real(exe, args, on_line, on_start=on_start, env=env)
    return runner


class StopKillsEveryRealProcess(unittest.TestCase):
    """Phase 2's ingest and the concurrent download as two REAL processes,
    both started through ``terrain_adapter.run_converter_streaming`` (the
    real ``ensure_pool_tiles`` download path and the real ingest call):
    one Stop kills both, the run ends as a quiet Cancel, and nothing of
    either is left behind."""

    def test_one_stop_kills_phase2_and_the_download(self):
        with tempfile.TemporaryDirectory() as root:
            marks = os.path.join(root, "marks")
            tmp = os.path.join(root, "tmp")
            cache = os.path.join(root, "cache")
            out = os.path.join(root, "out")
            for d in (marks, tmp, cache):
                os.makedirs(d)
            exe = _stand_in_converter(self, root)
            uri = ("type=xyz&url=https%3A//example.com/terrarium/%7Bz%7D/"
                   "%7Bx%7D/%7By%7D.png&zmax=15")
            entries = [{"kind": "files", "infos": _files()},
                       {"kind": "xyz", "uri": uri, "extent": dict(_EXT)}]
            layers = [_LayerEntry(layer_type="raster", source_path=f"/s{k}",
                                  extent=dict(_EXT)) for k in range(2)]
            resolved = {f"/s{k}": e for k, e in enumerate(entries)}
            worker = _MapConverterWorker(layers, out, [30],
                                         [e.source_path for e in layers])
            status, errs, oks = [], [], []
            worker.status.connect(status.append)
            worker.finished_err.connect(errs.append)
            worker.finished_ok.connect(oks.append)
            procs = []
            real_register = worker._register_proc

            def register(proc):
                procs.append(proc)
                real_register(proc)

            worker._register_proc = register
            from waveshed.core import binary_manager
            with mock.patch.object(_MapConverterWorker, "_resolve_raster",
                                   side_effect=lambda e: resolved[
                                       e["source_path"]]), \
                 mock.patch.object(_MapConverterWorker, "_resolve_buildings",
                                   return_value=None), \
                 mock.patch.object(mct, "find_binary", return_value=exe), \
                 mock.patch.object(binary_manager, "find_binary",
                                   return_value=exe), \
                 mock.patch.object(mct, "_plan_cross_check"), \
                 mock.patch.object(mct, "_converter_reports_tiles",
                                   return_value=True), \
                 mock.patch.object(ta, "preflight_ingest_jobs"), \
                 mock.patch.object(ta, "get_cache_dir", return_value=cache), \
                 mock.patch.object(ta, "run_converter_streaming",
                                   side_effect=_launching_stand_in(exe)), \
                 mock.patch.object(oe, "free_bytes", return_value=_HUGE), \
                 mock.patch.object(tempfile, "tempdir", tmp), \
                 mock.patch.dict(os.environ,
                                 {"WAVESHED_STAND_IN_DIR": marks}):
                runner = threading.Thread(target=worker.run)
                runner.start()
                both = _wait_for(lambda: sorted(
                    n.split(".")[0] for n in os.listdir(marks)) ==
                    ["download", "ingest"], timeout=30)
                alive = [p for p in procs if p.poll() is None]
                worker.cancel()            # ONE Stop
                runner.join(30)
                left = sorted(os.listdir(tmp))
            self.assertTrue(both, f"not both running: {os.listdir(marks)}")
            self.assertEqual(len(alive), 2, "two processes at once")
            self.assertFalse(runner.is_alive(), "the worker did not end")
            for proc in procs:
                self.assertIsNotNone(proc.poll(), "a process outlived Stop")
            self.assertEqual(errs, [])
            self.assertEqual(oks, [])
            self.assertEqual(status[-1], "Cancelled.")
            self.assertEqual(left, [], "job files left in %TEMP%")
            self.assertEqual([n for n in os.listdir(out)
                              if n.startswith(mct._PHASE2_SCRATCH_PREFIX)
                              or n.endswith(".abt")], [])


def _find_real_converter():
    """The aether_converter binary AETHER_BIN_DIR/PATH provides, or None."""
    import shutil
    bin_dir = os.environ.get("AETHER_BIN_DIR", "")
    if bin_dir:
        for name in ("aether_converter", "aether_converter.exe"):
            p = os.path.join(bin_dir, name)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return shutil.which("aether_converter")


def _surface(lat, lon, base):
    import numpy as np
    return (base + 200.0 * np.sin(lat * 7.0) * np.cos(lon * 5.0)
            + 30.0 * lat)


def _terrarium_png(zoom, x, y, base):
    """One 256² terrarium tile of :func:`_surface` (stdlib PNG encoder)."""
    import zlib
    import numpy as np
    n = 2.0 ** zoom
    j, i = np.mgrid[0:256, 0:256]
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * (y + (j + 0.5)
                                                          / 256) / n))))
    lon = (x + (i + 0.5) / 256) / n * 360.0 - 180.0
    v = _surface(lat, lon, base) + 32768.0
    rgb = np.dstack([np.floor(v / 256), np.floor(v) % 256,
                     np.floor((v - np.floor(v)) * 256)]).astype(np.uint8)
    raw = b"".join(b"\x00" + rgb[r].tobytes() for r in range(256))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 256, 256, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


class RealConverterEndToEnd(unittest.TestCase):
    """The shipped worker with the real aether_converter (AETHER_BIN_DIR),
    real GeoTIFFs and a local terrarium server. A and C are proven and come
    out complete in Phase 2; B is proven but its file has a no-data hole
    (second download pass); D has only slivers of files (downloaded next to
    Phase 2). Every output tile is byte for byte the one-shot build — one
    ingest of the whole stack over pool tiles fetched in ONE job — and a
    second run rebuilds nothing."""

    def test_byte_identical_to_the_one_shot_build(self):
        exe = _find_real_converter()
        if not exe:
            self.skipTest("aether_converter not provided via "
                          "AETHER_BIN_DIR or PATH")
        from _real_gdal import HAVE_GDAL, real_gdal, write_tif
        if not HAVE_GDAL:
            self.skipTest("needs the real GDAL")
        import functools
        import http.server
        import numpy as np
        from waveshed.core import binary_manager

        expected, n = _tiles()
        with tempfile.TemporaryDirectory() as root:
            tiles_dir = os.path.join(root, "tiles")
            for x in range(2137, 2153):             # z12 over 8..9 E
                os.makedirs(os.path.join(tiles_dir, "12", str(x)))
                for y in range(1423, 1449):          # z12 over 47..48 N
                    with open(os.path.join(tiles_dir, "12", str(x),
                                           f"{y}.png"), "wb") as fh:
                        fh.write(_terrarium_png(12, x, y, 900.0))

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

            # Files: 0.001 deg pixels (halo 0.004 deg), each 0.01 deg wider
            # than its tile; b has a no-data block inside B.
            dem = os.path.join(root, "dem")
            os.makedirs(dem)
            boxes = {"a": _b(7.99, 8.51, 46.99, 47.51),
                     "b": _b(8.49, 9.01, 46.99, 47.51),
                     "c": _b(7.99, 8.51, 47.49, 48.01)}
            paths = []
            with real_gdal():
                for name, box in boxes.items():
                    px = 520
                    r, c = np.mgrid[0:px, 0:px]
                    lat = box["north"] - (r + 0.5) * 0.001
                    lon = box["west"] + (c + 0.5) * 0.001
                    arr = _surface(lat, lon, 1000.0).astype(np.float32)
                    if name == "b":
                        arr[250:300, 200:260] = -9999.0
                    path = os.path.join(dem, f"{name}.tif")
                    write_tif(path, box, size=(px, px), array=arr,
                              nodata=-9999.0)
                    paths.append(path)
                # The infos the index would give these plain 4326 files
                # (halo = 4 source px).
                infos = [_info(p, boxes[os.path.basename(p)[0]],
                               fp=f"{os.path.getsize(p)}:1", halo_deg=0.004)
                         for p in paths]

                files_ent = {"kind": "files", "infos": infos}
                layers = [_LayerEntry(layer_type="raster", source_path=dem,
                                      extent=dict(_EXT)),
                          _LayerEntry(layer_type="raster", source_path=uri,
                                      extent=dict(_EXT))]

                def run_worker(out, cache):
                    resolved = {dem: {"kind": "files", "infos": infos},
                                uri: {"kind": "xyz", "uri": uri,
                                      "extent": dict(_EXT)}}
                    worker = _MapConverterWorker(
                        layers, out, [30], [dem, uri])
                    rec = {"status": [], "log": [], "err": [], "ok": [],
                           "pool": [], "launch": []}
                    worker.status.connect(rec["status"].append)
                    worker.log_line.connect(rec["log"].append)
                    worker.finished_err.connect(rec["err"].append)
                    worker.finished_ok.connect(rec["ok"].append)
                    real_pool = ta.ensure_pool_tiles
                    real_run = ta.run_converter_streaming

                    def pool(u, specs, res, **kw):
                        rec["pool"].append(sorted(t["filename"]
                                                  for t in specs))
                        return real_pool(u, specs, res, **kw)

                    def launch(exe_, args, on_line, **kw):
                        rec["launch"].append(args[0])
                        return real_run(exe_, args, on_line, **kw)

                    with mock.patch.object(
                            _MapConverterWorker, "_resolve_raster",
                            side_effect=lambda e: resolved[
                                e["source_path"]]), \
                         mock.patch.object(_MapConverterWorker,
                                           "_resolve_buildings",
                                           return_value=None), \
                         mock.patch.object(mct, "find_binary",
                                           return_value=exe), \
                         mock.patch.object(binary_manager, "find_binary",
                                           return_value=exe), \
                         mock.patch.object(ta, "get_cache_dir",
                                           return_value=cache), \
                         mock.patch.object(ta, "ensure_pool_tiles",
                                           side_effect=pool), \
                         mock.patch.object(ta, "run_converter_streaming",
                                           side_effect=launch):
                        worker.run()
                    return rec

                out = os.path.join(root, "out")
                cache = os.path.join(root, "cache")
                first = run_worker(out, cache)
                second = run_worker(out, cache)

                # The one-shot build: pool tiles of all four in ONE job,
                # one ingest of the whole stack.
                ref_out = os.path.join(root, "ref")
                os.makedirs(ref_out)
                with mock.patch.object(ta, "get_cache_dir",
                                       return_value=os.path.join(root, "rc")), \
                     mock.patch.object(binary_manager, "find_binary",
                                       return_value=exe):
                    pool = ta.ensure_pool_tiles(
                        uri, [t for _r, t in expected], 30)
                    jobs, _temps = mct._build_tile_jobs(
                        expected, ref_out, True,
                        [files_ent, {"kind": "xyz", "pool": {30: pool}}],
                        None, root)
                    job_file = os.path.join(root, "one_shot.json")
                    with open(job_file, "w") as fh:
                        json.dump(jobs, fh)
                    rc = ta.run_converter_streaming(
                        exe, ["ingest", "--job-file", job_file],
                        lambda _line: None)
            self.assertEqual(rc, 0)
            built = {name: open(os.path.join(out, name), "rb").read()
                     for name in n.values()}
            one_shot = {name: open(os.path.join(ref_out, name), "rb").read()
                        for name in n.values()}

        self.assertEqual(first["err"], [])
        self.assertEqual(first["ok"], [out])
        self.assertIn("Phase 2: 3 tiles from local files — 2 complete, "
                      "1 need the pool", first["log"])
        # D next to Phase 2, then the hole B: nothing else downloaded.
        self.assertEqual(first["pool"], [[n["D"]], [n["B"]]])
        # Phase 2's ingest and D's download (either starts first); then D's
        # band batch and the second pass for B, at the same time (either
        # starts first); then the holes' batch (2026-10-23: Phase 3 per
        # band, one ingest at a time).
        self.assertEqual(sorted(first["launch"][:2]), ["download", "ingest"])
        self.assertEqual(sorted(first["launch"][2:4]), ["download", "ingest"])
        self.assertEqual(first["launch"][4:], ["ingest"])
        for name in n.values():
            self.assertGreater(len(built[name]), 44, name)
            self.assertEqual(built[name], one_shot[name],
                             f"{name} differs from the one-shot build")
        self.assertEqual(second["err"], [])
        self.assertEqual(second["launch"], [])
        self.assertEqual(second["pool"], [])
        self.assertEqual(second["status"][-1], "All tiles already exist.")


class TwoPhaseProgress(_Case):

    def test_slots_and_the_download_correction(self):
        n = self.n
        calls = []
        real = mct._estimate_download_work

        def spy(model, entries, expected, **kw):
            calls.append(sorted(t["filename"] for _r, t in expected))
            return real(model, entries, expected, **kw)

        with mock.patch.object(mct, "_estimate_download_work",
                               side_effect=spy), \
             mock.patch.object(ta, "estimate_pool_download_units",
                               side_effect=lambda uri, specs, **kw:
                               float(len(specs))):
            r = self.run_(converter=_Converter(coverage={n["B"]: (1, 2)}))
        self.assertEqual(r.errs, [])
        # The download next to Phase 2 (the unproven D), then the second
        # pass over the hole Phase 2 found (B) — slots per (entry, zoom).
        self.assertEqual(calls, [[n["D"]], [n["B"]]])
        model = r.worker.progress_model
        p2_tiles = [t for _r, t in self.expected
                    if t["filename"] in (n["A"], n["B"], n["C"])]
        self.assertEqual(model.work(mct._CONVERT_FILES),
                         mct._convert_work(p2_tiles))
        self.assertEqual(model.work(("download", 0, 12)), 1.0)
        self.assertEqual(model.work(("download", 0, 12, 2)), 1.0)
        # Phase 3's work sits in one slot per batch (D's band, the hole B);
        # ("convert",) — the work no batch held yet — is empty at the end.
        batches = sorted(k for k in model.keys() if k[:1] == ("convert",)
                         and len(k) == 2 and isinstance(k[1], int))
        self.assertEqual(batches, [("convert", 1), ("convert", 2)])
        tile = {t["filename"]: t for _r, t in self.expected}
        self.assertEqual(sorted(model.work(k) for k in batches), sorted(
            [mct._convert_work([tile[n["D"]]]),
             mct._convert_work([tile[n["B"]]])]))
        self.assertEqual(model.work(("convert",)), 0.0)
        self.assertEqual(model.percent(), 100)


# ---------------------------------------------------------------------------
# Disk need: arithmetic, drives, refusal, re-checks, short files
# ---------------------------------------------------------------------------

class DiskArithmetic(unittest.TestCase):

    def test_a_2m_tile_is_62582828_bytes(self):
        expected = mct._enumerate_tiles(mct._snap_bbox(
            {"south": 47.0, "north": 47.1, "west": 8.0, "east": 8.1}, [2]), [2])
        self.assertEqual(len(expected), 1)
        tile = expected[0][1]
        self.assertEqual(tile["size_px"], 5556)
        self.assertEqual(ta._abt_whole_bytes(5556), 62_582_828)
        self.assertEqual(44 + 5556 * ((5556 * 2 + 255) & ~255), 62_582_828)
        with tempfile.TemporaryDirectory() as d:
            items = mct._disk_items(expected, [], os.path.join(d, "o"), d)
        self.assertEqual(items[0], (mct._NEED_OUTPUT, os.path.join(d, "o"),
                                    62_582_828))

    def test_pool_tiles_already_pooled_cost_nothing(self):
        expected, n = _tiles()
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(ta, "get_cache_dir", return_value=d):
            pool = ta._pool_dir(_XYZ)
            os.makedirs(pool)
            ra, ta_ = [rt for rt in expected if rt[1]["filename"] == n["A"]][0]
            with open(os.path.join(pool, n["A"]), "wb") as fh:
                fh.truncate(ta._abt_whole_bytes(ta_["size_px"]))
            items = mct._disk_items(
                expected, [{"kind": "xyz", "uri": _XYZ}], d, d,
                download=expected)
        pool_items = [i for i in items if i[0] == mct._NEED_POOL]
        self.assertEqual(pool_items, [(mct._NEED_POOL, pool,
                                       3 * ta._abt_whole_bytes(
                                           ta_["size_px"]))])

    def test_window_copies_of_materialized_files(self):
        expected, n = _tiles()
        px = 1.0 / 1024
        box = _b(8.0, 8.25, 47.0, 47.25)       # inside tile A
        plain = _info("/dem/plain.tif", box, px_x=px, px_y=px)
        zstd = _info("/dem/z.tif", box, px_x=px, px_y=px, dtype="Int16",
                     materialize="ZSTD compression")
        scaled = _info("/dem/s.tif", box, px_x=px, px_y=px, dtype="Int16",
                       materialize="scale/offset", unscale=True)
        a = [rt for rt in expected if rt[1]["filename"] == n["A"]]
        with tempfile.TemporaryDirectory() as d:
            def temp(infos):
                items = mct._disk_items(a, [{"kind": "files",
                                             "infos": infos}], d, d)
                return [i[2] for i in items if i[0] == mct._NEED_TEMP][0]
            self.assertEqual(temp([plain]), 0)
            self.assertEqual(temp([zstd]), 256 * 256 * 2)
            self.assertEqual(temp([scaled]), 256 * 256 * 4)   # Float32
            self.assertEqual(temp([zstd, scaled]), 256 * 256 * 6)


class DrivesAndRefusal(unittest.TestCase):

    @staticmethod
    def _volume(path):
        d = path.strip("/").split("/")[0]
        return d, f"{d.upper()}:"

    def test_items_group_per_drive(self):
        free = {"a": 50 * GB, "b": 3 * GB}
        needs = oe.drive_needs(
            [("output tiles", "/a/out", 10 * GB),
             ("terrain pool", "/b/pool", 2 * GB),
             ("temporary copies", "/a/tmp", 1 * GB)],
            free_of=lambda p: free[p.strip("/").split("/")[0]],
            volume=self._volume)
        self.assertEqual([(d.drive, d.labels, d.need, d.free) for d in needs],
                         [("A:", ("output tiles", "temporary copies"),
                           11 * GB, 50 * GB),
                          ("B:", ("terrain pool",), 2 * GB, 3 * GB)])
        # B: 2 GB needed, 3 GB free − 2 GB reserve = 1 GB → short.
        self.assertEqual([d.drive for d in oe.short_drives(needs)], ["B:"])
        self.assertEqual(oe.format_drive_need(needs[1]),
                         "B: need 2.00 GB / free 3.00 GB (terrain pool)")
        text = oe.refusal_text(oe.short_drives(needs), "this conversion")
        self.assertIn("Not enough disk space for this conversion", text)
        self.assertIn("B: need 2.00 GB / free 3.00 GB", text)
        self.assertNotIn("A:", text)

    def test_unknown_free_space_never_refuses(self):
        needs = oe.drive_needs([("output tiles", "/a/o", 10 ** 15)],
                               free_of=lambda _p: None, volume=self._volume)
        self.assertEqual(oe.short_drives(needs), [])

    def test_real_volumes_share_a_key(self):
        with tempfile.TemporaryDirectory() as d:
            a = oe.volume_of(os.path.join(d, "x", "not", "made"))
            b = oe.volume_of(d)
        self.assertEqual(a, b)


class WorkerDiskRechecks(_Case):

    def test_refused_before_the_download(self):
        conv = _Converter()
        r = self.run_(converter=conv, reports_tiles=False,
                      free={"v": 1 * GB})
        self.assertEqual(len(r.errs), 1)
        self.assertIn("Not enough disk space for the terrain download and "
                      "the remaining tiles", r.errs[0])
        self.assertIn("need", r.errs[0])
        self.assertEqual(r.asked, [])
        self.assertEqual(conv.calls, [])

    def test_refused_before_phase3_with_the_exact_remainder(self):
        conv = _Converter(coverage={self.n["B"]: (1, 2)})
        r = _Run(self, self.out, self.cache, converter=conv)

        def disk_fills_up(call_no, _specs, _kw):
            if call_no == 2:            # during the second pass
                r.free["v"] = 1 * GB

        r.on_pool = disk_fills_up
        r.run()
        self.assertEqual(len(r.errs), 1)
        self.assertIn("Not enough disk space for the remaining tiles",
                      r.errs[0])
        # Every Phase-3 batch is checked with its own tiles before it starts
        # (2026-10-23): D's band batch may have run before the disk filled
        # up; the hole batch (B), checked after its download, never runs.
        self.assertTrue(conv.calls[0]["phase2"])
        self.assertNotIn(self.n["B"], [os.path.basename(j["output_path"])
                                       for c in conv.calls[1:]
                                       for j in c["jobs"]])
        self.assertEqual(sorted(r.asked), sorted(self.n[k] for k in "BD"))

    def test_refused_before_the_second_download(self):
        conv = _Converter(coverage={self.n["B"]: (1, 2)})
        r = _Run(self, self.out, self.cache, converter=conv)
        conv.before_phase2 = _first_band_landed(r)

        def disk_fills_up(call_no, _specs, _kw):
            r.free["v"] = 1 * GB        # during the download next to Phase 2

        r.on_pool = disk_fills_up
        r.run()
        self.assertEqual(len(r.errs), 1)
        self.assertIn("Not enough disk space for the terrain download and "
                      "the remaining tiles", r.errs[0])
        self.assertEqual([c["phase2"] for c in conv.calls], [True])
        self.assertEqual(r.asked, [self.n["D"]])

    def test_refused_before_phase2(self):
        # Before anything starts: Phase 2's tiles, the download next to it
        # and the remaining tiles, in one need.
        conv = _Converter()
        r = self.run_(converter=conv, free={"v": 1 * GB})
        self.assertEqual(len(r.errs), 1)
        self.assertIn("Not enough disk space for the tiles built from local "
                      "files, the terrain download and the remaining tiles",
                      r.errs[0])
        self.assertEqual(conv.calls, [])
        self.assertEqual(r.asked, [])
        self.assertEqual(self.scratch_dirs(), [])


class ShortFilesAreRebuilt(_Case):

    def test_tiles_to_build_rebuilds_a_short_file(self):
        os.makedirs(self.out)
        n = self.n
        sizes = {t["filename"]: t["size_px"] for _r, t in self.expected}
        open(os.path.join(self.out, n["A"]), "w").close()          # empty
        with open(os.path.join(self.out, n["B"]), "wb") as fh:      # short
            fh.write(b"AETH" + b"\0" * 100)
        for k in "CD":
            with open(os.path.join(self.out, n[k]), "wb") as fh:
                fh.truncate(ta._abt_whole_bytes(sizes[n[k]]))
        pending, rebuild, unknown = mct._tiles_to_build(
            self.expected, self.out, False, {}, {})
        self.assertEqual(sorted(t["filename"] for _r, t in pending),
                         sorted([n["A"], n["B"]]))
        self.assertEqual(rebuild, set())

    def test_the_worker_rebuilds_a_short_tile(self):
        os.makedirs(self.out)
        n = self.n
        sizes = {t["filename"]: t["size_px"] for _r, t in self.expected}
        for k in "BCD":
            with open(os.path.join(self.out, n[k]), "wb") as fh:
                fh.truncate(ta._abt_whole_bytes(sizes[n[k]]))
        with open(os.path.join(self.out, n["A"]), "wb") as fh:
            fh.truncate(ta._abt_whole_bytes(sizes[n["A"]]) - 1)
        conv = _Converter()
        r = self.run_(converter=conv,
                      entries=[{"kind": "files", "infos": _files()}])
        self.assertEqual(r.errs, [])
        self.assertEqual([[os.path.basename(j["output_path"])
                           for j in c["jobs"]] for c in conv.calls],
                         [[n["A"]]])


# ---------------------------------------------------------------------------
# The tab: label and refusal
# ---------------------------------------------------------------------------

class _Text:
    text = ""

    def setText(self, text):
        self.text = text


def _tab(out_dir, layers):
    tab = mct.MapConverterTab.__new__(mct.MapConverterTab)
    tab._worker = None
    tab._run_reported = False
    tab._run_model = None
    tab._est_cache = {}
    tab._layers = layers
    tab._get_output_resolutions = lambda: [30]
    tab._preflight_folders = lambda: True
    tab._get_output_dir = lambda: out_dir
    tab._log = mock.Mock()
    tab._chk_overwrite = mock.Mock(isChecked=mock.Mock(return_value=False))
    tab._progress = mock.Mock(value=mock.Mock(return_value=0))
    tab._progress_timer = mock.Mock()
    tab._lbl_status = _Text()
    tab._lbl_estimate = _Text()
    return tab


def _xyz_layer():
    return _LayerEntry(layer_type="raster", source_path=_XYZ,
                       extent=dict(_EXT))


class TabDiskEstimate(unittest.TestCase):

    def test_estimate_prices_output_pool_and_raw_tiles_per_drive(self):
        expected, _n = _tiles()
        per_tile = ta._abt_whole_bytes(expected[0][1]["size_px"])
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(ta, "get_cache_dir", return_value=d):
            n_tiles, needs = mct._estimate_disk_need(
                [_xyz_layer()], [30], os.path.join(d, "out"), False,
                tmp_dir=d, cache={})
        self.assertEqual(n_tiles, 4)
        self.assertEqual(len(needs), 1)               # one volume here
        # 4 output + 4 pool tiles + the raw source tiles their z12 download
        # adds to <cache>/xyz: 216 of them (aether_converter 0.2.7 for these
        # four specs: "[Download] z=12 tiles=12x18=216"), 200 KiB each.
        self.assertEqual(needs[0].need,
                         8 * per_tile + 216 * ta.XYZ_SOURCE_TILE_BYTES)
        self.assertEqual(set(needs[0].labels),
                         {mct._NEED_OUTPUT, mct._NEED_POOL, mct._NEED_RAW,
                          mct._NEED_TEMP})

    def test_label_shows_need_and_free_red_when_short(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(ta, "get_cache_dir", return_value=d):
            tab = _tab(os.path.join(d, "out"), [_xyz_layer()])
            with mock.patch.object(oe, "free_bytes", return_value=_HUGE):
                tab._update_estimate()
                tab.flush_estimate()     # the estimate thread's work, inline
            self.assertIn("need ", tab._lbl_estimate.text)
            self.assertIn(" / free ", tab._lbl_estimate.text)
            self.assertNotIn("color:red", tab._lbl_estimate.text)
            with mock.patch.object(oe, "free_bytes", return_value=1 * GB):
                tab._update_estimate()
                tab.flush_estimate()
            self.assertIn("color:red", tab._lbl_estimate.text)

    def test_on_run_refuses_with_the_same_numbers(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(ta, "get_cache_dir", return_value=d):
            tab = _tab(os.path.join(d, "out"), [_xyz_layer()])
            box = mock.Mock()
            with mock.patch.object(oe, "free_bytes", return_value=1 * GB), \
                 mock.patch.object(mct, "QMessageBox", box), \
                 mock.patch.object(mct, "resolve_sources_with_progress") \
                    as resolve:
                tab._on_run()
                tab._update_estimate()
                tab.flush_estimate()
        box.critical.assert_called_once()
        title, msg = box.critical.call_args[0][1:3]
        self.assertEqual(title, "Not enough disk space")
        self.assertIn("Not enough disk space for this conversion", msg)
        resolve.assert_not_called()
        self.assertIsNone(tab._worker)
        # The dialog's numbers are the label's.
        line = [ln for ln in msg.splitlines() if "need" in ln][0].strip()
        self.assertIn(line, tab._lbl_estimate.text)


# ---------------------------------------------------------------------------
# Site Analysis: coverage rasters + pool, per drive
# ---------------------------------------------------------------------------

class SiteAnalysisDiskRefusal(unittest.TestCase):

    def _jobs(self, model="MIN_ALT", rng=100.0, res=10):
        p = mock.Mock(model=model, max_range_km=rng, resolution_m=res,
                      az_start=0.0, az_end=360.0)
        return [(p, "Site 1")]

    def test_raster_and_pool_on_one_drive_add_up(self):
        from waveshed.gui import site_analysis_tab as sat
        raster = oe.engine_output_bytes("MIN_ALT", 100.0, 10)
        pool_mb = 1024
        with tempfile.TemporaryDirectory() as d:
            fits = raster + pool_mb * 1024 ** 2 + oe.DISK_RESERVE_BYTES
            self.assertIsNone(sat._disk_refusal(
                self._jobs(), pool_mb, d, d, free_of=lambda _p: fits))
            msg = sat._disk_refusal(self._jobs(), pool_mb, d, d,
                                    free_of=lambda _p: fits - 1)
        self.assertIn("Not enough disk space for this analysis", msg)
        self.assertIn("coverage rasters, terrain pool", msg)

    def test_separate_drives_are_judged_separately(self):
        from waveshed.gui import site_analysis_tab as sat
        free = {"out": 100 * GB, "cache": 2 * GB}
        with mock.patch.object(oe, "volume_of",
                               side_effect=lambda p: (os.path.basename(p),
                                                      os.path.basename(p))):
            msg = sat._disk_refusal(self._jobs(), 500, "/x/out", "/x/cache",
                                    free_of=lambda p: free[
                                        os.path.basename(p)])
        self.assertIn("cache need", msg)
        self.assertIn("(terrain pool)", msg)
        self.assertNotIn("coverage rasters", msg)

    def test_a_raster_that_does_not_fit_names_a_resolution_that_would(self):
        from waveshed.gui import site_analysis_tab as sat
        with tempfile.TemporaryDirectory() as d:
            msg = sat._disk_refusal(self._jobs("MIN_ALT", 500.0, 2), 0, d, d,
                                    free_of=lambda _p: 73 * GB)
        self.assertIn("5 m pixels would fit for Site 1", msg)


if __name__ == "__main__":
    unittest.main()
