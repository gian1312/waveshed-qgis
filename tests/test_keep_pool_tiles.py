"""Map Converter "Keep pool tiles" (2026-10-23, default OFF).

The pool copy of a downloaded tile only pays off for a Site Analysis run
from the same XYZ source; otherwise it doubles the disk use of a
conversion. With the box off, the worker removes the pool tiles THIS run
downloaded — ``terrain_adapter.ensure_pool_tiles`` names them
(``PoolTiles.fetched``; ``present`` = already pooled) — with their ``.src``
/ ``.rebuild`` sidecars, after a SUCCESSFUL run only. Pinned with the
shipped worker, the real ``ensure_pool_tiles`` over a fake download and the
two-phase fake converter:

* off + success: exactly the run's pool tiles (and sidecars) go, the count
  and MB are logged; tiles pooled before the run and the raw source-tile
  cache (``<cache>/xyz``) are untouched;
* off + Stop, off + a failed batch: everything stays (the next run reuses
  it);
* on: everything stays;
* the estimate label says "(pool tiles removed at the end)" when off, and
  still counts the pool tiles (they exist while the run runs).
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

import waveshed.core.terrain_adapter as ta
from waveshed.core import output_estimate as oe
import waveshed.gui.map_converter_tab as mct
from waveshed.gui.map_converter_tab import _LayerEntry, _MapConverterWorker

from test_mc_pipeline import _xyz_only
from test_two_phase_convert import (_EXT, _HUGE, _XYZ, _Converter, _tab,
                                    _tiles)

#: The real downloader entry point (the runs below patch the module's).
_REAL_ENSURE = ta.ensure_pool_tiles

#: The QgsSettings stub the plugin actually resolves (pytest may load
#: conftest under a second module name, with its own class).
_QS = sys.modules["qgis.core"].QgsSettings


def _write_pool_tile(path, spec, short_by=0):
    """A whole (sparse) ``.abt`` for *spec*, as the converter leaves it."""
    size = int(spec["size_px"])
    stride = (size * 2 + 255) & ~255
    px = 1.0 / size
    with open(path, "wb") as fh:
        fh.write(b"AETH" + struct.pack("<HH", 1, size)
                 + struct.pack("<dddd", spec["ul_lat"], spec["ul_lon"], px,
                               px)
                 + struct.pack("<hH", 0, stride))
        fh.truncate(ta._abt_whole_bytes(size) - short_by)


class _FakeDownload:
    """``_try_rust_download`` stand-in: writes every tile it is asked for
    into the pool, a ``.rebuild`` flag on *flag* (degraded) and a ``.src``
    sidecar on *src* — what a download can leave beside a tile."""

    def __init__(self, flag=(), src=()):
        self.flag, self.src = set(flag), set(src)
        self.asked = []

    def __call__(self, source, pool_dir, _bbox, _res, todo, _bm, **_kw):
        for spec in todo:
            name = spec["filename"]
            self.asked.append(name)
            path = os.path.join(pool_dir, name)
            _write_pool_tile(path, spec)
            if name in self.flag:
                open(path + ta._REBUILD_SUFFIX, "w").close()
            elif os.path.exists(path + ta._REBUILD_SUFFIX):
                os.remove(path + ta._REBUILD_SUFFIX)    # as a clean pass
            if name in self.src:
                with open(path + ta._SOURCE_FP_SUFFIX, "w") as fh:
                    fh.write("fp")
        return ta.DownloadOutcome(True)


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
        with mock.patch.object(ta, "get_cache_dir", return_value=self.cache):
            self.pool = ta._pool_dir(_XYZ)
            self.raw = ta.xyz_cache_dir()
        os.makedirs(self.pool)
        # The raw source-tile cache, as a converter leaves it.
        self.pack = os.path.join(self.raw, "svc", "z12", "x1_y0.pack")
        os.makedirs(os.path.dirname(self.pack))
        with open(self.pack, "wb") as fh:
            fh.write(b"raw tiles")
        self.spec = {t["filename"]: t for _r, t in self.expected}

    def tearDown(self):
        self._tmp.cleanup()

    def pool_files(self):
        return sorted(os.listdir(self.pool))

    def preexisting(self, key):
        """A tile pooled by an earlier run (Site Analysis), with its .src."""
        name = self.n[key]
        path = os.path.join(self.pool, name)
        _write_pool_tile(path, self.spec[name])
        with open(path + ta._SOURCE_FP_SUFFIX, "w") as fh:
            fh.write("earlier run")
        return name

    def run_(self, keep=False, converter=None, download=None,
             on_worker=None):
        """The shipped worker over an XYZ-only stack (two 0.5 deg bands,
        every tile downloaded), the REAL ensure_pool_tiles over
        *download*."""
        download = download or _FakeDownload()
        converter = converter or _Converter()
        layers = [_LayerEntry(layer_type="raster", source_path="/src0",
                              extent=dict(_EXT))]
        entry = _xyz_only()[0]
        worker = _MapConverterWorker(layers, self.out, [30], ["/src0"],
                                     keep_pool_tiles=keep)
        rec = {"logs": [], "errs": [], "oks": [], "worker": worker,
               "download": download}
        worker.log_line.connect(rec["logs"].append)
        worker.finished_err.connect(rec["errs"].append)
        worker.finished_ok.connect(rec["oks"].append)
        if on_worker is not None:
            on_worker(worker)
        with mock.patch.object(_MapConverterWorker, "_resolve_raster",
                               return_value=entry), \
             mock.patch.object(mct, "find_binary", return_value="/bin/conv"), \
             mock.patch.object(mct, "_plan_cross_check"), \
             mock.patch.object(mct, "_converter_reports_tiles",
                               return_value=True), \
             mock.patch.object(ta, "preflight_ingest_jobs"), \
             mock.patch.object(ta, "get_cache_dir", return_value=self.cache), \
             mock.patch.object(ta, "ensure_pool_tiles",
                               side_effect=_REAL_ENSURE), \
             mock.patch.object(ta, "_try_rust_download",
                               side_effect=download), \
             mock.patch.object(oe, "free_bytes", return_value=_HUGE), \
             mock.patch.object(ta, "run_converter_streaming",
                               side_effect=converter), \
             mock.patch.object(tempfile, "tempdir", self.temp):
            worker.run()
        return rec


# ---------------------------------------------------------------------------
# ensure_pool_tiles names what it fetched
# ---------------------------------------------------------------------------

class PoolTilesReport(_Case):

    def test_fetched_versus_present(self):
        specs = [t for _r, t in self.expected]
        a, b, c, d = (self.n[k] for k in "ABCD")
        self.preexisting("A")                         # whole: present
        _write_pool_tile(os.path.join(self.pool, b), self.spec[b])
        open(os.path.join(self.pool, b + ta._REBUILD_SUFFIX), "w").close()
        _write_pool_tile(os.path.join(self.pool, c), self.spec[c],
                         short_by=1000)               # a killed run's file
        download = _FakeDownload()
        with mock.patch.object(ta, "get_cache_dir", return_value=self.cache), \
             mock.patch.object(ta, "_try_rust_download",
                               side_effect=download):
            got = ta.ensure_pool_tiles(_XYZ, specs, 30)
        self.assertIsInstance(got, dict)              # every caller's view
        self.assertEqual(got, {n: os.path.join(os.path.abspath(self.pool), n)
                               for n in (a, b, c, d)})
        self.assertEqual(got.fetched, sorted([b, c, d]))
        self.assertEqual(got.present, [a])
        self.assertEqual(sorted(download.asked), sorted([b, c, d]))
        self.assertEqual(got.pool_dir, os.path.abspath(self.pool))
        # A second call finds them all: nothing fetched.
        with mock.patch.object(ta, "get_cache_dir", return_value=self.cache), \
             mock.patch.object(ta, "_try_rust_download",
                               side_effect=AssertionError("downloaded")):
            again = ta.ensure_pool_tiles(_XYZ, specs, 30)
        self.assertEqual(again.fetched, [])
        self.assertEqual(again.present, sorted([a, b, c, d]))

    def test_remove_pool_tiles_takes_exactly_the_names_and_sidecars(self):
        a, b = self.n["A"], self.n["B"]
        keep = self.preexisting("C")
        for name in (a, b):
            _write_pool_tile(os.path.join(self.pool, name), self.spec[name])
        open(os.path.join(self.pool, a + ta._REBUILD_SUFFIX), "w").close()
        with open(os.path.join(self.pool, b + ta._SOURCE_FP_SUFFIX), "w") as fh:
            fh.write("x")
        size = os.path.getsize(os.path.join(self.pool, a))
        got = ta.remove_pool_tiles(self.pool, [a, b, "never_there.abt"])
        self.assertEqual(got, ta.PoolRemoval(2, 2 * size, 0))
        self.assertEqual(self.pool_files(),
                         [keep, keep + ta._SOURCE_FP_SUFFIX])
        self.assertTrue(os.path.isfile(self.pack))

    def test_a_tile_still_linked_from_a_view_frees_nothing(self):
        a = self.n["A"]
        path = os.path.join(self.pool, a)
        _write_pool_tile(path, self.spec[a])
        view = os.path.join(self.root, "views", "v1")
        os.makedirs(view)
        os.link(path, os.path.join(view, a))
        got = ta.remove_pool_tiles(self.pool, [a])
        self.assertEqual(got, ta.PoolRemoval(1, 0, 1))
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.isfile(os.path.join(view, a)))


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------

class KeepPoolTilesOff(_Case):

    def test_success_removes_exactly_the_runs_pool_tiles(self):
        a, b, c, d = (self.n[k] for k in "ABCD")
        before = self.preexisting("A")
        download = _FakeDownload(flag=[b], src=[c])
        rec = self.run_(download=download)
        self.assertEqual(rec["errs"], [])
        self.assertEqual(rec["oks"], [self.out])
        # The run downloaded B C D (A was pooled) and converted all four.
        self.assertEqual(sorted(download.asked), sorted([b, c, d]))
        self.assertEqual(sorted(n for n in os.listdir(self.out)
                                if n.endswith(".abt")), sorted([a, b, c, d]))
        # Gone: B C D with their sidecars. Kept: A and its .src, the raw
        # source-tile cache.
        self.assertEqual(self.pool_files(),
                         [before, before + ta._SOURCE_FP_SUFFIX])
        self.assertTrue(os.path.isfile(self.pack))
        with open(self.pack, "rb") as fh:
            self.assertEqual(fh.read(), b"raw tiles")
        mb = 3 * ta._abt_whole_bytes(self.spec[b]["size_px"]) / 1024 ** 2
        removed = [m for m in rec["logs"] if m.startswith("Removed ")]
        self.assertEqual(removed, [
            f"Removed 3 pool tile(s) this run downloaded ({mb:,.0f} MB "
            f"freed) — tick \"Keep pool tiles\" to keep them for Site "
            f"Analysis runs from the same source; the downloaded source "
            f"tiles stay cached"])
        self.assertIn("  the pool tiles this run downloads are removed once "
                      "it has succeeded (\"Keep pool tiles\" is off)",
                      rec["logs"])
        # After the conversion summary, before the run reports success.
        logs = rec["logs"]
        self.assertGreater(logs.index(removed[0]),
                           next(i for i, m in enumerate(logs)
                                if m.startswith("Conversion complete: ")))

    def test_stop_keeps_them(self):
        def on_batch(job_file):
            converter.worker.cancel()
            return -15                     # killed: nothing written

        from test_mc_pipeline import _Recorder
        converter = _Recorder(on_batch=on_batch)
        rec = self.run_(converter=converter,
                        on_worker=lambda w: setattr(converter, "worker", w))
        self.assertEqual((rec["errs"], rec["oks"]), ([], []))
        self.assertIn("Cancelled by user.", rec["logs"])
        downloaded = rec["download"].asked
        self.assertTrue(downloaded)
        self.assertTrue(set(downloaded) <= set(self.pool_files()),
                        (downloaded, self.pool_files()))
        self.assertFalse([m for m in rec["logs"] if m.startswith("Removed ")])

    def test_a_failure_keeps_them(self):
        from test_mc_pipeline import _Recorder
        converter = _Recorder(on_batch=lambda _job: 1)   # the ingest fails
        rec = self.run_(converter=converter)
        self.assertEqual(len(rec["errs"]), 1)
        self.assertIn("Converter failed (exit code 1)", rec["errs"][0])
        downloaded = rec["download"].asked
        self.assertTrue(downloaded)
        self.assertTrue(set(downloaded) <= set(self.pool_files()))
        self.assertFalse([m for m in rec["logs"] if m.startswith("Removed ")])


class KeepPoolTilesOn(_Case):

    def test_everything_stays(self):
        before = self.preexisting("A")
        rec = self.run_(keep=True)
        self.assertEqual(rec["errs"], [])
        fetched = sorted(rec["download"].asked)
        self.assertEqual(len(fetched), 3)
        self.assertEqual(self.pool_files(),
                         sorted([before, before + ta._SOURCE_FP_SUFFIX]
                                + fetched))
        self.assertFalse([m for m in rec["logs"] if m.startswith("Removed ")])
        self.assertFalse([m for m in rec["logs"] if "Keep pool tiles" in m])
        self.assertTrue(os.path.isfile(self.pack))


# ---------------------------------------------------------------------------
# The box, its setting, the estimate label
# ---------------------------------------------------------------------------

class TheBoxAndTheLabel(unittest.TestCase):

    def setUp(self):
        self._saved = dict(_QS._store)
        _QS._store.pop(mct._KEEP_POOL_KEY, None)

    def tearDown(self):
        _QS._store.clear()
        _QS._store.update(self._saved)

    def test_setting_default_off_and_saved_on_toggle(self):
        self.assertFalse(mct._keep_pool_tiles_setting())
        tab = mct.MapConverterTab.__new__(mct.MapConverterTab)
        tab._update_estimate = mock.Mock()
        self.assertFalse(tab._keep_pool_tiles())       # no box: the setting
        tab._on_keep_pool_toggled(True)
        self.assertIs(_QS._store[mct._KEEP_POOL_KEY], True)
        self.assertTrue(mct._keep_pool_tiles_setting())
        tab._update_estimate.assert_called_once()
        _QS._store[mct._KEEP_POOL_KEY] = "false"       # as QSettings reads
        self.assertFalse(mct._keep_pool_tiles_setting())
        tab._chk_keep_pool = mock.Mock(isChecked=mock.Mock(return_value=True))
        self.assertTrue(tab._keep_pool_tiles())
        self.assertIn("doubles the disk use", mct._KEEP_POOL_TIP)
        self.assertIn("Site Analysis", mct._KEEP_POOL_TIP)

    def test_the_label_says_removed_at_the_end_when_off(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(ta, "get_cache_dir", return_value=d), \
             mock.patch.object(oe, "free_bytes", return_value=_HUGE):
            tab = _tab(os.path.join(d, "out"), [_LayerEntry(
                layer_type="raster", source_path=_XYZ, extent=dict(_EXT))])
            tab._update_estimate()
            tab.flush_estimate()         # the estimate thread's work, inline
            off = tab._lbl_estimate.text
            tab._chk_keep_pool = mock.Mock(
                isChecked=mock.Mock(return_value=True))
            tab._update_estimate()
            tab.flush_estimate()
            on = tab._lbl_estimate.text
        self.assertIn("terrain pool", off)
        self.assertIn(") (pool tiles removed at the end)", off)
        self.assertNotIn("removed at the end", on)
        # The need itself is the same: the pool tiles exist during the run.
        strip = off.replace(" (pool tiles removed at the end)", "")
        self.assertEqual(strip, on)

    def test_the_worker_gets_the_box(self):
        captured = {}

        class _W:
            def __init__(self, *a, **kw):
                captured.update(kw)
                for name in ("progress", "status", "log_line", "finished_ok",
                             "finished_err", "finished"):
                    setattr(self, name, mock.Mock())

            def start(self):
                pass

        with tempfile.TemporaryDirectory() as d:
            tab = _tab(os.path.join(d, "out"), [_LayerEntry(
                layer_type="raster", source_path="/dem/a.tif",
                extent=dict(_EXT))])
            tab._btn_run = mock.Mock()
            tab._btn_cancel = mock.Mock()
            tab._chk_keep_pool = mock.Mock(
                isChecked=mock.Mock(return_value=True))
            with mock.patch.object(mct, "binaries_warning", return_value=None), \
                 mock.patch.object(mct, "_MapConverterWorker", _W), \
                 mock.patch.object(mct, "_estimate_disk_need",
                                   return_value=None), \
                 mock.patch.object(mct, "_pending_render_jobs",
                                   return_value=(None, [])), \
                 mock.patch.object(mct, "resolve_sources_with_progress",
                                   return_value=["/dem/a.tif"]):
                tab._on_run()
        self.assertIs(captured.get("keep_pool_tiles"), True)


if __name__ == "__main__":
    unittest.main()
