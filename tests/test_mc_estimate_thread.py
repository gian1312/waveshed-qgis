"""The Map Converter's estimate label off the GUI thread (owner, 2026-11-07:
"decouple the checkboxes from the computation; they lag until the size
estimate is done").

Every "Generate tiles at" toggle — and every row added or removed, extent
set, "Keep pool tiles" / "Rebuild existing tiles" toggle — used to run
``_estimate_tile_count_and_mb`` and ``_estimate_disk_need`` on the GUI
thread (43,650 swissALTI3D files + an XYZ layer, 2,438 tiles: 0.3-3.7 s per
toggle in the container on a synthetic copy of that stack, more on the
owner's HDD). Pinned here:

* a request answers what needs no file at once and otherwise only sets
  "Estimating…" and (re)starts the single-shot 150 ms debounce timer — the
  heavy functions are never called inside it, its wall time is bounded;
* the timer takes the inputs as PLAIN values (``_plain_entry``: the QGIS
  layer as its source string, extent / decisions / CRS answers copied) and
  runs both estimates in ONE ``_EstimateThread`` per tab; at most one runs
  and one waits, a newer request replaces the waiting one and tells the
  running one to stop;
* a result is shown only while its generation is the latest — stale ones are
  dropped; a burst of requests makes one estimate;
* a failure is the ``disk estimate failed: …`` warning, never raised;
* ``_est_cache`` is touched under ``_est_lock`` (the thread and ``_on_run``);
* ``flush_estimate()`` is the synchronous hook (the stub QThread never runs).
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import waveshed.core.terrain_adapter as ta
from waveshed.core import output_estimate as oe
import waveshed.gui.map_converter_tab as mct
from waveshed.gui.map_converter_tab import _LayerEntry
from test_two_phase_convert import _EXT, _HUGE, _XYZ, _tab, _xyz_layer

GB = 1024 ** 3


class _Heavy:
    """Stand-ins for the two estimates: count the calls, note the thread and
    the resolutions, take *delay* seconds."""

    def __init__(self, delay=0.0, need=None):
        self.delay = delay
        self.count_calls = []
        self.need_calls = []
        self.need = need
        self.lock_held = []

    def count(self, bbox, res, output_dir="", overwrite=True, entries=None):
        self.count_calls.append((threading.get_ident(), list(res)))
        time.sleep(self.delay)
        return 7 * len(res), 1000 * len(res)

    def disk_need(self, layers, res, output_dir, overwrite, tmp_dir=None,
                  cache=None, should_cancel=None):
        self.need_calls.append((threading.get_ident(), list(res)))
        time.sleep(self.delay)
        return self.need

    def patch(self):
        return mock.patch.multiple(
            mct, _estimate_tile_count_and_mb=self.count,
            _estimate_disk_need=self.disk_need)

    @property
    def calls(self):
        return len(self.count_calls) + len(self.need_calls)


def _estimating(tab):
    return tab._lbl_estimate.text == mct._ESTIMATING_TEXT


class _Base(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ws_estthread_")
        self.out = os.path.join(self._tmp.name, "out")
        self.res = [30]
        self.tab = _tab(self.out, [_xyz_layer()])
        self.tab._get_output_resolutions = lambda: list(self.res)
        self.tab._chk_keep_pool = mock.Mock(
            isChecked=mock.Mock(return_value=True))
        # A timer of its own per tab (the stub QTimer is ONE shared mock).
        patcher = mock.patch.object(mct, "QTimer",
                                    side_effect=lambda *_a: mock.MagicMock())
        patcher.start()
        self.addCleanup(patcher.stop)
        mct._LIVE_ESTIMATES.clear()

    def tearDown(self):
        mct._LIVE_ESTIMATES.clear()
        self._tmp.cleanup()


class ARequestNeverComputes(_Base):

    def test_a_toggle_returns_before_the_estimate(self):
        # Each stand-in estimate takes 1 s while the GUI side is timed: a
        # handler that computed (or waited) would take >= 2 s. The bound is
        # 0.5 s, not tighter — inside the full suite a gen-2 GC pass / GIL
        # hand-off once made this ~0.2 ms handler measure 155 ms.
        heavy = _Heavy(delay=1.0)
        with heavy.patch():
            t0 = time.perf_counter()
            self.tab._on_res_toggled(2)          # a box ticked by hand
            handler_s = time.perf_counter() - t0
            self.assertEqual(heavy.calls, 0)
            self.assertTrue(_estimating(self.tab))
            self.assertTrue(self.tab._res_user_touched)
            # The debounce: a single shot of 150 ms, (re)started.
            timer = self.tab._est_timer
            timer.setSingleShot.assert_called_with(True)
            timer.setInterval.assert_called_with(mct._ESTIMATE_DEBOUNCE_MS)
            self.assertEqual(mct._ESTIMATE_DEBOUNCE_MS, 150)
            timer.start.assert_called_once_with()
            # The timer firing starts the thread — still nothing computed
            # on this thread.
            t0 = time.perf_counter()
            self.tab._on_estimate_timer()
            timer_s = time.perf_counter() - t0
            self.assertEqual(heavy.calls, 0)
            thread = self.tab._est_thread
            self.assertIsInstance(thread, mct._EstimateThread)
            self.assertIn(thread, mct._LIVE_ESTIMATES)
            self.assertTrue(_estimating(self.tab))
            # The thread does the work (here run by hand: the stub QThread
            # never starts one).
            heavy.delay = 0.0
            thread.run()
            thread.finished.emit()
        self.assertLess(handler_s, 0.5)
        self.assertLess(timer_s, 0.5)
        self.assertEqual([r for _t, r in heavy.count_calls], [[30]])
        self.assertEqual([r for _t, r in heavy.need_calls], [[30]])
        self.assertIn(".abt tiles: 7 | Est. disk: 1,000 MB",
                      self.tab._lbl_estimate.text)
        self.assertNotIn(thread, mct._LIVE_ESTIMATES)
        self.assertIsNone(self.tab._est_running)
        self.assertIsNone(thread.job, "the thread keeps no job")

    def test_the_gui_thread_never_waits_for_a_running_estimate(self):
        """With a real second thread under the stub QThread: the toggle and
        the timer return at once while the estimate takes 1 s elsewhere (a
        GUI side that waited would take >= 1 s; bound 0.5 s, see above); its
        result then fills the label."""
        heavy = _Heavy(delay=0.5)
        done = threading.Event()

        def start(thread):
            def go():
                thread.run()
                thread.finished.emit()
                done.set()
            threading.Thread(target=go, daemon=True).start()

        with heavy.patch(), mock.patch.object(mct._EstimateThread, "start",
                                              start):
            t0 = time.perf_counter()
            self.tab._on_res_toggled(2)
            self.tab._on_estimate_timer()
            gui_s = time.perf_counter() - t0
            self.assertTrue(_estimating(self.tab))
            self.assertTrue(done.wait(10))
        self.assertLess(gui_s, 0.5)
        main = threading.get_ident()
        self.assertTrue(all(t != main for t, _r in heavy.count_calls
                            + heavy.need_calls))
        self.assertIn(".abt tiles: 7 |", self.tab._lbl_estimate.text)

    def test_what_needs_no_file_is_answered_at_once(self):
        heavy = _Heavy()
        with heavy.patch():
            self.tab._update_estimate()
            self.tab._on_estimate_timer()           # job 1 running
            job1 = self.tab._est_running
            self.res = []
            self.tab._update_estimate()
            self.assertEqual(self.tab._lbl_estimate.text,
                             "Select at least one output resolution.")
            self.assertTrue(job1.superseded.is_set())
            self.assertFalse(self.tab._est_requested)
            self.tab._est_timer.stop.assert_called()
            self.res = [30]
            self.tab._layers.append(_LayerEntry(
                layer_type="raster", source_path="/dem/a.tif"))
            self.tab._update_estimate()
            self.assertEqual(
                self.tab._lbl_estimate.text,
                "1 layer(s) have no extent set. Select a layer and click "
                "'Draw Extent' or 'Extent from Layer'.")
            self.tab._layers = [_LayerEntry(layer_type="buildings",
                                            source_path="/b.fgb")]
            self.tab._update_estimate()
            self.assertEqual(
                self.tab._lbl_estimate.text,
                "Add raster layers and set extents to see estimate.")
            # Job 1's late answer is dropped.
            self.tab._est_thread.run()
            self.tab._est_thread.finished.emit()
        self.assertEqual(self.tab._lbl_estimate.text,
                         "Add raster layers and set extents to see estimate.")
        self.assertEqual(heavy.calls, 1, "job 1 stopped after its count")


class StaleAndPending(_Base):

    def test_stale_results_are_discarded(self):
        heavy = _Heavy()
        with heavy.patch():
            self.tab._update_estimate()
            self.tab._on_estimate_timer()
            thread = self.tab._est_thread
            job1 = thread.job
            self.assertEqual(job1.out_res, [30])
            # The user ticks 2 m while job 1 runs: job 1 is stale and told
            # to stop; job 2 waits for it.
            self.res = [2, 30]
            self.tab._on_res_toggled(2)
            self.assertTrue(job1.superseded.is_set())
            self.tab._on_estimate_timer()
            self.assertIs(thread.job, job1)
            self.assertEqual(self.tab._est_pending.out_res, [2, 30])
            thread.run()                      # job 1 ends: nothing shown
            self.assertTrue(_estimating(self.tab))
            thread.finished.emit()            # job 2 starts, same thread
            self.assertIs(self.tab._est_thread, thread)
            self.assertEqual(thread.job.out_res, [2, 30])
            self.assertIsNone(self.tab._est_pending)
            thread.run()
            thread.finished.emit()
        self.assertIn(".abt tiles: 14 |", self.tab._lbl_estimate.text)
        self.assertEqual([r for _t, r in heavy.need_calls], [[2, 30]])

    def test_a_complete_but_stale_result_is_never_shown(self):
        heavy = _Heavy()
        with heavy.patch():
            self.tab._update_estimate()
            old = self.tab._estimate_job()
            self.res = [2, 30]
            self.tab._update_estimate()       # newer request
            self.tab._on_estimated(mct._run_estimate(old))
            self.assertTrue(_estimating(self.tab))
            self.tab.flush_estimate()
        self.assertIn(".abt tiles: 14 |", self.tab._lbl_estimate.text)

    def test_a_newer_request_replaces_the_pending_one(self):
        heavy = _Heavy()
        with heavy.patch():
            self.tab._update_estimate()
            self.tab._on_estimate_timer()
            thread = self.tab._est_thread
            self.res = [5, 30]
            self.tab._update_estimate()
            self.tab._on_estimate_timer()
            first_pending = self.tab._est_pending
            self.res = [10, 30]
            self.tab._update_estimate()
            self.assertIsNone(self.tab._est_pending, "dropped: stale")
            self.tab._on_estimate_timer()
            self.assertEqual(self.tab._est_pending.out_res, [10, 30])
            self.assertIsNot(self.tab._est_pending, first_pending)
            thread.run()
            thread.finished.emit()
            thread.run()
            thread.finished.emit()
        self.assertEqual([r for _t, r in heavy.need_calls], [[10, 30]])
        self.assertIn(".abt tiles: 14 |", self.tab._lbl_estimate.text)

    def test_a_stale_pending_job_is_never_started(self):
        heavy = _Heavy()
        with heavy.patch():
            self.tab._update_estimate()
            self.tab._on_estimate_timer()
            thread = self.tab._est_thread
            self.res = [5, 30]
            self.tab._update_estimate()
            self.tab._on_estimate_timer()     # pending: [5, 30]
            stale = self.tab._est_pending
            self.res = [90]
            self.tab._update_estimate()       # timer restarted, not fired
            self.tab._est_pending = stale     # (as if set after the call)
            thread.run()
            thread.finished.emit()            # stale pending: not started
            self.assertIsNone(self.tab._est_running)
            self.tab._on_estimate_timer()
            thread.run()
            thread.finished.emit()
        self.assertEqual([r for _t, r in heavy.need_calls], [[90]])

    def test_rapid_toggles_make_one_estimate(self):
        heavy = _Heavy()
        with heavy.patch():
            for res in ([2], [2, 5], [2, 5, 10], [2, 10], [2, 30, 90]):
                self.res = res
                self.tab._on_res_toggled(2)
            self.assertEqual(heavy.calls, 0)
            self.assertEqual(self.tab._est_timer.start.call_count, 5)
            self.tab._on_estimate_timer()     # the one debounce that ends
            self.tab._on_estimate_timer()     # nothing requested any more
            self.tab.flush_estimate()         # nor here
            thread = self.tab._est_thread
            thread.run()
            thread.finished.emit()
        self.assertEqual([r for _t, r in heavy.count_calls], [[2, 30, 90]])
        self.assertEqual([r for _t, r in heavy.need_calls], [[2, 30, 90]])


class FailuresAreWarnings(_Base):

    def _log(self):
        return mock.patch.object(mct.QgsMessageLog, "logMessage")

    def test_a_disk_need_failure_is_the_warning(self):
        def boom(*_a, **_kw):
            raise RuntimeError("boom")
        heavy = _Heavy()
        with heavy.patch(), mock.patch.object(mct, "_estimate_disk_need",
                                              boom), self._log() as log:
            self.tab._update_estimate()
            self.tab.flush_estimate()
        log.assert_called_once_with("disk estimate failed: boom", mct.TAG,
                                    mct.Qgis.MessageLevel.Warning)
        text = self.tab._lbl_estimate.text
        self.assertIn(".abt tiles: 7 | Est. disk: 1,000 MB", text)
        self.assertNotIn("<br>", text)

    def test_a_count_failure_is_the_warning_and_said(self):
        def boom(*_a, **_kw):
            raise ValueError("kaboom")
        heavy = _Heavy()
        with heavy.patch(), mock.patch.object(
                mct, "_estimate_tile_count_and_mb", boom), \
                self._log() as log:
            self.tab._update_estimate()
            self.tab._on_estimate_timer()
            self.tab._est_thread.run()        # raises nothing
            self.tab._est_thread.finished.emit()
        log.assert_called_once_with("disk estimate failed: kaboom", mct.TAG,
                                    mct.Qgis.MessageLevel.Warning)
        self.assertEqual(self.tab._lbl_estimate.text,
                         "Estimate failed: kaboom")

    def test_the_thread_survives_anything(self):
        with mock.patch.object(mct, "_run_estimate",
                               side_effect=MemoryError()), self._log() as log:
            self.tab._update_estimate()
            self.tab._on_estimate_timer()
            self.tab._est_thread.run()
            self.tab._est_thread.finished.emit()
        log.assert_called_once_with("disk estimate failed: MemoryError",
                                    mct.TAG, mct.Qgis.MessageLevel.Warning)
        self.assertEqual(self.tab._lbl_estimate.text,
                         "Estimate failed: MemoryError")
        self.assertIsNone(self.tab._est_running)

    def test_the_inline_run_reports_like_the_thread(self):
        def boom(*_a, **_kw):
            raise ValueError("kaboom")
        with mock.patch.object(mct, "_estimate_tile_count_and_mb", boom), \
                mock.patch.object(mct, "_estimate_disk_need",
                                  side_effect=OSError("disk gone")), \
                self._log() as log:
            self.tab._update_estimate()
            self.tab.flush_estimate()         # raises nothing
        self.assertEqual([c.args[0] for c in log.call_args_list],
                         ["disk estimate failed: kaboom",
                          "disk estimate failed: disk gone"])
        self.assertEqual(self.tab._lbl_estimate.text,
                         "Estimate failed: kaboom")
        with mock.patch.object(mct, "_run_estimate",
                               side_effect=KeyError("x")), self._log():
            self.tab._update_estimate()
            self.tab.flush_estimate()
        self.assertEqual(self.tab._lbl_estimate.text,
                         "Estimate failed: 'x'")


class PlainInputsAndTheLock(_Base):

    def test_the_thread_reads_plain_values_only(self):
        layer = mock.Mock()
        layer.source.return_value = _XYZ
        row = _LayerEntry(layer_type="raster", source_path=_XYZ,
                          qgis_layer=layer, extent=dict(_EXT))
        folder_path = os.path.join(self._tmp.name, "dem")
        os.makedirs(folder_path)
        folder = _LayerEntry(layer_type="raster", source_path=folder_path,
                             extent=dict(_EXT))
        folder.decisions = {"/dem/folder/a.tif": "skip"}
        self.tab._layers = [row, folder]
        self.tab._update_estimate()
        job = self.tab._estimate_job()
        self.assertEqual(layer.source.call_count, 1)
        plain = job.layers[0]
        self.assertIsInstance(plain.qgis_layer, mct._PlainLayer)
        self.assertEqual(plain.qgis_layer.source(), _XYZ)
        self.assertEqual(mct._entry_kind(plain), "xyz")
        # Copies: the rows may change while the thread reads.
        row.extent["south"] = 0.0
        folder.decisions["/dem/folder/b.tif"] = "use"
        self.assertEqual(plain.extent, _EXT)
        self.assertEqual(job.layers[1].decisions,
                         {"/dem/folder/a.tif": "skip"})
        self.assertEqual(job.layers[1].overlap_priority,
                         mct.terrain_index.PRIORITY_FINEST)
        self.assertEqual(job.combined, dict(_EXT))
        with mock.patch.object(ta, "get_cache_dir",
                               return_value=self._tmp.name), \
                mock.patch.object(oe, "free_bytes", return_value=_HUGE):
            result = mct._run_estimate(job)
        self.assertEqual(result.errors, [])
        self.assertEqual(layer.source.call_count, 1,
                         "the QGIS layer was read by the estimate thread")

    def test_a_deleted_layer_is_the_disk_warning_as_before(self):
        layer = mock.Mock()
        layer.source.side_effect = RuntimeError(
            "wrapped C/C++ object of type QgsRasterLayer has been deleted")
        self.tab._layers = [_LayerEntry(layer_type="raster",
                                        source_path=_XYZ, qgis_layer=layer,
                                        extent=dict(_EXT))]
        with mock.patch.object(ta, "get_cache_dir",
                               return_value=self._tmp.name), \
                mock.patch.object(mct.QgsMessageLog, "logMessage") as log:
            self.tab._update_estimate()
            self.tab.flush_estimate()
        self.assertEqual(log.call_count, 1)
        self.assertIn("has been deleted", log.call_args[0][0])
        self.assertIn(".abt tiles: 4 |", self.tab._lbl_estimate.text)

    def test_the_cache_is_touched_under_the_lock(self):
        held = []

        def need(layers, res, output_dir, overwrite, tmp_dir=None,
                 cache=None, should_cancel=None):
            held.append((self.tab._est_lock.locked(),
                         cache is self.tab._est_cache))
            return None
        with mock.patch.object(mct, "_estimate_disk_need", need):
            self.tab._update_estimate()
            self.tab._on_estimate_timer()
            self.tab._est_thread.run()
            self.tab._est_thread.finished.emit()
            # _on_run prices on the GUI thread with the same cache.
            with mock.patch.object(mct, "resolve_sources_with_progress",
                                   return_value=None):
                self.tab._on_run()
        self.assertEqual(held, [(True, True), (True, True)])
        self.assertFalse(self.tab._est_lock.locked())

    def test_a_superseded_estimate_stops_at_its_next_poll(self):
        polls = []

        def need(layers, res, output_dir, overwrite, tmp_dir=None,
                 cache=None, should_cancel=None):
            polls.append(should_cancel())
            self.tab._update_estimate()       # a newer request meanwhile
            polls.append(should_cancel())
            mct._stop_if_cancelled(should_cancel)
            raise AssertionError("not stopped")
        with mock.patch.object(mct, "_estimate_disk_need", need):
            self.tab._update_estimate()
            self.tab._on_estimate_timer()
            self.tab._est_thread.run()
        self.assertEqual(polls, [False, True])
        self.assertTrue(_estimating(self.tab))

    def test_closing_takes_no_more_requests(self):
        self.tab._rect_tool = None
        self.tab._update_estimate()
        self.tab._on_estimate_timer()
        job = self.tab._est_running
        self.tab._update_estimate()
        self.tab.cancel_worker()
        self.assertTrue(job.superseded.is_set())
        self.assertFalse(self.tab._est_requested)
        self.assertIsNone(self.tab._est_pending)
        self.tab._est_timer.stop.assert_called()
        # The running thread stays referenced until it ends.
        self.assertIn(self.tab._est_thread, mct._LIVE_ESTIMATES)


class TheRealLabel(_Base):
    """Through the real estimates: the label is the one the GUI thread
    used to compute, number for number."""

    def test_same_text_as_before(self):
        with mock.patch.object(ta, "get_cache_dir",
                               return_value=self._tmp.name), \
                mock.patch.object(oe, "free_bytes", return_value=_HUGE):
            self.tab._update_estimate()
            self.tab.flush_estimate()
            text = self.tab._lbl_estimate.text
            combined = mct._union_extent(self.tab._layers)
            n, mb = mct._estimate_tile_count_and_mb(
                combined, [30], output_dir=self.out, overwrite=False,
                entries=self.tab._layers)
            _n, needs = mct._estimate_disk_need(
                self.tab._layers, [30], self.out, False, cache={})
        expected = (f'<span style="color:black">.abt tiles: {n:,} | '
                    f"Est. disk: {mb:,} MB ({mb / 1024:.1f} GB)</span>")
        for need in needs:
            if need.need:
                expected += (f'<br><span style="color:black">'
                             f"{oe.format_drive_need(need)}</span>")
        self.assertEqual(text, expected)


class Wiring(unittest.TestCase):

    def test_boxes_and_buttons_reach_the_estimate(self):
        tab = mct.MapConverterTab.__new__(mct.MapConverterTab)
        for name in ("_btn_add_layer", "_btn_add_all_layers",
                     "_btn_extent_all", "_table", "_btn_add_folder",
                     "_btn_add_buildings", "_btn_download_buildings",
                     "_btn_up", "_btn_down", "_btn_remove",
                     "_btn_draw_extent", "_btn_layer_extent", "_btn_browse",
                     "_btn_run", "_btn_cancel", "_btn_inspect",
                     "_btn_auto_res", "_chk_keep_pool", "_chk_overwrite",
                     "_combo_overlap"):
            setattr(tab, name, mock.Mock())
        tab._res_checks = {r: mock.Mock() for r in (2, 30)}
        tab._connect_signals()
        tab._connect_res_checks()
        tab._chk_overwrite.toggled.connect.assert_called_once_with(
            tab._update_estimate)
        tab._btn_auto_res.clicked.connect.assert_called_once_with(
            tab._on_auto_resolutions)
        for cb in tab._res_checks.values():
            cb.stateChanged.connect.assert_called_once_with(
                tab._on_res_toggled)


if __name__ == "__main__":
    unittest.main()
