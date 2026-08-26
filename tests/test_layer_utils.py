"""Unit tests for core.layer_utils DEM/imagery classification.

QGIS stubs provided by conftest.py.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import unittest
from unittest import mock

from waveshed.core.layer_utils import (
    aether_role,
    classify_raster_layer,
    dem_layer_warning,
    hide_from_dem_picker,
    is_aether_output,
    mark_aether_output,
)

# Byte in the stubbed Qgis.DataType (see conftest); Float32 = 6.
_BYTE = 0
_FLOAT32 = 6
_INT16 = 2
_ARGB32 = 12


class _FakeLayer:
    """A raster layer with no ``providerType()``, like the QGIS objects that
    predate it — classification must fall back to the URI's own spelling."""

    def __init__(self, source="", bands=1, dtype=_FLOAT32, name="layer"):
        self._source, self._bands, self._dtype, self._name = source, bands, dtype, name

    def source(self):
        return self._source

    def name(self):
        return self._name

    def dataProvider(self):
        prov = mock.Mock()
        prov.bandCount.return_value = self._bands
        prov.dataType.return_value = self._dtype
        return prov


class _ProviderLayer(_FakeLayer):
    """A raster layer that reports its provider, as QGIS layers do."""

    def __init__(self, provider="gdal", **kwargs):
        super().__init__(**kwargs)
        self._provider = provider

    def providerType(self):
        return self._provider


class TestClassifyRasterLayer(unittest.TestCase):

    def test_osm_xyz_is_imagery(self):
        src = "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png"
        self.assertEqual(classify_raster_layer(_FakeLayer(source=src)), "imagery")

    def test_satellite_xyz_is_imagery(self):
        src = "type=xyz&url=https://server/World_Imagery/{z}/{y}/{x}"
        self.assertEqual(classify_raster_layer(_FakeLayer(source=src)), "imagery")

    def test_terrarium_xyz_is_dem(self):
        # The plugin's own primary terrain source must NOT be flagged.
        src = "type=xyz&url=https://s3.amazonaws.com/elevation-tiles/terrarium/{z}/{x}/{y}.png"
        self.assertEqual(classify_raster_layer(_FakeLayer(source=src)), "dem")

    def test_mapbox_terrainrgb_xyz_is_dem(self):
        src = "type=xyz&interpretation=terrainrgb&url=https://api.mapbox.com/{z}/{x}/{y}.png"
        self.assertEqual(classify_raster_layer(_FakeLayer(source=src)), "dem")

    def test_unknown_xyz_defaults_to_imagery(self):
        src = "type=xyz&url=https://example.com/{z}/{x}/{y}.png"
        self.assertEqual(classify_raster_layer(_FakeLayer(source=src)), "imagery")

    def test_local_float32_singleband_is_dem(self):
        lyr = _FakeLayer(source="/data/swissalti.tif", bands=1, dtype=_FLOAT32)
        self.assertEqual(classify_raster_layer(lyr), "dem")

    def test_local_int16_singleband_is_dem(self):
        lyr = _FakeLayer(source="/data/srtm.tif", bands=1, dtype=_INT16)
        self.assertEqual(classify_raster_layer(lyr), "dem")

    def test_local_rgb_is_imagery(self):
        lyr = _FakeLayer(source="/data/ortho.tif", bands=3, dtype=_BYTE)
        self.assertEqual(classify_raster_layer(lyr), "imagery")

    def test_local_singleband_byte_is_imagery(self):
        lyr = _FakeLayer(source="/data/mask.tif", bands=1, dtype=_BYTE)
        self.assertEqual(classify_raster_layer(lyr), "imagery")


class TestRenderedServiceLayers(unittest.TestCase):
    """WMS/WMTS return a *picture*, so band heuristics cannot classify them.

    A WMS basemap reports a single non-Byte ARGB32 band, so the band checks
    fell straight through to "dem" and the plugin happily ran propagation over
    a rendered street map. Only "type=xyz" was URL-aware before this.
    """

    _WMS_BASEMAP = ("contextualWMSLegend=0&crs=EPSG:3857&dpiMode=7"
                    "&format=image/png&layers=osm&styles"
                    "&url=https://tile.openstreetmap.org/wms")

    def test_wms_basemap_is_imagery(self):
        lyr = _ProviderLayer(provider="wms", source=self._WMS_BASEMAP,
                             bands=1, dtype=_FLOAT32)
        self.assertEqual(classify_raster_layer(lyr), "imagery")

    def test_wms_without_any_url_hint_is_still_imagery(self):
        src = ("crs=EPSG:2056&format=image/png&layers=ch.swisstopo.pixelkarte"
               "&url=https://wms.geo.admin.ch/")
        lyr = _ProviderLayer(provider="wms", source=src, bands=1, dtype=_FLOAT32)
        self.assertEqual(classify_raster_layer(lyr), "imagery")

    def test_an_elevation_sounding_wms_is_imagery_anyway(self):
        # USGS 3DEP's WMS lives at elevation.nationalmap.gov and serves shaded
        # relief. The URL hint used to win and the layer was offered as a DEM,
        # so an analysis ran over a picture of terrain and failed silently.
        # A WMS cannot deliver values at all: Qt decodes the response with
        # QImage, which refuses 32-bit samples, so even image/tiff draws
        # nothing. The provider kind wins over the URL.
        src = ("crs=EPSG:4326&format=image/tiff&layers=elevation"
               "&url=https://elevation.nationalmap.gov/arcgis/services/dem/wms")
        lyr = _ProviderLayer(provider="wms", source=src, bands=1, dtype=_FLOAT32)
        self.assertEqual(classify_raster_layer(lyr), "imagery")

    def test_a_wcs_elevation_service_is_still_a_dem(self):
        # The counterpart: a COVERAGE service does return real values, so the
        # elevation hint is trustworthy there. This is where 3DEP's values
        # actually come from.
        src = ("cache=PreferNetwork&crs=EPSG:4326&format=GeoTIFF"
               "&identifier=elevation&url=https://example.org/dem/wcs")
        lyr = _ProviderLayer(provider="wcs", source=src, bands=1, dtype=_FLOAT32)
        self.assertEqual(classify_raster_layer(lyr), "dem")

    def test_wmts_basemap_is_imagery(self):
        src = ("crs=EPSG:3857&format=image/jpeg&layers=aerial&type=wmts"
               "&tileMatrixSet=GoogleMapsCompatible&url=https://example.org/wmts")
        lyr = _ProviderLayer(provider="wms", source=src, bands=1, dtype=_FLOAT32)
        self.assertEqual(classify_raster_layer(lyr), "imagery")

    def test_wmts_source_marker_works_without_a_provider_type(self):
        src = ("crs=EPSG:3857&type=wmts&tileMatrixSet=GoogleMapsCompatible"
               "&url=https://example.org/wmts")
        self.assertEqual(classify_raster_layer(_FakeLayer(source=src)), "imagery")

    def test_wcs_coverage_falls_through_to_the_band_heuristics(self):
        # WCS returns real values (GeoTIFF), not a rendering, so a single
        # Float32 band genuinely is elevation.
        src = ("cache=PreferNetwork&crs=EPSG:4326&format=GeoTIFF"
               "&identifier=height&url=https://example.org/wcs")
        lyr = _ProviderLayer(provider="wcs", source=src, bands=1, dtype=_FLOAT32)
        self.assertEqual(classify_raster_layer(lyr), "dem")

    def test_wcs_rgb_coverage_is_still_imagery(self):
        src = ("crs=EPSG:4326&format=GeoTIFF&identifier=ortho"
               "&url=https://example.org/wcs")
        lyr = _ProviderLayer(provider="wcs", source=src, bands=3, dtype=_BYTE)
        self.assertEqual(classify_raster_layer(lyr), "imagery")

    def test_argb32_band_is_imagery_whatever_the_provider(self):
        # Backstop for any rendered-image provider not named explicitly.
        lyr = _ProviderLayer(provider="somethingelse", source="/x",
                             bands=1, dtype=_ARGB32)
        self.assertEqual(classify_raster_layer(lyr), "imagery")

    def test_a_local_gdal_dem_is_unaffected(self):
        lyr = _ProviderLayer(provider="gdal", source="/data/swissalti.tif",
                             bands=1, dtype=_FLOAT32)
        self.assertEqual(classify_raster_layer(lyr), "dem")

    def test_xyz_provider_type_is_still_url_classified(self):
        src = "type=xyz&url=https://s3.amazonaws.com/x/terrarium/{z}/{x}/{y}.png"
        lyr = _ProviderLayer(provider="wms", source=src, bands=1, dtype=_FLOAT32)
        self.assertEqual(classify_raster_layer(lyr), "dem")


class TestDemLayerWarning(unittest.TestCase):

    def test_warns_on_imagery(self):
        src = "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png"
        msg = dem_layer_warning(_FakeLayer(source=src, name="OpenStreetMap"))
        self.assertIsNotNone(msg)
        self.assertIn("OpenStreetMap", msg)

    def test_no_warning_on_dem(self):
        lyr = _FakeLayer(source="/data/swissalti.tif", bands=1, dtype=_FLOAT32)
        self.assertIsNone(dem_layer_warning(lyr))


class _PropLayer:
    """Fake layer with QGIS-style custom-property storage."""

    def __init__(self):
        self._props = {}

    def setCustomProperty(self, key, value):
        self._props[key] = value

    def customProperty(self, key, default=None):
        return self._props.get(key, default)


class TestAetherOutputTag(unittest.TestCase):

    def test_unmarked_layer_is_not_output(self):
        self.assertFalse(is_aether_output(_PropLayer()))

    def test_marked_layer_is_output(self):
        lyr = _PropLayer()
        mark_aether_output(lyr)
        self.assertTrue(is_aether_output(lyr))

    def test_layer_without_customproperty_api_is_safe(self):
        # Must not raise on an object lacking the QGIS custom-property API.
        self.assertFalse(is_aether_output(object()))
        mark_aether_output(object())  # no-op, must not raise

    def test_coverage_result_is_hidden_from_dem_picker(self):
        lyr = _PropLayer()
        mark_aether_output(lyr, "coverage")
        self.assertEqual(aether_role(lyr), "coverage")
        self.assertTrue(hide_from_dem_picker(lyr))

    def test_terrain_output_is_kept_in_dem_picker(self):
        # Terrain we generated IS valid elevation — must stay selectable.
        lyr = _PropLayer()
        mark_aether_output(lyr, "terrain")
        self.assertTrue(is_aether_output(lyr))
        self.assertFalse(hide_from_dem_picker(lyr))

    def test_foreign_layer_is_kept_in_dem_picker(self):
        self.assertFalse(hide_from_dem_picker(_PropLayer()))


if __name__ == "__main__":
    unittest.main()
