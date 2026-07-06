"""Scan cached ``.abt`` terrain tiles for the defects that cause coverage stripes.

Point it at the plugin's terrain cache (``~/.aether/cache`` by default, or a
specific ``<hash>`` subfolder printed in the QGIS log as ".abt cache dir: ...").
It checks each tile for the two plugin-diagnosable causes of banding:

  1. All-zero ROWS / COLUMNS  -> a tile that failed to download (or came back as
     a blank sea-level PNG under server throttling) becomes a correctly-placed
     rectangle of zeros. Zero rows at the same latitude across several tiles =
     the horizontal bands; zero columns at the same longitude = vertical bands.
  2. Row-stride u16 overflow  -> the .abt header stores the row stride as a
     u16; a 1-degree tile at 2 m needs stride 111360, which wraps past 65535
     and makes the reader walk rows at the wrong pitch (a repeating shear).
     Recent plugin builds shrink the sub-tile size to avoid this; this flags
     any older tile still on disk that overflowed.

.abt header (little-endian, 44 bytes): [0:4]="AETH", [4:6]=version u16,
[6:8]=width u16, [8:16]=ul_lat f64, [16:24]=ul_lon f64, [24:32]=scale_y f64,
[32:40]=scale_x f64, [40:42]=base_elev i16, [42:44]=row_stride u16. Body starts
at 44; each row is row_stride bytes, first width*2 bytes are i16 elevations.

Usage:
    python tools/scan_abt.py --cache ~/.aether/cache [--zero-frac 0.001]
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from collections import defaultdict

try:
    import numpy as np
except ImportError:
    print("[Error] numpy is required. Run with QGIS's Python (it bundles numpy).",
          file=sys.stderr)
    raise SystemExit(1)


def _aligned_stride(width: int) -> int:
    """The true on-disk row stride: width*2 bytes aligned up to 256."""
    return (width * 2 + 255) & ~255


def scan_tile(path: str) -> dict:
    """Parse one .abt and scan for zero rows/cols and stride overflow."""
    data = np.fromfile(path, dtype=np.uint8)
    if data.size < 44 or bytes(data[:4]) != b"AETH":
        return {"path": path, "error": "not an .abt file"}

    width = struct.unpack_from("<H", data, 6)[0]
    ul_lat = struct.unpack_from("<d", data, 8)[0]
    ul_lon = struct.unpack_from("<d", data, 16)[0]
    scale_y = struct.unpack_from("<d", data, 24)[0]
    scale_x = struct.unpack_from("<d", data, 32)[0]
    hdr_stride = struct.unpack_from("<H", data, 42)[0]

    true_stride = _aligned_stride(width)
    file_stride = (data.size - 44) // width if width else 0

    result = {
        "path": path, "width": width, "ul_lat": ul_lat, "ul_lon": ul_lon,
        "scale_y": scale_y, "scale_x": scale_x,
        "hdr_stride": hdr_stride, "true_stride": true_stride,
        "file_stride": file_stride,
        # The reader trusts the header stride; if it disagrees with the real
        # aligned stride the u16 field overflowed and rows will be misread.
        "stride_overflow": (hdr_stride != (true_stride & 0xFFFF)),
        "zero_rows": [], "zero_cols": [], "error": None,
    }

    body_len = width * true_stride
    if data.size < 44 + body_len:
        result["error"] = (
            f"truncated: {data.size} bytes, expected >= {44 + body_len}"
        )
        return result

    body = data[44:44 + body_len].reshape(width, true_stride)
    elev = np.ascontiguousarray(body[:, : width * 2]).view(np.int16)  # (H, W)

    row_nonzero = elev.any(axis=1)
    col_nonzero = elev.any(axis=0)
    result["zero_rows"] = np.where(~row_nonzero)[0].tolist()
    result["zero_cols"] = np.where(~col_nonzero)[0].tolist()
    return result


def _ranges(indices: list) -> list:
    """Collapse a sorted index list into (start, end) inclusive ranges."""
    out = []
    for i in indices:
        if out and i == out[-1][1] + 1:
            out[-1][1] = i
        else:
            out.append([i, i])
    return [(a, b) for a, b in out]


def main() -> int:
    ap = argparse.ArgumentParser(description="Scan .abt tiles for stripe causes.")
    ap.add_argument("--cache", required=True,
                    help="Cache dir (scanned recursively for *.abt).")
    ap.add_argument("--zero-frac", type=float, default=0.0,
                    help="Only report a tile if zero rows+cols exceed this "
                         "fraction of its size (default 0 = report any).")
    args = ap.parse_args()

    abts = []
    for root, _, files in os.walk(args.cache):
        for f in files:
            if f.lower().endswith(".abt"):
                abts.append(os.path.join(root, f))
    if not abts:
        print(f"[Error] No .abt files under {args.cache}", file=sys.stderr)
        return 1
    abts.sort()

    print(f"Scanning {len(abts)} .abt tiles under {args.cache}\n")
    overflow_tiles = 0
    banded_tiles = 0
    # Latitude of every zero row, rounded, tallied across tiles -> shared
    # latitudes indicate a horizontal band spanning multiple tiles.
    lat_band_hits: dict = defaultdict(int)
    lon_band_hits: dict = defaultdict(int)

    for path in abts:
        r = scan_tile(path)
        name = os.path.basename(path)
        if r.get("error"):
            print(f"  ! {name}: {r['error']}")
            continue

        w = r["width"]
        nzr, nzc = len(r["zero_rows"]), len(r["zero_cols"])
        frac = (nzr + nzc) / (2 * w) if w else 0.0
        flags = []
        if r["stride_overflow"]:
            overflow_tiles += 1
            flags.append(
                f"STRIDE-OVERFLOW hdr={r['hdr_stride']} true={r['true_stride']}"
            )
        if nzr or nzc:
            banded_tiles += 1
            for rr in r["zero_rows"]:
                lat_band_hits[round(r["ul_lat"] - rr * r["scale_y"], 4)] += 1
            for cc in r["zero_cols"]:
                lon_band_hits[round(r["ul_lon"] + cc * r["scale_x"], 4)] += 1

        if flags or frac > args.zero_frac:
            zr = _ranges(r["zero_rows"])
            zc = _ranges(r["zero_cols"])
            print(f"  {name}: {w}x{w}  zero_rows={nzr} {zr[:4]}  "
                  f"zero_cols={nzc} {zc[:4]}  {' '.join(flags)}")

    print("\n--- summary ---")
    print(f"  tiles scanned:      {len(abts)}")
    print(f"  stride-overflow:    {overflow_tiles}  "
          f"(-> u16 overflow; upgrade plugin / avoid 2 m at 1-deg tiles)")
    print(f"  tiles with zeros:   {banded_tiles}  "
          f"(-> missing/blank source tiles; lower aether/download_connections)")

    shared_lats = [(lat, n) for lat, n in lat_band_hits.items() if n >= 2]
    shared_lons = [(lon, n) for lon, n in lon_band_hits.items() if n >= 2]
    if shared_lats:
        shared_lats.sort(key=lambda t: -t[1])
        print(f"  HORIZONTAL bands at {len(shared_lats)} latitudes shared "
              f"across tiles, e.g. {shared_lats[:5]}")
    if shared_lons:
        shared_lons.sort(key=lambda t: -t[1])
        print(f"  VERTICAL bands at {len(shared_lons)} longitudes shared "
              f"across tiles, e.g. {shared_lons[:5]}")
    if not (overflow_tiles or banded_tiles):
        print("  No zero bands or stride overflow found — if stripes persist, "
              "the artifact is downstream in aether_core tile sampling.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
