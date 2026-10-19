"""The fast GeoTIFF header probe answers exactly what the GDAL probe answers.

``terrain_index.probe_geotiff_fast`` reads an ordinary GeoTIFF's first IFD
without GDAL (~40x faster per file). It must either decline a file (None → the
GDAL probe handles it) or return the same index entry the GDAL probe returns —
for every variant a terrain folder can hold.
"""

import math
import os
import shutil
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


@unittest.skipUnless(HAVE_GDAL, "needs the real GDAL")
class TestFastProbeMatchesGdal(unittest.TestCase):
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
        # data/torture broken_nogt.tif: matrix origin 0.5/0.5 + PixelIsPoint
        # shifts to exactly (0, 1, 0, 0, 0, 1), which GDAL reports as absent.
        real = os.path.join(os.path.dirname(__file__), "..", "data", "torture",
                            "dem", "broken", "broken_nogt.tif")
        if not os.path.exists(real):
            self.skipTest("torture fixture not present")
        self.assertIsNone(ti.probe_geotiff_fast(real))
        with mock.patch.object(ti, "probe_geotiff_fast", return_value=None):
            self.assertFalse(ti.probe_file(real)["readable"])

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


if __name__ == "__main__":
    unittest.main()
