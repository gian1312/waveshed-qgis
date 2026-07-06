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
import re
import subprocess
import tempfile
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

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


# DEM sources already warned about this run (requested range exceeds the DEM
# extent). Keeps the "assuming 0 m past the border" notice to once per analysis
# run instead of once per site/height. Cleared by reset_terrain_warnings().
_warned_extent_sources: set = set()


def reset_terrain_warnings() -> None:
    """Reset per-run terrain warnings. Call once when an analysis run starts so
    the 'range exceeds DEM extent' warning is shown once, not per job."""
    _warned_extent_sources.clear()


def get_cache_dir() -> str:
    from qgis.core import QgsSettings
    return QgsSettings().value("aether/cache_dir", os.path.expanduser("~/.aether/cache"))


def _get_download_connections() -> int:
    from qgis.core import QgsSettings
    return int(QgsSettings().value("aether/download_connections", 256))


def _get_download_max_passes() -> int:
    """Max download attempts (initial + retries) when tiles fail.

    Default 4: the first pass downloads everything; each retry re-fetches ONLY
    the sub-tiles that ended up with a gap, at half the connections, so retries
    are cheap (not a full re-download). Set to 1 via QgsSettings
    ("aether/download_max_passes") to disable retries.
    """
    from qgis.core import QgsSettings
    try:
        return max(1, int(QgsSettings().value("aether/download_max_passes", 4)))
    except (TypeError, ValueError):
        return 4


# Matches the converter's authoritative completeness line, e.g.
#   [Stats] Tiles: 697340/697343 OK (100.0% success)
_TILES_OK_RE = re.compile(r"\[Stats\]\s*Tiles:\s*(\d+)\s*/\s*(\d+)\s*OK")


def _parse_download_completeness(stderr_lines: List[str]) -> Tuple[int, int]:
    """Return ``(ok_tiles, total_tiles)`` from converter stderr.

    Returns ``(-1, -1)`` when the stats line is absent (older converter or
    aborted run) so callers can fall back to a cache-only check.
    """
    ok = total = -1
    for line in stderr_lines:
        m = _TILES_OK_RE.search(line)
        if m:
            ok, total = int(m.group(1)), int(m.group(2))
    return ok, total


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


# Marker file left in a cache dir when the download finished with gaps, so the
# holed .abt set is NOT reused as a valid cache — the next run re-downloads.
_INCOMPLETE_MARKER = ".incomplete"


def _mark_incomplete(cache_dir: str) -> None:
    try:
        open(os.path.join(cache_dir, _INCOMPLETE_MARKER), "w").close()
    except OSError:
        pass


def _clear_incomplete(cache_dir: str) -> None:
    try:
        os.remove(os.path.join(cache_dir, _INCOMPLETE_MARKER))
    except OSError:
        pass


def _cache_hit(cache_dir: str) -> bool:
    if not os.path.isdir(cache_dir):
        return False
    files = os.listdir(cache_dir)
    if _INCOMPLETE_MARKER in files:
        return False  # a prior run left gaps — force a fresh download
    return any(f.endswith(".abt") for f in files)


# ---------------------------------------------------------------------------
# Sub-tile specs (1-degree grid, matching prepare_data.py)
# ---------------------------------------------------------------------------

# Tile geometry mirrors AETHER's reference pipeline
# (python/drivers/prepare_data.py) so aether_core reads plugin-generated and
# prepare_data-generated .abt tiles identically. Two pieces are kept in sync:
#   * _subtile_degrees  <-> prepare_data's `sub_size_macro`
#   * _tile_params      <-> prepare_data's calc_size / exact_res / ul_lat block
#
# u16 safety net: the .abt writer stores the row stride as a u16
# (aether_converter download.rs `stride as u16`); stride = size_px*2 aligned to
# 256 must stay < 65536, else a 2 m tile at 1 deg (size_px 55556 -> stride
# 111360) wraps and shears the terrain into bands. We shrink the tile until it
# fits — 0.1/0.5 deg high-res tiles are already well under the limit.
_ABT_MAX_SIZE_PX = 32000  # keeps aligned stride (size_px*2) below the u16 limit


def _subtile_degrees(resolution_m: int, high_res: bool = False) -> float:
    """Sub-tile size in degrees, matching prepare_data.py's ``sub_size_macro``.

    *high_res* selects the finer tiling AETHER uses when high-resolution local
    terrain is fused (0.1 deg at <=3 m, else 0.5 deg); otherwise 1 deg, like the
    base-DEM path. Either way the tile is shrunk further should the .abt row
    stride overflow the u16 header field.
    """
    if high_res:
        sub = 0.1 if resolution_m <= 3 else 0.5
    else:
        sub = 1.0
    while sub > 0.05 and resolution_m > 0 and (
        sub * 111_111.0 / resolution_m
    ) > _ABT_MAX_SIZE_PX:
        sub /= 2.0
    return sub


def _tile_params(sub: float, lat: float, lon: float,
                 resolution_m: int) -> Dict[str, Any]:
    """One tile's .abt header params — identical formula to prepare_data.py.

    ``calc_size`` is rounded up to a multiple of 4 (BC6H block alignment) and
    the resolution recomputed so the tile spans exactly *sub* degrees; ``ul_lat``
    is the tile's north edge (``lat + sub``), ``ul_lon`` its west edge.
    """
    target_deg = resolution_m / 111_111.0
    calc_size = int(round(sub / target_deg))
    calc_size = ((calc_size + 3) // 4) * 4
    exact_res = sub / calc_size * 111_111.0
    return {
        "ul_lat": round(lat + sub, 6),
        "ul_lon": round(lon, 6),
        "size_px": calc_size,
        "exact_res_m": exact_res,
        "filename": f"tile_N{lat + sub:.2f}E{lon:.2f}_{resolution_m}m.abt",
    }


def _compute_subtiles(
    bbox: Dict[str, float],
    resolution_m: int,
    tx_lat: float = 0.0,
    tx_lon: float = 0.0,
    max_range_km: float = 0.0,
    az_start: float = 0.0,
    az_end: float = 360.0,
    high_res: bool = False,
) -> List[Dict[str, Any]]:
    tiles = []
    sub = _subtile_degrees(resolution_m, high_res)
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

            tiles.append(_tile_params(sub, lat, lon, resolution_m))
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

def _run_converter_download_once(
    converter_exe: str, job_file: str
) -> Tuple[int, List[str]]:
    """Run one ``aether_converter download`` pass, streaming stderr to the log.

    Returns ``(returncode, stderr_lines)``. Raises RuntimeError on a
    non-recoverable disk-space error (no point retrying or falling back).
    """
    stderr_lines: List[str] = []
    # `with` closes the stderr pipe (and waits) so we don't leak a file handle
    # (the ResourceWarning). stdout goes to DEVNULL — Rust logs to stderr, so we
    # never read stdout and don't want its pipe filling up either.
    with subprocess.Popen(
        [converter_exe, "download", "--job-file", job_file],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        creationflags=_SUBPROCESS_FLAGS,
    ) as proc:
        # Stream stderr for progress (Rust prints to stderr).
        for line in iter(proc.stderr.readline, ""):
            line = line.strip()
            if line:
                _log(f"    {line}")
                stderr_lines.append(line)

    if proc.returncode != 0:
        stderr_text = " ".join(stderr_lines).lower()
        if "insufficient disk" in stderr_text or "not enough space" in stderr_text:
            detail = stderr_lines[-1] if stderr_lines else "unknown"
            raise RuntimeError(
                f"Insufficient disk space for terrain download. {detail}"
            )
    return proc.returncode, stderr_lines


# Concurrency floor for the halving retry — below this, extra parallelism buys
# nothing and only risks more dropped connections.
_MIN_DOWNLOAD_CONNECTIONS = 32


def _abt_has_gaps(path: str, block: int = 64) -> bool:
    """True if the .abt contains a fully-zero block of *block*×*block* pixels.

    The converter writes a *failed* XYZ tile as 0 m (aether_converter
    download.rs), so a failed tile leaves a zero block. We use this to pick
    which sub-tiles to re-download. Genuine sea-level 0 (ocean) also trips it,
    but the retry loop stops on the download's own OK/total count, so a
    legitimately-zero tile can't loop forever.
    """
    try:
        import numpy as np
        with open(path, "rb") as fh:
            head = fh.read(44)
            if len(head) < 44 or head[:4] != b"AETH":
                return False
            size = int.from_bytes(head[6:8], "little")
            stride = int.from_bytes(head[42:44], "little")
            body = np.frombuffer(fh.read(), dtype=np.uint8)
        need = size * stride
        if size < block or body.size < need:
            return False
        rows = body[:need].reshape(size, stride)
        elev = np.ascontiguousarray(rows[:, : size * 2]).view(np.int16)
        h = (size // block) * block
        zc = (elev[:h, :h] == 0).reshape(h // block, block, h // block, block)
        return bool(zc.all(axis=(1, 3)).any())
    except Exception:
        return False


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

    job_file = os.path.join(tempfile.gettempdir(), f"aether_dl_{os.getpid()}.json")

    def _run_pass(specs: List[Dict[str, Any]], conn: int):
        """Write + run one download pass for *specs* at *conn* connections.

        Returns ``(returncode, ok_tiles, total_tiles)``.
        """
        job = {
            "url_template": url_template,
            "encoding": encoding,
            "output_dir": os.path.abspath(cache_dir),
            "tiles": specs,
            "zoom": zoom,
            "max_connections": conn,
        }
        with open(job_file, "w") as fh:
            json.dump(job, fh)
        rc, lines = _run_converter_download_once(converter_exe, job_file)
        ok, total = _parse_download_completeness(lines)
        return rc, ok, total

    max_passes = _get_download_max_passes()
    conn = _get_download_connections()
    _log(f"  Rust download: z={zoom}, {len(tile_specs)} tiles, "
         f"encoding={encoding}, connections={conn}")

    # First pass downloads everything. On failure, re-download ONLY the sub-tiles
    # that actually ended up with a gap, and HALVE the connections each pass:
    # the failures are connection drops on the largest tiles under high
    # parallelism, so backing off lets them through. The converter cannot retry
    # individual XYZ tiles (it rewrites a whole .abt), so a gapped sub-tile is
    # the smallest unit we can re-fetch. An incomplete result is marked so it is
    # not reused as a valid cache — the next run re-downloads it.
    try:
        specs = tile_specs
        for attempt in range(1, max_passes + 1):
            t = time.perf_counter()
            rc, ok, total = _run_pass(specs, conn)
            elapsed = time.perf_counter() - t
            if rc != 0:
                _log(f"  Rust download failed (exit {rc})")
                return False
            if total < 0:
                # Older converter without completeness stats — trust exit 0.
                _log(f"  Rust download: {elapsed:.1f}s")
                _clear_incomplete(cache_dir)
                return _cache_hit(cache_dir)
            if ok >= total:
                _log(f"  Rust download: {elapsed:.1f}s, {ok}/{total} tiles OK "
                     f"({len(specs)} sub-tile(s) @ {conn} conn)")
                _clear_incomplete(cache_dir)
                return True

            missing = total - ok
            # Which sub-tiles actually carry a gap? Re-fetch only those.
            affected = [s for s in tile_specs
                        if _abt_has_gaps(os.path.join(cache_dir, s["filename"]))]
            if (attempt >= max_passes or not affected
                    or conn <= _MIN_DOWNLOAD_CONNECTIONS):
                _log(f"  WARNING: {missing} tiles still failed after {attempt} "
                     f"pass(es); {len(affected)} sub-tile(s) left with gaps — "
                     f"using partial terrain and marking it for re-download.")
                _mark_incomplete(cache_dir)
                return _cache_hit(cache_dir)

            conn = max(_MIN_DOWNLOAD_CONNECTIONS, conn // 2)
            specs = affected
            _log(f"  Rust download incomplete ({missing} tiles): retrying "
                 f"{len(specs)} affected sub-tile(s) @ {conn} connections "
                 f"(pass {attempt + 1}/{max_passes})")
        _mark_incomplete(cache_dir)
        return _cache_hit(cache_dir)
    except RuntimeError:
        raise
    except Exception as exc:
        _log(f"  Rust download error: {exc}")
        return False


# ---------------------------------------------------------------------------
# Path 2/3: GeoTIFF extraction (GDAL or QGIS) + converter
# ---------------------------------------------------------------------------

def _try_gdal_warp(src: str, dest: str, bbox: Dict[str, float],
                   resolution_m: Optional[int] = None) -> bool:
    try:
        opts = dict(
            dstSRS="EPSG:4326",
            outputBounds=[bbox["west"], bbox["south"], bbox["east"], bbox["north"]],
            format="GTiff", outputType=gdal.GDT_Float32, resampleAlg=gdal.GRA_Bilinear,
            # Fill everything outside the source footprint (and source-nodata
            # pixels) with 0 m rather than leaving it nodata, so an analysis
            # radius that reaches past the DEM still produces a full-extent
            # raster — sea-level assumption where we have no elevation. We do
            # NOT set dstNodata, so aether_converter reads the 0-fill as valid
            # ground instead of skipping it (which produced empty coverage).
            warpOptions=["INIT_DEST=0"],
        )
        if resolution_m:
            deg_px = resolution_m / 111_111.0
            # Pin the base raster to the analysis grid, and cap its size so a
            # radius far larger than the DEM can't allocate a giant raster
            # (the previous auto-resolution warp blew up / failed here).
            nc = (bbox["east"] - bbox["west"]) / deg_px
            nr = (bbox["north"] - bbox["south"]) / deg_px
            cap = 20000
            if max(nc, nr) > cap:
                deg_px *= max(nc, nr) / cap
            opts["xRes"] = deg_px
            opts["yRes"] = deg_px
        r = gdal.Warp(dest, src, options=gdal.WarpOptions(**opts))
        if r:
            # Strip any nodata flag so the 0-fill beyond the DEM edge (and any
            # source-nodata pixels, now 0) is read as valid sea-level ground by
            # aether_converter instead of being skipped — otherwise coverage
            # past the DEM border comes out empty even with INIT_DEST=0.
            for b in range(1, r.RasterCount + 1):
                try:
                    r.GetRasterBand(b).DeleteNoDataValue()
                except Exception:
                    pass
            r.FlushCache()
            r = None
            return True
    except Exception as exc:
        _log(f"  GDAL warp error: {exc}")
    return False


def _warn_if_bbox_exceeds_source(ds, bbox: Dict[str, float],
                                 source_key: str = "") -> None:
    """Warn (once per run) when *bbox* reaches beyond the source dataset *ds*.

    Non-fatal and best-effort: the warp fills the gap with 0 m, but the user
    should know part of the result is flat-filled rather than real terrain.
    Uses GDAL's WGS84 extent so it works regardless of the source CRS.
    *source_key* dedups the warning so a multi-site/height run shows it once,
    not per terrain build (see :func:`reset_terrain_warnings`).
    """
    try:
        info = gdal.Info(ds, format="json")
        ext = info.get("wgs84Extent") if isinstance(info, dict) else None
        if not ext:
            return
        ring = ext["coordinates"][0]
        lons = [c[0] for c in ring]
        lats = [c[1] for c in ring]
        src_w, src_e = min(lons), max(lons)
        src_s, src_n = min(lats), max(lats)
        m = 1e-6
        if (bbox["west"] < src_w - m or bbox["east"] > src_e + m
                or bbox["south"] < src_s - m or bbox["north"] > src_n + m):
            if source_key and source_key in _warned_extent_sources:
                return
            if source_key:
                _warned_extent_sources.add(source_key)
            _log(
                "  WARNING: requested area extends beyond the DEM coverage "
                f"(DEM bounds ~ N{src_n:.3f} S{src_s:.3f} E{src_e:.3f} W{src_w:.3f}). "
                "Missing areas are assumed 0 m (sea level); coverage there is "
                "not reliable — use a DEM that covers the full range."
            )
    except Exception as exc:
        _log(f"  (DEM-extent check skipped: {exc})")


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


def _extract_from_local_dir(terrain_dir: str, dest: str, bbox: Dict[str, float],
                            resolution_m: Optional[int] = None) -> None:
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
    # Tell the user (once per run) if the range spills past the available data.
    _warn_if_bbox_exceeds_source(ds, bbox, terrain_dir)
    ds = None
    if not _try_gdal_warp(vrt, dest, bbox, resolution_m):
        raise RuntimeError("GDAL Warp on VRT failed")
    if os.path.exists(vrt):
        os.remove(vrt)


def _extract_reproject(dem_layer: Any, dest: str, bbox: Dict[str, float],
                       resolution_m: int, binary_manager: Any = None) -> None:
    source = dem_layer.source()
    if os.path.isfile(source) or source.startswith("/vsi"):
        t = time.perf_counter()
        ds = gdal.Open(source)
        if ds is not None:
            _warn_if_bbox_exceeds_source(ds, bbox, source)
            ds = None
        if _try_gdal_warp(source, dest, bbox, resolution_m):
            _log(f"  GDAL warp: {time.perf_counter() - t:.1f}s")
            return
    _export_via_qgis(dem_layer, dest, bbox, resolution_m)


def _run_converter(exe: str, job_file: str) -> None:
    # Stream the converter's output into the log so the .abt creation step is
    # visible (merge stdout+stderr; the converter logs to both). Keep a tail
    # for the error message.
    lines: List[str] = []
    with subprocess.Popen(
        [exe, "ingest", "--job-file", job_file],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        creationflags=_SUBPROCESS_FLAGS,
    ) as proc:
        for line in iter(proc.stdout.readline, ""):
            line = line.rstrip()
            if line:
                _log(f"    {line}")
                lines.append(line)
    if proc.returncode != 0:
        tail = "\n".join(lines[-20:])
        raise RuntimeError(f"converter failed ({proc.returncode}):\n{tail}")


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
    # Log the resolved cache path so the user can inspect the .abt tiles
    # directly (e.g. to check for missing tiles that show up as empty stripes
    # through a line-of-sight). Set QgsSettings "aether/cache_dir" to relocate.
    _log(f"  .abt cache dir: {cache_dir}")

    if _cache_hit(cache_dir):
        _log(f"  cache HIT ({time.perf_counter() - t0:.1f}s)")
        return cache_dir

    # A local terrain directory is high-resolution data (swissALTI-style), so
    # tile it like prepare_data.py's Swiss branch (0.1/0.5 deg); an XYZ base
    # DEM uses 1 deg. This keeps .abt tiles identical to the reference pipeline.
    high_res = bool(terrain_dir)
    subtiles = _compute_subtiles(bbox, resolution_m, tx_lat, tx_lon,
                                  max_range_km, az_start, az_end, high_res)
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

    # Path 2/3: Extract a GeoTIFF PER sub-tile, then convert. Each .abt tile
    # gets its own base GeoTIFF clipped to just that tile's extent, instead of
    # one raster spanning the whole analysis area. The converter loads each
    # base whole-image (via the tiff crate, which caps the decode buffer), so a
    # single sector-wide raster blew past that limit and the converter silently
    # wrote terrain-less tiles -> empty coverage. Splitting per tile bounds
    # every decode to one tile, independent of the total range or resolution.
    temp_tifs: List[str] = []
    try:
        jobs = []
        for t in subtiles:
            res_deg = t["exact_res_m"] / 111_111.0
            # ~4 px halo so the converter's edge sampling has neighbours.
            margin = res_deg * 4.0
            sub_bbox = {
                "north": t["ul_lat"] + margin,
                "south": t["ul_lat"] - t["size_px"] * res_deg - margin,
                "west": t["ul_lon"] - margin,
                "east": t["ul_lon"] + t["size_px"] * res_deg + margin,
            }
            tile_tif = os.path.join(
                tempfile.gettempdir(), f"aether_{cache_hash}_{t['filename']}.tif"
            )
            t1 = time.perf_counter()
            if terrain_dir and os.path.isdir(terrain_dir):
                _extract_from_local_dir(terrain_dir, tile_tif, sub_bbox, resolution_m)
            else:
                _extract_reproject(dem_layer, tile_tif, sub_bbox, resolution_m,
                                   binary_manager)
            temp_tifs.append(tile_tif)
            _log(f"  extract {t['filename']}: {time.perf_counter() - t1:.1f}s, "
                 f"{os.path.getsize(tile_tif) / 1e6:.0f}MB")

            jobs.append({
                "output_path": os.path.abspath(os.path.join(cache_dir, t["filename"])),
                "format": "r16sint",
                "ul_lat": t["ul_lat"],
                "ul_lon": t["ul_lon"],
                "resolution_m": t["exact_res_m"],
                "size_px": t["size_px"],
                "base_tif": os.path.abspath(tile_tif),
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
        for tf in temp_tifs:
            if os.path.exists(tf):
                os.remove(tf)

    _log(f"  TOTAL: {time.perf_counter() - t0:.1f}s")
    return cache_dir
