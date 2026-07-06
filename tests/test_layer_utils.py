"""Unit tests for core.layer_utils DEM/imagery classification.

QGIS stubs provided by conftest.py.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import unittest
from unittest import mock

from aether_qgis.core.layer_utils import (
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


class _FakeLayer:
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
