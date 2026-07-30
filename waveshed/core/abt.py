"""Reader for AETHER ``.abt`` terrain tiles.

``.abt`` is the tile format ``aether_converter`` writes and ``aether_core``
reads: a 44-byte little-endian header followed by row-padded ``int16``
elevations in **half-metre counts**.

.. code-block:: text

    [0:4]   b"AETH"      magic
    [4:6]   u16          format version
    [6:8]   u16          width == height, in pixels
    [8:16]  f64          upper-left latitude
    [16:24] f64          upper-left longitude
    [24:32] f64          scale_y (degrees)
    [32:40] f64          scale_x (degrees)
    [40:42] i16          base elevation (unused here)
    [42:44] u16          row stride in bytes, 256-byte aligned

The plugin normally treats ``.abt`` as opaque — it hands a tile directory to
the engine and never looks inside.  Two features do need to read it back: the
Map Converter's "Inspect .abt" (show me the terrain I just built) and the
Altitude Explorer's above-sea-level view, which adds the terrain a run used to
that run's minimum-altitude surface.  Both go through this module so there is
one decoder rather than two.

GUI-free; numpy and GDAL are imported lazily (both ship with QGIS — never add
them as pip dependencies, see CLAUDE.md).
"""

from __future__ import annotations

import math
import os
import struct
from typing import Any, Callable, List, NamedTuple, Optional, Sequence

#: Bytes of fixed header before the first pixel row.
HEADER_SIZE = 44

#: Header magic identifying an AETHER terrain tile.
MAGIC = b"AETH"

#: Metres of elevation per stored int16 count.
ELEV_STEP_M = 0.5

#: Value written for "no tile covered this pixel" in a mosaic.
MOSAIC_NODATA_M = -9999.0

#: Elevations below this are fill/void rather than terrain (the Dead Sea shore,
#: the deepest land on Earth, is about -430 m — anything under -5000 m is a
#: converter gap, not a place).
MIN_VALID_ELEV_M = -5000.0

#: Largest mosaic edge, in pixels.  A whole terrain cache can span degrees at
#: 10 m; assembling that at native resolution would allocate tens of GB, and
#: nothing downstream (a preview layer, a coverage-sized grid) can use it.
DEFAULT_MAX_DIM = 16384


class TileHeader(NamedTuple):
    """The parts of an ``.abt`` header this package uses."""

    path: str
    #: Tile edge in pixels (tiles are square).
    size: int
    ul_lat: float
    ul_lon: float
    #: Degrees per pixel.
    pixel_res: float
    #: Row stride in bytes (>= ``size * 2``, 256-byte aligned).
    stride: int


class MosaicGrid(NamedTuple):
    """Target grid a set of tiles is assembled onto (WGS84, north-up)."""

    #: Westernmost longitude / northernmost latitude of the grid.
    west: float
    north: float
    #: Degrees per pixel.
    resolution: float
    width: int
    height: int
    #: True when the grid was coarsened to fit a pixel budget.
    decimated: bool

    @property
    def geo_transform(self) -> List[float]:
        """GDAL geotransform for this grid."""
        return [self.west, self.resolution, 0.0,
                self.north, 0.0, -self.resolution]


def list_tiles(abt_dir: str) -> List[str]:
    """Return the ``.abt`` files directly inside *abt_dir*, sorted.

    Not recursive: a terrain cache directory holds its tiles flat, and
    descending would sweep in a neighbouring run's tiles from a parent folder.
    """
    try:
        names = sorted(os.listdir(abt_dir))
    except OSError:
        return []
    return [
        os.path.join(abt_dir, name) for name in names
        if name.lower().endswith(".abt")
    ]


def read_header(path: str) -> Optional[TileHeader]:
    """Read *path*'s header, or None if it is not a readable ``.abt`` tile."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(HEADER_SIZE)
        if len(raw) < HEADER_SIZE:
            return None
        magic, _version, size, ul_lat, ul_lon, _sc_y, sc_x, _base, stride = (
            struct.unpack("<4sHHddddhH", raw)
        )
        if magic != MAGIC or size <= 0:
            return None
        # Older converter builds wrote the whole tile's span in scale_x rather
        # than the per-pixel step. A real per-pixel step is tiny (10 m is 9e-5
        # degrees), so anything at or above 0.005 degrees is a tile span.
        pixel_res = sc_x if sc_x < 0.005 else sc_x / size
        if pixel_res <= 0:
            return None
        return TileHeader(path, size, ul_lat, ul_lon, pixel_res, stride)
    except (OSError, struct.error):
        return None


def read_headers(paths: Sequence[str]) -> List[TileHeader]:
    """Read every readable header in *paths*, skipping the ones that fail."""
    headers = [read_header(path) for path in paths]
    return [h for h in headers if h is not None]


def read_tile(header: TileHeader):
    """Read one tile's elevations as an ``int16`` array (half-metre counts).

    Returns None if the body is short or unreadable — a truncated tile leaves a
    hole in the mosaic rather than failing the whole assembly.
    """
    import numpy as np

    size = header.size
    row_bytes = size * 2
    pad = max(0, header.stride - row_bytes)
    try:
        with open(header.path, "rb") as handle:
            handle.seek(HEADER_SIZE)
            grid = np.zeros((size, size), dtype=np.int16)
            for row in range(size):
                chunk = handle.read(row_bytes)
                if len(chunk) < row_bytes:
                    return None
                grid[row, :] = np.frombuffer(chunk, dtype=np.int16)
                if pad:
                    handle.seek(pad, os.SEEK_CUR)
        return grid
    except (OSError, ValueError):
        return None


def _ceil_pixels(span: float, resolution: float) -> int:
    """Pixels needed to cover *span* at *resolution*, immune to float noise.

    Tile corners and steps are degrees held as doubles, so a span that is
    exactly two pixels wide routinely divides out as 2.0000000000000018 — and a
    bare ``ceil`` turns that into a phantom row of no-data along the south and
    east edges of every mosaic.
    """
    if resolution <= 0:
        return 0
    return int(math.ceil(round(span / resolution, 6)))


def mosaic_grid(
    headers: Sequence[TileHeader], max_dim: int = DEFAULT_MAX_DIM,
) -> Optional[MosaicGrid]:
    """Grid covering every tile in *headers*, at the finest resolution present.

    Coarsened uniformly if either edge would exceed *max_dim*.  Returns None
    for an empty tile list.
    """
    if not headers:
        return None

    resolution = min(h.pixel_res for h in headers)
    north = max(h.ul_lat for h in headers)
    south = min(h.ul_lat - h.size * h.pixel_res for h in headers)
    west = min(h.ul_lon for h in headers)
    east = max(h.ul_lon + h.size * h.pixel_res for h in headers)

    width = _ceil_pixels(east - west, resolution)
    height = _ceil_pixels(north - south, resolution)
    decimated = False
    if max_dim > 0 and max(width, height) > max_dim:
        resolution *= max(width, height) / float(max_dim)
        width = _ceil_pixels(east - west, resolution)
        height = _ceil_pixels(north - south, resolution)
        decimated = True

    return MosaicGrid(west, north, resolution, max(1, width), max(1, height),
                      decimated)


def paste_tile(canvas, tile_m, row_off: int, col_off: int) -> None:
    """Paste *tile_m* (metres) into *canvas* at ``(row_off, col_off)``.

    Clipped to the canvas on every side, and void pixels are skipped so a tile
    that only partly covers its footprint does not punch holes in a neighbour
    that already filled them.  In-place; out-of-canvas tiles are a no-op.
    """
    height, width = canvas.shape
    tile_h, tile_w = tile_m.shape

    src_r0, src_c0 = 0, 0
    dst_r0, dst_c0 = row_off, col_off
    dst_r1, dst_c1 = row_off + tile_h, col_off + tile_w

    if dst_r0 < 0:
        src_r0 = -dst_r0
        dst_r0 = 0
    if dst_c0 < 0:
        src_c0 = -dst_c0
        dst_c0 = 0
    dst_r1 = min(dst_r1, height)
    dst_c1 = min(dst_c1, width)
    if dst_r1 <= dst_r0 or dst_c1 <= dst_c0:
        return

    src = tile_m[src_r0:src_r0 + (dst_r1 - dst_r0),
                 src_c0:src_c0 + (dst_c1 - dst_c0)]
    valid = src > MIN_VALID_ELEV_M
    canvas[dst_r0:dst_r1, dst_c0:dst_c1][valid] = src[valid]


class MosaicCanceled(Exception):
    """Raised by :func:`build_mosaic` when its ``should_cancel`` hook goes True.

    Mosaicking a whole terrain cache is minutes of work on a big analysis, and
    it is the first thing the sea-level view does — without a way out, closing
    QGIS mid-build would leave a thread running past the widget that owns it.
    """


def build_mosaic(headers: Sequence[TileHeader], grid: MosaicGrid,
                 should_cancel: Optional[Callable[[], bool]] = None):
    """Assemble *headers* onto *grid* as a float32 metres array.

    Pixels no tile covered keep :data:`MOSAIC_NODATA_M`.  Tiles finer than the
    grid are decimated by index selection rather than averaged: this is a
    terrain *surface*, and the caller resamples it properly (bilinear, via
    GDAL) when it matters.

    *should_cancel* is polled once per tile; when it goes True the assembly
    stops with :class:`MosaicCanceled` and nothing is written.
    """
    import numpy as np

    canvas = np.full((grid.height, grid.width), MOSAIC_NODATA_M,
                     dtype=np.float32)
    for header in headers:
        if should_cancel is not None and should_cancel():
            raise MosaicCanceled()
        raw = read_tile(header)
        if raw is None:
            continue
        tile_m = raw.astype(np.float32) * ELEV_STEP_M

        span_deg = header.size * header.pixel_res
        edge = max(1, int(round(span_deg / grid.resolution)))
        if edge != header.size:
            rows = np.linspace(0, header.size - 1, edge).astype(int)
            tile_m = tile_m[rows, :][:, rows]

        col_off = int(round((header.ul_lon - grid.west) / grid.resolution))
        row_off = int(round((grid.north - header.ul_lat) / grid.resolution))
        paste_tile(canvas, tile_m, row_off, col_off)
    return canvas


class MosaicResult(NamedTuple):
    """What :func:`mosaic_to_geotiff` wrote."""

    path: str
    grid: MosaicGrid
    tile_count: int


def mosaic_to_geotiff(
    abt_dir: str,
    out_path: str,
    max_dim: int = DEFAULT_MAX_DIM,
    on_log: Optional[Callable[[str], Any]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> MosaicResult:
    """Mosaic every tile in *abt_dir* into one WGS84 float32 GeoTIFF.

    Raises ValueError when the directory holds no readable tile, and
    :class:`MosaicCanceled` if *should_cancel* goes True — before any file is
    created, so a cancelled run leaves nothing behind.
    """
    from osgeo import gdal, osr

    headers = read_headers(list_tiles(abt_dir))
    if not headers:
        raise ValueError(
            f"No readable .abt terrain tiles in {abt_dir}"
        )
    grid = mosaic_grid(headers, max_dim)
    if grid is None:  # pragma: no cover — headers is non-empty here
        raise ValueError(f"Could not derive a grid for the tiles in {abt_dir}")

    if on_log is not None:
        note = " (downsampled)" if grid.decimated else ""
        on_log(
            f"Mosaicking {len(headers)} .abt tiles into "
            f"{grid.width}x{grid.height} px at "
            f"{grid.resolution * 111111:.1f} m{note}"
        )

    canvas = build_mosaic(headers, grid, should_cancel)

    gdal.UseExceptions()
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(
        out_path, grid.width, grid.height, 1, gdal.GDT_Float32,
        options=["COMPRESS=DEFLATE", "TILED=YES"],
    )
    dataset.SetGeoTransform(grid.geo_transform)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    dataset.SetProjection(srs.ExportToWkt())
    band = dataset.GetRasterBand(1)
    band.SetNoDataValue(MOSAIC_NODATA_M)
    band.WriteArray(canvas)
    band.FlushCache()
    band = None
    dataset = None

    return MosaicResult(out_path, grid, len(headers))
