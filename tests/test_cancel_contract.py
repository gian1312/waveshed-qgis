"""Stop / Cancel contract (2026-10-20).

Stop used to do nothing until the terrain step ended: the Site Analysis
worker could only kill the aether_core process, the converter prints ingest
progress only every 10 tiles (so polling output lines cannot stop it), and
the Map Converter left Convert disabled after a Cancel. Pinned here:

* ``prepare_terrain`` / ``prepare_terrain_profile`` take ``should_cancel``
  (polled per tile, band, folder-index file and output line) and ``on_start``
  (every engine process — download pass, ingest — handed to the caller so its
  cancel() can kill it). A stop is always ``ConverterCancelled``; scratch
  files are still removed.
* Site Analysis and P2P: the worker registers those processes (one started
  after Stop is killed on registration), a stopped run ends without an
  error, and Stop never blocks the GUI thread — the run UI comes back only
  on ``QThread.finished``; the kill itself waits for nothing (Windows: the
  handle, then a taskkill nobody on the GUI thread waits for).
* Map Converter: a cancelled run gives Convert back on ``QThread.finished``
  (also after a done run), kills an engine that starts after Cancel, removes
  its %TEMP% job file, and its per-tile loops poll the flag.

The end-to-end tests run the real shared runner on a real child process
that prints one line and then goes quiet, exactly like an ingest between two
progress reports.
"""

# Bootstrap stubs before any plugin import.
import conftest  # noqa: F401

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import waveshed.core.terrain_adapter as ta
from waveshed.core import binary_manager as bm
from waveshed.core import range_profile as rp
from waveshed.core.job_builder import CoverageParams, P2PParams
import waveshed.gui.map_converter_tab as mct
import waveshed.gui.p2p_tab as p2p_tab
import waveshed.gui.site_analysis_tab as sat
from waveshed.gui.map_converter_tab import (
    MapConverterTab, _LayerEntry, _MapConverterWorker,
)
from waveshed.gui.site_analysis_tab import SiteAnalysisTab


_WMS = "contextualWMSLegend=0&crs=EPSG:4326&url=https://wms.example/x"
_XYZ = ("type=xyz&url=https%3A//example.com/%7Bz%7D/%7Bx%7D/%7By%7D.png"
        "&zmax=15")

# A stand-in engine: one line, then silence — an ingest between two of its
# every-10-tiles progress reports. Only a kill ends it early.
_QUIET_ENGINE = ("import time; print('[Ingest] started', flush=True); "
                 "time.sleep(30)")

_REAL_RUNNER = ta.run_converter_streaming


def _wms_layer():
    layer = mock.Mock()
    layer.source.return_value = _WMS
    layer.crs.return_value = conftest._FakeCRS("EPSG:4326")
    return layer


def _xyz_layer():
    layer = mock.Mock()
    layer.name.return_value = "terrarium"
    layer.source.return_value = _XYZ
    return layer


def _converter_bm():
    manager = mock.Mock()
    manager.find_binary.return_value = "aether_converter"
    return manager


def _fake_export(dem_layer, dest, bbox, resolution_m):
    with open(dest, "wb") as fh:
        fh.write(b"\0" * 8)


def _never():
    return False


def _no_start(_proc):
    return None


class _Button:
    def __init__(self):
        self.enabled = True
        self.visible = True

    def setEnabled(self, on):
        self.enabled = bool(on)

    def setVisible(self, on):
        self.visible = bool(on)


class _Label(_Button):
    text = ""

    def setText(self, text):
        self.text = text


# ---------------------------------------------------------------------------
# (a) prepare_terrain / prepare_terrain_profile forward the cancel hooks
# ---------------------------------------------------------------------------

class TerrainForwardsCancelHooks(unittest.TestCase):

    def test_ingest_gets_should_cancel_and_on_start(self):
        seen = {}

        def fake_run_converter(exe, job_file, should_cancel=None,
                               on_start=None):
            seen.update(should_cancel=should_cancel, on_start=on_start)
            return []

        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(ta, "get_cache_dir", return_value=root), \
             mock.patch.object(ta, "_export_via_qgis",
                               side_effect=_fake_export), \
             mock.patch.object(ta, "_run_converter",
                               side_effect=fake_run_converter):
            ta.prepare_terrain(_wms_layer(), 47.4, 8.5, 5.0, 30,
                               _converter_bm(), should_cancel=_never,
                               on_start=_no_start)
        self.assertIs(seen["should_cancel"], _never)
        self.assertIs(seen["on_start"], _no_start)

    def test_download_gets_should_cancel_and_on_start(self):
        seen = {}

        def fake_download(source, pool, bbox, res, subtiles, manager,
                          pbf_dir=None, progress_cb=None, **kw):
            seen.update(kw)
            return ta.DownloadOutcome(True)

        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(ta, "get_cache_dir", return_value=root), \
             mock.patch.object(ta, "classify_raster_layer",
                               return_value="dem"), \
             mock.patch.object(ta, "_try_rust_download",
                               side_effect=fake_download), \
             mock.patch.object(ta, "_finish_view"):
            ta.prepare_terrain(_xyz_layer(), 46.945, 7.41, 1.0, 30,
                               mock.Mock(), should_cancel=_never,
                               on_start=_no_start)
        self.assertIs(seen["should_cancel"], _never)
        self.assertIs(seen["on_start"], _no_start)

    def test_download_pass_gets_them_and_the_cancel_gets_through(self):
        # _try_rust_download → _run_converter_download_once: a cancel is
        # re-raised, never turned into a failed DownloadOutcome, and the
        # per-run job file is still removed.
        tiles = mct._enumerate_tiles(mct._snap_bbox(
            {"south": 47.1, "north": 47.25, "west": 8.1, "east": 8.35},
            [2]), [2])
        seen = {}

        def fake_once(exe, job_file, on_line=None, should_cancel=None,
                      on_start=None):
            seen.update(should_cancel=should_cancel, on_start=on_start,
                        job_file=job_file)
            raise ta.ConverterCancelled()

        with tempfile.TemporaryDirectory() as pool, \
             mock.patch.object(ta, "_run_converter_download_once",
                               side_effect=fake_once):
            with self.assertRaises(ta.ConverterCancelled):
                ta._try_rust_download(_XYZ, pool, {}, 2,
                                      [t for _r, t in tiles], _converter_bm(),
                                      should_cancel=_never,
                                      on_start=_no_start)
        self.assertIs(seen["should_cancel"], _never)
        self.assertIs(seen["on_start"], _no_start)
        self.assertFalse(os.path.exists(seen["job_file"]))

    def test_profile_polls_per_band_and_forwards_both(self):
        stop = {"now": False}
        calls = []

        def fake_prepare(*_a, **kw):
            calls.append(kw)
            stop["now"] = True          # Stop pressed during the first band
            return "view"

        with tempfile.TemporaryDirectory() as cache, \
             mock.patch.object(ta, "prepare_terrain",
                               side_effect=fake_prepare), \
             mock.patch.object(ta, "get_cache_dir", return_value=cache):
            with self.assertRaises(ta.ConverterCancelled):
                ta.prepare_terrain_profile(
                    _xyz_layer(), 47.05, 8.48, rp.adaptive_bands(5, 20),
                    binary_manager=None, should_cancel=lambda: stop["now"],
                    on_start=_no_start)
        self.assertEqual(len(calls), 1, "a band after Stop was still built")
        self.assertIs(calls[0]["on_start"], _no_start)
        self.assertTrue(calls[0]["should_cancel"]())

    def test_fingerprint_loop_polls_per_tile(self):
        fps = []

        def fake_fp(infos, bbox, res, index=None):
            fps.append(bbox)
            return "fp"

        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(ta, "get_cache_dir", return_value=root), \
             mock.patch.object(ta, "_gather_source_infos",
                               return_value=([{"path": "a.tif"}], False)
                               ) as gather, \
             mock.patch.object(ta, "_infos_near",
                               side_effect=lambda infos, _tiles: infos), \
             mock.patch.object(ta, "tile_source_fingerprint",
                               side_effect=fake_fp), \
             mock.patch.object(ta, "_run_converter") as conv:
            cancel = lambda: len(fps) >= 1   # noqa: E731
            with self.assertRaises(ta.ConverterCancelled):
                ta.prepare_terrain(_wms_layer(), 47.4, 8.5, 5.0, 30,
                                   _converter_bm(), should_cancel=cancel)
        self.assertEqual(len(fps), 1, "fingerprinting went on after Stop")
        self.assertIs(gather.call_args.kwargs["should_cancel"], cancel)
        conv.assert_not_called()

    def test_job_loop_polls_per_tile_and_still_removes_scratch(self):
        exports = []

        def fake_export(dem_layer, dest, bbox, resolution_m):
            _fake_export(dem_layer, dest, bbox, resolution_m)
            exports.append(dest)

        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(ta, "get_cache_dir", return_value=root), \
             mock.patch.object(ta, "_export_via_qgis",
                               side_effect=fake_export), \
             mock.patch.object(ta, "_run_converter") as conv:
            with self.assertRaises(ta.ConverterCancelled):
                # 5 km at 30 m spans two 0.5 deg tiles: Stop after the first.
                ta.prepare_terrain(_wms_layer(), 47.4, 8.5, 5.0, 30,
                                   _converter_bm(),
                                   should_cancel=lambda: bool(exports))
        self.assertEqual(len(exports), 1, "the next tile was still exported")
        conv.assert_not_called()
        self.assertFalse(os.path.exists(exports[0]),
                         "the finally no longer removed the scratch export")

    def test_folder_scan_stop_is_converter_cancelled(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(ta, "folder_source_infos",
                               side_effect=ta.terrain_index.ScanCancelled
                               ) as infos:
            with self.assertRaises(ta.ConverterCancelled):
                ta._gather_source_infos(None, d, should_cancel=_never)
        self.assertIs(infos.call_args.kwargs["should_cancel"], _never)

    def test_pool_check_polls_per_tile(self):
        tiles = mct._enumerate_tiles(mct._snap_bbox(
            {"south": 47.1, "north": 47.25, "west": 8.1, "east": 8.35},
            [2]), [2])
        polls = []

        def cancel():
            polls.append(1)
            return len(polls) > 1

        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(ta, "get_cache_dir", return_value=root):
            with self.assertRaises(ta.ConverterCancelled):
                ta.estimate_pool_download_units(
                    _XYZ, [t for _r, t in tiles], should_cancel=cancel)
            # Without a stop it still never raises.
            self.assertGreater(ta.estimate_pool_download_units(
                _XYZ, [t for _r, t in tiles]), 0)
        self.assertEqual(len(polls), 2)


# ---------------------------------------------------------------------------
# (b) _run_converter: cancel is ConverterCancelled, the runner gets on_start
# ---------------------------------------------------------------------------

class RunConverterCancel(unittest.TestCase):

    def test_per_line_cancel_raises_and_the_runner_gets_on_start(self):
        proc = mock.Mock()
        started = []
        seen = {}

        def on_start(p):
            started.append(p)

        def fake_runner(exe, args, on_line, on_start=None, env=None):
            seen["on_start"] = on_start
            on_start(proc)
            on_line("Progress: 10/40")
            return 0

        with mock.patch.object(ta, "run_converter_streaming",
                               side_effect=fake_runner):
            with self.assertRaises(ta.ConverterCancelled):
                ta._run_converter("conv", "job.json",
                                  should_cancel=lambda: True,
                                  on_start=on_start)
        self.assertIs(seen["on_start"], on_start)
        self.assertEqual(started, [proc])

    def test_killed_process_is_a_cancel_not_a_converter_failure(self):
        stop = {"now": False}

        def fake_runner(exe, args, on_line, on_start=None, env=None):
            stop["now"] = True        # cancel() killed it before any line
            return -15

        with mock.patch.object(ta, "run_converter_streaming",
                               side_effect=fake_runner):
            with self.assertRaises(ta.ConverterCancelled):
                ta._run_converter("conv", "job.json",
                                  should_cancel=lambda: stop["now"])

    def test_a_real_failure_is_still_a_runtime_error(self):
        def fake_runner(exe, args, on_line, on_start=None, env=None):
            on_line("[Error] boom")
            return 1

        with mock.patch.object(ta, "run_converter_streaming",
                               side_effect=fake_runner):
            with self.assertRaises(RuntimeError) as caught:
                ta._run_converter("conv", "job.json", should_cancel=_never)
        self.assertIn("converter failed (1)", str(caught.exception))
        self.assertIn("[Error] boom", str(caught.exception))

    def test_killed_download_pass_is_a_cancel_not_a_retry(self):
        stop = {"now": False}

        def fake_runner(exe, args, on_line, on_start=None, env=None):
            stop["now"] = True
            return -15

        with mock.patch.object(ta, "run_converter_streaming",
                               side_effect=fake_runner):
            with self.assertRaises(ta.ConverterCancelled):
                ta._run_converter_download_once(
                    "conv", "job.json", should_cancel=lambda: stop["now"])
            # No stop: the exit code is the caller's to judge, as before.
            stop["now"] = False
            rc, _lines = ta._run_converter_download_once(
                "conv", "job.json", should_cancel=lambda: False)
        self.assertEqual(rc, -15)


# ---------------------------------------------------------------------------
# (c) Site Analysis worker: Stop kills the terrain step's engine process
# ---------------------------------------------------------------------------

def _site_job(**kw):
    params = CoverageParams(tx_lat=47.4, tx_lon=8.5, resolution_m=30,
                            max_range_km=5, output_name="site1", **kw)
    return [(params, "Site 1")]


def _sleeper(test: unittest.TestCase) -> subprocess.Popen:
    """A real process that outlives the test unless something kills it."""
    proc = subprocess.Popen([sys.executable, "-c",
                             "import time; time.sleep(30)"])

    def reap():
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    test.addCleanup(reap)
    return proc


def _drive_quiet_engine(worker, build_target, extra_patches=()):
    """Run *worker* on the real terrain path with a quiet engine that only a
    kill ends, pressing Stop from another thread meanwhile.

    *build_target* ``(module, name)`` is the job builder that must never run
    after Stop. Returns ``(errs, oks, procs, build mock, elapsed s)``.
    """
    errs, oks, procs = [], [], []
    worker.finished_err.connect(errs.append)
    worker.finished_ok.connect(oks.append)
    line_seen = threading.Event()

    def quiet_engine(exe, args, on_line, on_start=None, env=None):
        def spy(proc):
            procs.append(proc)
            if on_start is not None:
                on_start(proc)

        def hook(line):
            on_line(line)
            line_seen.set()
        return _REAL_RUNNER(sys.executable, ["-c", _QUIET_ENGINE], hook,
                            on_start=spy, env=env)

    def press_stop():
        # The GUI thread: Stop arrives while the worker blocks on an
        # engine that prints nothing more.
        if line_seen.wait(10):
            time.sleep(0.2)
        worker.cancel()

    stopper = threading.Thread(target=press_stop)
    with tempfile.TemporaryDirectory() as root, \
         mock.patch.object(ta, "get_cache_dir", return_value=root), \
         mock.patch.object(bm, "check_engine_for_job",
                           return_value="0.4.10"), \
         mock.patch.object(bm, "find_binary",
                           return_value="aether_converter"), \
         mock.patch.object(ta, "run_converter_streaming",
                           side_effect=quiet_engine), \
         mock.patch.object(*build_target) as build:
        for p in extra_patches:
            p.start()
        try:
            t0 = time.monotonic()
            stopper.start()
            worker.run()
            elapsed = time.monotonic() - t0
        finally:
            for p in extra_patches:
                p.stop()
            stopper.join(15)
    return errs, oks, procs, build, elapsed


class SiteWorkerStopsTheTerrainStep(unittest.TestCase):

    def _drive(self, worker, extra_patches=()):
        return _drive_quiet_engine(worker, (sat, "build_coverage_job"),
                                   extra_patches)

    def test_stop_during_ingest_kills_the_converter(self):
        exports = []

        def fake_export(dem_layer, dest, bbox, resolution_m):
            _fake_export(dem_layer, dest, bbox, resolution_m)
            exports.append(dest)

        with tempfile.TemporaryDirectory() as d:
            worker = sat._SiteAnalysisWorker(_site_job(), _wms_layer(),
                                             os.path.join(d, "out"))
            errs, oks, procs, build, elapsed = self._drive(
                worker, [mock.patch.object(
                    ta, "_export_via_qgis", side_effect=fake_export)])
        self.assertEqual(errs, [], "a stopped run surfaced an error")
        self.assertEqual(oks, [])
        self.assertEqual(len(procs), 1)
        self.assertIsNotNone(procs[0].poll(), "the converter outlived Stop")
        self.assertLess(elapsed, 15, "Stop waited for the engine to finish")
        build.assert_not_called()
        self.assertIsNone(worker._proc)
        self.assertTrue(exports)
        self.assertFalse(any(os.path.exists(p) for p in exports))

    def test_stop_during_download_kills_the_downloader(self):
        with tempfile.TemporaryDirectory() as d:
            worker = sat._SiteAnalysisWorker(_site_job(), _xyz_layer(),
                                             os.path.join(d, "out"))
            errs, oks, procs, build, elapsed = self._drive(
                worker, [mock.patch.object(
                    ta, "classify_raster_layer", return_value="dem")])
        self.assertEqual(errs, [], "a stopped download surfaced an error")
        self.assertEqual(oks, [])
        self.assertEqual(len(procs), 1, "a retry pass ran after Stop")
        self.assertIsNotNone(procs[0].poll(), "the download outlived Stop")
        self.assertLess(elapsed, 15)
        build.assert_not_called()

    def test_engine_started_after_stop_is_killed_on_registration(self):
        proc = _sleeper(self)
        errs = []

        with tempfile.TemporaryDirectory() as d:
            worker = sat._SiteAnalysisWorker(_site_job(), mock.Mock(),
                                             os.path.join(d, "out"))
            worker.finished_err.connect(errs.append)

            def fake_prepare(**kw):
                # Stop lands after the last poll, before the launch...
                worker.cancel()
                self.assertTrue(kw["should_cancel"]())
                # ...and the engine starts anyway: killed right there.
                kw["on_start"](proc)
                self.assertIsNotNone(proc.wait(timeout=10))
                raise ta.ConverterCancelled()

            with mock.patch.object(sat, "prepare_terrain",
                                   side_effect=fake_prepare), \
                 mock.patch.object(bm, "check_engine_for_job",
                                   return_value="0.4.10"), \
                 mock.patch.object(sat, "build_coverage_job") as build:
                worker.run()
        self.assertEqual(errs, [])
        build.assert_not_called()
        self.assertIsNone(worker._proc)

    def test_profile_terrain_gets_the_same_hooks(self):
        seen = {}
        bands = rp.adaptive_bands(5, 20)
        with tempfile.TemporaryDirectory() as d:
            worker = sat._SiteAnalysisWorker(
                _site_job(range_profile=bands), mock.Mock(),
                os.path.join(d, "out"))
            errs = []
            worker.finished_err.connect(errs.append)

            def fake_profile(**kw):
                seen.update(kw)
                raise ta.ConverterCancelled()

            with mock.patch.object(sat, "prepare_terrain_profile",
                                   side_effect=fake_profile), \
                 mock.patch.object(bm, "check_engine_for_job",
                                   return_value="0.4.10"):
                worker.run()
        self.assertEqual(errs, [])
        self.assertEqual(seen["on_start"], worker._register_proc)
        self.assertFalse(seen["should_cancel"]())
        worker.cancel()
        self.assertTrue(seen["should_cancel"]())


# ---------------------------------------------------------------------------
# (d) Site Analysis tab: Stop never blocks, Run returns only on finished
# ---------------------------------------------------------------------------

class SiteTabStopWaitsForTheThread(unittest.TestCase):

    def _tab(self):
        tab = SiteAnalysisTab.__new__(SiteAnalysisTab)
        tab._worker = None
        tab.btn_run = _Button()
        tab.btn_stop = _Button()
        tab.progress_bar = mock.Mock()
        tab.status_label = _Label()
        tab.chk_buildings = mock.Mock(isChecked=mock.Mock(return_value=False))
        tab._selected_terrain_dir = lambda: ""
        tab._sites_rb = mock.Mock()
        return tab

    def _launch(self, tab, d):
        tab._launch_worker(_site_job(), mock.Mock(), d)
        worker = tab._worker
        worker.isRunning = lambda: True
        worker.wait = mock.Mock(return_value=True)
        return worker

    def test_stop_keeps_run_disabled_until_the_thread_ends(self):
        tab = self._tab()
        with tempfile.TemporaryDirectory() as d:
            worker = self._launch(tab, d)
            self.assertFalse(tab.btn_run.enabled)
            self.assertTrue(tab.btn_stop.enabled and tab.btn_stop.visible)

            tab._on_stop()
            self.assertTrue(worker._canceled)
            worker.wait.assert_not_called()       # GUI thread never blocks
            self.assertFalse(tab.btn_run.enabled,
                             "Run came back while the worker still runs")
            self.assertFalse(tab.btn_stop.enabled)
            self.assertEqual(tab.status_label.text, "Stopping...")

            worker.isRunning = lambda: False
            worker.finished.emit()
        self.assertTrue(tab.btn_run.enabled)
        self.assertFalse(tab.btn_stop.visible)

    def test_a_late_finished_of_an_older_worker_leaves_the_new_run(self):
        tab = self._tab()
        with tempfile.TemporaryDirectory() as d:
            old = self._launch(tab, d)
            tab._on_stop()
            old.isRunning = lambda: False
            new = self._launch(tab, d)
            old.finished.emit()
            self.assertFalse(tab.btn_run.enabled)
            self.assertTrue(tab.btn_stop.enabled)
            new.finished.emit()
        self.assertTrue(tab.btn_run.enabled)

    def test_error_then_finished_resets_once_and_stays_reset(self):
        tab = self._tab()
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(sat, "show_run_error") as shown:
            worker = self._launch(tab, d)
            worker.finished_err.emit("engine said no")
            self.assertTrue(tab.btn_run.enabled)
            worker.isRunning = lambda: False
            worker.finished.emit()
        shown.assert_called_once()
        self.assertTrue(tab.btn_run.enabled)
        self.assertFalse(tab.btn_stop.visible)

    def test_dialog_close_waits_bounded_and_reenables_nothing_early(self):
        tab = self._tab()
        with tempfile.TemporaryDirectory() as d:
            worker = self._launch(tab, d)
            tab.cancel_worker()
            self.assertTrue(worker._canceled)
            worker.wait.assert_called_once_with(5000)
            # The wait timed out (isRunning still True): Run stays off.
            self.assertFalse(tab.btn_run.enabled)
            worker.isRunning = lambda: False
            worker.finished.emit()
        self.assertTrue(tab.btn_run.enabled)


# ---------------------------------------------------------------------------
# P2P tab: the same contract as Site Analysis (worker and tab)
# ---------------------------------------------------------------------------

# Bern -> Thun, the batch tests/test_p2p_tab.py drives the worker with.
_BERN = (46.9480, 7.4474)
_THUN = (46.7580, 7.6280)


def _p2p_worker(d, layer):
    csv_path = os.path.join(d, "batch.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(f"S,BERN,{_BERN[0]:.6f},{_BERN[1]:.6f},30.0,AGL\n")
        fh.write(f"R,THUN,{_THUN[0]:.6f},{_THUN[1]:.6f},2.0,AGL\n")
    params = P2PParams(tx_lat=_BERN[0], tx_lon=_BERN[1],
                       output_name="p2p_test", resolution_m=30, model="ITM")
    return p2p_tab._P2PWorker(params, layer, os.path.join(d, "out"), csv_path)


class P2PWorkerStopsTheTerrainStep(unittest.TestCase):

    def _drive(self, worker, extra_patches=()):
        return _drive_quiet_engine(worker, (p2p_tab, "build_p2p_job"),
                                   extra_patches)

    def test_stop_during_ingest_kills_the_converter(self):
        exports = []

        def fake_export(dem_layer, dest, bbox, resolution_m):
            _fake_export(dem_layer, dest, bbox, resolution_m)
            exports.append(dest)

        with tempfile.TemporaryDirectory() as d:
            worker = _p2p_worker(d, _wms_layer())
            errs, oks, procs, build, elapsed = self._drive(
                worker, [mock.patch.object(
                    ta, "_export_via_qgis", side_effect=fake_export)])
        self.assertEqual(errs, [], "a stopped run surfaced an error")
        self.assertEqual(oks, [])
        self.assertEqual(len(procs), 1)
        self.assertIsNotNone(procs[0].poll(), "the converter outlived Stop")
        self.assertLess(elapsed, 15, "Stop waited for the engine to finish")
        build.assert_not_called()
        self.assertIsNone(worker._proc)
        self.assertTrue(exports)
        self.assertFalse(any(os.path.exists(p) for p in exports))

    def test_stop_during_download_kills_the_downloader(self):
        with tempfile.TemporaryDirectory() as d:
            worker = _p2p_worker(d, _xyz_layer())
            errs, oks, procs, build, elapsed = self._drive(
                worker, [mock.patch.object(
                    ta, "classify_raster_layer", return_value="dem")])
        self.assertEqual(errs, [], "a stopped download surfaced an error")
        self.assertEqual(oks, [])
        self.assertEqual(len(procs), 1, "a retry pass ran after Stop")
        self.assertIsNotNone(procs[0].poll(), "the download outlived Stop")
        self.assertLess(elapsed, 15)
        build.assert_not_called()

    def test_engine_started_after_stop_is_killed_on_registration(self):
        proc = _sleeper(self)
        errs = []

        with tempfile.TemporaryDirectory() as d:
            worker = _p2p_worker(d, mock.Mock())
            worker.finished_err.connect(errs.append)

            def fake_prepare(**kw):
                worker.cancel()          # Stop after the last poll...
                self.assertTrue(kw["should_cancel"]())
                kw["on_start"](proc)     # ...the engine starts: killed
                self.assertIsNotNone(proc.wait(timeout=10))
                raise ta.ConverterCancelled()

            # p2p_tab imports prepare_terrain from the module at run time.
            with mock.patch.object(ta, "prepare_terrain",
                                   side_effect=fake_prepare), \
                 mock.patch.object(bm, "check_engine_for_job",
                                   return_value="0.4.10"), \
                 mock.patch.object(p2p_tab, "build_p2p_job") as build:
                worker.run()
        self.assertEqual(errs, [])
        build.assert_not_called()
        self.assertIsNone(worker._proc)


class P2PTabStopWaitsForTheThread(unittest.TestCase):

    def _tab(self):
        tab = p2p_tab.P2PTab.__new__(p2p_tab.P2PTab)
        tab._worker = None
        tab.btn_run = _Button()
        tab.btn_stop = _Button()
        tab.btn_view_plots = _Button()
        tab.progress_bar = mock.Mock()
        tab.result_area = mock.Mock()
        tab._reset_rubber_bands = mock.Mock()
        return tab

    def _launch(self, tab, d):
        tab._launch_worker(P2PParams(output_name="p2p_test", resolution_m=30),
                           mock.Mock(), d, os.path.join(d, "batch.csv"), "")
        worker = tab._worker
        worker.isRunning = lambda: True
        worker.wait = mock.Mock(return_value=True)
        return worker

    def test_stop_keeps_run_disabled_until_the_thread_ends(self):
        tab = self._tab()
        with tempfile.TemporaryDirectory() as d:
            worker = self._launch(tab, d)
            self.assertFalse(tab.btn_run.enabled)
            self.assertTrue(tab.btn_stop.enabled and tab.btn_stop.visible)

            tab._on_stop()
            self.assertTrue(worker._canceled)
            worker.wait.assert_not_called()       # GUI thread never blocks
            self.assertFalse(tab.btn_run.enabled,
                             "Run came back while the worker still runs")
            self.assertFalse(tab.btn_stop.enabled)
            tab.result_area.append.assert_called_with("Stopping...")

            worker.isRunning = lambda: False
            worker.finished.emit()
        self.assertTrue(tab.btn_run.enabled)
        self.assertFalse(tab.btn_stop.visible)

    def test_a_late_finished_of_an_older_worker_leaves_the_new_run(self):
        tab = self._tab()
        with tempfile.TemporaryDirectory() as d:
            old = self._launch(tab, d)
            tab._on_stop()
            old.isRunning = lambda: False
            new = self._launch(tab, d)
            old.finished.emit()
            self.assertFalse(tab.btn_run.enabled)
            self.assertTrue(tab.btn_stop.enabled)
            new.finished.emit()
        self.assertTrue(tab.btn_run.enabled)

    def test_error_then_finished_resets_once_and_stays_reset(self):
        tab = self._tab()
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(p2p_tab, "show_run_error") as shown:
            worker = self._launch(tab, d)
            worker.finished_err.emit("engine said no")
            self.assertTrue(tab.btn_run.enabled)
            worker.isRunning = lambda: False
            worker.finished.emit()
        shown.assert_called_once()
        self.assertTrue(tab.btn_run.enabled)
        self.assertFalse(tab.btn_stop.visible)

    def test_dialog_close_waits_bounded_and_reenables_nothing_early(self):
        tab = self._tab()
        with tempfile.TemporaryDirectory() as d:
            worker = self._launch(tab, d)
            tab.cancel_worker()
            self.assertTrue(worker._canceled)
            worker.wait.assert_called_once_with(5000)
            tab._reset_rubber_bands.assert_called_once()
            # The wait timed out (isRunning still True): Run stays off.
            self.assertFalse(tab.btn_run.enabled)
            worker.isRunning = lambda: False
            worker.finished.emit()
        self.assertTrue(tab.btn_run.enabled)


# ---------------------------------------------------------------------------
# (e) Map Converter: Convert comes back, no job file left, loops poll
# ---------------------------------------------------------------------------

_RAW = {"south": 47.0, "north": 47.1, "west": 8.0, "east": 8.1}


class MapConverterCancel(unittest.TestCase):

    def _tab(self, out_dir):
        tab = MapConverterTab.__new__(MapConverterTab)
        tab._worker = None
        tab._run_reported = False
        tab._run_model = None
        tab._layers = [_LayerEntry(layer_type="raster", source_path="/a.tif",
                                   extent=dict(_RAW), target_resolutions=[30])]
        tab._get_output_resolutions = lambda: [30]
        tab._preflight_folders = lambda: True
        tab._get_output_dir = lambda: out_dir
        tab._log = mock.Mock()
        tab._chk_overwrite = mock.Mock(isChecked=mock.Mock(return_value=True))
        tab._progress = mock.Mock(value=mock.Mock(return_value=0))
        tab._progress_timer = mock.Mock()
        tab._lbl_status = _Label()
        tab._btn_run = _Button()
        tab._btn_cancel = _Button()
        return tab

    def _start(self, tab):
        with mock.patch.object(mct, "_pending_render_jobs",
                               return_value=(None, [])), \
             mock.patch.object(mct, "_estimate_run_work"), \
             mock.patch.object(mct, "resolve_sources_with_progress",
                               return_value=["/a.tif"]):
            tab._on_run()
        worker = tab._worker
        worker.isRunning = lambda: True
        return worker

    def test_convert_comes_back_after_a_cancelled_run(self):
        with tempfile.TemporaryDirectory() as out_dir:
            tab = self._tab(out_dir)
            worker = self._start(tab)
            self.assertFalse(tab._btn_run.enabled)
            self.assertTrue(tab._btn_cancel.enabled)

            tab._on_cancel()
            self.assertTrue(worker._canceled)
            # The worker unwinds; a cancelled run emits neither ok nor err.
            with mock.patch.object(_MapConverterWorker, "_resolve_raster",
                                   return_value={"kind": "files",
                                                 "infos": []}), \
                 mock.patch.object(_MapConverterWorker, "_resolve_buildings",
                                   return_value=None):
                worker.run()
            self.assertFalse(tab._btn_run.enabled,
                             "Convert came back before the thread ended")

            worker.isRunning = lambda: False
            worker.finished.emit()
        self.assertTrue(tab._btn_run.enabled, "Convert stayed disabled")
        self.assertFalse(tab._btn_cancel.enabled)
        self.assertEqual(tab._lbl_status.text, "Cancelled.")
        self.assertTrue(tab._run_model.stopped)

    def test_done_then_finished_is_not_a_cancel(self):
        with tempfile.TemporaryDirectory() as out_dir:
            tab = self._tab(out_dir)
            worker = self._start(tab)
            worker.finished_ok.emit(out_dir)
            self.assertTrue(tab._btn_run.enabled)
            worker.isRunning = lambda: False
            worker.finished.emit()
        self.assertNotEqual(tab._lbl_status.text, "Cancelled.")

    def test_a_cancel_after_a_done_run_still_gives_convert_back(self):
        # _run_reported belongs to ONE run: a done run's True must not make
        # the next, cancelled run look reported (Convert would stay off).
        with tempfile.TemporaryDirectory() as out_dir:
            tab = self._tab(out_dir)
            first = self._start(tab)
            first.finished_ok.emit(out_dir)
            first.isRunning = lambda: False
            first.finished.emit()
            self.assertTrue(tab._btn_run.enabled)

            second = self._start(tab)
            self.assertFalse(tab._run_reported)
            self.assertFalse(tab._btn_run.enabled)
            tab._on_cancel()
            self.assertTrue(second._canceled)
            second.isRunning = lambda: False
            second.finished.emit()         # cancelled: neither ok nor err
        self.assertTrue(tab._btn_run.enabled,
                        "Convert stayed disabled after a done run's cancel")
        self.assertEqual(tab._lbl_status.text, "Cancelled.")

    def test_engine_started_after_cancel_is_killed_on_registration(self):
        proc = _sleeper(self)
        with tempfile.TemporaryDirectory() as out_dir:
            worker = _MapConverterWorker([], out_dir, [30], [])
        worker.cancel()                    # no process yet: nothing to kill
        worker._register_proc(proc)        # ...then the engine starts
        self.assertIsNotNone(proc.wait(timeout=10))
        self.assertIs(worker._proc, proc)

    def test_ingest_started_after_cancel_ends_as_a_clean_cancel(self):
        proc = _sleeper(self)

        def fake_runner(exe, args, on_line, on_start=None, env=None):
            fake_runner.worker.cancel()    # Stop lands just before launch
            on_start(proc)                 # the converter starts anyway
            return proc.wait(timeout=10)   # ...killed on registration

        with tempfile.TemporaryDirectory() as tmp:
            errs, status = self._run_to_ingest(fake_runner, tmp)
        self.assertEqual(errs, [], "the start-after-cancel race surfaced")
        self.assertIn("Cancelled.", status)
        self.assertIsNotNone(proc.poll())

    def _run_to_ingest(self, fake_runner, tmp):
        """Run a worker to the converter call, %TEMP% redirected to *tmp*."""
        src = os.path.join(tmp, "a.tif")
        open(src, "w").close()
        info = {"path": src, "crs": None, "crs_authid": "EPSG:4326",
                "native_bounds": {"west": 0, "east": 20, "south": 40,
                                  "north": 50},
                "wgs84_bounds": {"west": 0, "east": 20, "south": 40,
                                 "north": 50},
                "halo_deg": 0.001}
        entry = _LayerEntry(layer_type="raster", source_path=src,
                            extent=dict(_RAW), target_resolutions=[30])
        out_dir = os.path.join(tmp, "out")
        worker = _MapConverterWorker([entry], out_dir, [30], [src],
                                     overwrite=True)
        fake_runner.worker = worker
        errs, status = [], []
        worker.finished_err.connect(errs.append)
        worker.status.connect(status.append)
        with mock.patch.object(_MapConverterWorker, "_resolve_raster",
                               return_value={"kind": "files",
                                             "infos": [info]}), \
             mock.patch.object(_MapConverterWorker, "_resolve_buildings",
                               return_value=None), \
             mock.patch.object(mct, "find_binary", return_value="/bin/conv"), \
             mock.patch.object(mct, "_plan_cross_check"), \
             mock.patch.object(mct.tempfile, "gettempdir", return_value=tmp), \
             mock.patch.object(ta, "run_converter_streaming",
                               side_effect=fake_runner):
            worker.run()
        return errs, status

    def test_cancelled_ingest_removes_its_job_file(self):
        seen = {}

        def fake_runner(exe, args, on_line, on_start=None, env=None):
            seen["job_file"] = args[-1]
            self.assertTrue(os.path.exists(args[-1]))
            raise ta.ConverterCancelled()

        with tempfile.TemporaryDirectory() as tmp:
            errs, status = self._run_to_ingest(fake_runner, tmp)
            self.assertEqual(os.path.dirname(seen["job_file"]), tmp)
            self.assertFalse(os.path.exists(seen["job_file"]),
                             "the %TEMP% job file outlived the cancel")
        self.assertEqual(errs, [])
        self.assertIn("Cancelled.", status)

    def test_failed_ingest_removes_its_job_file_too(self):
        seen = {}

        def fake_runner(exe, args, on_line, on_start=None, env=None):
            seen["job_file"] = args[-1]
            return 3

        with tempfile.TemporaryDirectory() as tmp:
            errs, _status = self._run_to_ingest(fake_runner, tmp)
            self.assertFalse(os.path.exists(seen["job_file"]))
        self.assertEqual(len(errs), 1)

    def test_fingerprints_stop_when_should_cancel_is_true(self):
        expected = mct._enumerate_tiles(mct._snap_bbox(
            {"south": 47.1, "north": 47.25, "west": 8.1, "east": 8.35},
            [2]), [2])
        self.assertGreater(len(expected), 2)
        polls = []

        def cancel():
            polls.append(1)
            return len(polls) > 1

        with mock.patch.object(ta, "source_identity",
                               return_value="id") as ident:
            with self.assertRaises(ta.ConverterCancelled):
                mct._stack_tile_fingerprints(
                    expected, [{"kind": "xyz", "uri": "u"}], None,
                    should_cancel=cancel)
        self.assertEqual(ident.call_count, 1, "tiles kept being hashed")
        # No stop: every tile still gets its fingerprint.
        with mock.patch.object(ta, "source_identity", return_value="id"):
            fps = mct._stack_tile_fingerprints(
                expected, [{"kind": "xyz", "uri": "u"}], None)
        self.assertEqual(len(fps), len(expected))

    def test_job_build_stops_and_removes_its_scratch(self):
        expected = mct._enumerate_tiles(mct._snap_bbox(
            {"south": 47.1, "north": 47.25, "west": 8.1, "east": 8.35},
            [2]), [2])
        made = []

        def fake_sources(infos, tile_bbox, tmp_dir, tag, output_res_m,
                         index=None):
            path = os.path.join(tmp_dir, f"{tag}.tif")
            open(path, "w").close()
            made.append(path)
            return [{"path": path}], [path]

        with tempfile.TemporaryDirectory() as out_dir, \
             mock.patch.object(mct, "build_tile_sources",
                               side_effect=fake_sources):
            with self.assertRaises(ta.ConverterCancelled):
                mct._build_tile_jobs(
                    expected, out_dir, True, [{"kind": "files", "infos": []}],
                    None, out_dir, should_cancel=lambda: bool(made))
            self.assertEqual(len(made), 1)
            self.assertFalse(os.path.exists(made[0]))

    def test_stop_in_a_per_tile_loop_ends_the_worker_quietly(self):
        # Stop lands during the plan check; the fingerprint loop is the
        # next thing to run and must end the run as a clean cancel.
        with tempfile.TemporaryDirectory() as tmp:
            entry = _LayerEntry(layer_type="raster", source_path="/a.tif",
                                extent=dict(_RAW), target_resolutions=[30])
            worker = _MapConverterWorker([entry], tmp, [30], ["/a.tif"],
                                         overwrite=True)
            errs, oks, status = [], [], []
            worker.finished_err.connect(errs.append)
            worker.finished_ok.connect(oks.append)
            worker.status.connect(status.append)
            with mock.patch.object(_MapConverterWorker, "_resolve_raster",
                                   return_value={"kind": "files",
                                                 "infos": []}), \
                 mock.patch.object(_MapConverterWorker, "_resolve_buildings",
                                   return_value=None), \
                 mock.patch.object(mct, "find_binary",
                                   return_value="/bin/conv"), \
                 mock.patch.object(mct, "_plan_cross_check",
                                   side_effect=lambda *a: worker.cancel()), \
                 mock.patch.object(mct, "_build_tile_jobs") as build:
                worker.run()
        self.assertEqual(errs, [])
        self.assertEqual(oks, [])
        self.assertIn("Cancelled.", status)
        build.assert_not_called()


# ---------------------------------------------------------------------------
# (f) The kill itself never blocks the GUI thread (Site Analysis, P2P)
# ---------------------------------------------------------------------------

class _FakeEngine:
    """An engine Popen: running until killed; records what was done to it."""

    def __init__(self, running=True, pid=4242):
        self.pid = pid
        self.returncode = None if running else 0
        self.done = []

    def poll(self):
        return self.returncode

    def kill(self):
        self.done.append("kill")
        self.returncode = 1

    def terminate(self):
        self.done.append("terminate")
        self.returncode = -15

    def wait(self, timeout=None):
        raise AssertionError("Stop waited for the engine")


class _FakeTaskkill:
    """``subprocess.Popen`` stand-in for the taskkill launch: recorded; its
    ``wait`` blocks until released and notes the thread that waited."""

    started = []

    def __init__(self, argv, **kwargs):
        self.argv, self.kwargs = list(argv), kwargs
        self.release = threading.Event()
        self.waited_on = []
        _FakeTaskkill.started.append(self)

    def wait(self, timeout=None):
        self.waited_on.append(threading.current_thread())
        # Bounded: a caller that waits here fails the timing check instead
        # of hanging the suite.
        self.release.wait(2.0 if timeout is None else min(timeout, 2.0))
        return 0

    communicate = wait


def _wait_until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return bool(pred())


class StopKillNeverBlocks(unittest.TestCase):
    """``_kill_proc`` of the Site Analysis and P2P workers (Stop, on the GUI
    thread) = ``terrain_adapter.kill_engine_process``. Windows: the process
    is killed through its own handle at once, then ``taskkill /F /T`` for its
    children is launched and never waited for (a blocking taskkill froze the
    GUI 0.1–1 s, more under antivirus). POSIX: ``terminate()``, as before."""

    KILLS = (("site analysis", sat._kill_proc), ("p2p", p2p_tab._kill_proc))

    def setUp(self):
        _FakeTaskkill.started = []
        self.addCleanup(lambda: [tk.release.set() for tk in _FakeTaskkill.started])

    def kill_on(self, platform_name, engine):
        with mock.patch.object(sys, "platform", platform_name), \
             mock.patch.object(ta.subprocess, "Popen", _FakeTaskkill), \
             mock.patch.object(ta.subprocess, "run",
                               side_effect=AssertionError("blocking run")):
            for _name, kill in self.KILLS:
                kill(engine)

    def test_windows_kills_by_handle_then_fires_taskkill(self):
        for name, kill in self.KILLS:
            with self.subTest(name):
                engine = _FakeEngine()
                with mock.patch.object(sys, "platform", "win32"), \
                     mock.patch.object(ta.subprocess, "Popen", _FakeTaskkill), \
                     mock.patch.object(ta.subprocess, "run",
                                       side_effect=AssertionError("blocking run")):
                    t0 = time.monotonic()
                    kill(engine)
                    returned = time.monotonic() - t0
                self.assertEqual(engine.done, ["kill"], "killed by its handle")
                tk = _FakeTaskkill.started[-1]
                self.assertEqual(tk.argv, ["taskkill", "/F", "/T", "/PID", "4242"])
                for stream in ("stdin", "stdout", "stderr"):
                    self.assertIs(tk.kwargs[stream], subprocess.DEVNULL, stream)
                self.assertEqual(tk.kwargs["creationflags"], 0x08000000,
                                 "CREATE_NO_WINDOW")
                # taskkill has not finished (its wait blocks) and Stop has
                # returned: reaped on another thread, which holds the
                # engine's Popen (its handle: the PID stays reserved).
                self.assertLess(returned, 0.5, "Stop waited for taskkill")
                self.assertTrue(_wait_until(lambda: tk.waited_on))
                self.assertIsNot(tk.waited_on[0], threading.current_thread())
                self.assertIn(engine, tk.waited_on[0]._args)
                tk.release.set()
                tk.waited_on[0].join(5)
                self.assertFalse(tk.waited_on[0].is_alive())

    def test_windows_leaves_an_ended_process_alone(self):
        engine = _FakeEngine(running=False)
        self.kill_on("win32", engine)
        self.assertEqual((engine.done, _FakeTaskkill.started), ([], []))

    def test_windows_without_taskkill_still_killed_the_engine(self):
        engine = _FakeEngine()
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.object(ta.subprocess, "Popen",
                               side_effect=FileNotFoundError("taskkill")):
            sat._kill_proc(engine)
        self.assertEqual(engine.done, ["kill"])

    def test_posix_terminates_as_before(self):
        for plat in ("linux", "darwin"):
            with self.subTest(plat):
                engine = _FakeEngine()
                self.kill_on(plat, engine)
                self.assertEqual(engine.done, ["terminate"],
                                 "SIGTERM once; the second kill saw it ended")
                self.assertEqual(_FakeTaskkill.started, [])

    def test_a_real_process_dies_without_a_wait(self):
        """The real POSIX/Windows path on this machine: Stop returns at once
        and the engine is gone shortly after."""
        proc = _sleeper(self)
        t0 = time.monotonic()
        sat._kill_proc(proc)
        self.assertLess(time.monotonic() - t0, 0.5, "Stop blocked")
        self.assertIsNotNone(proc.wait(timeout=10))


if __name__ == "__main__":
    unittest.main()
