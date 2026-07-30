"""GDAL/numpy post-processing for MIN_ALT results.

Three operations, all consuming the 16-bit minimum-LOS-altitude rasters
produced by the ``MIN_ALT`` model (``value * 0.5 m`` AGL, ``65535`` = no data):

* :func:`merge_best_site` — combine several sites into a per-pixel *best*
  (lowest) required altitude plus a *which-site-serves-it* map.
* :func:`generate_contours` — iso-altitude ("reach") lines at a metre interval.
* :func:`build_amsl_raster` — re-reference the surface from above-ground to
  above-sea-level by adding the terrain it was computed over.

This module is GUI-free and imports GDAL/numpy lazily (both are bundled with
QGIS; never add them as pip deps — see CLAUDE.md).  QgsRasterRenderer /
QgsVectorLayer construction for the outputs lives in ``result_loader``.
"""

from __future__ import annotations

import math
import os
from typing import Callable, List, NamedTuple, Optional, Sequence, Tuple

from .abt import MIN_VALID_ELEV_M
from .min_alt import MIN_ALT_MAX_RAW, MIN_ALT_SENTINEL, MIN_ALT_STEP_M

#: Byte value used in the best-site map for "no site reaches this pixel".
BEST_SITE_NODATA: int = 255

#: Largest union grid :func:`merge_best_site` will attempt, in pixels.  The two
#: accumulators cost 3 bytes/pixel, so this caps them at ~1.2 GB; beyond that a
#: merge is far more likely to be a mistake (sites far apart, or mismatched
#: resolutions) than a genuine request, and silently OOM-killing QGIS is a much
#: worse answer than an explanation.
MAX_MERGE_PIXELS: int = 400_000_000

#: Bytes of accumulator per union pixel (uint16 altitude + uint8 site index).
_MERGE_BYTES_PER_PIXEL: int = 3

#: Target pixels per read when streaming a warped raster in horizontal strips.
_STRIP_PIXELS: int = 4_000_000


class MergeCanceled(Exception):
    """Raised by :func:`merge_best_site` when its ``should_cancel`` hook returns
    True — lets the GUI stop a long multi-site merge between inputs."""


def fold_best_site(best_alt, best_site, arr, idx):
    """Fold one aligned MIN_ALT array into the running best-altitude/best-site
    accumulators.  Pure numpy — no GDAL — so the reduction is unit-testable.

    A pixel is updated only when *arr* is strictly lower than the current best;
    the sentinel (65535) can never win, so uncovered pixels keep their state and
    ties go to the earlier site.

    *best_alt* and *best_site* are updated **in place** (and returned for
    convenience).  The obvious ``np.where(...).astype(...)`` spelling allocates
    four full-size temporaries per input — on a wide multi-site union that alone
    was enough to run QGIS out of memory mid-merge.  ``np.copyto`` under a mask
    keeps the peak at one bool array.
    """
    import numpy as np

    arr = arr.astype(np.uint16, copy=False)
    better = arr < best_alt
    np.copyto(best_alt, arr, where=better)
    np.copyto(best_site, np.uint8(idx), where=better)
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


def _check_merge_size(width: int, height: int, n_inputs: int) -> None:
    """Refuse a union grid too large to reduce in memory, with a diagnosis.

    The union is taken at the *finest* input resolution, so one high-resolution
    layer — or two sites far enough apart that the box between them dwarfs the
    coverage itself — inflates the grid quadratically while adding no data.
    """
    if width * height <= MAX_MERGE_PIXELS:
        return
    gb = width * height * _MERGE_BYTES_PER_PIXEL / 1e9
    raise ValueError(
        f"Merging these {n_inputs} layers spans a {width} x {height} pixel grid "
        f"(~{gb:.1f} GB of memory), which is more than this merge can hold. "
        "That usually means the sites are far enough apart that the box around "
        "them is mostly empty, or one layer is at a much finer resolution than "
        "the rest — the union is taken at the finest one. Re-export them over a "
        "common area and resolution, or merge fewer sites at a time."
    )


def merge_best_site(
    inputs: Sequence[Tuple[str, str]],
    out_alt: str,
    out_site: str,
    should_cancel: Optional[Callable[[], bool]] = None,
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
    should_cancel:
        Optional zero-arg predicate polled once per input; when it returns True
        the merge raises :class:`MergeCanceled` before writing any output (so a
        GUI can stop a long merge without leaving partial files behind).

    Returns
    -------
    list[str]
        Site labels in index order (index *i* in *out_site* → ``labels[i]``).

    Notes
    -----
    All inputs are warped (nearest-neighbour, to preserve the sentinel and the
    exact 0.5 m quantisation) onto a shared union grid in the first input's CRS,
    then reduced pixel-by-pixel.  "Best" is the *lowest* required altitude; ties
    go to the earlier site.

    Only the two output accumulators are ever held whole: each input is warped
    to a *virtual* (VRT) dataset that resamples on demand and is read back in
    horizontal strips, so no full-size copy of an input exists at any point.
    """
    from osgeo import gdal
    import numpy as np

    if len(inputs) < 2:
        raise ValueError("Best-site merge needs at least two MIN_ALT layers.")
    if should_cancel is not None and should_cancel():
        raise MergeCanceled()

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

    # A warped VRT resamples lazily on read, so this costs nothing until the
    # strips below pull from it — unlike format="MEM", which materialises the
    # whole union grid for every input.
    warp_opts = gdal.WarpOptions(
        format="VRT",
        outputBounds=(min_x, min_y, max_x, max_y),
        xRes=res, yRes=res,
        dstSRS=proj,
        srcNodata=MIN_ALT_SENTINEL,
        dstNodata=MIN_ALT_SENTINEL,
        outputType=gdal.GDT_UInt16,
        resampleAlg="near",
    )

    best_alt = None
    best_site = None
    width = height = strip = 0
    geo_transform = None

    # ---- 2. Stream each input through the reducer ----
    for idx, (path, _label) in enumerate(inputs):
        if should_cancel is not None and should_cancel():
            raise MergeCanceled()
        vrt = gdal.Warp("", path, options=warp_opts)
        if vrt is None:
            raise RuntimeError(f"Failed to align raster: {path}")

        if best_alt is None:
            # Take the grid from the warper rather than deriving it from the
            # bounds: every input shares these warp options, so the first VRT
            # *is* the target grid, and re-deriving it risks a half-pixel
            # disagreement that would read off the end of the band.
            width, height = vrt.RasterXSize, vrt.RasterYSize
            geo_transform = vrt.GetGeoTransform()
            _check_merge_size(width, height, len(inputs))
            best_alt = np.full((height, width), MIN_ALT_SENTINEL, dtype=np.uint16)
            best_site = np.full((height, width), BEST_SITE_NODATA, dtype=np.uint8)
            strip = max(1, min(height, _STRIP_PIXELS // width))

        band = vrt.GetRasterBand(1)
        for y in range(0, height, strip):
            # Cancel checks per strip, not just per input, so a merge over a
            # handful of very large rasters still stops promptly.
            if should_cancel is not None and should_cancel():
                raise MergeCanceled()
            rows = min(strip, height - y)
            arr = band.ReadAsArray(0, y, width, rows)
            # Slices are views: folding into them writes through to the
            # accumulators without copying a strip back.
            fold_best_site(best_alt[y:y + rows], best_site[y:y + rows], arr, idx)
        band = None
        vrt = None

    # ---- 3. Write outputs ----
    _write_raster(out_alt, best_alt, geo_transform, proj,
                  gdal.GDT_UInt16, MIN_ALT_SENTINEL, scale=MIN_ALT_STEP_M)
    _write_raster(out_site, best_site, geo_transform, proj,
                  gdal.GDT_Byte, BEST_SITE_NODATA, scale=None)

    return labels


def _create_raster(path, width, height, geo_transform, projection, gdal_type,
                   nodata, scale):
    """Create a single-band GeoTIFF and return the open dataset.

    The caller writes the band and drops its reference to close the file.
    ``scale`` re-declares the GDAL SCALE/OFFSET tags that carry the MIN_ALT
    quantisation (0.5 m per count) — without them the file still holds the
    right numbers, but every reader outside this plugin shows raw counts.
    """
    from osgeo import gdal
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(path, width, height, 1, gdal_type,
                       options=["COMPRESS=DEFLATE", "TILED=YES"])
    ds.SetGeoTransform(geo_transform)
    ds.SetProjection(projection)
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(float(nodata))
    if scale is not None:
        band.SetScale(scale)
        band.SetOffset(0.0)
    return ds


def _write_raster(path, array, geo_transform, projection, gdal_type,
                  nodata, scale) -> None:
    h, w = array.shape
    ds = _create_raster(path, w, h, geo_transform, projection, gdal_type,
                        nodata, scale)
    band = ds.GetRasterBand(1)
    band.WriteArray(array)
    band.FlushCache()
    ds = None


# ---------------------------------------------------------------------------
# Above-sea-level re-referencing
# ---------------------------------------------------------------------------

class AmslCanceled(Exception):
    """Raised by :func:`build_amsl_raster` when its ``should_cancel`` hook goes
    True — the partial output is removed before it propagates."""


#: GeoTIFF metadata key naming the elevation source a twin was built from.
#: Without it the only thing distinguishing two twins of the same layer is
#: their timestamp, and a twin built from the wrong DEM would be re-used
#: forever after the user corrected the choice.
AMSL_TERRAIN_KEY = "WAVESHED_AMSL_TERRAIN"


def amsl_terrain_of(path: str) -> Optional[str]:
    """The elevation source recorded in an AMSL twin, or None if unreadable.

    None is also what a file written before this key existed returns, so the
    caller treats it as a miss and rebuilds — which is the safe direction.
    """
    from osgeo import gdal

    try:
        gdal.UseExceptions()
        ds = gdal.Open(path)
        if ds is None:
            return None
        value = ds.GetMetadataItem(AMSL_TERRAIN_KEY)
        ds = None
        return value or None
    except Exception:  # noqa: BLE001 — a miss, not a failure
        return None


class AmslResult(NamedTuple):
    """What :func:`build_amsl_raster` produced."""

    path: str
    #: Lowest and highest AMSL altitude written, in metres (0.0/0.0 when the
    #: source has no reachable pixel at all).
    min_m: float
    max_m: float
    #: Pixels that are reachable in the source but had no terrain under them,
    #: and are therefore blank in the output.
    missing_terrain_px: int
    #: Reachable pixels in the source, for scale on the number above.
    reachable_px: int

    @property
    def missing_fraction(self) -> float:
        """0.0–1.0 share of reachable pixels lost to missing terrain."""
        if self.reachable_px <= 0:
            return 0.0
        return self.missing_terrain_px / float(self.reachable_px)


def to_amsl_raw(agl_raw, terrain_m, nodata_raw: int = MIN_ALT_SENTINEL):
    """Add terrain height to a MIN_ALT tile, keeping the same encoding.

    A MIN_ALT pixel answers "how far above the ground here must I be?".  A
    pilot holds a *sea-level* altitude instead, and is in line-of-sight exactly
    where ``terrain + required_agl <= flight altitude`` — so adding the ground
    under each pixel turns the whole raster into that comparison, with no
    change to what the renderer or the slider do with it.

    Pure numpy so the arithmetic — the clamping and the two ways a pixel can
    end up blank — is unit-testable without GDAL.

    Parameters
    ----------
    agl_raw:
        Source tile in raw u16 counts (0.5 m each).
    terrain_m:
        Ground elevation in metres, on the *same grid*, already resampled.
    nodata_raw:
        Source value meaning "never visible"; those pixels stay blank.

    Notes
    -----
    Ground below sea level is clamped to 0 m: the u16 encoding cannot express a
    negative altitude, and the affected places (the Dead Sea, the Caspian
    depression) are the only ones where the two differ.
    """
    import numpy as np

    agl = np.asarray(agl_raw)
    terrain = np.asarray(terrain_m, dtype=np.float32)

    # NaN is neutralised before the cast rather than after: it is masked out
    # below either way, but casting it to an integer warns on every strip.
    ground_m = np.nan_to_num(terrain, nan=0.0, posinf=0.0, neginf=0.0)
    ground_counts = np.rint(
        np.clip(ground_m, 0.0, MIN_ALT_MAX_RAW * MIN_ALT_STEP_M) / MIN_ALT_STEP_M
    )
    total = agl.astype(np.int32) + ground_counts.astype(np.int32)
    np.clip(total, 0, MIN_ALT_MAX_RAW, out=total)
    out = total.astype(np.uint16)

    # Two ways to end up blank: the source never sees the transmitter, or we do
    # not know the ground here — a comparison against an invented sea-level
    # altitude is worse than an honest hole. (`> MIN_VALID_ELEV_M` also rejects
    # NaN, which is how a warped float DEM spells its own no-data.)
    out[(agl >= nodata_raw) | ~(terrain > MIN_VALID_ELEV_M)] = MIN_ALT_SENTINEL
    return out


def build_amsl_raster(
    min_alt_path: str,
    terrain_path: str,
    out_path: str,
    should_cancel: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    terrain_tag: str = "",
) -> AmslResult:
    """Write the above-sea-level twin of a MIN_ALT raster.

    *terrain_path* is any GDAL-readable elevation raster in metres (the run's
    own ``.abt`` cache, mosaicked by :mod:`.abt`, or a DEM the user picked); it
    is resampled onto the MIN_ALT grid here, so it need not match in extent,
    resolution, or CRS.  The output keeps the MIN_ALT encoding exactly — same
    0.5 m counts, same sentinel — so the Altitude Explorer, the contour tool
    and the best-site merge all drive it unchanged.

    Parameters
    ----------
    should_cancel:
        Polled once per strip; when it goes True the partial file is removed
        and :class:`AmslCanceled` is raised.
    on_progress:
        Called with a 0.0–1.0 fraction as strips complete.  Must not raise.
    terrain_tag:
        Recorded in the output under :data:`AMSL_TERRAIN_KEY` so a cached twin
        can be told apart from one built over different elevation data.
    """
    from osgeo import gdal
    import numpy as np

    gdal.UseExceptions()
    src = _open(min_alt_path)
    width, height = src.RasterXSize, src.RasterYSize
    geo_transform = src.GetGeoTransform()
    projection = src.GetProjection()
    src_band = src.GetRasterBand(1)
    nodata = src_band.GetNoDataValue()
    nodata_raw = int(nodata) if nodata is not None else MIN_ALT_SENTINEL

    min_x, min_y, max_x, max_y, _res = _bounds(src)

    # Pinned by width/height rather than by resolution: this VRT has to land on
    # the source's grid *exactly*, and asking for a pixel size instead lets a
    # rounding difference add a row that then reads off the end of the tile.
    warp_kwargs = dict(
        format="VRT",
        outputBounds=(min_x, min_y, max_x, max_y),
        width=width, height=height,
        outputType=gdal.GDT_Float32,
        # Bilinear: elevation is a continuous surface, and nearest-neighbour
        # would stair-step the sea-level altitudes at the source DEM's grain.
        resampleAlg="bilinear",
        dstNodata=MIN_VALID_ELEV_M - 1.0,
    )
    if projection:
        # Only when the source declares one: handing gdal.Warp an empty dstSRS
        # is an error, not a "leave it alone".
        warp_kwargs["dstSRS"] = projection
    terrain_vrt = gdal.Warp("", terrain_path,
                            options=gdal.WarpOptions(**warp_kwargs))
    if terrain_vrt is None:
        raise RuntimeError(f"Could not align the terrain raster: {terrain_path}")
    if (terrain_vrt.RasterXSize, terrain_vrt.RasterYSize) != (width, height):
        raise RuntimeError(
            "The terrain raster could not be aligned to the coverage grid "
            f"({terrain_vrt.RasterXSize}x{terrain_vrt.RasterYSize} vs "
            f"{width}x{height})."
        )
    terrain_band = terrain_vrt.GetRasterBand(1)

    dst = _create_raster(out_path, width, height, geo_transform, projection,
                         gdal.GDT_UInt16, MIN_ALT_SENTINEL, MIN_ALT_STEP_M)
    dst.SetMetadataItem(AMSL_TERRAIN_KEY, terrain_tag or terrain_path)
    dst_band = dst.GetRasterBand(1)

    strip = max(1, min(height, _STRIP_PIXELS // max(1, width)))
    lowest = MIN_ALT_SENTINEL
    highest = 0
    missing = 0
    reachable = 0
    canceled = False
    complete = False

    try:
        for y in range(0, height, strip):
            if should_cancel is not None and should_cancel():
                canceled = True
                break
            rows = min(strip, height - y)
            agl = src_band.ReadAsArray(0, y, width, rows)
            terrain = terrain_band.ReadAsArray(0, y, width, rows)
            amsl = to_amsl_raw(agl, terrain, nodata_raw)
            dst_band.WriteArray(amsl, 0, y)

            live = agl < nodata_raw
            reachable += int(np.count_nonzero(live))
            # Reachable in the source but blank here — terrain we did not have.
            missing += int(np.count_nonzero(live & (amsl == MIN_ALT_SENTINEL)))
            written = amsl[amsl < MIN_ALT_SENTINEL]
            if written.size:
                lowest = min(lowest, int(written.min()))
                highest = max(highest, int(written.max()))
            if on_progress is not None:
                on_progress(min(1.0, (y + rows) / float(height)))
        complete = not canceled
    finally:
        # The band proxies dangle once their datasets close, so they go first.
        src_band = terrain_band = dst_band = None
        src = terrain_vrt = dst = None
        # A half-written twin must not survive: the rows never reached are
        # zeros, which read as "reachable at sea level" — and the caller's
        # cache would happily serve that file forever, since it is newer than
        # the source it was built from.
        if not complete:
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
            except OSError:
                pass

    if canceled:
        raise AmslCanceled()

    if lowest > highest:  # nothing reachable anywhere
        lowest = highest = 0
    return AmslResult(
        out_path, lowest * MIN_ALT_STEP_M, highest * MIN_ALT_STEP_M,
        missing, reachable,
    )


class ContourCanceled(Exception):
    """Raised by :func:`generate_contours` when its ``should_cancel`` hook goes
    True — contouring is abortable mid-raster, not only between rasters."""


class ContourResult(NamedTuple):
    """What :func:`generate_contours` produced, for the GUI to style and report."""

    #: The GeoPackage written (``<gpkg>|layername=contours`` to open it).
    path: str
    #: Contour levels actually drawn, in metres AGL, ascending.
    levels_m: List[float]
    #: Number of line features written.
    feature_count: int
    #: Ground resolution the contours were traced at, in raster CRS units.
    resolution: float
    #: True when the raster was resampled down to meet ``max_pixels``.
    decimated: bool


#: Pixel budget for the contouring grid.  A MIN_ALT surface is jagged at pixel
#: scale — every terrain-shadow edge is a step — so tracing it at native
#: resolution yields millions of tiny, unreadable segments and takes minutes.
#: ~2000x2000 is well beyond what contour lines can express on a map, and
#: averaging down to it both smooths the noise and cuts the work quadratically.
MAX_CONTOUR_PIXELS: int = 4_000_000

#: More levels than this cannot be drawn legibly or quickly; asking for them is
#: a mis-set interval, so it is reported rather than attempted.
MAX_CONTOUR_LEVELS: int = 200


def _contour_grid(src, nodata: float, max_pixels: int):
    """Return ``(dataset, decimated, resolution)`` — *src* resampled to at most
    *max_pixels* pixels, with the no-data sentinel guaranteed to be tagged.

    Everything goes through the warper even when no decimation is needed: it is
    also what guarantees ``dstNodata`` is set, which the level scan below relies
    on to ignore the 65535 "never visible" sentinel.
    """
    from osgeo import gdal

    w, h = src.RasterXSize, src.RasterYSize
    _, _, _, _, src_res = _bounds(src)

    out_w, out_h = w, h
    if w * h > max_pixels:
        scale = math.sqrt(max_pixels / float(w * h))
        out_w = max(2, int(w * scale))
        out_h = max(2, int(h * scale))

    ds = gdal.Warp("", src, options=gdal.WarpOptions(
        format="MEM",
        width=out_w, height=out_h,
        srcNodata=nodata, dstNodata=nodata,
        # 'average' (not 'near') is the point: it low-pass filters the pixel
        # jitter that would otherwise survive decimation as line noise.
        resampleAlg="average",
    ))
    if ds is None:
        raise RuntimeError("Could not resample the raster for contouring.")
    return ds, (out_w, out_h) != (w, h), src_res * (w / float(out_w))


def _nice_interval(value_m: float) -> float:
    """Round *value_m* up to a tidy contour interval.

    Uses the same 1 / 2.5 / 5 ladder as :func:`min_alt.nice_ceiling`, so the
    interval suggested here lands on the numbers the altitude slider already
    snaps to (…10, 25, 50, 100, 250, 500…).
    """
    if value_m <= 0:
        return 1.0
    base = 10.0 ** math.floor(math.log10(value_m))
    for mult in (1.0, 2.5, 5.0):
        if value_m <= mult * base:
            return mult * base
    return 10.0 * base


def _contour_levels(band, interval_m: float, base_m: float):
    """Return ``(levels_raw, levels_m)`` — the levels that fall strictly inside
    the band's data range, in raw counts and in metres.

    Deriving these up front (rather than letting GDAL infer them) is what lets
    an unusable request be reported instead of run, and hands the caller the
    exact legend breaks so styling needs no pass over the output features.
    """
    try:
        lo_raw, hi_raw = band.ComputeRasterMinMax(False)
    except Exception:
        return [], []  # all-sentinel raster: GDAL finds no valid pixels

    lo_m = max(0.0, float(lo_raw) * MIN_ALT_STEP_M)
    hi_m = float(hi_raw) * MIN_ALT_STEP_M
    if hi_m <= lo_m:
        return [], []

    first = int(math.floor((lo_m - base_m) / interval_m)) + 1
    last = int(math.ceil((hi_m - base_m) / interval_m)) - 1
    count = last - first + 1
    if count > MAX_CONTOUR_LEVELS:
        suggested = _nice_interval((hi_m - lo_m) / MAX_CONTOUR_LEVELS)
        raise ValueError(
            f"A {interval_m:g} m interval needs {count} contour levels across "
            f"this layer's {lo_m:.0f}–{hi_m:.0f} m range — more than can be "
            f"drawn or read. Use an interval of at least {suggested:g} m."
        )
    if count <= 0:
        return [], []

    levels_m = [base_m + k * interval_m for k in range(first, last + 1)]
    return [m / MIN_ALT_STEP_M for m in levels_m], levels_m


def generate_contours(
    in_path: str,
    out_path: str,
    interval_m: float = 25.0,
    base_m: float = 0.0,
    max_pixels: int = MAX_CONTOUR_PIXELS,
    should_cancel: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
) -> ContourResult:
    """Generate iso-altitude contour lines from a MIN_ALT raster.

    Lines are drawn every *interval_m* metres of required altitude.  The output
    GeoPackage layer carries an ``alt_m`` (Real) attribute for styling/labels.

    Parameters
    ----------
    max_pixels:
        Pixel budget for the traced grid (see :data:`MAX_CONTOUR_PIXELS`);
        larger rasters are averaged down to fit.
    should_cancel:
        Polled during tracing; when it goes True the run stops and raises
        :class:`ContourCanceled`, leaving no output file behind.
    on_progress:
        Called with a 0.0–1.0 fraction as tracing proceeds (once per whole
        percent).  Must not raise.
    """
    from osgeo import gdal, ogr, osr

    gdal.UseExceptions()
    if interval_m <= 0:
        raise ValueError("Contour interval must be positive.")

    src = _open(in_path)
    nodata = src.GetRasterBand(1).GetNoDataValue()
    if nodata is None:
        nodata = float(MIN_ALT_SENTINEL)

    work, decimated, resolution = _contour_grid(src, nodata, max_pixels)
    src = None
    band = work.GetRasterBand(1)

    levels_raw, levels_m = _contour_levels(band, interval_m, base_m)
    if not levels_raw:
        raise ValueError(
            "Nothing to contour — this layer has no reachable pixels, or its "
            f"whole altitude range fits inside one {interval_m:g} m step."
        )

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    if os.path.exists(out_path):
        os.remove(out_path)

    drv = ogr.GetDriverByName("GPKG")
    dst = drv.CreateDataSource(out_path)
    srs = osr.SpatialReference()
    wkt = work.GetProjection()
    if wkt:
        srs.ImportFromWkt(wkt)
    # The default SPATIAL_INDEX=YES is already right for a bulk load: for a
    # layer it just created, the GPKG driver defers the R-tree — no per-INSERT
    # triggers exist during the load — and builds it at flush on a background
    # thread. Turning it off to build the index by hand only loses the thread.
    layer = dst.CreateLayer("contours", srs, ogr.wkbLineString25D)
    layer.CreateField(ogr.FieldDefn("id", ogr.OFTInteger))
    # elev_raw is the contour's raw band value, and the 25D geometry's Z carries
    # the same number — alt_m below is the metres column to style and label on.
    layer.CreateField(ogr.FieldDefn("elev_raw", ogr.OFTReal))
    # Declared up front so the metres column costs no ALTER TABLE later.
    layer.CreateField(ogr.FieldDefn("alt_m", ogr.OFTReal))

    canceled = False

    def _tick(complete, _message, _data):
        # Runs inside GDAL's C call, once per whole percent (the binding does
        # that throttling itself). Returning 0 aborts the trace — and so does
        # raising, because the binding turns an exception into a printed
        # traceback plus an abort. Hence the blanket catch: a hiccup emitting
        # progress must not silently truncate the contours.
        nonlocal canceled
        try:
            if should_cancel is not None and should_cancel():
                canceled = True
                return 0
            if on_progress is not None:
                on_progress(float(complete))
        except Exception:  # noqa: BLE001 — an abort would be far worse
            pass
        return 1

    # Contours run in RAW pixel units (0.5 m/count), so the levels go out
    # unconverted. FIXED_LEVELS *alone*, deliberately: since GDAL 3.10 a
    # LEVEL_INTERVAL passed alongside is unioned with it rather than overridden
    # (older GDAL gives FIXED_LEVELS precedence), and the interval iterator
    # includes the data endpoints this function deliberately excludes. Those
    # extra contours would sit outside every legend class — drawn by nothing.
    options = [
        "FIXED_LEVELS=" + ",".join(f"{lv:.6f}" for lv in levels_raw),
        f"NODATA={float(nodata)}",
        "ID_FIELD=0",
        "ELEV_FIELD=1",
    ]

    # One transaction around the whole load. Without it each CreateFeature is
    # its own SQLite commit — an fsync per line — which is what made contouring
    # a large coverage raster look like QGIS had hung.
    dst.StartTransaction()
    failure = None
    try:
        # ContourGenerateEx (GDAL >= 2.4; QGIS bundles 3.x) — keyword options
        # are stable across versions, unlike the positional ContourGenerate
        # binding. ELEV_FIELD=1 -> 'elev_raw' gets the raw contour value.
        #
        # The return code is load-bearing: aborting from the progress callback
        # makes GDAL return CE_Failure *without* setting a CPL error, so
        # UseExceptions() raises nothing. Discard it and a cancelled run commits
        # a half-traced layer and reports success.
        if gdal.ContourGenerateEx(
            band, layer, options=options, callback=_tick,
        ) != gdal.CE_None:
            failure = RuntimeError(
                f"GDAL could not trace contours for {os.path.basename(in_path)}"
            )
    except Exception as exc:  # noqa: BLE001 — re-raised below, after cleanup
        failure = exc

    if failure is not None:
        # Nothing in this cleanup may mask the failure that got us here — the
        # message is all the user will see.
        try:
            dst.RollbackTransaction()
        except Exception:  # noqa: BLE001
            pass
        # The layer proxy dangles once its datasource closes, so it goes first.
        layer = band = work = dst = None
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except OSError:
            pass
        if canceled:
            raise ContourCanceled() from None
        raise failure
    dst.CommitTransaction()

    feature_count = layer.GetFeatureCount()

    # Metres attribute as ONE set-based UPDATE rather than a per-feature
    # SetFeature() loop, for the same reason the load is wrapped above.
    result = dst.ExecuteSQL(
        f"UPDATE contours SET alt_m = COALESCE(elev_raw, 0) * {MIN_ALT_STEP_M}"
    )
    if result is not None:  # UPDATE yields no result set; release if one appears
        dst.ReleaseResultSet(result)

    # Close the GeoPackage before returning — the caller opens this file in
    # QGIS the moment we do. Layer proxy first, as in the failure path.
    layer = band = work = dst = None
    return ContourResult(
        out_path, levels_m, feature_count, resolution, decimated,
    )
