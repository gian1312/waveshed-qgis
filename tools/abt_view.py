"""Render an ``.abt`` terrain tile and locate the *scattered* zero blocks that
cause the stripe/patch artifacts ``scan_abt.py`` cannot see.

``scan_abt.py`` only flags rows/columns that are zero across the *entire* tile.
A failed or blank 256-px web tile leaves a zero **rectangle** that still shares
its rows and columns with real terrain, so a full-row/col scan reports the tile
as clean while you can plainly see the hole. This tool instead:

  1. Renders each ``.abt`` to a georeferenced GeoTIFF (if GDAL/osgeo is present,
     e.g. QGIS's Python) or a 16-bit PGM (numpy only) so you can open it and
     *see* the holes, overlaid on your coverage when it's a GeoTIFF.
  2. Detects fully-zero **blocks** (default 64x64 px) — the scattered patches —
     and prints each one's pixel box, centre lat/lon, and, with ``--zoom``, the
     source slippy-map tile ``z/x/y`` so you can cross-check the download log
     (``[Download] first connect error … x=…/y=…`` / ``SLOW tile …``) or open
     the tile URL directly.

.abt header (little-endian, 44 bytes): [6:8]=width u16, [8:16]=ul_lat f64,
[16:24]=ul_lon f64, [24:32]=scale_y f64, [32:40]=scale_x f64, [42:44]=stride
u16. Body at 44; each row is ``stride`` bytes, first ``width*2`` are i16
elevations (stored as metres*2).

Usage:
    python tools/abt_view.py --cache <cache-or-tile> [--render out_dir]
                             [--zoom 14] [--block 64]
"""

from __future__ import annotations

import argparse
import math
import os
import struct
import sys

try:
    import numpy as np
except ImportError:
    print("[Error] numpy required. Run with QGIS's Python.", file=sys.stderr)
    raise SystemExit(1)

try:
    from osgeo import gdal, osr
    _HAVE_GDAL = True
except ImportError:
    _HAVE_GDAL = False


def _aligned_stride(width: int) -> int:
    return (width * 2 + 255) & ~255


def load_abt(path: str):
    """Return (elev int16 HxW, ul_lat, ul_lon, scale_y, scale_x) or None."""
    data = np.fromfile(path, dtype=np.uint8)
    if data.size < 44 or bytes(data[:4]) != b"AETH":
        return None
    width = struct.unpack_from("<H", data, 6)[0]
    ul_lat = struct.unpack_from("<d", data, 8)[0]
    ul_lon = struct.unpack_from("<d", data, 16)[0]
    scale_y = struct.unpack_from("<d", data, 24)[0]
    scale_x = struct.unpack_from("<d", data, 32)[0]
    stride = _aligned_stride(width)
    body_len = width * stride
    if width == 0 or data.size < 44 + body_len:
        return None
    body = data[44:44 + body_len].reshape(width, stride)
    elev = np.ascontiguousarray(body[:, : width * 2]).view(np.int16)
    return elev, ul_lat, ul_lon, scale_y, scale_x


# i16 at/under this renders as NaN/transparent in the map-converter inspector
# (it keeps only elev*0.5 > -5000 m).
NODATA_I16 = -10000


def zero_blocks(elev, block: int):
    """List of (r0, c0) top-left px of block-sized cells that are entirely a
    'hole' — a nodata sentinel (<= -5000 m, the inspector's NaN) or all zero."""
    h, w = elev.shape
    nbr, nbc = h // block, w // block
    if nbr == 0 or nbc == 0:
        return []
    core = elev[: nbr * block, : nbc * block].reshape(nbr, block, nbc, block)
    hole = (core <= NODATA_I16).all(axis=(1, 3)) | ~core.any(axis=(1, 3))
    return [(int(br) * block, int(bc) * block)
            for br, bc in zip(*np.where(hole))]


def lonlat_to_tile(lon: float, lat: float, z: int):
    n = 2 ** z
    xt = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    yt = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return xt, yt


def render(path, elev, ul_lat, ul_lon, scale_y, scale_x, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(path))[0]
    if _HAVE_GDAL:
        out = os.path.join(out_dir, base + ".tif")
        h, w = elev.shape
        ds = gdal.GetDriverByName("GTiff").Create(out, w, h, 1, gdal.GDT_Int16)
        ds.SetGeoTransform([ul_lon, scale_x, 0, ul_lat, 0, -scale_y])
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        ds.SetProjection(srs.ExportToWkt())
        ds.GetRasterBand(1).WriteArray(elev)
        ds = None
        return out
    # Fallback: 16-bit PGM (shift to unsigned so 0 shows as black).
    out = os.path.join(out_dir, base + ".pgm")
    vis = (elev.astype(np.int32) - int(elev.min())).astype(">u2")
    h, w = elev.shape
    with open(out, "wb") as fh:
        fh.write(f"P5\n{w} {h}\n65535\n".encode())
        fh.write(vis.tobytes())
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Render + locate .abt zero blocks.")
    ap.add_argument("--cache", required=True,
                    help="A .abt file, or a dir scanned recursively for *.abt.")
    ap.add_argument("--render", metavar="DIR",
                    help="Write a viewable GeoTIFF (or PGM) per tile here.")
    ap.add_argument("--zoom", type=int, default=0,
                    help="XYZ zoom used for the download (from the log, e.g. "
                         "14) to map each gap to a z/x/y tile.")
    ap.add_argument("--block", type=int, default=64,
                    help="Zero-block size in px (default 64 ~ a quarter web "
                         "tile at 10 m).")
    args = ap.parse_args()

    if os.path.isfile(args.cache):
        abts = [args.cache]
    else:
        abts = []
        for root, _, files in os.walk(args.cache):
            abts += [os.path.join(root, f) for f in files
                     if f.lower().endswith(".abt")]
    abts.sort()
    if not abts:
        print(f"[Error] No .abt under {args.cache}", file=sys.stderr)
        return 1

    if args.render and not _HAVE_GDAL:
        print("[Note] osgeo/GDAL not found — rendering PGM instead of GeoTIFF. "
              "Run with QGIS's Python for a georeferenced .tif.\n")

    total_gaps = 0
    for path in abts:
        loaded = load_abt(path)
        name = os.path.basename(path)
        if loaded is None:
            print(f"  ! {name}: not a readable .abt")
            continue
        elev, ul_lat, ul_lon, scale_y, scale_x = loaded
        blocks = zero_blocks(elev, args.block)
        total_gaps += len(blocks)

        line = f"{name}: {elev.shape[1]}x{elev.shape[0]}  zero-blocks={len(blocks)}"
        if args.render:
            line += f"  -> {render(path, elev, ul_lat, ul_lon, scale_y, scale_x, args.render)}"
        print(line)

        for (r0, c0) in blocks[:40]:
            lat = ul_lat - (r0 + args.block / 2) * scale_y
            lon = ul_lon + (c0 + args.block / 2) * scale_x
            loc = f"px(row={r0},col={c0}) lat={lat:.5f} lon={lon:.5f}"
            if args.zoom:
                xt, yt = lonlat_to_tile(lon, lat, args.zoom)
                loc += f"  tile z={args.zoom}/x={xt}/y={yt}"
            print(f"      gap {loc}")
        if len(blocks) > 40:
            print(f"      … {len(blocks) - 40} more")

    print(f"\n{total_gaps} zero-block gap(s) across {len(abts)} tile(s).")
    if total_gaps and args.zoom:
        print("Cross-check the z/x/y against the download log's failed/SLOW "
              "tiles. If a gap tile is NOT in the log, the download counted it "
              "OK but its data never reached the .abt (decode/assembly), not a "
              "fetch failure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
