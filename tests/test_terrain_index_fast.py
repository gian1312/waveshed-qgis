"""The fast GeoTIFF header probes answer exactly what the GDAL probe answers.

``terrain_index.probe_geotiff_fast`` reads an ordinary GeoTIFF's first IFD
without GDAL (~40x faster per file); ``terrain_index.entries_from_probe``
builds entries from ``aether_converter probe`` lines (aether-probe/1). Each
must either decline a file (→ the GDAL probe handles it) or return the same
index entry the GDAL probe returns — for every variant a terrain folder can
hold. The converter path runs the real ``aether_converter`` (>= 0.2.7, from
AETHER_BIN_DIR or PATH) when there is one, else the lines it recorded for the
same synthetic files (``fixtures/probe_parity.jsonl``; header facts as
recorded, ``path``/``size``/``mtime`` from the file written here).
"""

import json
import math
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import conftest  # noqa: F401 — QGIS/PyQt stubs

from _real_gdal import GDAL, OSR, HAVE_GDAL, real_gdal, write_tif

from waveshed.core import terrain_index as ti

LV95 = {"west": 2_600_000.0, "east": 2_601_000.0, "south": 1_200_000.0, "north": 1_201_000.0}
WGS = {"west": 7.40, "east": 7.41, "south": 46.90, "north": 46.91}
UTM = {"west": 400_000.0, "east": 401_000.0, "south": 5_190_000.0, "north": 5_191_000.0}

#: Keys compared exactly / with float tolerance (WKT text may be spelled
#: differently for the same EPSG CRS — compared through the authid instead).
EXACT = ("probe", "readable", "reason", "driver", "bands", "xsize", "ysize", "dtype",
         "compression", "nbits", "rotated", "flipped", "crs_authid", "geographic")
FLOATS = ("scale", "offset", "nodata", "px_x", "px_y", "px_x_m", "px_y_m", "px_m")


def _close(a, b):
    if a is None or b is None:
        return a is b
    if isinstance(a, float) and math.isnan(a):
        return isinstance(b, float) and math.isnan(b)
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


class _SameEntry:
    def assertSame(self, fast, slow, name):
        self.assertIsNotNone(fast, f"{name}: the fast probe declined an ordinary file")
        for k in EXACT:
            self.assertEqual(fast.get(k), slow.get(k), f"{name}: {k}")
        for k in FLOATS:
            self.assertTrue(_close(fast.get(k), slow.get(k)), f"{name}: {k} {fast.get(k)} vs {slow.get(k)}")
        for a, b in zip(fast["geotransform"], slow["geotransform"]):
            self.assertTrue(_close(a, b), f"{name}: geotransform {fast['geotransform']} vs {slow['geotransform']}")
        for side in ("native_bounds", "wgs84_bounds"):
            for k in ("west", "east", "south", "north"):
                self.assertAlmostEqual(fast[side][k], slow[side][k], places=7, msg=f"{name}: {side}.{k}")
        self.assertEqual(len(fast["footprint"]), len(slow["footprint"]), name)
        for (x0, y0), (x1, y1) in zip(fast["footprint"], slow["footprint"]):
            self.assertAlmostEqual(x0, x1, places=6, msg=name)
            self.assertAlmostEqual(y0, y1, places=6, msg=name)


@unittest.skipUnless(HAVE_GDAL, "needs the real GDAL")
class TestFastProbeMatchesGdal(_SameEntry, unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self._gdal = real_gdal()
        self._gdal.__enter__()

    def tearDown(self):
        self._gdal.__exit__(None, None, None)
        shutil.rmtree(self.root, ignore_errors=True)

    def _both(self, name, **kw):
        path = write_tif(os.path.join(self.root, name), kw.pop("bounds", LV95), **kw)
        fast = ti.probe_geotiff_fast(path)
        with mock.patch.object(ti, "probe_geotiff_fast", return_value=None):
            slow = ti.probe_file(path)
        return path, fast, slow

    def test_ordinary_variants_are_identical(self):
        g = GDAL
        cases = [
            ("lv95_f32.tif", {}),
            ("lv95_int16_lzw_tiled.tif", {"dtype": g.GDT_Int16, "options": ["COMPRESS=LZW", "TILED=YES",
                                                                           "BLOCKXSIZE=16", "BLOCKYSIZE=16"]}),
            ("lv95_deflate_pred3.tif", {"options": ["COMPRESS=DEFLATE", "PREDICTOR=3"]}),
            ("lv95_zstd.tif", {"options": ["COMPRESS=ZSTD"]}),
            ("lv95_packbits_uint16.tif", {"dtype": g.GDT_UInt16, "options": ["COMPRESS=PACKBITS"]}),
            ("lv95_bigtiff.tif", {"options": ["BIGTIFF=YES"]}),
            ("lv95_nodata.tif", {"nodata": -9999.0}),
            ("lv95_nodata_nan.tif", {"nodata": float("nan")}),
            ("lv95_scaled.tif", {"dtype": g.GDT_Int16, "scale": 0.1, "offset": 5.0}),
            ("lv95_byte.tif", {"dtype": g.GDT_Byte}),
            ("lv95_f64.tif", {"dtype": g.GDT_Float64}),
            ("wgs84.tif", {"bounds": WGS, "epsg": 4326}),
            ("wgs84_nonsquare.tif", {"bounds": WGS, "epsg": 4326, "size": (20, 40)}),
            ("utm32.tif", {"bounds": UTM, "epsg": 32632}),
        ]
        for name, kw in cases:
            with self.subTest(name):
                _path, fast, slow = self._both(name, **kw)
                self.assertTrue(slow["readable"], f"{name}: {slow.get('reason')}")
                self.assertSame(fast, slow, name)

    def test_pixel_is_point_gets_the_same_corner_shift(self):
        path = write_tif(os.path.join(self.root, "pip.tif"), LV95)
        ds = GDAL.Open(path, GDAL.GA_Update)
        ds.SetMetadataItem("AREA_OR_POINT", "Point")
        ds = None
        fast = ti.probe_geotiff_fast(path)
        with mock.patch.object(ti, "probe_geotiff_fast", return_value=None):
            slow = ti.probe_file(path)
        self.assertSame(fast, slow, "pixel-is-point")

    def test_unusual_files_are_left_to_gdal(self):
        g = GDAL
        declined = {
            "rgb.tif": {"bands": 3, "dtype": g.GDT_Byte},
            "nocrs.tif": {"epsg": None},
            "rotated.tif": {"gt": (2_600_000.0, 50.0, 5.0, 1_201_000.0, 5.0, -50.0)},
            "lerc.tif": {"options": ["COMPRESS=LERC"]},
            "custom_crs.tif": {"wkt": 'PROJCS["custom",GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],PARAMETER["latitude_of_origin",46],PARAMETER["central_meridian",8],PARAMETER["scale_factor",1],PARAMETER["false_easting",0],PARAMETER["false_northing",0],UNIT["metre",1]]'},
        }
        for name, kw in declined.items():
            with self.subTest(name):
                path = write_tif(os.path.join(self.root, name), LV95, **kw)
                self.assertIsNone(ti.probe_geotiff_fast(path), name)
                # …and the full probe still answers for it (through GDAL).
                self.assertIn("readable", ti.probe_file(path))

    def test_the_identity_geotransform_is_no_georeferencing_as_in_gdal(self):
        # The torture set's broken_nogt.tif, made here by its recipe
        # (tools/make_torture_project.py) so no gitignored fixture can be
        # missing: a PixelIsPoint GeoTIFF given the identity geotransform,
        # its CRS kept. On disk a matrix with origin 0.5/0.5 + PixelIsPoint,
        # which the corner shift turns into exactly (0, 1, 0, 0, 0, 1) — what
        # GDAL reports as absent.
        def recipe(name, gt):
            path = write_tif(os.path.join(self.root, name), WGS, epsg=4326)
            ds = GDAL.Open(path, GDAL.GA_Update)
            ds.SetMetadataItem("AREA_OR_POINT", "Point")
            ds = None
            ds = GDAL.Open(path, GDAL.GA_Update)
            wkt = ds.GetProjection()
            ds.SetGeoTransform(gt)
            ds.SetProjection(wkt)
            ds = None
            return path

        path = recipe("broken_nogt.tif", (0.0, 1.0, 0.0, 0.0, 0.0, 1.0))
        old = GDAL.GetConfigOption("GTIFF_POINT_GEO_IGNORE")
        GDAL.SetConfigOption("GTIFF_POINT_GEO_IGNORE", "TRUE")
        try:            # the matrix as written, without the corner shift
            self.assertEqual(GDAL.Open(path).GetGeoTransform(),
                             (0.5, 1.0, 0.0, 0.5, 0.0, 1.0))
        finally:
            GDAL.SetConfigOption("GTIFF_POINT_GEO_IGNORE", old)
        self.assertIsNone(ti.probe_geotiff_fast(path))
        with mock.patch.object(ti, "probe_geotiff_fast", return_value=None):
            slow = ti.probe_file(path)
        self.assertFalse(slow["readable"])
        self.assertTrue(slow["reason"].startswith("no georeferencing"),
                        slow["reason"])
        # The same recipe with a real geotransform (the same matrix and keys)
        # is an ordinary file: the fast probe declined the identity alone.
        twin = recipe("twin.tif", (7.40, 0.0005, 0.0, 46.90, 0.0, 0.0005))
        fast = ti.probe_geotiff_fast(twin)
        with mock.patch.object(ti, "probe_geotiff_fast", return_value=None):
            self.assertSame(fast, ti.probe_file(twin), "twin.tif")

    def test_a_sidecar_sends_the_file_to_gdal(self):
        path = write_tif(os.path.join(self.root, "side.tif"), LV95)
        open(path + ".aux.xml", "w").write("<PAMDataset/>")
        self.assertIsNone(ti.probe_geotiff_fast(path))

    def test_a_truncated_file_is_left_to_gdal(self):
        path = write_tif(os.path.join(self.root, "cut.tif"), LV95, size=(200, 200),
                         options=["TILED=YES"])
        size = os.path.getsize(path)
        with open(path, "r+b") as fh:
            fh.truncate(size // 2)
        self.assertIsNone(ti.probe_geotiff_fast(path))
        with mock.patch.object(ti, "probe_geotiff_fast", return_value=None):
            self.assertFalse(ti.probe_file(path)["readable"])

    def test_non_tiff_extensions_are_not_attempted(self):
        self.assertIsNone(ti.probe_geotiff_fast(os.path.join(self.root, "x.asc")))
        self.assertIsNone(ti.probe_geotiff_fast("/vsizip/a.zip/b.tif"))


# ---------------------------------------------------------------------------
# The converter probe path (aether_converter probe → entries_from_probe)
# ---------------------------------------------------------------------------

PROBE_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                             "probe_parity.jsonl")
#: The custom (non-EPSG) CRS the declined cases use.
CUSTOM_WKT = ('PROJCS["custom",GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
              'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
              'PARAMETER["latitude_of_origin",46],PARAMETER["central_meridian",8],PARAMETER["scale_factor",1],'
              'PARAMETER["false_easting",0],PARAMETER["false_northing",0],UNIT["metre",1]]')


def probe_cases():
    """``{name: write_tif kwargs}`` of the files the probe parity writes.
    ``fixtures/probe_parity.jsonl`` holds the real converter's lines for
    exactly these (regenerate with ``python3 tests/test_terrain_index_fast.py
    --record <aether_converter>`` after changing them). Extra keys: ``point``
    (AREA_OR_POINT=Point), ``cut`` (truncate to half its size)."""
    g = GDAL
    return {
        # Entries must equal the GDAL probe's:
        "p_lv95_f32.tif": {},
        "p_lv95_int16_lzw_tiled.tif": {"dtype": g.GDT_Int16, "options": [
            "COMPRESS=LZW", "TILED=YES", "BLOCKXSIZE=16", "BLOCKYSIZE=16"]},
        "p_lv95_deflate_pred3.tif": {"options": ["COMPRESS=DEFLATE", "PREDICTOR=3"]},
        "p_lv95_zstd.tif": {"options": ["COMPRESS=ZSTD"]},
        "p_lv95_packbits_uint16.tif": {"dtype": g.GDT_UInt16, "options": ["COMPRESS=PACKBITS"]},
        "p_lv95_uint32.tif": {"dtype": g.GDT_UInt32},
        "p_lv95_int32_tiled.tif": {"dtype": g.GDT_Int32, "options": [
            "TILED=YES", "BLOCKXSIZE=16", "BLOCKYSIZE=16"]},
        "p_lv95_bigtiff.tif": {"options": ["BIGTIFF=YES"]},
        "p_lv95_nodata.tif": {"nodata": -9999.0},
        "p_lv95_nodata_nan.tif": {"nodata": float("nan")},
        "p_lv95_scaled.tif": {"dtype": g.GDT_Int16, "scale": 0.1, "offset": 5.0},
        "p_lv95_byte.tif": {"dtype": g.GDT_Byte},
        "p_lv95_f64.tif": {"dtype": g.GDT_Float64},
        "p_lv95_point.tif": {"point": True},
        "p_lv95_rotated.tif": {"gt": (2_600_000.0, 50.0, 5.0, 1_201_000.0, 5.0, -50.0)},
        "p_lv95_big_tile.tif": {"bounds": {"west": 2_560_000.0, "east": 2_640_000.0,
                                           "south": 1_160_000.0, "north": 1_240_000.0},
                                "epsg": 2056},
        "p_wgs84.tif": {"bounds": WGS, "epsg": 4326},
        "p_wgs84_nonsquare.tif": {"bounds": WGS, "epsg": 4326, "size": (20, 40)},
        "p_utm32.tif": {"bounds": UTM, "epsg": 32632},
        "p_utm32_big.tif": {"bounds": {"west": 300_000.0, "east": 700_000.0,
                                       "south": 5_000_000.0, "north": 5_400_000.0},
                            "epsg": 32632},
        # Must be left to GDAL (ok:false, or a fact the shortcut does not vouch for):
        "d_rgb.tif": {"bands": 3, "dtype": g.GDT_Byte},
        "d_nocrs.tif": {"epsg": None},
        "d_lerc.tif": {"options": ["COMPRESS=LERC"]},
        "d_custom_crs.tif": {"wkt": CUSTOM_WKT},
        "d_int8.tif": {"dtype": g.GDT_Byte, "options": ["PIXELTYPE=SIGNEDBYTE"]},
        "d_cut.tif": {"size": (200, 200), "options": ["TILED=YES"], "cut": True},
        # Pixels cut off behind an intact header: ok:true, last_chunk_end past
        # the size — unreadable, as GDAL's block read says (test_compressed_…).
        "g_cut_deflate.tif": {"size": (200, 200), "options": ["TILED=YES", "COMPRESS=DEFLATE"],
                              "array": "noise", "cut": True},
    }


def write_case(root, name, kw):
    import numpy as np
    kw = dict(kw)
    if "bounds" not in kw and "epsg" not in kw and "wkt" not in kw:
        kw["epsg"] = 2056                 # the LV95 default bounds are LV95
    point, cut = kw.pop("point", False), kw.pop("cut", False)
    if kw.get("array") == "noise":
        w, h = kw.get("size", (20, 20))
        kw["array"] = np.random.default_rng(7).uniform(0, 3000, (h, w))
    path = write_tif(os.path.join(root, name), kw.pop("bounds", LV95), **kw)
    if point:
        ds = GDAL.Open(path, GDAL.GA_Update)
        ds.SetMetadataItem("AREA_OR_POINT", "Point")
        ds = None
    if cut:
        with open(path, "r+b") as fh:
            fh.truncate(os.path.getsize(path) // 2)
    return path


def real_converter():
    """``aether_converter`` >= 0.2.7 from AETHER_BIN_DIR or PATH, else None."""
    exe_name = "aether_converter" + (".exe" if os.name == "nt" else "")
    cands = []
    if os.environ.get("AETHER_BIN_DIR"):
        cands.append(os.path.join(os.environ["AETHER_BIN_DIR"], exe_name))
    found = shutil.which("aether_converter")
    if found:
        cands.append(found)
    for exe in cands:
        if not os.path.isfile(exe):
            continue
        try:
            out = subprocess.run([exe, "--version"], capture_output=True,
                                 text=True, timeout=60).stdout.split()
        except (OSError, subprocess.SubprocessError):
            continue
        if len(out) == 2 and out[0] == "aether_converter":
            parts = [int(x) for x in out[1].split(".")[:3] if x.isdigit()]
            if tuple(parts) >= (0, 2, 7):
                return exe
    return None


def recorded_line(name, path):
    """The fixture's line for *name*, re-bound to *path* (its stat facts).
    ``last_chunk_end`` keeps its recorded distance to the end of the file —
    another GDAL/zlib build compresses to other sizes, the layout (last block
    at the end; a cut file's half a file beyond it) stays."""
    with open(PROBE_FIXTURE, encoding="utf-8") as fh:
        lines = {json.loads(l)["path"]: json.loads(l) for l in fh if l.strip()}
    line = dict(lines[name])
    st = os.stat(path)
    end, size = line.get("last_chunk_end"), line.get("size")
    if type(end) is int and type(size) is int:
        line["last_chunk_end"] = end - size + st.st_size
    line.update(path=path, size=st.st_size, mtime=int(st.st_mtime),
                mtime_ns=st.st_mtime_ns)
    return line


def probe_facts(paths):
    """``({path: line}, source)`` for *paths* ({name: path}): the real
    converter's lines when there is one, else the recorded ones."""
    exe = real_converter()
    if exe is not None:
        run = ti.run_probe(exe, list(paths.values()))
        assert not run.error, run.error
        return run.facts, "converter"
    return {p: recorded_line(n, p) for n, p in paths.items()}, "recorded"


@unittest.skipUnless(HAVE_GDAL, "needs the real GDAL")
class TestConverterProbeMatchesGdal(_SameEntry, unittest.TestCase):
    """Entries built from aether-probe/1 lines equal the GDAL probe's."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        cls._gdal = real_gdal()
        cls._gdal.__enter__()
        cls.paths = {n: write_case(cls.root, n, kw) for n, kw in probe_cases().items()}
        cls.facts, cls.source = probe_facts(cls.paths)
        cls.entries = ti.entries_from_probe(cls.facts)

    @classmethod
    def tearDownClass(cls):
        cls._gdal.__exit__(None, None, None)
        shutil.rmtree(cls.root, ignore_errors=True)

    def gdal_entry(self, path):
        with mock.patch.object(ti, "probe_geotiff_fast", return_value=None):
            return ti.probe_file(path)

    def test_every_listed_file_has_its_line(self):
        self.assertEqual(set(self.facts), set(self.paths.values()), self.source)
        for p, f in self.facts.items():
            self.assertEqual(f["schema"], ti.PROBE_SCHEMA)
            st = os.stat(p)
            self.assertEqual((f["size"], f["mtime_ns"]), (st.st_size, st.st_mtime_ns))
            if f["ok"]:     # toolkit changelog 35; a stale converter/fixture fails here
                self.assertIn("last_chunk_end", f, f"{self.source}: {p}")

    def test_probe_built_entries_equal_gdal_built_entries(self):
        for name, path in self.paths.items():
            if not name.startswith("p_"):
                continue
            with self.subTest(name, source=self.source):
                slow = self.gdal_entry(path)
                self.assertTrue(slow["readable"], f"{name}: {slow.get('reason')}")
                built = self.entries.get(path)
                self.assertIsNotNone(built, f"{name}: the probe path declined "
                                            f"an ordinary file ({self.facts[path]})")
                self.assertSame(built, slow, name)
                self.assertEqual(ti.problem_of(built), ti.problem_of(slow), name)
                self.assertEqual(ti.materialize_reason(built, path),
                                 ti.materialize_reason(dict(slow, size=built.get("size")), path),
                                 name)

    def test_files_gdal_must_judge_get_no_probe_entry(self):
        for name, path in self.paths.items():
            if not name.startswith("d_"):
                continue
            with self.subTest(name, source=self.source):
                self.assertNotIn(path, self.entries, f"{name}: {self.facts[path]}")
                self.assertIn("readable", ti.probe_file(path))
        # The truncated uncompressed file is the GDAL probe's to call truncated.
        self.assertFalse(self.gdal_entry(self.paths["d_cut.tif"])["readable"])

    def test_the_identity_geotransform_is_left_to_gdal(self):
        # torture broken_nogt.tif: the probe reports the matrix + PixelIsPoint
        # shift as exactly (0, 1, 0, 0, 0, 1), which GDAL calls "no geotransform".
        line = dict(self.facts[self.paths["p_lv95_f32.tif"]],
                    geotransform=[0.0, 1.0, 0.0, 0.0, 0.0, 1.0], epsg=4326)
        self.assertEqual(ti.entries_from_probe({"x.tif": line}), {})

    def test_lines_the_mapping_does_not_know_are_left_to_gdal(self):
        base = self.facts[self.paths["p_lv95_f32.tif"]]
        for change in ({"ok": False, "reason": "corrupt TIFF: x"}, {"schema": "aether-probe/2"},
                       {"dtype": "u64"}, {"dtype": "f16"}, {"compression": {"code": 34887, "name": "LERC"}},
                       {"epsg": None}, {"epsg": 32767}, {"proj": "+proj=tmerc"}, {"bands": 2},
                       {"nodata": "none"}, {"geotransform": [1.0, 2.0]},
                       {"geotransform": [float("nan")] * 6}, {"width": 0}):
            with self.subTest(change):
                self.assertEqual(ti.entries_from_probe({"x.tif": dict(base, **change)}), {})

    def test_sidecar_files_never_go_to_the_probe(self):
        path = os.path.join(self.root, "side.tif")
        siblings = {path.lower(), (path + ".aux.xml").lower()}
        self.assertFalse(ti._probe_candidate(path, siblings))
        self.assertFalse(ti._probe_candidate(path[:-4] + ".asc", set()))
        self.assertFalse(ti._probe_candidate("/vsizip/a.zip/b.tif", set()))
        self.assertTrue(ti._probe_candidate(path, {path.lower()}))

    def test_compressed_pixels_cut_off_behind_the_header(self):
        """A COMPRESSED file cut off behind an intact header is ok:true to the
        probe, but its last_chunk_end (the end of the bottom-right block,
        toolkit changelog 35) lies past its size — the block GDAL's probe
        reads and fails on. The probe path gives GDAL's entry: unreadable,
        the same reason text, no CRS/geometry. (Was a known gap until the
        toolkit reported where the pixel data ends.)"""
        path = self.paths["g_cut_deflate.tif"]
        line = self.facts[path]
        self.assertTrue(line["ok"], line)
        self.assertGreater(line["last_chunk_end"], line["size"], self.source)
        slow = self.gdal_entry(path)
        built = self.entries.get(path)
        self.assertIsNotNone(built, f"{self.source}: {line}")
        self.assertFalse(slow["readable"], slow)
        self.assertFalse(built["readable"], built)
        self.assertEqual(ti.problem_of(built)[0], ti.problem_of(slow)[0])
        self.assertEqual(ti.problem_of(built)[0], ti.KIND_UNUSABLE)
        for e in (built, slow):
            self.assertTrue(e["reason"].startswith(ti.TRUNCATED_REASON + " ("), e["reason"])
        self.assertEqual(set(built), set(slow) - {"size", "mtime_ns"})
        for k in EXACT:
            if k != "reason":
                self.assertEqual(built.get(k), slow.get(k), k)
        for k in ("scale", "offset", "nodata"):
            self.assertTrue(_close(built[k], slow[k]), k)
        self.assertEqual(built["geotransform"], slow["geotransform"])

    def test_last_chunk_end_null_or_past_the_size_is_truncated(self):
        whole = self.facts[self.paths["p_lv95_deflate_pred3.tif"]]
        size = whole["size"]
        self.assertLessEqual(whole["last_chunk_end"], size, self.source)
        self.assertTrue(ti.entries_from_probe({"x.tif": whole})["x.tif"]["readable"])
        for end in (None, size + 1):
            with self.subTest(last_chunk_end=end):
                e = ti.entries_from_probe({"x.tif": dict(whole, last_chunk_end=end)})["x.tif"]
                self.assertFalse(e["readable"])
                self.assertEqual(ti.problem_of(e)[0], ti.KIND_UNUSABLE)
                self.assertTrue(e["reason"].startswith(ti.TRUNCATED_REASON + " ("), e["reason"])
                self.assertNotIn("crs_authid", e)
        # Not a count: GDAL judges.
        for bad in ("12", -1, True, 1.5):
            with self.subTest(last_chunk_end=bad):
                self.assertEqual(ti.entries_from_probe({"x.tif": dict(whole, last_chunk_end=bad)}), {})
        # A converter built before changelog 35 (no field): compressed pixels
        # are not vouched for (GDAL judges); uncompressed ones keep the size rule.
        old = {k: v for k, v in whole.items() if k != "last_chunk_end"}
        self.assertEqual(ti.entries_from_probe({"x.tif": old}), {})
        plain = self.facts[self.paths["p_lv95_f32.tif"]]
        self.assertEqual(plain["compression"]["code"], 1)
        old_plain = {k: v for k, v in plain.items() if k != "last_chunk_end"}
        self.assertTrue(ti.entries_from_probe({"x.tif": old_plain})["x.tif"]["readable"])


def _record(exe):
    """Write fixtures/probe_parity.jsonl from the real converter."""
    root = tempfile.mkdtemp()
    try:
        with real_gdal():
            paths = {n: write_case(root, n, kw) for n, kw in probe_cases().items()}
        run = ti.run_probe(exe, list(paths.values()))
        assert not run.error and len(run.facts) == len(paths), run
        with open(PROBE_FIXTURE, "w", encoding="utf-8", newline="\n") as fh:
            for n, p in paths.items():
                fh.write(json.dumps(dict(run.facts[p], path=n)) + "\n")
        print(f"recorded {len(paths)} lines with {exe}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--record":
        _record(sys.argv[2])
    else:
        unittest.main()
