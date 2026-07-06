"""Convert swissALTIRegio ``.xyz.zip`` tiles into georeferenced GeoTIFFs.

Why this exists
---------------
swisstopo ships swissALTIRegio as gridded ASCII ``.xyz`` tiles (``x y z`` with
10 m spacing) in **LV95 / EPSG:2056**, but the raw ``.xyz`` format carries **no
CRS**. Loaded directly in QGIS the coordinates (eastings/northings in the
millions) are interpreted in the project CRS, so the tiles jump to the wrong
place — usually far off near 0/0. Assigning EPSG:2056 fixes the position.

This tool unzips each tile, writes a GeoTIFF with EPSG:2056 assigned (no
reprojection — the data is already LV95, we only label it), and can build a
single ``.vrt`` mosaic over the lot. Point the plugin's "local terrain
directory" at the output folder (it scans for ``.tif``) to feed these into the
download -> convert -> select pipeline, or just load the ``.vrt`` to view them
in the correct location.

Run it with a GDAL-equipped Python — QGIS's own Python or the OSGeo4W shell:
    "C:\\OSGeo4W\\bin\\python-qgis.bat" tools/xyz_to_geotiff.py \
        --tiles data/swissaltiregio_tiles --out data/swissaltiregio_tif --vrt

Usage:
    python tools/xyz_to_geotiff.py --tiles <dir> --out <dir> [--vrt] [--workers N]
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

# LV95 — the CRS swissALTIRegio is delivered in (encoded as _2056_ in filenames).
SWISS_CRS = "EPSG:2056"

try:
    from osgeo import gdal
except ImportError:
    print(
        "[Error] Could not import osgeo.gdal. Run this with a GDAL-equipped "
        "Python — QGIS's Python or the OSGeo4W shell (python-qgis.bat), not a "
        "bare system Python.",
        file=sys.stderr,
    )
    raise SystemExit(1)

gdal.UseExceptions()

_TIF_CREATION = ["COMPRESS=DEFLATE", "PREDICTOR=2", "TILED=YES"]


def _is_valid_tif(path: str) -> bool:
    """True if *path* opens as a raster with a defined CRS (already converted)."""
    if not os.path.isfile(path) or os.path.getsize(path) < 128:
        return False
    try:
        ds = gdal.Open(path)
        ok = ds is not None and bool(ds.GetProjection())
        ds = None
        return ok
    except Exception:
        return False


def convert_one(zip_path: str, out_dir: str) -> tuple[str, str, str]:
    """Convert one ``.xyz.zip`` to an EPSG:2056 GeoTIFF.

    Returns (name, status, detail); status in {skip, ok, fail}.
    """
    base = os.path.basename(zip_path)
    stem = base[:-4] if base.lower().endswith(".zip") else base  # strip .zip
    if stem.lower().endswith(".xyz"):
        stem = stem[:-4]
    out_tif = os.path.join(out_dir, stem + ".tif")

    if _is_valid_tif(out_tif):
        return (base, "skip", out_tif)

    tmp_xyz = None
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = [n for n in zf.namelist() if n.lower().endswith(".xyz")]
            if not members:
                return (base, "fail", "no .xyz inside zip")
            with zf.open(members[0]) as src:
                fd, tmp_xyz = tempfile.mkstemp(suffix=".xyz")
                with os.fdopen(fd, "wb") as dst:
                    dst.write(src.read())

        # -a_srs semantics: assign EPSG:2056, do NOT reproject (data is already
        # LV95). The XYZ driver skips the non-numeric "x y z" header row.
        gdal.Translate(
            out_tif, tmp_xyz,
            options=gdal.TranslateOptions(
                format="GTiff",
                outputSRS=SWISS_CRS,
                creationOptions=_TIF_CREATION,
            ),
        )
        if not _is_valid_tif(out_tif):
            return (base, "fail", "GeoTIFF invalid after translate")
        return (base, "ok", out_tif)
    except Exception as exc:  # noqa: BLE001 — report any tile failure, keep going
        return (base, "fail", str(exc))
    finally:
        if tmp_xyz and os.path.exists(tmp_xyz):
            try:
                os.remove(tmp_xyz)
            except OSError:
                pass


def build_vrt(out_dir: str, vrt_path: str) -> int:
    """Build a single VRT mosaic over every GeoTIFF in *out_dir*."""
    tifs = sorted(
        os.path.join(out_dir, f)
        for f in os.listdir(out_dir)
        if f.lower().endswith(".tif")
    )
    if not tifs:
        return 0
    ds = gdal.BuildVRT(vrt_path, tifs)
    if ds is None:
        raise RuntimeError("BuildVRT returned None")
    ds.FlushCache()
    ds = None
    return len(tifs)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Convert swissALTIRegio .xyz.zip tiles to EPSG:2056 GeoTIFFs."
    )
    ap.add_argument("--tiles", required=True, help="Directory of .xyz.zip tiles.")
    ap.add_argument("--out", required=True, help="Output directory for GeoTIFFs.")
    ap.add_argument("--workers", type=int, default=6, help="Parallel workers.")
    ap.add_argument("--vrt", action="store_true",
                    help="Also build a single mosaic.vrt over all GeoTIFFs.")
    args = ap.parse_args()

    zips = sorted(
        os.path.join(args.tiles, f)
        for f in os.listdir(args.tiles)
        if f.lower().endswith(".xyz.zip")
    )
    if not zips:
        print(f"[Error] No .xyz.zip tiles in {args.tiles}", file=sys.stderr)
        return 1
    os.makedirs(args.out, exist_ok=True)

    total = len(zips)
    print(f"xyz->GeoTIFF ({SWISS_CRS}): {total} tiles -> {args.out} "
          f"({args.workers} workers)")
    t0 = time.perf_counter()
    ok = skip = fail = 0
    failures: list[str] = []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(convert_one, z, args.out): z for z in zips}
        done = 0
        for fut in as_completed(futs):
            name, status, detail = fut.result()
            done += 1
            if status == "ok":
                ok += 1
            elif status == "skip":
                skip += 1
            else:
                fail += 1
                failures.append(f"{name}\t{detail}")
            if done % 25 == 0 or done == total:
                elapsed = time.perf_counter() - t0
                rate = done / elapsed if elapsed else 0
                eta = (total - done) / rate if rate else 0
                print(
                    f"  {done}/{total}  ok={ok} skip={skip} fail={fail}  "
                    f"{rate:.1f} tiles/s  ETA {eta/60:.1f} min",
                    flush=True,
                )

    if failures:
        fail_log = os.path.join(args.out, "_convert_failures.txt")
        with open(fail_log, "w", encoding="utf-8") as fh:
            fh.write("\n".join(failures) + "\n")
        print(f"[Warn] {fail} tiles failed — logged to {fail_log}")

    if args.vrt and ok + skip > 0:
        vrt_path = os.path.join(args.out, "mosaic.vrt")
        n = build_vrt(args.out, vrt_path)
        print(f"Built mosaic over {n} GeoTIFFs -> {vrt_path}")

    dt = time.perf_counter() - t0
    print(f"Done in {dt/60:.1f} min: ok={ok} skip={skip} fail={fail} of {total}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
