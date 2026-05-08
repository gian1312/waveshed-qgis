"""Terrain adapter — converts any QGIS raster layer to .abt tiles for aether_core.

Three extraction paths (fastest first):
  1. Rust downloader: XYZ tiles → .abt directly (parallel HTTP, no intermediate)
  2. GDAL Warp: local GeoTIFF/VRT/COG → temp GeoTIFF → converter → .abt
  3. QGIS writeRaster: any provider (WMS, WCS, etc.) → temp GeoTIFF → converter → .abt

Also supports a local terrain directory of GeoTIFF/DEM files (GDAL path).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import subprocess
import tempfile
import time
import urllib.parse
from typing import Any, Dict, List, Optional

from osgeo import gdal

_SUBPROCESS_FLAGS = (
    subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
)


def _log(msg: str) -> None:
    try:
        from qgis.core import Qgis, QgsMessageLog
        QgsMessageLog.logMessage(msg, "AETHER-Terrain", Qgis.MessageLevel.Info)
    except Exception:
        pass


def get_cache_dir() -> str:
    from qgis.core import QgsSettings
    return QgsSettings().value("aether/cache_dir", os.path.expanduser("~/.aether/cache"))


def _get_download_connections() -> int:
    from qgis.core import QgsSettings
    return int(QgsSettings().value("aether/download_connections", 256))


def get_source_resolution_info(dem_layer: Any) -> str:
    """Read the resolution from the source. For XYZ tiles, compute from zoom level."""
    try:
        source = dem_layer.source()

        # XYZ tiles: compute from zmax.
        if "type=xyz" in source:
            params = dict(urllib.parse.parse_qsl(source))
            z_max = int(params.get("zmax", "15"))
            # At equator: res = 40075000 / (2^z * 256)
            res_equator = 40_075_000 / (2 ** z_max * 256)
            encoding = params.get("interpretation", "")
            label = encoding if encoding else "XYZ"
            return f"{label} z{z_max} (~{res_equator:.0f}m at equator, less at higher latitudes)"

        # Local files: read from provider.
        provider = dem_layer.dataProvider()
        if provider is None or provider.xSize() <= 0 or provider.ySize() <= 0:
            return "Unknown"

        ext = provider.extent()
        src_crs = dem_layer.crs()
        if src_crs.isGeographic():
            center_lat = (ext.yMinimum() + ext.yMaximum()) / 2.0
            cos_lat = max(math.cos(math.radians(center_lat)), 0.01)
            x_m = ext.width() / provider.xSize() * 111_111 * cos_lat
            y_m = ext.height() / provider.ySize() * 111_111
            return f"~{min(x_m, y_m):.1f} m/px ({provider.xSize()}x{provider.ySize()})"
        else:
            x_m = ext.width() / provider.xSize()
            y_m = ext.height() / provider.ySize()
            return f"~{min(x_m, y_m):.1f} m/px ({provider.xSize()}x{provider.ySize()})"
    except Exception as e:
        return f"Unknown ({e})"


# ---------------------------------------------------------------------------
# Bbox
# ---------------------------------------------------------------------------

def _compute_sector_bbox(
    tx_lat: float, tx_lon: float, max_range_km: float,
    az_start: float = 0.0, az_end: float = 360.0,
) -> Dict[str, float]:
    """Compute tight bounding box for circular sector.

    Matches aether_core's bbox logic (coverage.rs).  Only includes the
    area actually covered by the azimuth arc at the given range.
    """
    earth_r = 6_371_000.0
    pi_180 = math.pi / 180.0
    range_m = max_range_km * 1000.0
    cos_tx = max(math.cos(math.radians(tx_lat)), 1e-6)

    range_deg_lat = range_m / (earth_r * pi_180)
    range_deg_lon = range_m / (earth_r * cos_tx * pi_180)

    def in_arc(a: float) -> bool:
        if az_start <= az_end:
            return az_start <= a <= az_end
        return a >= az_start or a <= az_end          # wraps past 360

    # Test arc endpoints + any cardinal direction inside the arc.
    test_angles = [az_start, az_end]
    for cardinal in (0.0, 90.0, 180.0, 270.0):
        if in_arc(cardinal):
            test_angles.append(cardinal)

    min_dlat = 0.0
    max_dlat = 0.0
    min_dlon = 0.0
    max_dlon = 0.0
    for a in test_angles:
        rad = math.radians(a)
        dlat = range_deg_lat * math.cos(rad)
        dlon = range_deg_lon * math.sin(rad)
        min_dlat = min(min_dlat, dlat)
        max_dlat = max(max_dlat, dlat)
        min_dlon = min(min_dlon, dlon)
        max_dlon = max(max_dlon, dlon)

    margin_lat = range_deg_lat * 0.01
    margin_lon = range_deg_lon * 0.01
    return {
        "north": tx_lat + max_dlat + margin_lat,
        "south": tx_lat + min_dlat - margin_lat,
        "east":  tx_lon + max_dlon + margin_lon,
        "west":  tx_lon + min_dlon - margin_lon,
    }


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_key(source: str, bbox: Dict[str, float], resolution_m: int) -> str:
    return hashlib.md5(f"{source}|{bbox}|{resolution_m}".encode()).hexdigest()


def _cache_hit(cache_dir: str) -> bool:
    if not os.path.isdir(cache_dir):
        return False
    return any(f.endswith(".abt") for f in os.listdir(cache_dir))


# ---------------------------------------------------------------------------
# Sub-tile specs (1-degree grid, matching prepare_data.py)
# ---------------------------------------------------------------------------

def _compute_subtiles(
    bbox: Dict[str, float],
    resolution_m: int,
    tx_lat: float = 0.0,
    tx_lon: float = 0.0,
    max_range_km: float = 0.0,
    az_start: float = 0.0,
    az_end: float = 360.0,
) -> List[Dict[str, Any]]:
    tiles = []
    sub = 1.0
    lat_start = math.floor(bbox["south"] / sub) * sub
    lon_start = math.floor(bbox["west"] / sub) * sub

    # Pre-compute for circle/sector test (skip tiles outside the range).
    earth_r = 6_371_000.0
    range_m = max_range_km * 1000.0 if max_range_km > 0 else 0.0
    cos_tx = max(math.cos(math.radians(tx_lat)), 1e-6)

    def in_arc(a: float) -> bool:
        if az_start <= az_end:
            return az_start <= a <= az_end
        return a >= az_start or a <= az_end

    def tile_intersects_sector(t_south: float, t_north: float, t_west: float, t_east: float) -> bool:
        """Check if a 1-degree tile intersects the circular sector."""
        if range_m <= 0:
            return True  # No range filter — keep all.

        # 1. Circle test: nearest point on tile rectangle to TX.
        clamp_lat = max(t_south, min(tx_lat, t_north))
        clamp_lon = max(t_west, min(tx_lon, t_east))
        dlat_m = (clamp_lat - tx_lat) * earth_r * math.pi / 180.0
        dlon_m = (clamp_lon - tx_lon) * earth_r * cos_tx * math.pi / 180.0
        dist_m = math.sqrt(dlat_m * dlat_m + dlon_m * dlon_m)
        if dist_m > range_m * 1.02:  # 2% margin
            return False

        # 2. Azimuth test (only if not full circle).
        if az_start == 0.0 and az_end == 360.0:
            return True

        # Check if any tile corner falls in the arc, or if the arc
        # passes through the tile.  Test all 4 corners + tile centre.
        for plat in (t_south, t_north, (t_south + t_north) / 2):
            for plon in (t_west, t_east, (t_west + t_east) / 2):
                dy = plat - tx_lat
                dx = (plon - tx_lon) * cos_tx
                if abs(dy) < 1e-12 and abs(dx) < 1e-12:
                    return True  # TX is inside the tile.
                bearing = math.degrees(math.atan2(dx, dy)) % 360.0
                if in_arc(bearing):
                    return True

        return False

    lat = lat_start
    while lat < bbox["north"]:
        lon = lon_start
        while lon < bbox["east"]:
            if not tile_intersects_sector(lat, lat + sub, lon, lon + sub):
                lon = round(lon + sub, 6)
                continue

            target_deg = resolution_m / 111_111.0
            calc_size = int(round(sub / target_deg))
            calc_size = ((calc_size + 3) // 4) * 4
            exact_res = sub / calc_size * 111_111.0

            tiles.append({
                "ul_lat": round(lat + sub, 6),
                "ul_lon": round(lon, 6),
                "size_px": calc_size,
                "exact_res_m": exact_res,
                "filename": f"tile_N{lat + sub:.2f}E{lon:.2f}_{resolution_m}m.abt",
            })
            lon = round(lon + sub, 6)
        lat = round(lat + sub, 6)
    return tiles


def _estimate_abt_disk_mb(subtiles: List[Dict[str, Any]]) -> int:
    """Estimate total disk space needed for .abt output files (MB)."""
    total = 0
    for t in subtiles:
        sz = t["size_px"]
        bpr = sz * 2
        stride = (bpr + 255) & ~255
        total += 44 + stride * sz
    return total // (1024 * 1024)


# ---------------------------------------------------------------------------
# Path 1: Rust downloader (XYZ → .abt directly)
# ---------------------------------------------------------------------------

def _try_rust_download(
    source: str,
    cache_dir: str,
    bbox: Dict[str, float],
    resolution_m: int,
    subtiles: List[Dict[str, Any]],
    binary_manager: Any,
) -> bool:
    """Download XYZ tiles via Rust and produce .abt files directly."""
    if "type=xyz" not in source:
        return False

    params = dict(urllib.parse.parse_qsl(source))
    url_raw = params.get("url", "")
    if not url_raw:
        return False

    url_template = urllib.parse.unquote(url_raw)

    encoding = "terrarium"
    if "mapbox" in params.get("interpretation", "").lower():
        encoding = "mapbox"

    # Zoom: at lat θ, ground resolution at zoom z is:
    #   res = 40075000 * cos(θ) / (2^z * 256)
    # So z = log2(40075000 * cos(θ) / (res * 256))
    # We need res <= resolution_m, so: z = ceil(log2(...))
    center_lat = (bbox["north"] + bbox["south"]) / 2.0
    cos_lat = max(math.cos(math.radians(center_lat)), 0.1)
    z_max = int(params.get("zmax", "15"))
    z_ideal = math.ceil(math.log2(40_075_000.0 * cos_lat / (resolution_m * 256)))
    zoom = min(max(z_ideal, 0), z_max)

    try:
        converter_exe = binary_manager.find_binary("aether_converter")
    except RuntimeError:
        return False

    tile_specs = [
        {
            "filename": t["filename"],
            "ul_lat": t["ul_lat"],
            "ul_lon": t["ul_lon"],
            "size_px": t["size_px"],
            "resolution_m": t["exact_res_m"],
        }
        for t in subtiles
    ]

    job = {
        "url_template": url_template,
        "encoding": encoding,
        "output_dir": os.path.abspath(cache_dir),
        "tiles": tile_specs,
        "zoom": zoom,
        "max_connections": _get_download_connections(),
    }

    job_file = os.path.join(tempfile.gettempdir(), f"aether_dl_{os.getpid()}.json")
    with open(job_file, "w") as fh:
        json.dump(job, fh)

    _log(f"  Rust download: z={zoom}, {len(tile_specs)} tiles, encoding={encoding}")
    t = time.perf_counter()

    try:
        proc = subprocess.Popen(
            [converter_exe, "download", "--job-file", job_file],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=_SUBPROCESS_FLAGS,
        )
        # Stream stderr for progress (Rust prints to stderr).
        stderr_lines: List[str] = []
        for line in iter(proc.stderr.readline, ""):
            line = line.strip()
            if line:
                _log(f"    {line}")
                stderr_lines.append(line)
        proc.wait()

        if os.path.exists(job_file):
            #os.remove(job_file)
            pass
        if proc.returncode != 0:
            stderr_text = " ".join(stderr_lines).lower()
            # Disk space errors are non-recoverable — don't waste time with fallback
            if "insufficient disk" in stderr_text or "not enough space" in stderr_text:
                detail = stderr_lines[-1] if stderr_lines else "unknown"
                raise RuntimeError(
                    f"Insufficient disk space for terrain download. {detail}"
                )
            _log(f"  Rust download failed (exit {proc.returncode})")
            return False

        _log(f"  Rust download: {time.perf_counter() - t:.1f}s")
        return _cache_hit(cache_dir)
    except RuntimeError:
        raise
    except Exception as exc:
        _log(f"  Rust download error: {exc}")
        return False


# ---------------------------------------------------------------------------
# Path 2/3: GeoTIFF extraction (GDAL or QGIS) + converter
# ---------------------------------------------------------------------------

def _try_gdal_warp(src: str, dest: str, bbox: Dict[str, float]) -> bool:
    try:
        r = gdal.Warp(dest, src, options=gdal.WarpOptions(
            dstSRS="EPSG:4326",
            outputBounds=[bbox["west"], bbox["south"], bbox["east"], bbox["north"]],
            format="GTiff", outputType=gdal.GDT_Float32, resampleAlg=gdal.GRA_Bilinear,
        ))
        if r:
            r.FlushCache()
            r = None
            return True
    except Exception:
        pass
    return False


def _export_via_qgis(dem_layer: Any, dest: str, bbox: Dict[str, float], resolution_m: int) -> None:
    from qgis.core import (
        QgsCoordinateReferenceSystem, QgsProject,
        QgsRasterFileWriter, QgsRasterPipe, QgsRasterProjector, QgsRectangle,
    )
    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    extent = QgsRectangle(bbox["west"], bbox["south"], bbox["east"], bbox["north"])
    provider = dem_layer.dataProvider()
    if provider is None:
        raise RuntimeError("No data provider")

    pipe = QgsRasterPipe()
    if not pipe.set(provider.clone()):
        raise RuntimeError("Pipe setup failed")

    if dem_layer.crs() != wgs84:
        proj = QgsRasterProjector()
        proj.setCrs(dem_layer.crs(), wgs84, QgsProject.instance().transformContext())
        if not pipe.insert(pipe.size(), proj):
            raise RuntimeError("Projector insert failed")

    deg_px = resolution_m / 111_111.0
    nc = max(1, int(round(extent.width() / deg_px)))
    nr = max(1, int(round(extent.height() / deg_px)))
    cap = 16384
    if max(nc, nr) > cap:
        r = max(nc, nr) / cap
        nc, nr = int(nc / r), int(nr / r)

    _log(f"    writeRaster {nc}x{nr} ({nc * nr / 1e6:.1f}M px)...")
    t = time.perf_counter()
    w = QgsRasterFileWriter(dest)
    w.setOutputFormat("GTiff")
    err = w.writeRaster(pipe, nc, nr, extent, wgs84, QgsProject.instance().transformContext())
    _log(f"    writeRaster: {time.perf_counter() - t:.1f}s (err={err})")
    if err != QgsRasterFileWriter.NoError:
        raise RuntimeError(f"writeRaster failed (code {err})")


def _extract_from_local_dir(terrain_dir: str, dest: str, bbox: Dict[str, float]) -> None:
    tifs = []
    for root, _, files in os.walk(terrain_dir):
        for f in files:
            if f.lower().endswith((".tif", ".tiff", ".dem", ".hgt")):
                tifs.append(os.path.join(root, f))
    if not tifs:
        raise RuntimeError(f"No terrain files in {terrain_dir}")

    _log(f"  local dir: {len(tifs)} files")
    vrt = dest + ".vrt"
    ds = gdal.BuildVRT(vrt, tifs)
    if ds is None:
        raise RuntimeError("BuildVRT failed")
    ds.FlushCache()
    ds = None
    if not _try_gdal_warp(vrt, dest, bbox):
        raise RuntimeError("GDAL Warp on VRT failed")
    if os.path.exists(vrt):
        os.remove(vrt)


def _extract_reproject(dem_layer: Any, dest: str, bbox: Dict[str, float],
                       resolution_m: int, binary_manager: Any = None) -> None:
    source = dem_layer.source()
    if os.path.isfile(source) or source.startswith("/vsi"):
        t = time.perf_counter()
        if _try_gdal_warp(source, dest, bbox):
            _log(f"  GDAL warp: {time.perf_counter() - t:.1f}s")
            return
    _export_via_qgis(dem_layer, dest, bbox, resolution_m)


def _run_converter(exe: str, job_file: str) -> None:
    r = subprocess.run(
        [exe, "ingest", "--job-file", job_file],
        capture_output=True, text=True, creationflags=_SUBPROCESS_FLAGS,
    )
    if r.returncode != 0:
        raise RuntimeError(f"converter failed ({r.returncode}):\n{r.stderr}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def prepare_terrain(
    dem_layer: Any,
    tx_lat: float,
    tx_lon: float,
    max_range_km: float,
    resolution_m: int,
    binary_manager: Any,
    feedback: Optional[Any] = None,
    terrain_dir: Optional[str] = None,
    az_start: float = 0.0,
    az_end: float = 360.0,
) -> str:
    """Produce .abt terrain tiles for aether_core. Returns cache directory."""
    t0 = time.perf_counter()
    source = terrain_dir or dem_layer.source()
    _log(f"prepare_terrain: resolution={resolution_m}m, range={max_range_km}km, "
         f"az={az_start:.1f}-{az_end:.1f}")

    bbox = _compute_sector_bbox(tx_lat, tx_lon, max_range_km, az_start, az_end)

    cache_hash = _cache_key(source, bbox, resolution_m)
    # Include azimuth in cache key so different sectors don't collide.
    if not (az_start == 0.0 and az_end == 360.0):
        cache_hash = hashlib.md5(
            f"{cache_hash}|{az_start}|{az_end}".encode()
        ).hexdigest()
    cache_dir = os.path.join(get_cache_dir(), cache_hash)

    if _cache_hit(cache_dir):
        _log(f"  cache HIT ({time.perf_counter() - t0:.1f}s)")
        return cache_dir

    subtiles = _compute_subtiles(bbox, resolution_m, tx_lat, tx_lon,
                                  max_range_km, az_start, az_end)
    disk_mb = _estimate_abt_disk_mb(subtiles)
    _log(f"  cache MISS — {len(subtiles)} sub-tiles, ~{disk_mb} MB output, "
         f"bbox N={bbox['north']:.3f} S={bbox['south']:.3f} "
         f"E={bbox['east']:.3f} W={bbox['west']:.3f}")
    os.makedirs(cache_dir, exist_ok=True)

    # Path 1: Rust downloader (XYZ → .abt directly, no intermediate file).
    if not terrain_dir and "type=xyz" in dem_layer.source():
        if _try_rust_download(dem_layer.source(), cache_dir, bbox,
                              resolution_m, subtiles, binary_manager):
            _log(f"  TOTAL: {time.perf_counter() - t0:.1f}s")
            return cache_dir
        _log("  Rust download unavailable, falling back")

    # Path 2/3: Extract GeoTIFF, then convert.
    temp_tif = os.path.join(tempfile.gettempdir(), f"aether_{cache_hash}.tif")
    try:
        t1 = time.perf_counter()
        if terrain_dir and os.path.isdir(terrain_dir):
            _extract_from_local_dir(terrain_dir, temp_tif, bbox)
        else:
            _extract_reproject(dem_layer, temp_tif, bbox, resolution_m, binary_manager)
        _log(f"  extract: {time.perf_counter() - t1:.1f}s, "
             f"{os.path.getsize(temp_tif) / 1e6:.0f}MB")

        # Build batch converter job.
        jobs = []
        for t in subtiles:
            jobs.append({
                "output_path": os.path.abspath(os.path.join(cache_dir, t["filename"])),
                "format": "r16sint",
                "ul_lat": t["ul_lat"],
                "ul_lon": t["ul_lon"],
                "resolution_m": t["exact_res_m"],
                "size_px": t["size_px"],
                "base_tif": os.path.abspath(temp_tif),
                "swiss_tifs": [],
            })

        job_file = os.path.join(cache_dir, "batch_job.json")
        with open(job_file, "w") as fh:
            json.dump(jobs, fh)

        t2 = time.perf_counter()
        exe = binary_manager.find_binary("aether_converter")
        _run_converter(exe, job_file)
        _log(f"  converter: {time.perf_counter() - t2:.1f}s ({len(jobs)} tiles)")
    finally:
        if os.path.exists(temp_tif):
            os.remove(temp_tif)

    _log(f"  TOTAL: {time.perf_counter() - t0:.1f}s")
    return cache_dir
