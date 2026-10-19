"""Borrow the REAL osgeo (GDAL/OSR) for tests that need real rasters.

conftest stubs ``osgeo`` for the whole session; the folder index
(core/terrain_index) and the materialize path are only meaningful against
real files, so these helpers import the real package once (with the stub
swapped out for the import) and patch it into the plugin modules for the
duration of a test.
"""

import contextlib
import sys
from unittest import mock


def _import_real():
    stubs = {name: mod for name, mod in sys.modules.items()
             if name == "osgeo" or name.startswith("osgeo.")}
    for name in stubs:
        sys.modules.pop(name, None)
    try:
        from osgeo import gdal, gdal_array, osr  # noqa: F401
        real = {n: m for n, m in sys.modules.items()
                if n == "osgeo" or n.startswith("osgeo.")}
        return gdal, osr, real
    except Exception:  # noqa: BLE001 — no GDAL: callers skip
        return None, None, {}
    finally:
        for name in [n for n in list(sys.modules)
                     if n == "osgeo" or n.startswith("osgeo.")]:
            sys.modules.pop(name, None)
        sys.modules.update(stubs)


GDAL, OSR, _REAL_MODULES = _import_real()
HAVE_GDAL = GDAL is not None and hasattr(GDAL, "PushErrorHandler")


@contextlib.contextmanager
def real_gdal():
    """Patch the real gdal/osr into terrain_index and terrain_adapter."""
    from waveshed.core import terrain_adapter, terrain_index
    stubs = {name: mod for name, mod in sys.modules.items()
             if name == "osgeo" or name.startswith("osgeo.")}
    sys.modules.update(_REAL_MODULES)
    try:
        with mock.patch.object(terrain_index, "gdal", GDAL), \
             mock.patch.object(terrain_index, "osr", OSR), \
             mock.patch.object(terrain_adapter, "gdal", GDAL):
            yield GDAL, OSR
    finally:
        for name in [n for n in list(sys.modules)
                     if n == "osgeo" or n.startswith("osgeo.")]:
            sys.modules.pop(name, None)
        sys.modules.update(stubs)


def write_tif(path, bounds, epsg=4326, value=500.0, size=(20, 20),
              dtype=None, nodata=None, options=None, bands=1, scale=None,
              offset=None, wkt=None, array=None, gt=None):
    """A small GeoTIFF covering *bounds* (west/east/south/north, in the CRS
    of *epsg*; ``epsg=None`` writes no CRS at all)."""
    import numpy as np
    gdal, osr = GDAL, OSR
    dtype = gdal.GDT_Float32 if dtype is None else dtype
    w, h = size
    ds = gdal.GetDriverByName("GTiff").Create(path, w, h, bands, dtype,
                                              options or [])
    if gt is None:
        gt = (bounds["west"], (bounds["east"] - bounds["west"]) / w, 0.0,
              bounds["north"], 0.0, -(bounds["north"] - bounds["south"]) / h)
    ds.SetGeoTransform(gt)
    if wkt is not None:
        ds.SetProjection(wkt)
    elif epsg is not None:
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(epsg)
        ds.SetProjection(srs.ExportToWkt())
    for b in range(1, bands + 1):
        band = ds.GetRasterBand(b)
        data = array if array is not None else np.full((h, w), value)
        band.WriteArray(data)
        if nodata is not None:
            band.SetNoDataValue(nodata)
        if scale is not None:
            band.SetScale(scale)
        if offset is not None:
            band.SetOffset(offset)
    ds.FlushCache()
    ds = None
    return path
