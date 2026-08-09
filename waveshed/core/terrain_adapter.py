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
import shutil
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
        QgsMessageLog.logMessage(msg, "Waveshed-Terrain", Qgis.MessageLevel.Info)
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
    return QgsSettings().value("waveshed/cache_dir", os.path.expanduser("~/.aether/cache"))


# A cache entry directory is named by _cache_key, i.e. an md5 hex digest.
_CACHE_ENTRY_RE = re.compile(r"^[0-9a-f]{32}$")


def cache_entries() -> List[str]:
    """Absolute paths of the terrain cache entries under the cache root.

    Only md5-named directories — the shape ``_cache_key`` produces — count as
    ours. The cache root is user-configurable (QgsSettings
    "waveshed/cache_dir") and may well be a directory holding other things, so
    nothing else is ever reported here, and therefore nothing else can be
    deleted by ``clear_cache``.
    """
    root = get_cache_dir()
    entries: List[str] = []
    # The pool holds the tiles, the views hold links to them, and a bare md5
    # directly under the root is a per-request cache from an older plugin.
    for parent in (root,
                   os.path.join(root, _POOL_DIRNAME),
                   os.path.join(root, _VIEW_DIRNAME)):
        try:
            for name in sorted(os.listdir(parent)):
                path = os.path.join(parent, name)
                if _CACHE_ENTRY_RE.match(name) and os.path.isdir(path):
                    entries.append(path)
        except OSError:
            continue
    return entries


def dir_size_bytes(path: str) -> int:
    """Total size of *path* and everything under it. Unreadable files count 0."""
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def cache_size_bytes() -> int:
    """Total size on disk of every terrain cache entry."""
    return sum(dir_size_bytes(p) for p in cache_entries())


def clear_cache() -> Tuple[int, int]:
    """Delete every terrain cache entry. Returns ``(entries removed, bytes freed)``.

    The cache has no eviction of any kind, so this is the only way to reclaim
    the space — and the only way to pick up republished OpenFreeMap building
    data, whose cache token is deliberately date-free.
    """
    removed = 0
    freed = 0
    for path in cache_entries():
        size = dir_size_bytes(path)
        try:
            shutil.rmtree(path)
        except OSError as exc:  # noqa: PERF203 — report and keep going
            _log(f"  cache clear: could not remove {path} ({exc})")
            continue
        removed += 1
        freed += size
    _log(f"  cache clear: removed {removed} entr(ies), freed {freed / 1e6:.0f} MB")
    return removed, freed


def _get_download_connections() -> int:
    from qgis.core import QgsSettings
    return int(QgsSettings().value("waveshed/download_connections", 256))


def _get_download_max_passes() -> int:
    """Max download attempts (initial + retries) when tiles fail.

    Default 4: the first pass downloads everything; each retry re-fetches ONLY
    the sub-tiles that ended up with a gap, at half the connections, so retries
    are cheap (not a full re-download). Set to 1 via QgsSettings
    ("waveshed/download_max_passes") to disable retries.
    """
    from qgis.core import QgsSettings
    try:
        return max(1, int(QgsSettings().value("waveshed/download_max_passes", 4)))
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

# Opaque cache identity for the downloaded building source. It does not encode
# a snapshot date: OpenFreeMap republishes periodically, and pinning that here
# would invalidate every cached tile on each republish. Clear the terrain cache
# to pick up newer building data.
_OSM_BUILDINGS_TOKEN = "openfreemap:z14"


def source_fingerprint(buildings: str) -> str:
    """Stable identity for a data source, for use in a cache key.

    Path alone is not enough — editing the file in place must invalidate the
    cache — so mtime and size ride along.  For a directory (a set of .fgb
    parts, or a .gdb) the contents are aggregated, since the directory's own
    mtime does not change when a file inside it is rewritten.
    """
    # Not every buildings source is a file. A downloaded source (OpenFreeMap)
    # identifies itself with an opaque token, which is already stable — treat
    # anything that is not an existing path as one.
    if not os.path.exists(buildings):
        return buildings

    path = os.path.abspath(buildings)
    try:
        if os.path.isdir(path):
            parts = []
            for root, _, files in os.walk(path):
                for f in sorted(files):
                    fp = os.path.join(root, f)
                    try:
                        st = os.stat(fp)
                    except OSError:
                        continue
                    parts.append(f"{os.path.relpath(fp, path)}:"
                                 f"{int(st.st_mtime)}:{st.st_size}")
            return f"{path}|" + "|".join(parts)
        st = os.stat(path)
        return f"{path}|{int(st.st_mtime)}|{st.st_size}"
    except OSError:
        return path


# Bumped whenever tile geometry, provenance or cache layout changes, so tiles
# built by an older plugin can never be mistaken for current ones.
#   v2: tile extent is resolution-keyed (ABT_EXTENT_DEG) instead of a flat 1
#       degree, and the key is the tile set rather than the request bbox.
#   v3: tiles are pooled per tile; source identity is normalised; the XYZ zoom
#       is computed per tile instead of from the request's bbox centre.
_CACHE_SCHEMA = "v3"


def source_identity(source: str) -> str:
    """Stable identity for an elevation source, for the pool key.

    ``dem_layer.source()`` is a QGIS URI whose spelling depends on how the user
    happened to add the layer — parameter order, ``zmin``, ``interpretation``,
    percent-encoding.  Two spellings of the same Mapzen endpoint must not
    produce two pools and a full re-download of byte-identical data, so an XYZ
    URI is reduced to the parts that actually decide the pixels:

    * the tile URL template — which service the elevation comes from;
    * the encoding — how the PNG channels decode to metres;
    * ``zmax`` — the cap on the zoom a tile can be built from, which changes
      the sampled detail (see :func:`tile_zoom`).

    Anything else in the URI is presentation.  Non-XYZ sources (a file, a VRT,
    a local terrain directory) are already identified by their path.
    """
    if "type=xyz" not in source:
        return source
    params = dict(urllib.parse.parse_qsl(source))
    url = urllib.parse.unquote(params.get("url", ""))
    encoding = ("mapbox" if "mapbox" in params.get("interpretation", "").lower()
                else "terrarium")
    return f"xyz|{url}|{encoding}|zmax={params.get('zmax', '15')}"


def tile_zoom(lat: float, resolution_m: float, z_max: int) -> int:
    """XYZ zoom giving *resolution_m* ground sampling at *lat*, capped at z_max.

    At latitude θ a zoom-z tile pixel spans ``40075000*cos(θ)/(2^z * 256)``
    metres, so ``z = ceil(log2(40075000*cos(θ)/(res*256)))``.

    Take *lat* from the TILE, never from the request's bounding box: a tile
    shared by two runs whose bboxes are centred at different latitudes would
    otherwise be built from different source zooms, and its pixels would depend
    on which run happened to fetch it first.  Keyed on the tile, zoom is a pure
    function of (source, tile, resolution) and needs no term in the cache key.
    """
    cos_lat = max(math.cos(math.radians(lat)), 0.1)
    z_ideal = math.ceil(math.log2(40_075_000.0 * cos_lat / (resolution_m * 256)))
    return min(max(z_ideal, 0), z_max)


def _tile_center_lat(tile: Dict[str, Any]) -> float:
    """Latitude of a sub-tile's centre, from its own header parameters."""
    span_deg = tile["size_px"] * tile["exact_res_m"] / 111_111.0
    return tile["ul_lat"] - span_deg / 2.0


def _cache_key(source: str, subtiles: List[Dict[str, Any]], resolution_m: int,
               buildings: Optional[str] = None) -> str:
    """Hash identifying one .abt terrain cache.

    Keyed on the *tile set*, not on the request that produced it.  Tile names
    from ``_tile_params`` are globally canonical (snapped to the
    ``ABT_EXTENT_DEG`` grid), so two sites, two ranges or two azimuth sectors
    needing the same tiles share one cache.  Hashing the raw bbox floats made
    a sub-metre site nudge — or 30 km vs 31 km at one site — a guaranteed
    miss, and every run re-downloaded terrain it already had.

    The azimuth sector needs no term of its own: a narrower sector selects a
    strict subset of the tiles and therefore hashes differently, so a sector
    cache can never be mistaken for a full-circle one.

    Buildings are baked into the .abt pixels, so a cache built without them
    must never be reused for a run that wants them (or one that wants a
    different building set).
    """
    names = ",".join(sorted(t["filename"] for t in subtiles))
    key = f"{_CACHE_SCHEMA}|{source}|{names}|{resolution_m}"
    if buildings:
        key += f"|{source_fingerprint(buildings)}"
    return hashlib.md5(key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Tile pool
# ---------------------------------------------------------------------------
#
# Tiles live once, addressed by their own geography, in a per-source pool.
# Each run then gets a small "view" directory holding links to exactly the
# tiles it needs.
#
# The view is not an optimisation — it is required.  aether_core reads EVERY
# .abt in the directory it is handed: engines/coverage.rs builds
# `relevant_tiles` from the whole listing with no bbox filter, so pointing it
# at the pool would drag every tile ever built for that source into the
# terrain atlas and the output grid.  abt.list_tiles is flat for the same
# reason, and job_builder passes exactly one terrain_dir.
#
# Keying a whole directory on the request cannot express the common case,
# which is why the earlier per-directory schemes re-downloaded constantly:
# shrinking the range, nudging the site or narrowing the sector all yield a
# SUBSET of tiles that are already on disk.  Set-equality on a directory key
# calls that a miss and fetches every one of them again.  Membership is a
# per-tile question, so it is asked per tile.
_POOL_DIRNAME = "pool"
_VIEW_DIRNAME = "views"

# Sidecar next to a pooled tile that exists but must not be reused: written
# with a gap in it, or built without the buildings its pool identity claims.
# The engine never sees these — it is given the view, and list_tiles only
# matches .abt.
_REBUILD_SUFFIX = ".rebuild"


def _finish_view(pool_dir: str, view_dir: str, names: List[str],
                 t0: float) -> None:
    """Link this run's tiles into its view directory and log the outcome."""
    gone = _sync_view(pool_dir, view_dir, names)
    if gone:
        _log(f"  WARNING: {len(gone)} of {len(names)} tile(s) could not be "
             f"provided (first: {gone[0]}) — coverage will have holes there")
    _log(f"  TOTAL: {time.perf_counter() - t0:.1f}s")


def _pool_dir(source: str, buildings: Optional[str] = None) -> str:
    """Directory holding every tile built for this source (+ building set).

    Resolution is not part of the identity: ``_tile_params`` puts it in the
    filename, so tiles of different resolutions coexist without colliding.
    """
    key = f"{_CACHE_SCHEMA}|{source_identity(source)}"
    if buildings:
        key += f"|{source_fingerprint(buildings)}"
    return os.path.join(get_cache_dir(), _POOL_DIRNAME,
                        hashlib.md5(key.encode()).hexdigest())


def _tile_is_whole(path: str) -> bool:
    """True if *path* is a fully written .abt, by its own declared geometry.

    A tile from a killed run exists and has a valid header over missing rows;
    the engine would read it as real terrain and the coverage would come out
    with holes and no error.  Compare the declared size against the file.
    """
    from . import abt
    header = abt.read_header(path)
    if header is None:
        return False
    try:
        return os.path.getsize(path) >= abt.HEADER_SIZE + header.size * header.stride
    except OSError:
        return False


def _tile_ready(pool_dir: str, name: str) -> bool:
    """True if the pool holds a usable copy of tile *name*."""
    if os.path.exists(os.path.join(pool_dir, name + _REBUILD_SUFFIX)):
        return False
    return _tile_is_whole(os.path.join(pool_dir, name))


def _set_rebuild_flags(pool_dir: str, names: List[str],
                       bad: Optional[set] = None) -> None:
    """Flag *bad* tiles for rebuild and clear the flag on the rest of *names*."""
    bad = bad or set()
    for name in names:
        path = os.path.join(pool_dir, name + _REBUILD_SUFFIX)
        try:
            if name in bad:
                open(path, "w").close()
            elif os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def _link_pbf_view(pool_dir: str, view_dir: str, names: List[str]) -> None:
    """Link this run's building tiles out of the pool into its own directory.

    Tiles that 404 (no buildings there — normal over water) simply have no
    file, which is not an error.  Strangers are pruned because the converter
    decodes every file in the directory for every output tile it writes.
    """
    os.makedirs(view_dir, exist_ok=True)
    wanted = set(names)
    try:
        for stale in os.listdir(view_dir):
            if stale not in wanted:
                try:
                    os.remove(os.path.join(view_dir, stale))
                except OSError:
                    pass
    except OSError:
        pass
    for name in names:
        src = os.path.join(pool_dir, name)
        dst = os.path.join(view_dir, name)
        if not os.path.exists(src) or os.path.exists(dst):
            continue
        try:
            os.link(src, dst)
        except OSError:
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass


def _sync_view(pool_dir: str, view_dir: str, names: List[str]) -> List[str]:
    """Populate *view_dir* with exactly *names*, linked from the pool.

    Hardlinks cost nothing; where the filesystem refuses one (a different
    volume, FAT, or Windows without the privilege) we fall back to a copy.
    Returns the names that could not be provided.
    """
    os.makedirs(view_dir, exist_ok=True)
    wanted = set(names)

    # Anything else in the view would be read as terrain by the engine.
    try:
        for stale in os.listdir(view_dir):
            if stale.lower().endswith(".abt") and stale not in wanted:
                try:
                    os.remove(os.path.join(view_dir, stale))
                except OSError:
                    pass
    except OSError:
        pass

    unavailable = []
    for name in names:
        src = os.path.join(pool_dir, name)
        dst = os.path.join(view_dir, name)
        if _tile_is_whole(dst):
            continue
        if os.path.exists(dst):
            try:
                os.remove(dst)      # truncated leftover from a killed run
            except OSError:
                pass
        if not _tile_is_whole(src):
            unavailable.append(name)
            continue
        try:
            os.link(src, dst)
        except OSError:
            try:
                shutil.copy2(src, dst)
            except OSError:
                unavailable.append(name)
    return unavailable


# ---------------------------------------------------------------------------
# Sub-tile specs (resolution-keyed grid)
# ---------------------------------------------------------------------------

# .abt tile extent in degrees per resolution — the single source of truth for
# every terrain path in the plugin (this module and gui/map_converter_tab).
# The ladder is the one agreed in AETHER's
# "Design Documents/qgis_plugin/TODO.md" 3.5: finer resolutions get smaller
# tiles.  Extents must be non-decreasing with resolution.
#
# Tile extent is load-bearing for the engine, not just a disk-size convenience.
# aether_core's solver loads every tile a wedge touches into ONE contiguous
# terrain-atlas allocation and rejects the job when that exceeds
# 0.9 * min(gpu max_buffer_size, 4 GiB) ~= 3.86 GB (solver.rs `single_alloc_ok`
# against `SystemLimits::max_allocation_bytes`, engines/coverage.rs).  A 1 deg
# tile at 5 m is 22224 px -> 44544 B stride * 22224 rows = 0.99 GB, so a site
# whose bbox crosses both a parallel and a meridian needs four of them
# (3.96 GB) and the run is rejected — with a message blaming VRAM, which
# cannot help, because that ceiling does not scale with
# processing.max_vram_usage_gb.  The same layer at 0.25 deg is 62.6 MB.
ABT_EXTENT_DEG = {
    2: 0.1,
    5: 0.25,
    10: 0.25,
    30: 0.5,
    90: 1.0,
    250: 2.0,
}

# _tile_params still mirrors prepare_data.py's calc_size / exact_res / ul_lat
# block, so aether_core reads plugin-generated and prepare_data-generated .abt
# tiles identically. Only the *extent* ladder above differs — deliberately.
#
# u16 safety net: the .abt writer stores the row stride as a u16
# (aether_converter download.rs `stride as u16`); stride = size_px*2 aligned to
# 256 must stay < 65536, so size_px must stay under ~32000, else the tile wraps
# and shears the terrain into bands. We shrink the tile until it fits.
_ABT_MAX_SIZE_PX = 32000  # keeps aligned stride (size_px*2) below the u16 limit


def _extent_for_resolution(resolution_m: float) -> float:
    """``ABT_EXTENT_DEG`` as a "<=" ladder, for resolutions off the table.

    job_builder.VALID_RESOLUTIONS only offers the table's own keys, but the
    engine accepts any resolution >= 0.1 m, so an off-table value takes the
    next entry up (and anything coarser than the last entry takes its extent).
    """
    for res in sorted(ABT_EXTENT_DEG):
        if resolution_m <= res:
            return ABT_EXTENT_DEG[res]
    return ABT_EXTENT_DEG[max(ABT_EXTENT_DEG)]


def _subtile_degrees(resolution_m: int) -> float:
    """Sub-tile size in degrees for *resolution_m*, from ``ABT_EXTENT_DEG``.

    The extent is shrunk further should the .abt row stride overflow the u16
    header field.
    """
    sub = _extent_for_resolution(resolution_m)
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
) -> List[Dict[str, Any]]:
    tiles = []
    sub = _subtile_degrees(resolution_m)
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
        """Check if a tile intersects the circular sector."""
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


# Estimated .abt cache size (MB) at which a run is worth questioning before it
# commits the user to a very long download.  Chosen so a routine analysis never
# nags: 50 GB is already far past anything a normal range/resolution produces.
_SIZE_WARN_MB = 50 * 1024
_SIZE_STRONG_WARN_MB = 200 * 1024


def analysis_bbox(
    tx_lat: float,
    tx_lon: float,
    max_range_km: float,
    az_start: float = 0.0,
    az_end: float = 360.0,
) -> Dict[str, float]:
    """Public wrapper: the WGS84 bbox one analysis needs terrain for."""
    return _compute_sector_bbox(tx_lat, tx_lon, max_range_km, az_start, az_end)


def estimate_terrain_disk_mb(
    tx_lat: float,
    tx_lon: float,
    max_range_km: float,
    resolution_m: int,
    az_start: float = 0.0,
    az_end: float = 360.0,
) -> int:
    """Estimate the .abt terrain cache (MB) one analysis will generate.

    Runs the same bbox/sub-tile maths ``prepare_terrain`` will, so the GUI can
    warn *before* a run starts rather than after gigabytes have downloaded.
    """
    bbox = _compute_sector_bbox(tx_lat, tx_lon, max_range_km, az_start, az_end)
    subtiles = _compute_subtiles(bbox, resolution_m, tx_lat, tx_lon,
                                 max_range_km, az_start, az_end)
    return _estimate_abt_disk_mb(subtiles)


def buildings_identity(buildings_file: Optional[str] = None,
                       osm_buildings: bool = False) -> Optional[str]:
    """One identity covering both building sources, or None for plain terrain.

    Shared by ``prepare_terrain`` and the pre-run estimate so the GUI prices
    the same pool the run will actually use.
    """
    parts = []
    if buildings_file:
        parts.append(source_fingerprint(buildings_file))
    if osm_buildings:
        parts.append(_OSM_BUILDINGS_TOKEN)
    return "+".join(parts) if parts else None


def terrain_plan(
    source: str,
    tx_lat: float,
    tx_lon: float,
    max_range_km: float,
    resolution_m: int,
    az_start: float = 0.0,
    az_end: float = 360.0,
    buildings: Optional[str] = None,
) -> Dict[str, Any]:
    """What one analysis will actually have to build, given what is pooled.

    The pre-run warning used to price the whole tile set every time, so a run
    whose terrain was already on disk still announced tens of gigabytes of
    "download" and trained the user to click through it. Price only the tiles
    that are genuinely missing.
    """
    bbox = _compute_sector_bbox(tx_lat, tx_lon, max_range_km, az_start, az_end)
    subtiles = _compute_subtiles(bbox, resolution_m, tx_lat, tx_lon,
                                 max_range_km, az_start, az_end)
    pool = _pool_dir(source, buildings)
    missing = [t for t in subtiles if not _tile_ready(pool, t["filename"])]
    return {
        "bbox": bbox,
        "tiles_total": len(subtiles),
        "tiles_cached": len(subtiles) - len(missing),
        "tiles_missing": len(missing),
        "download_mb": _estimate_abt_disk_mb(missing),
        "total_mb": _estimate_abt_disk_mb(subtiles),
    }


def terrain_size_warning(disk_mb: int,
                         cached_mb: int = 0) -> Optional[Tuple[str, bool]]:
    """Return ``(message, strong)`` when *disk_mb* is big enough to confirm.

    *disk_mb* is what still has to be built; *cached_mb* is what is already
    pooled and is mentioned only so the number is not mistaken for the whole
    job.  ``strong`` is True past the 200 GB mark, where the caller should
    default the confirmation to "No".  Returns ``None`` for unremarkable sizes.
    """
    if disk_mb < _SIZE_WARN_MB:
        return None
    strong = disk_mb >= _SIZE_STRONG_WARN_MB
    gb = disk_mb / 1024.0
    msg = (
        f"This analysis still needs about {gb:,.0f} GB of terrain data.\n\n"
        "Downloading and converting it will take a long time and may fill "
        "the disk holding the terrain cache."
    )
    if cached_mb > 0:
        msg += (f"\n\n({cached_mb / 1024.0:,.0f} GB is already cached and will "
                "be reused.)")
    if strong:
        msg += (
            "\n\nThat is an extreme amount. Consider a coarser resolution or "
            "a shorter range before continuing."
        )
    else:
        msg += "\n\nA coarser resolution or shorter range would reduce it."
    msg += "\n\nContinue anyway?"
    return msg, strong


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


#: Printed by ``aether_converter download`` once per run when it has fused a
#: ``buildings_pbf_dir``. Its absence is how we detect an engine that predates
#: the feature — see ``_try_rust_download``.
_BUILDINGS_APPLIED_MARKER = "[Buildings]"


def _buildings_were_applied(lines: List[str]) -> bool:
    """True if the converter reported fusing buildings during this pass."""
    return any(_BUILDINGS_APPLIED_MARKER in ln for ln in lines)


def _binary_has_buildings_support(exe: str) -> Optional[bool]:
    """Whether *exe* was compiled with buildings-on-download, by inspection.

    The marker is a literal in the binary, so its presence is decidable without
    running anything. This exists to tell "the engine is too old" apart from
    "a different, older engine is first on the search path" — by far the more
    common cause, and indistinguishable from the log otherwise.

    Returns None if the file cannot be read.
    """
    try:
        with open(exe, "rb") as fh:
            blob = fh.read()
    except OSError:
        return None
    return _BUILDINGS_APPLIED_MARKER.encode() in blob


def _try_rust_download(
    source: str,
    cache_dir: str,
    bbox: Dict[str, float],
    resolution_m: int,
    subtiles: List[Dict[str, Any]],
    binary_manager: Any,
    pbf_dir: Optional[str] = None,
) -> bool:
    """Download XYZ tiles via Rust and produce .abt files directly.

    *pbf_dir*, when given, is handed to the converter as ``buildings_pbf_dir``
    so buildings are fused during the download instead of forcing the whole run
    onto the QGIS export + ingest path (which is 7-17x slower).

    That field is **additive**, so a converter predating it parses the job
    happily, ignores the field, and writes perfectly good building-*free*
    terrain — which would then be cached under a pool identity claiming it has
    buildings. To stop that, a pass that requested buildings and saw no
    ``[Buildings]`` line in the output is treated as a failed fast path: the
    tiles are flagged for rebuild and we fall back to the ingest path.
    """
    # Each of the three bail-outs below used to return False without a word,
    # and the caller printed one generic "Rust download unavailable" for all of
    # them — so a missing engine looked exactly like an unsupported layer, and
    # the run then spent the whole extract on the slow path before anything
    # mentioned it. Say which one it was.
    if "type=xyz" not in source:
        _log("  fast download skipped: the DEM layer is not an XYZ tile "
             "service (only XYZ can be fetched directly)")
        return False

    params = dict(urllib.parse.parse_qsl(source))
    url_raw = params.get("url", "")
    if not url_raw:
        _log(f"  fast download skipped: no 'url' in the layer source "
             f"({source[:120]})")
        return False

    url_template = urllib.parse.unquote(url_raw)

    encoding = "terrarium"
    if "mapbox" in params.get("interpretation", "").lower():
        encoding = "mapbox"

    # TODO(waveshed): the live XYZ download path has only been exercised against
    # Mapzen Global Terrain (Terrarium encoding). Test it end-to-end with other
    # elevation-tile services too — e.g. AWS Terrain Tiles, Mapbox Terrain-RGB,
    # and Nextzen — and verify encoding detection, {z}/{x}/{y} URL templating,
    # per-service zoom limits, and API-key/attribution handling. See TODO.md.

    # Zoom: at lat θ, ground resolution at zoom z is:
    #   res = 40075000 * cos(θ) / (2^z * 256)
    # So z = log2(40075000 * cos(θ) / (res * 256))
    # We need res <= resolution_m, so: z = ceil(log2(...))
    z_max = int(params.get("zmax", "15"))

    try:
        converter_exe = binary_manager.find_binary("aether_converter")
    except RuntimeError as exc:
        _log(f"  fast download skipped: {exc}")
        return False

    # Group by the zoom each tile needs for its OWN latitude. A single job-wide
    # zoom taken from the request's bbox centre made a tile's pixels depend on
    # which run fetched it first — two runs sharing a tile but centred at
    # different latitudes would build it from different source zooms. Grouped
    # this way, zoom is a pure function of (source, tile, resolution), so the
    # pool needs no zoom term to stay honest.
    by_zoom: Dict[int, List[Dict[str, Any]]] = {}
    for t in subtiles:
        spec = {
            "filename": t["filename"],
            "ul_lat": t["ul_lat"],
            "ul_lon": t["ul_lon"],
            "size_px": t["size_px"],
            "resolution_m": t["exact_res_m"],
        }
        z = tile_zoom(_tile_center_lat(t), t["exact_res_m"], z_max)
        by_zoom.setdefault(z, []).append(spec)

    # *cache_dir* is the tile pool: only the tiles this run is missing were
    # passed in, and they are written straight into it.
    all_names = [t["filename"] for t in subtiles]

    def _produced() -> bool:
        return any(_tile_is_whole(os.path.join(cache_dir, n)) for n in all_names)

    job_file = os.path.join(tempfile.gettempdir(), f"aether_dl_{os.getpid()}.json")

    # Set by _run_pass; read after the passes to detect an engine that silently
    # ignored buildings_pbf_dir. A list so the closure can write to it.
    buildings_seen: List[bool] = []

    def _run_pass(specs: List[Dict[str, Any]], conn: int, zoom: int):
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
        # Omitted entirely when unset, so an older converter sees exactly the
        # job shape it sees today.
        if pbf_dir:
            job["buildings_pbf_dir"] = os.path.abspath(pbf_dir)
        with open(job_file, "w") as fh:
            json.dump(job, fh)
        rc, lines = _run_converter_download_once(converter_exe, job_file)
        if pbf_dir and rc == 0:
            buildings_seen.append(_buildings_were_applied(lines))
        ok, total = _parse_download_completeness(lines)
        return rc, ok, total

    max_passes = _get_download_max_passes()
    base_conn = _get_download_connections()
    _log(f"  Rust download: {len(all_names)} output tiles (.abt) in "
         f"{len(by_zoom)} zoom group(s) {sorted(by_zoom)}, "
         f"encoding={encoding}, connections={base_conn}")

    # Capability gate, decided from the binary itself rather than from what it
    # prints. `buildings_pbf_dir` is an additive field, so an engine predating
    # it parses the job, ignores the field and writes building-*free* terrain
    # that would then be pooled under a buildings-keyed identity.
    #
    # This was first gated on the engine's `[Buildings]` output line, which
    # produced a false negative against an engine that demonstrably has the
    # feature — and a false negative here is expensive, since it sends the run
    # down the 7-17x slower export+ingest path. The marker is a literal in the
    # binary, so its presence is decidable by inspection, before anything runs.
    if pbf_dir:
        supported = _binary_has_buildings_support(converter_exe)
        if supported is False:
            _log("  this aether_converter predates buildings-on-download — "
                 "using the slower ingest path instead.")
            _log(f"    engine: {converter_exe}")
            # Be specific about which copy is stale. The engine lives in
            # several places and they are updated by different actions: the
            # pipeline's "Distribute" step publishes to AETHER_Web/bin and
            # MPT_SIGMA/aether/, and does NOT touch the directory the plugin
            # reads. A build can therefore be correctly distributed and still
            # be old here, which is not obvious from either side.
            _log("    This is the copy the plugin runs, and it is updated "
                 "separately from a 'Distribute' build. Refresh it with the "
                 "pipeline's \"Deploy engine to ~/.aether/bin (local test)\" "
                 "action, or with Settings → Download binaries.")
            return False
        if supported is None:
            _log(f"  could not read {converter_exe} to check for "
                 "buildings-on-download; assuming it is supported")
        _log("  buildings fused during download (buildings_pbf_dir)")

    ok_all = True
    for zoom in sorted(by_zoom):
        if not _download_zoom_group(
                cache_dir, by_zoom[zoom], zoom, base_conn, max_passes,
                _run_pass, _produced):
            ok_all = False

    # The engine's own report is only a warning here, never the gate — see the
    # note on the capability check above. A supporting engine that printed
    # nothing still produced buildings; a *non*-supporting one never got this
    # far, because the gate refused the fast path before any download ran.
    if pbf_dir and buildings_seen and not any(buildings_seen):
        _log("  note: no '[Buildings]' line in the converter output, but the "
             "engine has the feature — buildings were applied. Report this if "
             "the result is missing buildings.")

    return ok_all


def _download_zoom_group(
    cache_dir: str,
    tile_specs: List[Dict[str, Any]],
    zoom: int,
    base_conn: int,
    max_passes: int,
    _run_pass: Any,
    _produced: Any,
) -> bool:
    """Download one homogeneous-zoom group, with the halving retry."""
    all_names = [s["filename"] for s in tile_specs]
    conn = base_conn

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
            rc, ok, total = _run_pass(specs, conn, zoom)
            elapsed = time.perf_counter() - t
            if rc != 0:
                _log(f"  Rust download failed (exit {rc}) at z={zoom}")
                return False
            if total < 0:
                # Older converter without completeness stats — trust exit 0.
                _log(f"  Rust download z={zoom}: {elapsed:.1f}s")
                _set_rebuild_flags(cache_dir, all_names)
                return _produced()
            if ok >= total:
                _log(f"  Rust download z={zoom}: {elapsed:.1f}s, "
                     f"{ok}/{total} source tiles (XYZ) OK "
                     f"→ {len(specs)} output tile(s) @ {conn} conn")
                _set_rebuild_flags(cache_dir, all_names)
                return True

            missing = total - ok
            # Which sub-tiles need re-fetching? Ones with a hole in them, and
            # ones never written at all — _abt_has_gaps cannot see the latter,
            # since it reports False for a file it cannot open.
            affected = []
            for s in tile_specs:
                path = os.path.join(cache_dir, s["filename"])
                if not os.path.exists(path) or _abt_has_gaps(path):
                    affected.append(s)
            if not affected:
                # Source tiles were unavailable, but no output tile ended up
                # with a hole: the server simply has nothing there (ocean,
                # past zmax, a permanent 404), which no amount of retrying
                # changes. That is a COMPLETE result, and treating it as a
                # failure was a standing bug — a single missing source tile
                # out of hundreds of thousands marked the cache incomplete on
                # the first run, and since only this function ever clears the
                # marker, every later run re-downloaded the entire set and
                # then landed here again. Note the converter reports such a
                # run as "100.0% success" (see _TILES_OK_RE), so this is the
                # common case, not an edge case.
                _log(f"  Rust download: {elapsed:.1f}s, {missing} source "
                     f"tile(s) unavailable but every output tile is intact "
                     f"→ complete")
                _set_rebuild_flags(cache_dir, all_names)
                return True

            if attempt >= max_passes or conn <= _MIN_DOWNLOAD_CONNECTIONS:
                _log(f"  WARNING: {missing} source tile(s) still failed after "
                     f"{attempt} pass(es); {len(affected)} output tile(s) left "
                     f"with gaps — using partial terrain and flagging just "
                     f"those tiles for rebuild.")
                # Flag only the gapped tiles, so the next run re-fetches those
                # and keeps the rest. Usable-but-gapped terrain is still worth
                # returning: the caller would otherwise redo the whole
                # preparation through the far slower path in this same run.
                _set_rebuild_flags(cache_dir, all_names,
                                   {s["filename"] for s in affected})
                return _produced()

            conn = max(_MIN_DOWNLOAD_CONNECTIONS, conn // 2)
            specs = affected
            _log(f"  Rust download incomplete ({missing} source tile(s) "
                 f"missing): retrying {len(specs)} affected output tile(s) "
                 f"@ {conn} connections (pass {attempt + 1}/{max_passes})")
        _set_rebuild_flags(cache_dir, all_names,
                           {s["filename"] for s in specs})
        return _produced()
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


_TERRAIN_EXTS = (".tif", ".tiff", ".dem", ".hgt")


def list_terrain_files(terrain_dir: str) -> List[str]:
    """Return every terrain raster under *terrain_dir* (recursive)."""
    found: List[str] = []
    for root, _, files in os.walk(terrain_dir):
        for f in files:
            if f.lower().endswith(_TERRAIN_EXTS):
                found.append(os.path.join(root, f))
    return found


def list_abt_tiles(terrain_dir: str) -> List[str]:
    """Return the ``.abt`` tiles directly inside *terrain_dir*.

    Deliberately **not** recursive: the engine resolves terrain with a plain
    `read_dir` over the one directory it is given (`engines/coverage.rs`,
    `engines/p2p.rs`, `engines/cpu_coverage.rs`), so tiles in sub-directories
    are invisible to it. Reporting them here would promise coverage the run
    would not have.
    """
    try:
        return sorted(
            os.path.join(terrain_dir, f)
            for f in os.listdir(terrain_dir)
            if f.lower().endswith(".abt")
            and os.path.isfile(os.path.join(terrain_dir, f))
        )
    except OSError:
        return []


def is_abt_tile_dir(terrain_dir: Optional[str]) -> bool:
    """True if *terrain_dir* holds pre-built ``.abt`` tiles.

    A directory of `.abt` files is a finished engine input, not a source to
    convert — the two look alike in a folder picker but must not be conflated:
    handing `.abt` files to the converter as `base_tif` would fail, and running
    the conversion at all would waste the work the Map Converter already did.
    """
    return bool(terrain_dir) and bool(list_abt_tiles(terrain_dir))


def _dataset_wgs84_bbox(ds) -> Optional[Dict[str, float]]:
    """Return a GDAL dataset's WGS84 bounds, or None if GDAL can't report them."""
    info = gdal.Info(ds, format="json")
    ext = info.get("wgs84Extent") if isinstance(info, dict) else None
    if not ext:
        return None
    ring = ext["coordinates"][0]
    lons = [c[0] for c in ring]
    lats = [c[1] for c in ring]
    return {"west": min(lons), "east": max(lons),
            "south": min(lats), "north": max(lats)}


def bboxes_intersect(a: Dict[str, float], b: Dict[str, float]) -> bool:
    """True when two WGS84 bboxes overlap by more than a rounding epsilon."""
    m = 1e-9
    return not (a["east"] <= b["west"] + m or a["west"] >= b["east"] - m
                or a["north"] <= b["south"] + m or a["south"] >= b["north"] - m)


# Below this fraction of the analysis area, a local terrain source is worth
# interrupting the user for. Not 1.0: the analysis bbox carries a 1% margin
# (_compute_sector_bbox) and tiles rarely line up with a DEM edge exactly, so
# demanding total coverage would fire on every well-set-up run.
_COVERAGE_OK_FRACTION = 0.98


def bbox_covered_fraction(source: Dict[str, float],
                          needed: Dict[str, float]) -> float:
    """Fraction of *needed* that *source* covers, 0.0-1.0.

    Plain lat/lon area: the two boxes are always close together here, so the
    cos(lat) term cancels and would not change the reported percentage.
    """
    want = ((needed["north"] - needed["south"])
            * (needed["east"] - needed["west"]))
    if want <= 0:
        return 1.0
    dlat = min(source["north"], needed["north"]) - max(source["south"], needed["south"])
    dlon = min(source["east"], needed["east"]) - max(source["west"], needed["west"])
    if dlat <= 0 or dlon <= 0:
        return 0.0
    return min(1.0, (dlat * dlon) / want)


#: A pre-built tile set this much coarser than the requested resolution is
#: worth stopping for. Slack enough to ignore the exact-resolution rounding a
#: tile carries (29.99757 m for a nominal 30 m tile), tight enough that 30 m
#: tiles cannot pass for a 5 m request.
_ABT_RES_TOLERANCE = 1.25


def _abt_resolution_warning(headers: List[Any],
                            resolution_m: Optional[float]) -> Optional[str]:
    """Warn when pre-built tiles are coarser than the analysis asks for.

    The engine picks the best-fit tile per output cell, so a mixed set is fine
    and a *finer* set is fine — it just downsamples. Coarser is not: the run
    upsamples the coarse data and reports it at the requested resolution, which
    looks like detail that was never measured.
    """
    if not resolution_m or not headers:
        return None
    # Tile resolution in metres, from degrees-per-pixel.
    res = sorted(h.pixel_res * 111_111.0 for h in headers)
    finest = res[0]
    if finest <= resolution_m * _ABT_RES_TOLERANCE:
        return None
    spread = (f"{finest:.0f} m" if len(set(round(r) for r in res)) == 1
              else f"{finest:.0f}–{res[-1]:.0f} m")
    return (
        f"The pre-built tiles are {spread}, but the analysis is set to "
        f"{resolution_m:g} m.\n\nNo finer terrain exists in that folder, so "
        f"the result would be computed from {finest:.0f} m data and presented "
        f"at {resolution_m:g} m — detail that was never measured.\n\n"
        f"Set the resolution to {finest:.0f} m, or build finer tiles."
    )


def _abt_coverage_warning(terrain_dir: str, paths: List[str],
                          bbox: Dict[str, float],
                          resolution_m: Optional[float] = None,
                          osm_buildings: bool = False,
                          buildings_file: Optional[str] = None,
                          ) -> Optional[str]:
    """Check pre-built ``.abt`` tiles suit this analysis, by their own headers.

    Same contract as the raster check: warn on anything that would silently
    produce fiction. Three failure modes are specific to `.abt` — a tile whose
    header cannot be read (the engine will skip it), a set that is coarser than
    the requested resolution, and buildings that cannot be added because they
    are already burned into the pixels.
    """
    from . import abt

    headers = []
    unreadable = []
    for p in paths:
        h = abt.read_header(p)
        if h is None:
            unreadable.append(os.path.basename(p))
        else:
            headers.append(h)

    if not headers:
        return (
            f"None of the {len(paths)} .abt file(s) in:\n{terrain_dir}\n\n"
            "could be read as terrain tiles. They may be truncated or not "
            "actually .abt files."
        )

    south = min(h.ul_lat - h.size * h.pixel_res for h in headers)
    north = max(h.ul_lat for h in headers)
    west = min(h.ul_lon for h in headers)
    east = max(h.ul_lon + h.size * h.pixel_res for h in headers)
    src = {"north": north, "south": south, "east": east, "west": west}

    extents = (
        f"Tiles cover ~ N{north:.3f} S{south:.3f} E{east:.3f} W{west:.3f}\n"
        f"Analysis needs ~ N{bbox['north']:.3f} S{bbox['south']:.3f} "
        f"E{bbox['east']:.3f} W{bbox['west']:.3f}"
    )
    frac = bbox_covered_fraction(src, bbox)
    if frac <= 0.0:
        return (
            f"The {len(headers)} .abt tile(s) in:\n{terrain_dir}\n\ndo not "
            f"overlap the analysis area at all.\n\n{extents}"
        )
    if frac < _COVERAGE_OK_FRACTION:
        return (
            f"The .abt tiles in:\n{terrain_dir}\n\ncover only about "
            f"{frac * 100:.0f}% of the analysis area. The rest has no terrain "
            f"and will be computed as flat 0 m sea level.\n\n{extents}"
        )

    res_msg = _abt_resolution_warning(headers, resolution_m)
    if res_msg:
        return res_msg

    if osm_buildings or buildings_file:
        # Buildings are burned into .abt pixels when the tile is built, so a
        # pre-built set either already has them or cannot get them. Silently
        # ignoring the checkbox is the failure this exists to prevent.
        return (
            "Buildings were requested, but pre-built .abt tiles are used "
            f"exactly as they are:\n{terrain_dir}\n\nBuilding heights are "
            "baked into a tile when it is built, so they cannot be added now. "
            "The result will include buildings only if these tiles were built "
            "with them.\n\nUse a downloaded/converted terrain source instead "
            "if you need buildings applied for this run."
        )

    if unreadable:
        return (
            f"{len(unreadable)} file(s) in:\n{terrain_dir}\n\ncould not be "
            f"read as .abt tiles and will be ignored by the engine "
            f"(first: {unreadable[0]}). Coverage there will be missing."
        )
    return None


def terrain_coverage_warning(terrain_dir: str,
                             bbox: Dict[str, float],
                             resolution_m: Optional[float] = None,
                             osm_buildings: bool = False,
                             buildings_file: Optional[str] = None,
                             ) -> Optional[str]:
    """Warn when *terrain_dir* cannot serve this analysis.

    *resolution_m* and the buildings flags are only consulted for pre-built
    `.abt` directories, where the tiles are finished output and neither can be
    changed by the run — a raster source is converted at whatever resolution is
    asked for, and can have buildings burned in on the way.

    Fires for the unambiguous mistakes — an empty or unreadable directory, or
    one whose data lies somewhere else entirely — and also when the terrain
    covers only part of the analysis area.  Partial coverage is not benign:
    once a terrain directory is selected it is the *only* source
    (``_extract_from_local_dir``), with no fall back to the base DEM, so
    everything past its edge is built as 0 m and the coverage computed there
    is fiction.  A small tolerance keeps a one-pixel overhang quiet.
    """
    try:
        if not os.path.isdir(terrain_dir):
            return (
                f"The terrain directory does not exist:\n{terrain_dir}\n\n"
                "Check the path in Settings."
            )

        # Pre-built .abt tiles are checked by their own headers, not by GDAL —
        # GDAL cannot open a .abt at all, so the mosaic path below would report
        # a directory of perfectly good tiles as unreadable terrain.
        abts = list_abt_tiles(terrain_dir)
        if abts:
            return _abt_coverage_warning(terrain_dir, abts, bbox,
                                         resolution_m, osm_buildings,
                                         buildings_file)

        tifs = list_terrain_files(terrain_dir)
        if not tifs:
            # Mention .abt too: a user who points this at Map Converter output
            # nested one level down gets told what is actually wrong.
            return (
                f"No terrain files were found in:\n{terrain_dir}\n\n"
                "Expected GeoTIFF (.tif/.tiff), .dem or .hgt files (searched "
                "recursively), or pre-built .abt tiles directly in this "
                "folder (not in sub-folders — the engine does not recurse)."
            )

        vrt = gdal.BuildVRT("", tifs)
        if vrt is None:
            return (
                f"The terrain files in:\n{terrain_dir}\n\ncould not be read as "
                "a single mosaic. They may have mixed projections or be "
                "corrupt."
            )
        src = _dataset_wgs84_bbox(vrt)
        vrt = None
        if src is None:
            return None

        extents = (
            f"Terrain covers ~ N{src['north']:.3f} S{src['south']:.3f} "
            f"E{src['east']:.3f} W{src['west']:.3f}\n"
            f"Analysis needs  ~ N{bbox['north']:.3f} S{bbox['south']:.3f} "
            f"E{bbox['east']:.3f} W{bbox['west']:.3f}\n\n"
        )
        if not bboxes_intersect(src, bbox):
            return (
                f"The terrain data in:\n{terrain_dir}\n\ndoes not cover the "
                "analysis area at all.\n\n" + extents +
                "The whole area would be treated as 0 m (sea level), so the "
                "result would be meaningless."
            )

        covered = bbox_covered_fraction(src, bbox)
        if covered >= _COVERAGE_OK_FRACTION:
            return None
        return (
            f"The terrain data in:\n{terrain_dir}\n\nreaches only about "
            f"{covered * 100:.0f}% of the analysis area.\n\n" + extents +
            "Once a terrain directory is selected it is the only source — "
            "there is no fall back to the base DEM — so the remaining "
            f"{(1 - covered) * 100:.0f}% is built as 0 m (sea level) and any "
            "coverage shown there is not real.\n\n"
            "Either shorten the range to what the terrain covers, or add the "
            "missing tiles to the directory."
        )
    except Exception as exc:  # noqa: BLE001 — a pre-flight check must not block.
        _log(f"  (terrain-coverage check skipped: {exc})")
        return None


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
        src = _dataset_wgs84_bbox(ds)
        if src is None:
            return
        src_w, src_e = src["west"], src["east"]
        src_s, src_n = src["south"], src["north"]
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
    tifs = list_terrain_files(terrain_dir)
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
                       resolution_m: int) -> None:
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
    buildings_file: Optional[str] = None,
    osm_buildings: bool = False,
) -> str:
    """Produce .abt terrain tiles for aether_core. Returns cache directory.

    Buildings can come from *buildings_file* (FlatGeobuf, absolute roof
    elevations) and/or *osm_buildings* (OpenFreeMap vector tiles, heights above
    ground). Either way they are burned into the terrain surface, so the choice
    is part of the cache identity.
    """
    t0 = time.perf_counter()
    source = terrain_dir or dem_layer.source()
    _log(f"prepare_terrain: resolution={resolution_m}m, range={max_range_km}km, "
         f"az={az_start:.1f}-{az_end:.1f}"
         + (f", buildings={os.path.basename(buildings_file)}"
            if buildings_file else "")
         + (", buildings=openfreemap" if osm_buildings else ""))

    # A directory of .abt tiles is already an engine input — hand it over
    # untouched. Everything below this point exists to *produce* .abt tiles, so
    # running any of it would rebuild what the Map Converter already built (and
    # the converter would reject a .abt as its `base_tif` anyway).
    if is_abt_tile_dir(terrain_dir):
        tiles = list_abt_tiles(terrain_dir)
        _log(f"  using {len(tiles)} pre-built .abt tile(s) from {terrain_dir} "
             f"— no download or conversion")
        # Re-run the same checks the GUI shows as a dialog. A Processing run
        # never sees that dialog, and this path skips every other safeguard in
        # this function, so anything wrong with the tiles has to be said here
        # or the run produces a confident, wrong answer with no trace.
        problem = _abt_coverage_warning(
            terrain_dir, tiles,
            _compute_sector_bbox(tx_lat, tx_lon, max_range_km, az_start, az_end),
            resolution_m, osm_buildings, buildings_file,
        )
        if problem:
            for line in problem.splitlines():
                if line.strip():
                    _log(f"  WARNING: {line.strip()}")
        return terrain_dir

    bbox = _compute_sector_bbox(tx_lat, tx_lon, max_range_km, az_start, az_end)
    subtiles = _compute_subtiles(bbox, resolution_m, tx_lat, tx_lon,
                                 max_range_km, az_start, az_end)
    expected = [t["filename"] for t in subtiles]

    # One identity covering both sources, so switching either one — or turning
    # buildings off — lands on a different cache entry.
    parts = []
    if buildings_file:
        parts.append(source_fingerprint(buildings_file))
    if osm_buildings:
        parts.append(_OSM_BUILDINGS_TOKEN)
    buildings_id = "+".join(parts) if parts else None

    # The pool holds tiles for this source; the view is what the engine reads.
    pool_dir = _pool_dir(source, buildings_id)
    view_dir = os.path.join(get_cache_dir(), _VIEW_DIRNAME,
                            _cache_key(source, subtiles, resolution_m,
                                       buildings_id))
    os.makedirs(pool_dir, exist_ok=True)
    # Log the resolved paths so the user can inspect the .abt tiles directly
    # (e.g. to check for missing tiles that show up as empty stripes through a
    # line-of-sight). Set QgsSettings "waveshed/cache_dir" to relocate.
    _log(f"  tile pool: {pool_dir}")
    _log(f"  .abt view dir: {view_dir}")

    # Membership is per tile, so a smaller range, a nudged site or a narrower
    # sector fetches nothing: every tile it needs is already pooled.
    todo = [t for t in subtiles if not _tile_ready(pool_dir, t["filename"])]
    if not todo:
        gone = _sync_view(pool_dir, view_dir, expected)
        if not gone:
            _log(f"  cache HIT — {len(expected)} tile(s) from the pool "
                 f"({time.perf_counter() - t0:.1f}s)")
            return view_dir
        _log(f"  {len(gone)} pooled tile(s) vanished between check and link; "
             f"rebuilding those")
        todo = [t for t in subtiles if t["filename"] in set(gone)]

    # Building anything at all needs the converter, so check before the work
    # rather than after it. Both paths call it, and the fast one falls back
    # silently when it is absent — without this the user pays the entire slow
    # extract (tens of minutes) only for the run to then fail on the missing
    # engine. A full cache hit above needs no binary and never reaches here.
    try:
        binary_manager.find_binary("aether_converter")
    except RuntimeError as exc:
        raise RuntimeError(
            f"{exc}\n\nTerrain cannot be prepared without the Aether engine."
        ) from exc

    disk_mb = _estimate_abt_disk_mb(todo)
    # "output tiles" = the .abt files we write. Kept distinct from the "source
    # tiles" (XYZ web tiles) counted during the download below — the two counts
    # differ by orders of magnitude and used to be indistinguishable in the log.
    _log(f"  cache MISS — {len(todo)} of {len(subtiles)} output tiles (.abt) "
         f"need building, ~{disk_mb} MB on disk, "
         f"bbox N={bbox['north']:.3f} S={bbox['south']:.3f} "
         f"E={bbox['east']:.3f} W={bbox['west']:.3f}")

    # Fetch building tiles before the converter runs, so they are on disk when
    # the ingest jobs reference them.
    pbf_dir: Optional[str] = None
    buildings_missing = False
    if osm_buildings:
        # Building tiles pool alongside the terrain tiles — they are globally
        # addressed (z_x_y.pbf), so 30 km -> 31 km reuses every one of them
        # instead of re-fetching 1369 tiles.  The converter, though, is handed
        # a per-run view: ingest.rs re-scans the whole buildings directory for
        # every output tile it writes, so pointing it at a pool that grows
        # across runs would make each run slower than the last.
        pbf_pool = os.path.join(pool_dir, "_buildings")
        pbf_dir = os.path.join(view_dir, "_buildings")
        try:
            from . import openfreemap
            openfreemap.download_building_tiles(bbox, pbf_pool, log=_log)
            _link_pbf_view(pbf_pool, pbf_dir, [
                openfreemap.tile_filename(*t)
                for t in openfreemap.tiles_for_bbox(bbox)
            ])
        except Exception as exc:  # noqa: BLE001
            # Terrain without buildings beats no terrain at all, but the user
            # must know the result is not what they asked for.
            _log(f"  WARNING: building download failed ({exc}); "
                 "continuing without buildings")
            pbf_dir = None
            # The cache identity says these tiles carry buildings. They do not,
            # so the directory must not be reused — otherwise the next run
            # hits the cache, skips the download, and silently produces
            # building-free terrain with no warning at all.
            buildings_missing = True

    # Path 1: Rust downloader (XYZ → .abt directly, no intermediate file).
    #
    # OpenFreeMap buildings ride along on this path now — the downloader takes a
    # `buildings_pbf_dir` and fuses them onto the finished tiles. This used to
    # divert every buildings run onto the QGIS export + ingest path, which
    # measured 7-17x slower (a 50 km run spent ~1560 s of 1687 s inside
    # `writeRaster`) and was the single reason buildings were unusable.
    #
    # A FlatGeobuf `buildings_file` still has to divert: it is an IngestJob
    # field only, and the downloader has no equivalent.
    if buildings_file and not terrain_dir and "type=xyz" in dem_layer.source():
        _log("  building file requested — using the converter ingest path "
             "(the downloader fuses OpenFreeMap tiles only, not FlatGeobuf)")
    elif not terrain_dir and "type=xyz" in dem_layer.source():
        if _try_rust_download(dem_layer.source(), pool_dir, bbox,
                              resolution_m, todo, binary_manager,
                              pbf_dir=pbf_dir):
            # Reached either with buildings fused in (pbf_dir set) or with no
            # buildings at all. In the second case the pool identity may still
            # claim buildings — a failed OpenFreeMap download clears pbf_dir
            # but not the identity — so those tiles must not be reused.
            if buildings_missing:
                _set_rebuild_flags(pool_dir, [t["filename"] for t in todo],
                                   {t["filename"] for t in todo})
                _log("  buildings were requested but unavailable — tiles "
                     "flagged for rebuild so the next run retries them")
            _finish_view(pool_dir, view_dir, expected, t0)
            return view_dir
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
        for t in todo:
            res_deg = t["exact_res_m"] / 111_111.0
            # ~4 px halo so the converter's edge sampling has neighbours.
            margin = res_deg * 4.0
            sub_bbox = {
                "north": t["ul_lat"] + margin,
                "south": t["ul_lat"] - t["size_px"] * res_deg - margin,
                "west": t["ul_lon"] - margin,
                "east": t["ul_lon"] + t["size_px"] * res_deg + margin,
            }
            # Disambiguate by pool identity AND process. Tile names are
            # globally canonical (position + resolution only), so the same
            # name means different pixels for a different source or building
            # set — and two runs of the *same* source (Site Analysis and Map
            # Converter together, or two windows) would otherwise read and
            # write one shared scratch file, with the `finally` below deleting
            # it out from under whichever is still using it.
            # NB: the pid makes this per-run, which is correct only while this
            # stays scratch. Caching the extract between runs (an open TODO)
            # means dropping the pid *and* the `finally`, not just the latter.
            tile_tif = os.path.join(
                tempfile.gettempdir(),
                f"aether_{os.path.basename(pool_dir)}_{os.getpid()}_{t['filename']}.tif",
            )
            t1 = time.perf_counter()
            if terrain_dir and os.path.isdir(terrain_dir):
                _extract_from_local_dir(terrain_dir, tile_tif, sub_bbox, resolution_m)
            else:
                _extract_reproject(dem_layer, tile_tif, sub_bbox, resolution_m)
            temp_tifs.append(tile_tif)
            _log(f"  extract {t['filename']}: {time.perf_counter() - t1:.1f}s, "
                 f"{os.path.getsize(tile_tif) / 1e6:.0f}MB")

            job = {
                "output_path": os.path.abspath(os.path.join(pool_dir, t["filename"])),
                "format": "r16sint",
                "ul_lat": t["ul_lat"],
                "ul_lon": t["ul_lon"],
                "resolution_m": t["exact_res_m"],
                "size_px": t["size_px"],
                "base_tif": os.path.abspath(tile_tif),
                "swiss_tifs": [],
            }
            # Omitted entirely when unset — MPT_SIGMA and older converters must
            # keep seeing exactly the job shape they do today.
            if buildings_file:
                job["buildings_file"] = os.path.abspath(buildings_file)
            if pbf_dir:
                job["buildings_pbf_dir"] = os.path.abspath(pbf_dir)
            jobs.append(job)

        job_file = os.path.join(view_dir, "batch_job.json")
        os.makedirs(view_dir, exist_ok=True)
        with open(job_file, "w") as fh:
            json.dump(jobs, fh)

        t2 = time.perf_counter()
        exe = binary_manager.find_binary("aether_converter")
        _run_converter(exe, job_file)
        _log(f"  converter: {time.perf_counter() - t2:.1f}s ({len(jobs)} tiles)")

        # The converter can exit 0 having skipped a tile it could not build,
        # so flag whatever did not land whole. Buildings that were requested
        # but unavailable flag everything: the pool identity claims buildings
        # these pixels do not carry.
        built = [t["filename"] for t in todo]
        bad = {n for n in built
               if not _tile_is_whole(os.path.join(pool_dir, n))}
        if bad:
            _log(f"  WARNING: {len(built) - len(bad)}/{len(built)} tiles "
                 f"written whole; {len(bad)} short or missing "
                 f"(first: {sorted(bad)[0]}) — flagged for rebuild")
        if buildings_missing:
            _log("  buildings were requested but unavailable — tiles flagged "
                 "for rebuild so the next run retries them")
            bad = set(built)
        _set_rebuild_flags(pool_dir, built, bad)
    finally:
        for tf in temp_tifs:
            if os.path.exists(tf):
                os.remove(tf)

    _finish_view(pool_dir, view_dir, expected, t0)
    return view_dir
