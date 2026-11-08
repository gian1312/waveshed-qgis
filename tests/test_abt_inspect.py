"""Map Converter → Inspect .abt (2026-10-23): fast and off the GUI thread.

The old Inspect mosaicked EVERY tile of the folder at the FINEST resolution
into a 16,384² canvas, synchronously on the GUI thread, and tiles of
different resolutions overwrote each other in file order. Pinned here:

* the resolutions of a folder come from the header index (no tile body is
  read), with tile counts, bytes and the share of the folder's extent each
  covers; the default is the COARSEST resolution covering >= 95 % of the
  union of all tiles' extents (none does → the coarsest first until
  together they do);
* only the chosen tiles are read, and only the rows the canvas samples —
  the same canvas a whole read gives;
* several resolutions: painted coarse first, so the finest wins whatever
  the file order;
* the GeoTIFF is tiled + DEFLATE with internal AVERAGE overviews 2-32 and
  keeps its no-data (real GDAL); Cancel while the overviews build leaves
  no file;
* the dialog's note warns when the chosen tiles hold more than 2 GB;
* ``MapConverterTab._on_inspect_abt`` runs the header index and the mosaic
  in ``terrain_folder_scan.TaskThread``s (with the conftest stubs and a
  real thread), never on the calling thread.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import os
import struct
import sys
import tempfile
import threading
import unittest
from unittest import mock

import numpy as np

from waveshed.core import abt, abt_index, abt_inspect
import waveshed.gui.map_converter_tab as mct
from waveshed.gui import terrain_folder_scan as tfs

from _real_gdal import GDAL, HAVE_GDAL, real_gdal

DEG_PER_M = 1.0 / 111_111.0


def _write(path, size, ul_lat, ul_lon, res_m, value=None, array=None):
    """A synthetic ``.abt`` tile: *size*² px at *res_m* (its pixel step in
    degrees), every pixel *value* metres (or *array*, metres)."""
    px = res_m * DEG_PER_M
    stride = (size * 2 + 255) & ~255
    if array is None:
        array = np.full((size, size), value if value is not None else 0.0)
    counts = np.round(np.asarray(array, dtype=np.float64)
                      / abt.ELEV_STEP_M).astype("<i2")
    body = np.zeros((size, stride), dtype=np.uint8)
    body[:, :size * 2] = counts.view(np.uint8).reshape(size, size * 2)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<4sHHddddhH", b"AETH", 1, size, ul_lat, ul_lon,
                             px, px, 0, stride))
        fh.write(body.tobytes())
    return path


def _span(size, res_m):
    return size * res_m * DEG_PER_M


class _Folder(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self._tmp.name, "abt")
        os.makedirs(self.dir)
        self.cache = os.path.join(self._tmp.name, "cache")
        os.makedirs(self.cache)
        patcher = mock.patch("waveshed.core.terrain_index.index_dir",
                             return_value=os.path.join(self.cache, "index"))
        patcher.start()
        self.addCleanup(patcher.stop)
        abt_index.forget()
        self.addCleanup(abt_index.forget)

    def tearDown(self):
        self._tmp.cleanup()

    def grid(self, res_m, nx, ny, size=20, north=47.5, west=8.0, value=0.0,
             prefix=None):
        """*nx* × *ny* tiles of *res_m* from (north, west), eastwards and
        southwards."""
        span = _span(size, res_m)
        paths = []
        for j in range(ny):
            for i in range(nx):
                name = f"{prefix or 'tile'}_{res_m}m_{j}_{i}.abt"
                paths.append(_write(os.path.join(self.dir, name), size,
                                    north - j * span, west + i * span,
                                    res_m, value))
        return paths

    def plan(self):
        plan = abt_inspect.plan_folder(self.dir)
        self.assertIsNotNone(plan)
        return plan


# ---------------------------------------------------------------------------
# The choice: groups from the header index, the default rule
# ---------------------------------------------------------------------------

class ResolutionChoice(_Folder):

    def test_groups_counts_and_bytes_without_reading_a_tile(self):
        self.grid(30, 4, 4, size=40)               # 0.0432 deg square
        self.grid(90, 2, 2, size=40)               # 0.0648 deg square
        self.grid(250, 1, 1, size=40)              # 0.09 deg square
        with mock.patch.object(abt, "read_tile",
                               side_effect=AssertionError("read_tile")), \
             mock.patch.object(abt, "read_tile_rows",
                               side_effect=AssertionError("read_tile_rows")):
            plan = self.plan()
        self.assertEqual([(g.res_m, g.count) for g in plan.groups],
                         [(30, 16), (90, 4), (250, 1)])
        for g in plan.groups:
            self.assertEqual(g.file_bytes, sum(os.path.getsize(h.path)
                                               for h in g.headers))
        # A second look reads no header at all: the index answers.
        abt_index.expire(self.dir)
        self.plan()
        self.assertEqual(abt_index.get_index(self.dir).last_reads, 0)

    def test_default_is_the_coarsest_resolution_that_covers_the_folder(self):
        self.grid(30, 4, 4, size=40)
        self.grid(90, 2, 2, size=40)
        self.grid(250, 1, 1, size=40)
        plan = self.plan()
        cov = {g.res_m: g.coverage for g in plan.groups}
        self.assertAlmostEqual(cov[250], 1.0, places=6)
        self.assertAlmostEqual(cov[90], (0.0648 / 0.09) ** 2, delta=0.01)
        self.assertAlmostEqual(cov[30], (0.0432 / 0.09) ** 2, delta=0.01)
        self.assertEqual(plan.default, (250,))

    def test_a_coarse_resolution_that_covers_only_part_is_not_the_default(
            self):
        # 90 m covers the whole folder; one 250 m tile a quarter of it.
        self.grid(90, 2, 2, size=40)                   # 0.0648 deg square
        _write(os.path.join(self.dir, "corner_250m.abt"), 14, 47.5, 8.0, 250)
        plan = self.plan()
        cov = {g.res_m: g.coverage for g in plan.groups}
        self.assertLess(cov[250], abt_inspect.COVERS_FRACTION)
        self.assertGreaterEqual(cov[90], 0.95)
        self.assertEqual(plan.default, (90,))

    def test_95_percent_is_enough(self):
        # 30 m misses one of 25 cells (96 %), 90 m only half of them.
        paths = self.grid(30, 5, 5, size=20)
        os.remove(paths[-1])
        self.grid(90, 1, 1, size=20)               # 1/9 of 30 m's square
        self.grid(10, 5, 5, size=60)               # 10 m over the same
        plan = self.plan()
        cov = {g.res_m: g.coverage for g in plan.groups}
        self.assertAlmostEqual(cov[30], 24 / 25, delta=0.005)
        self.assertEqual(plan.default, (30,))

    def test_rings_no_single_resolution_covers_take_the_coarsest_first(self):
        # A range-profile view: 30 m in the middle, 90 m around it — no
        # footprint twice. Together they cover the folder.
        span90 = _span(20, 90)
        for j in range(3):
            for i in range(3):
                if (i, j) == (1, 1):
                    continue
                _write(os.path.join(self.dir, f"r_90m_{j}_{i}.abt"), 20,
                       47.5 - j * span90, 8.0 + i * span90, 90)
        n = 3                       # 30 m tiles of 20 px fill the hole
        span30 = span90 / n
        for j in range(n):
            for i in range(n):
                _write(os.path.join(self.dir, f"r_30m_{j}_{i}.abt"), 20,
                       47.5 - span90 - j * span30, 8.0 + span90 + i * span30,
                       30)
        plan = self.plan()
        self.assertTrue(all(g.coverage < 0.95 for g in plan.groups),
                        [(g.res_m, g.coverage) for g in plan.groups])
        self.assertEqual(plan.default, (30, 90))
        self.assertAlmostEqual(plan.coverage([30, 90]), 1.0, places=6)

    def test_labels_and_the_big_read_warning(self):
        self.grid(30, 2, 2, size=40)
        plan = self.plan()
        (g,) = plan.groups
        self.assertEqual(abt_inspect.group_label(g),
                         f"30 m — 4 tile(s), {g.file_bytes / 1024 ** 2:,.0f} "
                         f"MB, covers 100 % of the folder")
        note = abt_inspect.choice_note(plan, [30])
        self.assertTrue(note.startswith("Reads 4 tile(s) — about "), note)
        self.assertNotIn("Cancel stops it", note)
        # A single-resolution folder whose tiles hold more than 2 GB.
        g.file_bytes = 106 * 1024 ** 3
        note = abt_inspect.choice_note(plan, [30])
        self.assertIn("The chosen tiles hold 106.0 GB: building the preview "
                      "reads about ", note)
        self.assertIn("Cancel stops it.", note)
        self.assertEqual(abt_inspect.choice_note(plan, []),
                         "Pick at least one resolution.")


# ---------------------------------------------------------------------------
# The mosaic: only the chosen tiles, only the sampled rows, finest wins
# ---------------------------------------------------------------------------

class MosaicReads(_Folder):

    def test_only_the_chosen_resolution_is_read(self):
        self.grid(30, 3, 3, size=30, value=100.0)
        self.grid(90, 1, 1, size=30, value=200.0)
        plan = self.plan()
        read = []
        real = abt.read_tile_rows

        def spy(header, rows):
            read.append(os.path.basename(header.path))
            return real(header, rows)

        headers = abt_inspect.chosen_headers(plan, [90])
        with mock.patch.object(abt, "read_tile_rows", side_effect=spy):
            grid = abt.mosaic_grid(headers, abt.INSPECT_MAX_DIM)
            canvas = abt.build_mosaic(headers, grid)
        self.assertEqual(read, ["tile_90m_0_0.abt"])
        self.assertTrue((canvas == 200.0).all())

    def test_sampled_rows_only_and_the_same_canvas_as_a_whole_read(self):
        rng = np.random.default_rng(7)
        values = rng.integers(-200, 4000, size=(400, 400)).astype(float)
        path = _write(os.path.join(self.dir, "big_2m.abt"), 400, 47.5, 8.0, 2,
                      array=values)
        (header,) = abt.read_headers([path])
        grid = abt.mosaic_grid([header], max_dim=40)
        self.assertEqual((grid.width, grid.height), (40, 40))
        stats = {}
        canvas = abt.build_mosaic([header], grid, stats=stats)
        # The old algorithm: the whole tile, then the index selection.
        whole = abt.read_tile(header).astype(np.float32) * abt.ELEV_STEP_M
        pick = np.linspace(0, 399, 40).astype(int)
        np.testing.assert_array_equal(canvas, whole[pick, :][:, pick])
        self.assertEqual(stats["tiles"], 1)
        self.assertEqual(stats["bytes"], 40 * 400 * 2)
        self.assertEqual(stats["bytes"], abt.mosaic_read_bytes([header], grid))
        self.assertLess(stats["bytes"], os.path.getsize(path) / 9)

    def test_finest_wins_whatever_the_file_order(self):
        # The 30 m tile sorts BEFORE the 90 m one by name; file order used
        # to paint 90 m over it.
        coarse = _write(os.path.join(self.dir, "b_90m.abt"), 30, 47.5, 8.0, 90,
                        value=100.0)
        fine = _write(os.path.join(self.dir, "a_30m.abt"), 30, 47.5, 8.0, 30,
                      value=200.0)
        headers = abt.read_headers([fine, coarse])         # file order
        self.assertEqual([os.path.basename(h.path)
                          for h in abt.mosaic_order(headers)],
                         ["b_90m.abt", "a_30m.abt"])
        grid = abt.mosaic_grid(headers, abt.INSPECT_MAX_DIM)
        canvas = abt.build_mosaic(headers, grid)
        third = grid.width // 3
        self.assertTrue((canvas[:third, :third] == 200.0).all())
        self.assertTrue((canvas[third + 1:, :] == 100.0).all())
        self.assertTrue((canvas[:, third + 1:] == 100.0).all())


@unittest.skipUnless(HAVE_GDAL, "needs the real GDAL")
class PreviewGeoTiff(_Folder):

    def test_tiled_deflate_with_internal_overviews_and_nodata(self):
        self.grid(30, 3, 2, size=200, value=500.0)
        # A void corner in one tile: no-data in the result and its overviews.
        arr = np.full((200, 200), 700.0)
        arr[:100, :100] = -9999 * abt.ELEV_STEP_M          # converter VOID
        _write(os.path.join(self.dir, "tile_30m_0_0.abt"), 200, 47.5, 8.0, 30,
               array=arr)
        plan = self.plan()
        out = os.path.join(self._tmp.name, "preview.tif")
        ticks = []
        with real_gdal():
            result = abt.mosaic_to_geotiff(
                self.dir, out, max_dim=abt.INSPECT_MAX_DIM,
                headers=abt_inspect.chosen_headers(plan, [30]),
                overviews=True,
                progress=lambda d, t, text: ticks.append((d, t, text)))
            ds = GDAL.Open(out)
            band = ds.GetRasterBand(1)
            self.assertEqual(band.GetOverviewCount(), 5)
            self.assertEqual([band.GetOverview(i).XSize for i in range(5)],
                             [-(-ds.RasterXSize // f) for f in
                              abt.OVERVIEW_LEVELS])
            self.assertEqual(band.GetNoDataValue(), abt.MOSAIC_NODATA_M)
            self.assertEqual(band.GetOverview(0).GetNoDataValue(),
                             abt.MOSAIC_NODATA_M)
            self.assertEqual(
                ds.GetMetadata("IMAGE_STRUCTURE").get("COMPRESSION"),
                "DEFLATE")
            self.assertEqual(band.GetBlockSize(), [256, 256])
            data = band.ReadAsArray()
            ov = band.GetOverview(0).ReadAsArray()
            ds = band = None
        self.assertFalse(os.path.exists(out + ".ovr"), "overviews are internal")
        self.assertEqual(result.overviews, abt.OVERVIEW_LEVELS)
        self.assertEqual((result.tiles_read, result.tile_count), (6, 6))
        self.assertEqual(result.bytes_read, abt.mosaic_read_bytes(
            abt_inspect.chosen_headers(plan, [30]), result.grid))
        self.assertEqual(data[0, 0], abt.MOSAIC_NODATA_M)
        self.assertEqual(data[150, 150], 700.0)
        self.assertEqual(data[-1, -1], 500.0)
        # AVERAGE ignores no-data: the void corner stays void.
        self.assertEqual(ov[0, 0], abt.MOSAIC_NODATA_M)
        self.assertEqual(ov[-1, -1], 500.0)
        self.assertEqual([t for t in ticks if t[2] == "Reading tiles"][-1],
                         (6, 6, "Reading tiles"))
        self.assertTrue(any(t[2] == "Building overviews" for t in ticks))

    def test_cancel_while_the_overviews_build_leaves_nothing(self):
        self.grid(30, 2, 2, size=200, value=500.0)
        out = os.path.join(self._tmp.name, "preview.tif")
        state = {"stop": False}

        def progress(_d, _t, text):
            if text == "Building overviews":
                state["stop"] = True

        with real_gdal():
            with self.assertRaises(abt.MosaicCanceled):
                abt.mosaic_to_geotiff(self.dir, out, overviews=True,
                                      progress=progress,
                                      should_cancel=lambda: state["stop"])
        self.assertTrue(state["stop"])
        self.assertFalse(os.path.exists(out))


# ---------------------------------------------------------------------------
# The tab: off the GUI thread, with the conftest stubs
# ---------------------------------------------------------------------------

def _start_in_a_real_thread(thread):
    """``TaskThread.start`` under the stubs: run it in a real thread and
    wait (the stub ``QThread`` has no thread of its own)."""
    t = threading.Thread(target=thread.run, name="waveshed-test-task")
    t.start()
    t.join(60)


@unittest.skipUnless(HAVE_GDAL, "needs the real GDAL")
class InspectOffTheGuiThread(_Folder):

    def tab(self):
        tab = mct.MapConverterTab.__new__(mct.MapConverterTab)
        tab._log = mock.Mock()
        tab._get_output_dir = lambda: self._tmp.name
        return tab

    def run_inspect(self, choose):
        self.grid(30, 3, 3, size=60, value=500.0)
        self.grid(90, 1, 1, size=60, value=300.0)
        tab = self.tab()
        gui = threading.get_ident()
        where = {}
        real_plan, real_mosaic = abt_inspect.plan_folder, abt.mosaic_to_geotiff

        def plan_spy(*a, **kw):
            where["plan"] = threading.get_ident()
            return real_plan(*a, **kw)

        def mosaic_spy(*a, **kw):
            where["mosaic"] = threading.get_ident()
            where["mosaic_kw"] = kw
            return real_mosaic(*a, **kw)

        layer = mock.Mock()
        layer.isValid.return_value = True
        qtcore = sys.modules["qgis.PyQt.QtCore"]
        from waveshed.core import result_loader
        with mock.patch.object(mct.QFileDialog, "getExistingDirectory",
                               create=True, return_value=self.dir), \
             mock.patch.object(mct, "_ask_inspect_choice",
                               side_effect=choose) as ask, \
             mock.patch.object(tfs.TaskThread, "start",
                               _start_in_a_real_thread), \
             mock.patch.object(tfs, "QProgressDialog"), \
             mock.patch.object(qtcore, "QEventLoop", mock.MagicMock(),
                               create=True), \
             mock.patch.object(abt_inspect, "plan_folder",
                               side_effect=plan_spy), \
             mock.patch.object(abt, "mosaic_to_geotiff",
                               side_effect=mosaic_spy), \
             mock.patch.object(mct, "QgsRasterLayer",
                               return_value=layer) as raster_layer, \
             mock.patch.object(result_loader,
                               "add_layer_to_project") as add, \
             mock.patch.object(tempfile, "tempdir", self._tmp.name), \
             real_gdal():
            tab._on_inspect_abt()
        logs = [c.args[0] for c in tab._log.appendPlainText.call_args_list]
        return gui, where, ask, raster_layer, add, logs

    def test_header_index_and_mosaic_run_in_task_threads(self):
        gui, where, ask, raster_layer, add, logs = self.run_inspect(
            lambda _parent, plan: list(plan.default))
        self.assertNotEqual(where["plan"], gui)
        self.assertNotEqual(where["mosaic"], gui)
        plan = ask.call_args.args[1]
        self.assertEqual(plan.default, (90,))       # covers the folder
        kw = where["mosaic_kw"]
        self.assertEqual(kw["max_dim"], abt.INSPECT_MAX_DIM)
        self.assertTrue(kw["overviews"])
        self.assertEqual({os.path.basename(h.path) for h in kw["headers"]},
                         {"tile_90m_0_0.abt"})
        out_tif = raster_layer.call_args.args[0]
        self.assertTrue(out_tif.endswith("aether_inspect_abt_90m.tif"), out_tif)
        self.assertTrue(os.path.isfile(out_tif))
        add.assert_called_once()
        summary = [m for m in logs if m.startswith("Inspect .abt: ")]
        self.assertEqual(len(summary), 1, logs)
        self.assertRegex(summary[0], r"^Inspect \.abt: 1 tile\(s\) read "
                                     r"\(90 m\), [\d.,]+ MB read in "
                                     r"[\d.]+ s → .*\(\d+ × \d+ px, "
                                     r"overviews 2–32\)$")

    def test_two_resolutions_and_cancel_in_the_dialog(self):
        _gui, where, _ask, raster_layer, _add, logs = self.run_inspect(
            lambda _parent, _plan: None)
        self.assertNotIn("mosaic", where)
        raster_layer.assert_not_called()
        _gui, where, _ask, raster_layer, _add, logs = self.run_inspect(
            lambda _parent, _plan: [30, 90])
        self.assertEqual(len(where["mosaic_kw"]["headers"]), 10)
        self.assertTrue(raster_layer.call_args.args[0].endswith(
            "aether_inspect_abt_30m+90m.tif"))
        self.assertTrue(any("(30 m + 90 m)" in m for m in logs), logs)


if __name__ == "__main__":
    unittest.main()
