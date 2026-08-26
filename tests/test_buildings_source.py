"""Unit tests for core.buildings_source — vector buildings → FlatGeobuf.

The converter reads FlatGeobuf in WGS84 and NOTHING else (no GDAL/OGR in the
toolkit, by design), and it does not refuse what it cannot read: it prints one
``[Warn]`` line, exits 0, and writes terrain with no buildings on it. So the
only proof that a source "reaches the converter" is that what we hand over is
a real .fgb, in degrees, with its attributes intact — which is what these
tests check, with real GDAL, on real files.

conftest stubs ``osgeo`` for the whole session so the other unit tests never
need GDAL; :func:`real_osgeo` borrows the real package back for the duration
of a test and puts the stub straight back.
"""

# Bootstrap stubs before any plugin import.
import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import contextlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from waveshed.core import buildings_source as bs


@contextlib.contextmanager
def real_osgeo():
    """Swap conftest's ``osgeo`` stub for the real package, then swap back."""
    stubs = {name: mod for name, mod in sys.modules.items()
             if name == "osgeo" or name.startswith("osgeo.")}
    for name in stubs:
        sys.modules.pop(name, None)
    try:
        from osgeo import gdal, ogr, osr
        yield gdal, ogr, osr
    finally:
        for name in [n for n in list(sys.modules)
                     if n == "osgeo" or n.startswith("osgeo.")]:
            sys.modules.pop(name, None)
        sys.modules.update(stubs)


def _have_real_gdal() -> bool:
    try:
        with real_osgeo() as (_gdal, ogr, _osr):
            return ogr.GetDriverByName("FlatGeobuf") is not None
    except Exception:  # noqa: BLE001 — no GDAL here is a skip, not an error
        return False


_HAVE_GDAL = _have_real_gdal()

#: FlatGeobuf's file signature — what the converter checks before anything
#: else ("Missing magic bytes. Is this an fgb file?").
_FGB_MAGIC = b"fgb\x03fgb\x01"

_BERN = (7.44, 46.94)


class TestSublayerSplit(unittest.TestCase):
    """QGIS spells a container sublayer "<file>|layername=<x>"."""

    def test_plain_path_is_unchanged(self):
        self.assertEqual(bs._split_sublayer("/data/b.fgb"), ("/data/b.fgb", ""))

    def test_layername_is_split_off(self):
        self.assertEqual(bs._split_sublayer("/d/x.gpkg|layername=buildings"),
                         ("/d/x.gpkg", "buildings"))


class TestIsConverterReadable(unittest.TestCase):
    """What ingest.rs `apply_buildings` can actually open."""

    def test_an_fgb_file_is_readable(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "b.fgb")
            open(p, "wb").close()
            self.assertTrue(bs.is_converter_readable(p))

    def test_a_directory_of_fgb_parts_is_readable(self):
        # apply_buildings scans a directory for *.fgb — that is a supported
        # buildings source, not an accident.
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "part1.fgb"), "wb").close()
            self.assertTrue(bs.is_converter_readable(d))

    def test_an_empty_directory_is_not(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(bs.is_converter_readable(d))

    def test_a_geojson_is_not(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "b.geojson")
            open(p, "w").close()
            self.assertFalse(bs.is_converter_readable(p))

    def test_a_sublayer_uri_is_not(self):
        # The raw URI is exactly what used to reach File::open, which fails
        # with ENOENT on the "|layername=" suffix.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.gpkg")
            open(p, "wb").close()
            self.assertFalse(bs.is_converter_readable(f"{p}|layername=b"))

    def test_a_missing_path_is_not(self):
        self.assertFalse(bs.is_converter_readable("/no/such/buildings.fgb"))
        self.assertFalse(bs.is_converter_readable(""))


@unittest.skipUnless(_HAVE_GDAL, "needs the real osgeo/GDAL with FlatGeobuf")
class TestRealConversions(unittest.TestCase):
    """Torture rows 3.2a/3.2b/3.3/3.5/3.6, with real files and real GDAL."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        cache = os.path.join(self.dir, "fgb_cache")
        os.makedirs(cache)
        # Keep the session-spanning conversion cache out of the test.
        patcher = mock.patch.object(bs, "fgb_cache_dir", return_value=cache)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.logged = []

    def _resolve(self, source, src_crs=""):
        with real_osgeo():
            return bs.resolve_buildings_source(source, src_crs,
                                               log=self.logged.append)

    def _assert_is_fgb(self, path):
        self.assertTrue(os.path.isfile(path), f"{path} is not a file")
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(len(_FGB_MAGIC)), _FGB_MAGIC,
                             "the converter's first check is the magic bytes")
        self.assertTrue(bs.is_converter_readable(path))

    def _read_back(self, path, layer_name=None):
        with real_osgeo() as (_gdal, ogr, _osr):
            ds = ogr.Open(path)
            layer = (ds.GetLayerByName(layer_name) if layer_name
                     else ds.GetLayer(0))
            defn = layer.GetLayerDefn()
            return {
                "name": layer.GetName(),
                "count": layer.GetFeatureCount(),
                "extent": layer.GetExtent(),
                "fields": [defn.GetFieldDefn(i).GetName()
                           for i in range(defn.GetFieldCount())],
            }

    def _geojson(self, name="osm_bern.geojson"):
        lon, lat = _BERN
        ring = [[lon, lat], [lon + 1e-4, lat], [lon + 1e-4, lat + 1e-4],
                [lon, lat + 1e-4], [lon, lat]]
        path = os.path.join(self.dir, name)
        with open(path, "w") as fh:
            json.dump({"type": "FeatureCollection", "features": [{
                "type": "Feature", "properties": {"height": 12.0},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }]}, fh)
        return path

    def _shapefile_lv95(self, name="lv95_buildings.shp"):
        """A shapefile in EPSG:2056 — Bern's Bundesplatz, in LV95 metres."""
        path = os.path.join(self.dir, name)
        with real_osgeo() as (_gdal, ogr, osr):
            ds = ogr.GetDriverByName("ESRI Shapefile").CreateDataSource(path)
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(2056)
            layer = ds.CreateLayer("lv95_buildings", srs, ogr.wkbPolygon)
            layer.CreateField(ogr.FieldDefn("height", ogr.OFTReal))
            feat = ogr.Feature(layer.GetLayerDefn())
            feat.SetField("height", 12.0)
            feat.SetGeometry(ogr.CreateGeometryFromWkt(
                "POLYGON((2600000 1200000,2600010 1200000,"
                "2600010 1200010,2600000 1200010,2600000 1200000))"))
            layer.CreateFeature(feat)
            ds = None
        return path

    def _gpkg_two_layers(self, name="multi.gpkg"):
        path = os.path.join(self.dir, name)
        with real_osgeo() as (_gdal, ogr, osr):
            ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(4326)
            for layer_name, lon in (("buildings", 7.44), ("roads", 7.50)):
                layer = ds.CreateLayer(layer_name, srs, ogr.wkbPolygon)
                feat = ogr.Feature(layer.GetLayerDefn())
                feat.SetGeometry(ogr.CreateGeometryFromWkt(
                    f"POLYGON(({lon} 46.94,{lon + 0.001} 46.94,"
                    f"{lon + 0.001} 46.941,{lon} 46.941,{lon} 46.94))"))
                layer.CreateFeature(feat)
            ds = None
        return path

    def test_geojson_becomes_a_real_fgb(self):
        # "Missing magic bytes. Is this an fgb file?" was the whole of row 3.3.
        out = self._resolve(self._geojson())
        self._assert_is_fgb(out)
        info = self._read_back(out)
        self.assertEqual(info["count"], 1)
        self.assertIn("height", info["fields"],
                      "attribute heights must survive the conversion")

    def test_the_output_is_a_file_not_a_directory_named_fgb(self):
        # Regression: the ".part" temp name has no .fgb extension, so GDAL's
        # FlatGeobuf driver wrote a DIRECTORY dataset and the rename made it a
        # directory called "<name>.fgb". Nothing failed loudly — the converter
        # scans a directory of parts too — but the cache never held the file
        # it claims to, and opening it as one gives EISDIR.
        out = self._resolve(self._geojson())
        self.assertFalse(os.path.isdir(out), out)
        self.assertTrue(os.path.isfile(out), out)

    def test_lv95_shapefile_is_reprojected_to_wgs84(self):
        out = self._resolve(self._shapefile_lv95())
        self._assert_is_fgb(out)
        info = self._read_back(out)
        min_x, max_x, min_y, max_y = info["extent"]
        # 2600000/1200000 in LV95 is Bern: 7.4386 E, 46.9511 N.
        self.assertAlmostEqual(min_x, 7.4386, places=2)
        self.assertAlmostEqual(min_y, 46.9511, places=2)
        self.assertLess(max_x - min_x, 0.01)
        self.assertLess(max_y - min_y, 0.01)
        self.assertIn("height", info["fields"])

    def test_gpkg_sublayer_converts_the_layer_that_was_asked_for(self):
        gpkg = self._gpkg_two_layers()
        out = self._resolve(f"{gpkg}|layername=buildings")
        self._assert_is_fgb(out)
        self.assertNotIn("|", out, "the raw QGIS URI must not survive")
        info = self._read_back(out)
        self.assertEqual(info["count"], 1)
        # "buildings" sits at 7.44, "roads" at 7.50 — this pins which one.
        self.assertAlmostEqual(info["extent"][0], 7.44, places=3)

    def test_the_two_sublayers_are_two_conversions(self):
        gpkg = self._gpkg_two_layers()
        buildings = self._resolve(f"{gpkg}|layername=buildings")
        roads = self._resolve(f"{gpkg}|layername=roads")
        self.assertNotEqual(buildings, roads)
        self.assertAlmostEqual(self._read_back(roads)["extent"][0], 7.50,
                               places=3)

    def test_an_existing_wgs84_fgb_is_passed_through_untouched(self):
        # Row 3.6: a FlatGeobuf that is already WGS84 must not be re-converted.
        source = self._resolve(self._geojson())      # a real .fgb to start from
        out = self._resolve(source)
        self.assertEqual(out, os.path.abspath(source))

    def test_a_declared_crs_reprojects_an_fgb(self):
        # An .fgb whose coordinates are NOT degrees, declared by the layer:
        # passing it through would place the buildings in the ocean.
        source = self._resolve(self._shapefile_lv95())
        out = self._resolve(source, src_crs="EPSG:2056")
        self.assertNotEqual(out, os.path.abspath(source))
        self._assert_is_fgb(out)

    def test_an_unconvertible_source_comes_back_unchanged_and_unreadable(self):
        # The caller (prepare_terrain) has to be able to SEE that the burn
        # will not happen; silently dropping the path would hide it.
        path = os.path.join(self.dir, "buildings.txt")
        with open(path, "w") as fh:
            fh.write("not a vector file")
        out = self._resolve(path)
        self.assertEqual(out, path)
        self.assertFalse(bs.is_converter_readable(out))

    def test_a_conversion_is_cached_and_reused(self):
        source = self._geojson()
        first = self._resolve(source)
        stamp = os.stat(first).st_mtime_ns
        second = self._resolve(source)
        self.assertEqual(first, second)
        self.assertEqual(os.stat(second).st_mtime_ns, stamp,
                         "the cached conversion was rebuilt")

    def test_editing_the_source_invalidates_the_conversion(self):
        source = self._geojson()
        first = self._resolve(source)
        with open(source, "a") as fh:
            fh.write("\n")          # changes size + mtime → new fingerprint
        self.assertNotEqual(self._resolve(source), first)

    def _gpkg_tin(self, name="tin_roofs.gpkg"):
        """A TIN Z layer — the shape of swissBUILDINGS3D 3.0 GDB roofs."""
        path = os.path.join(self.dir, name)
        with real_osgeo() as (_gdal, ogr, osr):
            ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(4326)
            layer = ds.CreateLayer("roofs", srs, ogr.wkbTINZ)
            for wkt in (
                "TIN Z (((7.423 46.957 550,7.424 46.957 550,"
                "7.4235 46.9578 560,7.423 46.957 550)))",
                "TIN Z (((7.425 46.956 555,7.426 46.956 555,"
                "7.4255 46.9568 565,7.425 46.956 555)))",
            ):
                feat = ogr.Feature(layer.GetLayerDefn())
                feat.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
                layer.CreateFeature(feat)
            ds = None
        return path

    def test_a_tin_source_converts_with_all_its_features(self):
        # swissBUILDINGS3D 3.0 (torture row 3.1): PROMOTE_TO_MULTI turns TIN
        # into MultiSurface, the FlatGeobuf writer rejects every feature, and
        # -skipfailures delivered a structurally valid, EMPTY .fgb that
        # burned nothing. A TIN source must be written natively instead.
        out = self._resolve(self._gpkg_tin() + "|layername=roofs")
        self._assert_is_fgb(out)
        self.assertEqual(self._read_back(out)["count"], 2)

    def test_an_empty_conversion_is_refused_not_cached(self):
        # The failure shape behind the TIN bug, pinned on its own: whatever
        # makes VectorTranslate write none of the source's features, the
        # empty file must not come back as a "conversion" — cached, it would
        # burn nothing forever, and is_converter_readable would say fine.
        src = self._geojson()

        def fake_count(path, layer_name=""):
            return 0 if path.lower().endswith(".fgb") else 5

        with mock.patch.object(bs, "_layer_feature_count",
                               side_effect=fake_count):
            out = self._resolve(src)
        self.assertEqual(out, os.path.abspath(src))
        self.assertFalse(bs.is_converter_readable(out))
        cache = bs.fgb_cache_dir()
        self.assertEqual([f for f in os.listdir(cache)
                          if f.endswith(".fgb")], [],
                         "the empty result must not enter the cache")


if __name__ == "__main__":
    unittest.main()
