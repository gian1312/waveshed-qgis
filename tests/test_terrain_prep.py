"""One terrain preparation per Site Analysis run (2026-10-23).

The owner's Run on a 43,650-file swissALTI3D folder checked the folder index
twice (the GUI's scan thread, then the worker after ``expire_all``), built
the source infos three times (the coverage check's own list, the plan's
``folder_source_infos``, the worker's) and a FootprintIndex twice before the
converter started. Now ``site_analysis_tab._terrain_dir_checks`` makes ONE
``terrain_adapter.TerrainPrep`` in the coverage check's thread and the
worker's ``prepare_terrain`` / ``prepare_terrain_profile`` use it while it
is current. Pinned here:

* one Run (two sites, one of them at two altitudes, and an adaptive site)
  = ONE folder enumeration, ONE source-info build, ONE FootprintIndex (the
  builders counted), the lines "local dir", "overlap priority" and the
  folder-index line once; without the bundle the worker reads the folder
  itself again (what the counters would show before);
* the jobs are the ones reading the folder gives — fixed and profile jobs
  with and without the bundle write the same ingest jobs (``sources[]``
  order, materialized-copy names), and the coverage warning and the plan
  numbers are the same;
* ``SubsetFootprintIndex`` answers as a FootprintIndex built over the
  sub-list, element for element;
* a stale bundle (older than ``FRESH_S``, files added, the index changed,
  another folder, the overlap priority or the answers changed) is ignored:
  the folder is read as before;
* callers without a bundle (Processing, P2P, the torture runner) read the
  folder as before.
"""

# Bootstrap stubs before any plugin import.
import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import collections
import json
import os
import random
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np

import waveshed.core.terrain_adapter as ta
import waveshed.gui.site_analysis_tab as sat
from _real_gdal import HAVE_GDAL, real_gdal, write_tif
from test_footprint_index import _random_layout, _random_tiles
from waveshed.core import binary_manager as bm
from waveshed.core import range_profile as rp
from waveshed.core import terrain_index as ti
from waveshed.core.job_builder import CoverageParams
from waveshed.gui import terrain_folder_scan as tfs

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: Scratch inside the project's gitignored .tmp (never the container drive).
_SCRATCH = os.path.join(_REPO, ".tmp", "test_terrain_prep")

#: Bern (LV95 2,600,000 / 1,200,000) and a second site ~7 km south-east.
SITE_A = (46.9511, 7.4386)
SITE_B = (46.90, 7.50)
RANGE_KM = 8.0
RES = 90


def _scratch_dir(test: unittest.TestCase) -> str:
    os.makedirs(_SCRATCH, exist_ok=True)
    d = tempfile.mkdtemp(prefix="t_", dir=_SCRATCH)
    test.addCleanup(shutil.rmtree, d, True)
    return d


def _tif(folder, name, box, value, px=100, **kw):
    """One LV95 GeoTIFF over *box* (west, east, south, north metres)."""
    w, e, s, n = box
    path = os.path.join(folder, name)
    write_tif(path, {"west": w, "east": e, "south": s, "north": n},
              epsg=2056, size=(px, px),
              array=np.full((px, px), value, np.float32), **kw)
    return path


#: The module's folder (made once: 83 GeoTIFFs cost ~3 s on the Windows
#: mount). Tests treat it as read-only or undo what they change.
_SHARED: dict = {}


def _shared_folder() -> str:
    """9 x 9 files of 4 km at 40 m around Bern, one scaled file over the
    centre (materialize-routed: its window copy is named by its position in
    the job's file list) and one far file east of every tile the sites
    need (so the files near the tiles are a strict sub-list) — 83 files,
    above ``_infos_near``'s 64."""
    dem = _SHARED.get("dem")
    if dem is None:
        os.makedirs(_SCRATCH, exist_ok=True)
        _SHARED["root"] = root = tempfile.mkdtemp(prefix="dem_", dir=_SCRATCH)
        dem = os.path.join(root, "dem")
        os.makedirs(dem)
        for i in range(9):
            for j in range(9):
                e, n = 2_582_000 + 4000 * i, 1_182_000 + 4000 * j
                _tif(dem, f"g_{e // 1000}_{n // 1000}.tif",
                     (e, e + 4000, n, n + 4000), 500.0 + i + j)
        _tif(dem, "scaled.tif", (2_596_000, 2_604_000, 1_196_000, 1_204_000),
             900.0, px=200, scale=0.5)
        _tif(dem, "far.tif", (2_700_000, 2_704_000, 1_200_000, 1_204_000),
             700.0)
        _SHARED["dem"] = dem
    return dem


def tearDownModule():
    root = _SHARED.pop("root", None)
    _SHARED.pop("dem", None)
    if root:
        shutil.rmtree(root, True)


def _model_tile(job):
    """A whole .abt at the job's output path (header + zero rows) — enough
    for the pool and the views; the pixels are not under test here."""
    size = int(job["size_px"])
    stride = (size * 2 + 255) & ~255
    res_deg = job["resolution_m"] / 111_111.0
    with open(job["output_path"], "wb") as fh:
        fh.write(b"AETH" + struct.pack("<HH", 1, size)
                 + struct.pack("<dddd", job["ul_lat"], job["ul_lon"],
                               res_deg, res_deg)
                 + struct.pack("<hH", 0, stride))
        fh.truncate(44 + stride * size)


class _Engine:
    """``run_converter_streaming`` stand-in: an ingest records its jobs and
    writes whole model tiles; aether_core (``--config``) exits 0."""

    def __init__(self):
        self.batches = []

    def __call__(self, exe, args, on_line, on_start=None, env=None, **_kw):
        if "--job-file" in args:
            with open(args[args.index("--job-file") + 1]) as fh:
                jobs = json.load(fh)
            self.batches.append(jobs)
            for job in jobs:
                _model_tile(job)
            on_line(f"Progress: {len(jobs)}/{len(jobs)}")
        return 0

    def jobs(self):
        """Every ingest job, output path reduced to its name (the scratch
        directory around it is random)."""
        out = []
        for batch in self.batches:
            for job in batch:
                job = dict(job)
                job["output_path"] = os.path.basename(job["output_path"])
                out.append(job)
        return out


class _Counted:
    """Counts the three builders a run must not repeat: the folder
    enumeration (``terrain_index._walk_stat``), the source-info build
    (``terrain_index.source_infos``) and ``FootprintIndex.__init__``."""

    def __init__(self):
        self.n = collections.Counter()
        real_walk, real_infos = ti._walk_stat, ti.source_infos
        real_init = ta.FootprintIndex.__init__

        def walk(*a, **k):
            self.n["walk"] += 1
            return real_walk(*a, **k)

        def infos(*a, **k):
            self.n["source_infos"] += 1
            return real_infos(*a, **k)

        def init(index, *a, **k):
            self.n["footprint_index"] += 1
            real_init(index, *a, **k)

        self._patches = [mock.patch.object(ti, "_walk_stat", walk),
                         mock.patch.object(ti, "source_infos", infos),
                         mock.patch.object(ta.FootprintIndex, "__init__",
                                           init)]

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()


@unittest.skipUnless(HAVE_GDAL, "needs the real GDAL")
class _FolderCase(unittest.TestCase):
    """The real LV95 folder of :func:`_shared_folder`, a fresh cache (index,
    pool, views) per test."""

    def setUp(self):
        self.root = _scratch_dir(self)
        self.cache = os.path.join(self.root, "cache")
        p = mock.patch.object(ta, "get_cache_dir",
                              side_effect=lambda: self.cache)
        p.start()
        self.addCleanup(p.stop)
        self._gdal = real_gdal()
        self._gdal.__enter__()
        self.addCleanup(self._gdal.__exit__, None, None, None)
        ti.forget_indexes()
        ti.clear_approvals()
        self.addCleanup(ti.forget_indexes)
        self.addCleanup(ti.clear_approvals)
        self.dem = _shared_folder()
        self.engine = _Engine()

    def _bm(self):
        b = mock.Mock()
        b.find_binary.return_value = "aether_converter"
        return b

    def _scan(self):
        """The Run click's index check (the GUI's scan thread)."""
        return ti.get_index(self.dem).refresh()

    def _clear_pool(self):
        for sub in ("pool", "views"):
            shutil.rmtree(os.path.join(self.cache, sub), ignore_errors=True)
        self.engine.batches.clear()

    def _fixed(self, prep, site=SITE_A, log=None):
        params = CoverageParams(tx_lat=site[0], tx_lon=site[1],
                                resolution_m=RES, max_range_km=RANGE_KM)
        with mock.patch.object(ta, "run_converter_streaming",
                               side_effect=self.engine), \
             mock.patch.object(ta, "_log",
                               side_effect=(log.append if log is not None
                                            else lambda _m: None)):
            return ta.prepare_terrain(
                None, site[0], site[1], RANGE_KM, RES, self._bm(),
                terrain_dir=self.dem,
                region_union=ta.run_region_union([params]),
                terrain_prep=prep)

    def _profile(self, prep, bands, log=None):
        params = CoverageParams(tx_lat=SITE_A[0], tx_lon=SITE_A[1],
                                resolution_m=bands[0][1],
                                max_range_km=bands[-1][0] / 1000.0,
                                range_profile=bands)
        with mock.patch.object(ta, "run_converter_streaming",
                               side_effect=self.engine), \
             mock.patch.object(ta, "_log",
                               side_effect=(log.append if log is not None
                                            else lambda _m: None)):
            return ta.prepare_terrain_profile(
                None, SITE_A[0], SITE_A[1], bands, self._bm(),
                terrain_dir=self.dem, output_res_m=bands[0][1],
                region_union=ta.run_region_union([params]),
                terrain_prep=prep)


# ---------------------------------------------------------------------------
# The jobs are the ones reading the folder gives
# ---------------------------------------------------------------------------

class SameJobsWithAndWithoutTheBundle(_FolderCase):

    def test_fixed_job(self):
        view_a = self._fixed(None)
        jobs_a = self.engine.jobs()
        self._clear_pool()
        self._scan()
        prep = ta.build_terrain_prep(self.dem)
        log = []
        with _Counted() as c, \
             mock.patch.object(ta, "SubsetFootprintIndex",
                               wraps=ta.SubsetFootprintIndex) as subset:
            view_b = self._fixed(prep, log=log)
        jobs_b = self.engine.jobs()
        # The files near the tiles are a strict sub-list (far.tif is not
        # near), matched through the bundle's index.
        self.assertEqual(subset.call_count, 1)
        self.assertLess(len(subset.call_args.args[1]), len(prep.infos))
        self.assertTrue(jobs_a)
        self.assertEqual(jobs_b, jobs_a)
        self.assertEqual(os.path.basename(view_b), os.path.basename(view_a))
        paths = [s["path"] for j in jobs_a for s in j["sources"]]
        self.assertTrue(any("aether_src_" in p for p in paths),
                        "the scaled file reaches the job as a window copy")
        self.assertFalse(any(p.endswith("far.tif") for p in paths))
        # No index check, no source-info build, no FootprintIndex.
        self.assertEqual(dict(c.n), {})
        self.assertTrue(any("from this run's terrain preparation" in m
                            for m in log), log)
        self.assertFalse(any("local dir" in m or "overlap priority" in m
                             for m in log), log)

    def test_profile_job(self):
        bands = rp.adaptive_bands(30, 15)
        view_a = self._profile(None, bands)
        jobs_a = self.engine.jobs()
        self._clear_pool()
        self._scan()
        prep = ta.build_terrain_prep(self.dem)
        with _Counted() as c:
            view_b = self._profile(prep, bands)
        jobs_b = self.engine.jobs()
        self.assertGreater(len(self.engine.batches), 1, "every band builds")
        self.assertEqual(jobs_b, jobs_a)
        self.assertEqual(os.path.basename(view_b), os.path.basename(view_a))
        self.assertEqual(dict(c.n), {})

    def test_coverage_warning_and_plan_numbers(self):
        """``_terrain_dir_checks`` (bundle) against the two reads it
        replaced: the coverage check's own list and the plan's
        ``folder_source_infos`` + FootprintIndex — site B reaches past the
        folder, so there IS a warning to compare."""
        jobs = [CoverageParams(tx_lat=s[0], tx_lon=s[1], resolution_m=RES,
                               max_range_km=r)
                for s, r in ((SITE_A, RANGE_KM), (SITE_B, 15.0))]
        union = ta._union_bbox([ta.analysis_bbox(p.tx_lat, p.tx_lon,
                                                 p.max_range_km)
                                for p in jobs])
        region_union = ta.run_region_union(jobs)
        self._scan()
        cov, priced, subs, bstate, prep = sat._terrain_dir_checks(
            self.dem, union, RES, jobs, False, self.dem, None, region_union)
        self.assertIsInstance(prep, ta.TerrainPrep)
        self.assertEqual((subs, bstate), ([], None))
        self.assertIs(prep.coverage, cov)
        self.assertIs(prep.plan, priced)
        self.assertIn("reaches only about", cov or "")
        want_cov = ta.terrain_coverage_warning(self.dem, union,
                                               resolution_m=RES)
        infos = ta.folder_source_infos(self.dem)
        want_plan = sat._plan_jobs(jobs, self.dem, None, region_union,
                                   (infos, False, ta.FootprintIndex(infos)))
        self.assertEqual(cov, want_cov)
        self.assertEqual(priced, want_plan)
        self.assertGreater(priced["source_files"], 0)
        # The bundle's list is the one a run reads, element for element.
        self.assertEqual(prep.infos, infos)


class SubsetIndexIsTheSubListsIndex(unittest.TestCase):

    def test_random_layouts(self):
        hits = 0
        for seed in range(40):
            rng = random.Random(seed)
            infos, lon, lat = _random_layout(rng, rng.randint(10, 120))
            full = ta.FootprintIndex(infos)
            sub = [i for i in infos if rng.random() < 0.6]
            subset = ta.SubsetFootprintIndex(full, sub)
            own = ta.FootprintIndex(sub)
            for tb, res in _random_tiles(rng, lon, lat, 40):
                got = ta._tile_contributors(sub, tb, res, index=subset)
                want = ta._tile_contributors(sub, tb, res, index=own)
                self.assertEqual([(i, id(f), h) for i, f, h in got],
                                 [(i, id(f), h) for i, f, h in want])
                hits += bool(want)
        self.assertGreater(hits, 300, "the layouts must actually hit files")

    def test_bound_to_its_own_list(self):
        infos = [{"path": "a", "halo_deg": 0.001,
                  "wgs84_bounds": {"west": 7, "east": 8, "south": 46,
                                   "north": 47}}]
        subset = ta.SubsetFootprintIndex(ta.FootprintIndex(infos), infos[:])
        with self.assertRaises(ValueError):
            subset.contributors(infos, infos[0]["wgs84_bounds"], 90.0)

    def test_index_for(self):
        infos = [{"path": "a", "halo_deg": 0.001,
                  "wgs84_bounds": {"west": 7, "east": 8, "south": 46,
                                   "north": 47}}]
        full = ta.FootprintIndex(infos)
        prep = ta.TerrainPrep("/f", None, 0, time.time(), 1, "finest", {},
                              infos, full)
        self.assertIs(prep.index_for(infos), full)
        self.assertIsNone(prep.index_for([]))
        self.assertIsInstance(prep.index_for(infos[:]),
                              ta.SubsetFootprintIndex)


# ---------------------------------------------------------------------------
# A stale bundle is ignored; callers without one read the folder as before
# ---------------------------------------------------------------------------

class StaleBundleIsIgnored(_FolderCase):

    def _prep(self):
        self._scan()
        return ta.build_terrain_prep(self.dem)

    def _reads_again(self, prep, why):
        self.assertIn(why, prep.stale_reason(self.dem) or "")
        log = []
        with _Counted() as c:
            self._fixed(prep, log=log)
        self.assertEqual(c.n["source_infos"], 1, "the folder is read again")
        self.assertEqual(c.n["footprint_index"], 1)
        self.assertTrue(any("not reused" in m and why in m for m in log), log)
        self.assertTrue(any("local dir" in m for m in log), log)
        return c

    def test_current_bundle(self):
        self.assertIsNone(self._prep().stale_reason(self.dem))

    def test_older_than_fresh_s(self):
        prep = self._prep()
        prep.checked_at -= ti.FRESH_S + 1.0
        ti.expire_all()                      # as the worker's run start
        c = self._reads_again(prep, "checked")
        self.assertEqual(c.n["walk"], 1, "the index is checked again")

    def test_a_file_added(self):
        prep = self._prep()
        before = ti.dir_mtime_ns(self.dem)
        new = _tif(self.dem, "new.tif",
                   (2_598_000, 2_602_000, 1_198_000, 1_202_000), 1234.0,
                   px=400)
        self.addCleanup(os.remove, new)
        if ti.dir_mtime_ns(self.dem) == before:   # a coarse-mtime volume
            os.utime(self.dem, ns=(before + 10 ** 9, before + 10 ** 9))
        ti.expire_all()
        self._reads_again(prep, "added, removed or renamed")
        paths = [s["path"] for j in self.engine.jobs() for s in j["sources"]]
        self.assertIn(new, paths)

    def test_the_index_changed(self):
        prep = self._prep()
        p = os.path.join(self.dem, "g_2598_1198.tif")
        st = os.stat(p)
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
        self.addCleanup(os.utime, p, ns=(st.st_atime_ns, st.st_mtime_ns))
        ti.get_index(self.dem).refresh()          # e.g. another tab's scan
        self._reads_again(prep, "index changed")

    def test_another_folder(self):
        prep = self._prep()
        for other in (self.root, None):
            self.assertEqual(prep.stale_reason(other),
                             "another terrain folder")

    def test_overlap_priority_changed(self):
        prep = self._prep()
        ti.set_overlap_priority(self.dem, ti.PRIORITY_NAME)
        self.addCleanup(ti.set_overlap_priority, self.dem, ti.PRIORITY_FINEST)
        self._reads_again(prep, "overlap priority")

    def test_answers_changed(self):
        prep = self._prep()
        ti.approve(self.dem, {os.path.join(self.dem, "far.tif"): "skip"})
        self.assertIn("answers", prep.stale_reason(self.dem))


class WithoutABundleAsBefore(_FolderCase):
    """Processing, P2P and the torture runner pass no bundle: the folder is
    read per call, the near files get their own FootprintIndex."""

    def test_prepare_terrain_reads_the_folder(self):
        log = []
        with _Counted() as c, \
             mock.patch.object(ta, "tile_source_fingerprint",
                               wraps=ta.tile_source_fingerprint) as fp:
            self._fixed(None, log=log)
        self.assertEqual(c.n["source_infos"], 1)
        self.assertEqual(c.n["footprint_index"], 1)
        self.assertIs(type(fp.call_args.kwargs["index"]), ta.FootprintIndex)
        self.assertTrue(any("local dir" in m for m in log))
        self.assertFalse(any("this run's terrain preparation" in m
                             for m in log))

    def test_callers_without_a_bundle(self):
        """Their sources never name one — the default (None) applies."""
        for rel in ("waveshed/algorithms/coverage.py",
                    "waveshed/algorithms/p2p.py", "waveshed/gui/p2p_tab.py",
                    "tools/torture_runner.py"):
            with open(os.path.join(_REPO, rel), encoding="utf-8") as fh:
                self.assertNotIn("terrain_prep", fh.read(), rel)


# ---------------------------------------------------------------------------
# One Run: one enumeration, one source-info build, one FootprintIndex
# ---------------------------------------------------------------------------

class OneRunPreparesOnce(_FolderCase):
    """``_on_run`` (a stand-in tab, the real scan thread's work, the real
    ``_terrain_dir_checks`` and ``_launch_worker``) then the real worker
    over four jobs: site A at two altitudes, site B, and an adaptive site."""

    def _jobs(self):
        out = []
        for k, (site, alt) in enumerate(((SITE_A, 1.5), (SITE_A, 10.0),
                                         (SITE_B, 1.5))):
            out.append((CoverageParams(tx_lat=site[0], tx_lon=site[1],
                                       resolution_m=RES,
                                       max_range_km=RANGE_KM, rx_height=alt,
                                       model="LOS", output_name=f"j{k}"),
                        f"Job {k}"))
        out.append((CoverageParams(tx_lat=SITE_A[0], tx_lon=SITE_A[1],
                                   resolution_m=30, max_range_km=15,
                                   range_profile=rp.adaptive_bands(30, 15),
                                   model="LOS", output_name="j3"), "Job 3"))
        return out

    def _tab(self, jobs):
        tab = sat.SiteAnalysisTab.__new__(sat.SiteAnalysisTab)
        tab._worker = None
        tab.sites_table = mock.MagicMock()
        tab.sites_table.rowCount.return_value = 1
        tab.alt_table = mock.MagicMock()
        tab.alt_table.rowCount.return_value = 2
        tab._read_sites = lambda: [{"coords": SITE_A}]
        tab._selected_dem_layer = lambda: None
        tab._selected_terrain_dir = lambda: self.dem
        tab.line_output_dir = mock.Mock(text=mock.Mock(
            return_value=os.path.join(self.root, "out")))
        tab._refresh_asset_cache = lambda: None
        tab._build_jobs = lambda: jobs
        tab.chk_buildings = mock.Mock(isChecked=mock.Mock(return_value=False))
        for name in ("btn_run", "btn_stop", "progress_bar", "status_label"):
            setattr(tab, name, mock.Mock())
        return tab

    @staticmethod
    def _scan_folder(_parent, folder, after=None, title="", refresh=True):
        """``scan_folder`` without the dialog: the same thread, the same
        work (``FolderScanThread.run``), the same log line."""
        th = tfs.FolderScanThread(folder, after, None, refresh=refresh)
        runner = threading.Thread(target=th.run)
        runner.start()
        runner.join()
        if th.error is not None:
            raise RuntimeError(th.error)
        st = th.result["stats"]
        if st is not None:
            tfs._log(f"terrain folder index {folder}: {st.files} file(s) — "
                     f"{st.new} new, {st.changed} changed")
        return th.result

    def _run(self, hand_over=True):
        jobs = self._jobs()
        tab = self._tab(jobs)
        box = mock.Mock()
        box.StandardButton = sat.QMessageBox.StandardButton
        box.warning.return_value = box.StandardButton.Yes
        logged, results, errors, preps = [], [], [], []
        real_prepare = sat.prepare_terrain
        real_profile = sat.prepare_terrain_profile

        def prepare(**kw):
            preps.append(kw.get("terrain_prep"))
            return real_prepare(**kw)

        def profile(**kw):
            preps.append(kw.get("terrain_prep"))
            return real_profile(**kw)

        with mock.patch.object(sat, "dem_layer_warning", return_value=None), \
             mock.patch.object(sat, "save_output_dir"), \
             mock.patch.object(sat, "binaries_warning", return_value=None), \
             mock.patch.object(sat, "output_space_problem",
                               return_value=(None, False)), \
             mock.patch.object(sat, "_disk_refusal", return_value=None), \
             mock.patch.object(sat, "QMessageBox", box), \
             mock.patch.object(sat.terrain_folder_scan, "scan_folder",
                               side_effect=self._scan_folder), \
             mock.patch.object(sat.QgsMessageLog, "logMessage",
                               side_effect=lambda m, *a, **k:
                               logged.append(m)), \
             mock.patch.object(sat, "prepare_terrain", side_effect=prepare), \
             mock.patch.object(sat, "prepare_terrain_profile",
                               side_effect=profile), \
             mock.patch.object(ta, "run_converter_streaming",
                               side_effect=self.engine), \
             mock.patch.object(bm, "check_engine_for_job",
                               return_value="0.4.10"), \
             mock.patch.object(bm, "find_binary",
                               side_effect=lambda name: name), \
             mock.patch.object(bm, "engine_exported_tif", return_value=True), \
             mock.patch.object(sat, "find_binary",
                               side_effect=lambda name: name), \
             mock.patch.object(sat.api_key, "apply_license_env"), \
             _Counted() as c:
            tab._on_run()
            worker = tab._worker
            self.assertIsNotNone(worker, "the run was launched")
            if not hand_over:          # the worker before the bundle
                worker.terrain_prep = None
            worker.finished_ok.disconnect()
            worker.finished_err.disconnect()
            worker.finished_ok.connect(results.append)
            worker.finished_err.connect(errors.append)
            worker.run()
        # The finished worker stays on the tab; the bundle does not.
        self.assertIsNone(worker.terrain_prep)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(len(results[0]), len(jobs))
        return c, logged, preps

    def test_one_preparation_per_run(self):
        c, logged, preps = self._run()
        self.assertEqual(c.n["walk"], 1, "one index check (the Run click's)")
        self.assertEqual(c.n["source_infos"], 1, "one source-info build")
        self.assertEqual(c.n["footprint_index"], 1, "one FootprintIndex")
        count = {k: sum(k in m for m in logged)
                 for k in ("local dir", "overlap priority", "folder index")}
        self.assertEqual(count, {"local dir": 1, "overlap priority": 1,
                                 "folder index": 1}, logged)
        self.assertEqual(len(preps), 4)
        self.assertIsInstance(preps[0], ta.TerrainPrep)
        self.assertTrue(all(p is preps[0] for p in preps),
                        "every job shares the run's bundle")
        self.assertTrue(self.engine.batches, "terrain was built")

    def test_without_the_hand_over_the_worker_reads_again(self):
        """What the counters show for a worker that reads the folder
        itself (the torture runner's, Processing's way): a second check
        and its own source infos and FootprintIndex."""
        c, logged, preps = self._run(hand_over=False)
        self.assertEqual(preps, [None] * 4)
        self.assertEqual(c.n["walk"], 2)
        self.assertGreaterEqual(c.n["source_infos"], 2)
        self.assertGreaterEqual(c.n["footprint_index"], 2)


class FinishedThreadsKeepNoBundle(unittest.TestCase):
    """The scan thread lives on under its Qt parent (the tab): what
    *after* returned — the run's TerrainPrep — is handed out, not kept."""

    def test_scan_folder_drops_the_threads_result(self):
        made = []

        class Recording(tfs.FolderScanThread):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                made.append(self)

        qtcore = sys.modules["qgis.PyQt.QtCore"]
        with mock.patch.object(tfs, "FolderScanThread", Recording), \
             mock.patch.object(Recording, "start", lambda th: th.run()), \
             mock.patch.object(tfs, "QProgressDialog",
                               return_value=mock.MagicMock()), \
             mock.patch.object(qtcore, "QEventLoop", mock.MagicMock(),
                               create=True), \
             mock.patch.object(ti, "get_index", return_value=mock.Mock()):
            result = tfs.scan_folder(None, "/folder", refresh=False,
                                     after=lambda _index: "the bundle")
        self.assertEqual(result["extra"], "the bundle")
        self.assertEqual(len(made), 1)
        self.assertIsNone(made[0].result)
        self.assertIsNone(made[0]._after)


if __name__ == "__main__":
    unittest.main()
