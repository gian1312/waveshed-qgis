"""Terrain folders of mixed files: index, pre-flight, coverage, priority,
per-tile invalidation, materialize routing — against REAL GeoTIFFs.

Each class pins one defect found by running a folder of "various GeoTIFFs
and other formats with different borders than our .abt tiles" through the
plugin (D3 stale cache, D4 one bad file aborts / CRS-less files inherit,
D5 false coverage warning, formats skipped, name-order priority, scale/
offset, degraded downloads).
"""

# Bootstrap stubs before any plugin import.
import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import json
import os
import shutil
import tempfile
import time
import unittest
import zipfile
from unittest import mock

import numpy as np

import waveshed.core.terrain_adapter as ta
from waveshed.core import terrain_index as ti
from _real_gdal import GDAL, OSR, HAVE_GDAL, real_gdal, write_tif
from test_terrain_adapter import _write_pool_abt


def _bounds(w, e, s, n):
    return {"west": w, "east": e, "south": s, "north": n}


def _lv95_bounds_for(wgs: dict) -> dict:
    """LV95 rectangle enclosing a WGS84 bbox (so the file covers it)."""
    src = OSR.SpatialReference()
    src.ImportFromEPSG(4326)
    src.SetAxisMappingStrategy(OSR.OAMS_TRADITIONAL_GIS_ORDER)
    dst = OSR.SpatialReference()
    dst.ImportFromEPSG(2056)
    dst.SetAxisMappingStrategy(OSR.OAMS_TRADITIONAL_GIS_ORDER)
    ct = OSR.CoordinateTransformation(src, dst)
    pts = [ct.TransformPoint(x, y)[:2] for x in (wgs["west"], wgs["east"])
           for y in (wgs["south"], wgs["north"])]
    return _bounds(min(p[0] for p in pts), max(p[0] for p in pts),
                   min(p[1] for p in pts), max(p[1] for p in pts))


class _Folder(unittest.TestCase):
    """A temp root with dem/ and a private cache; real GDAL patched in."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ws_ti_")
        self.dem = os.path.join(self.root, "dem")
        os.makedirs(self.dem)
        self._patches = [
            mock.patch.object(ta, "get_cache_dir",
                              return_value=os.path.join(self.root, "cache")),
        ]
        for p in self._patches:
            p.start()
        self._gdal = real_gdal()
        self._gdal.__enter__()
        ti.forget_indexes()
        ti.clear_approvals()
        conftest._MockQSettings._store.clear()

    def tearDown(self):
        self._gdal.__exit__(None, None, None)
        for p in self._patches:
            p.stop()
        ti.forget_indexes()
        ti.clear_approvals()
        shutil.rmtree(self.root, ignore_errors=True)

    def tif(self, name, bounds, **kw):
        path = os.path.join(self.dem, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return write_tif(path, bounds, **kw)


@unittest.skipUnless(HAVE_GDAL, "needs the real GDAL")
class TestIndexRefresh(_Folder):
    """A repeat run must not re-open every file (69 s for 2,500 files)."""

    def test_repeat_refresh_probes_nothing_and_survives_a_restart(self):
        for i in range(3):
            self.tif(f"t{i}.tif", _bounds(7 + i, 7.5 + i, 46, 46.5))
        idx = ti.get_index(self.dem)
        first = idx.refresh()
        self.assertEqual((first.new, first.unchanged), (3, 0))
        ti.forget_indexes()                      # a new QGIS session
        with mock.patch.object(ti, "probe_file",
                               side_effect=AssertionError("re-probed")):
            again = ti.get_index(self.dem).refresh()
        self.assertEqual((again.new, again.changed, again.unchanged), (0, 0, 3))

    def test_only_new_changed_and_removed_files_are_touched(self):
        a = self.tif("a.tif", _bounds(7, 7.5, 46, 46.5))
        self.tif("b.tif", _bounds(8, 8.5, 46, 46.5))
        idx = ti.get_index(self.dem)
        idx.refresh()
        os.remove(os.path.join(self.dem, "b.tif"))
        write_tif(a, _bounds(7, 7.6, 46, 46.5), value=900.0)
        os.utime(a, ns=(time.time_ns(), time.time_ns() + 10**9))
        self.tif("c.tif", _bounds(9, 9.5, 46, 46.5))
        probed = []
        real_probe = ti.probe_file
        with mock.patch.object(ti, "probe_file",
                               side_effect=lambda p, *a: probed.append(p)
                               or real_probe(p, *a)):
            st = idx.refresh()
        self.assertEqual((st.new, st.changed, st.removed), (1, 1, 1))
        self.assertEqual(sorted(os.path.basename(p) for p in probed),
                         ["a.tif", "c.tif"])
        self.assertAlmostEqual(
            idx.entries[a]["wgs84_bounds"]["east"], 7.6, places=6)

    def test_the_probe_records_what_routing_needs(self):
        lv = _lv95_bounds_for(_bounds(7.40, 7.45, 46.90, 46.95))
        p = self.tif("lv95.tif", lv, epsg=2056,
                     options=["COMPRESS=ZSTD"], size=(40, 30))
        e = ti.probe_file(p)
        self.assertTrue(e["readable"], e["reason"])
        self.assertEqual(e["crs_authid"], "EPSG:2056")
        self.assertEqual(e["compression"], "ZSTD")
        self.assertEqual(e["dtype"], "Float32")
        self.assertEqual(e["bands"], 1)
        w = e["wgs84_bounds"]
        self.assertLess(w["west"], 7.40 + 1e-6)
        self.assertGreater(w["north"], 46.95 - 1e-6)
        self.assertGreater(len(e["footprint"]), 4)
        self.assertAlmostEqual(e["px_x_m"], (lv["east"] - lv["west"]) / 40)
        self.assertEqual(ti.materialize_reason(e, p), "ZSTD compression")


@unittest.skipUnless(HAVE_GDAL, "needs the real GDAL")
class TestFormatsAreScanned(_Folder):
    """.asc/.img/.bil/.xyz/zip members used to be skipped silently."""

    def test_gdal_formats_and_zip_members_are_found_sidecars_are_not(self):
        base = self.tif("base.tif", _bounds(7, 7.2, 46, 46.2))
        for drv, name in (("AAIGrid", "a.asc"), ("HFA", "b.img"),
                          ("EHdr", "c.bil"), ("XYZ", "d.xyz")):
            GDAL.Translate(os.path.join(self.dem, name), base, format=drv)
        with zipfile.ZipFile(os.path.join(self.dem, "z.zip"), "w") as zf:
            zf.write(base, "inner/e.tif")
            zf.write(base, "readme.txt")
        # Things that must never be scanned:
        for junk in ("mosaic.vrt", "base.tif.aux.xml", "base.tif.ovr",
                     "base.tif.msk", "base.tfw", "._base.tif", "c.hdr"):
            p = os.path.join(self.dem, junk)
            if not os.path.exists(p):
                open(p, "w").close()
        found = [os.path.basename(p) for p in ta.list_terrain_files(self.dem)]
        for want in ("base.tif", "a.asc", "b.img", "c.bil", "d.xyz", "e.tif"):
            self.assertIn(want, found)
        for junk in ("mosaic.vrt", "base.tif.aux.xml", "base.tif.ovr",
                     "base.tif.msk", "base.tfw", "._base.tif", "c.hdr",
                     "readme.txt"):
            self.assertNotIn(junk, found)
        idx = ti.get_index(self.dem)
        idx.refresh()
        zipped = [p for p in idx.entries if p.startswith("/vsizip/")]
        self.assertEqual(len(zipped), 1)
        self.assertTrue(idx.entries[zipped[0]]["readable"])
        self.assertIn(".vrt", ti.EXCLUDED_EXTS)          # with its reason
        for p in idx.entries:
            if not p.endswith("base.tif"):
                self.assertIsNotNone(
                    ti.materialize_reason(idx.entries[p], p), p)


@unittest.skipUnless(HAVE_GDAL, "needs the real GDAL")
class TestBadFilesArePreflighted(_Folder):
    """One bad file used to abort the run, or (CRS-less) silently inherit the
    first file's CRS in the Map Converter."""

    def _folder(self):
        lv = _lv95_bounds_for(_bounds(7.40, 7.45, 46.90, 46.95))
        self.tif("good_lv95.tif", lv, epsg=2056)
        self.tif("nocrs.tif", lv, epsg=None)
        self.tif("rgb.tif", _bounds(7.4, 7.5, 46.9, 47.0), bands=3,
                 dtype=GDAL.GDT_Byte, value=120)
        self.tif("grey8.tif", _bounds(7.4, 7.5, 46.9, 47.0),
                 dtype=GDAL.GDT_Byte, value=120)
        good = self.tif("cut.tif", _bounds(7.4, 7.5, 46.9, 47.0),
                        size=(400, 400))
        size = os.path.getsize(good)
        with open(good, "r+b") as fh:
            fh.truncate(int(size * 0.4))

    def test_every_problem_is_named_with_its_reason(self):
        self._folder()
        idx = ti.get_index(self.dem)
        idx.refresh()
        probs = {os.path.basename(p): (k, r) for p, k, r in idx.problems()}
        self.assertEqual(probs["nocrs.tif"][0], ti.KIND_NOCRS)
        self.assertEqual(probs["rgb.tif"][0], ti.KIND_UNUSABLE)
        self.assertIn("3 bands", probs["rgb.tif"][1])
        self.assertEqual(probs["grey8.tif"][0], ti.KIND_SUSPECT)
        self.assertIn("not elevation", probs["grey8.tif"][1])
        self.assertEqual(probs["cut.tif"][0], ti.KIND_UNUSABLE)
        self.assertIn("truncated", probs["cut.tif"][1])
        self.assertNotIn("good_lv95.tif", probs)

    def test_unanswered_problems_refuse_the_run_naming_every_file(self):
        self._folder()
        with self.assertRaises(RuntimeError) as caught:
            ta._gather_source_infos(None, self.dem)
        msg = str(caught.exception)
        for name in ("nocrs.tif", "rgb.tif", "grey8.tif", "cut.tif"):
            self.assertIn(name, msg)
        self.assertIn("no coordinate system (CRS)", msg)

    def test_confirmed_skips_run_with_the_good_file_only(self):
        self._folder()
        idx = ti.get_index(self.dem)
        idx.refresh()
        ti.approve(self.dem, {p: ti.DECISION_SKIP for p, _k, _r in idx.problems()})
        infos, use_export = ta._gather_source_infos(None, self.dem)
        self.assertFalse(use_export)
        self.assertEqual([os.path.basename(i["path"]) for i in infos],
                         ["good_lv95.tif"])

    def test_a_changed_file_must_be_confirmed_again(self):
        self._folder()
        idx = ti.get_index(self.dem)
        idx.refresh()
        ti.approve(self.dem, {p: ti.DECISION_SKIP for p, _k, _r in idx.problems()})
        p = os.path.join(self.dem, "grey8.tif")
        os.utime(p, ns=(time.time_ns(), time.time_ns() + 5 * 10**9))
        ti.expire_all()
        with self.assertRaises(RuntimeError) as caught:
            ta._gather_source_infos(None, self.dem)
        self.assertIn("grey8.tif", str(caught.exception))
        self.assertNotIn("rgb.tif", str(caught.exception))

    def test_crs_less_file_never_inherits_the_folder_crs(self):
        """Map Converter: the entry's folder CRS used to georeference it."""
        lv = _lv95_bounds_for(_bounds(7.40, 7.45, 46.90, 46.95))
        self.tif("good_lv95.tif", lv, epsg=2056)
        self.tif("nocrs.tif", lv, epsg=None)
        import waveshed.gui.map_converter_tab as mct
        w = mct._MapConverterWorker.__new__(mct._MapConverterWorker)
        w._canceled = False
        w._log = lambda m: None
        entry = {"resolved_source": {"kind": "file", "path": self.dem},
                 "crs_authid": "EPSG:2056", "extent": None}
        with self.assertRaises(RuntimeError) as caught:
            w._resolve_raster(entry)
        self.assertIn("nocrs.tif", str(caught.exception))
        # Assigned explicitly, it is used — in the CRS the user gave.
        entry["crs_overrides"] = {os.path.join(self.dem, "nocrs.tif"):
                                  "EPSG:2056"}
        got = w._resolve_raster(entry)
        names = sorted(os.path.basename(i["path"]) for i in got["infos"])
        self.assertEqual(names, ["good_lv95.tif", "nocrs.tif"])
        assigned = [i for i in got["infos"] if i["path"].endswith("nocrs.tif")]
        self.assertEqual(assigned[0]["crs_arg"], "EPSG:2056")


@unittest.skipUnless(HAVE_GDAL, "needs the real GDAL")
class TestCoverageFromFootprints(_Folder):
    """BuildVRT dropped files in another CRS (false "only 22%") and a bbox
    could never show a missing file in the middle."""

    BBOX = _bounds(7.40, 7.50, 46.90, 46.96)

    def test_mixed_crs_full_coverage_does_not_warn(self):
        lv = _lv95_bounds_for(_bounds(7.39, 7.46, 46.89, 46.97))
        self.tif("west_lv95.tif", lv, epsg=2056)
        self.tif("east_wgs84.tif", _bounds(7.455, 7.51, 46.89, 46.97))
        self.assertIsNone(ta.terrain_coverage_warning(self.dem, self.BBOX))

    def test_a_missing_file_in_the_middle_is_reported(self):
        xs = np.linspace(7.39, 7.51, 4)
        ys = np.linspace(46.89, 46.97, 4)
        for i in range(3):
            for j in range(3):
                if (i, j) == (1, 1):
                    continue                     # the hole
                self.tif(f"t_{i}{j}.tif",
                         _bounds(xs[i], xs[i + 1], ys[j], ys[j + 1]))
        msg = ta.terrain_coverage_warning(self.dem, self.BBOX)
        self.assertIsNotNone(msg)
        self.assertIn("GAP INSIDE", msg)
        self.assertIn("82%", msg)        # the hole: 0.04 x 0.027 of 0.1 x 0.06

    def test_one_missing_file_of_a_hundred_is_still_a_hole(self):
        # 99% covered passes the 98% overhang tolerance — but a hole in the
        # middle is a missing file, never an overhang.
        xs = np.linspace(7.39, 7.51, 11)
        ys = np.linspace(46.89, 46.97, 11)
        for i in range(10):
            for j in range(10):
                if (i, j) != (5, 4):
                    self.tif(f"t_{i:02d}{j:02d}.tif",
                             _bounds(xs[i], xs[i + 1], ys[j], ys[j + 1]),
                             size=(8, 8))
        msg = ta.terrain_coverage_warning(self.dem, self.BBOX)
        self.assertIsNotNone(msg)
        self.assertIn("has a hole", msg)
        self.assertIn("GAP INSIDE", msg)

    def test_ground_past_the_edge_is_not_called_a_gap(self):
        self.tif("half.tif", _bounds(7.39, 7.45, 46.89, 46.97))
        msg = ta.terrain_coverage_warning(self.dem, self.BBOX)
        self.assertIsNotNone(msg)
        self.assertIn("50%", msg)
        self.assertNotIn("GAP INSIDE", msg)


@unittest.skipUnless(HAVE_GDAL, "needs the real GDAL")
class TestOverlapPriority(_Folder):
    """Plain path sort let a coarse filler beat a fine dataset."""

    def _pair(self):
        self.tif("a_coarse_filler.tif", _bounds(7.0, 8.0, 46.0, 47.0),
                 size=(10, 10))
        self.tif("b_fine.tif", _bounds(7.4, 7.6, 46.4, 46.6), size=(200, 200))

    def test_finest_resolution_wins_by_default(self):
        self._pair()
        infos, _ = ta._gather_source_infos(None, self.dem)
        self.assertEqual(os.path.basename(infos[0]["path"]), "b_fine.tif")
        tile = _bounds(7.45, 7.5, 46.45, 46.5)
        srcs, _t = ta.build_tile_sources(infos, tile, self.root, "t", 30.0)
        self.assertEqual(os.path.basename(srcs[0]["path"]), "b_fine.tif")

    def test_name_order_is_available_and_warns(self):
        self._pair()
        ti.set_overlap_priority(self.dem, ti.PRIORITY_NAME)
        logged = []
        with mock.patch.object(ta, "_log", side_effect=logged.append):
            infos, _ = ta._gather_source_infos(None, self.dem)
        self.assertEqual(os.path.basename(infos[0]["path"]),
                         "a_coarse_filler.tif")
        text = "\n".join(logged)
        self.assertIn("overlap priority: file name order", text)
        self.assertIn("WARNING", text)
        self.assertIn("differ in resolution", text)

    def test_the_effective_order_is_logged(self):
        self._pair()
        logged = []
        with mock.patch.object(ta, "_log", side_effect=logged.append):
            ta._gather_source_infos(None, self.dem)
        line = [m for m in logged if "overlap priority" in m][0]
        self.assertIn("finest resolution wins", line)
        self.assertLess(line.index("b_fine.tif"),
                        line.index("a_coarse_filler.tif"))


@unittest.skipUnless(HAVE_GDAL, "needs the real GDAL")
class TestMaterializeRouting(_Folder):
    """Decided by the file's metadata, not its extension."""

    TILE = _bounds(7.40, 7.42, 46.90, 46.92)

    def _sources(self):
        idx = ti.get_index(self.dem)
        idx.refresh()
        infos, _ = ti.source_infos(self.dem, ti.PRIORITY_FINEST, index=idx)
        return ta.build_tile_sources(infos, self.TILE, self.root, "t", 30.0)

    def test_scale_offset_reaches_the_converter_as_metres(self):
        self.tif("scaled.tif", _bounds(7.39, 7.43, 46.89, 46.93),
                 dtype=GDAL.GDT_Int16, value=5000, scale=0.1,
                 nodata=-32768)
        srcs, temps = self._sources()
        self.assertEqual(len(temps), 1, "a scaled GeoTIFF must be copied")
        ds = GDAL.Open(srcs[0]["path"])
        band = ds.GetRasterBand(1)
        self.assertAlmostEqual(float(band.ReadAsArray().max()), 500.0, 3)
        self.assertIn(band.GetScale(), (None, 1.0))
        self.assertEqual(band.GetNoDataValue(), -32768.0)

    def test_a_single_file_layer_is_routed_the_same_way(self):
        p = self.tif("scaled.tif", _bounds(7.39, 7.43, 46.89, 46.93),
                     dtype=GDAL.GDT_Int16, value=5000, scale=0.1)
        with mock.patch.object(ta, "source_file_info",
                               return_value={"path": p, "crs": None,
                                             "crs_authid": "EPSG:4326",
                                             "native_bounds": {},
                                             "wgs84_bounds": {},
                                             "halo_deg": 0.0}):
            info = ta.file_layer_info(p, "EPSG:4326")
        self.assertEqual(info["materialize"], "scale/offset")
        self.assertTrue(info["unscale"])
        self.assertRegex(info["fp"], r"^\d+:\d+$")

    def test_zstd_is_window_copied_uncompressed(self):
        self.tif("z.tif", _bounds(7.39, 7.43, 46.89, 46.93),
                 options=["COMPRESS=ZSTD"])
        srcs, temps = self._sources()
        self.assertEqual(len(temps), 1)
        ds = GDAL.Open(srcs[0]["path"])
        self.assertNotIn(
            (ds.GetMetadata("IMAGE_STRUCTURE") or {}).get("COMPRESSION"),
            ("ZSTD", "LERC"))

    def test_rotated_geotransform_becomes_north_up(self):
        gt = (7.39, 0.002, 0.0002, 46.93, 0.0002, -0.002)
        self.tif("rot.tif", None, gt=gt, size=(20, 20))
        srcs, temps = self._sources()
        self.assertEqual(len(temps), 1)
        g = GDAL.Open(srcs[0]["path"]).GetGeoTransform()
        self.assertEqual((g[2], g[4]), (0.0, 0.0))
        self.assertLess(g[5], 0)

    def test_a_custom_crs_is_passed_as_a_proj_string(self):
        srs = OSR.SpatialReference()
        srs.SetFromUserInput("+proj=tmerc +lat_0=46 +lon_0=7.4 +k=1 +x_0=0 "
                             "+y_0=0 +ellps=GRS80 +units=m +no_defs")
        b = _bounds(-2000, 4000, 98000, 105000)   # ~46.88-46.95 N
        self.tif("custom.tif", b, wkt=srs.ExportToWkt())
        srcs, _ = self._sources()
        self.assertTrue(srcs, "the custom-CRS file must contribute")
        self.assertTrue(srcs[0]["crs"].startswith("+proj=tmerc"), srcs[0])


@unittest.skipUnless(HAVE_GDAL, "needs the real GDAL")
class TestPerTileInvalidation(_Folder):
    """D3: a folder's pool key was the folder path — a changed or added file
    was a "cache HIT" serving the old terrain."""

    QUAD = {
        "tile_N47.00E7.50_30m.abt": _bounds(7.55, 7.95, 46.55, 46.95),
        "tile_N47.00E8.00_30m.abt": _bounds(8.05, 8.45, 46.55, 46.95),
        "tile_N47.50E7.50_30m.abt": _bounds(7.55, 7.95, 47.05, 47.45),
        "tile_N47.50E8.00_30m.abt": _bounds(8.05, 8.45, 47.05, 47.45),
    }

    def _run(self):
        built = []

        def fake_run_converter(exe, job_file):
            with open(job_file) as fh:
                jobs = json.load(fh)
            for job in jobs:
                built.append(os.path.basename(job["output_path"]))
                _write_pool_abt(job["output_path"])
            return []

        bm = mock.Mock()
        bm.find_binary.return_value = "aether_converter"
        with mock.patch.object(ta, "_run_converter",
                               side_effect=fake_run_converter):
            ta.reset_terrain_warnings()
            ta.prepare_terrain(None, 47.0, 8.0, 10.0, 30, bm,
                               terrain_dir=self.dem)
        return sorted(built)

    def test_only_the_tiles_a_changed_or_added_file_touches_rebuild(self):
        names = sorted(self.QUAD)
        fine = {"size": (400, 400)}            # halo << the 0.05 deg inset
        self.tif("ne.tif", self.QUAD[names[3]], **fine)
        self.tif("nw.tif", self.QUAD[names[2]], **fine)
        self.tif("se.tif", self.QUAD[names[1]], **fine)
        self.assertEqual(self._run(), names)          # first run: all four
        self.assertEqual(self._run(), [])             # unchanged: a hit
        ne = os.path.join(self.dem, "ne.tif")
        write_tif(ne, self.QUAD[names[3]], value=900.0, **fine)
        os.utime(ne, ns=(time.time_ns(), time.time_ns() + 10**9))
        self.assertEqual(self._run(), [names[3]])     # changed file
        self.tif("sw.tif", self.QUAD[names[0]], **fine)
        self.assertEqual(self._run(), [names[0]])     # added file
        os.remove(os.path.join(self.dem, "se.tif"))
        self.assertEqual(self._run(), [names[1]])     # removed file

    def test_switching_the_overlap_rule_rebuilds_only_overlapped_tiles(self):
        names = sorted(self.QUAD)
        self.tif("a_coarse.tif", _bounds(7.55, 7.95, 46.55, 46.95),
                 size=(40, 40))
        self.tif("b_fine.tif", _bounds(7.6, 7.9, 46.6, 46.9), size=(300, 300))
        self.tif("other.tif", self.QUAD[names[3]], size=(400, 400))
        self._run()
        ti.set_overlap_priority(self.dem, ti.PRIORITY_NAME)
        self.assertEqual(self._run(), [names[0]])


@unittest.skipUnless(HAVE_GDAL, "needs the real GDAL")
class TestMapConverterRebuildsChangedTiles(_Folder):
    """Map Converter skipped existing tiles unless "Rebuild existing tiles" —
    a changed file in a folder left the old pixels in place."""

    def test_changed_inputs_rebuild_without_overwrite(self):
        import waveshed.gui.map_converter_tab as mct
        names = sorted(TestPerTileInvalidation.QUAD)
        quad = TestPerTileInvalidation.QUAD
        for n, tag in zip(names, ("sw", "se", "nw", "ne")):
            self.tif(f"{tag}.tif", quad[n], size=(400, 400))
        expected = mct._enumerate_tiles(
            mct._snap_bbox(_bounds(7.6, 8.4, 46.6, 47.4), [30]), [30])
        self.assertEqual(sorted(t["filename"] for _r, t in expected), names)

        def entries():
            ti.expire_all()
            return [{"kind": "files",
                     "infos": ta.folder_source_infos(self.dem)}]
        out = os.path.join(self.root, "out")
        os.makedirs(out)
        before = mct._stack_tile_fingerprints(expected, entries(), None)
        for n in names:
            _write_pool_abt(os.path.join(out, n))
        mct._write_output_manifest(out, before)
        ne = os.path.join(self.dem, "ne.tif")
        write_tif(ne, quad[names[3]], value=900.0, size=(400, 400))
        os.utime(ne, ns=(time.time_ns(), time.time_ns() + 10**9))
        after = mct._stack_tile_fingerprints(expected, entries(), None)
        manifest = mct._read_output_manifest(out)
        changed = {n for n in names if manifest[n] != after[n]}
        self.assertEqual(changed, {names[3]})
        jobs, _t = mct._build_tile_jobs(expected, out, False, entries(), None,
                                        self.root, rebuild=changed)
        self.assertEqual([os.path.basename(j["output_path"]) for j in jobs],
                         [names[3]])


class TestDegradedDownloadTiles(unittest.TestCase):
    """Toolkit protocol: a failed source tile filled from a coarser zoom is a
    ``[Degraded]`` line, not counted as OK — and has no hole, so the hole
    scan alone pooled coarse terrain as complete."""

    # Two output tiles at 46-47 N; z12 source tile 2134/1434 lies in the
    # western one (7.55-7.64 E, 47.0-47.07 N... see _xyz_tile_bbox).
    SPECS = [
        {"filename": "tile_west.abt", "ul_lat": 47.5, "ul_lon": 7.5,
         "size_px": 1852, "resolution_m": 30.0},
        {"filename": "tile_east.abt", "ul_lat": 47.5, "ul_lon": 8.0,
         "size_px": 1852, "resolution_m": 30.0},
    ]

    @staticmethod
    def _xyz(lat, lon, z):
        import math
        n = 2 ** z
        x = int((lon + 180.0) / 360.0 * n)
        y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
        return x, y

    def _lines(self, ok, total, degraded):
        out = [f"[Degraded] z=12 x={x} y={y} reason=timeout filled_from=z11"
               for x, y in degraded]
        if degraded:
            out.append(f"[Stats] DEGRADED: {len(degraded)} of {total} source "
                       f"tile(s) filled from coarser zooms after a failed fetch")
        out.append(f"[Stats] Tiles: {ok}/{total} OK")
        return out

    def _group(self, pool, passes, max_passes=3):
        calls = []

        def run_pass(specs, conn, zoom):
            calls.append(([s["filename"] for s in specs], conn))
            for s in specs:
                _write_pool_abt(os.path.join(pool, s["filename"]))
            return (0,) + passes[len(calls) - 1]

        logged = []
        with mock.patch.object(ta, "_log", side_effect=logged.append), \
             mock.patch.object(ta, "_abt_has_holes", return_value=False), \
             mock.patch.object(ta, "_abt_has_zero_fill", return_value=False):
            out = ta._download_zoom_group(pool, self.SPECS, 12, 256,
                                          max_passes, run_pass, lambda: True)
        return out, calls, "\n".join(logged)

    def test_lines_parse_and_map_to_the_output_tile_they_hit(self):
        x, y = self._xyz(47.2, 7.7, 12)
        d = ta._parse_degraded(self._lines(99, 100, [(x, y)]))
        self.assertEqual(d, [ta.DegradedTile(12, x, y, "timeout", 11)])
        self.assertEqual(ta._parse_degraded_count(self._lines(99, 100, [(x, y)])), 1)
        self.assertEqual(ta._specs_hit_by_degraded(self.SPECS, d),
                         {"tile_west.abt"})

    def test_degraded_tile_is_retried_then_flagged_and_kept(self):
        x, y = self._xyz(47.2, 7.7, 12)
        bad = self._lines(99, 100, [(x, y)])
        bad1 = (99, 100, bad)
        bad2 = (0, 1, self._lines(0, 1, [(x, y)]))
        with tempfile.TemporaryDirectory() as pool:
            out, calls, log = self._group(pool, [bad1, bad2], max_passes=2)
            self.assertTrue(out)
            # Retried alone, at reduced connections.
            self.assertEqual(calls[1], (["tile_west.abt"], 128))
            # Kept for this run, flagged for the next one.
            self.assertTrue(os.path.exists(os.path.join(pool, "tile_west.abt")))
            self.assertFalse(ta._tile_ready(pool, "tile_west.abt"))
            self.assertTrue(ta._tile_ready(pool, "tile_east.abt"))
        self.assertIn("1 terrain tile(s) were filled from coarser data (z11)", log)
        self.assertIn("re-downloaded next run", log)

    def test_a_retry_that_succeeds_clears_everything(self):
        x, y = self._xyz(47.2, 7.7, 12)
        with tempfile.TemporaryDirectory() as pool:
            out, calls, log = self._group(
                pool, [(99, 100, self._lines(99, 100, [(x, y)])),
                       (1, 1, self._lines(1, 1, []))])
            self.assertTrue(out)
            self.assertEqual(len(calls), 2)
            self.assertTrue(ta._tile_ready(pool, "tile_west.abt"))
        self.assertNotIn("coarser data", log)

    def test_complete_and_no_data_branches_are_unchanged(self):
        with tempfile.TemporaryDirectory() as pool:
            out, calls, _ = self._group(pool, [(100, 100, self._lines(100, 100, []))])
            self.assertTrue(out)
            self.assertEqual(len(calls), 1)
        no_data = ["[Stats] NO-DATA (HTTP 404): 4 of 100 tile(s)",
                   "[Stats] Tiles: 96/100 OK"]
        with tempfile.TemporaryDirectory() as pool:
            out, calls, _ = self._group(pool, [(96, 100, no_data)])
            self.assertTrue(out)
            self.assertEqual(len(calls), 1)
            self.assertTrue(ta._tile_ready(pool, "tile_west.abt"))
        # 404s plus a degraded tile: NOT the all-404 complete branch.
        x, y = self._xyz(47.2, 7.7, 12)
        mixed = (["[Stats] NO-DATA (HTTP 404): 4 of 100 tile(s)"]
                 + self._lines(95, 100, [(x, y)]))
        with tempfile.TemporaryDirectory() as pool:
            out, calls, _ = self._group(pool, [(95, 100, mixed)] * 3)
            self.assertTrue(out)
            self.assertGreater(len(calls), 1)
            self.assertFalse(ta._tile_ready(pool, "tile_west.abt"))

    def test_everything_degraded_is_not_a_total_failure(self):
        # ok=0 used to mean "nothing arrived → drop every tile"; coarse fill
        # is terrain, so it is kept (and flagged).
        x, y = self._xyz(47.2, 7.7, 12)
        x2, y2 = self._xyz(47.2, 8.2, 12)
        lines = self._lines(0, 2, [(x, y), (x2, y2)])
        with tempfile.TemporaryDirectory() as pool:
            out, _calls, log = self._group(pool, [(0, 2, lines)] * 3)
            self.assertTrue(out)
            self.assertTrue(os.path.exists(os.path.join(pool, "tile_east.abt")))
        self.assertNotIn("flat 0 m terrain", log)


if __name__ == "__main__":
    unittest.main()
