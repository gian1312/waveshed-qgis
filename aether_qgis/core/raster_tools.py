"""GDAL/numpy post-processing for MIN_ALT results.

Two operations, both consuming the 16-bit minimum-LOS-altitude rasters produced
by the ``MIN_ALT`` model (``value * 0.5 m`` AGL, ``65535`` = no data):

* :func:`merge_best_site` — combine several sites into a per-pixel *best*
  (lowest) required altitude plus a *which-site-serves-it* map.
* :func:`generate_contours` — iso-altitude ("reach") lines at a metre interval.

This module is GUI-free and imports GDAL/numpy lazily (both are bundled with
QGIS; never add them as pip deps — see CLAUDE.md).  QgsRasterRenderer /
QgsVectorLayer construction for the outputs lives in ``result_loader``.
"""

from __future__ import annotations

import os
from typing import List, Optional, Sequence, Tuple

from .min_alt import MIN_ALT_SENTINEL, MIN_ALT_STEP_M

#: Byte value used in the best-site map for "no site reaches this pixel".
BEST_SITE_NODATA: int = 255


def fold_best_site(best_alt, best_site, arr, idx):
    """Fold one aligned MIN_ALT array into the running best-altitude/best-site
    accumulators.  Pure numpy — no GDAL — so the reduction is unit-testable.

    A pixel is updated only when *arr* is strictly lower than the current best;
    the sentinel (65535) can never win, so uncovered pixels keep their state and
    ties go to the earlier site.
    """
    import numpy as np

    arr = arr.astype(np.uint16, copy=False)
    better = arr < best_alt
    best_alt = np.where(better, arr, best_alt).astype(np.uint16)
    best_site = np.where(better, np.uint8(idx), best_site).astype(np.uint8)
    return best_alt, best_site


def reduce_best_site(arrays):
    """Reduce a list of aligned MIN_ALT arrays to ``(best_alt, best_site)``.

    Convenience wrapper over :func:`fold_best_site` (used by tests; the GDAL path
    folds incrementally to keep memory low)."""
    import numpy as np

    if not arrays:
        raise ValueError("reduce_best_site needs at least one array.")
    shape = arrays[0].shape
    best_alt = np.full(shape, MIN_ALT_SENTINEL, dtype=np.uint16)
    best_site = np.full(shape, BEST_SITE_NODATA, dtype=np.uint8)
    for idx, arr in enumerate(arrays):
        best_alt, best_site = fold_best_site(best_alt, best_site, arr, idx)
    return best_alt, best_site


def _open(path: str):
    from osgeo import gdal
    gdal.UseExceptions()
    ds = gdal.Open(path)
    if ds is None:
        raise RuntimeError(f"Could not open raster: {path}")
    return ds


def _bounds(ds) -> Tuple[float, float, float, float, float]:
    """Return ``(min_x, min_y, max_x, max_y, res)`` for *ds* (res = finest of
    the two pixel dimensions, always positive)."""
    gt = ds.GetGeoTransform()
    x0, px, _, y0, _, py = gt
    w, h = ds.RasterXSize, ds.RasterYSize
    min_x = x0
    max_x = x0 + w * px
    max_y = y0
    min_y = y0 + h * py
    # py is normally negative (north-up); guard either orientation.
    if min_y > max_y:
        min_y, max_y = max_y, min_y
    res = min(abs(px), abs(py))
    return min_x, min_y, max_x, max_y, res


def merge_best_site(
    inputs: Sequence[Tuple[str, str]],
    out_alt: str,
    out_site: str,
) -> List[str]:
    """Merge several MIN_ALT rasters into a best-altitude + best-site pair.

    Parameters
    ----------
    inputs:
        ``[(raster_path, site_label), ...]`` — two or more MIN_ALT GeoTIFFs.
    out_alt:
        Path for the output UInt16 *best required altitude* raster (same
        encoding as the inputs, so the Altitude Explorer can drive it).
    out_site:
        Path for the output Byte *best site index* raster (value = index into
        the returned label list; ``255`` = uncovered).

    Returns
    -------
    list[str]
        Site labels in index order (index *i* in *out_site* → ``labels[i]``).

    Notes
    -----
    All inputs are warped (nearest-neighbour, to preserve the sentinel and the
    exact 0.5 m quantisation) onto a shared union grid in the first input's CRS,
    then reduced pixel-by-pixel.  "Best" is the *lowest* required altitude; ties
    go to the earlier site.  Memory stays at ~3 arrays by streaming one warped
    raster at a time rather than stacking all N.
    """
    from osgeo import gdal
    import numpy as np

    if len(inputs) < 2:
        raise ValueError("Best-site merge needs at least two MIN_ALT layers.")

    gdal.UseExceptions()
    labels = [label for _, label in inputs]

    # ---- 1. Target grid: union extent, finest resolution, first CRS ----
    src_datasets = [_open(path) for path, _ in inputs]
    proj = src_datasets[0].GetProjection()
    # Union bounds are combined in the first input's CRS, so all inputs must
    # share it (they do for sites exported over the same region). Refuse a
    # silent misalignment otherwise.
    for (path, _lbl), ds in zip(inputs, src_datasets):
        if ds.GetProjection() != proj:
            raise RuntimeError(
                "All layers must share one CRS to merge. "
                f"'{os.path.basename(path)}' differs from the first layer — "
                "reproject them to a common CRS first."
            )
    min_x = min_y = None
    max_x = max_y = None
    res = None
    for ds in src_datasets:
        b_min_x, b_min_y, b_max_x, b_max_y, b_res = _bounds(ds)
        min_x = b_min_x if min_x is None else min(min_x, b_min_x)
        min_y = b_min_y if min_y is None else min(min_y, b_min_y)
        max_x = b_max_x if max_x is None else max(max_x, b_max_x)
        max_y = b_max_y if max_y is None else max(max_y, b_max_y)
        res = b_res if res is None else min(res, b_res)
    src_datasets = None  # release; re-opened per-warp below

    warp_opts = gdal.WarpOptions(
        format="MEM",
        outputBounds=(min_x, min_y, max_x, max_y),
        xRes=res, yRes=res,
        dstSRS=proj,
        srcNodata=MIN_ALT_SENTINEL,
        dstNodata=MIN_ALT_SENTINEL,
        outputType=gdal.GDT_UInt16,
        resampleAlg="near",
    )

    best_alt: Optional["np.ndarray"] = None
    best_site: Optional["np.ndarray"] = None

    # ---- 2. Stream each input through the reducer ----
    for idx, (path, _label) in enumerate(inputs):
        mem = gdal.Warp("", path, options=warp_opts)
        if mem is None:
            raise RuntimeError(f"Failed to align raster: {path}")
        arr = mem.GetRasterBand(1).ReadAsArray().astype(np.uint16)
        mem = None

        if best_alt is None:
            best_alt = np.full(arr.shape, MIN_ALT_SENTINEL, dtype=np.uint16)
            best_site = np.full(arr.shape, BEST_SITE_NODATA, dtype=np.uint8)

        best_alt, best_site = fold_best_site(best_alt, best_site, arr, idx)

    # ---- 3. Write outputs ----
    geo_transform = (min_x, res, 0.0, max_y, 0.0, -res)
    _write_raster(out_alt, best_alt, geo_transform, proj,
                  gdal.GDT_UInt16, MIN_ALT_SENTINEL, scale=MIN_ALT_STEP_M)
    _write_raster(out_site, best_site, geo_transform, proj,
                  gdal.GDT_Byte, BEST_SITE_NODATA, scale=None)

    return labels


def _write_raster(path, array, geo_transform, projection, gdal_type,
                  nodata, scale) -> None:
    from osgeo import gdal
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    h, w = array.shape
    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(path, w, h, 1, gdal_type,
                       options=["COMPRESS=DEFLATE", "TILED=YES"])
    ds.SetGeoTransform(geo_transform)
    ds.SetProjection(projection)
    band = ds.GetRasterBand(1)
    band.WriteArray(array)
    band.SetNoDataValue(float(nodata))
    if scale is not None:
        band.SetScale(scale)
        band.SetOffset(0.0)
    band.FlushCache()
    ds = None


def generate_contours(
    in_path: str,
    out_path: str,
    interval_m: float = 25.0,
    base_m: float = 0.0,
) -> str:
    """Generate iso-altitude contour lines from a MIN_ALT raster.

    Lines are drawn every *interval_m* metres of required altitude.  The output
    GeoPackage layer carries an ``alt_m`` (Real) attribute for styling/labels.

    Returns the ``out_path`` (``<gpkg>|layername=contours`` form is *not*
    used — the caller opens the file directly).
    """
    from osgeo import gdal, ogr, osr

    gdal.UseExceptions()
    if interval_m <= 0:
        raise ValueError("Contour interval must be positive.")

    src = _open(in_path)
    band = src.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    if nodata is None:
        nodata = float(MIN_ALT_SENTINEL)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    if os.path.exists(out_path):
        os.remove(out_path)

    drv = ogr.GetDriverByName("GPKG")
    dst = drv.CreateDataSource(out_path)
    srs = osr.SpatialReference()
    wkt = src.GetProjection()
    if wkt:
        srs.ImportFromWkt(wkt)
    layer = dst.CreateLayer("contours", srs, ogr.wkbLineString25D)
    layer.CreateField(ogr.FieldDefn("id", ogr.OFTInteger))
    layer.CreateField(ogr.FieldDefn("elev_raw", ogr.OFTReal))

    # Contours run in RAW pixel units (0.5 m/count); convert the metre interval.
    interval_raw = interval_m / MIN_ALT_STEP_M
    base_raw = base_m / MIN_ALT_STEP_M

    # ContourGenerateEx (GDAL >= 2.4; QGIS bundles 3.x) — keyword options are
    # stable across versions, unlike the positional ContourGenerate binding.
    # ELEV_FIELD=1 -> 'elev_raw' gets the raw pixel value of each contour.
    gdal.ContourGenerateEx(band, layer, options=[
        f"LEVEL_INTERVAL={interval_raw}",
        f"LEVEL_BASE={base_raw}",
        f"NODATA={float(nodata)}",
        "ID_FIELD=0",
        "ELEV_FIELD=1",
    ])

    # Add a metres attribute derived from the raw contour elevation.
    layer.CreateField(ogr.FieldDefn("alt_m", ogr.OFTReal))
    layer.ResetReading()
    for feat in layer:
        elev_raw = feat.GetField("elev_raw")
        feat.SetField("alt_m", (elev_raw or 0.0) * MIN_ALT_STEP_M)
        layer.SetFeature(feat)

    dst = None
    src = None
    return out_path
