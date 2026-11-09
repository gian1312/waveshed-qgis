"""Terrain adapter — converts any QGIS raster layer to .abt tiles for aether_core.

Three extraction paths (fastest first):
  1. Rust downloader: XYZ tiles → .abt directly (parallel HTTP, no intermediate)
  2. Direct sources: local GeoTIFF/DEM files are handed to
     ``aether_converter ingest`` as ``sources[]`` entries in their OWN CRS —
     the converter reprojects while sampling, so the plugin never warps.
     Formats its tiff reader cannot open (.hgt/.dem, VRT, /vsi*, oversized
     files) are window-copied to plain GeoTIFF at native resolution first
     (see :func:`materialize_for_converter`).
  3. QGIS writeRaster: non-file providers (WMS, WMTS, ArcGIS, …) → temp
     WGS84 GeoTIFF (server-rendered; unavoidable) → converter → .abt

Also supports a local terrain directory of GeoTIFF/DEM files (path 2).
"""

from __future__ import annotations

import contextlib
import errno
import heapq
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.parse
from typing import (Any, Callable, Dict, Iterable, List, NamedTuple, Optional,
                    Sequence, Tuple)

from osgeo import gdal

from . import abt_index, hashes, run_progress, terrain_index
from .job_builder import VALID_RESOLUTIONS
from .layer_utils import classify_raster_layer


def _is_windows() -> bool:
    """THE Windows gate of the plugin: ``sys.platform`` (also
    ``gui/terrain_folder_scan._is_windows``), so a test that flips
    ``sys.platform`` reaches every Windows path it gates."""
    return sys.platform == "win32"


#: ``creationflags`` value that starts a console program without a console
#: window (Windows' CREATE_NO_WINDOW; the constant exists on Windows only).
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _no_window_flags() -> int:
    """``creationflags`` of a launch from QGIS: CREATE_NO_WINDOW on Windows
    (else a console window flashes up for every engine start), 0 elsewhere
    (POSIX ``Popen`` refuses anything else)."""
    return _CREATE_NO_WINDOW if _is_windows() else 0


#: ``creationflags`` of every engine launch — evaluated once, at import, so a
#: test that flips ``sys.platform`` never hands a POSIX ``Popen`` a Windows
#: flag.
_SUBPROCESS_FLAGS = _no_window_flags()


def _log(msg: str) -> None:
    with contextlib.suppress(Exception):
        from qgis.core import Qgis, QgsMessageLog
        QgsMessageLog.logMessage(msg, "Waveshed-Terrain", Qgis.MessageLevel.Info)


# DEM sources already warned about this run (requested range exceeds the DEM
# extent). Keeps the "assuming 0 m past the border" notice to once per analysis
# run instead of once per site/height. Cleared by reset_terrain_warnings().
_warned_extent_sources: set = set()

# Rendered sources already warned about this run (the server answered with
# empty blocks INSIDE its own published extent — see
# _warn_rendered_export_gap). Per source, not per tile: one partial answer
# usually affects several tiles of the same run, and the user needs the fact
# once. Cleared by reset_terrain_warnings().
_warned_render_gap_sources: set = set()


def reset_terrain_warnings() -> None:
    """Reset per-run terrain warnings. Call once when an analysis run starts so
    the 'range exceeds DEM extent' warning is shown once, not per job."""
    _warned_extent_sources.clear()
    _warned_render_gap_sources.clear()
    # A run sees every folder change made before it started: the folder
    # indexes are re-checked (stat) once per run, then shared by its jobs.
    # (A Site Analysis run whose TerrainPrep is current made that check at
    # the Run click and reads no index after this; a stale one's read
    # re-checks because of it.)
    terrain_index.expire_all()


def get_cache_dir() -> str:
    """Absolute path of the terrain cache root.

    Every pool and view directory is built by joining onto this, and those
    paths are handed to an engine subprocess that resolves them itself. A
    QgsSettings value that is blank, ``~``-relative or plain relative would
    therefore make the whole cache identity depend on the process working
    directory — which QGIS moves (file dialogs, Processing) — so the same
    identity computed twice in one run could name two different directories
    and the converter would die on the one that was never created. Normalised
    here, once, so that cannot happen.
    """
    from qgis.core import QgsSettings
    default = os.path.expanduser("~/.aether/cache")
    raw = QgsSettings().value("waveshed/cache_dir", default)
    root = str(raw).strip() if raw is not None else ""
    return os.path.abspath(os.path.expanduser(root or default))


# A cache entry directory is named by _cache_key, i.e. an md5 hex digest.
_CACHE_ENTRY_RE = re.compile(r"^[0-9a-f]{32}$")

#: The raw source-tile cache under the cache root (``<cache>/xyz``): every
#: XYZ download job names it as ``cache_dir`` (toolkit CONTRACT changelog
#: 38, aether_converter >= 0.2.8; older converters ignore the field). The
#: converter owns its layout — ``<service_key>/z<zoom>/x<x>_y<yblock>.pack``
#: packs of the fetched PNG/WebP bytes plus ``<service_key>/service.json`` —
#: and the plugin never reads it; it only creates the directory, prices its
#: growth (:func:`raw_tile_bytes`) and deletes it with the cache.
XYZ_CACHE_DIRNAME = "xyz"


def xyz_cache_dir() -> str:
    """Absolute path of the raw source-tile cache, ``<cache>/xyz``."""
    return os.path.join(get_cache_dir(), XYZ_CACHE_DIRNAME)


def cache_entries() -> List[str]:
    """Absolute paths of the terrain cache entries under the cache root.

    Only md5-named directories — the shape ``_cache_key`` produces — count as
    ours, plus exactly one more: the raw source-tile cache ``<cache>/xyz``
    (:func:`xyz_cache_dir`, last), when it exists. The cache root is
    user-configurable (QgsSettings "waveshed/cache_dir") and may well be a
    directory holding other things, so nothing else is ever reported here,
    and therefore nothing else can be deleted by ``clear_cache``.
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
    raw = os.path.join(root, XYZ_CACHE_DIRNAME)
    if os.path.isdir(raw):
        entries.append(raw)
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
    """Delete every terrain cache entry (:func:`cache_entries` — the cached
    terrain sets and the downloaded source tiles, ``<cache>/xyz``). Returns
    ``(entries removed, bytes freed)``.

    The cache has no eviction of any kind, so this is the only way to reclaim
    the space — and the only way to pick up republished OpenFreeMap building
    data, whose cache token is deliberately date-free (and, with the source
    tiles gone, republished elevation tiles). The folder index
    (``<cache>/index``, per-folder file metadata) is NOT terrain and is kept:
    :func:`clear_index` removes it.
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


#: A folder index file: ``<md5(folder)>.json`` (``terrain_index.index_path``)
#: or a pre-built folder's header index ``abt-<md5(folder)>.json``
#: (``abt_index.index_path``).
_INDEX_FILE_RE = re.compile(r"^(?:abt-)?[0-9a-f]{32}\.json$")


def index_files() -> List[str]:
    """Absolute paths of the folder-index files (``<cache>/index/<md5>.json``,
    see ``terrain_index``, and the ``.abt`` header indexes
    ``abt-<md5>.json``, see ``abt_index``). Only those names count — the
    cache root may hold other things (see :func:`cache_entries`)."""
    root = terrain_index.index_dir()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    return [os.path.join(root, n) for n in names
            if _INDEX_FILE_RE.match(n) and os.path.isfile(os.path.join(root, n))]


def clear_index() -> Tuple[int, int]:
    """Delete every folder index (:func:`index_files`) and drop the indexes
    held in memory, so the next scan of each folder reads every file again.
    Returns ``(files removed, bytes freed)``. (A scan running at that moment
    writes its own folder's index again when it finishes.)"""
    removed = 0
    freed = 0
    for path in index_files():
        try:
            size = os.path.getsize(path)
            os.remove(path)
        except OSError as exc:  # noqa: PERF203 — report and keep going
            _log(f"  index clear: could not remove {path} ({exc})")
            continue
        removed += 1
        freed += size
    terrain_index.forget_indexes()
    abt_index.forget()
    _log(f"  index clear: removed {removed} folder index file(s), "
         f"freed {freed / 1e6:.0f} MB")
    return removed, freed


def _get_download_connections() -> int:
    from qgis.core import QgsSettings
    return int(QgsSettings().value("waveshed/download_connections", 256))


def _get_download_max_passes() -> int:
    """Max download attempts (initial + retries) when tiles fail.

    Default 4: the first pass downloads everything; each retry re-fetches ONLY
    the output tiles that ended up with a gap (whole), at the configured
    connections — the converter backs off by itself, so the plugin does not
    stack a second backoff on top. Degraded-only source tiles get ONE repair
    pass instead (``repair_source_tiles``, converter >= 0.2.6) that patches
    just those source tiles into the existing output tiles. Set to 1 via
    QgsSettings ("waveshed/download_max_passes") to disable retries.
    """
    from qgis.core import QgsSettings
    try:
        return max(1, int(QgsSettings().value("waveshed/download_max_passes", 4)))
    except (TypeError, ValueError):
        return 4


# Matches the converter's authoritative completeness line, e.g.
#   [Stats] Tiles: 697340/697343 OK (100.0% success)
_TILES_OK_RE = re.compile(r"\[Stats\]\s*Tiles:\s*(\d+)\s*/\s*(\d+)\s*OK")

# Matches the converter's no-data tally (CONTRACT changelog item 17), e.g.
#   [Stats] NO-DATA (HTTP 404): 43 of 49 tile(s) — the service has no data there
# A 404 is the service answering "no tile here" — data, not failure. The
# converter writes those pixels as void and exits 0; this count is how the
# plugin knows the misses are PERMANENT (retrying cannot change them) and that
# the user must be told part of their area reads as 0 m sea level.
_NO_DATA_RE = re.compile(r"\[Stats\]\s*NO-DATA\s*\(HTTP 404\):\s*(\d+)\s+of\s+\d+")


# Degraded-tile protocol (toolkit, 2026-10): a source tile whose fetch failed
# and that the downloader filled from a coarser zoom is reported per tile and
# in a summary, and is NOT counted as OK in the "[Stats] Tiles:" line:
#   [Degraded] z=15 x=17000 y=11500 reason=timeout filled_from=z14
#   [Stats] DEGRADED: 3 of 4096 source tile(s) filled from coarser zooms after a failed fetch
# A degraded output tile has no hole (the coarse fill covers it), so the
# hole scan alone would call it complete and pool coarse terrain forever.
_DEGRADED_RE = re.compile(
    r"\[Degraded\]\s+z=(\d+)\s+x=(\d+)\s+y=(\d+)"
    r"(?:\s+reason=(.*?))?(?:\s+filled_from=z?(\d+))?\s*$")
_DEGRADED_STATS_RE = re.compile(r"\[Stats\]\s*DEGRADED:\s*(\d+)\s+of\s+(\d+)")


class DegradedTile(NamedTuple):
    z: int
    x: int
    y: int
    reason: str
    filled_from: Optional[int]


def _parse_degraded(lines: List[str]) -> List[DegradedTile]:
    """Every ``[Degraded]`` source tile in one download pass's output."""
    out = []
    for line in lines:
        m = _DEGRADED_RE.search(line)
        if m:
            out.append(DegradedTile(int(m.group(1)), int(m.group(2)),
                                    int(m.group(3)), (m.group(4) or "").strip(),
                                    int(m.group(5)) if m.group(5) else None))
    return out


def _parse_degraded_count(lines: List[str]) -> int:
    """The ``[Stats] DEGRADED: n of total`` summary (0 when absent)."""
    count = 0
    for line in lines:
        m = _DEGRADED_STATS_RE.search(line)
        if m:
            count = int(m.group(1))
    return count


def _xyz_tile_bbox(z: int, x: int, y: int) -> Dict[str, float]:
    """WGS84 bounds of web-mercator tile z/x/y."""
    n = 2.0 ** z

    def lat(yy: float) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * yy / n))))
    return {"west": x / n * 360.0 - 180.0, "east": (x + 1) / n * 360.0 - 180.0,
            "north": lat(y), "south": lat(y + 1)}


def _specs_hit_by_degraded(specs: List[Dict[str, Any]],
                           degraded: List[DegradedTile]) -> set:
    """Output tiles (download specs) a degraded source tile intersects."""
    hit = set()
    if not degraded:
        return hit
    boxes = [_xyz_tile_bbox(d.z, d.x, d.y) for d in degraded]
    for s in specs:
        span = s["size_px"] * s["resolution_m"] / 111_111.0
        sb = {"north": s["ul_lat"], "south": s["ul_lat"] - span,
              "west": s["ul_lon"], "east": s["ul_lon"] + span}
        if any(bboxes_intersect(sb, b) for b in boxes):
            hit.add(s["filename"])
    return hit


def _warn_degraded_tiles(n_tiles: int, degraded: List[DegradedTile],
                         zoom: int) -> None:
    """The user-facing notice for coarse-filled tiles kept for this run."""
    fills = sorted({d.filled_from for d in degraded if d.filled_from is not None})
    src = ", ".join(f"z{z}" for z in fills) or "a coarser zoom"
    _log(f"  WARNING: {n_tiles} terrain tile(s) were filled from coarser data "
         f"({src}) after download failures at z{zoom} — they are used for "
         f"this run and will be re-downloaded next run.")


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


def _parse_download_no_data(stderr_lines: List[str]) -> int:
    """Source tiles the service answered 404 for, from converter stderr.

    0 when the line is absent — either nothing was missing or the converter
    predates the no-data tally, and in both cases the caller must treat the
    misses as failures (the old, safe reading), never assume they were 404s.
    """
    count = 0
    for line in stderr_lines:
        m = _NO_DATA_RE.search(line)
        if m:
            count = int(m.group(1))
    return count


def _warn_no_data_tiles(zoom: int, no_data: int, total: int) -> None:
    """The XYZ counterpart of :func:`_warn_if_bbox_exceeds_bounds`.

    An XYZ service publishes no extent, so the only way to learn the
    requested area reaches past its coverage is the service answering 404 —
    which the converter reports and this turns into the same user-facing
    contract the DEM-file path states: missing areas are assumed 0 m (sea
    level) by Site Analysis, kept as voids by the Map Converter, and the
    result over them is not reliable.
    """
    _log(
        f"  WARNING: {no_data} of {total} source tile(s) at z{zoom} have no "
        f"data (HTTP 404) — the requested area reaches past this service's "
        f"coverage. Missing areas are assumed 0 m (sea level) by Site "
        f"Analysis and left as no-data by the Map Converter; coverage there "
        f"is not reliable — use a source that covers the full range."
    )


def get_source_resolution_info(dem_layer: Any) -> str:
    """Read the resolution from the source. For XYZ tiles, compute from zoom level."""
    try:
        source = dem_layer.source()

        # XYZ tiles: compute from zmax. Use the *effective* zmax — the label
        # drives the user's resolution choice, and advertising a zoom the
        # service does not publish is how a run ends up asking for tiles that
        # all 404 (see resolve_zmax).
        if "type=xyz" in source:
            z_max, _warning = resolve_zmax(source)
            # At equator: res = 40075000 / (2^z * 256)
            res_equator = 40_075_000 / (2 ** z_max * 256)
            label = xyz_encoding(source) or "XYZ"
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
_OSM_BUILDINGS_SOURCE_ID = "openfreemap:z14"


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


# ---------------------------------------------------------------------------
# XYZ elevation encoding and zoom limits
# ---------------------------------------------------------------------------

#: The two RGB elevation encodings ``aether_converter download`` can decode.
ENCODING_TERRARIUM = "terrarium"
ENCODING_MAPBOX = "mapbox"

# Provider knowledge lives in DATA, not code: waveshed/resources/
# known_services.json holds the URL substrings that pin a tile service to an
# elevation encoding and/or to the deepest zoom it actually publishes. The
# matching logic below stays here; only the facts moved.
#
# Why the encoding matters: Terrarium and Mapbox Terrain-RGB are both plain
# 8-bit RGB PNGs, so nothing in the *pixels* tells them apart — decode a
# Terrain-RGB tile as Terrarium and every elevation lands near -32000 m,
# which the engine happily treats as terrain.  QGIS's own ``interpretation=``
# parameter is authoritative when the layer carries one, but a hand-added XYZ
# layer usually carries none.
_KNOWN_SERVICES_PATH = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "resources",
    "known_services.json"))

# Lazy cache for the parsed known-services list (loaded at most once).
_known_services_cache: Optional[List[Dict[str, Any]]] = None


def _known_services() -> List[Dict[str, Any]]:
    """The known-elevation-services table, loaded lazily from resources.

    A missing or invalid file is a packaging bug, not a runtime condition the
    plugin can paper over — every XYZ encoding/zoom decision depends on this
    data, so guessing without it risks silently wrong terrain. Fail loudly.
    """
    global _known_services_cache
    if _known_services_cache is not None:
        return _known_services_cache

    path = _KNOWN_SERVICES_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"The plugin's known-services table could not be read:\n{path}\n"
            f"({exc})\n\nThis file ships with the plugin — its absence or "
            "corruption is a packaging bug. Reinstall the Waveshed plugin."
        ) from exc

    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, list):
        raise RuntimeError(
            f"The plugin's known-services table is invalid:\n{path}\n"
            "(expected a top-level object with a \"services\" list). "
            "Reinstall the Waveshed plugin."
        )
    for i, svc in enumerate(services):
        match = svc.get("match") if isinstance(svc, dict) else None
        bad = (
            not isinstance(match, str) or not match
            or ("max_zoom" in svc and not isinstance(svc["max_zoom"], int))
            or ("encoding" in svc and svc["encoding"] not in
                (ENCODING_TERRARIUM, ENCODING_MAPBOX))
        )
        if bad:
            raise RuntimeError(
                f"The plugin's known-services table is invalid:\n{path}\n"
                f"(services[{i}] = {svc!r} — every entry needs a non-empty "
                f"\"match\" string; \"max_zoom\" must be an integer; "
                f"\"encoding\" must be \"{ENCODING_TERRARIUM}\" or "
                f"\"{ENCODING_MAPBOX}\"). Reinstall the Waveshed plugin."
            )
    _known_services_cache = services
    return services


def _xyz_url(source: str) -> str:
    """The decoded ``url=`` tile template of an XYZ layer URI (``""`` if none)."""
    params = dict(urllib.parse.parse_qsl(source))
    return urllib.parse.unquote(params.get("url", ""))


def _xyz_encoding_hints(url_template: str) -> Tuple[bool, bool]:
    """``(mapbox_hint, terrarium_hint)`` present in *url_template*."""
    low = url_template.lower()
    mapbox = terrarium = False
    for svc in _known_services():
        if svc["match"] in low:
            if svc.get("encoding") == ENCODING_MAPBOX:
                mapbox = True
            elif svc.get("encoding") == ENCODING_TERRARIUM:
                terrarium = True
    return mapbox, terrarium


#: The ONLY two values QGIS itself honours in ``interpretation=`` (measured on
#: 3.44.7 — ``terrarium``, ``mapboxterrain``, ``terrainrgb`` and the rest are
#: silently ignored and the layer stays an ARGB32 picture).  Note QGIS's name
#: for the Mapbox Terrain-RGB family is ``maptilerterrain``, which contains
#: neither "mapbox" nor "terrarium": read as a substring it matched nothing,
#: the plugin fell back to Terrarium, and the ground decoded to about
#: -32350 m — on a layer QGIS was drawing as correct elevation.
_QGIS_INTERPRETATION = {
    "terrariumterrain": ENCODING_TERRARIUM,
    "maptilerterrain": ENCODING_MAPBOX,
}


def xyz_encoding(source: str) -> Optional[str]:
    """Elevation encoding an XYZ URI pins itself to, or None if it pins none.

    ``interpretation=`` wins when QGIS wrote one; otherwise the URL template
    decides.  None means the URI is undecided — either it names no known
    service at all, or it names both families at once.
    :func:`resolve_xyz_encoding` turns that into a default or an error;
    :func:`source_identity` turns it into the historical default so a URI that
    is later disambiguated lands on the same pool.
    """
    params = dict(urllib.parse.parse_qsl(source))
    interpretation = params.get("interpretation", "").lower()
    known = _QGIS_INTERPRETATION.get(interpretation)
    if known is not None:
        return known
    # A hand-written value QGIS ignores still says what the user meant, and
    # the plugin can honour it even where the canvas will not.
    if "mapbox" in interpretation or "maptiler" in interpretation:
        return ENCODING_MAPBOX
    if "terrarium" in interpretation:
        return ENCODING_TERRARIUM

    mapbox, terrarium = _xyz_encoding_hints(_xyz_url(source))
    if mapbox != terrarium:
        return ENCODING_MAPBOX if mapbox else ENCODING_TERRARIUM
    return None


def resolve_xyz_encoding(source: str) -> str:
    """The encoding to decode *source* with, for the paths that fetch tiles.

    Raises :class:`RuntimeError` when the URL names both encoding families —
    that is genuinely ambiguous and guessing it has a 50 % chance of producing
    terrain 32 km below sea level, so the user has to say which it is.  A URL
    naming *neither* keeps the historical Terrarium default (that is what every
    working self-hosted mirror relies on today) but says so in the log.
    """
    encoding = xyz_encoding(source)
    if encoding is not None:
        return encoding

    mapbox, terrarium = _xyz_encoding_hints(_xyz_url(source))
    if mapbox and terrarium:
        raise RuntimeError(
            "This elevation layer's URL mentions both Mapbox Terrain-RGB and "
            "Terrarium encoding, so the plugin cannot tell how its tiles "
            "decode to metres. Decoding with the wrong one puts the terrain "
            "around -32000 m.\n\n"
            "Set the layer's interpretation explicitly — in the XYZ "
            "connection dialog, or by adding 'interpretation=terrarium' or "
            "'interpretation=mapboxterrain' to the layer URI. Already-cached "
            "terrain for this layer is kept."
        )
    _log("  NOTE: this XYZ layer names no known elevation service and carries "
         "no 'interpretation=' parameter — assuming Terrarium encoding. If the "
         "terrain comes out near -32000 m it is Mapbox Terrain-RGB; set "
         "interpretation=mapboxterrain on the layer.")
    return ENCODING_TERRARIUM


# Deepest zoom each known elevation-tile service actually publishes lives in
# known_services.json (per-entry "max_zoom").  QGIS defaults a hand-added XYZ
# layer to ``zmax=18``, but no public terrain service goes that deep: every
# tile past the real maximum 404s, the converter writes those as 0 m, and the
# run completes — confidently — over a flat sea-level plane.  Clamping via
# ``resolve_zmax`` is what stops that.

#: Absolute ceiling for a slippy-map zoom; past this it is a typo, not a level.
_XYZ_ZOOM_CEILING = 24
#: Historical default when the URI carries no ``zmax``.
_XYZ_DEFAULT_ZMAX = 15


def service_max_zoom(url_template: str) -> Optional[int]:
    """Deepest zoom *url_template*'s service is known to publish, else None."""
    low = url_template.lower()
    for svc in _known_services():
        zoom = svc.get("max_zoom")
        if zoom is not None and svc["match"] in low:
            return zoom
    return None


def xyz_native_resolution_m(source: str) -> Optional[float]:
    """Approximate native ground resolution (m at the equator) of an XYZ URI.

    Computed from the EFFECTIVE max zoom (:func:`resolve_zmax` — validated
    and clamped to what the service really publishes), so a hand-added layer
    carrying QGIS's default ``zmax=18`` on a z15 service reports the real
    ~4.8 m instead of a fictitious 0.6 m. Returns None for non-XYZ sources.
    The one XYZ-resolution helper for every tab.
    """
    if "type=xyz" not in source:
        return None
    z_max, _warning = resolve_zmax(source)
    # At the equator: res = 40075000 / (2^z * 256); less at higher latitudes.
    return round(40_075_000 / (2 ** z_max * 256), 1)


def resolve_zmax(source: str) -> Tuple[int, Optional[str]]:
    """``(effective_zmax, warning)`` for an XYZ source.

    ``int(params["zmax"])`` used to be taken at face value, unvalidated: a
    non-numeric value crashed the run, and QGIS's own default of 18 on a
    service that stops at 15 made every tile 404 at fine resolutions — which
    the converter writes as 0 m, so the analysis ran to completion over a flat
    sea.  Clamp to what the service really publishes and say so.

    The *raw* zmax stays in :func:`source_identity`, deliberately: clamping the
    identity too would move every existing ``zmax=18`` pool, discarding the
    coarse-resolution tiles in it that were always correct (a 30 m run never
    asks for a zoom past 15 in the first place).
    """
    params = dict(urllib.parse.parse_qsl(source))
    raw = params.get("zmax")
    warning: Optional[str] = None

    if raw is None or raw == "":
        z_max = _XYZ_DEFAULT_ZMAX
    else:
        try:
            z_max = int(raw)
        except (TypeError, ValueError):
            return _XYZ_DEFAULT_ZMAX, (
                f"the layer's zmax ({raw!r}) is not a number — using "
                f"z{_XYZ_DEFAULT_ZMAX}."
            )

    if z_max < 0:
        return 0, f"the layer's zmax ({z_max}) is negative — using z0."
    if z_max > _XYZ_ZOOM_CEILING:
        warning = (f"the layer's zmax ({z_max}) is past any real slippy-map "
                   f"zoom — capped at z{_XYZ_ZOOM_CEILING}.")
        z_max = _XYZ_ZOOM_CEILING

    known = service_max_zoom(_xyz_url(source))
    if known is not None and z_max > known:
        warning = (
            f"this service publishes tiles only to z{known}, but the layer "
            f"says zmax={z_max}. Every request past z{known} returns 404 and "
            f"the converter writes those tiles as 0 m, so the analysis would "
            f"run over a flat sea-level plane. Capping at z{known} — fix the "
            f"layer's Max. Zoom Level to silence this."
        )
        z_max = known

    return z_max, warning


# Bumped whenever tile geometry, provenance or cache layout changes, so tiles
# built by an older plugin can never be mistaken for current ones.
#   v2: tile extent is resolution-keyed (ABT_EXTENT_DEG) instead of a flat 1
#       degree, and the key is the tile set rather than the request bbox.
#   v3: tiles are pooled per tile; source identity is normalised; the XYZ zoom
#       is computed per tile instead of from the request's bbox centre.
#   v4: the converter area-averages each output pixel's footprint instead of
#       taking one source sample from it, and every stored elevation changes,
#       for the same tile geometry — nothing in the key would otherwise differ,
#       so a v3 pool would keep serving the aliased tiles forever. Within v4
#       the ingest source format also changed: jobs now pass ``sources[]``
#       (each file in its OWN CRS, reprojected by the converter while
#       sampling) instead of a plugin-side GDAL warp to a WGS84 ``base_tif``.
#       Pixels may move by up to one source sample where a projected CRS used
#       to go through the warp — accepted within the same schema, alongside
#       the area-averaging change it ships with.
#   v5: the converter's sampling registration moved — the averaging window is
#       anchored on each output cell's CENTRE (the footprint the contract
#       always promised) instead of its NW corner, and RasterPixelIsPoint
#       sources (Copernicus, SRTM) are shifted half a source pixel to their
#       true corner origin. Same tile geometry, every stored elevation may
#       move by up to one source sample; missing XYZ tiles are now VOID
#       instead of 0 m fill. A v4 pool would keep serving the misregistered
#       tiles forever.
_CACHE_SCHEMA = "v5"

# File-built tiles only (local files, folders, rendered exports — everything
# the converter ``ingest``s; XYZ downloads never take that path), so a fix that
# moves only ingest bytes does not throw away every downloaded tile.
#   g2: ingest honours non-square source pixels (separate X / Y step). It
#       used the X step for both since the converter took over reprojection in
#       v4, stretching e.g. Copernicus GLO-30 north of 50°N up to 2× north–south
#       (measured 90 m median, 432 m max error on N50E014, 2026-10-13).
_FILE_SOURCE_SCHEMA = "g2"


def _schema_for(source: str) -> str:
    """Cache-key schema term for *source*: XYZ pools stay on _CACHE_SCHEMA,
    file-built pools also carry _FILE_SOURCE_SCHEMA."""
    if "type=xyz" in source:
        return _CACHE_SCHEMA
    return f"{_CACHE_SCHEMA}.{_FILE_SOURCE_SCHEMA}"


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

    The encoding comes from :func:`xyz_encoding`, the same detector the
    download path uses, so the key can never disagree with the bytes that were
    written under it.  An undecided URI falls back to Terrarium — the value it
    has always had — so adding an explicit ``interpretation=terrarium`` later
    keeps the existing pool instead of re-downloading it.

    ``zmax`` is the *raw* URI value, not the clamped one from
    :func:`resolve_zmax`: clamping it here would move every pool whose layer
    says ``zmax=18``, including the coarse-resolution tiles in it that were
    never affected by the 404s (see :func:`resolve_zmax`).
    """
    if "type=xyz" not in source:
        return source
    params = dict(urllib.parse.parse_qsl(source))
    url = _xyz_url(source)
    encoding = xyz_encoding(source) or ENCODING_TERRARIUM
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
               buildings: Optional[str] = None,
               regions: Optional[Dict[str, Optional[Dict[str, float]]]] = None,
               ) -> str:
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

    The source term goes through :func:`source_identity`, the SAME
    normalisation ``_pool_dir`` applies. It used to hash the raw layer URI,
    so the two halves of one cache disagreed about what "the same source" is:
    re-adding an XYZ layer with the parameters in another order (or with
    ``zmin=``/``interpretation=`` spelled differently) kept the pool and moved
    the view to a fresh directory — the view identity moving underneath a
    pool that did not.

    *regions* ``{tile name: region bbox or None}`` — the part of each tile
    the run needs when it is served from a partial pool tile (see
    :func:`resolve_pool_tile`). Every non-None region is hashed in, so a view
    linked to a ``.part`` built for one region is never served to a run that
    needs another; whole-tile runs (no region anywhere) keep their key.
    """
    names = ",".join(sorted(t["filename"] for t in subtiles))
    key = (f"{_schema_for(source)}|{source_identity(source)}|{names}"
           f"|{resolution_m}")
    if buildings:
        key += f"|{source_fingerprint(buildings)}"
    parts = sorted(
        f"{name}@{r['west']!r},{r['south']!r},{r['east']!r},{r['north']!r}"
        for name, r in (regions or {}).items() if r is not None)
    if parts:
        key += "|regions=" + hashes.md5(
            ";".join(parts).encode()).hexdigest()
    return hashes.md5(key.encode()).hexdigest()


def _view_dir(source: str, subtiles: List[Dict[str, Any]],
              resolution_m: int, buildings: Optional[str] = None,
              regions: Optional[Dict[str, Optional[Dict[str, float]]]] = None,
              ) -> str:
    """Absolute view directory for one run — the directory the engine reads.

    The counterpart of :func:`_pool_dir`, and the ONE place the view path is
    assembled, so no caller can compute it a second time and land somewhere
    else (see :func:`get_cache_dir` on why a second computation could move).
    *regions*: see :func:`_cache_key`.
    """
    return os.path.join(get_cache_dir(), _VIEW_DIRNAME,
                        _cache_key(source, subtiles, resolution_m, buildings,
                                   regions))


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

# A file-built pool tile built for only PART of its extent (the region one
# run needed — see :func:`resolve_pool_tile`) is pooled as ``<name>.part``
# (``tile_….abt.part``), never under its canonical ``.abt`` name: everything
# that lists or reads pool tiles by name or by ``*.abt`` (abt.list_tiles,
# list_abt_tiles, the view prune, _tile_ready, _listed_tile_ready, the
# engine's read_dir) therefore sees "no tile" there. Its sidecar
# ``<name>.part.json`` records which regions are real terrain and is bound
# to the ``.part`` file's size + mtime.
_PART_SUFFIX = ".part"
_PART_META_SUFFIX = ".part.json"


def _finish_view(pool_dir: str, view_dir: str, names: List[str],
                 t0: float,
                 tiles: Optional[Dict[str, Tuple[Dict[str, Any],
                                                 Optional[Dict[str, float]]]]
                        ] = None,
                 voids: Optional[Dict[str, Optional[int]]] = None) -> None:
    """Link this run's tiles into its view directory and log the outcome
    (*tiles*, *voids*: see :func:`_sync_view`)."""
    gone = _sync_view(pool_dir, view_dir, names, tiles=tiles, voids=voids)
    if gone:
        _log(f"  WARNING: {len(gone)} of {len(names)} tile(s) could not be "
             f"provided (first: {gone[0]}) — coverage will have holes there")
    _log(f"  TOTAL: {time.perf_counter() - t0:.1f}s")


def _pool_dir(source: str, buildings: Optional[str] = None) -> str:
    """Directory holding every tile built for this source (+ building set).

    Resolution is not part of the identity: ``_tile_params`` puts it in the
    filename, so tiles of different resolutions coexist without colliding.
    """
    key = f"{_schema_for(source)}|{source_identity(source)}"
    if buildings:
        key += f"|{source_fingerprint(buildings)}"
    return os.path.join(get_cache_dir(), _POOL_DIRNAME,
                        hashes.md5(key.encode()).hexdigest())


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


def _abt_whole_bytes(size_px: int) -> int:
    """Bytes of a whole ``.abt`` tile of *size_px*: the header plus
    ``size × stride``, rows padded to 256 bytes as the converter writes
    them (the stride :func:`_tile_is_whole` reads from the header)."""
    from . import abt
    return abt.HEADER_SIZE + size_px * ((size_px * 2 + 255) & ~255)


def _pool_listing(pool_dir: str) -> Dict[str, Any]:
    """``{name: os.DirEntry}`` of *pool_dir* — one listing ({} when absent).
    The entries carry the sizes (free on Windows; stat'ed lazily, once, on
    the entries asked for elsewhere)."""
    try:
        with os.scandir(pool_dir) as it:
            return {e.name: e for e in it}
    except OSError:
        return {}


def _listed_tile_whole(listing: Dict[str, Any], spec: Dict[str, Any],
                       pool_dir: str, name: Optional[str] = None) -> bool:
    """:func:`_tile_is_whole` from a :func:`_pool_listing`: a file at least
    header + ``size × stride`` bytes long for the tile *spec* is whole — no
    header read. Only a file SHORTER than that has its own header read (a
    killed run's tile, or one of another size under this name: an older
    extent ladder), so the rule is ``_tile_is_whole``'s for it. *name*
    (default: the spec's canonical file name) checks another file of the
    tile by the same rule — its ``.part``."""
    name = name or spec["filename"]
    entry = listing.get(name)
    if entry is None:
        return False
    try:
        if not entry.is_file():
            return False
        if entry.stat().st_size >= _abt_whole_bytes(int(spec["size_px"])):
            return True
    except (OSError, KeyError, TypeError, ValueError):
        pass
    return _tile_is_whole(os.path.join(pool_dir, name))


def _listed_tile_ready(listing: Dict[str, Any], spec: Dict[str, Any],
                       pool_dir: str) -> bool:
    """:func:`_tile_ready` from a :func:`_pool_listing` — no rebuild flag in
    the listing and :func:`_listed_tile_whole`. For checking many tiles at
    once (download estimate, ``ensure_pool_tiles``, the Site Analysis stale
    check); one file → :func:`_tile_ready`."""
    if spec["filename"] + _REBUILD_SUFFIX in listing:
        return False
    return _listed_tile_whole(listing, spec, pool_dir)


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


def _drop_pool_tiles(pool_dir: str, names: List[str]) -> int:
    """Remove tiles *names* (and their rebuild flags) from the pool.

    A failed download does NOT leave a half-written file behind: the engine
    pads every output tile to its full size before it decides the run failed,
    so what is on disk is a complete, valid, perfectly readable .abt of 0 m
    terrain. Flagging it for rebuild is not enough — ``_sync_view`` links a
    flagged tile into the run anyway (a gapped tile still holds usable
    terrain), and anything reading the pool directly sees a whole tile. The
    only way a failure cannot be mistaken for sea level is for the file not to
    be there.

    Safe because every caller only ever downloads tiles that were NOT already
    usable in the pool (``_tile_ready`` filtered them), so a previously good
    pooled tile can never be in *names*.

    Returns how many tiles were removed.
    """
    dropped = 0
    for name in names:
        try:
            os.remove(os.path.join(pool_dir, name))
            dropped += 1
        except OSError:
            pass                       # never written, or already gone
        try:
            os.remove(os.path.join(pool_dir, name + _REBUILD_SUFFIX))
        except OSError:
            pass
    return dropped


class PoolRemoval(NamedTuple):
    """What :func:`remove_pool_tiles` did."""

    #: ``.abt`` files removed.
    removed: int
    #: Bytes those files gave back (a file still hard-linked from a terrain
    #: view frees nothing).
    freed_bytes: int
    #: Removed files still linked elsewhere (a Site Analysis view).
    linked: int


def remove_pool_tiles(pool_dir: str, names: Sequence[str]) -> PoolRemoval:
    """Remove exactly the pool tiles *names* (``<name>.abt``) with their
    ``.src`` / ``.rebuild`` sidecars from *pool_dir* — the Map Converter's
    "Keep pool tiles" off, after a successful run, for the tiles that run
    downloaded. Nothing else in the pool, and never the raw source-tile
    cache. A file Windows refuses to delete (held open) stays. The void-count
    records of the tiles that are gone go with them
    (:func:`_record_pool_voids`)."""
    removed = freed = linked = 0
    gone: List[str] = []
    for name in names:
        path = os.path.join(pool_dir, name)
        try:
            st = os.stat(path)
        except OSError:
            st = None
            gone.append(name)      # never there
        try:
            os.remove(path)
        except OSError:
            st = None              # never there, or refused: nothing removed
        if st is not None:
            removed += 1
            gone.append(name)
            if getattr(st, "st_nlink", 1) > 1:
                linked += 1
            else:
                freed += int(st.st_size)
        for suffix in (_SOURCE_FP_SUFFIX, _REBUILD_SUFFIX):
            try:
                os.remove(path + suffix)
            except OSError:
                pass
    _record_pool_voids(pool_dir, {name: None for name in gone})
    return PoolRemoval(removed, freed, linked)


def osm_buildings_store_dir() -> str:
    """THE OpenFreeMap building-tile store (``{z}_{x}_{y}.pbf``, z14):
    ``<cache>/pool/<md5("buildings|openfreemap:z14")>``.

    One store for every consumer — Site Analysis (every source, band and
    site) and the Map Converter's OSM buildings row — so a tile fetched by
    one is never fetched again by the other. The tiles are globally
    addressed, independent of any terrain source. An md5-named directory
    under the pool, so :func:`cache_entries` lists it and Clear Terrain
    Cache deletes it with the terrain sets. Engines never read it: each run
    links the tiles it needs into its own view (the converter decodes every
    file of the directory it is handed)."""
    key = f"buildings|{_OSM_BUILDINGS_SOURCE_ID}"
    return os.path.join(get_cache_dir(), _POOL_DIRNAME,
                        hashes.md5(key.encode()).hexdigest())


def osm_buildings_view_dir(names: Sequence[str],
                           marker: Optional[str] = None) -> str:
    """The view directory holding exactly the building tiles *names* (one
    Map Converter ingest batch): ``<cache>/views/<md5 of the names>``, so a
    batch over the same tiles reuses its links. *marker*: the view of a
    converter run launched before its fetch ends (a Site Analysis download
    or ingest, a Map Converter batch on aether_converter >= 0.2.12), named
    from the tiles it NEEDS and completed by that marker file
    (``osm_buildings.ready_view`` / ``complete_view``) — its own key, never
    a view the sequential path relinks."""
    key = (f"buildings|{_OSM_BUILDINGS_SOURCE_ID}|"
           + ",".join(sorted(names)))
    if marker:
        key += f"|ready:{marker}"
    return os.path.join(get_cache_dir(), _VIEW_DIRNAME,
                        hashes.md5(key.encode()).hexdigest())


def osm_buildings_list_dir() -> str:
    """The directory of the building tile LISTS (aether_converter >= 0.2.14,
    ``buildings_pbf_list``, toolkit CONTRACT changelog 51):
    ``<cache>/views/<md5("buildings|openfreemap:z14|lists")>`` — ONE
    md5-named directory, so :func:`cache_entries` lists it and Clear
    Terrain Cache deletes it with the views (a list file directly in
    ``<cache>/views`` would survive the clear: only md5-named DIRECTORIES
    are cache entries)."""
    key = f"buildings|{_OSM_BUILDINGS_SOURCE_ID}|lists"
    return os.path.join(get_cache_dir(), _VIEW_DIRNAME,
                        hashes.md5(key.encode()).hexdigest())


def osm_buildings_list_path(names: Sequence[str]) -> str:
    """The list file of a converter run burning the building tiles it
    NEEDS, *names* (``buildings_pbf_list``, aether_converter >= 0.2.14): in
    :func:`osm_buildings_list_dir`, ``<md5 of the sorted names + "|list">
    .txt`` — its own key, never the name of a view directory of the same
    names. It names the tiles with data among them, read straight from THE
    store (``osm_buildings.ready_list`` / ``complete_list``): no view is
    linked."""
    key = (f"buildings|{_OSM_BUILDINGS_SOURCE_ID}|"
           + ",".join(sorted(names)) + "|list")
    return os.path.join(osm_buildings_list_dir(),
                        hashes.md5(key.encode()).hexdigest() + ".txt")


def link_pbf_view(store_dir: str, view_dir: str, names: Sequence[str]) -> int:
    """Make *view_dir* hold exactly *names* out of *store_dir*: strangers
    removed, missing names hard-linked (copied where the file system has no
    hard links). ONE listing of the view, no per-tile stat — a Map
    Converter batch links tens of thousands of tiles. A name the store does
    not hold raises ``RuntimeError`` naming it (the caller lists only tiles
    it has just seen there). Returns the number of names in the view.

    Beyond :data:`_LINK_PARALLEL_MIN` names the links are made on
    :func:`link_threads` threads where that pays (a hard link is a
    metadata round trip; 2,000 of them went from 12.6 s to 4.4 s on 16
    threads on the container's network mount) and one after the other on
    Windows (owner's bench 2026-11-07, 20,000 links on an NTFS HDD: 1
    thread 2,120/s, 4 → 2,051/s, 8 → 1,698/s, 16 → 1,540/s — NTFS
    serialises the inserts into one directory, so threads only contend;
    the 16-thread version linked 21,000 tiles in 24 s where 12 s had done)."""
    os.makedirs(view_dir, exist_ok=True)
    wanted = set(names)
    try:
        have = set(os.listdir(view_dir))
    except OSError:
        have = set()
    for stale in have - wanted:
        try:
            os.remove(os.path.join(view_dir, stale))
        except OSError:
            pass

    def link_one(name: str) -> Optional[str]:
        """None, or the error text for a tile that could not be provided."""
        src = os.path.join(store_dir, name)
        dst = os.path.join(view_dir, name)
        try:
            os.link(src, dst)
            return None
        except FileExistsError:
            return None
        except OSError:
            pass
        try:
            shutil.copy2(src, dst)
        except OSError as exc:
            return (f"Building tile {name} could not be linked into "
                    f"{view_dir} ({exc})")
        return None

    todo = sorted(wanted - have)
    threads = link_threads()
    pool = None
    if len(todo) < _LINK_PARALLEL_MIN or threads <= 1:
        results = map(link_one, todo)
    else:
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(max_workers=min(threads, len(todo)))
        results = pool.map(link_one, todo, chunksize=64)
    try:
        for err in results:
            if err is not None:
                raise RuntimeError(err)
    finally:
        if pool is not None:
            pool.shutdown(wait=True)
    return len(wanted)


#: Threads for :func:`link_pbf_view` where the file system takes them
#: (network mounts, ext4, APFS): I/O-bound hard links.
LINK_THREADS = 16
#: … and on Windows: one. NTFS serialises the inserts into one directory
#: (owner's bench 2026-11-07, 20,000 links: 2,120/s on one thread, 1,540/s
#: on sixteen).
LINK_THREADS_WINDOWS = 1
#: A/B override: a positive integer of threads for :func:`link_pbf_view`.
LINK_THREADS_ENV = "WAVESHED_LINK_THREADS"
#: Below this many links the loop runs inline.
_LINK_PARALLEL_MIN = 64


def link_threads() -> int:
    """How many threads :func:`link_pbf_view` uses: :data:`LINK_THREADS_ENV`
    when it holds a positive integer, else :data:`LINK_THREADS_WINDOWS` on
    Windows, else :data:`LINK_THREADS`."""
    raw = os.environ.get(LINK_THREADS_ENV, "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return LINK_THREADS_WINDOWS if _is_windows() else LINK_THREADS


def drop_pbf_tiles(names: Sequence[str]) -> None:
    """Remove building tiles *names* from the store and from every view
    linking them (``osm_buildings.batch_view`` dirs of both tabs, and the
    ``_buildings`` dirs older Site Analysis runs left in their views): a
    tile the converter could not decode must be fetched anew by the next
    run, and a view keeps a hard link to the old bytes."""
    targets = [osm_buildings_store_dir()]
    views_root = os.path.join(get_cache_dir(), _VIEW_DIRNAME)
    try:
        for view in os.listdir(views_root):
            targets.append(os.path.join(views_root, view))
            targets.append(os.path.join(views_root, view, "_buildings"))
    except OSError:
        pass
    for directory in targets:
        for name in names:
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                pass


def _tile_has_voids(path: str) -> Optional[bool]:
    """Does the .abt at *path* contain any void/fill sample? None = unreadable.

    Anything at or below the plugin's validity floor counts — the converter's
    ``-9999`` void sentinel and any legacy fill alike — mirroring the
    ``> MIN_VALID_ELEV_M`` rule everything else in this package applies.
    """
    import numpy as np
    from . import abt
    header = abt.read_header(path)
    if header is None:
        return None
    try:
        mm = np.memmap(path, dtype=np.uint8, mode="r")
        body = mm[abt.HEADER_SIZE:abt.HEADER_SIZE + header.size * header.stride]
        rows = body.reshape(header.size, header.stride)
        samples = rows[:, :header.size * 2].view(np.int16)
        return bool((samples <= abt.MIN_VALID_ELEV_COUNTS).any())
    except (OSError, ValueError):
        return None


def _fill_view_voids(src: str, dst: str) -> bool:
    """Copy pool tile *src* to *dst* with every void sample replaced by 0 m.

    The Site Analysis contract — stated by ``void_fill_m: 0.0`` on the ingest
    path and by the extent warning — is that ground the source does not cover
    is 0 m sea level. The download path pools tiles WITH voids (the Map
    Converter needs them kept), so the fill happens here, per view: the pool
    stays pristine and only what ``aether_core`` reads is filled. Without
    this, the engine read the raw ``-9999`` sentinel as terrain 5 km below
    sea level wherever an XYZ run crossed its service's coverage edge.
    """
    import numpy as np
    from . import abt
    header = abt.read_header(src)
    if header is None:
        return False
    try:
        shutil.copy2(src, dst)
        mm = np.memmap(dst, dtype=np.uint8, mode="r+")
        body = mm[abt.HEADER_SIZE:abt.HEADER_SIZE + header.size * header.stride]
        rows = body.reshape(header.size, header.stride)
        samples = rows[:, :header.size * 2].view(np.int16)
        filled = int((samples <= abt.MIN_VALID_ELEV_COUNTS).sum())
        samples[samples <= abt.MIN_VALID_ELEV_COUNTS] = 0
        mm.flush()
        del mm
        _log(f"  view fill: {os.path.basename(dst)} — {filled:,} void "
             f"sample(s) set to 0 m (sea level) for the engine")
        return True
    except (OSError, ValueError) as exc:
        _log(f"  WARNING: could not fill voids into {os.path.basename(dst)}: "
             f"{exc}")
        try:
            os.remove(dst)
        except OSError:
            pass
        return False


def _sync_view(pool_dir: str, view_dir: str, names: List[str],
               keep: Optional[Sequence[str]] = None,
               tiles: Optional[Dict[str, Tuple[Dict[str, Any],
                                               Optional[Dict[str, float]]]]
                      ] = None,
               voids: Optional[Dict[str, Optional[int]]] = None) -> List[str]:
    """Populate *view_dir* with exactly *names*, linked from the pool.

    Hardlinks cost nothing; where the filesystem refuses one (a different
    volume, FAT, or Windows without the privilege) we fall back to a copy.
    A pool tile that contains voids is not linked but COPIED WITH ITS VOIDS
    FILLED to 0 m — see :func:`_fill_view_voids`; the pool copy keeps them.
    *keep* names further tiles the view legitimately holds from ANOTHER pool
    (a range profile whose bands differ in buildings): they are not pruned.

    *tiles* ``{name: (spec, region)}`` routes those names through
    :func:`resolve_pool_tile` (``for_link``): the pool file may be the
    complete ``<name>`` or a ``<name>.part`` covering *region*, and is linked
    under the canonical name either way. For such a name a pool source that
    is missing (or was renamed: a ``.part`` promoted to a whole tile) is never
    "fresh" — the view copy is dropped and the link re-resolved.

    *voids* ``{name: void samples or None}`` (:func:`_view_voids`: the
    converter's recorded count of the pool file linked): known 0 → linked
    without reading the tile; known > 0 → :func:`_fill_view_voids` at once
    (no scan first); unknown (None, or a name not in it) → scanned
    (:func:`_tile_has_voids`), then linked or filled — as before. Logs one
    ``view:`` line (linked from recorded counts, scanned, filled) when it
    provided any tile. Returns the names that could not be provided.
    """
    t_sync = time.perf_counter()
    os.makedirs(view_dir, exist_ok=True)
    wanted = set(names) | set(keep or ())

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
    linked_known = scanned = filled = 0
    listing = _pool_listing(pool_dir) if tiles else {}
    for name in names:
        dst = os.path.join(view_dir, name)
        resolved = tiles is not None and name in tiles
        if resolved:
            spec, region = tiles[name]
            src: Optional[str] = resolve_pool_tile(
                pool_dir, spec, region, listing=listing, for_link=True).path
        else:
            src = os.path.join(pool_dir, name)
        if _tile_is_whole(dst):
            if src is None:
                fresh = False
            else:
                try:
                    fresh = (os.path.samefile(dst, src)
                             or os.path.getmtime(dst) >= os.path.getmtime(src))
                except OSError:
                    # A canonical pool tile that vanished keeps the view's
                    # whole copy (as always); a resolved (.part-backed) link
                    # whose source is gone or renamed must be re-resolved.
                    fresh = not resolved
            if fresh:
                continue
            # The pool tile was rebuilt since this view was made (a filled
            # copy does not track the pool the way a hardlink does), so the
            # view copy is stale — rebuild it below.
        if os.path.exists(dst):
            try:
                os.remove(dst)      # truncated leftover from a killed run
            except OSError:
                pass
        if src is None or not _tile_is_whole(src):
            unavailable.append(name)
            continue
        # A gapped pool tile (the run crossed the source's coverage edge)
        # must not reach the engine raw: the view gets a 0 m-filled copy —
        # the Site Analysis sea-level contract — while the pool keeps its
        # voids for the Map Converter. The converter's recorded count says
        # which tiles have any; only a tile without one is read to find out.
        count = voids.get(name) if voids else None
        if count is None:
            scanned += 1
            gapped = bool(_tile_has_voids(src))
        else:
            gapped = count > 0
        if gapped:
            if _fill_view_voids(src, dst):
                filled += 1
            else:
                unavailable.append(name)
            continue
        try:
            os.link(src, dst)
        except OSError:
            try:
                shutil.copy2(src, dst)
            except OSError:
                unavailable.append(name)
                continue
        if count is not None:
            linked_known += 1
    if linked_known or scanned or filled:
        _log(f"  view: {linked_known} tile(s) linked from recorded void "
             f"counts, {scanned} scanned, {filled} filled "
             f"({time.perf_counter() - t_sync:.1f}s)")
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


def tile_footprint(tile: Dict[str, Any]) -> Tuple[float, float, float]:
    """(north, west, extent) of a subtile in degrees — equal for two tiles of
    different resolutions that cover the same ground (5 m and 10 m share the
    0.25 deg grid). aether_core keeps one tile per footprint (the one closest to
    the job resolution), so a profile must never hand it both."""
    extent = tile["size_px"] * tile["exact_res_m"] / 111_111.0
    return (round(tile["ul_lat"], 6), round(tile["ul_lon"], 6), round(extent, 6))


def _band_tiles(subtiles: List[Dict[str, Any]], tx_lat: float, tx_lon: float,
                min_range_km: float = 0.0,
                skip_footprints: Optional[set] = None) -> List[Dict[str, Any]]:
    """Drop the subtiles a finer profile band already covers: those lying
    wholly inside the *min_range_km* disc and those whose footprint is in
    *skip_footprints*. No-op with the defaults."""
    if min_range_km <= 0 and not skip_footprints:
        return subtiles
    earth_r = 6_371_000.0
    cos_tx = max(math.cos(math.radians(tx_lat)), 1e-6)
    inner_m = min_range_km * 1000.0
    kept = []
    for t in subtiles:
        fp = tile_footprint(t)
        if skip_footprints and fp in skip_footprints:
            continue
        north, west, extent = fp
        far = 0.0
        for lat in (north - extent, north):
            for lon in (west, west + extent):
                dy = (lat - tx_lat) * earth_r * math.pi / 180.0
                dx = (lon - tx_lon) * earth_r * cos_tx * math.pi / 180.0
                far = max(far, math.hypot(dx, dy))
        if inner_m > 0 and far < inner_m:
            continue
        kept.append(t)
    return kept


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
        parts.append(_OSM_BUILDINGS_SOURCE_ID)
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
    min_range_km: float = 0.0,
    skip_footprints: Optional[set] = None,
    output_res_m: Optional[float] = None,
    source_infos: Optional[Tuple[List[Dict[str, Any]], bool,
                                 Optional[FootprintIndex]]] = None,
    region_union: Optional[Dict[str, List[Dict[str, float]]]] = None,
) -> Dict[str, Any]:
    """What one analysis will actually have to build, given what is pooled.

    *min_range_km* / *skip_footprints* narrow it to one band of a range
    profile, exactly as :func:`prepare_terrain` does (see
    :func:`terrain_plan_profile`).

    The pre-run warning used to price the whole tile set every time, so a run
    whose terrain was already on disk still announced tens of gigabytes of
    "download" and trained the user to click through it. Price only the tiles
    that are genuinely missing — per tile through :func:`resolve_pool_tile`,
    with the job's region (:func:`tile_regions`), so a ``.part`` that already
    holds the region is pooled terrain and a miss of a file-source tile is
    priced as the region it builds: ``tiles_partial`` of the missing tiles
    are partial builds covering ``build_deg2`` (of ``tile_deg2`` deg² of
    those tiles). With *source_infos* (``(infos, use_qgis_export, index)``,
    as :func:`_read_source_once` returns) fingerprints are checked and the
    files feeding the regions to build are priced too (``source_files``,
    ``source_mb``); without it (the GUI thread never reads a folder index)
    those two are None. ``download_mb`` stays the ``.abt`` bytes written: a
    ``.part`` keeps the whole tile's geometry on disk. ``raw_mb`` (XYZ
    sources only, else 0) is what downloading the missing tiles may add to
    the raw source-tile cache (:func:`raw_tile_bytes`, MiB rounded up) — on
    the cache drive too, on top of ``download_mb``. ``buildings_mb`` (with
    OpenFreeMap buildings) bounds the building tiles those builds fetch
    into the store (the z14 tiles of the missing tiles ×
    ``osm_buildings.TILE_BYTES_ESTIMATE``, the store not listed).
    *output_res_m* / *region_union*: as for :func:`prepare_terrain`.

    A pre-built ``.abt`` folder (:func:`is_abt_tile_dir`) is used as it is:
    nothing to build, so everything is 0 (``prebuilt`` True) — it used to be
    priced as a source to convert ("171 to build (~805 MB)").
    """
    bbox = _compute_sector_bbox(tx_lat, tx_lon, max_range_km, az_start, az_end)
    if is_abt_tile_dir(source):
        _log(f"  terrain plan: pre-built folder — nothing to build ({source})")
        return _prebuilt_plan(bbox)
    subtiles = _compute_subtiles(bbox, resolution_m, tx_lat, tx_lon,
                                 max_range_km, az_start, az_end)
    subtiles = _band_tiles(subtiles, tx_lat, tx_lon, min_range_km, skip_footprints)
    pool = _pool_dir(source, buildings)
    infos: Optional[List[Dict[str, Any]]] = None
    index: Optional[FootprintIndex] = None
    if source_infos is not None:
        infos, qgis_export, index = source_infos
        partial_ok = bool(infos) and not qgis_export
        if infos and (index is None or index.infos is not infos):
            index = footprint_index(infos)
    else:
        partial_ok = _partial_capable_source(source)
    regions = tile_regions(subtiles, bbox, resolution_m, output_res_m,
                           partial_ok)
    listing = _pool_listing(pool)
    missing: List[Dict[str, Any]] = []
    build: Dict[str, Optional[List[Dict[str, float]]]] = {}
    for t in subtiles:
        fp_of = ((lambda box, t=t: tile_source_fingerprint(
            infos, box, t["exact_res_m"], index=index)) if infos else None)
        verdict = resolve_pool_tile(
            pool, t, regions[t["filename"]], fp_of=fp_of, listing=listing,
            extra_regions=(region_union or {}).get(t["filename"]))
        if not verdict.hit:
            missing.append(t)
            build[t["filename"]] = verdict.regions
    partial = [t for t in missing if build[t["filename"]] is not None]
    tile_deg2 = sum(_bbox_area(_subtile_bbox(t)) for t in missing)
    build_deg2 = sum(
        sum(_bbox_area(r) for r in build[t["filename"]])
        if build[t["filename"]] is not None else _bbox_area(_subtile_bbox(t))
        for t in missing)
    source_files, source_mb = (_price_feeding_files(infos, index, missing,
                                                    build)
                               if infos else (None, None))
    # Only the shared XYZ downloader fetches source tiles (and caches them).
    raw = raw_tile_bytes(source, missing) if "type=xyz" in source else 0
    # OpenFreeMap building tiles the builds fetch (prepare_terrain's cover:
    # the whole missing tiles, a partial build's regions) — an upper bound
    # like the raw tiles: per extent its z14 rectangle (tiles shared by two
    # extents count twice), the store not listed (GUI thread; O(1) a tile).
    bld = 0
    if missing and buildings and _OSM_BUILDINGS_SOURCE_ID in buildings:
        from . import openfreemap
        from . import osm_buildings as osm_b
        bld = sum(openfreemap.estimate_tile_count(box)
                  for t in missing
                  for box in (build[t["filename"]] or [_subtile_bbox(t)])
                  ) * osm_b.TILE_BYTES_ESTIMATE
    return {
        "bbox": bbox,
        "tiles_total": len(subtiles),
        "tiles_cached": len(subtiles) - len(missing),
        "tiles_missing": len(missing),
        "tiles_partial": len(partial),
        "build_deg2": build_deg2,
        "tile_deg2": tile_deg2,
        "source_files": source_files,
        "source_mb": source_mb,
        "download_mb": _estimate_abt_disk_mb(missing),
        "raw_mb": -(-raw // (1024 * 1024)),
        "buildings_mb": -(-bld // (1024 * 1024)),
        "total_mb": _estimate_abt_disk_mb(subtiles),
        "footprints": {tile_footprint(t) for t in subtiles},
    }


def _prebuilt_plan(bbox: Optional[Dict[str, float]]) -> Dict[str, Any]:
    """:func:`terrain_plan`'s answer for a pre-built ``.abt`` folder."""
    plan: Dict[str, Any] = {k: 0 for k in _PLAN_SUM_KEYS}
    plan.update({"bbox": bbox, "source_files": None, "source_mb": None,
                 "footprints": set(), "prebuilt": True})
    return plan


def _price_feeding_files(infos: List[Dict[str, Any]],
                         index: Optional[FootprintIndex],
                         missing: List[Dict[str, Any]],
                         build: Dict[str, Optional[List[Dict[str, float]]]],
                         ) -> Tuple[int, int]:
    """``(files, MB)`` the builds of *missing* read: the contributors of
    each tile's regions to build (*build*; None = the whole tile), once
    each, sized from the folder index's ``size:mtime`` fingerprint."""
    feeding: Dict[int, Dict[str, Any]] = {}
    for t in missing:
        boxes = build[t["filename"]] or [_subtile_bbox(t)]
        for idx, info, _h in _region_contributors(infos, boxes,
                                                  t["exact_res_m"],
                                                  index=index):
            feeding[idx] = info
    size = 0
    for info in feeding.values():
        try:
            size += int(str(info.get("fp", "")).split(":")[0])
        except ValueError:
            pass
    return len(feeding), size // (1024 * 1024)


#: Summed by :func:`terrain_plan_profile` (``source_*`` stay None unless
#: every band could price its files).
_PLAN_SUM_KEYS = ("tiles_total", "tiles_cached", "tiles_missing",
                  "tiles_partial", "build_deg2", "tile_deg2",
                  "download_mb", "raw_mb", "buildings_mb", "total_mb")


def terrain_plan_profile(
    source: str,
    tx_lat: float,
    tx_lon: float,
    bands: List[Tuple[float, int]],
    az_start: float = 0.0,
    az_end: float = 360.0,
    buildings: Optional[str] = None,
    band_buildings: Optional[Sequence[bool]] = None,
    output_res_m: Optional[float] = None,
    source_infos: Optional[Tuple[List[Dict[str, Any]], bool,
                                 Optional[FootprintIndex]]] = None,
    region_union: Optional[Dict[str, List[Dict[str, float]]]] = None,
) -> Dict[str, Any]:
    """:func:`terrain_plan` for a range profile — the sum of its bands, each
    narrowed exactly as :func:`prepare_terrain_profile` builds it.

    *band_buildings* (one flag per band) prices a band without buildings
    against the building-free pool, as the run will build it; None = every
    band uses *buildings*. *output_res_m* / *source_infos* /
    *region_union*: as for :func:`terrain_plan` (a pre-built ``.abt``
    folder: nothing to build, all 0)."""
    if is_abt_tile_dir(source):
        _log(f"  terrain plan: pre-built folder — nothing to build ({source})")
        return _prebuilt_plan(_compute_sector_bbox(
            tx_lat, tx_lon, bands[-1][0] / 1000.0, az_start, az_end)
            if bands else None)
    total: Dict[str, Any] = {"bbox": None, "source_files": None,
                             "source_mb": None}
    total.update({k: 0 for k in _PLAN_SUM_KEYS})
    covered: set = set()
    inner_km = 0.0
    for idx, (until_m, step) in enumerate(bands):
        band_b = (buildings if band_buildings is None or band_buildings[idx]
                  else None)
        plan = terrain_plan(source, tx_lat, tx_lon, until_m / 1000.0, step,
                            az_start, az_end, band_b,
                            min_range_km=inner_km, skip_footprints=covered,
                            output_res_m=output_res_m,
                            source_infos=source_infos,
                            region_union=region_union)
        for key in _PLAN_SUM_KEYS:
            total[key] += plan.get(key, 0)
        for key in ("source_files", "source_mb"):
            if plan.get(key) is not None:
                total[key] = (total[key] or 0) + plan[key]
        total["bbox"] = plan["bbox"]
        covered = covered | plan["footprints"]
        inner_km = until_m / 1000.0
    return total


def terrain_size_warning(disk_mb: int,
                         cached_mb: int = 0,
                         source: Optional[str] = None,
                         ) -> Optional[Tuple[str, bool]]:
    """Return ``(message, strong)`` when *disk_mb* is big enough to confirm.

    *disk_mb* is what still has to be built; *cached_mb* is what is already
    pooled and is mentioned only so the number is not mistaken for the whole
    job.  ``strong`` is True past the 200 GB mark, where the caller should
    default the confirmation to "No".  Returns ``None`` for unremarkable sizes,
    and for a *source* that is a pre-built ``.abt`` folder (nothing to build).
    """
    if source and is_abt_tile_dir(source):
        _log(f"  terrain size: pre-built folder — nothing to build ({source})")
        return None
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

class ConverterCancelled(Exception):
    """Raised by an ``on_line`` callback to abort a streaming engine run.

    ``run_converter_streaming`` kills the process and re-raises, so the
    caller keeps its own cancel control flow.
    """


# ---------------------------------------------------------------------------
# Launch preflight
# ---------------------------------------------------------------------------
#
# Windows answers a missing DIRECTORY component with ERROR_PATH_NOT_FOUND,
# which Rust surfaces as "The system cannot find the path specified (os error
# 3)" and which names nothing at all. A run that dies that way leaves no way
# to tell whether the pool, the view, the job file's parent or a source's
# folder is the one that went missing. These helpers (a) re-create the
# directories the engine is about to write into immediately before the
# launch, so a dir that disappeared since it was first made is simply made
# again, and (b) fail with the offending PATH in the message when something
# the engine must read is genuinely absent.

def ensure_engine_dirs(*dirs: Optional[str]) -> None:
    """``makedirs(exist_ok=True)`` for every non-empty directory in *dirs*.

    Call it immediately before a job file is written or the engine is
    launched, however early the same directory was created: on Windows these
    have been observed to vanish between the two.
    """
    for d in dirs:
        if not d:
            continue
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"Terrain cache directory could not be created: {d} ({exc})"
            ) from exc


def _missing_path_detail(path: str) -> str:
    """``path``, plus the nearest ancestor that DOES exist — the whole point
    of the message is to say which directory went missing."""
    parent = os.path.dirname(os.path.abspath(path))
    probe = parent
    while probe and not os.path.isdir(probe):
        nxt = os.path.dirname(probe)
        if nxt == probe:
            break
        probe = nxt
    if os.path.isdir(parent):
        return f"{path} (its directory {parent} exists)"
    return f"{path} (its directory {parent} is MISSING; nearest existing "\
           f"ancestor: {probe or '<none>'})"


def _assert_job_file_readable(args: List[str]) -> None:
    """Raise if a ``--job-file`` / ``--config`` argument names a missing file.

    Both aether_converter subcommands and aether_core take their whole input
    as one file path; the engine reading it and finding nothing reports
    os error 3 with no path in it. Checked here — the single Popen site — so
    every caller gets the same named diagnosis.
    """
    for flag in ("--job-file", "--config"):
        if flag not in args:
            continue
        idx = args.index(flag)
        if idx + 1 >= len(args):
            continue
        path = args[idx + 1]
        if not os.path.isfile(path):
            raise RuntimeError(
                f"The engine job file vanished before launch: "
                f"{_missing_path_detail(path)}"
            )


def preflight_ingest_jobs(jobs: List[Dict[str, Any]]) -> None:
    """Check every path an ingest batch depends on, naming the first missing.

    Verifies that each job's ``output_path`` has a parent directory (making
    it if it is not there — the pool and the output folder are ours to
    create) and that every ``sources[]`` entry is really on disk. Without
    this the converter dies on the first one with os error 3 and says
    nothing about which path it was.

    Deliberately says nothing about ``buildings_file`` /
    ``buildings_pbf_dir``: buildings that cannot be read DEGRADE — one
    warning, tiles flagged for rebuild, terrain without them — and turning
    that into an abort here would break the contract prepare_terrain
    implements a few hundred lines down.

    Each distinct path is checked once: a folder file feeds every tile it
    touches (56,691 references to 43,650 files in a 2,610-tile batch), and
    every output of a batch shares one directory.

    The source checks run on :data:`PREFLIGHT_THREADS` threads: one
    ``os.path.exists`` is a disk round trip (the stat releases the GIL), and
    on the owner's HDD, busy with a download beside it, 30,000 of them in a
    row cost 42 s before Phase 2 of a Map Converter run and 12 s before a
    band — the whole gap between "Phase 2: converting" and "Launching".
    The first missing path is reported, as before.
    """
    seen_dirs: set = set()
    seen_srcs: set = set()
    paths: List[str] = []
    for job in jobs:
        out = job.get("output_path")
        if out:
            parent = os.path.dirname(os.path.abspath(out))
            if parent not in seen_dirs:
                seen_dirs.add(parent)
                ensure_engine_dirs(parent)
                if not os.path.isdir(parent):
                    raise RuntimeError(
                        f"Terrain output directory is missing and could not "
                        f"be created: {_missing_path_detail(out)}"
                    )
        for src in job.get("sources", []):
            path = src.get("path") if isinstance(src, dict) else src
            if not path or path in seen_srcs:
                continue
            seen_srcs.add(path)
            paths.append(path)
    missing = _first_missing(paths)
    if missing is not None:
        raise RuntimeError(
            f"Terrain source vanished before the converter ran: "
            f"{_missing_path_detail(missing)}"
        )


#: Threads for :func:`preflight_ingest_jobs`'s existence checks (I/O-bound
#: stats; 32 keeps a busy HDD's queue full without starving the converter).
PREFLIGHT_THREADS = 32
#: Below this many paths the checks run inline — a thread pool costs more
#: than it saves.
_PREFLIGHT_PARALLEL_MIN = 64


def _first_missing(paths: List[str]) -> Optional[str]:
    """The first of *paths* (in order) that does not exist, else None —
    checked in parallel beyond :data:`_PREFLIGHT_PARALLEL_MIN`."""
    if len(paths) < _PREFLIGHT_PARALLEL_MIN:
        for path in paths:
            if not os.path.exists(path):
                return path
        return None
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=min(PREFLIGHT_THREADS, len(paths)))
    try:
        for path, ok in zip(paths, pool.map(os.path.exists, paths,
                                            chunksize=64)):
            if not ok:
                return path
        return None
    finally:
        # A miss stops the walk; the remaining stats are dropped, not
        # awaited (Python < 3.9 has no cancel_futures: it waits — correct,
        # slower).
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            pool.shutdown(wait=True)


#: ``[Tile] <output file name> covered=<covered_px>/<total_px>`` — printed by
#: a batch ingest (aether_converter >= 0.2.7) once per successfully written
#: tile, flushed together with (and before) the progress line counting it.
#: Older converters print nothing: coverage unknown.
_TILE_LINE_RE = re.compile(r"\[Tile\]\s+(.+?)\s+covered=(\d+)/(\d+)\s*$")


def parse_tile_line(line: str) -> Optional[Tuple[str, int, int]]:
    """``(output file name, covered_px, total_px)`` of an ingest ``[Tile]``
    line, or None for any other line. ``covered_px`` counts the pixels at
    least one source supplied a sample for (void fill excluded), so
    ``covered == total`` means the tile has no hole."""
    m = _TILE_LINE_RE.search(line or "")
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3))


#: ``[Tile] <output file name> voids=<n>`` — aether_converter >= 0.2.11
#: (``binary_manager.CONVERTER_TILE_VOIDS_MIN_VERSION``, toolkit CONTRACT
#: changelog 46): the samples at or below the ``.abt`` validity floor
#: (-5000 counts = -2500 m, ``abt.MIN_VALID_ELEV_COUNTS``: the -9999 void
#: sentinel and anything the engine must not read as ground, deep water
#: included) in the file as written — the same rule :func:`_tile_has_voids`
#: applies. A download prints one per tile a NON-repair pass wrote (after
#: assembly and the building burn; a repair pass prints none), an ingest one
#: right after the tile's ``covered=`` line (after its void fill, so 0 with
#: ``void_fill_m``). :data:`_TILE_LINE_RE` never matches it (no ``covered=``).
_TILE_VOIDS_RE = re.compile(r"\[Tile\]\s+(.+?)\s+voids=(\d+)\s*$")


def parse_tile_voids_line(line: str) -> Optional[Tuple[str, int]]:
    """``(output file name, void samples)`` of a ``[Tile] … voids=`` line,
    or None for any other line (the ``covered=`` line included)."""
    m = _TILE_VOIDS_RE.search(line or "")
    if not m:
        return None
    return m.group(1), int(m.group(2))


def _parse_tile_voids(lines: Iterable[str]) -> Dict[str, int]:
    """``{output file name: void samples}`` of every ``voids=`` line in
    *lines* (a name printed twice: the last one; no lines: {})."""
    counts: Dict[str, int] = {}
    try:
        it = iter(lines or ())
    except TypeError:
        return counts
    for line in it:
        hit = parse_tile_voids_line(line) if isinstance(line, str) else None
        if hit is not None:
            counts[hit[0]] = hit[1]
    return counts


#: The converter's diagnostic lines (per-tile ``[Rust] tile …``, per-strip
#: ``[Strip …]``) go to a file, not the log panel: ``AETHER_DIAG_FILE``
#: (toolkit CONTRACT changelog 37, aether_converter >= 0.2.8; an older
#: converter ignores it and prints them — ``_run_converter`` drops the tile
#: lines). One file per day, ``<cache>/logs/aether_converter-<YYYYmmdd>.log``.
#: ``aether_core-<YYYYmmdd>.log`` beside it is the engine's full console
#: (:mod:`waveshed.core.engine_log`), rotated with the same rule.
DIAG_ENV = "AETHER_DIAG_FILE"
#: Diagnostics files older than this (mtime) are deleted.
DIAG_KEEP_DAYS = 14
_DIAG_FILE_RE = re.compile(r"^aether_(?:converter|core)-\d{8}\.log$")
#: What this session announced last: today's path, or "!<dir>" for a
#: directory that could not be made (each logged once).
_diag_announced: Optional[str] = None


def diag_file_path() -> str:
    """Today's converter diagnostics file (under :func:`get_cache_dir`)."""
    return os.path.join(get_cache_dir(), "logs",
                        f"aether_converter-{time.strftime('%Y%m%d')}.log")


def _rotate_diag_files(log_dir: str) -> None:
    """Delete the diagnostics files in *log_dir* older than
    :data:`DIAG_KEEP_DAYS` — only ``aether_converter-<date>.log`` (the cache
    root may hold other things, see :func:`cache_entries`). Never raises."""
    cutoff = time.time() - DIAG_KEEP_DAYS * 86400
    try:
        names = os.listdir(log_dir)
    except OSError as exc:
        _log(f"converter diagnostics: old files in {log_dir} not rotated ({exc})")
        return
    for name in names:
        if not _DIAG_FILE_RE.match(name):
            continue
        path = os.path.join(log_dir, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:  # noqa: PERF203 — in use / gone: keep going
            continue


def _with_diag_file(env: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
    """*env* for an engine child with :data:`DIAG_ENV` = today's
    :func:`diag_file_path` — unless the child's environment (*env*, else
    this process's) already sets it. The first launch of the session (and
    of a new day) creates the directory, rotates it and logs the path once;
    a directory that cannot be made leaves *env* as it is (the converter
    prints the lines, as before), logged once. Never raises."""
    global _diag_announced
    base = os.environ if env is None else env
    if DIAG_ENV in base:
        return env
    log_dir = ""
    try:
        path = diag_file_path()
        log_dir = os.path.dirname(path)
        os.makedirs(log_dir, exist_ok=True)
        if _diag_announced != path:
            _diag_announced = path
            _rotate_diag_files(log_dir)
            _log(f"converter diagnostics: {path}")
        out = dict(base)
        out[DIAG_ENV] = path
        return out
    except Exception as exc:  # noqa: BLE001 — diagnostics never stop a run
        if _diag_announced != "!" + log_dir:
            _diag_announced = "!" + log_dir
            _log(f"converter diagnostics: no file ({log_dir or 'cache dir'}: "
                 f"{exc}) — the converter prints them")
        return env


class StreamTiming(NamedTuple):
    """``time.perf_counter()`` stamps of one engine run of
    :func:`run_converter_streaming` (the download passes' timing lane)."""

    launched: float
    first_line: Optional[float]
    last_line: Optional[float]
    #: The process has exited and been reaped.
    exited: float

    @property
    def exit_after_last_line(self) -> Optional[float]:
        """Seconds from the last line read to the reaped exit (None when
        the engine printed nothing)."""
        return (None if self.last_line is None
                else max(0.0, self.exited - self.last_line))


#: Per thread: the :class:`StreamTiming` of its last finished engine run.
_STREAM_TIMING = threading.local()


def last_stream_timing() -> Optional[StreamTiming]:
    """The :class:`StreamTiming` of the last engine run THIS thread finished
    through :func:`run_converter_streaming` (None before one, after a run
    that raised, and after :func:`forget_stream_timing`)."""
    return getattr(_STREAM_TIMING, "last", None)


def forget_stream_timing() -> None:
    """Drop this thread's :func:`last_stream_timing` (before a run whose
    timing is wanted, so a stale record is never read for it)."""
    _STREAM_TIMING.last = None


def run_converter_streaming(exe: str, args: List[str], on_line: Any,
                            on_start: Any = None,
                            env: Optional[Dict[str, str]] = None) -> int:
    """THE engine subprocess runner — the single ``Popen`` site in the plugin.

    Runs ``exe *args`` with stdout+stderr merged and streams every non-empty
    (rstripped) line into *on_line*. *on_start(proc)* exposes the process so
    a worker can kill it on cancel; *on_line* may raise
    :class:`ConverterCancelled` to abort (the process is terminated, then the
    exception propagates). *env* replaces the environment when given (e.g.
    aether_core's license injection). Returns the process exit code.

    The child's environment gets ``AETHER_DIAG_FILE`` (the converter's
    per-tile/per-strip diagnostics file, :func:`_with_diag_file`) unless it
    already sets one — an explicit value always wins.

    Every engine invocation (aether_converter download/ingest, aether_core)
    goes through here so pipe handling, encoding and cancellation exist
    exactly once.

    Raises RuntimeError NAMING the missing path when the job/config file the
    arguments point at is not there at launch time — see
    :func:`_assert_job_file_readable`.

    The run's :class:`StreamTiming` (launch, first and last line, reaped
    exit — ``perf_counter``) is kept per thread for
    :func:`last_stream_timing` once it has ended normally.
    """
    _STREAM_TIMING.last = None
    _assert_job_file_readable(args)
    env = _with_diag_file(env)
    first: Optional[float] = None
    last: Optional[float] = None
    with subprocess.Popen(  # nosec B603 — engine binary from binary_manager, args are our own
        [exe, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True, encoding="utf-8", errors="replace",
        env=env,
        creationflags=_SUBPROCESS_FLAGS,
    ) as proc:
        launched = time.perf_counter()
        if on_start is not None:
            on_start(proc)
        try:
            for line in iter(proc.stdout.readline, ""):
                line = line.rstrip()
                if line:
                    last = time.perf_counter()
                    if first is None:
                        first = last
                    on_line(line)
        except ConverterCancelled:
            with contextlib.suppress(Exception):  # best-effort kill on cancel
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            raise
    _STREAM_TIMING.last = StreamTiming(launched, first, last,
                                       time.perf_counter())
    return proc.returncode


def kill_engine_process(proc: Any) -> None:
    """Stop one engine process at once and return at once — Stop calls this
    on the GUI thread (the Site Analysis and P2P workers' ``_kill_proc``).

    A process that has ended is left alone. POSIX: ``terminate()`` (SIGTERM),
    as always. Windows: ``kill()`` — TerminateProcess on the Popen's own
    handle, immediate, and never another process — then ``taskkill /F /T``
    for children a launcher may have started, fire-and-forget (output
    discarded, no console window; the blocking taskkill this replaces froze
    the GUI 0.1–1 s, more under antivirus). A daemon thread reaps taskkill
    and holds *proc* until then: Windows does not reuse a process ID while a
    handle to the process is open, so the PID taskkill gets cannot meanwhile
    name another process."""
    if proc.poll() is not None:
        return
    if not _is_windows():
        proc.terminate()
        return
    try:
        proc.kill()
    except OSError:
        pass
    try:
        taskkill = subprocess.Popen(  # nosec B603 B607 — Windows taskkill, fixed args + our child's PID
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=_no_window_flags())
    except OSError:
        return
    threading.Thread(target=_reap_taskkill, args=(taskkill, proc),
                     name="waveshed-taskkill", daemon=True).start()


def _reap_taskkill(taskkill: Any, _engine: Any) -> None:
    """Wait for :func:`kill_engine_process`'s taskkill off the GUI thread;
    *_engine* (its Popen, i.e. its process handle) is held until then."""
    try:
        taskkill.wait(timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass                        # best effort: nobody waits for it


# The converter's download progress line is a PARSED INTERFACE (see
# aether-tools docs/CONTRACT.md). Exact format, download.rs ~:747:
#   [Download] {pct}% ({done}/{total}) — {mb:.1} MB/s, {n} errors, {m} in-flight
_DL_PROGRESS_RE = re.compile(r"\[Download\]\s+(\d+)%\s+\((\d+)/(\d+)\)")


def _parse_download_progress(line: str) -> Optional[Tuple[int, int, int]]:
    """``(pct, done, total)`` from a ``[Download] X% (a/b) — …`` line, or None."""
    m = _DL_PROGRESS_RE.search(line)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


class DownloadProgressAggregator:
    """Download progress of one ``_try_rust_download`` call → one 0..1.

    Work is counted in XYZ **source tiles**, the unit the converter reports.
    Each zoom group starts with its first pass (exact count from the tile
    geometry, :func:`_source_tile_count` — the converter's own formula) plus a
    small *tail* that is only released when the group's outcome is known; a
    gap-repair retry pass ADDS its tiles when it starts. The fraction follows
    :class:`run_progress.WorkProgress` (remaining-work rule): it never goes
    backwards, it moves with every reported tile — retry passes included —
    and it reaches 1.0 only when every group is finished. (The old max-only
    per-group fraction sat at 100 % of a group while its retry passes ran.)

    ``group_update`` is the original per-group-fraction interface, kept for
    callers that only know fractions.
    """

    #: Share of a group held back until the group's outcome is known — the
    #: room a retry pass moves the bar in after the first pass hit 100 %.
    TAIL_SHARE = 0.01

    def __init__(self, group_weights: List[int],
                 group_units: Optional[List[float]] = None) -> None:
        self._w = [max(0, int(w)) for w in group_weights]
        self._total = sum(self._w) or 1
        self._fracs = [0.0] * len(self._w)
        units = (list(group_units) if group_units is not None
                 else [float(w) for w in self._w])
        self._work = run_progress.WorkProgress()
        self._pass: Dict[int, int] = {}
        self._pass_done: Dict[int, int] = {}
        self._pass_total: Dict[int, int] = {}
        for gi, u in enumerate(units):
            self._work.set_work(("pass", gi, 1), max(0.0, float(u)))
            self._work.set_work(("tail", gi),
                                max(1.0, math.ceil(self.TAIL_SHARE * u)))

    # -- legacy fraction interface ------------------------------------------
    def group_update(self, group_idx: int, frac: float) -> float:
        """Set group *group_idx* progress to *frac*; return overall 0..1."""
        frac = min(max(frac, 0.0), 1.0)
        self._fracs[group_idx] = max(self._fracs[group_idx], frac)
        return sum(w * f for w, f in zip(self._w, self._fracs)) / self._total

    # -- pass interface (source tiles) --------------------------------------
    def pass_start(self, gi: int, pass_no: int, est_tiles: float) -> None:
        """Pass *pass_no* of group *gi* starts; a retry ADDS its work."""
        self._pass[gi] = pass_no
        self._pass_done[gi] = 0
        self._pass_total[gi] = int(est_tiles)
        self._work.set_work(("pass", gi, pass_no), max(0.0, float(est_tiles)))

    def pass_total(self, gi: int, total: int) -> None:
        """The converter stated the pass total (header or progress line)."""
        self._pass_total[gi] = int(total)
        self._work.set_work(("pass", gi, self._pass.get(gi, 1)), float(total))

    def pass_progress(self, gi: int, done: int, total: int) -> float:
        if total != self._pass_total.get(gi):
            self.pass_total(gi, total)
        self._pass_done[gi] = max(self._pass_done.get(gi, 0), int(done))
        key = ("pass", gi, self._pass.get(gi, 1))
        nxt = run_progress.next_download_report(self._pass_done[gi], total)
        return self._work.set_done(key, done, ceiling=float(nxt))

    def pass_end(self, gi: int) -> float:
        """The pass's process ended: whatever it did not report is over."""
        return self._work.finish(("pass", gi, self._pass.get(gi, 1)))

    def group_end(self, gi: int) -> float:
        """The group's outcome is known (complete, partial or failed)."""
        for p in range(1, self._pass.get(gi, 1) + 1):
            self._work.finish(("pass", gi, p))
        return self._work.finish(("tail", gi))

    def fraction(self) -> float:
        return self._work.fraction()

    def info(self, gi: int) -> Dict[str, Any]:
        """Unit counts for an event sink (see ``_try_rust_download``)."""
        key = ("pass", gi, self._pass.get(gi, 1))
        done = self._work.done_units
        pd = self._pass_done.get(gi, 0)
        pt = self._pass_total.get(gi, 0)
        ceiling = done
        if pt and self._work.done(key) < self._work.work(key):
            ceiling = done + max(0, run_progress.next_download_report(pd, pt)
                                 - pd)
        return {
            "done_units": done,
            "total_units": self._work.total_units,
            "ceiling_units": ceiling,
            "pass": self._pass.get(gi, 1),
            "pass_done": pd,
            "pass_total": pt,
        }


def _source_tile_count(specs: List[Dict[str, Any]], zoom: int) -> int:
    """XYZ source tiles one download pass over *specs* at *zoom* fetches.

    The converter's own formula (download.rs ``run_download_async``: union
    bbox of the job's tiles, ``lon2tx``/``lat2ty`` floor, nx·ny), so the bar
    knows a pass's size before its header line arrives.
    """
    if not specs:
        return 0
    s_ = min(t["ul_lat"] - t["size_px"] * t["resolution_m"] / 111_111.0
             for t in specs)
    n_ = max(t["ul_lat"] for t in specs)
    w_ = min(t["ul_lon"] for t in specs)
    e_ = max(t["ul_lon"] + t["size_px"] * t["resolution_m"] / 111_111.0
             for t in specs)
    n = 2.0 ** zoom

    def tx(lon: float) -> int:
        return int(max(0.0, math.floor((lon + 180.0) / 360.0 * n)))

    def ty(lat: float) -> int:
        r = math.radians(max(min(lat, 89.9999), -89.9999))
        return int(max(0.0, math.floor(
            (1.0 - math.asinh(math.tan(r)) / math.pi) / 2.0 * n)))

    return (tx(e_) - tx(w_) + 1) * (ty(s_) - ty(n_) + 1)


def _download_specs_by_zoom(source: str, subtiles: List[Dict[str, Any]]
                            ) -> Dict[int, List[Dict[str, Any]]]:
    """Download specs grouped by the zoom each tile needs at its OWN latitude
    (see the note in ``_try_rust_download``)."""
    z_max, _warning = resolve_zmax(source)
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
    return by_zoom


#: Bytes one XYZ source tile is taken to occupy in the raw source-tile cache
#: (:func:`xyz_cache_dir`) when the disk need is priced — an UPPER bound:
#: the converter's own per-tile figure behind the ``(~N MB)`` of its
#: ``[Download] z=`` header (download.rs ``tile_size_kb = 200``, a guess
#: "high" by its own comment). Measured: AWS Terrarium z15, 256 px PNG,
#: 389 MB / 4,690 tiles = 83 KB a tile (toolkit CONTRACT changelog 36);
#: 512 px services (Mapterhorn, MapTiler) carry four times the samples.
XYZ_SOURCE_TILE_BYTES = 200 * 1024


class _TileGrid:
    """Web-mercator tile indices and edges at one zoom, computed as
    download.rs does (``lon2tx``/``lat2ty`` floor, ``tx2lon``/``ty2lat``;
    same floats). Row edges are kept: neighbouring specs test the same
    tile-rows, and the GUI thread prices thousands of tiles per refresh."""

    def __init__(self, zoom: int) -> None:
        self.n = 2.0 ** zoom
        self._row_edge: Dict[int, float] = {}

    def lon2tx(self, lon: float) -> int:
        return int(max(0.0, math.floor((lon + 180.0) / 360.0 * self.n)))

    def lat2ty(self, lat: float) -> int:
        return int(max(0.0, math.floor(
            (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi)
            / 2.0 * self.n)))

    def tx2lon(self, x: int) -> float:
        return x / self.n * 360.0 - 180.0

    def ty2lat(self, y: int) -> float:
        lat = self._row_edge.get(y)
        if lat is None:
            lat = self._row_edge[y] = math.degrees(math.atan(math.sinh(
                math.pi * (1.0 - 2.0 * y / self.n))))
        return lat

    def touched(self, ul_lat: float, ul_lon: float, span: float,
                bounds: Tuple[int, int, int, int]
                ) -> Optional[Tuple[int, int, int, int]]:
        """``(x_first, x_last, y_first, y_last)`` of the tiles a spec
        touches (download.rs ``tile_touches_spec``: strict overlap), within
        the job's rectangle *bounds* ``(x0, x1, y0, y1)``; None for none.
        Its own range plus one tile of slack per side (the floor rounding)
        is scanned; the test is separable, so the result is a rectangle."""
        x0, x1, y0, y1 = bounds
        xs = [x for x in range(max(self.lon2tx(ul_lon) - 1, x0),
                               min(self.lon2tx(ul_lon + span) + 1, x1) + 1)
              if self.tx2lon(x) < ul_lon + span
              and self.tx2lon(x + 1) > ul_lon]
        ys = [y for y in range(max(self.lat2ty(ul_lat) - 1, y0),
                               min(self.lat2ty(ul_lat - span) + 1, y1) + 1)
              if self.ty2lat(y + 1) < ul_lat
              and self.ty2lat(y) > ul_lat - span]
        return (xs[0], xs[-1], ys[0], ys[-1]) if xs and ys else None


def _row_union_size(rows: Dict[int, List[Tuple[int, int]]]) -> int:
    """Cells covered by per-row column intervals ``{row: [(first, last)]}``
    (inclusive, overlapping or not)."""
    count = 0
    for spans in rows.values():
        spans.sort()
        lo, hi = spans[0]
        for a, b in spans[1:]:
            if a > hi + 1:
                count += hi - lo + 1
                lo, hi = a, b
            else:
                hi = max(hi, b)
        count += hi - lo + 1
    return count


def _fetch_tile_count(specs: List[Dict[str, Any]], zoom: int) -> int:
    """XYZ source tiles one ``download`` pass over *specs* at *zoom* FETCHES:
    the tiles of the job's bounding rectangle that touch at least one output
    tile (aether_converter >= 0.2.7, toolkit CONTRACT changelog 30) — the
    total of its ``[Download] z=`` header. Mirrors download.rs
    ``tiles_to_fetch`` (:class:`_TileGrid`); the touched tiles of one spec
    form a rectangle, so the union is counted per tile-row from merged
    column intervals. :func:`_source_tile_count` is the whole rectangle —
    what the progress estimate counts before that header arrives."""
    if not specs:
        return 0
    grid = _TileGrid(zoom)
    geo = [(s["ul_lat"], s["ul_lon"],
            s["size_px"] * (s["resolution_m"] / 111_111.0)) for s in specs]
    bounds = (grid.lon2tx(min(lon for _lat, lon, _sp in geo)),
              grid.lon2tx(max(lon + sp for _lat, lon, sp in geo)),
              grid.lat2ty(max(lat for lat, _lon, _sp in geo)),
              grid.lat2ty(min(lat - sp for lat, _lon, sp in geo)))
    rows: Dict[int, List[Tuple[int, int]]] = {}
    for ul_lat, ul_lon, span in geo:
        rect = grid.touched(ul_lat, ul_lon, span, bounds)
        if rect is not None:
            for y in range(rect[2], rect[3] + 1):
                rows.setdefault(y, []).append((rect[0], rect[1]))
    return _row_union_size(rows)


def raw_tile_bytes(source: str, subtiles: List[Dict[str, Any]]) -> int:
    """Upper bound of the bytes the raw source-tile cache grows by when the
    download fetches *subtiles* (the output tiles missing from the pool):
    per zoom group (:func:`_download_specs_by_zoom`, the downloader's own
    jobs) the source tiles that pass fetches (:func:`_fetch_tile_count`)
    × :data:`XYZ_SOURCE_TILE_BYTES`. Tiles the cache already holds cost
    nothing in reality, but the plugin never reads the packs, so they are
    counted too. 0 for no tiles."""
    if not subtiles:
        return 0
    return XYZ_SOURCE_TILE_BYTES * sum(
        _fetch_tile_count(specs, z)
        for z, specs in _download_specs_by_zoom(source, subtiles).items())


def estimate_pool_download_units(
    source_uri: str,
    subtiles: List[Dict[str, Any]],
    pool_check: bool = True,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> float:
    """Source-tile units ``ensure_pool_tiles`` will report for *subtiles*.

    Tiles already in the pool cost nothing; the rest are counted per zoom
    group exactly as the download progress counts them (first pass + tail).
    *pool_check* lists the pool — call it off the GUI thread; with False the
    pure-geometry upper bound is returned (every tile missing). The check is
    ONE directory listing with sizes (:func:`_pool_listing`, no header read
    per tile) under :func:`_tile_ready`'s rule. Never raises, except
    :class:`ConverterCancelled` once *should_cancel()* is true — polled per
    tile, so thousands of them must not hold up a Cancel.
    """
    try:
        todo = list(subtiles)
        if pool_check:
            pool_dir = _pool_dir(source_uri)
            listing = _pool_listing(pool_dir)
            missing = []
            for t in todo:
                if should_cancel is not None and should_cancel():
                    raise ConverterCancelled()
                if not _listed_tile_ready(listing, t, pool_dir):
                    missing.append(t)
            todo = missing
        if not todo:
            return 0.0
        total = 0.0
        for z, specs in _download_specs_by_zoom(source_uri, todo).items():
            u = float(_source_tile_count(specs, z))
            total += u + max(1.0, math.ceil(
                DownloadProgressAggregator.TAIL_SHARE * u))
        return total
    except ConverterCancelled:
        raise                      # a Cancel is not an estimate failure
    except Exception:  # noqa: BLE001 — an estimate must never fail a run
        return float(len(subtiles))


#: Every how many per cent a download pass's ``[Download] N%`` progress line
#: reaches the log panel (:func:`_run_converter_download_once`).
DOWNLOAD_PANEL_STEP = 10

#: The panel tag of the current thread (``_PANEL_TAG.tag``): a prefix on
#: every download line the panel gets while a caller runs several converter
#: downloads at once (the Map Converter's two lanes per pool, 2026-11-07,
#: owner: the z14 pass's ``10% (575/5742)`` interleaved with the z15 pass's
#: ``10% (8113/81127)`` with nothing to tell them apart). Per thread, so
#: each lane tags its own lines; the raw lines (``lines``, *on_line*) never
#: carry it. Site Analysis sets none.
_PANEL_TAG = threading.local()


def set_panel_tag(tag: Optional[str]) -> None:
    """Prefix the panel copy of this thread's download lines with *tag*
    (e.g. ``"[z14] "``); None or "" removes it."""
    _PANEL_TAG.tag = tag or ""


def panel_tag() -> str:
    """This thread's panel tag, "" when none is set."""
    return getattr(_PANEL_TAG, "tag", "") or ""


def _run_converter_download_once(
    converter_exe: str, job_file: str, on_line: Any = None,
    should_cancel: Any = None, on_start: Any = None,
) -> Tuple[int, List[str]]:
    """Run one ``aether_converter download`` pass, streaming output to the log.

    Returns ``(returncode, lines)``. *on_line*, when given, sees every line
    too (progress parsing). *should_cancel()* is polled per output line and
    raises :class:`ConverterCancelled` (the shared runner then kills the
    process); *on_start(proc)* exposes the process so a worker can kill it
    directly. Raises RuntimeError on a non-recoverable disk-space error (no
    point retrying or falling back).
    """
    lines: List[str] = []

    def handle(line: str) -> None:
        if should_cancel is not None and should_cancel():
            raise ConverterCancelled()
        # The converter prints a progress line per percent (100 a pass,
        # 2,000 in one of the owner's sessions): the panel gets every
        # DOWNLOAD_PANEL_STEP per cent and the last one; *lines* and
        # *on_line* (the progress bar, the ETA) still see them all, and a
        # converter >= 0.2.11 also writes them to its diagnostics file.
        prog = _parse_download_progress(line)
        if parse_tile_voids_line(line) is not None:
            pass        # one per output tile (0.2.11): read, never shown
        elif (prog is None or prog[0] % DOWNLOAD_PANEL_STEP == 0
                or prog[1] >= prog[2]):
            _log(f"    {panel_tag()}{line}")
        lines.append(line)
        if on_line is not None:
            on_line(line)

    rc = run_converter_streaming(
        converter_exe, ["download", "--job-file", job_file], handle,
        on_start=on_start)

    if rc != 0 and should_cancel is not None and should_cancel():
        # cancel() killed the process (on_start) before a line reached the
        # per-line check: the exit code is the kill, not a failure to retry.
        raise ConverterCancelled()
    if rc != 0:
        text = " ".join(lines).lower()
        if "insufficient disk" in text or "not enough space" in text:
            detail = lines[-1] if lines else "unknown"
            raise RuntimeError(
                f"Insufficient disk space for terrain download. {detail}"
            )
    return rc, lines


def _converter_repair_min_version() -> str:
    from .binary_manager import CONVERTER_REPAIR_MIN_VERSION
    return CONVERTER_REPAIR_MIN_VERSION


def _abt_block_flags(path: str, block: int = 64) -> Tuple[bool, bool]:
    """``(has a fully-VOID block, has a fully-zero block)`` of the .abt's
    *block*×*block* pixel blocks — ONE read of the file, one min/max pass
    (10 ms for a 2 m tile against 27 ms for the two predicates). ``(False,
    False)`` for a file it cannot open or parse."""
    try:
        import numpy as np
        from . import abt
        with open(path, "rb") as fh:
            head = fh.read(44)
            if len(head) < 44 or head[:4] != b"AETH":
                return False, False
            size = int.from_bytes(head[6:8], "little")
            stride = int.from_bytes(head[42:44], "little")
            body = np.frombuffer(fh.read(), dtype=np.uint8)
        need = size * stride
        if size < block or body.size < need:
            return False, False
        rows = body[:need].reshape(size, stride)
        elev = np.ascontiguousarray(rows[:, : size * 2]).view(np.int16)
        h = (size // block) * block
        nb = h // block
        strips = elev[:h, :h].reshape(nb, block, h)
        top = strips.max(axis=1).reshape(nb, nb, block).max(axis=2)
        low = strips.min(axis=1).reshape(nb, nb, block).min(axis=2)
        return (bool((top <= abt.MIN_VALID_ELEV_COUNTS).any()),
                bool(((low == 0) & (top == 0)).any()))
    except Exception:
        return False, False


#: The answer one :func:`_abt_block_flags` read gave and its caller did not
#: ask for, kept for the other question on the SAME unchanged file (path,
#: size, mtime_ns) and taken by it: the gap check asks
#: :func:`_abt_has_holes` then :func:`_abt_has_zero_fill` of one tile — one
#: read, where each used to read the whole tile (the owner's 706 × 62 MB
#: z15 band: 44 GB twice). Per thread.
_block_scan_memo = threading.local()


def _abt_block_answer(path: str, block: int, which: int) -> bool:
    """Flag *which* (0 holes, 1 zero fill) of :func:`_abt_block_flags`."""
    try:
        st = os.stat(path)
        ident = (os.path.abspath(path), block, st.st_size, st.st_mtime_ns)
    except OSError:
        return False
    kept = getattr(_block_scan_memo, "kept", None)
    _block_scan_memo.kept = None
    if kept is not None and kept[0] == ident and kept[1] == which:
        return kept[2]
    flags = _abt_block_flags(path, block)
    _block_scan_memo.kept = (ident, 1 - which, flags[1 - which])
    return flags[which]


def _abt_has_zero_fill(path: str, block: int = 64) -> bool:
    """True if the .abt contains a fully-zero block of *block*×*block* pixels.

    A converter OLDER than the missing-tile fix wrote a failed XYZ tile as
    0 m marked real, so a zero block is the signature of failure fill from
    such a build. Kept as the honesty backstop (the torture suite calls it);
    genuine sea-level 0 (ocean) also trips it, which is why it only ever
    flags, never deletes. Shares its read with :func:`_abt_has_holes` of
    the same file (:func:`_abt_block_answer`).
    """
    return _abt_block_answer(path, block, 1)


def _abt_has_holes(path: str, block: int = 64) -> bool:
    """True if the .abt contains a fully-VOID block of *block*×*block* pixels.

    The converter writes a *failed* XYZ tile as the -9999 void sentinel, so
    a failed tile leaves a void block. Used to pick which sub-tiles to
    re-download. Genuine no-coverage voids (past a fixture's edge, ocean in
    a bathymetry-less set) also trip it, but the retry loop is bounded by
    its own pass count, so a legitimately-void tile can't loop forever.
    Shares its read with :func:`_abt_has_zero_fill` of the same file.
    """
    return _abt_block_answer(path, block, 0)


#: Lines of ``aether_converter download`` output that say WHY a run failed:
#: anyhow's top-level ``Error:`` (plus the ``Caused by:`` chain under it) and
#: the engine's own error tally, ``[Stats] ERRORS (12): decode=12``. Everything
#: else it prints is progress.
_DOWNLOAD_CAUSE_RE = re.compile(
    r"^\s*(?:error:|caused by:|\[stats\]\s+errors)", re.IGNORECASE)


def _download_failure_detail(lines: List[str], keep: int = 6) -> str:
    """The engine's own words for why a download failed, or "".

    The converter diagnoses the failure precisely — "decode=12" for a service
    serving WebP to a PNG-only decoder — and the plugin used to throw all of it
    away and raise one constant string about tiles that "could not be fetched",
    which sends the user to look at their network instead of at the codec.
    Same shape as the ingest path's ``_run_converter``: the cause if the engine
    named one, else the tail of what it said.
    """
    causes = [ln for ln in lines if _DOWNLOAD_CAUSE_RE.match(ln)]
    return "\n".join((causes or lines)[-keep:])


class DownloadOutcome(NamedTuple):
    """Did the download produce usable tiles, and if not, what did the engine say?

    Truthy exactly when it worked, so every ``if _try_rust_download(...)``
    caller reads as it always did.
    """

    ok: bool
    detail: str = ""
    #: The void-count records of the pool tiles this download wrote (name →
    #: record, see :data:`TILES_MANIFEST_SCHEMA`; also written into the
    #: pool's manifest) — None when no pass printed a count.
    voids: Optional[Dict[str, Dict[str, int]]] = None

    def __bool__(self) -> bool:
        return bool(self.ok)


#: Acquisition routes terrain has ACTUALLY been acquired through since the
#: last :func:`pop_acquisition_routes` — "download" (shared Rust XYZ
#: downloader), "render" (per-tile QGIS export of a rendered server),
#: "sources" (local files handed to the converter), "terrain_dir" (a
#: user-supplied .abt folder). Appended by the one primitive that owns each
#: route, so a run that silently changed path is visible to the torture
#: suite: terrain that exists is not the same as terrain that came the way
#: the catalogue intended.
_ACQUISITION_ROUTES: List[str] = []


def _record_route(route: str) -> None:
    if len(_ACQUISITION_ROUTES) < 256:
        _ACQUISITION_ROUTES.append(route)


def _source_route(source: str, dem_layer: Any) -> str:
    """The router-contract route for *source* (the only route its pool can
    have been filled by — the XYZ render fallback is a hard error now)."""
    if "type=xyz" in source:
        return "download"
    src = source
    with contextlib.suppress(Exception):  # routing must not depend on the layer
        if dem_layer is not None:
            src = dem_layer.source() or source
    base = src.partition("|")[0]
    if os.path.isfile(base) or src.startswith("/vsi"):
        return "sources"
    return "render"


def pop_acquisition_routes() -> List[str]:
    """The routes recorded since the last call, clearing the record.

    Diagnostic surface for the torture runner (``pipeline.route``): it pops
    before a worker run and after it, and fails the row when the set of
    routes is not exactly what the catalogue declares — a fallback that
    still produced terrain is a failed test, not a passed one.
    """
    out = list(_ACQUISITION_ROUTES)
    _ACQUISITION_ROUTES.clear()
    return out


#: Absolute terrain directories :func:`prepare_terrain` has handed the engine
#: since the last :func:`pop_prepared_view_dirs` — one entry per successful
#: return, whichever way it came (a cache hit, a download, an ingest, or a
#: user-supplied .abt folder passed straight through). Capped the same way as
#: ``_ACQUISITION_ROUTES``: a long QGIS session must not grow a list forever.
_PREPARED_VIEW_DIRS: List[str] = []


def _record_view_dir(view_dir: str) -> None:
    if len(_PREPARED_VIEW_DIRS) < 256:
        _PREPARED_VIEW_DIRS.append(os.path.abspath(view_dir))


def pop_prepared_view_dirs() -> List[str]:
    """The terrain directories recorded since the last call, clearing them.

    Diagnostic surface for the torture runner: it pops before a driven run
    and reads the LAST entry after it, which is the directory aether_core was
    actually pointed at — so a row can inspect the .abt tiles the engine read
    rather than guessing at the cache key that produced them.
    """
    out = list(_PREPARED_VIEW_DIRS)
    _PREPARED_VIEW_DIRS.clear()
    return out


#: Printed by ``aether_converter download`` once per run when it has fused a
#: ``buildings_pbf_dir``. Its absence is how we detect an engine that predates
#: the feature — see ``_try_rust_download``.
_BUILDINGS_APPLIED_MARKER = "[Buildings]"


def _buildings_were_applied(lines: List[str]) -> bool:
    """True if the converter reported fusing buildings during this pass."""
    return any(_BUILDINGS_APPLIED_MARKER in ln for ln in lines)


#: What ``aether_converter ingest`` says when it cannot use a
#: ``buildings_file`` — an unreadable format ("Missing magic bytes. Is this an
#: fgb file?"), a path that is not there, a geometry type it does not draw.
#: Current engines fail the run outright, so ``_run_converter`` raises before
#: this is ever consulted; older ones warned and exited 0, and the plugin has
#: to work against whichever engine is deployed.
_BUILDINGS_FAILED_MARKER = "failed to apply buildings"


def _buildings_complaint(lines: List[str]) -> Optional[str]:
    """The converter's own complaint about a buildings burn, if it made one.

    An older engine exits 0 either way — a job whose buildings could not be
    read still writes perfectly good building-free terrain — so this line is
    all that separates "buildings applied" from "buildings silently dropped".
    """
    for line in lines:
        if _BUILDINGS_FAILED_MARKER in line.lower():
            return line.strip()
    return None


#: Marker on the per-tile line ``aether_converter ingest`` prints for every
#: ``buildings_file`` burn (ingest.rs ``apply_buildings``). The PBF path uses
#: ``[Info] buildings_pbf_dir:`` instead, so within one ingest run these lines
#: are the buildings-file report and nothing else.
_BUILDINGS_REPORT_MARKER = "[Buildings]"
#: How many pixels that tile's burn actually raised.
_BUILDINGS_RAISED_RE = re.compile(r"(\d[\d,]*)\s+px raised")


def _buildings_drew_nothing(lines: List[str]) -> Optional[str]:
    """The converter's report when a burn raised no pixel anywhere, else None.

    Judged over the WHOLE run, never per tile: the edge tiles of any area lie
    outside the footprints and legitimately draw nothing, so failing on those
    would flag every ordinary run for rebuild. If not one tile was raised,
    though, the burn did not happen — the source may be readable and still
    carry no usable height — and the pool identity would otherwise claim
    buildings these pixels do not have.

    Returns None when the engine reported nothing at all: older builds printed
    nothing on a successful burn, and silence cannot be read either way.
    """
    reports = [ln for ln in lines if _BUILDINGS_REPORT_MARKER in ln]
    if not reports:
        return None
    for line in reports:
        raised = _BUILDINGS_RAISED_RE.search(line)
        if raised and int(raised.group(1).replace(",", "")) > 0:
            return None
    return reports[-1].strip()


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


class _PassTiming:
    """The download's timing lane (2026-10-23): ONE Info line per converter
    pass, logged when the next pass is about to launch (or, for the last
    one, when the download ends)::

        timing: z15 pass 1: converter exit +0.3s after its last line;
        post-pass parse 0.0s, flags 0.0s, degraded/repair plan 228.4s,
        next launch 0.0s

    — where the time between the converter's last line and the next launch
    went: the reaped exit (:class:`StreamTiming`), reading the pass's lines
    (completeness, ``[Degraded]`` mapping, buildings), the rebuild flags /
    dropped tiles, the gap scan and the repair plan, and the next job's
    preparation. The owner's run had 3 min 48 s of silence between a
    degraded z15 pass and its repair pass that no log line explained.
    ``perf_counter`` only; the line is never parsed.

    :meth:`ended` opens a pass's record (its clock starts at the exit),
    :meth:`mark` books the time since the previous mark to one step,
    :meth:`launching` logs the open record (*now* = the next launch, None =
    no next pass in this download)."""

    STEPS = ("parse", "flags", "plan")

    def __init__(self) -> None:
        self._rec: Optional[Dict[str, Any]] = None

    def ended(self, zoom: int, pass_no: int, repair: bool,
              stream: Optional[StreamTiming]) -> None:
        self.launching(None)
        start = stream.exited if stream is not None else time.perf_counter()
        self._rec = {"zoom": zoom, "pass": pass_no, "repair": repair,
                     "exit": (stream.exit_after_last_line
                              if stream is not None else None),
                     "silent": stream is not None and stream.last_line is None,
                     "mark": start, **{s: 0.0 for s in self.STEPS}}

    def mark(self, step: str) -> None:
        rec = self._rec
        if rec is None:
            return
        now = time.perf_counter()
        rec[step] += max(0.0, now - rec["mark"])
        rec["mark"] = now

    def launching(self, now: Optional[float]) -> None:
        rec, self._rec = self._rec, None
        if rec is None:
            return
        if rec["exit"] is not None:
            exit_text = f"+{rec['exit']:.1f}s after its last line"
        elif rec["silent"]:
            exit_text = "with no line printed"
        else:
            exit_text = "+? after its last line"
        nxt = ("—" if now is None
               else f"{max(0.0, now - rec['mark']):.1f}s")
        kind = "repair pass" if rec["repair"] else "pass"
        _log(f"  timing: z{rec['zoom']} {kind} {rec['pass']}: converter exit "
             f"{exit_text}; post-pass parse {rec['parse']:.1f}s, flags "
             f"{rec['flags']:.1f}s, degraded/repair plan {rec['plan']:.1f}s, "
             f"next launch {nxt}")


def _try_rust_download(
    source: str,
    cache_dir: str,
    bbox: Dict[str, float],
    resolution_m: int,
    subtiles: List[Dict[str, Any]],
    binary_manager: Any,
    pbf_dir: Optional[str] = None,
    progress_cb: Any = None,
    should_cancel: Any = None,
    on_start: Any = None,
    burns: Any = None,
    pbf_ready: Optional[str] = None,
    pbf_wait_s: Optional[float] = None,
    pbf_list: Optional[str] = None,
) -> DownloadOutcome:
    """Download XYZ tiles via Rust and produce .abt files directly.

    *pbf_dir*, when given, is handed to the converter as ``buildings_pbf_dir``
    so buildings are fused during the download instead of forcing the whole run
    onto the QGIS export + ingest path (which is 7-17x slower).

    *pbf_ready* / *pbf_wait_s* (with *pbf_dir*; aether_converter >= 0.2.10,
    toolkit CONTRACT changelog 45 — the caller gates): every pass carries
    them as ``buildings_pbf_ready`` / ``buildings_pbf_wait_s``, so the
    directory may still be filling while the terrain downloads — the
    converter reads it only at its buildings post-pass, once the marker
    file is there (``[Buildings] waiting for the building tiles (…)`` /
    ``… ready after S s``, logged like every line). Repair passes ignore
    both (their run's post-pass already saw the marker).

    *pbf_list* (with *pbf_dir* = THE store; aether_converter >= 0.2.14,
    toolkit CONTRACT changelog 51 — the caller gates) instead of
    *pbf_ready*: every pass carries it as ``buildings_pbf_list`` (+
    ``buildings_pbf_wait_s``): the converter decodes only the tiles that
    file names, from the store, and waits for the file as for the marker;
    repair passes ignore it. Never both (the converter refuses the pair).

    *burns* (an ``osm_buildings.BurnMonitor``, with *pbf_dir*) reads the
    lines of every pass (one ``[Buildings] … pbf tile(s)`` report owed per
    pass that exits 0; none by a repair pass); a pass that warned it could
    not decode building tiles stops the download
    (``UndecodableBuildings``) — the caller judges the rest
    (``BurnMonitor.verdict``).

    That field is **additive**, so a converter predating it parses the job
    happily, ignores the field, and writes perfectly good building-*free*
    terrain — which would then be cached under a pool identity claiming it has
    buildings. To stop that, a pass that requested buildings and saw no
    ``[Buildings]`` line in the output is treated as a failed fast path: the
    tiles are flagged for rebuild and we fall back to the ingest path.

    Returns a :class:`DownloadOutcome`: falsy when the tiles could not be
    produced, carrying the engine's own explanation for the caller to raise.
    """
    # Each of the three bail-outs below used to return False without a word,
    # and the caller printed one generic "Rust download unavailable" for all of
    # them — so a missing engine looked exactly like an unsupported layer, and
    # the run then spent the whole extract on the slow path before anything
    # mentioned it. Say which one it was.
    if pbf_ready and pbf_list:
        raise ValueError("buildings_pbf_ready and buildings_pbf_list are "
                         "exclusive (toolkit CONTRACT changelog 51)")
    if "type=xyz" not in source:
        reason = ("the DEM layer is not an XYZ tile service (only XYZ can "
                  "be fetched directly)")
        _log(f"  fast download skipped: {reason}")
        return DownloadOutcome(False, reason)

    params = dict(urllib.parse.parse_qsl(source))
    url_raw = params.get("url", "")
    if not url_raw:
        reason = f"no 'url' in the layer source ({source[:120]})"
        _log(f"  fast download skipped: {reason}")
        return DownloadOutcome(False, reason)

    url_template = urllib.parse.unquote(url_raw)

    # Shared with source_identity() so the cache key can never claim an
    # encoding the tiles were not built with. Raises when the URL genuinely
    # cannot be told apart (see resolve_xyz_encoding).
    encoding = resolve_xyz_encoding(source)

    # TODO(waveshed): the live XYZ download path has only been exercised against
    # Mapzen Global Terrain (Terrarium encoding). Test it end-to-end with other
    # elevation-tile services too — e.g. AWS Terrain Tiles, Mapbox Terrain-RGB,
    # and Nextzen — and verify encoding detection, {z}/{x}/{y} URL templating,
    # per-service zoom limits, and API-key/attribution handling. See TODO.md.

    # Zoom: at lat θ, ground resolution at zoom z is:
    #   res = 40075000 * cos(θ) / (2^z * 256)
    # So z = log2(40075000 * cos(θ) / (res * 256))
    # We need res <= resolution_m, so: z = ceil(log2(...))
    #
    # zmax is whatever the layer's URI says, which for a hand-added XYZ layer
    # is QGIS's default of 18 — deeper than any real terrain service. Validate
    # and clamp it here rather than trusting int() on it.
    z_max, zmax_warning = resolve_zmax(source)
    if zmax_warning:
        _log(f"  WARNING: {zmax_warning}")

    try:
        converter_exe = binary_manager.find_binary("aether_converter")
    except RuntimeError as exc:
        _log(f"  fast download skipped: {exc}")
        return DownloadOutcome(False, str(exc))

    # Group by the zoom each tile needs for its OWN latitude. A single job-wide
    # zoom taken from the request's bbox centre made a tile's pixels depend on
    # which run fetched it first — two runs sharing a tile but centred at
    # different latitudes would build it from different source zooms. Grouped
    # this way, zoom is a pure function of (source, tile, resolution), so the
    # pool needs no zoom term to stay honest.
    by_zoom = _download_specs_by_zoom(source, subtiles)

    # *cache_dir* is the tile pool: only the tiles this run is missing were
    # passed in, and they are written straight into it.
    all_names = [t["filename"] for t in subtiles]

    # The raw source-tile cache (<cache>/xyz, the job's `cache_dir`): shared
    # by every source and both tabs — the converter keys it by service and
    # zoom itself. Resolved once per call, like the pool.
    raw_dir = os.path.abspath(xyz_cache_dir())

    def _produced() -> bool:
        return any(_tile_is_whole(os.path.join(cache_dir, n)) for n in all_names)

    # Per-run scratch job file: the Map Converter worker and Site Analysis
    # can download in the same QGIS process now, so a pid-derived shared name
    # would race. mkstemp is unique per call; removed in the finally below.
    job_fd, job_file = tempfile.mkstemp(prefix="aether_dl_", suffix=".json")
    os.close(job_fd)

    # Set by _run_pass; read after the passes to detect an engine that silently
    # ignored buildings_pbf_dir. A list so the closure can write to it.
    buildings_seen: List[bool] = []

    # The void count of every pool tile a pass of this download wrote (name
    # → record bound to the file as that pass left it; None = unknown): a
    # NON-repair pass's `[Tile] <name> voids=<n>` lines (aether_converter
    # >= 0.2.11 — parsed when present, nothing waits for them); a repair
    # pass rewrites its tiles and prints none, so they are unknown again, as
    # is every tile of a pass without a line for it. Recorded in the pool's
    # manifest once the download ends (_settle_voids), checked against the
    # file again first.
    voids_seen: Dict[str, Optional[Dict[str, int]]] = {}
    voids_settled: List[Dict[str, Dict[str, int]]] = []

    def _note_voids(specs: List[Dict[str, Any]], lines: List[str],
                    repair: bool) -> None:
        counts = {} if repair else _parse_tile_voids(lines)
        for s in specs:
            name = s["filename"]
            n = counts.get(name)
            voids_seen[name] = (None if n is None else _voids_record(
                os.path.join(cache_dir, name), n))

    def _settle_voids() -> Dict[str, Dict[str, int]]:
        """Record this download's counts in the pool (once): a record whose
        file changed since its pass (another writer) or is gone (dropped
        after a failure) is dropped instead."""
        if not voids_settled:
            final: Dict[str, Optional[Dict[str, int]]] = {}
            for name, rec in voids_seen.items():
                if rec is not None and _voids_record(
                        os.path.join(cache_dir, name), rec["voids"]) != rec:
                    rec = None
                final[name] = rec
            _record_pool_voids(cache_dir, final)
            voids_settled.append({n: r for n, r in final.items() if r})
        return voids_settled[0]

    # Aggregated download progress across zoom groups and retry passes, for
    # callers that want a live bar: progress_cb(frac_0_to_1, label). A
    # callback that also has a ``download_event(info)`` attribute gets the
    # raw counts too (source tiles done/total incl. retry passes, pass k/n,
    # MB/s, errors) — the Map Converter's whole-run bar; plain callables
    # (Site Analysis, Processing) keep the two-argument contract.
    zoom_order = sorted(by_zoom)
    aggregator = (DownloadProgressAggregator(
        [len(by_zoom[z]) for z in zoom_order],
        [_source_tile_count(by_zoom[z], z) for z in zoom_order])
        if progress_cb else None)
    sink = getattr(progress_cb, "download_event", None)
    max_passes_for_info = _get_download_max_passes()
    passes_run: Dict[int, int] = {}
    # The timing lane: one line per pass (_PassTiming), passes per zoom.
    timing = _PassTiming()
    timing_passes: Dict[int, int] = {}

    def _emit(gi: int, zoom: int, label: str, extra: Optional[Dict] = None
              ) -> None:
        progress_cb(aggregator.fraction(), label)
        if sink is not None:
            info = aggregator.info(gi)
            info.update({"zoom": zoom, "group": gi, "groups": len(zoom_order),
                         "max_passes": max_passes_for_info})
            if extra:
                info.update(extra)
            sink(info)

    def _run_pass(specs: List[Dict[str, Any]], conn: int, zoom: int,
                  repair_source_tiles: Optional[List[Tuple[int, int, int]]]
                  = None):
        """Write + run one download pass for *specs* at *conn* connections.

        *repair_source_tiles*, when given, makes it a repair pass: the job
        carries ``repair_source_tiles: [[z, x, y], ...]`` and the converter
        fetches only those source tiles and patches them into the existing
        output .abt files of *specs* (CONVERTER_REPAIR_MIN_VERSION).

        Returns ``(returncode, ok_tiles, total_tiles, lines)`` — the output
        lines ride along because they are the only place the *reason* for a
        failure exists (see :func:`_download_failure_detail`).
        """
        # The pool was created long before this (prepare_terrain /
        # ensure_pool_tiles) and the scratch job file long before that;
        # re-make both here, a statement away from the launch, because the
        # converter writes its output .abt straight into `output_dir` and a
        # directory that went missing in between is what "os error 3" is.
        # The raw tile cache is made here too (first download of a cache).
        ensure_engine_dirs(os.path.abspath(cache_dir),
                           os.path.dirname(os.path.abspath(job_file)),
                           raw_dir,
                           os.path.abspath(pbf_dir) if pbf_dir else None)
        job = {
            "url_template": url_template,
            "encoding": encoding,
            "output_dir": os.path.abspath(cache_dir),
            "tiles": specs,
            "zoom": zoom,
            "max_connections": conn,
            # Raw source-tile cache (toolkit CONTRACT changelog 38,
            # aether_converter >= 0.2.8; older builds ignore the field): the
            # converter keeps every fetched XYZ tile's raw PNG/WebP bytes
            # there — real server answers only (200 bodies, 404 "no tile";
            # never a degraded or failed tile) — and later jobs read those
            # instead of the network, fetching only the missing ones. Its
            # `[Download] cache: <hits> of <n> source tiles from cache` line
            # is logged like every other line, never parsed; a cached tile
            # counts as fetched in progress and [Stats], and every .abt byte
            # is the same. Every pass (first, retry, repair) carries it.
            "cache_dir": raw_dir,
        }
        # Omitted entirely when unset, so an older converter sees exactly the
        # job shape it sees today.
        if pbf_dir:
            job["buildings_pbf_dir"] = os.path.abspath(pbf_dir)
            if pbf_ready or pbf_list:
                # Every pass: a later zoom group's first pass may launch
                # before the marker / the list exists (an earlier group
                # failed before its post-pass) — it must wait, never burn a
                # directory still being filled.
                if pbf_list:
                    job["buildings_pbf_list"] = os.path.abspath(pbf_list)
                else:
                    job["buildings_pbf_ready"] = pbf_ready
                if pbf_wait_s is not None:      # absent: the converter's 1800
                    job["buildings_pbf_wait_s"] = pbf_wait_s
        if repair_source_tiles:
            job["repair_source_tiles"] = [[int(z), int(x), int(y)]
                                          for z, x, y in repair_source_tiles]
        with open(job_file, "w") as fh:
            json.dump(job, fh)

        on_line = None
        if progress_cb is not None:
            group_idx = zoom_order.index(zoom)
            pass_no = passes_run.get(zoom, 0) + 1
            passes_run[zoom] = pass_no
            # A repair pass's work is the source tiles it was asked to
            # patch; the converter's header line confirms (or corrects) it.
            aggregator.pass_start(
                group_idx, pass_no,
                len(repair_source_tiles) if repair_source_tiles
                else _source_tile_count(specs, zoom))
            kind = {"repair": True} if repair_source_tiles else {}
            if repair_source_tiles:
                tag = (f"z{zoom}, repair pass "
                       f"{pass_no}/{max_passes_for_info}")
            else:
                tag = (f"z{zoom}" if pass_no == 1
                       else f"z{zoom}, pass {pass_no}/{max_passes_for_info}")
            _emit(group_idx, zoom, f"Downloading terrain ({tag})",
                  {"event": "pass_start", **kind})

            def on_line(line: str) -> None:
                header = run_progress.parse_download_header(line)
                if header is not None:
                    aggregator.pass_total(group_idx, header[1])
                    _emit(group_idx, zoom, f"Downloading terrain ({tag}): "
                          f"0/{header[1]} source tiles",
                          {"event": "header", **kind})
                    return
                parsed = run_progress.parse_download_line(line)
                if parsed:
                    aggregator.pass_progress(group_idx, parsed.done,
                                             parsed.total)
                    _emit(group_idx, zoom,
                          f"Downloading terrain ({tag}): "
                          f"{parsed.done}/{parsed.total} source tiles",
                          {"event": "progress", "mbps": parsed.mbps,
                           "errors": parsed.errors,
                           "in_flight": parsed.in_flight, **kind})

        timing_passes[zoom] = timing_passes.get(zoom, 0) + 1
        # The previous pass's timing line: its "next launch" ends here.
        timing.launching(time.perf_counter())
        forget_stream_timing()
        for s in specs:                 # rewritten from here on: unknown
            voids_seen[s["filename"]] = None
        try:
            rc, lines = _run_converter_download_once(
                converter_exe, job_file, on_line=on_line,
                should_cancel=should_cancel, on_start=on_start)
        finally:
            if progress_cb is not None:
                aggregator.pass_end(group_idx)
        timing.ended(zoom, timing_passes[zoom], bool(repair_source_tiles),
                     last_stream_timing())
        _note_voids(specs, lines, bool(repair_source_tiles))
        if pbf_dir and rc == 0:
            buildings_seen.append(_buildings_were_applied(lines))
        if pbf_dir and burns is not None:
            # The post-pass burns after the terrain is written, so there is
            # nothing to save by a kill mid-pass: the pass's lines are read
            # once it has ended, and a decode warning stops the download.
            burns.begin(0 if repair_source_tiles else 1, what="download")
            for ln in lines:
                burns.feed(ln)
            burns.end(ok=(rc == 0))
            if burns.batch_bad:
                from .osm_buildings import UndecodableBuildings
                raise UndecodableBuildings()
        ok, total = _parse_download_completeness(lines)
        timing.mark("parse")
        return rc, ok, total, lines

    max_passes = max_passes_for_info
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
            return DownloadOutcome(
                False, f"this aether_converter ({converter_exe}) predates "
                       f"buildings-on-download")
        if supported is None:
            _log(f"  could not read {converter_exe} to check for "
                 "buildings-on-download; assuming it is supported")
        _log("  buildings fused during download (buildings_pbf_dir"
             + (f"; the converter waits for {pbf_list}" if pbf_list
                else f"; the converter waits for {pbf_ready} there"
                if pbf_ready else "") + ")")

    # Asked lazily (it launches the converter once, then is cached): only a
    # group with degraded-only tiles needs to know.
    repair_answer: List[bool] = []

    def _repair_supported() -> bool:
        if not repair_answer:
            from .binary_manager import converter_supports_repair
            try:
                repair_answer.append(bool(converter_supports_repair(converter_exe)))
            except Exception:  # noqa: BLE001 — a probe must never fail a run
                repair_answer.append(False)
        return repair_answer[0]

    try:
        ok_all = True
        reasons: List[str] = []
        for zoom in sorted(by_zoom):
            group = _download_zoom_group(
                cache_dir, by_zoom[zoom], zoom, base_conn, max_passes,
                _run_pass, _produced, repair_supported=_repair_supported,
                timing=timing)
            if progress_cb is not None:
                gi = zoom_order.index(zoom)
                aggregator.group_end(gi)
                _emit(gi, zoom, f"Downloading terrain (z{zoom}): done",
                      {"event": "group_end"})
            if not group:
                ok_all = False
                if group.detail:
                    reasons.append(f"z{zoom}: {group.detail}")

        # The engine's own report is only a warning here, never the gate —
        # see the note on the capability check above. A supporting engine
        # that printed nothing still produced buildings; a *non*-supporting
        # one never got this far, because the gate refused the fast path
        # before any download ran.
        if (pbf_dir and burns is None and buildings_seen
                and not any(buildings_seen)):
            _log("  note: no '[Buildings]' line in the converter output, but "
                 "the engine has the feature — buildings were applied. Report "
                 "this if the result is missing buildings.")

        return DownloadOutcome(ok_all, "\n".join(reasons),
                               _settle_voids() or None)
    finally:
        # The last pass's timing line: no next launch in this download.
        timing.launching(None)
        # A download that raised (Stop, a fetch failure, disk) still
        # records what its passes counted for the tiles left as they wrote
        # them (once: a return above settled already).
        _settle_voids()
        try:
            os.remove(job_file)
        except OSError:
            pass


def _download_zoom_group(
    cache_dir: str,
    tile_specs: List[Dict[str, Any]],
    zoom: int,
    base_conn: int,
    max_passes: int,
    _run_pass: Any,
    _produced: Any,
    repair_supported: Any = None,
    timing: Optional[_PassTiming] = None,
) -> DownloadOutcome:
    """Download one homogeneous-zoom group, with gap-repair retry passes.

    *repair_supported* (a callable, asked only when needed, or a bool) says
    whether the converter honours ``repair_source_tiles``; absent → False.
    *timing* (the download's :class:`_PassTiming`, whose passes *_run_pass*
    opens) gets the post-pass steps booked: reading the lines, the rebuild
    flags / dropped tiles, the gap scan and the repair plan.

    A failure carries the engine's own diagnosis (``DownloadOutcome.detail``)
    and leaves NOTHING of this group in the pool — see :func:`_drop_pool_tiles`.
    """
    all_names = [s["filename"] for s in tile_specs]
    # Every pass uses the configured connections. The converter runs its own
    # adaptive connection window under max_connections as the ceiling; the
    # old per-pass halving here stacked a second backoff on top of it (one bad
    # tile → 128, 64, 32 connections on a healthy link).
    conn = base_conn
    if timing is None:
        timing = _PassTiming()          # no pass is ever opened: books nothing

    def _can_repair() -> bool:
        if callable(repair_supported):
            return bool(repair_supported())
        return bool(repair_supported)

    # First pass downloads everything. Output tiles with a gap (hole, never
    # written) are re-downloaded WHOLE in further passes — a hole is a source
    # tile that never arrived. Output tiles whose only problem is degraded
    # source tiles (coarse-filled, no hole) get ONE repair pass when the
    # converter supports it: only those source tiles are fetched and patched
    # into the existing .abt (``repair_source_tiles``); an older converter
    # re-downloads those output tiles whole instead. An incomplete result is
    # marked so it is not reused as a valid cache — the next run re-downloads
    # it.
    try:
        specs = tile_specs
        repair: Optional[List[DegradedTile]] = None   # next pass repairs these
        repaired = False                               # the one repair pass ran
        for attempt in range(1, max_passes + 1):
            t = time.perf_counter()
            was_repair = repair
            repair = None
            if was_repair:
                rc, ok, total, lines = _run_pass(
                    specs, conn, zoom, repair_source_tiles=sorted(
                        {(d.z, d.x, d.y) for d in was_repair}))
            else:
                rc, ok, total, lines = _run_pass(specs, conn, zoom)
            elapsed = time.perf_counter() - t
            if rc != 0 and was_repair:
                # The patch state of the repaired tiles is unknown, so they
                # are rewritten whole if a pass is left, dropped otherwise.
                # The rest of the group was complete and stays.
                detail = _download_failure_detail(lines)
                timing.mark("parse")
                _log(f"  Rust download repair pass failed (exit {rc}) at "
                     f"z={zoom}" + (f":\n    {detail}" if detail else ""))
                if attempt < max_passes:
                    _log(f"  re-downloading the {len(specs)} repaired output "
                         f"tile(s) whole @ {conn} connections (pass "
                         f"{attempt + 1}/{max_passes})")
                    timing.mark("plan")
                    continue
                gone = _drop_pool_tiles(cache_dir,
                                        [s["filename"] for s in specs])
                timing.mark("flags")
                _log(f"    {gone} unusable tile(s) removed from the pool")
                return DownloadOutcome(False, detail or f"exit {rc}")
            if rc != 0:
                detail = _download_failure_detail(lines)
                timing.mark("parse")
                _log(f"  Rust download failed (exit {rc}) at z={zoom}"
                     + (f":\n    {detail}" if detail else ""))
                # The engine pads every output tile to full size BEFORE it
                # decides the run failed, so a complete, valid .abt of 0 m
                # terrain is sitting in the pool right now. Leave it and the
                # next run reports a cache hit over flat sea.
                gone = _drop_pool_tiles(cache_dir, all_names)
                timing.mark("flags")
                _log(f"    {gone} unusable tile(s) removed from the pool")
                return DownloadOutcome(False, detail or f"exit {rc}")
            if total < 0:
                # Older converter without completeness stats — trust exit 0.
                _log(f"  Rust download z={zoom}: {elapsed:.1f}s")
                _set_rebuild_flags(cache_dir, all_names)
                timing.mark("flags")
                return DownloadOutcome(_produced())
            # Degraded source tiles (failed, filled from a coarser zoom) are
            # not counted in `ok`, so a pass with any of them never takes the
            # complete branch below; their output tiles carry no hole, so
            # they are added to the affected set explicitly.
            degraded = _parse_degraded(lines)
            n_degraded = max(len(degraded), _parse_degraded_count(lines))
            no_data = _parse_download_no_data(lines)
            if (was_repair and not degraded
                    and ok < total - no_data):
                # A repair pass that could not fetch some of its tiles leaves
                # the coarse fill in place (no hole to find), whether or not
                # it re-announced them: the asked-for tiles are still degraded.
                degraded = list(was_repair)
                n_degraded = max(n_degraded, total - no_data - ok)
            degraded_names = _specs_hit_by_degraded(specs, degraded)
            timing.mark("parse")
            if ok >= total and not n_degraded:
                _log(f"  Rust download z={zoom}: {elapsed:.1f}s, "
                     f"{ok}/{total} source tiles (XYZ) OK "
                     f"→ {len(specs)} output tile(s) @ {conn} conn"
                     + (" (repair pass)" if was_repair else ""))
                _set_rebuild_flags(cache_dir, all_names)
                timing.mark("flags")
                return DownloadOutcome(True)

            if total - ok == no_data and not n_degraded:
                # Every miss is the service answering HTTP 404 — "no tile
                # here". That is data, not failure: a bounded source (a local
                # fixture, a regional DEM service) 404s the whole world
                # outside its coverage, and the converter wrote those pixels
                # as void. Retrying cannot change a permanent answer, and
                # flagging the tiles for rebuild would re-download and re-404
                # them on every later run — so this is a COMPLETE result. The
                # user is told, loudly, which part of their area is sea.
                _warn_no_data_tiles(zoom, no_data, total)
                _log(f"  Rust download z={zoom}: {elapsed:.1f}s, {ok}/{total} "
                     f"source tiles OK + {no_data} no-data (404) "
                     f"→ {len(specs)} output tile(s), voids where the service "
                     f"has nothing")
                _set_rebuild_flags(cache_dir, all_names)
                timing.mark("flags")
                return DownloadOutcome(True)

            if ok == 0 and no_data == 0 and not n_degraded:
                # TOTAL failure, which is a different animal from the partial
                # one warned about further down: not one source tile arrived
                # and none of the misses is a no-data answer (all-404 returned
                # as complete above; a 404-plus-real-failure mix takes the
                # retry path below, where only the real failures retry), so
                # the network died, the key is wrong, or the engine predates
                # the no-data tally and cannot say. Retrying at half the
                # connections cannot fix any of those, so say what it is,
                # make sure none of it is cached, and let the caller fall back.
                _log(f"  ERROR: Rust download z={zoom}: 0 of {total} source "
                     f"tile(s) fetched — nothing was downloaded, so these "
                     f"tiles would be flat 0 m terrain.")
                _log(f"    Usual cause: the tile URL/API key is wrong, the "
                     f"network is down, or this aether_converter is too old "
                     f"to distinguish no-data (HTTP 404) from failure — "
                     f"update the engine binaries if the area is simply "
                     f"outside this service's coverage.")
                # Removed, not flagged: an engine that exits 0 having fetched
                # nothing leaves exactly the same full-size 0 m tiles behind as
                # one that exits non-zero, and a flagged tile is still linked
                # into a run and still read straight off the pool.
                gone = _drop_pool_tiles(cache_dir, all_names)
                timing.mark("flags")
                _log(f"    {gone} flat 0 m tile(s) removed from the pool")
                detail = _download_failure_detail(lines)
                return DownloadOutcome(
                    False,
                    detail or f"0 of {total} source tile(s) fetched at z{zoom}")

            missing = total - ok
            # Which sub-tiles need re-fetching? Ones with a hole in them
            # (VOID blocks from the current converter, zero blocks from an
            # older one), and ones never written at all — the block scans
            # cannot see the latter, reporting False for an unopenable file.
            # Those are GAPPED (re-downloaded whole); degraded ones are
            # affected too but may only need their source tiles patched.
            #
            # A hole is a source tile that failed WITHOUT a coarser fill, and
            # `[Stats] Tiles: ok/total` counts a degraded tile as not OK — so
            # only `missing - n_degraded` misses can have left one. None
            # (degraded-only): no tile is read — the owner's z15 band read
            # all 706 tiles of 62 MB twice (44 GB from an HDD, 159-252 s,
            # "degraded/repair plan" in the timing line) to find nothing
            # behind one timed-out tile the converter had filled from z14.
            # Else the converter does not say WHICH output tiles they hit
            # (download.rs prints only the first failure of a class —
            # `[Download] first timeout: z/x/y`, none for HTTP errors or 404
            # — and its `[Strip …] … FAILED` lines carry no coordinates), so
            # every tile THIS pass wrote is read, once: tiles of the group
            # outside *specs* were left clean by an earlier pass and not
            # touched since.
            hard = max(0, missing - n_degraded)
            if hard:
                _log(f"  gap scan: reading {len(specs)} output tile(s) at "
                     f"z{zoom} — {hard} source tile(s) missing without a "
                     f"coarser fill"
                     + (f" ({no_data} of them no-data, HTTP 404)"
                        if no_data else "")
                     + "; the converter does not name the output tiles "
                       "they hit")
            affected = []
            gapped = []
            for s in specs:
                path = os.path.join(cache_dir, s["filename"])
                gap = not os.path.exists(path) or bool(hard) and (
                    _abt_has_holes(path) or _abt_has_zero_fill(path))
                if gap:
                    gapped.append(s)
                if gap or s["filename"] in degraded_names:
                    affected.append(s)
            # The gap scan (when there is one) reads the pass's tiles: the
            # plan's usual cost.
            timing.mark("plan")
            if n_degraded and not degraded_names:
                _log(f"  note: the engine reported {n_degraded} degraded "
                     f"source tile(s) at z{zoom} but none could be placed on "
                     f"an output tile of this pass")
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
                timing.mark("flags")
                return DownloadOutcome(True)

            # Degraded-only: nothing to re-fetch but coarse-filled source
            # tiles. That gets ONE repair pass; still degraded after it, the
            # tiles are kept for this run and flagged (below).
            degraded_only = bool(degraded_names) and not gapped
            if attempt >= max_passes or (degraded_only and repaired):
                if no_data > 0:
                    # Part of the remaining gaps is the service answering 404
                    # — permanent sea, not a retryable failure. The user must
                    # hear the sea-level contract even though the run also had
                    # real failures (the all-404 case warned and returned
                    # complete above).
                    _warn_no_data_tiles(zoom, no_data, total)
                # Degraded-only tiles have no hole — say "coarser data", not
                # "gaps", or the log contradicts the degraded warning below.
                left = ("filled from coarser data" if degraded_only
                        else "left with gaps")
                _log(f"  WARNING: {missing} source tile(s) still failed after "
                     f"{attempt} pass(es); {len(affected)} output tile(s) "
                     f"{left} — using partial terrain and flagging just "
                     f"those tiles for rebuild.")
                # Flag only the gapped tiles, so the next run re-fetches those
                # and keeps the rest. Usable-but-gapped terrain is still worth
                # returning: the caller would otherwise redo the whole
                # preparation through the far slower path in this same run.
                _set_rebuild_flags(cache_dir, all_names,
                                   {s["filename"] for s in affected})
                timing.mark("flags")
                if degraded_names:
                    # Kept for THIS run (coarse terrain beats none), flagged
                    # so the next run re-fetches exactly these.
                    _warn_degraded_tiles(len(degraded_names), degraded, zoom)
                return DownloadOutcome(_produced(),
                                      _download_failure_detail(lines))

            if degraded_only and not repaired:
                repair_specs = [s for s in tile_specs
                                if s["filename"] in degraded_names]
                placed = [d for d in degraded
                          if _specs_hit_by_degraded(repair_specs, [d])]
                if _can_repair():
                    timing.mark("plan")
                    repair, repaired, specs = placed, True, repair_specs
                    _log(f"  Rust download z={zoom}: only degraded source "
                         f"tiles left ({len(placed)} filled from coarser "
                         f"zooms, no gaps) → repair pass "
                         f"{attempt + 1}/{max_passes}: patching "
                         f"{len({(d.z, d.x, d.y) for d in placed})} source "
                         f"tile(s) into {len(repair_specs)} existing output "
                         f"tile(s) via repair_source_tiles @ {conn} "
                         f"connections")
                    continue
                _log(f"  Rust download z={zoom}: only degraded source tiles "
                     f"left, but this aether_converter does not report "
                     f"version >= {_converter_repair_min_version()} "
                     f"(repair_source_tiles) → fallback: "
                     f"re-downloading the affected output tile(s) whole")
            specs = affected
            _log(f"  Rust download incomplete ({missing} source tile(s) "
                 f"missing"
                 + (f", {n_degraded} of them filled from coarser zooms"
                    if n_degraded else "")
                 + f"): retrying {len(specs)} affected output tile(s) "
                 f"@ {conn} connections (pass {attempt + 1}/{max_passes})")
            timing.mark("plan")
        _set_rebuild_flags(cache_dir, all_names,
                           {s["filename"] for s in specs})
        timing.mark("flags")
        return DownloadOutcome(_produced())
    except ConverterCancelled:
        raise                      # cancellation must reach the caller
    except RuntimeError:
        raise
    except Exception as exc:
        _log(f"  Rust download error: {exc}")
        return DownloadOutcome(False, f"{type(exc).__name__}: {exc}")


class _DefaultBinaryManager:
    """binary_manager stand-in for callers that hold no manager object.

    ``_try_rust_download`` speaks to a ``.find_binary(name)`` interface; the
    Map Converter only has the module-level ``binary_manager.find_binary``.
    """

    @staticmethod
    def find_binary(name: str) -> str:
        from .binary_manager import find_binary
        return find_binary(name)


class PoolTiles(dict):
    """What :func:`ensure_pool_tiles` returns: ``{filename: absolute pool
    path}`` — a plain dict to every caller — plus the pool directory and
    which of the names THIS call downloaded (:attr:`fetched`: missing,
    short or flagged for rebuild when it started) versus the ones that were
    in the pool already (:attr:`present`). The Map Converter removes the
    tiles its run fetched after a successful run unless "Keep pool tiles" is
    ticked (``remove_pool_tiles``)."""

    def __init__(self, mapping: Optional[Dict[str, str]] = None,
                 pool_dir: str = "", fetched: Sequence[str] = (),
                 present: Sequence[str] = ()) -> None:
        super().__init__(mapping or {})
        self.pool_dir = pool_dir
        self.fetched: List[str] = list(fetched)
        self.present: List[str] = list(present)


def ensure_pool_tiles(
    source_uri: str,
    subtiles: List[Dict[str, Any]],
    resolution_m: int,
    binary_manager: Any = None,
    progress_cb: Any = None,
    should_cancel: Any = None,
    on_start: Any = None,
) -> PoolTiles:
    """Shared acquisition entry: make the pool hold every tile in *subtiles*.

    THE one XYZ downloader for both terrain paths. Uses the same pool cache
    Site Analysis uses (v4 identity from ``source_identity`` — pure terrain,
    deliberately NO buildings term: buildings are applied at ingest by the
    Map Converter, which keeps this pool shareable between the tabs) and the
    same download machinery (zoom groups, gap-repair retry passes).

    Returns ``{filename: absolute pool path}`` for every requested tile — a
    :class:`PoolTiles`, which also names the tiles this call downloaded
    (``fetched``) and the ones already pooled (``present``).
    Fails loudly: a download that cannot provide the tiles is a RuntimeError
    — there is no render fallback (rendering XYZ through QGIS is exactly the
    aliasing/UI-freeze failure this replaces).

    *progress_cb(frac_0_to_1, label)* is optional; *binary_manager* defaults
    to the module-level binary discovery.
    """
    if binary_manager is None:
        binary_manager = _DefaultBinaryManager()
    pool_dir = _pool_dir(source_uri)
    os.makedirs(pool_dir, exist_ok=True)
    _log(f"  shared pool: {pool_dir}")

    listing = _pool_listing(pool_dir)
    todo = [t for t in subtiles
            if not _listed_tile_ready(listing, t, pool_dir)]
    if todo:
        outcome = _try_rust_download(source_uri, pool_dir, {}, resolution_m,
                                     todo, binary_manager, pbf_dir=None,
                                     progress_cb=progress_cb,
                                     should_cancel=should_cancel,
                                     on_start=on_start)
        if not outcome:
            # The engine diagnosed this precisely — "decode=12" for a service
            # serving WebP to a PNG-only decoder, a 404 tally for a bad
            # template — and a constant "could not be fetched" throws that away
            # and points the user at their network. Same shape as the ingest
            # path's `_run_converter`: the engine's own words, in the message.
            raise RuntimeError(
                f"Terrain download failed: {len(todo)} tile(s) could not be "
                f"fetched from the elevation service. The engine reported:\n"
                f"{outcome.detail or '(no output)'}\n"
                f"See the Waveshed-Terrain log for the full output, fix the "
                f"layer, and run again."
            )
    else:
        _log(f"  pool HIT — all {len(subtiles)} tile(s) already downloaded")

    fetched = {t["filename"] for t in todo}
    mapping = PoolTiles(
        pool_dir=os.path.abspath(pool_dir),
        fetched=sorted(fetched),
        present=sorted({t["filename"] for t in subtiles} - fetched))
    missing: List[str] = []
    if todo:
        listing = _pool_listing(pool_dir)     # the download wrote tiles
    for t in subtiles:
        path = os.path.join(pool_dir, t["filename"])
        # A tile flagged for rebuild (gap-repair exhausted) still holds
        # usable partial terrain for THIS run — the same policy Site
        # Analysis applies when it links such tiles into its view. Only a
        # missing/short file is a hard failure.
        if _listed_tile_whole(listing, t, pool_dir):
            mapping[t["filename"]] = os.path.abspath(path)
        else:
            missing.append(t["filename"])
    if missing:
        raise RuntimeError(
            f"{len(missing)} of {len(subtiles)} terrain tile(s) are missing "
            f"from the pool after download (first: {missing[0]}). The "
            f"elevation service did not provide them — check the layer's "
            f"zoom limit and URL, then run again."
        )
    _record_route("download")
    return mapping


# ---------------------------------------------------------------------------
# Path 2/3: direct sources[] (converter reprojects) + converter
# ---------------------------------------------------------------------------
#
# There is deliberately NO GDAL-warp path here any more. The converter samples
# every source in its own CRS (``sources[]`` + GeoKeys); a CRS it cannot
# handle is *its* hard error, surfaced verbatim — silently warping around it
# would hide exactly the failures the toolkit is designed to report.

def _bounds_from_geotransform(gt: Tuple[float, ...], xsize: int,
                              ysize: int) -> Dict[str, float]:
    """Axis-aligned bounds of a raster in its own CRS, from its geotransform.

    Evaluates all four corners so rotated/sheared geotransforms and negative
    pixel heights come out right.
    """
    xs = []
    ys = []
    for px, py in ((0, 0), (xsize, 0), (0, ysize), (xsize, ysize)):
        xs.append(gt[0] + gt[1] * px + gt[2] * py)
        ys.append(gt[3] + gt[4] * px + gt[5] * py)
    return {"west": min(xs), "east": max(xs),
            "south": min(ys), "north": max(ys)}


def _expand_bbox(bbox: Dict[str, float], margin: float) -> Dict[str, float]:
    """*bbox* grown by *margin* on every side (same units as the bbox)."""
    return {"west": bbox["west"] - margin, "east": bbox["east"] + margin,
            "south": bbox["south"] - margin, "north": bbox["north"] + margin}


def _clamp_bbox(a: Dict[str, float],
                b: Dict[str, float]) -> Optional[Dict[str, float]]:
    """Intersection of two bboxes, or None when they do not overlap."""
    out = {"west": max(a["west"], b["west"]),
           "east": min(a["east"], b["east"]),
           "south": max(a["south"], b["south"]),
           "north": min(a["north"], b["north"])}
    if out["west"] >= out["east"] or out["south"] >= out["north"]:
        return None
    return out


def _union_bbox(bboxes: List[Dict[str, float]]) -> Optional[Dict[str, float]]:
    """Axis-aligned union of *bboxes* (None for an empty list)."""
    if not bboxes:
        return None
    return {"west": min(b["west"] for b in bboxes),
            "east": max(b["east"] for b in bboxes),
            "south": min(b["south"] for b in bboxes),
            "north": max(b["north"] for b in bboxes)}


def _bbox_transform(bbox: Dict[str, float], src_crs: Any,
                    dst_crs: Any) -> Dict[str, float]:
    """*bbox* transformed between CRSs via QGIS (densified bounding box).

    The one place coordinates are transformed: QgsCoordinateTransform's own
    ``transformBoundingBox`` — generic for any CRS pair, no hand-rolled
    formulas. Kept minimal so every caller's *logic* (intersection, halo
    arithmetic) stays pure-Python testable with injected results.
    """
    from qgis.core import QgsCoordinateTransform, QgsProject, QgsRectangle
    xform = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
    rect = xform.transformBoundingBox(
        QgsRectangle(bbox["west"], bbox["south"], bbox["east"], bbox["north"]))
    return {"west": rect.xMinimum(), "east": rect.xMaximum(),
            "south": rect.yMinimum(), "north": rect.yMaximum()}


#: Halo, in source pixels, added around a tile when deciding which files can
#: contribute to it (and when window-copying them): the converter's
#: area-averaging edge sampling needs neighbours past the tile border.
_SOURCE_HALO_PX = 4

#: A plain GeoTIFF above this size is window-copied per tile instead of being
#: handed over whole, to bound the converter's decode RAM.
_MATERIALIZE_MAX_TIFF_BYTES = 512 * 1024 * 1024


def source_file_info(path: str, crs_authid: Optional[str] = None,
                     declared_is_fallback: bool = False) -> Dict[str, Any]:
    """Everything the per-tile source filter needs to know about one raster.

    Returns ``{"path", "crs", "crs_authid", "native_bounds", "wgs84_bounds",
    "halo_deg"}`` where ``crs`` is the QgsCoordinateReferenceSystem object,
    ``native_bounds`` the file's bounds in its own CRS, ``wgs84_bounds`` the
    same transformed to WGS84 and ``halo_deg`` the 4-source-pixel halo in
    degrees of latitude (``build_tile_sources`` widens it per tile for the
    longitude direction and the output resolution).

    CRS precedence: with ``declared_is_fallback=False`` (single-file QGIS
    layer) the declared *crs_authid* is authoritative — a user can
    deliberately override a layer's CRS in QGIS. With
    ``declared_is_fallback=True`` (a FOLDER-level answer, detected from one
    file or asked from the user) each file's own embedded CRS wins and the
    declared one applies only to files with no usable embedded CRS —
    otherwise a folder mixing e.g. a projected national DEM with a WGS84
    filler would silently georeference every file as the first file's CRS.

    Hard errors, never guesses: a file that cannot be opened, has no
    geotransform, or has no resolvable CRS stops the run with a message naming
    the file — the converter would reject it anyway, only later and after the
    other tiles were built.
    """
    from qgis.core import QgsCoordinateReferenceSystem

    ds = gdal.Open(path, gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(
            f"Terrain source could not be opened as a raster:\n{path}\n\n"
            "Remove it from the terrain folder, or fix the file."
        )
    try:
        gt = ds.GetGeoTransform(can_return_null=True)
    except TypeError:              # older GDAL bindings lack the kwarg
        gt = ds.GetGeoTransform()
    # The identity transform is GDAL's own "no georeferencing" convention —
    # and a file can carry it EXPLICITLY (persisted tags), in which case
    # can_return_null hands it back non-null and the check below never
    # fired: the file then "covered" lon 0..width, filtered out of every
    # tile, and converted to a silent all-void batch. Found by the torture
    # suite's broken-nogt folder case, 2026-08-31.
    if gt == (0.0, 1.0, 0.0, 0.0, 0.0, 1.0):
        gt = None
    if gt is None:
        raise RuntimeError(
            f"Terrain source has no georeferencing (no geotransform):\n"
            f"{path}\n\nAssign one (e.g. gdal_edit.py -a_ullr …) or remove "
            "the file. Filename-derived georeferencing is not supported."
        )
    xsize, ysize = ds.RasterXSize, ds.RasterYSize
    wkt = ds.GetProjection()
    ds = None

    embedded = QgsCoordinateReferenceSystem(wkt) if wkt else None
    if embedded is not None and not embedded.isValid():
        embedded = None
    declared = (QgsCoordinateReferenceSystem(crs_authid)
                if crs_authid else None)
    if declared is not None and not declared.isValid():
        declared = None

    if declared_is_fallback:
        crs = embedded or declared      # the file knows itself best
    else:
        crs = declared or embedded      # the caller's declaration wins
    if crs is None:
        raise RuntimeError(
            f"Terrain source has no usable coordinate system:\n{path}\n\n"
            "Assign one (e.g. gdal_edit.py -a_srs EPSG:…), or add the layer "
            "to QGIS with its CRS set and use it as the DEM layer."
        )

    native_bounds = _bounds_from_geotransform(gt, xsize, ysize)
    # Pixel size in degrees, for the halo: geographic CRSs are already in
    # degrees; projected units are treated as metres and divided by 111 111
    # m/deg. Only a filter margin — a slightly generous halo merely admits a
    # file the converter then finds contributes nothing.
    px = max(abs(gt[1]), abs(gt[5]))
    px_deg = px if crs.isGeographic() else px / 111_111.0
    authid = crs.authid() or ""

    if authid == "EPSG:4326":
        wgs84_bounds = dict(native_bounds)
    else:
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        wgs84_bounds = _bbox_transform(native_bounds, crs, wgs84)

    return {
        "path": path,
        "crs": crs,
        "crs_authid": authid,
        "native_bounds": native_bounds,
        "wgs84_bounds": wgs84_bounds,
        "halo_deg": _SOURCE_HALO_PX * px_deg,
    }


def _needs_materialization(path: str) -> bool:
    """True when the converter's tiff reader cannot take *path* directly.

    Routing only — the copy itself happens in
    :func:`materialize_for_converter`. Non-TIFF formats (.hgt/.dem), VRTs and
    ``/vsi*`` virtual paths always need it; a plain GeoTIFF needs it only
    past ``_MATERIALIZE_MAX_TIFF_BYTES``. A file whose size cannot be read is
    handed over unchanged: if it is genuinely unreadable the converter fails
    loudly on it, which beats failing here on a stat quirk.
    """
    if path.startswith("/vsi"):
        return True
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".tif", ".tiff"):
        return True
    try:
        return os.path.getsize(path) > _MATERIALIZE_MAX_TIFF_BYTES
    except OSError:
        return False


def materialize_for_converter(
    path: str,
    dest: str,
    window: Optional[Dict[str, float]] = None,
    unscale: bool = False,
    rotated: bool = False,
) -> str:
    """Window-copy *path* to a plain GeoTIFF the converter can read.

    **Never resampled** for a north-up source: no target resolution, no
    warp — ``gdal.Translate`` copies the source pixels at native resolution,
    optionally windowed to *window* (a bbox in the SOURCE CRS, already
    clamped to the file's own extent by the caller so GDAL never zero-fills
    a partially-outside window with fake 0 m ground).

    *unscale*: the file carries a GDAL scale/offset (e.g. Int16 5000 with
    scale 0.1 = 500 m). The copy is written as Float32 physical values with
    no scale left on it, so the result is right whether or not the
    converter honours GDAL_METADATA scale/offset itself — it can never be
    applied twice, and never be forgotten. Nodata stays nodata.

    *rotated*: a rotated / sheared / south-up geotransform cannot be
    windowed by ``Translate`` and is not read by the converter at all; the
    one unavoidable resampling — into a north-up grid of the SAME CRS at
    the source's own pixel size (bilinear), outside the source = nodata —
    goes through ``gdal.Warp``. No reprojection.

    Shared by both terrain paths (Site Analysis and the Map Converter tab).
    Returns *dest*. Failure is a hard error naming the file.
    """
    src: Any = path
    if unscale:
        src = gdal.Translate("", path, format="VRT", unscale=True,
                             outputType=gdal.GDT_Float32)
        if src is None:
            raise RuntimeError(
                f"Terrain source could not be read (scale/offset):\n{path}")
    if rotated:
        ds = gdal.Open(path) if isinstance(src, str) else src
        if ds is None:
            raise RuntimeError(
                f"Terrain source could not be opened:\n{path}")
        gt = ds.GetGeoTransform()
        px = math.hypot(gt[1], gt[4])
        py = math.hypot(gt[2], gt[5])
        nodata = ds.GetRasterBand(1).GetNoDataValue()
        wopts: Dict[str, Any] = {
            "format": "GTiff", "dstSRS": ds.GetProjection(),
            "xRes": px, "yRes": py, "resampleAlg": "bilinear",
            "outputType": gdal.GDT_Float32,
            "dstNodata": nodata if nodata is not None else -9999.0,
        }
        if window:
            wopts["outputBounds"] = (window["west"], window["south"],
                                     window["east"], window["north"])
        out = gdal.Warp(dest, ds, **wopts)
        ds = src = None
    else:
        opts: Dict[str, Any] = {"format": "GTiff"}
        if window:
            opts["projWin"] = [window["west"], window["north"],
                               window["east"], window["south"]]
        out = gdal.Translate(dest, src, **opts)
        src = None
    if out is None:
        raise RuntimeError(
            f"Terrain source could not be converted to GeoTIFF for the "
            f"engine:\n{path}\n\nGDAL could not read it — check the file, or "
            "convert it to a plain GeoTIFF yourself and use that instead."
        )
    out.FlushCache()
    out = None
    return dest


def build_tile_sources(
    infos: List[Dict[str, Any]],
    tile_bbox: Dict[str, float],
    tmp_dir: str,
    tag: str,
    output_res_m: float,
    index: Optional[FootprintIndex] = None,
    regions: Optional[Sequence[Dict[str, float]]] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """``sources[]`` entries for one tile, from per-file *infos*.

    *infos* is in priority order (first = highest; the converter takes the
    first valid sample per pixel). A file contributes when its WGS84 bounds
    intersect the tile bbox grown by a per-file halo (below). Files the
    converter cannot read directly are window-copied (tile window + halo,
    clamped to the file) into *tmp_dir*; the created temp paths are returned
    for cleanup. *index* (a :class:`FootprintIndex` over *infos*) finds the
    files without a scan of the whole list and reuses the tile's match from
    :func:`tile_source_fingerprint` (see :func:`_tile_contributors`).

    Halo sizing: ``max(4 source px, 0.6 output cells)`` in degrees of
    latitude. The 4-source-px term gives the converter's edge sampling its
    neighbours; the 0.6-output-cell term matters when heavily decimating —
    an edge output cell's area-average footprint reaches half an output cell
    past the tile border (30 m output over a 2 m source needs 7.5 source px,
    far more than 4). The longitude component is divided by
    ``max(cos(center_lat), 0.2)`` because a degree of longitude shrinks with
    latitude — a metre-derived halo would otherwise be undersized 2x at
    60°N (the 0.2 floor caps the blow-up near the poles; over-inclusive is
    safe, it merely admits a file that contributes nothing).

    ``crs`` is set on an entry when the plugin knows an EPSG authid; non-EPSG
    authids (e.g. ``USER:100000``) are omitted because the toolkit accepts
    only ``EPSG:nnnn`` or a proj string — it then reads the file's own
    GeoKeys, and its error surfaces verbatim if they are unusable.

    *regions* (a partial pool tile, see :func:`resolve_pool_tile`): only the
    files those regions admit (:func:`_region_contributors`), in priority
    order — the job keeps the whole tile's geometry, so a cell inside a
    region samples exactly what a whole build gives it. A materialized
    window covers the regions plus halo; a ROTATED file keeps the whole
    tile's window, because its one resampling (``gdal.Warp``) is anchored at
    the window and a smaller one would move its grid.
    """
    _record_route("sources")

    sources: List[Dict[str, Any]] = []
    temps: List[str] = []
    contributors = (_region_contributors(infos, regions, output_res_m,
                                         index=index) if regions
                    else _tile_contributors(infos, tile_bbox, output_res_m,
                                            index=index))
    for idx, info, halo_bbox in contributors:
        path = info["path"]
        reason = (info["materialize"] if "materialize" in info
                  else ("materialize" if _needs_materialization(path) else None))
        if reason:
            if regions and info.get("rotated"):
                halo_bbox = _halo_box(info, tile_bbox, output_res_m)
            window_native = _window_in_source_crs(halo_bbox, info)
            window = (_clamp_bbox(window_native, info["native_bounds"])
                      if window_native is not None else None)
            if window is None:
                # The transformed tile+halo window misses the file's native
                # extent (the WGS84 filter and the native clamp can disagree
                # near a CRS's edge of validity). The file cannot contribute
                # to this tile — skip it. Falling back to a whole-file copy
                # here would defeat the very RAM bound windowing exists for
                # (a > 512 MB source would be copied in full).
                continue
            dest = os.path.join(tmp_dir, f"aether_src_{tag}_{idx}.tif")
            extra = {k: True for k in ("unscale", "rotated") if info.get(k)}
            materialize_for_converter(path, dest, window, **extra)
            temps.append(dest)
            path = dest
        entry: Dict[str, Any] = {"path": os.path.abspath(path)}
        crs = info.get("crs_arg")
        if crs is None and info.get("crs_authid", "").startswith("EPSG:"):
            crs = info["crs_authid"]
        if crs:
            entry["crs"] = crs
        sources.append(entry)
    return sources, temps


def _window_in_source_crs(halo_bbox: Dict[str, float],
                          info: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """*halo_bbox* (WGS84) in the file's own CRS — QGIS object when the info
    carries one (single-file layers), else OSR from the indexed WKT."""
    if info.get("crs_authid") == "EPSG:4326":
        return dict(halo_bbox)
    if info.get("crs") is not None:
        from qgis.core import QgsCoordinateReferenceSystem
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        return _bbox_transform(halo_bbox, wgs84, info["crs"])
    return terrain_index.bbox_from_wgs84(halo_bbox, info.get("crs_wkt", ""))


#: Bytes per sample of a GDAL data type name (the folder index's ``dtype``).
_DTYPE_BYTES = {"Byte": 1, "Int8": 1, "UInt16": 2, "Int16": 2, "UInt32": 4,
                "Int32": 4, "Float32": 4, "UInt64": 8, "Int64": 8,
                "Float64": 8, "CInt16": 4, "CInt32": 8, "CFloat32": 8,
                "CFloat64": 16}


def _materialized_sample_bytes(info: Dict[str, Any]) -> int:
    """Bytes per pixel of :func:`materialize_for_converter`'s copy of one
    file: Float32 for an unscaled or warped (rotated) copy, else the file's
    own type (from the info, or its folder-index entry); unknown: 4."""
    if info.get("unscale") or info.get("rotated"):
        return 4
    dtype = info.get("dtype")
    if not dtype and info.get("index_folder"):
        try:
            entry = terrain_index.get_index(
                info["index_folder"]).entries.get(info["path"]) or {}
            dtype = entry.get("dtype")
        except Exception:  # noqa: BLE001 — an estimate never fails a run
            dtype = None
    return _DTYPE_BYTES.get(str(dtype or ""), 4)


def materialized_window_bytes(infos: List[Dict[str, Any]],
                              tile_bbox: Dict[str, float],
                              output_res_m: float,
                              index: Optional[FootprintIndex] = None) -> int:
    """Temp bytes :func:`build_tile_sources` writes for one tile: per
    contributor it window-copies (``materialize`` routed), the tile+halo
    window in the file's own CRS, clamped to the file, in native pixels ×
    the copy's sample size (no compression assumed). Pure geometry — no file
    is opened."""
    total = 0
    for _idx, info, halo_bbox in _tile_contributors(infos, tile_bbox,
                                                    output_res_m, index=index):
        if not info.get("materialize"):
            continue
        try:
            win = _window_in_source_crs(halo_bbox, info)
            win = (_clamp_bbox(win, info["native_bounds"])
                   if win is not None else None)
        except Exception:  # noqa: BLE001 — no transform: nothing to price
            win = None
        if win is None:
            continue
        px_x = abs(float(info.get("px_x") or 0.0))
        px_y = abs(float(info.get("px_y") or 0.0))
        if px_x <= 0 or px_y <= 0:
            continue
        cols = math.ceil((win["east"] - win["west"]) / px_x)
        rows = math.ceil((win["north"] - win["south"]) / px_y)
        total += max(0, cols) * max(0, rows) * _materialized_sample_bytes(info)
    return total


Contributor = Tuple[int, Dict[str, Any], Dict[str, float]]

#: Bucket edge of :class:`FootprintIndex`, in degrees.
_INDEX_CELL_DEG = 0.1
#: A file spanning more buckets than this (a continental DEM) is not bucketed
#: cell by cell; it is a candidate for every tile instead.
_INDEX_MAX_FILE_CELLS = 1024
#: A lookup spanning more buckets than this scans the whole list instead.
_INDEX_MAX_QUERY_CELLS = 4096


def _finite(*values: Any) -> bool:
    try:
        return all(math.isfinite(v) for v in values)
    except TypeError:
        return False


class FootprintIndex:
    """Grid-bucket index over *infos*' ``wgs84_bounds`` — how
    :func:`_tile_contributors` finds a tile's files in a big folder without
    testing every file against every tile.

    Files are bucketed by 0.1° cell. A lookup takes the files whose buckets
    meet the tile grown by the halo rule's maximum (the largest ``halo_deg``
    in the list, 0.6 output cells, the same cos-latitude divisor) and runs
    THE exact test on those, in list order — a superset filter, so the result
    is the full scan's, element for element. Files with missing, non-finite
    or inverted bounds/halo, or spanning more than ``_INDEX_MAX_FILE_CELLS``
    buckets, are candidates for every tile.

    Bound to the very list it was built from (results carry positions in
    it). Each tile's result is kept per (tile bbox, output resolution), so
    the stale check and the job build of one run match a tile once.
    """

    def __init__(self, infos: List[Dict[str, Any]],
                 cell_deg: float = _INDEX_CELL_DEG) -> None:
        self.infos = infos
        self.cell = cell_deg
        self.max_halo: Any = 0.0
        self._buckets: Dict[Tuple[int, int], List[int]] = {}
        self._always: List[int] = []
        self._memo: Dict[Tuple[Any, ...], List[Contributor]] = {}
        c = cell_deg
        for idx, info in enumerate(infos):
            try:
                halo = info["halo_deg"]
                b = info["wgs84_bounds"]
                w, e, s, n = b["west"], b["east"], b["south"], b["north"]
                ok = _finite(halo, w, e, s, n) and w <= e and s <= n
            except (KeyError, TypeError):
                ok = False
            if not ok:
                self._always.append(idx)
                continue
            cx0, cx1 = math.floor(w / c), math.floor(e / c)
            cy0, cy1 = math.floor(s / c), math.floor(n / c)
            if (cx1 - cx0 + 1) * (cy1 - cy0 + 1) > _INDEX_MAX_FILE_CELLS:
                self._always.append(idx)
                continue
            if halo > self.max_halo:
                self.max_halo = halo
            for cy in range(cy0, cy1 + 1):
                for cx in range(cx0, cx1 + 1):
                    self._buckets.setdefault((cx, cy), []).append(idx)

    def __len__(self) -> int:
        return len(self.infos)

    def contributors(self, infos: List[Dict[str, Any]],
                     tile_bbox: Dict[str, float],
                     output_res_m: float) -> List[Contributor]:
        """:func:`_tile_contributors` for one tile, through the buckets."""
        if infos is not self.infos:
            raise ValueError("FootprintIndex used with a file list it was "
                             "not built from")
        w, s = tile_bbox["west"], tile_bbox["south"]
        e, n = tile_bbox["east"], tile_bbox["north"]
        key = (w, s, e, n, output_res_m)
        hit = self._memo.get(key)
        if hit is not None:
            return hit
        positions = self._candidates(w, s, e, n, output_res_m)
        out = _scan_contributors(infos, positions, tile_bbox, output_res_m)
        self._memo[key] = out
        return out

    def _candidates(self, w: Any, s: Any, e: Any, n: Any,
                    output_res_m: Any) -> Sequence[int]:
        everything = range(len(self.infos))
        if not (_finite(w, s, e, n, output_res_m) and output_res_m > 0
                and w <= e and s <= n):
            return everything
        # The query box contains every per-file halo box of the exact test:
        # same expressions, and max(halo, r) <= max(max_halo, r).
        center_lat = (n + s) / 2.0
        cos_lat = max(math.cos(math.radians(center_lat)), 0.2)
        hl = max(self.max_halo, 0.6 * output_res_m / 111_111.0)
        hlon = hl / cos_lat
        c = self.cell
        cx0, cx1 = math.floor((w - hlon) / c), math.floor((e + hlon) / c)
        cy0, cy1 = math.floor((s - hl) / c), math.floor((n + hl) / c)
        if (cx1 - cx0 + 1) * (cy1 - cy0 + 1) > _INDEX_MAX_QUERY_CELLS:
            return everything
        found = set(self._always)
        buckets = self._buckets
        for cy in range(cy0, cy1 + 1):
            for cx in range(cx0, cx1 + 1):
                hit = buckets.get((cx, cy))
                if hit:
                    found.update(hit)
        return sorted(found)


def footprint_index(infos: Optional[List[Dict[str, Any]]]
                    ) -> Optional[FootprintIndex]:
    """A :class:`FootprintIndex` over *infos* (None for no files)."""
    return FootprintIndex(infos) if infos else None


class SubsetFootprintIndex:
    """A :class:`FootprintIndex`'s answers for a SUB-list of its own infos
    (same dicts, same relative order — what :func:`_infos_near` returns),
    without building a second index: each lookup takes the full index's
    match and keeps the files in *infos*, at their positions in *infos*.

    The exact test is per file, so the result is the one a
    :class:`FootprintIndex` built over *infos* gives, element for element
    (positions included — they name a tile's materialized window copies).
    Kept per (tile bbox, output resolution) like the full index's own memo.
    """

    def __init__(self, full: FootprintIndex,
                 infos: List[Dict[str, Any]]) -> None:
        self.infos = infos
        self._full = full
        self._pos = {id(info): k for k, info in enumerate(infos)}
        self._memo: Dict[Tuple[Any, ...], List[Contributor]] = {}

    def __len__(self) -> int:
        return len(self.infos)

    def contributors(self, infos: List[Dict[str, Any]],
                     tile_bbox: Dict[str, float],
                     output_res_m: float) -> List[Contributor]:
        """:func:`_tile_contributors` for one tile of the sub-list."""
        if infos is not self.infos:
            raise ValueError("SubsetFootprintIndex used with a file list it "
                             "was not made for")
        key = (tile_bbox["west"], tile_bbox["south"], tile_bbox["east"],
               tile_bbox["north"], output_res_m)
        hit = self._memo.get(key)
        if hit is not None:
            return hit
        pos = self._pos
        out = [(pos[id(info)], info, halo) for _idx, info, halo
               in self._full.contributors(self._full.infos, tile_bbox,
                                          output_res_m)
               if id(info) in pos]
        self._memo[key] = out
        return out


def _scan_contributors(infos: List[Dict[str, Any]], positions: Sequence[int],
                       tile_bbox: Dict[str, float], output_res_m: float
                       ) -> List[Contributor]:
    """THE exact per-file test of :func:`_tile_contributors`, over
    *positions* of *infos* (ascending = priority order)."""
    out = []
    for idx in positions:
        info = infos[idx]
        halo_bbox = _halo_box(info, tile_bbox, output_res_m)
        if bboxes_intersect(info["wgs84_bounds"], halo_bbox):
            out.append((idx, info, halo_bbox))
    return out


def _halo_box(info: Dict[str, Any], tile_bbox: Dict[str, float],
              output_res_m: float) -> Dict[str, float]:
    """*tile_bbox* grown by *info*'s halo — THE halo rule (see
    :func:`build_tile_sources`): ``max(halo_deg, 0.6 output cells)`` of
    latitude, longitude divided by ``max(cos(centre lat), 0.2)``."""
    center_lat = (tile_bbox["north"] + tile_bbox["south"]) / 2.0
    cos_lat = max(math.cos(math.radians(center_lat)), 0.2)
    halo_lat = max(info["halo_deg"], 0.6 * output_res_m / 111_111.0)
    halo_lon = halo_lat / cos_lat
    return {
        "west": tile_bbox["west"] - halo_lon,
        "east": tile_bbox["east"] + halo_lon,
        "south": tile_bbox["south"] - halo_lat,
        "north": tile_bbox["north"] + halo_lat,
    }


def _region_contributors(infos: List[Dict[str, Any]],
                         regions: Sequence[Dict[str, float]],
                         output_res_m: float,
                         index: Optional[FootprintIndex] = None,
                         ) -> List[Contributor]:
    """:func:`_tile_contributors` of a UNION of regions of one tile: every
    file any region admits (each region by THE rule, normal halo), once, in
    priority order; its halo box is the union of the regions' halo boxes."""
    merged: Dict[int, Tuple[Dict[str, Any], List[Dict[str, float]]]] = {}
    for region in regions:
        for idx, info, halo in _tile_contributors(infos, region, output_res_m,
                                                  index=index):
            merged.setdefault(idx, (info, []))[1].append(halo)
    return [(idx, merged[idx][0], _union_bbox(merged[idx][1]))
            for idx in sorted(merged)]


def _tile_contributors(infos: List[Dict[str, Any]], tile_bbox: Dict[str, float],
                       output_res_m: float,
                       index: Optional[FootprintIndex] = None,
                       ) -> List[Contributor]:
    """``(index, info, halo bbox)`` for every file that can contribute to one
    tile, in priority order — THE filter, shared by :func:`build_tile_sources`
    and :func:`tile_source_fingerprint` so the stale-tile check can never
    disagree with what a rebuild would read (halo rule: see
    :func:`build_tile_sources`).

    With *index* (a :class:`FootprintIndex` built over this very *infos*
    list) only the files near the tile are tested and the result is kept per
    tile — the same list, in the same order, as the full scan."""
    if index is not None:
        return index.contributors(infos, tile_bbox, output_res_m)
    return _scan_contributors(infos, range(len(infos)), tile_bbox,
                              output_res_m)


def _subtile_bbox(t: Dict[str, Any]) -> Dict[str, float]:
    """WGS84 bbox of one sub-tile spec (the same arithmetic the job uses)."""
    res_deg = t["exact_res_m"] / 111_111.0
    return {"north": t["ul_lat"], "south": t["ul_lat"] - t["size_px"] * res_deg,
            "west": t["ul_lon"], "east": t["ul_lon"] + t["size_px"] * res_deg}


def _infos_near(infos: List[Dict[str, Any]], subtiles: List[Dict[str, Any]]
                ) -> List[Dict[str, Any]]:
    """*infos* that can touch any of *subtiles* (order kept). A 40k-file
    folder would otherwise be tested against every tile, per tile."""
    if not subtiles or len(infos) < 64:
        return infos
    boxes = [_subtile_bbox(t) for t in subtiles]
    union = _union_bbox(boxes)
    res = max(t["exact_res_m"] for t in subtiles)
    lat = max(abs(union["north"]), abs(union["south"]))
    margin_lat = max(max(i["halo_deg"] for i in infos), 0.6 * res / 111_111.0)
    margin_lon = margin_lat / max(math.cos(math.radians(lat)), 0.2)
    grown = {"west": union["west"] - margin_lon, "east": union["east"] + margin_lon,
             "south": union["south"] - margin_lat,
             "north": union["north"] + margin_lat}
    return [i for i in infos if bboxes_intersect(i["wgs84_bounds"], grown)]


#: Bump when the way a file reaches the converter changes (materialize
#: routing, unscale, crs argument) — every file-built tile is rebuilt once,
#: and only those (XYZ pools never carry a source fingerprint).
_SOURCE_ROUTE_VERSION = "r1"


def tile_source_fingerprint(infos: List[Dict[str, Any]],
                            tile_bbox: Dict[str, float],
                            output_res_m: float,
                            index: Optional[FootprintIndex] = None) -> str:
    """Fingerprint of exactly the files that feed one tile, in priority order:
    path, size+mtime (from the folder index), the CRS it is read in and its
    route. A file added, changed, removed or re-prioritised changes this for
    the tiles it touches — and for no other tile. *index*: see
    :func:`_tile_contributors`."""
    parts = [_SOURCE_ROUTE_VERSION]
    for _idx, info, _halo in _tile_contributors(infos, tile_bbox, output_res_m,
                                                index=index):
        parts.append("|".join((
            info["path"], str(info.get("fp", "")),
            str(info.get("crs_arg") or info.get("crs_authid") or ""),
            str(info.get("materialize") or ""),
            "u" if info.get("unscale") else "")))
    return hashes.md5("\n".join(parts).encode("utf-8")).hexdigest()


#: Edges of :func:`_rects_cover` closer than this (relative to the
#: coordinates' magnitude) are one edge: float noise between two files of one
#: grid (~1e-13°) is no gap, and nothing real is that small.
_COVER_EDGE_TOL = 1e-12
#: The tile's native window is grown by this fraction of its size before the
#: union test (the densified transform can cut a curved edge's extreme).
_COVER_WINDOW_PAD = 1e-4
#: Elementary cells beyond which the union test gives up (= not covered).
_COVER_MAX_CELLS = 4_000_000


def _edge_clusters(values: List[float], tol: float) -> Dict[float, int]:
    """Each value -> the index of its cluster of values within *tol*."""
    out: Dict[float, int] = {}
    k, start = -1, None
    for v in sorted(set(values)):
        if start is None or v - start > tol:
            k, start = k + 1, v
        out[v] = k
    return out


def _rects_cover(rects: List[Dict[str, float]], box: Dict[str, float]) -> bool:
    """True when the union of the axis-aligned *rects* covers ALL of *box* —
    exact (coordinate compression over the rects' own edges), no sampling."""
    W, S, E, N = box["west"], box["south"], box["east"], box["north"]
    if not (_finite(W, S, E, N) and W < E and S < N):
        return False
    clipped = []
    for r in rects:
        try:
            w, e = max(r["west"], W), min(r["east"], E)
            s, n = max(r["south"], S), min(r["north"], N)
        except (KeyError, TypeError):
            continue
        if _finite(w, e, s, n) and w < e and s < n:
            clipped.append((w, s, e, n))
    if not clipped:
        return False
    tol = _COVER_EDGE_TOL * max(1.0, abs(W), abs(E), abs(S), abs(N))
    xi = _edge_clusters([W, E] + [c[0] for c in clipped]
                        + [c[2] for c in clipped], tol)
    yi = _edge_clusters([S, N] + [c[1] for c in clipped]
                        + [c[3] for c in clipped], tol)
    nx, ny = xi[E], yi[N]
    if nx < 1 or ny < 1 or nx * ny > _COVER_MAX_CELLS:
        return False
    import numpy as np
    cov = np.zeros((ny, nx), dtype=bool)
    for w, s, e, n in clipped:
        cov[yi[s]:yi[n], xi[w]:xi[e]] = True
    return bool(cov.all())


CoverStacks = Sequence[Tuple[List[Dict[str, Any]], Optional[FootprintIndex]]]


def rect_cover_contributors(stacks: CoverStacks, tile_bbox: Dict[str, float],
                            output_res_m: float
                            ) -> Optional[List[Dict[str, Any]]]:
    """The files of *stacks* — ``(infos, index)`` per entry, exactly as their
    tile jobs read them (:func:`_tile_contributors`) — that feed one tile,
    when their RECTANGLES cover every part of it plus its halo; else None.
    Geometry only (the Map Converter's download estimate): holes inside the
    files are found by the converter's ``[Tile] covered=`` count.

    The halo is the rule's largest (max of the contributors' ``halo_deg`` and
    0.6 output cells, longitude by the cos-latitude divisor). The test is an
    exact union of the files' native rectangles, per CRS, over the region's
    native window (densified transform, padded): files in one CRS must cover
    it together; a union that needs two CRSs, a rotated/flipped file or a
    CRS that cannot be transformed counts as not covered — conservative.
    """
    contrib: List[Dict[str, Any]] = []
    for infos, index in stacks:
        contrib.extend(info for _i, info, _h in _tile_contributors(
            infos, tile_bbox, output_res_m, index=index))
    if not contrib:
        return None
    hl = max([0.6 * output_res_m / 111_111.0]
             + [info["halo_deg"] for info in contrib])
    center_lat = (tile_bbox["north"] + tile_bbox["south"]) / 2.0
    hlon = hl / max(math.cos(math.radians(center_lat)), 0.2)
    region = {"west": tile_bbox["west"] - hlon,
              "east": tile_bbox["east"] + hlon,
              "south": tile_bbox["south"] - hl,
              "north": tile_bbox["north"] + hl}
    groups: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = {}
    for info in contrib:
        if info.get("rotated"):
            continue
        key: Tuple[Any, ...] = (info.get("crs_authid") or "",
                                info.get("crs_wkt") or "",
                                info.get("crs_arg") or "")
        if key == ("", "", ""):
            key = ("", "", id(info))        # unknown CRS: stands alone
        groups.setdefault(key, []).append(info)
    for members in groups.values():
        box: Optional[Dict[str, float]] = None
        with contextlib.suppress(Exception):  # no transform: cannot prove cover
            win = _window_in_source_crs(region, members[0])
            pad_x = _COVER_WINDOW_PAD * (win["east"] - win["west"])
            pad_y = _COVER_WINDOW_PAD * (win["north"] - win["south"])
            box = {"west": win["west"] - pad_x,
                   "east": win["east"] + pad_x,
                   "south": win["south"] - pad_y,
                   "north": win["north"] + pad_y}
        if box is None:
            continue
        if _rects_cover([m.get("native_bounds") or {} for m in members], box):
            return contrib
    return None


def files_cover_tile(stacks: CoverStacks, tile_bbox: Dict[str, float],
                     output_res_m: float) -> bool:
    """True when the RECTANGLES of the files of *stacks* cover one tile plus
    its halo (:func:`rect_cover_contributors`). Geometry only — a file padded
    with nodata still leaves holes, which only the converter's own
    ``[Tile] covered=`` count reveals (the Map Converter's two-phase build);
    this is what the download ESTIMATE uses (an upper bound of the pool
    tiles a run fetches)."""
    return rect_cover_contributors(stacks, tile_bbox, output_res_m) is not None


#: Sidecar next to a pooled file-built tile: the tile_source_fingerprint it
#: was built from. A tile whose sidecar is missing or differs is stale.
_SOURCE_FP_SUFFIX = ".src"


def _read_source_fp(pool_dir: str, name: str) -> Optional[str]:
    try:
        with open(os.path.join(pool_dir, name + _SOURCE_FP_SUFFIX), "r") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _write_source_fps(pool_dir: str, fps: Dict[str, Optional[str]],
                      bad: set) -> None:
    """Record each built tile's fingerprint; drop it for tiles that failed."""
    for name, fp in fps.items():
        path = os.path.join(pool_dir, name + _SOURCE_FP_SUFFIX)
        try:
            if fp and name not in bad:
                with open(path, "w") as fh:
                    fh.write(fp)
            elif os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Partial pool tiles — build only the part of a tile the site needs
# ---------------------------------------------------------------------------
#
# A 50 km site at 90 m (1 deg tiles) touches 3-4 deg2 of tiles but needs
# ~1.2 deg2 of terrain. For a FILE source (folder / file layer — the sources
# whose bytes are a pure function of the files feeding each cell) a tile is
# built for its region only: R = tile ∩ (sector bbox grown by
# max(2 output px, 500 m)), snapped outward to the tile's pixel grid. The
# converter job keeps the whole tile's geometry and lists only the files R
# admits (normal halo), so every cell inside R samples exactly what a whole
# build gives it and the rest is the usual void fill. XYZ downloads and
# rendered services always build whole tiles.

#: Region margin around the sector bbox: max(this many output px, metres).
_REGION_MARGIN_PX = 2
_REGION_MARGIN_M = 500.0


class PoolTile(NamedTuple):
    """Verdict of :func:`resolve_pool_tile` for one tile of one job."""

    hit: bool
    #: Pool file serving the job (``<name>`` or ``<name>.part``) on a hit.
    path: Optional[str]
    #: Hit: *path* is a ``.part``. Miss: the rebuild writes a ``.part``.
    partial: bool
    #: Miss: the regions to build (None = the whole tile).
    regions: Optional[List[Dict[str, float]]]
    #: hit | hit-part | missing | stale | flagged | outside
    reason: str


def _bbox_area(b: Optional[Dict[str, float]]) -> float:
    if not b:
        return 0.0
    return max(0.0, b["east"] - b["west"]) * max(0.0, b["north"] - b["south"])


def _tile_region(tile: Dict[str, Any], sector_bbox: Dict[str, float],
                 output_res_m: float) -> Optional[Dict[str, float]]:
    """The part of *tile* a job over *sector_bbox* needs: the tile ∩ the
    bbox grown by ``max(2 output px, 500 m)``, snapped OUTWARD to the tile's
    own pixel grid (bit-identical edges for the same tile, so unions and
    containment are exact). None when that is the whole tile."""
    tb = _subtile_bbox(tile)
    size = int(tile["size_px"])
    res_deg = tile["exact_res_m"] / 111_111.0
    margin_m = max(_REGION_MARGIN_PX * float(output_res_m), _REGION_MARGIN_M)
    m_lat = margin_m / 111_111.0
    lat = min(max(abs(sector_bbox["north"]), abs(sector_bbox["south"])), 89.9)
    m_lon = m_lat / max(math.cos(math.radians(lat)), 1e-6)
    eps = 1e-9
    c0 = max(0, math.floor((sector_bbox["west"] - m_lon - tb["west"])
                           / res_deg + eps))
    c1 = min(size, math.ceil((sector_bbox["east"] + m_lon - tb["west"])
                             / res_deg - eps))
    r0 = max(0, math.floor((tb["north"] - sector_bbox["north"] - m_lat)
                           / res_deg + eps))
    r1 = min(size, math.ceil((tb["north"] - sector_bbox["south"] + m_lat)
                             / res_deg - eps))
    if (c0 == 0 and r0 == 0 and c1 == size and r1 == size) or \
            c1 <= c0 or r1 <= r0:
        return None                 # whole tile (or degenerate: be whole)
    return {"west": tile["ul_lon"] + c0 * res_deg,
            "east": tile["ul_lon"] + c1 * res_deg,
            "north": tile["ul_lat"] - r0 * res_deg,
            "south": tile["ul_lat"] - r1 * res_deg}


def tile_regions(subtiles: List[Dict[str, Any]], sector_bbox: Dict[str, float],
                 resolution_m: float, output_res_m: Optional[float] = None,
                 partial: bool = True) -> Dict[str, Optional[Dict[str, float]]]:
    """``{tile name: region or None}`` for one job's tiles (*partial* False:
    every tile whole). The margin's output pixel is the coarser of the tile
    resolution and *output_res_m* (a range profile's output grid). THE region
    rule — :func:`prepare_terrain`, :func:`prepare_terrain_profile`,
    :func:`terrain_plan` and :func:`run_region_union` all call it."""
    px = max(float(resolution_m), float(output_res_m or 0.0))
    return {t["filename"]: (_tile_region(t, sector_bbox, px) if partial
                            else None)
            for t in subtiles}


def _merge_regions(regions: Sequence[Dict[str, float]]
                   ) -> List[Dict[str, float]]:
    """*regions* without duplicates and without a region lying inside
    another one, in a stable order."""
    def inside(a: Dict[str, float], b: Dict[str, float]) -> bool:
        return (b["west"] <= a["west"] and a["east"] <= b["east"]
                and b["south"] <= a["south"] and a["north"] <= b["north"])
    uniq: List[Dict[str, float]] = []
    for r in sorted((dict(r) for r in regions),
                    key=lambda r: (-_bbox_area(r), r["south"], r["west"],
                                   r["north"], r["east"])):
        if not any(inside(r, u) for u in uniq):
            uniq.append(r)
    return sorted(uniq, key=lambda r: (r["south"], r["west"], r["north"],
                                       r["east"]))


def _part_paths(pool_dir: str, name: str) -> Tuple[str, str]:
    return (os.path.join(pool_dir, name + _PART_SUFFIX),
            os.path.join(pool_dir, name + _PART_META_SUFFIX))


def _read_part_meta(pool_dir: str, spec: Dict[str, Any],
                    listing: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The ``.part.json`` of tile *spec* when it is BOUND — the ``.part``
    is whole (the tile rule, :func:`_listed_tile_whole`), its size + mtime
    are the ones the sidecar recorded, the extent is this tile's — else
    None (absent, corrupt, or stale: treated as no partial tile)."""
    name = spec["filename"]
    entry = listing.get(name + _PART_SUFFIX)
    if entry is None or (name + _PART_META_SUFFIX) not in listing:
        return None
    try:
        with open(os.path.join(pool_dir, name + _PART_META_SUFFIX), "r",
                  encoding="utf-8") as fh:
            meta = json.load(fh)
        st = entry.stat()
        if (int(meta["abt_size"]) != st.st_size
                or int(meta["abt_mtime"]) != st.st_mtime_ns):
            return None
        extent = _subtile_bbox(spec)
        if any(abs(float(meta["extent"][k]) - extent[k]) > 1e-9
               for k in ("west", "east", "south", "north")):
            return None
        regions = [{"bbox": {k: float(r["bbox"][k])
                             for k in ("west", "east", "south", "north")},
                    "fp": r.get("fp")} for r in meta["regions"]]
        if not regions:
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not _listed_tile_whole(listing, spec, pool_dir, name + _PART_SUFFIX):
        return None
    return {"regions": regions, "mtime_ns": st.st_mtime_ns}


def _write_part_meta(pool_dir: str, spec: Dict[str, Any],
                     regions: List[Dict[str, Any]],
                     st: Optional[os.stat_result] = None) -> bool:
    """Write the ``.part.json`` of tile *spec* (temp + rename), bound to
    the ``.part`` file's size + mtime — *st* (the build's own file, statted
    before it was renamed into place, so a concurrent build's file can never
    inherit these regions) or the file as it is now. *regions*:
    ``[{"bbox", "fp"}]``."""
    part, meta_path = _part_paths(pool_dir, spec["filename"])
    tmp = f"{meta_path}.{os.getpid()}.tmp"
    try:
        st = st or os.stat(part)
        meta = {"extent": _subtile_bbox(spec), "regions": regions,
                "abt_size": st.st_size, "abt_mtime": st.st_mtime_ns}
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(meta, fh)
        replace_with_retry(tmp, meta_path)
        return True
    except OSError as exc:
        _remove_quietly(tmp)
        _log(f"  WARNING: could not record the regions of "
             f"{os.path.basename(part)}: {exc}")
        return False


def _remove_quietly(*paths: str) -> None:
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            pass


def resolve_pool_tile(pool_dir: str, spec: Dict[str, Any],
                      region: Optional[Dict[str, float]] = None,
                      fp_of: Optional[Callable[[Dict[str, float]],
                                               Optional[str]]] = None,
                      listing: Optional[Dict[str, Any]] = None,
                      extra_regions: Optional[
                          Sequence[Dict[str, float]]] = None,
                      for_link: bool = False) -> PoolTile:
    """THE pool hit rule for one tile of one job — the only way a ``.part``
    is ever reached (prepare_terrain, prepare_terrain_profile, the views,
    terrain_plan; Processing, the P2P tab and the torture runner come
    through those).

    *region*: the part of the tile the job needs (:func:`tile_regions`;
    None = all of it). *fp_of(bbox)*: :func:`tile_source_fingerprint` of the
    files feeding *bbox* (None = no fingerprint check: XYZ / rendered
    pools, or the pre-run estimate without the folder index).

    1. complete ``<name>`` + current ``.src`` (``fp_of`` of the whole tile),
       no ``.rebuild`` flag → hit (today's rule);
    2. else a bound ``.part`` (:func:`_read_part_meta`) whose regions cover
       *region*, every region's fingerprint current, no ``.rebuild`` flag →
       hit;
    3. else miss: rebuild *region* ∪ the ``.part``'s regions ∪
       *extra_regions* (the run's union, :func:`run_region_union`) — the
       whole tile (``regions`` None) when that union covers it.

    *for_link* (the view step, after the build decided): no flag and no
    fingerprint check — the complete tile, or a bound ``.part`` covering
    *region*; when both are there the newer one (the run's latest build).
    """
    name = spec["filename"]
    if listing is None:
        listing = _pool_listing(pool_dir)
    if for_link:
        return _link_verdict(pool_dir, spec, region, listing)
    flagged = (name + _REBUILD_SUFFIX) in listing
    reason = "missing"
    if _listed_tile_ready(listing, spec, pool_dir):
        # Whole and not flagged: today's rule, decided before any .part.
        if fp_of is None or (
                (_read_source_fp(pool_dir, name)
                 if name + _SOURCE_FP_SUFFIX in listing else None)
                == fp_of(_subtile_bbox(spec))):
            return PoolTile(True, os.path.join(pool_dir, name), False, None,
                            "hit")
        reason = "stale"
    elif flagged and _listed_tile_whole(listing, spec, pool_dir):
        reason = "flagged"
    meta = _read_part_meta(pool_dir, spec, listing)
    if meta is not None and region is not None:
        state = _part_state(meta, region, fp_of, flagged)
        if state == "hit-part":
            return PoolTile(True, os.path.join(pool_dir, name + _PART_SUFFIX),
                            True, None, state)
        if not (state == "outside" and reason != "missing"):
            reason = state
    return _miss_verdict(spec, region,
                         [r["bbox"] for r in meta["regions"]] if meta else [],
                         extra_regions, reason)


def _part_state(meta: Dict[str, Any], region: Dict[str, float],
                fp_of: Optional[Callable[[Dict[str, float]], Optional[str]]],
                flagged: bool) -> str:
    """Step 2 of :func:`resolve_pool_tile` for a bound ``.part``:
    ``hit-part``, or why not (``flagged`` / ``outside`` / ``stale``)."""
    if flagged:
        return "flagged"
    if not _rects_cover([r["bbox"] for r in meta["regions"]], region):
        return "outside"
    if fp_of is None or all(fp_of(r["bbox"]) == r["fp"]
                            for r in meta["regions"]):
        return "hit-part"
    return "stale"


def _miss_verdict(spec: Dict[str, Any], region: Optional[Dict[str, float]],
                  known: List[Dict[str, float]],
                  extra_regions: Optional[Sequence[Dict[str, float]]],
                  reason: str) -> PoolTile:
    """Step 3 of :func:`resolve_pool_tile`: what a rebuild covers — *region*
    ∪ the ``.part``'s regions (*known*) ∪ the run's union; the whole tile
    when the job needs all of it or the union covers it."""
    if region is None:
        return PoolTile(False, None, False, None, reason)
    union = _merge_regions(list(known) + [region] + list(extra_regions or ()))
    if _rects_cover(union, _subtile_bbox(spec)):
        return PoolTile(False, None, False, None, reason)
    return PoolTile(False, None, True, union, reason)


def _link_verdict(pool_dir: str, spec: Dict[str, Any],
                  region: Optional[Dict[str, float]],
                  listing: Dict[str, Any]) -> PoolTile:
    """``resolve_pool_tile(for_link=True)``: the complete tile, or a bound
    ``.part`` covering *region* — the newer of the two when both are there
    (the run's latest build); flags and fingerprints were the build's
    business."""
    name = spec["filename"]
    whole = PoolTile(True, os.path.join(pool_dir, name), False, None, "hit")
    part = PoolTile(True, os.path.join(pool_dir, name + _PART_SUFFIX), True,
                    None, "hit-part")
    complete = _listed_tile_whole(listing, spec, pool_dir)
    meta = _read_part_meta(pool_dir, spec, listing)
    covers = (meta is not None and region is not None
              and _rects_cover([r["bbox"] for r in meta["regions"]], region))
    if complete and covers:
        try:
            if meta["mtime_ns"] > listing[name].stat().st_mtime_ns:
                return part
        except (OSError, KeyError):
            pass
        return whole
    if complete:
        return whole
    if covers:
        return part
    return PoolTile(False, None, False, None, "missing")


def run_region_union(jobs: Sequence[Any]) -> Dict[str, List[Dict[str, float]]]:
    """Per tile name, the regions ALL *jobs* of one run need — so a tile
    several jobs share is built once, with their union, by the first of them
    (``prepare_terrain(..., region_union=...)``, also through
    :func:`prepare_terrain_profile`). *jobs*: CoverageParams-like objects
    (``tx_lat``, ``tx_lon``, ``max_range_km``, ``resolution_m``,
    ``az_start``, ``az_end``, ``range_profile``). A job needing a whole tile
    contributes the tile's own extent. Geometry only — no I/O."""
    out: Dict[str, List[Dict[str, float]]] = {}

    def add(tiles: List[Dict[str, Any]], bbox: Dict[str, float],
            res: float, output_res: Optional[float]) -> None:
        regions = tile_regions(tiles, bbox, res, output_res)
        for t in tiles:
            r = regions[t["filename"]] or _subtile_bbox(t)
            out.setdefault(t["filename"], []).append(r)

    for p in jobs:
        bands = getattr(p, "range_profile", None)
        if bands:
            for (_i, _until_km, step, _inner, bbox, tiles,
                 _skip) in _profile_band_tiles(p.tx_lat, p.tx_lon, bands,
                                               p.az_start, p.az_end):
                add(tiles, bbox, step, p.resolution_m)
        else:
            bbox = _compute_sector_bbox(p.tx_lat, p.tx_lon, p.max_range_km,
                                        p.az_start, p.az_end)
            add(_compute_subtiles(bbox, p.resolution_m, p.tx_lat, p.tx_lon,
                                  p.max_range_km, p.az_start, p.az_end),
                bbox, p.resolution_m, None)
    return {name: _merge_regions(rs) for name, rs in out.items()}


def _partial_capable_source(source: str) -> bool:
    """Can a pool of *source* hold partial tiles? Files and folders yes (the
    pre-run estimate's guess without the folder index); XYZ and rendered
    services never."""
    if not source or "type=xyz" in source:
        return False
    base = source.partition("|")[0]
    return (os.path.isdir(source) or os.path.isfile(base)
            or source.startswith("/vsi"))


#: Kept for callers that imported it: the folder scan's extension list
#: now lives in core/terrain_index (RASTER_EXTS, zip members included).
_TERRAIN_EXTS = tuple(sorted(terrain_index.RASTER_EXTS))


def list_terrain_files(terrain_dir: str) -> List[str]:
    """Return every terrain raster under *terrain_dir* (recursive), sorted.

    Any GDAL-readable raster format the index knows (GeoTIFF, .dem/.hgt,
    .asc, .img, .bil, gridded .xyz, .jp2, single-variable .nc, …) plus the
    raster members of ``.zip`` archives as ``/vsizip/`` paths. Excluded:
    ``.vrt`` (see ``terrain_index.EXCLUDED_EXTS`` — a mosaic of the files
    beside it), every sidecar (``.aux.xml``, ``.ovr``, ``.msk``, world
    files, ``.hdr``) and overviews.

    Sorted ascending by full path: the folder's FILE-NAME order (one of the
    two overlap-priority rules; the default is finest resolution first —
    ``terrain_index.order_infos``). os.walk order is filesystem-dependent —
    unsorted, two machines with the same folder would build different
    pixels. This is THE folder scanner: the Map Converter uses it too.
    """
    return terrain_index.list_rasters(terrain_dir)


def list_abt_tiles(terrain_dir: str) -> List[str]:
    """Return the ``.abt`` tiles directly inside *terrain_dir*.

    Deliberately **not** recursive: the engine resolves terrain with a plain
    `read_dir` over the one directory it is given (`engines/coverage.rs`,
    `engines/p2p.rs`, `engines/cpu_coverage.rs`), so tiles in sub-directories
    are invisible to it. Reporting them here would promise coverage the run
    would not have.

    One ``scandir`` through the folder's header index
    (:func:`abt_index.tile_names`), reused for ``abt_index.FRESH_S`` while
    the folder's own mtime holds — it used to be a listing plus one
    ``isfile`` per file on every call (~5 s for 2,463 tiles on the
    container's Windows mount, several calls per run).
    """
    if not terrain_dir or not os.path.isdir(terrain_dir):
        return []
    return [os.path.join(terrain_dir, n)
            for n in abt_index.tile_names(terrain_dir)]


def is_abt_tile_dir(terrain_dir: Optional[str]) -> bool:
    """True if *terrain_dir* holds pre-built ``.abt`` tiles.

    A directory of `.abt` files is a finished engine input, not a source to
    convert — the two look alike in a folder picker but must not be conflated:
    handing `.abt` files to the converter as `sources` would fail, and running
    the conversion at all would waste the work the Map Converter already did.
    Answered from the memoised listing (:func:`list_abt_tiles`), so asking
    again within a run costs one ``stat`` of the folder.
    """
    return bool(list_abt_tiles(terrain_dir)) if terrain_dir else False


# ---------------------------------------------------------------------------
# The Map Converter's record of an output folder (.waveshed_tiles.json)
# ---------------------------------------------------------------------------

#: The manifest the Map Converter keeps in its output folder: read back by
#: the Map Converter (rebuild by fingerprint) and, when that folder is used
#: as prepared terrain, by Site Analysis and P2P (do its tiles carry
#: buildings? — :func:`prepared_buildings`).
TILES_MANIFEST = ".waveshed_tiles.json"
#: Schema 2 (2026-10-23): ``{"schema": 2, "tiles": {name: fingerprint},
#: "buildings": {name: record}}``; schema 1 had ``tiles`` only (buildings
#: unknown). ``tiles`` keeps its schema-1 meaning, so an older plugin reads
#: a schema-2 file's fingerprints unchanged (and drops the buildings
#: records when it rewrites it — "unknown" again, never a wrong record).
#: Optional key ``"voids"`` (2026-10-25, no schema change): name →
#: ``{"voids": n, "abt_size": bytes, "abt_mtime": ns}`` — the converter's
#: ``[Tile] … voids=`` count (:func:`parse_tile_voids_line`) of a terrain
#: POOL tile, bound like a ``.part.json`` to the size + mtime of the file it
#: describes. The same file in a pool directory holds only these records
#: (written by the download / Site Analysis ingest, read by the views,
#: :func:`_view_voids`); absent, unbound or malformed = unknown. Written
#: only when a record exists, so a manifest without one is byte-identical to
#: before; an older plugin reads the file unchanged and drops the key when
#: it rewrites it (unknown again).
TILES_MANIFEST_SCHEMA = 2
#: A tile's buildings record — what its job burned, written only once the
#: converter's report of that batch was checked (``osm_buildings``
#: ``BurnMonitor``); sources join with "+" (``file+openfreemap:z14``).
BUILDINGS_OSM = _OSM_BUILDINGS_SOURCE_ID
BUILDINGS_FILE = "file"
BUILDINGS_NONE = "none"
#: Written while the buildings could not be verified (the tile is rebuilt
#: by the next Map Converter run; its fingerprint record is no fingerprint).
BUILDINGS_UNVERIFIED = "unverified"


class TilesManifest(NamedTuple):
    """One output folder's manifest: ``schema`` (0 = none readable),
    ``tiles`` name → fingerprint record, ``buildings`` name → buildings
    record (schema >= 2 only), ``voids`` name → void-count record (the
    optional key, schema >= 2; see :data:`TILES_MANIFEST_SCHEMA`)."""
    schema: int
    tiles: Dict[str, str]
    buildings: Dict[str, str]
    voids: Dict[str, Dict[str, int]] = types.MappingProxyType({})


def _voids_records(raw: Any) -> Dict[str, Dict[str, int]]:
    """The well-formed records of a manifest's ``"voids"`` value (an
    absent key, or a record without a non-negative integer count and its
    binding, is no record — unknown)."""
    out: Dict[str, Dict[str, int]] = {}
    if not isinstance(raw, dict):
        return out
    for name, rec in raw.items():
        try:
            n, size, mtime = (int(rec["voids"]), int(rec["abt_size"]),
                              int(rec["abt_mtime"]))
        except (KeyError, TypeError, ValueError):
            continue
        if n >= 0 and size >= 0:
            out[str(name)] = {"voids": n, "abt_size": size,
                              "abt_mtime": mtime}
    return out


def read_tiles_manifest(folder: str) -> TilesManifest:
    """*folder*'s :data:`TILES_MANIFEST`, schema 1 or 2 (schema 1: no
    buildings or void-count records); an absent or unreadable file is
    schema 0, empty."""
    try:
        with open(os.path.join(folder, TILES_MANIFEST), "r",
                  encoding="utf-8") as fh:
            doc = json.load(fh)
        tiles = dict(doc.get("tiles") or {})
        try:
            schema = int(doc.get("schema") or 1)
        except (TypeError, ValueError):
            schema = 1
        records = (dict(doc.get("buildings") or {}) if schema >= 2 else {})
        voids = _voids_records(doc.get("voids")) if schema >= 2 else {}
        return TilesManifest(schema, tiles,
                             {k: str(v) for k, v in records.items() if v},
                             voids)
    except (OSError, ValueError, AttributeError, TypeError):
        return TilesManifest(0, {}, {}, {})


#: ONE lock around every read-modify-write of a manifest: the ingest
#: batches of a run record their tiles one after the other, never over
#: each other's records.
_TILES_MANIFEST_LOCK = threading.Lock()


def write_tiles_manifest(folder: str, updates: Dict[str, Optional[str]],
                         buildings: Optional[Dict[str, Optional[str]]] = None,
                         voids: Optional[Dict[str, Optional[Dict[str, int]]]]
                         = None) -> None:
    """Merge *updates* (name → fingerprint; a false value drops the tile's
    records) and their *buildings* records (name → record; a name of
    *updates* without one has none afterwards — unknown) into *folder*'s
    manifest, written as schema :data:`TILES_MANIFEST_SCHEMA`:
    read-modify-write under one lock, a temp file beside it moved over it
    (:func:`replace_with_retry`), so a reader never sees half a file and no
    writer loses another's records. Best effort: a write that fails leaves
    the previous manifest. *voids* (name → void-count record, a false value
    drops it) is merged on its own — a name it does not mention keeps its
    record; the ``"voids"`` key is written only while a record exists."""
    path = os.path.join(folder, TILES_MANIFEST)
    with _TILES_MANIFEST_LOCK:
        cur = read_tiles_manifest(folder)
        tiles, records = dict(cur.tiles), dict(cur.buildings)
        counts = dict(cur.voids)
        for name, fp in updates.items():
            rec = (buildings or {}).get(name)
            if fp:
                tiles[name] = fp
            else:
                tiles.pop(name, None)
                counts.pop(name, None)
            if fp and rec:
                records[name] = rec
            else:
                records.pop(name, None)
        for name, count in (voids or {}).items():
            if count:
                counts[name] = dict(count)
            else:
                counts.pop(name, None)
        doc: Dict[str, Any] = {"schema": TILES_MANIFEST_SCHEMA,
                               "tiles": tiles, "buildings": records}
        if counts:
            doc["voids"] = counts
        tmp = f"{path}.{os.getpid()}-{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            replace_with_retry(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Void counts of pool tiles (aether_converter >= 0.2.11)
# ---------------------------------------------------------------------------
#
# A Site Analysis view fills a pool tile's voids to 0 m (_fill_view_voids);
# knowing WHICH tiles have any used to mean reading every one
# (_tile_has_voids): 119 tiles of 2 m, 7.3 GB memory-mapped, 18 s on the
# owner's machine (2026-10-24) for the two that had voids. The converter
# >= 0.2.11 prints each written tile's count (parse_tile_voids_line); it is
# recorded in the pool's TILES_MANIFEST ("voids", bound to the file's size
# + mtime), and the view links a tile recorded void-free without reading
# it, fills one recorded with voids without a scan first, and scans only a
# tile without a bound record — exactly as before.

def _voids_record(path: str, voids: int) -> Optional[Dict[str, int]]:
    """The void-count record of the file at *path* — *voids* bound to its
    size + mtime now; None when it cannot be stat'ed."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return {"voids": int(voids), "abt_size": st.st_size,
            "abt_mtime": st.st_mtime_ns}


def _bound_voids(rec: Any, st: os.stat_result) -> Optional[int]:
    """*rec*'s count when it is bound to the file *st* describes (same size
    and mtime), else None."""
    try:
        if (rec and int(rec["abt_size"]) == st.st_size
                and int(rec["abt_mtime"]) == st.st_mtime_ns):
            n = int(rec["voids"])
            return n if n >= 0 else None
    except (KeyError, TypeError, ValueError):
        pass
    return None


def _record_pool_voids(pool_dir: str,
                       records: Dict[str, Optional[Dict[str, int]]]) -> None:
    """Merge *records* (name → void-count record; None = unknown, the
    tile's record dropped) into the pool's manifest. Only drops and none of
    them recorded → nothing is written (no manifest appears in a pool whose
    converter prints no counts). Best effort: never fails a run."""
    if not records:
        return
    try:
        if not any(records.values()):
            held = read_tiles_manifest(pool_dir).voids
            if not any(name in held for name in records):
                return
        write_tiles_manifest(pool_dir, {}, None, voids=records)
    except Exception as exc:  # noqa: BLE001 — a record never fails a run
        _log(f"  (void counts of {len(records)} pool tile(s) not recorded: "
             f"{exc})")


def _view_voids(pool_dir: str, names: Sequence[str],
                tiles: Optional[Dict[str, Tuple[Dict[str, Any],
                                                Optional[Dict[str, float]]]]
                       ] = None,
                run: Optional[Dict[str, Dict[str, int]]] = None
                ) -> Dict[str, Optional[int]]:
    """*voids* for :func:`_sync_view`: per name, the recorded void count of
    the pool file the view links — this run's record (*run*, name → record)
    first, else the pool manifest's (a cache HIT: an earlier run's) — when
    it is bound to that file as it is now; None = unknown (no record, or a
    record of another file). *tiles* as for :func:`_sync_view`: a
    ``.part``-backed name resolves its file the way the view does
    (:func:`resolve_pool_tile`, ``for_link``) and keeps the scan unless a
    record is bound to that file."""
    held = read_tiles_manifest(pool_dir).voids
    if not held and not run:
        return {name: None for name in names}
    listing = _pool_listing(pool_dir) if tiles else {}
    out: Dict[str, Optional[int]] = {}
    for name in names:
        if tiles is not None and name in tiles:
            spec, region = tiles[name]
            src: Optional[str] = resolve_pool_tile(
                pool_dir, spec, region, listing=listing, for_link=True).path
        else:
            src = os.path.join(pool_dir, name)
        count: Optional[int] = None
        try:
            st = os.stat(src) if src is not None else None
        except OSError:
            st = None
        if st is not None:
            for rec in ((run or {}).get(name), held.get(name)):
                count = _bound_voids(rec, st)
                if count is not None:
                    break
        out[name] = count
    return out


#: :func:`prepared_buildings` states.
PREPARED_ALL = "all"
PREPARED_NONE = "none"
PREPARED_MIXED = "mixed"
PREPARED_UNKNOWN = "unknown"


class PreparedBuildings(NamedTuple):
    """Do the tiles a run takes from a prepared ``.abt`` folder carry
    buildings? ``state``: ``all`` / ``none`` (every tile recorded with /
    without), ``unknown`` (no tile has a record — no manifest, schema 1, a
    tile not built by the Map Converter, or ``unverified``), else ``mixed``.
    ``sources``: the records of the tiles with buildings."""
    state: str
    with_buildings: int
    without: int
    unknown: int
    sources: Tuple[str, ...]

    @property
    def total(self) -> int:
        return self.with_buildings + self.without + self.unknown

    def counts(self) -> str:
        """``"3 with buildings (openfreemap:z14), 1 without, 2 not
        recorded — of 6 tile(s)"``."""
        parts = []
        if self.with_buildings:
            parts.append(f"{self.with_buildings:,} with buildings"
                         + (f" ({', '.join(self.sources)})"
                            if self.sources else ""))
        if self.without:
            parts.append(f"{self.without:,} without")
        if self.unknown:
            parts.append(f"{self.unknown:,} not recorded")
        return f"{', '.join(parts) or 'none'} — of {self.total:,} tile(s)"


def prepared_buildings(terrain_dir: str, tiles: Iterable[str]
                       ) -> PreparedBuildings:
    """The buildings records (:func:`read_tiles_manifest`, schema 2) of
    *tiles* — the names or paths of the tiles a run SELECTED from the
    prepared folder *terrain_dir* (``select_abt_tiles``). One manifest read,
    GUI-free; Site Analysis (dialogs) and P2P (a log line) share it."""
    names = sorted({os.path.basename(str(t)) for t in tiles})
    man = read_tiles_manifest(terrain_dir)
    with_b = without = unknown = 0
    sources: set = set()
    for name in names:
        rec = man.buildings.get(name)
        if not rec or rec == BUILDINGS_UNVERIFIED:
            unknown += 1
        elif rec == BUILDINGS_NONE:
            without += 1
        else:
            with_b += 1
            sources.update(r for r in rec.split("+") if r)
    total = len(names)
    if total == 0 or unknown == total:
        state = PREPARED_UNKNOWN
    elif with_b == total:
        state = PREPARED_ALL
    elif without == total:
        state = PREPARED_NONE
    else:
        state = PREPARED_MIXED
    return PreparedBuildings(state, with_b, without, unknown,
                             tuple(sorted(sources)))


def prepared_buildings_line(state: PreparedBuildings, requested: bool
                            ) -> Optional[str]:
    """One log line on the buildings of a prepared folder's selected tiles
    (``WARNING:`` when buildings were *requested* and are not all there;
    None when none were requested and none are recorded)."""
    counts = state.counts()
    if not requested:
        if state.with_buildings:
            return (f"buildings: the prepared terrain carries buildings "
                    f"({counts}) although none were requested — they are "
                    f"part of its tiles")
        return None
    if state.state == PREPARED_ALL:
        return (f"buildings: the prepared terrain already carries buildings "
                f"({counts}) — nothing is added")
    if state.state == PREPARED_NONE:
        return (f"WARNING: buildings were requested, but the prepared "
                f"terrain carries none ({counts}) — they cannot be added to "
                f"a prepared folder; build it with buildings in the Map "
                f"Converter")
    if state.state == PREPARED_MIXED:
        return (f"WARNING: buildings were requested; the prepared terrain "
                f"carries them only in part ({counts}) — nothing is added")
    return (f"WARNING: buildings were requested, but whether the prepared "
            f"terrain carries any is not recorded ({counts}; no Map "
            f"Converter record of its buildings) — nothing is added to a "
            f"prepared folder")


def _log_prepared_buildings(terrain_dir: str, selection: "AbtSelection",
                            requested: bool) -> None:
    """Log :func:`prepared_buildings_line` for *selection*'s tiles (the
    prepared-folder routes of prepare_terrain / prepare_terrain_profile —
    Processing and the torture runner see no dialog)."""
    try:
        state = prepared_buildings(terrain_dir, selection.paths)
    except Exception as exc:  # noqa: BLE001 — a note never stops a run
        _log(f"  (prepared-terrain buildings check skipped: {exc})")
        return
    line = prepared_buildings_line(state, requested)
    if line:
        _log(f"  {line}")


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


class AbtSelection(NamedTuple):
    """Which pre-built tiles a run uses — chosen here, never by the engine.

    ``paths``: exactly one tile per footprint used. ``substitutions``:
    human-readable lines for every footprint that had to take a resolution
    COARSER than the ground it serves needs — shown to the user before the
    run, never silent. ``summary``: one log line, e.g. ``2 m × 6 tile(s),
    30 m × 12 tile(s)``. ``finer``: footprints that took a FINER resolution
    than needed (closest the folder had there; logged, no dialog).
    ``uncovered``: 0.25° cells (``north/south/west/east`` dicts) of the
    sector ∩ folder extent that the folder has data for but no chosen tile
    covers — the engine would see NODATA there. Expected empty (see
    :func:`select_abt_headers`); listed, never silent, if it ever is not.
    """

    paths: List[str]
    substitutions: List[str]
    summary: str
    finer: Sequence[str] = ()
    uncovered: Sequence[Dict[str, float]] = ()


def _abt_nominal_res(header: Any) -> int:
    """A tile's resolution in metres, snapped to the plugin's list."""
    res = header.pixel_res * 111_111.0
    return min(VALID_RESOLUTIONS, key=lambda r: abs(r - res))


def _rect_distance_range_m(n: float, s: float, w: float, e: float,
                           tx_lat: float, tx_lon: float) -> Tuple[float, float]:
    """(nearest, farthest) distance in metres from the TX to a lat/lon rect."""
    m_lat = 111_111.0
    m_lon = 111_111.0 * max(math.cos(math.radians(tx_lat)), 1e-6)
    cx = min(max(tx_lon, w), e)
    cy = min(max(tx_lat, s), n)
    near = math.hypot((cx - tx_lon) * m_lon, (cy - tx_lat) * m_lat)
    far = max(math.hypot((x - tx_lon) * m_lon, (y - tx_lat) * m_lat)
              for x in (w, e) for y in (s, n))
    return near, far


#: Edge (degrees) of the uncovered-cell check grid of :func:`select_abt_headers`.
ABT_HOLE_CELL_DEG = 0.25

#: Smallest bucket edge (degrees) of the rectangle lookups in
#: :func:`select_abt_headers` (the smallest footprint span otherwise, ≤ 1°).
_SEL_BUCKET_MIN_DEG = 0.05


class _RectBuckets:
    """Rectangles ``(n, s, w, e)`` bucketed on a square grid, for point
    lookups (inclusive edges)."""

    def __init__(self, cell: float) -> None:
        self.cell = cell
        self.grid: Dict[Tuple[int, int], List[Any]] = {}

    def add(self, item: Any, rect: Tuple[float, float, float, float]) -> None:
        n, s_, w, e = rect
        c, grid = self.cell, self.grid
        cols = range(math.floor(w / c), math.floor(e / c) + 1)
        for i in range(math.floor(s_ / c), math.floor(n / c) + 1):
            for j in cols:
                got = grid.get((i, j))
                if got is None:
                    grid[(i, j)] = [item]
                else:
                    got.append(item)

    def at(self, lat: float, lon: float) -> List[Any]:
        return self.grid.get((math.floor(lat / self.cell),
                              math.floor(lon / self.cell)), [])


def _cut_lines(values: List[float], eps: float = 1e-9) -> List[float]:
    """*values* (the two ends first) sorted, without repeats closer than
    *eps* — the ends kept exactly."""
    lo, hi = values[0], values[1]
    out = [lo]
    for v in sorted(values[2:]):
        if v - out[-1] > eps and hi - v > eps:
            out.append(v)
    out.append(hi)
    return out


def select_abt_headers(headers: Sequence[Any], tx_lat: float, tx_lon: float,
                       bands: Sequence[Tuple[float, int]],
                       az_start: float = 0.0,
                       az_end: float = 360.0) -> AbtSelection:
    """:func:`select_abt_tiles` on headers already read (``abt.TileHeader``
    sequence, sorted by path) — pure geometry, no file access.

    A footprint (corner + extent) is in play when it meets the sector bbox
    and a band ring; its *wanted step* is the finest step any ring crossing
    it needs. Only its NEEDED part counts: footprint ∩ sector bbox, within
    the outermost ring — cut into cells by the edges of the footprints
    overlapping it and sampled at each cell's point nearest the TX (exact,
    see :meth:`_AbtChooser._points`). Each point wants the step of its
    ring; its *limit* is that step where any footprint there holds it or
    finer, else the finest resolution the folder has there (a true gap).

    * Pass 1: every footprint holding a tile of exactly its wanted step is
      chosen with that tile.
    * Pass 2: a point no chosen tile of resolution ≤ its limit covers is
      open; it belongs to every footprint there whose finest tile is ≤ its
      limit (finer serves, a coarser tile never stands in where the folder
      has the step or finer). A footprint with no open point is left out,
      silently; else it is chosen with its coarsest tile ≤ the finest limit
      of its open points, which serves them all: coarser than the step they
      want → ``substitutions`` (a true gap), finer → ``finer``. Footprints
      are taken best match first — the smallest resolution mismatch
      (ratio), smaller footprint first — and re-ranked when a choice serves
      points they share.

    So every needed point a footprint could serve gets a tile, and a folder
    holding the step over the whole needed part gives exactly that step's
    tiles. The first two-pass version judged coverage over the WHOLE
    footprint at 5×5 sample points: it added a 0.5° 30 m tile for its
    samples outside a 50 km sector, with a false "needs 10 m, only 30 m"
    (six of them on the owner's 10 m run), and a 2 m tile for samples
    between 50 km and the bbox corner — the engine then ran on a 2 m grid
    (VRAM 2.8 instead of 1.4 GB). The single pass before it (until
    2026-10-23) dropped a footprint lacking its step whenever other
    footprints merely HELD that step — never asking whether those were
    themselves chosen; two such footprints dropped each other and left
    holes (the owner's 31.5 / 126 / 500 km run: 44 cells of 0.25°, NODATA
    in the engine, nothing logged). Best match first matters: taking
    smaller footprints first instead picks 2 m tiles for the 30/90 m rings
    of that run (330 tiles, 19.8 GiB, against the engine's ~3.86 GB
    terrain atlas) where 0.5° / 1° tiles of the right step exist.
    """
    if not headers or not bands:
        return AbtSelection([], [], "no tiles in range")
    chooser = _AbtChooser(headers, tx_lat, tx_lon, bands, az_start, az_end)
    if not chooser.rects:
        return AbtSelection([], [], "no tiles in range")
    chooser.pass1()
    chooser.pass2()
    return chooser.result(headers)


class _AbtChooser:
    """The state of one :func:`select_abt_headers` call. Footprint keys are
    ``(north, west, span)`` rounded to 1e-6°."""

    def __init__(self, headers: Sequence[Any], tx_lat: float, tx_lon: float,
                 bands: Sequence[Tuple[float, int]], az_start: float,
                 az_end: float) -> None:
        self.tx_lat, self.tx_lon = tx_lat, tx_lon
        self.max_m = float(bands[-1][0])
        self.sector = _compute_sector_bbox(tx_lat, tx_lon, self.max_m / 1000.0,
                                           az_start, az_end)
        self.rings: List[Tuple[float, float, int]] = []
        inner = 0.0
        for until_m, step in bands:
            self.rings.append((inner, float(until_m), int(step)))
            inner = float(until_m)
        # The metric of _rect_distance_range_m.
        self.m_lon = 111_111.0 * max(math.cos(math.radians(tx_lat)), 1e-6)
        self.rects: Dict[Any, Tuple[float, float, float, float]] = {}
        self.wants: Dict[Any, int] = {}
        self.tiles: Dict[Any, List[Tuple[int, Any]]] = {}
        for h in headers:
            self._add_tile(h)
        self.chosen: Dict[Any, Tuple[int, Any]] = {}
        # Pass 2's open points: (want, limit), the footprints that can serve
        # each (finest tile <= limit), served yet; per footprint its points.
        self.pts: List[Tuple[int, int]] = []
        self.servers: List[List[Any]] = []
        self.done: List[bool] = []
        self.reg: Dict[Any, List[int]] = {}
        self.coarser: Dict[Any, str] = {}
        self.finer: Dict[Any, str] = {}
        self.cell = min(1.0, max(_SEL_BUCKET_MIN_DEG,
                                 min((k[2] for k in self.rects), default=1.0)))
        self.picked = _RectBuckets(self.cell)

    def dist_m(self, lat: float, lon: float) -> float:
        return math.hypot((lat - self.tx_lat) * 111_111.0,
                          (lon - self.tx_lon) * self.m_lon)

    def want_at(self, lat: float, lon: float) -> int:
        return self._step_at(self.dist_m(lat, lon))

    def _step_at(self, d: float) -> int:
        for _lo, hi, step in self.rings:
            if d <= hi:
                return step
        return self.rings[-1][2]       # past the range: the outermost step

    def _add_tile(self, h: Any) -> None:
        """Group *h* into its footprint when that is in play (grouped ONCE;
        tiles kept in path order with their nominal resolution)."""
        span = h.size * h.pixel_res
        n, w = h.ul_lat, h.ul_lon
        s_, e = n - span, w + span
        sec = self.sector
        if (s_ > sec["north"] or n < sec["south"]
                or w > sec["east"] or e < sec["west"]):
            return
        near, far = _rect_distance_range_m(n, s_, w, e, self.tx_lat,
                                           self.tx_lon)
        steps = [st for lo, hi, st in self.rings if near <= hi and far >= lo]
        if not steps:
            return
        key = (round(n, 6), round(w, 6), round(span, 6))
        if key not in self.rects:
            self.rects[key] = (n, s_, w, e)
            self.wants[key] = min(steps)
            self.tiles[key] = []
        self.tiles[key].append((_abt_nominal_res(h), h))

    def pass1(self) -> None:
        """The exact step, wherever the footprint has it."""
        for key in sorted(self.rects):
            exact = [t for t in self.tiles[key] if t[0] == self.wants[key]]
            if exact:
                self.chosen[key] = exact[0]
                self.picked.add(key, self.rects[key])

    def _points(self, key: Any, every: "_RectBuckets"
                ) -> List[Tuple[float, float, int]]:
        """``(lat, lon, wanted step)`` sample points of the footprint's NEEDED
        part (footprint ∩ sector bbox, within the outermost ring): one per
        cell of the grid the edges of the footprints overlapping it cut it
        into — the cell's point nearest the TX, a hair inside.

        Exact: in such a cell the footprints holding it, and so what the
        folder has there and which chosen tiles cover it, do not change, and
        its nearest point wants its finest step (steps never get finer with
        distance), so a cell is served wherever its point is. *every* holds
        the footprints in play as ``(key, n, s, w, e, finest resolution)``.
        """
        n, s_, w, e = self.rects[key]
        sec = self.sector
        n2, s2 = min(n, sec["north"]), max(s_, sec["south"])
        w2, e2 = max(w, sec["west"]), min(e, sec["east"])
        if n2 <= s2 or e2 <= w2:
            return []                      # touches the bbox, needs nothing
        ys, xs = [s2, n2], [w2, e2]
        c = every.cell
        seen = set()
        cols = range(math.floor(w2 / c), math.floor(e2 / c) + 1)
        for i in range(math.floor(s2 / c), math.floor(n2 / c) + 1):
            for j in cols:
                for it in every.grid.get((i, j), ()):
                    if it[0] in seen:
                        continue
                    seen.add(it[0])
                    _k, fn, fs, fw, fe, _f = it
                    if s2 < fn < n2:
                        ys.append(fn)
                    if s2 < fs < n2:
                        ys.append(fs)
                    if w2 < fw < e2:
                        xs.append(fw)
                    if w2 < fe < e2:
                        xs.append(fe)
        ys, xs = _cut_lines(ys), _cut_lines(xs)
        reach = self.max_m
        tla, tlo, m_lon = self.tx_lat, self.tx_lon, self.m_lon
        pts = []
        for y0, y1 in zip(ys, ys[1:]):
            hy = (y1 - y0) * 1e-6
            la = min(max(tla, y0 + hy), y1 - hy)
            dy = (la - tla) * 111_111.0
            if abs(dy) > reach:
                continue
            for x0, x1 in zip(xs, xs[1:]):
                hx = (x1 - x0) * 1e-6
                lo = min(max(tlo, x0 + hx), x1 - hx)
                d = math.hypot(dy, (lo - tlo) * m_lon)
                if d <= reach:
                    pts.append((la, lo, self._step_at(d)))
        return pts

    def _inside_chosen(self, key: Any) -> bool:
        """One footprint pass 1 chose holds the whole of footprint ∩ bbox at
        a resolution ≤ the finest step it wants — every needed point of it
        is served, so its points are never sampled (the common case: a fine
        footprint inside a coarser chosen one)."""
        n, s_, w, e = self.rects[key]
        sec = self.sector
        n2, s2 = min(n, sec["north"]), max(s_, sec["south"])
        w2, e2 = max(w, sec["west"]), min(e, sec["east"])
        want = self.wants[key]
        for k in self.picked.at((n2 + s2) / 2, (w2 + e2) / 2):
            cn, cs, cw, ce = self.rects[k]
            if (self.chosen[k][0] <= want and cs <= s2 and n2 <= cn
                    and cw <= w2 and e2 <= ce):
                return True
        return False

    def _served(self, la: float, lo: float, limit: int) -> bool:
        for k in self.picked.at(la, lo):
            n, s_, w, e = self.rects[k]
            if self.chosen[k][0] <= limit and s_ <= la <= n and w <= lo <= e:
                return True
        return False

    def _register(self, la: float, lo: float, want: int,
                  every: "_RectBuckets") -> None:
        """Book one needed point pass 1 does not serve at its *want*: its
        limit, and — unless pass 1 serves it at that — the footprints that
        can serve it (*every*: see :meth:`_points`)."""
        here = [it for it in every.at(la, lo)
                if it[2] <= la <= it[1] and it[3] <= lo <= it[4]]
        if not here:
            return
        best = min(it[5] for it in here)
        limit = want if best <= want else best      # a true gap: the finest
        if limit > want and self._served(la, lo, limit):
            return
        chosen = self.chosen
        servers = [it[0] for it in here
                   if it[5] <= limit and it[0] not in chosen]
        if not servers:
            return
        pid = len(self.pts)
        self.pts.append((want, limit))
        self.servers.append(servers)
        self.done.append(False)
        reg = self.reg
        for k in servers:
            got = reg.get(k)
            if got is None:
                reg[k] = [pid]
            else:
                got.append(pid)

    def rank(self, key: Any):
        """``(sort key, want, (res, header))`` — *want* the finest step its
        open points want — or None when it has no open point (left out)."""
        open_ = [p for p in self.reg.get(key, ()) if not self.done[p]]
        if not open_:
            return None
        need = min(self.pts[p][1] for p in open_)
        want = min(self.pts[p][0] for p in open_)
        fits = [t for t in self.tiles[key] if t[0] <= need]
        if fits:
            # The coarsest tile <= every open limit serves them all (the
            # first such in path order).
            tile = max(fits, key=lambda t: t[0])
        else:                      # not reached: a server holds one <= limit
            tile = min(self.tiles[key], key=lambda t: (abs(t[0] - need), t[0]))
        res = tile[0]
        mismatch = abs(math.log(res / need)) if need > 0 else math.inf
        return ((round(mismatch, 9), res > need, key[2], key), want, tile)

    def pass2(self) -> None:
        """Book every needed point pass 1 leaves open, then best match
        first; a choice re-ranks the footprints sharing the points it
        serves."""
        every: Optional[_RectBuckets] = None
        for key in sorted(self.rects):
            if key in self.chosen or self._inside_chosen(key):
                continue
            if every is None:               # the footprints in play, once
                every = _RectBuckets(self.cell)
                for k, rect in self.rects.items():
                    every.add((k, *rect, min(t[0] for t in self.tiles[k])),
                              rect)
            for la, lo, want in self._points(key, every):
                if not self._served(la, lo, want):      # (limit >= want)
                    self._register(la, lo, want, every)
        version: Dict[Any, int] = {}
        heap: List[Tuple[Any, Any, int]] = []
        for key in sorted(self.reg):
            r = self.rank(key)
            if r is not None:
                version[key] = 0
                heap.append((r[0], key, 0))
        heapq.heapify(heap)
        while heap:
            sort_key, key, ver = heapq.heappop(heap)
            if version.get(key) != ver:
                continue                   # superseded entry
            r = self.rank(key)
            if r is None or r[0] != sort_key:
                self._requeue(key, r, heap, version)
                continue
            version.pop(key)
            for other in sorted(self._choose(key, r[1], r[2])):
                if other in version:
                    self._requeue(other, self.rank(other), heap, version)

    @staticmethod
    def _requeue(key: Any, r: Any, heap: List[Tuple[Any, Any, int]],
                 version: Dict[Any, int]) -> None:
        """Push *key* with its current rank *r*; None = no open point, left
        out."""
        if r is None:
            version.pop(key, None)
            return
        version[key] += 1
        heapq.heappush(heap, (r[0], key, version[key]))

    def _choose(self, key: Any, need: int, tile: Tuple[int, Any]) -> set:
        """Choose *key* with *tile* (*need* = the finest step its open points
        want, for the log lines); returns the other footprints sharing the
        points it serves."""
        self.chosen[key] = tile
        self.picked.add(key, self.rects[key])
        touched = set()
        for p in self.reg.get(key, ()):
            if not self.done[p] and self.pts[p][1] >= tile[0]:
                self.done[p] = True
                touched.update(self.servers[p])
        touched.discard(key)
        n, _s, w, _e = self.rects[key]
        label = f"N{n:.2f} E{w:.2f} ({key[2]:g}°)"
        if tile[0] > need:
            self.coarser[key] = (f"{label}: needs {need} m, the folder has "
                                 f"only {tile[0]} m there")
        elif tile[0] < need:
            self.finer[key] = (f"{label}: needs {need} m, the closest the "
                               f"folder has there is {tile[0]} m (finer, used "
                               "as is)")
        return touched

    def result(self, headers: Sequence[Any]) -> AbtSelection:
        counts: Dict[int, int] = {}
        for res, _h in self.chosen.values():
            counts[res] = counts.get(res, 0) + 1
        summary = ", ".join(f"{r} m × {c} tile(s)"
                            for r, c in sorted(counts.items()))
        return AbtSelection(
            [self.chosen[k][1].path for k in sorted(self.chosen)],
            [self.coarser[k] for k in sorted(self.coarser)],
            summary or "no tiles in range",
            [self.finer[k] for k in sorted(self.finer)],
            _abt_uncovered_cells(headers, self))


def _abt_uncovered_cells(headers: Sequence[Any],
                         chooser: "_AbtChooser") -> List[Dict[str, float]]:
    """The :data:`ABT_HOLE_CELL_DEG` cells whose centre lies in the sector
    bbox, within the range and inside a tile of the folder, but in no
    chosen tile."""
    spans = [(h.ul_lat, h.ul_lat - h.size * h.pixel_res, h.ul_lon,
              h.ul_lon + h.size * h.pixel_res) for h in headers]
    sec = chooser.sector
    north = min(sec["north"], max(r[0] for r in spans))
    south = max(sec["south"], min(r[1] for r in spans))
    west = max(sec["west"], min(r[2] for r in spans))
    east = min(sec["east"], max(r[3] for r in spans))
    c = ABT_HOLE_CELL_DEG
    folder = _RectBuckets(c)
    for r in spans:
        folder.add(r, r)

    def inside(found, la, lo):
        return any(r[1] <= la <= r[0] and r[2] <= lo <= r[3] for r in found)

    out: List[Dict[str, float]] = []
    for i in range(math.floor(south / c), math.ceil(north / c)):
        for j in range(math.floor(west / c), math.ceil(east / c)):
            la, lo = (i + 0.5) * c, (j + 0.5) * c
            if (south <= la <= north and west <= lo <= east
                    and chooser.dist_m(la, lo) <= chooser.max_m
                    and inside(folder.at(la, lo), la, lo)
                    and not inside((chooser.rects[k]
                                    for k in chooser.picked.at(la, lo)),
                                   la, lo)):
                out.append({"north": (i + 1) * c, "south": i * c,
                            "west": j * c, "east": (j + 1) * c})
    return out


def abt_holes_text(cells: Sequence[Dict[str, float]], limit: int = 6) -> str:
    """One sentence naming the uncovered *cells* (NODATA wording)."""
    shown = ", ".join(
        f"N{c['south']:.2f}–{c['north']:.2f} E{c['west']:.2f}–{c['east']:.2f}"
        for c in list(cells)[:limit])
    more = f" and {len(cells) - limit} more" if len(cells) > limit else ""
    return (f"{len(cells)} area(s) of {ABT_HOLE_CELL_DEG:g}° inside the "
            f"pre-built folder get no tile for this analysis ({shown}{more}). "
            "There is no terrain data there — the engine treats it as NODATA.")


#: How long a selection is reused for the same folder state and job: the
#: GUI pre-check, the coverage warning and the worker's view share one.
_ABT_SELECTION_TTL_S = 60.0
_abt_selections: Dict[Tuple[Any, ...], Tuple[float, AbtSelection]] = {}
_abt_selections_lock = threading.Lock()


def select_abt_tiles(terrain_dir: str, tx_lat: float, tx_lon: float,
                     bands: List[Tuple[float, int]],
                     az_start: float = 0.0, az_end: float = 360.0) -> AbtSelection:
    """Choose the pre-built tiles for a run with the given *bands*
    (``[(until_m, step_m), ...]``; a fixed run is one band).

    aether_core keeps, per footprint (corner + extent), the tile closest to the
    job's ``resolution_m`` and lets finer footprints overwrite coarser ones. That
    choice is silent, and with an output resolution that differs from the
    profile steps it would take e.g. 30 m terrain under a 2 m near field. So the
    plugin decides (:func:`select_abt_headers`: the exact step per footprint,
    then — over the ground the run needs — a tile only where chosen ones do
    not already serve it) and only the chosen tiles go into the view the
    engine sees.

    Headers come from the folder's header index (:func:`abt_index.read_folder`:
    one listing + stat, only new/changed tiles opened, ``FRESH_S`` reuse); the
    result is memoised for :data:`_ABT_SELECTION_TTL_S` per (folder, listing
    signature, TX, bands, azimuths).
    """
    listing = abt_index.read_folder(terrain_dir)
    key = (terrain_dir, listing.signature, float(tx_lat), float(tx_lon),
           tuple((float(u), int(s)) for u, s in bands),
           float(az_start), float(az_end))
    now = time.monotonic()
    with _abt_selections_lock:
        hit = _abt_selections.get(key)
        if hit is not None and now - hit[0] < _ABT_SELECTION_TTL_S:
            return hit[1]
    sel = select_abt_headers(listing.headers, tx_lat, tx_lon, bands,
                             az_start, az_end)
    with _abt_selections_lock:
        for old in [k for k, (t, _s) in _abt_selections.items()
                    if now - t >= _ABT_SELECTION_TTL_S]:
            del _abt_selections[old]
        _abt_selections[key] = (now, sel)
    return sel


def _abt_view_for(terrain_dir: str, tx_lat: float, tx_lon: float,
                  bands: List[Tuple[float, int]], az_start: float,
                  az_end: float) -> str:
    """Select, log and link the pre-built tiles a run uses; substitutions are
    logged as warnings here (the GUI already asked about them before the run),
    finer-than-needed footprints as info, uncovered cells as a warning."""
    sel = select_abt_tiles(terrain_dir, tx_lat, tx_lon, bands, az_start, az_end)
    _log(f"  pre-built terrain chosen: {sel.summary}")
    for line in sel.substitutions:
        _log(f"  WARNING: terrain substitution — {line}")
    for line in sel.finer:
        _log(f"  terrain finer than needed — {line}")
    if sel.uncovered:
        _log(f"  WARNING: {abt_holes_text(sel.uncovered)}")
    view_dir = abt_selection_view(terrain_dir, sel)
    _log(f"  .abt view dir: {view_dir} ({len(sel.paths)} tile(s))")
    _record_view_dir(view_dir)
    return view_dir


def abt_selection_view(terrain_dir: str, selection: AbtSelection) -> str:
    """A view directory holding exactly *selection*'s tiles (hard links, else
    copies), so the engine has nothing left to choose between."""
    names = [os.path.basename(pth) for pth in selection.paths]
    view_dir = _view_dir(terrain_dir, [{"filename": n} for n in names], "abt-select")
    ensure_engine_dirs(view_dir)
    wanted = set(names)
    for stale in os.listdir(view_dir):
        if stale.lower().endswith(".abt") and stale not in wanted:
            try:
                os.remove(os.path.join(view_dir, stale))
            except OSError:
                pass
    for src in selection.paths:
        dst = os.path.join(view_dir, os.path.basename(src))
        if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src):
            continue
        try:
            if os.path.exists(dst):
                os.remove(dst)
            os.link(src, dst)
        except OSError:
            shutil.copyfile(src, dst)
    return view_dir


def _abt_tile_headers(terrain_dir: str, paths: List[str]
                      ) -> Tuple[List[Any], List[str]]:
    """``(headers, unreadable names)`` of *paths* (tiles of *terrain_dir*)
    from the folder's header index; a path it does not know is read here."""
    from . import abt

    listing = abt_index.read_folder(terrain_dir)
    names = {os.path.basename(p) for p in paths}
    headers = [h for h in listing.headers if os.path.basename(h.path) in names]
    unreadable = [os.path.basename(p) for p in listing.unreadable
                  if os.path.basename(p) in names]
    known = {os.path.basename(h.path) for h in headers} | set(unreadable)
    for p in paths:
        if os.path.basename(p) in known:
            continue
        h = abt.read_header(p)
        if h is None:
            unreadable.append(os.path.basename(p))
        else:
            headers.append(h)
    return headers, unreadable


def _abt_coverage_warning(terrain_dir: str, paths: List[str],
                          bbox: Dict[str, float],
                          resolution_m: Optional[float] = None,
                          osm_buildings: bool = False,
                          buildings_file: Optional[str] = None,
                          selections: Optional[Sequence[AbtSelection]] = None,
                          ) -> Optional[str]:
    """Check pre-built ``.abt`` tiles suit this analysis, by their own headers.

    Same contract as the raster check: warn on anything that would silently
    produce fiction. Failure modes specific to `.abt` — a tile whose header
    cannot be read (the engine will skip it), a set that is coarser than the
    requested resolution, buildings that cannot be added because they are
    already burned into the pixels, and ground the folder has data for that
    no chosen tile covers (*selections*' ``uncovered`` cells). Nothing fills
    a pre-built folder's gaps (``abt_selection_view`` only links tiles), so
    missing ground is NODATA to the engine — never 0 m.

    Headers come from the folder's header index (:func:`abt_index.read_folder`)
    — no file is opened again when the selection already read it.
    """
    headers, unreadable = _abt_tile_headers(terrain_dir, paths)
    holes: List[Dict[str, float]] = []
    for sel in selections or ():
        holes.extend(c for c in sel.uncovered if c not in holes)

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
    hole_text = abt_holes_text(holes) + "\n\n" if holes else ""
    if frac < _COVERAGE_OK_FRACTION:
        return (
            f"The .abt tiles in:\n{terrain_dir}\n\ncover only about "
            f"{frac * 100:.0f}% of the analysis area. The rest has no terrain "
            "data — the engine treats it as NODATA (a pre-built folder is "
            f"used as it is; nothing fills its gaps).\n\n{hole_text}{extents}"
        )
    if holes:
        return (
            f"The .abt tiles in:\n{terrain_dir}\n\nleave holes in this "
            f"analysis.\n\n{hole_text}{extents}"
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
                             abt_selections: Optional[Sequence[AbtSelection]] = None,
                             infos: Optional[List[Dict[str, Any]]] = None,
                             ) -> Optional[str]:
    """Warn when *terrain_dir* cannot serve this analysis.

    *resolution_m* and the buildings flags are only consulted for pre-built
    `.abt` directories, where the tiles are finished output and neither can be
    changed by the run — a raster source is converted at whatever resolution is
    asked for, and can have buildings burned in on the way. So are
    *abt_selections* (the run's :func:`select_abt_tiles` results): ground
    the folder has data for but no chosen tile covers is named (NODATA).

    Fires for the unambiguous mistakes — an empty or unreadable directory, or
    one whose data lies somewhere else entirely — and also when the terrain
    covers only part of the analysis area.  Partial coverage is not benign:
    once a terrain directory is selected it is the *only* source
    (``_gather_source_infos``), with no fall back to the base DEM, so
    everything past its edge is built as 0 m and the coverage computed there
    is fiction.  A small tolerance keeps a one-pixel overhang quiet.

    Raster folders are measured from the folder index (per-file WGS84
    footprints, any CRS mix), which also finds GAPS INSIDE the data. The
    index is refreshed when older than ``terrain_index.FRESH_S`` — as slow as
    a folder scan, so never call this on the GUI thread (the Site Analysis
    tab runs it in ``gui/terrain_folder_scan``). *infos* (a raster folder's
    usable source infos as :func:`folder_source_infos` read them — the
    run's :class:`TerrainPrep`) are measured as given: no index look, no
    second build of the file list (the same files, in another order).
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
                                         buildings_file,
                                         selections=abt_selections)

        if not infos:
            index = terrain_index.get_index(terrain_dir)
            index.refresh(max_age_s=terrain_index.FRESH_S)
            if not index.entries:
                # Mention .abt too: a user who points this at Map Converter
                # output nested one level down gets told what is actually
                # wrong.
                return (
                    f"No terrain files were found in:\n{terrain_dir}\n\n"
                    "Expected elevation rasters GDAL can read (GeoTIFF, .dem, "
                    ".hgt, .asc, .img, .bil, .jp2, .nc, … — searched "
                    "recursively, .zip archives included; .vrt files are "
                    "ignored), or pre-built .abt tiles directly in this folder "
                    "(not in sub-folders — the engine does not recurse)."
                )
            # Every file the run would read: usable as is, or 8-bit files
            # the user chose to keep. Problem files are the pre-flight
            # prompt's business (they are skipped or the run is cancelled),
            # so they are not counted as coverage here.
            decisions = terrain_index.approved_decisions(terrain_dir, index)
            infos = []
            for p, e in index.entries.items():
                prob = terrain_index.problem_of(e)
                if prob is None or (
                        prob[0] == terrain_index.KIND_SUSPECT
                        and decisions.get(p) == terrain_index.DECISION_USE):
                    infos.append(terrain_index.entry_info(p, e))
            if not infos:
                return (
                    f"None of the {len(index.entries)} file(s) in:\n"
                    f"{terrain_dir}\n\ncan be used as terrain (see the list "
                    "of unusable files)."
                )
        # Per-file WGS84 footprints, each file in its OWN CRS — never a
        # mosaic. gdal.BuildVRT dropped every file whose CRS differed from
        # the first one and reported the bbox of what was left (a false
        # "reaches only about 22%" for a mixed LV95 + WGS84 folder), and a
        # bbox could never show a missing file in the middle.
        cov = terrain_index.coverage(infos, bbox)
        src = cov.data_bbox or _union_bbox([i["wgs84_bounds"] for i in infos])
        extents = (
            f"Terrain covers ~ N{src['north']:.3f} S{src['south']:.3f} "
            f"E{src['east']:.3f} W{src['west']:.3f}\n"
            f"Analysis needs  ~ N{bbox['north']:.3f} S{bbox['south']:.3f} "
            f"E{bbox['east']:.3f} W{bbox['west']:.3f}\n\n"
        )
        if cov.files == 0 or cov.covered <= 0.0:
            return (
                f"The terrain data in:\n{terrain_dir}\n\ndoes not cover the "
                "analysis area at all.\n\n" + extents +
                "The whole area would be treated as 0 m (sea level), so the "
                "result would be meaningless."
            )
        gap = ""
        if cov.internal_gap > 0.0 and cov.gap_bbox is not None:
            g = cov.gap_bbox
            pct = cov.internal_gap * 100
            gap = (
                f"About {pct:.0f}% of the area ({cov.gap_km2:,.1f} km²) is a "
                f"GAP INSIDE the terrain — ground between the files that no "
                f"file covers (within N{g['north']:.3f} S{g['south']:.3f} "
                f"E{g['east']:.3f} W{g['west']:.3f}). A file is probably "
                f"missing there.\n\n") if pct >= 0.5 else (
                f"There is a GAP INSIDE the terrain ({cov.gap_km2:,.2f} km²) "
                f"— ground between the files that no file covers (within "
                f"N{g['north']:.3f} S{g['south']:.3f} E{g['east']:.3f} "
                f"W{g['west']:.3f}). A file is probably missing there.\n\n")
        if cov.covered >= _COVERAGE_OK_FRACTION:
            if not gap:
                return None
            # Nearly everything is covered, but not by accident: a hole in
            # the middle of the data is a missing file, never an overhang.
            return (
                f"The terrain data in:\n{terrain_dir}\n\nhas a hole.\n\n"
                + gap + "That ground is built as 0 m (sea level) and any "
                "coverage shown there is not real. Add the missing file, or "
                "run anyway if the hole is intended.")
        return (
            f"The terrain data in:\n{terrain_dir}\n\nreaches only about "
            f"{cov.covered * 100:.0f}% of the analysis area.\n\n" + extents
            + gap +
            "Once a terrain directory is selected it is the only source — "
            "there is no fall back to the base DEM — so the remaining "
            f"{(1 - cov.covered) * 100:.0f}% is built as 0 m (sea level) and "
            "any coverage shown there is not real.\n\n"
            "Either shorten the range to what the terrain covers, or add the "
            "missing tiles to the directory."
        )
    except Exception as exc:  # noqa: BLE001 — a pre-flight check must not block.
        _log(f"  (terrain-coverage check skipped: {exc})")
        return None


def _warn_if_bbox_exceeds_bounds(src: Dict[str, float],
                                 bbox: Dict[str, float],
                                 source_key: str = "") -> None:
    """Warn (once per run) when *bbox* reaches beyond WGS84 bounds *src*.

    Non-fatal, bounds-only: with ``void_fill_m: 0.0`` the converter fills the
    gap with 0 m ground, but the user should know part of the result is
    flat-filled rather than real terrain. *source_key* dedups the warning so
    a multi-site/height run shows it once, not per terrain build (see
    :func:`reset_terrain_warnings`).
    """
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


def _warn_if_bbox_exceeds_source(ds, bbox: Dict[str, float],
                                 source_key: str = "") -> None:
    """Bounds-check *bbox* against GDAL dataset *ds* (any CRS), then warn.

    Thin wrapper over :func:`_warn_if_bbox_exceeds_bounds` for callers that
    hold a dataset rather than bounds. Best-effort: a dataset whose extent
    GDAL cannot report is skipped with a log line.
    """
    try:
        src = _dataset_wgs84_bbox(ds)
        if src is None:
            return
    except Exception as exc:
        _log(f"  (DEM-extent check skipped: {exc})")
        return
    _warn_if_bbox_exceeds_bounds(src, bbox, source_key)


def layer_native_resolution_m(layer: Any) -> Optional[float]:
    """Auto-detect a raster layer's ground sampling in metres, or ``None``.

    Lives beside the renderer that consumes it: the export resolution rule
    below needs it on BOTH tabs, and the Map Converter's copy used to be the
    only one — which is how Site Analysis came to export a WCS at the output
    resolution (the server's own pyramid answer, up to 24 m off on slopes)
    while the Map Converter exported near-native.
    """
    from qgis.core import QgsRasterLayer
    if not isinstance(layer, QgsRasterLayer):
        return None
    provider = layer.dataProvider()
    if provider is None or provider.xSize() <= 0:
        return None
    try:
        ext = provider.extent()
        crs = layer.crs()
        if crs.isGeographic():
            clat = (ext.yMinimum() + ext.yMaximum()) / 2.0
            cos_lat = max(math.cos(math.radians(clat)), 0.01)
            x_m = ext.width() / provider.xSize() * 111_111 * cos_lat
            y_m = ext.height() / provider.ySize() * 111_111
            return round(min(x_m, y_m), 1)
        return round(min(ext.width() / provider.xSize(),
                         ext.height() / provider.ySize()), 1)
    except Exception:
        return None


def render_resolution_m(native_res_m: Optional[float],
                        output_res_m: float) -> float:
    """Pixel size a rendered server is exported at, in metres.

    The FINER of (native resolution, requested output resolution), so the
    export never undersamples the output grid and the converter's
    area-averaging engages on the way down. ONE rule for BOTH tabs — the
    two sides must hand the converter the same pixels or their terrain
    diverges (measured: up to 61 m on NRW WCS slopes when Site Analysis
    exported at the output resolution instead).
    """
    return (min(float(native_res_m), float(output_res_m))
            if native_res_m else float(output_res_m))


def layer_wgs84_bounds(dem_layer: Any) -> Optional[Dict[str, float]]:
    """A loaded layer's own published extent, in WGS84. ``None`` if unknown.

    The one bounds signal a rendered provider (WMS/WMTS/WCS/ArcGIS) has: it
    publishes no files, so its declared extent is the only statement of which
    ground it claims to serve. Used both to warn that a run reaches past the
    service (:func:`_warn_if_bbox_exceeds_bounds`) and to tell "the service
    has nothing here" apart from "the service failed to deliver what it
    publishes" (:func:`rendered_export_gap`).
    """
    if dem_layer is None:
        return None
    try:
        from qgis.core import QgsCoordinateReferenceSystem
        ext = dem_layer.extent()
        if ext is None or ext.isEmpty():
            return None
        return _bbox_transform(
            {"west": ext.xMinimum(), "east": ext.xMaximum(),
             "south": ext.yMinimum(), "north": ext.yMaximum()},
            dem_layer.crs(), QgsCoordinateReferenceSystem("EPSG:4326"))
    except Exception:  # noqa: BLE001 — a bounds probe must never break a run
        return None


def _bbox_overlap(a: Dict[str, float], b: Dict[str, float]
                  ) -> Optional[Dict[str, float]]:
    """The rectangle two WGS84 bboxes share, or ``None`` when they share none."""
    if not bboxes_intersect(a, b):
        return None
    return {"west": max(a["west"], b["west"]),
            "east": min(a["east"], b["east"]),
            "south": max(a["south"], b["south"]),
            "north": min(a["north"], b["north"])}


#: Longest edge, in pixels, of the decimated read used to measure how much of
#: a rendered export came back empty. The question is "is a REGION missing",
#: not "which pixel" — a full read of a 16384x16384 Float32 export would cost
#: a gigabyte to answer it.
_GAP_PROBE_PX = 512


def _nodata_fraction(dest: str, window: Dict[str, float]) -> Optional[float]:
    """Share of *dest*'s samples inside WGS84 *window* that hold no data.

    ``None`` when it cannot be measured (unreadable file, no geotransform, an
    empty window) — an unmeasurable export is never reported as a gap.
    Decimated to :data:`_GAP_PROBE_PX` per edge: a server that dropped part of
    its answer drops blocks, not single pixels.
    """
    import numpy as np
    ds = gdal.Open(dest)
    if ds is None:
        return None
    gt = ds.GetGeoTransform()
    if not gt or gt[1] == 0 or gt[5] == 0:
        return None
    band = ds.GetRasterBand(1)
    if band is None:
        return None
    x0 = int(max(0, math.floor((window["west"] - gt[0]) / gt[1])))
    x1 = int(min(ds.RasterXSize, math.ceil((window["east"] - gt[0]) / gt[1])))
    # gt[5] is negative for a north-up raster, so north maps to the low row.
    y0 = int(max(0, math.floor((window["north"] - gt[3]) / gt[5])))
    y1 = int(min(ds.RasterYSize, math.ceil((window["south"] - gt[3]) / gt[5])))
    if x1 <= x0 or y1 <= y0:
        return None
    arr = band.ReadAsArray(
        x0, y0, x1 - x0, y1 - y0,
        buf_xsize=min(x1 - x0, _GAP_PROBE_PX),
        buf_ysize=min(y1 - y0, _GAP_PROBE_PX))
    if arr is None or arr.size == 0:
        return None
    arr = np.asarray(arr)
    missing = np.isnan(arr) if arr.dtype.kind == "f" else np.zeros(arr.shape, bool)
    nodata = band.GetNoDataValue()
    if nodata is not None:
        missing = missing | (arr == nodata)
    return float(missing.sum()) / float(arr.size)


def rendered_export_gap(dem_layer: Any, dest: str,
                        bbox: Dict[str, float]) -> Optional[float]:
    """How much of a rendered export came back empty INSIDE what it publishes.

    Ground outside the layer's own extent is allowed to be empty — that is
    the no-data contract (Site Analysis reads it as 0 m sea level, the Map
    Converter keeps it VOID), and the run is warned about it by bounds
    already. Empty ground *inside* the extent is a different fact: the
    service failed to deliver terrain it claims to serve, and nothing
    downstream can tell the two apart — the converter sees "no source
    covered this pixel" either way, ``void_fill_m: 0.0`` turns it into real
    0 m ground, and the engine then reports confident coverage over sea.

    Returns the fraction (0..1) of the published part of *bbox* that is
    no-data, or ``None`` when there is nothing published here or the export
    cannot be measured.
    """
    bounds = layer_wgs84_bounds(dem_layer)
    if bounds is None:
        return None
    window = _bbox_overlap(bbox, bounds)
    if window is None:
        return None
    try:
        return _nodata_fraction(dest, window)
    except Exception as exc:  # noqa: BLE001 — a probe must never fail an export
        _log(f"    export gap probe skipped ({exc})")
        return None


def _warn_rendered_export_gap(dem_layer: Any, gap: float,
                              source_key: str = "") -> None:
    """Say, once per run per source, that a render came back partly empty.

    The loud half of the no-data contract for the ONE route that had no way
    to state it: the XYZ path counts 404s (:func:`_warn_no_data_tiles`) and
    the file path checks bounds (:func:`_warn_if_bbox_exceeds_bounds`), but a
    rendered server that answers a request inside its own published extent
    with empty blocks used to reach the user as silent sea level. Measured on
    the NRW WCS (torture row 4.3): the two tabs' terrain disagreed by up to
    402 m where one tab's export came back whole and the other's did not, and
    the only trace anywhere was that cross-tab comparison.

    A warning, not an error: the contract is that missing ground degrades to
    sea LOUDLY, never that it aborts a run.
    """
    if source_key:
        if source_key in _warned_render_gap_sources:
            return
        _warned_render_gap_sources.add(source_key)
    try:
        name = dem_layer.name() or source_key
    except Exception:  # noqa: BLE001
        name = source_key
    _log(
        f"  WARNING: the rendered service '{name}' returned no data for "
        f"{gap:.1%} of the ground it publishes over this area. That is not "
        f"the edge of its coverage — it is a partial answer (a dropped "
        f"block, a capabilities/describe failure, a throttled request). "
        f"Site Analysis reads those pixels as 0 m (sea level) and the Map "
        f"Converter keeps them as no-data, so terrain there is not real; "
        f"re-run the analysis to re-request them."
    )


def _export_via_qgis(dem_layer: Any, dest: str, bbox: Dict[str, float], resolution_m: float) -> None:
    _record_route("render")
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
    if err != QgsRasterFileWriter.WriterError.NoError:
        raise RuntimeError(f"writeRaster failed (code {err})")

    # writeRaster reports success for an answer that arrived with holes in
    # it, so the export has to be looked at. Both tabs render through here,
    # which is why the check lives here and not in either tab.
    gap = rendered_export_gap(dem_layer, dest, bbox)
    if gap:
        try:
            source_key = dem_layer.source()
        except Exception:  # noqa: BLE001
            source_key = ""
        _warn_rendered_export_gap(dem_layer, gap, source_key)


def folder_source_infos(terrain_dir: str,
                        decisions: Optional[Dict[str, str]] = None,
                        crs_overrides: Optional[Dict[str, str]] = None,
                        mode: Optional[str] = None,
                        should_cancel: Any = None,
                        log: Any = None,
                        used: Optional[Dict[str, Any]] = None,
                        ) -> List[Dict[str, Any]]:
    """Usable source infos of a terrain FOLDER, in overlap-priority order,
    straight from its footprint index (``core/terrain_index``) — no per-run
    re-probe: unchanged files cost one stat, new/changed ones are probed in
    parallel. Shared by Site Analysis and the Map Converter.

    *decisions* are the user's pre-flight answers (``{path: "skip"|"use"}``;
    Site Analysis records them with ``terrain_index.approve``), *crs_overrides*
    the CRS the user assigned to CRS-less files (Map Converter). Any problem
    file without an answer is a hard error naming it — never silently
    dropped, never silently given another file's CRS. *log* (optional) also
    receives every line (the Map Converter's own log panel). *used*
    (optional dict) receives what the infos were read under: the index,
    its ``generation`` / ``refreshed_at`` / ``dir_mtime_ns`` after the
    refresh, the ``decisions`` and the ``mode`` (:func:`build_terrain_prep`).
    """
    def say(msg: str) -> None:
        _log(msg)
        if log is not None:
            log(msg)

    index = terrain_index.get_index(terrain_dir)
    stats = index.refresh(max_age_s=terrain_index.FRESH_S,
                          should_cancel=should_cancel)
    if not stats.reused:
        say(f"  folder index: {stats.files} file(s) — {stats.new} new, "
             f"{stats.changed} changed, {stats.removed} removed, "
             f"{stats.unchanged} unchanged ({stats.stat_s:.1f}s check, "
             f"{stats.probe_s:.1f}s read)")
    if not index.entries:
        raise RuntimeError(f"No terrain files in {terrain_dir}")
    if decisions is None:
        decisions = terrain_index.approved_decisions(terrain_dir, index)
    mode = mode or terrain_index.get_overlap_priority(terrain_dir)
    if used is not None:
        used.update(index=index, generation=index.generation,
                    refreshed_at=index.refreshed_at,
                    dir_mtime_ns=index.dir_mtime_ns,
                    decisions=dict(decisions), mode=mode)
    infos, skipped = terrain_index.source_infos(
        terrain_dir, mode, decisions, crs_overrides, index)
    for p, _kind, reason in skipped:
        say(f"  SKIPPED (confirmed by the user): "
             f"{terrain_index.display_path(p, terrain_dir)} — {reason}")
    if not infos:
        raise RuntimeError(
            f"None of the {len(index.entries)} file(s) in the terrain folder "
            f"is usable:\n{terrain_dir}")
    say(f"  local dir: {len(infos)} usable file(s)"
         + (f", {len(skipped)} skipped" if skipped else ""))
    say("  " + terrain_index.describe_order(infos, mode, terrain_dir))
    if mode == terrain_index.PRIORITY_NAME:
        _warn_coarse_wins(infos, terrain_dir, say)
    return infos


def _warn_coarse_wins(infos: List[Dict[str, Any]], folder: str,
                      say: Any = None) -> None:
    """Name order + overlapping files of different resolution = a coarse
    filler may win over a fine dataset. Said once per run, in the log."""
    pairs = terrain_index.mixed_resolution_overlaps(infos)
    if not pairs:
        return
    win, lose = pairs[0]
    (say or _log)(f"  WARNING: overlap priority is file name order, and overlapping "
         f"files differ in resolution — e.g. "
         f"{terrain_index.display_path(win['path'], folder)} "
         f"({win['px_m']:.1f} m) wins over "
         f"{terrain_index.display_path(lose['path'], folder)} "
         f"({lose['px_m']:.1f} m) where they overlap. Choose \"finest "
         f"resolution wins\" for this folder if that is not intended.")


def file_layer_info(path: str, crs_authid: Optional[str] = None
                    ) -> Dict[str, Any]:
    """:func:`source_file_info` for a single-file layer, plus what the index
    probe knows about routing (compression, rotation, scale/offset) and its
    size+mtime fingerprint — so a ZSTD or scaled single file reaches the
    converter the same safe way a folder member does."""
    info = source_file_info(path, crs_authid=crs_authid)
    probe = terrain_index.probe_file(path)
    if probe.get("readable"):
        info["materialize"] = terrain_index.materialize_reason(probe, path)
        info["unscale"] = terrain_index.needs_unscale(probe)
        info["rotated"] = bool(probe.get("rotated") or probe.get("flipped"))
        if probe.get("px_m"):
            info["px_m"] = float(probe["px_m"])
    st = terrain_index._stat(path)
    if st is not None:
        info["fp"] = f"{st[0]}:{st[1]}"
    return info


def _gather_source_infos(dem_layer: Any, terrain_dir: Optional[str],
                         should_cancel: Optional[Callable[[], bool]] = None,
                         ) -> Tuple[List[Dict[str, Any]], bool]:
    """``(source file infos, use_qgis_export)`` for one prepare_terrain run.

    A terrain directory yields its usable files from the folder index
    (:func:`folder_source_infos`), in overlap-priority order. A file-backed
    layer yields one info, carrying the layer's own authid when QGIS knows
    it. A non-file provider (WMS/WMTS/ArcGIS/…) yields no infos and
    ``use_qgis_export=True`` — those are server-rendered per tile via
    :func:`_export_via_qgis`, which is unavoidable.

    *should_cancel* reaches the folder index refresh (a first scan of a big
    folder takes seconds); its stop comes back as :class:`ConverterCancelled`,
    the one cancel exception of the terrain path.
    """
    if terrain_dir and os.path.isdir(terrain_dir):
        try:
            return folder_source_infos(terrain_dir,
                                       should_cancel=should_cancel), False
        except terrain_index.ScanCancelled:
            raise ConverterCancelled() from None

    src = dem_layer.source()
    if os.path.isfile(src) or src.startswith("/vsi"):
        authid = ""
        try:
            crs = dem_layer.crs()
            if crs is not None and crs.isValid():
                authid = crs.authid() or ""
        except Exception:  # noqa: BLE001 — fall back to the file's own CRS
            authid = ""
        return [file_layer_info(src, crs_authid=authid or None)], False

    return [], True


def _read_source_once(dem_layer: Any, terrain_dir: Optional[str],
                      should_cancel: Optional[Callable[[], bool]] = None,
                      ) -> Optional[Tuple[List[Dict[str, Any]], bool,
                                          Optional[FootprintIndex]]]:
    """``(infos, use_qgis_export, FootprintIndex)`` for the sources
    :func:`prepare_terrain` reads before its cache check (a folder, a file
    layer, a rendered provider); None for an XYZ layer, which has no files.
    The ``source_infos`` a multi-band caller hands to every band; None too
    for a layer prepare_terrain refuses as imagery (it says so first)."""
    if not (terrain_dir or (dem_layer is not None
                            and "type=xyz" not in dem_layer.source())):
        return None
    if (not (terrain_dir and os.path.isdir(terrain_dir))
            and dem_layer is not None
            and classify_raster_layer(dem_layer) == "imagery"):
        return None
    infos, qgis_export = _gather_source_infos(dem_layer, terrain_dir,
                                              should_cancel=should_cancel)
    return infos, qgis_export, footprint_index(infos)


class TerrainPrep:
    """ONE preparation of a terrain FOLDER for one Site Analysis run.

    Built once, in the coverage check's worker thread
    (``site_analysis_tab._terrain_dir_checks``, :func:`build_terrain_prep`),
    right after the Run click's index check, and handed to the run's worker:
    the usable source infos in overlap-priority order (``infos``), the
    :class:`FootprintIndex` over them (``index``), the coverage warning
    (``coverage``) and the priced plan (``plan``), plus the signature they
    were read under — the folder's :class:`terrain_index.FolderIndex`
    object, its ``generation``, the time of its last check
    (``checked_at``), the folder's own mtime, the overlap priority and the
    user's answers about problem files.

    :func:`prepare_terrain` / :func:`prepare_terrain_profile` use it instead
    of re-checking the index, rebuilding the source infos, re-sorting them
    and building a new FootprintIndex — while :meth:`stale_reason` is None
    (same index object and generation, younger than
    ``terrain_index.FRESH_S``, folder mtime, overlap priority and answers
    unchanged); otherwise they read the folder as every caller without a
    bundle does (Processing, P2P, the torture runner). Jobs of one run share
    it (same folder)."""

    def __init__(self, folder: str, folder_index: Any, generation: int,
                 checked_at: float, dir_mtime_ns: Optional[int], mode: str,
                 decisions: Dict[str, str], infos: List[Dict[str, Any]],
                 index: Optional[FootprintIndex]) -> None:
        self.folder = folder
        self.folder_index = folder_index
        self.generation = generation
        self.checked_at = checked_at
        self.dir_mtime_ns = dir_mtime_ns
        self.mode = mode
        self.decisions = dict(decisions)
        self.infos = infos
        self.index = index
        #: The coverage warning shown before the run (None = none).
        self.coverage: Optional[str] = None
        #: ``site_analysis_tab._plan_jobs``' priced totals.
        self.plan: Optional[Dict[str, Any]] = None

    def age_s(self) -> float:
        """Seconds since the folder index behind it was checked."""
        return time.time() - self.checked_at

    def stale_reason(self, terrain_dir: Optional[str]) -> Optional[str]:
        """Why this preparation cannot stand in for reading *terrain_dir*
        now, or None when it can."""
        if (not terrain_dir or terrain_index._norm_folder(terrain_dir)
                != terrain_index._norm_folder(self.folder)):
            return "another terrain folder"
        index = terrain_index.get_index(terrain_dir)
        if index is not self.folder_index:
            return "the folder index was reloaded"
        if index.generation != self.generation:
            return "the folder index changed since"
        age = self.age_s()
        if not 0.0 <= age < terrain_index.FRESH_S:
            return (f"checked {age:.0f} s ago — reused for "
                    f"{terrain_index.FRESH_S:.0f} s")
        mtime = terrain_index.dir_mtime_ns(terrain_dir)
        if mtime is None or mtime != self.dir_mtime_ns:
            return "files were added, removed or renamed in the folder"
        if terrain_index.get_overlap_priority(terrain_dir) != self.mode:
            return "the overlap priority changed"
        answers = terrain_index.approved_decisions(terrain_dir, index)
        if answers != self.decisions:
            return "the answers about unusable files changed"
        return None

    def index_for(self, infos: List[Dict[str, Any]]) -> Any:
        """The footprint index for *infos* — ``self.infos`` itself or a
        sub-list of it (:func:`_infos_near`): the bundle's own index, or a
        :class:`SubsetFootprintIndex` over it; None for no files."""
        if not infos:
            return None
        if infos is self.infos:
            return self.index
        return SubsetFootprintIndex(self.index, infos)


def build_terrain_prep(terrain_dir: str,
                       should_cancel: Optional[Callable[[], bool]] = None,
                       ) -> TerrainPrep:
    """:class:`TerrainPrep` for a raster *terrain_dir* — the source infos
    exactly as :func:`folder_source_infos` reads them for a run (its log
    lines included; the index check the Run click just made is reused for
    ``terrain_index.FRESH_S``) and ONE :class:`FootprintIndex` over them.
    Raises as :func:`folder_source_infos` does."""
    used: Dict[str, Any] = {}
    # Under the index's lock: no other refresh between the read and the
    # signature it is recorded with.
    with terrain_index.get_index(terrain_dir).lock:
        infos = folder_source_infos(terrain_dir, should_cancel=should_cancel,
                                    used=used)
    return TerrainPrep(terrain_dir, used["index"], used["generation"],
                       used["refreshed_at"], used["dir_mtime_ns"],
                       used["mode"], used["decisions"], infos,
                       footprint_index(infos))


def _current_prep(prep: Optional[TerrainPrep], terrain_dir: Optional[str]
                  ) -> Optional[TerrainPrep]:
    """*prep* when it still stands for *terrain_dir* (logged), else None —
    logged too when there was one: the caller reads the folder again."""
    if prep is None:
        return None
    why = prep.stale_reason(terrain_dir)
    if why is not None:
        _log(f"  terrain preparation of this run not reused ({why}) — "
             f"reading the terrain folder again")
        return None
    _log(f"  source files: {len(prep.infos)} usable, from this run's terrain "
         f"preparation (folder checked {prep.age_s():.1f} s ago)")
    return prep


def _run_converter(exe: str, job_file: str,
                   should_cancel: Optional[Callable[[], bool]] = None,
                   on_start: Optional[Callable[[Any], None]] = None,
                   burns: Any = None,
                   ) -> List[str]:
    # Stream the converter's output into the log so the .abt creation step is
    # visible. Keep a tail for the error message. Uses the one shared
    # subprocess runner (run_converter_streaming).
    #
    # The lines come back because exit 0 is not the whole verdict: a buildings
    # file it could not read is one `[Warn]` line and a successful exit (see
    # ingest.rs `[Warn] Failed to apply buildings`), so the caller has to read
    # what it said to know whether the run did what was asked.
    #
    # Cancel: the converter prints ingest progress only every 10 tiles, so the
    # per-line *should_cancel* check alone can lag by minutes — *on_start*
    # hands the process to the caller, whose cancel() kills it outright.
    #
    # The per-tile timing lines (`[Rust] tile <name>: …`, 920 in one run) are
    # diagnostics: a converter >= 0.2.8 writes them to AETHER_DIAG_FILE
    # (run_converter_streaming sets it); a 0.2.7 one still prints them, so
    # they are dropped here — neither logged nor kept for the error tail.
    #
    # *burns* (an ``osm_buildings.BurnMonitor``, when the jobs carry
    # ``buildings_pbf_dir``) reads every line; a decode warning stops the
    # converter at the next line (``UndecodableBuildings``, a
    # ConverterCancelled) — before its first tile.
    lines: List[str] = []

    def handle(line: str) -> None:
        if should_cancel is not None and should_cancel():
            raise ConverterCancelled()
        if line.startswith("[Rust] tile "):
            return
        if burns is not None and burns.feed(line):
            from .osm_buildings import UndecodableBuildings
            raise UndecodableBuildings()
        lines.append(line)
        # Kept for the callers, not logged — one line per tile would bury
        # the panel (the Map Converter drops the same two): the ``[Tile]
        # covered=`` count the callers read from the returned lines, and the
        # per-tile burn report the BurnMonitor sums into the run's one
        # "buildings:" line.
        if parse_tile_line(line) is not None:
            return
        if parse_tile_voids_line(line) is not None:
            return          # one per output tile (0.2.11): read, never shown
        if burns is not None and burns.last_kind == "report":
            return
        _log(f"    {line}")

    rc = run_converter_streaming(exe, ["ingest", "--job-file", job_file],
                                 handle, on_start=on_start)
    if rc != 0 and should_cancel is not None and should_cancel():
        # Killed by cancel() before a line reached the check above: the exit
        # code is the kill, not a converter failure.
        raise ConverterCancelled()
    if rc != 0:
        tail = "\n".join(lines[-20:])
        raise RuntimeError(f"converter failed ({rc}):\n{tail}")
    return lines


def _fetch_osm_buildings(todo: List[Dict[str, Any]],
                         verdicts: Dict[str, "PoolTile"],
                         should_cancel: Optional[Callable[[], bool]] = None,
                         cover_needed: Optional[Tuple[Any, list]] = None,
                         ) -> Optional[str]:
    """The strict OpenFreeMap z14 fetch for the pool tiles a run builds
    (``osm_buildings``: ONE cover rule, fetch and view for every route).

    The cover is the WHOLE pool tiles of *todo* (a partial file-source
    build: its regions) — not the run's sector: a tile pooled "with
    buildings" holds every building on it, whichever run built it. No tile
    cap; the count and a size bound (:data:`osm_buildings.TILE_BYTES_ESTIMATE`
    a tile) are logged. Returns the view to hand the converter
    (``buildings_pbf_dir``), or None when none of the tiles has data
    (warned — there are no buildings to burn there). A tile still failing,
    one never fetched, an undecodable answer or an unreadable TileJSON ENDS
    the run (RuntimeError naming the cause); Stop → :class:`ConverterCancelled`.
    Nothing has been built yet, so nothing is pooled without its buildings.

    The sequential path: ingests on aether_converter < 0.2.12 (they decode
    the directory when they start) and downloads on < 0.2.10; a newer
    converter's run fetches next to the conversion
    (:class:`_BuildingsBesideConverter`). *cover_needed*: ``(cover,
    needed)`` already computed (and logged) for this run
    (:func:`_osm_buildings_cover`)."""
    from . import openfreemap
    from . import osm_buildings as osm_b
    cover, needed = cover_needed or _osm_buildings_cover(todo, verdicts)
    if not needed:
        return None
    t0 = time.perf_counter()
    try:
        res = osm_b.ensure_tiles(needed, should_cancel=should_cancel, log=_log,
                                 require_data=False)
    except openfreemap.FetchCancelled:
        raise ConverterCancelled()
    except openfreemap.FetchFailed as exc:
        _log(f"  ERROR: Buildings download failed (OpenFreeMap z14): {exc}")
        raise RuntimeError(_buildings_fetch_failed_text(str(exc))) from exc
    if not _log_buildings_ready(res, time.perf_counter() - t0):
        return None
    cover.present = set(res.present)
    try:
        return osm_b.batch_view(cover.view_names())
    except (RuntimeError, OSError) as exc:
        _log(f"  ERROR: Buildings: {exc}")
        raise RuntimeError(f"Buildings: {exc}") from exc


def _osm_buildings_cover(todo: List[Dict[str, Any]],
                         verdicts: Dict[str, "PoolTile"]) -> Tuple[Any, list]:
    """``(BuildingCover, needed (z, x, y))`` of the pool tiles a run builds —
    the WHOLE tiles of *todo* (a partial file-source build: its regions) —
    with the count line logged (its size bound at
    ``osm_buildings.TILE_BYTES_ESTIMATE`` a tile) when any tile is needed."""
    from . import osm_buildings as osm_b
    extents: List[Dict[str, float]] = []
    for t in todo:
        regions = verdicts[t["filename"]].regions
        extents.extend(regions if regions else [_subtile_bbox(t)])
    cover = osm_b.BuildingCover(extents)
    needed = cover.needed()
    if needed:
        bound_mb = len(needed) * osm_b.TILE_BYTES_ESTIMATE / (1024 * 1024)
        _log(f"  buildings (OpenFreeMap z14): {len(needed):,} tile(s) for the "
             f"{len(todo)} pool tile(s) to build — whole tiles, not only this "
             f"run's area (at most ~{bound_mb:,.0f} MB at "
             f"{osm_b.TILE_BYTES_ESTIMATE // 1024} KB a tile; tiles in the "
             f"cache are reused)")
    return cover, needed


def _buildings_fetch_failed_text(cause: str) -> str:
    """The run's error when Site Analysis's building fetch failed."""
    return (f"Buildings download failed (OpenFreeMap z14): {cause}.\n\nThe run "
            f"was stopped — terrain without the requested buildings is not "
            f"built. The tiles already downloaded stay in the cache; run "
            f"again to fetch the rest, or untick 'Include buildings'.")


def _log_buildings_ready(res: Any, seconds: float) -> bool:
    """Log a finished building fetch (*res*, an ``openfreemap.FetchResult``):
    the counts, and the warning when no tile has data. True when some has."""
    _log(f"  buildings ready: {len(res.present):,} of {res.total:,} tile(s) "
         f"with data ({res.nbytes / 1e6:,.1f} MB; {res.cached:,} from the "
         f"cache, {res.empty:,} without data) in {seconds:.1f} s")
    if not res.present:
        _log(f"  WARNING: buildings: none of the {res.total:,} OpenFreeMap "
             f"tile(s) of these pool tiles has data — there are no buildings "
             f"to burn here; the terrain is built without any")
        return False
    return True


class _BuildingsBesideConverter:
    """Site Analysis's OpenFreeMap z14 fetch NEXT TO the converter run that
    burns the tiles — ONE class for both of its routes, *what*:

    * ``"download"`` — the terrain download of an XYZ source (2026-10-23;
      aether_converter >= 0.2.10, toolkit CONTRACT changelog 45 / §9b
      "building tiles fetched during the download"). The download job burns
      its ``buildings_pbf_dir`` after assembling the terrain, and used to
      read the directory as it found it then — so the whole fetch ran first
      (the owner's log: 27 s of building tiles, then 19 s of terrain, one
      after the other). The fetch starts in :meth:`run`, with the download.
    * ``"ingest"`` — the ingest of a folder / file / rendered source
      (2026-10-25; aether_converter >= 0.2.12, CONTRACT item 48). An ingest
      decoded the directory when it STARTED, so the fetch ran first (the
      owner's folder run: 6 s of 27 s); now it starts its terrain work at
      once and decodes the view when the marker appears, holding finished
      tiles in memory until then (no tile is written before). The fetch is
      started (:meth:`start`) before the ingest jobs are built, so it runs
      beside their build too; the jobs carry :meth:`job_fields`.

    Either way the job carries ``buildings_pbf_ready`` =
    ``osm_buildings.READY_MARKER`` and ``buildings_pbf_wait_s`` =
    ``osm_buildings.READY_WAIT_S`` (a download: every pass) and the
    converter waits for the marker at its burn (its ``[Buildings] waiting
    …`` / ``… ready after S s`` lines are logged like every line). The fetch
    is the shared thread (``osm_buildings.BuildingsFetch``, the Map
    Converter's) over the same cover as the sequential path — the WHOLE pool
    tiles to build (a partial file-source build: its regions) — and the
    same strict verdict; *pbf_dir* — the view — is named before it
    ends, from the tiles NEEDED (``osm_buildings.ready_view``). When every
    tile is in, the view gets exactly the tiles with data (none when there
    is none) and THEN the marker, atomically (``osm_buildings.
    complete_view``).

    LIST mode (*pbf_list*, aether_converter >= 0.2.14, toolkit CONTRACT
    changelog 51; both routes): no view is linked — *pbf_dir* is THE store
    and the jobs carry ``buildings_pbf_list`` = *pbf_list* (named before
    the fetch from the tiles NEEDED, a list an earlier run left removed:
    ``osm_buildings.ready_list``) + ``buildings_pbf_wait_s``; the list IS
    the ready signal: written atomically once every tile is in, naming
    exactly the tiles with data (none when there is none) —
    ``osm_buildings.complete_list`` — and the converter decodes only those,
    straight from the store. Everything below holds for the list as for
    the marker (:attr:`marker` is the file the converter waits for).

    The caller's side of the marker, kept here: a marker an earlier run left
    is removed before the launch (:func:`_buildings_beside` uses the
    sequential path when it cannot be); a fetch that fails — or ends without
    its marker for any reason, Stop included — kills the converter (every
    process goes through :meth:`converter_on_start`, also one launched after
    that; a run whose fetch ended so before its launch launches nothing) and
    never writes the marker. A download that does not end normally leaves
    terrain WITHOUT buildings under the output names (a killed converter
    cannot clean up), so the pass's pool tiles are removed (:meth:`_discard`,
    ``remove_pool_tiles``) — no building-keyed tile without its buildings
    survives; an ingest writes into the caller's scratch directory, which
    the caller removes. A failed fetch ends the run with its cause (the
    strict policy); Stop stays a quiet ``ConverterCancelled``; no fetch
    outlives the run (:meth:`close`)."""

    def __init__(self, cover: Any, needed: List[Tuple[int, int, int]],
                 pbf_dir: str, should_cancel: Optional[Callable[[], bool]],
                 on_start: Optional[Callable[[Any], None]],
                 what: str = "download",
                 pbf_list: Optional[str] = None) -> None:
        from . import osm_buildings as osm_b
        self.cover = cover
        #: The jobs' ``buildings_pbf_dir``: the view, or (list mode) THE
        #: store; :attr:`view` is the view (None in list mode).
        self.pbf_dir = pbf_dir
        self.pbf_list = pbf_list
        self.view = None if pbf_list else pbf_dir
        self.what = what
        #: The file the converter waits for: the list, or the view's marker.
        self.marker = (pbf_list if pbf_list
                       else os.path.join(pbf_dir, osm_b.READY_MARKER))
        #: perf_counter() of the fetch's start and of the marker's write.
        self.t_start: Optional[float] = None
        self.marked: Optional[float] = None
        #: No tile of the cover has data (warned; the marker still comes —
        #: the converter then reports no buildings in extent, 0 tiles).
        self.no_data = False
        self._user_cancel = should_cancel
        self._caller_on_start = on_start
        self._abandon = threading.Event()
        self._lock = threading.Lock()
        self._procs: List[Any] = []
        self._dead = False          # the fetch ended without its marker
        self.fetch = osm_b.BuildingsFetch(
            [needed], should_cancel=self._fetch_cancelled, log=_log,
            cover=cover, require_data=False, on_fetched=self._fetched,
            on_failed=self._failed, on_end=self._ended,
            failure_text=_buildings_fetch_failed_text)

    # -- what the converter run is handed -------------------------------------
    def job_fields(self) -> Dict[str, Any]:
        """The marker fields of every INGEST job carrying
        ``buildings_pbf_dir`` — ``buildings_pbf_ready`` or, in list mode,
        ``buildings_pbf_list``, never both — + ``buildings_pbf_wait_s`` (a
        download job gets them through ``_try_rust_download``:
        :meth:`launch_fields`)."""
        from . import osm_buildings as osm_b
        if self.pbf_list:
            return {"buildings_pbf_list": os.path.abspath(self.pbf_list),
                    "buildings_pbf_wait_s": osm_b.READY_WAIT_S}
        return {"buildings_pbf_ready": osm_b.READY_MARKER,
                "buildings_pbf_wait_s": osm_b.READY_WAIT_S}

    def launch_fields(self) -> Dict[str, Any]:
        """The same as ``_try_rust_download`` arguments: ``pbf_ready=`` or
        ``pbf_list=``, and ``pbf_wait_s=``."""
        from . import osm_buildings as osm_b
        if self.pbf_list:
            return {"pbf_list": self.pbf_list,
                    "pbf_wait_s": osm_b.READY_WAIT_S}
        return {"pbf_ready": osm_b.READY_MARKER,
                "pbf_wait_s": osm_b.READY_WAIT_S}

    def _stopped(self) -> bool:
        return self._user_cancel is not None and bool(self._user_cancel())

    def _fetch_cancelled(self) -> bool:
        return self._abandon.is_set() or self._stopped()

    def converter_cancel(self) -> bool:
        """The converter run's ``should_cancel``: Stop, or the fetch
        failed."""
        return self._stopped() or self.fetch.error is not None

    def converter_on_start(self, proc: Any) -> None:
        """The converter run's ``on_start``: the caller's hook (Stop kills
        it), and the process kept so the fetch can kill it — at once when
        the fetch already ended without its marker."""
        with self._lock:
            self._procs.append(proc)
            dead = self._dead
        if self._caller_on_start is not None:
            self._caller_on_start(proc)
        if dead:
            kill_engine_process(proc)

    def _kill_converter(self) -> None:
        with self._lock:
            self._dead = True
            procs = list(self._procs)
        for proc in procs:
            with contextlib.suppress(Exception):  # best effort, as for Stop
                kill_engine_process(proc)

    # -- the fetch thread's hooks --------------------------------------------
    def _fetched(self, res: Any) -> None:
        from . import osm_buildings as osm_b
        self.no_data = not _log_buildings_ready(res, res.seconds)
        names = self.cover.view_names()
        try:
            if self.pbf_list:
                # The list IS the signal: written atomically, the tiles with
                # data named — the converter reads them from the store.
                osm_b.complete_list(self.pbf_list, names)
            else:
                # The marker LAST: the converter reads the directory once,
                # right after it sees the marker.
                osm_b.complete_view(self.view, names)
        except (RuntimeError, OSError) as exc:
            raise RuntimeError(f"Buildings: {exc}") from exc
        self.marked = time.perf_counter()
        burner = "download" if self.what == "download" else "conversion"
        if self.pbf_list:
            _log(f"  buildings: {len(names):,} OpenFreeMap tile(s) listed "
                 f"for the converter in {self.pbf_list} — the {burner} "
                 f"burns them now")
        else:
            _log(f"  buildings: {len(names):,} OpenFreeMap tile(s) linked "
                 f"into {self.view}, {osm_b.READY_MARKER} written — the "
                 f"{burner} burns them now")

    def _failed(self, cause: str) -> None:
        if self.fetch.error == cause:      # the view or the marker
            _log(f"  ERROR: {cause}")
        else:
            _log(f"  ERROR: Buildings download failed (OpenFreeMap z14): "
                 f"{cause}")
        self._kill_converter()

    def _ended(self) -> None:
        if self.marked is None:
            # Failed or stopped: the converter would wait for nothing (and a
            # caller without an on_start hook could not kill it).
            self._kill_converter()

    # -- the run ---------------------------------------------------------------
    def start(self) -> None:
        """Start the fetch — once — and say so: a download's :meth:`run`
        does, with the download; an ingest calls it before its jobs are
        built."""
        if self.t_start is not None:
            return
        # The gate of this mode (the list: 0.2.14 on both routes).
        since = ("0.2.14" if self.pbf_list
                 else "0.2.10" if self.what == "download" else "0.2.12")
        if self.what == "download":
            _log(f"  buildings: fetched next to the terrain download "
                 f"(aether_converter >= {since} waits for {self.marker})")
        else:
            _log(f"  buildings: fetched next to the conversion "
                 f"(aether_converter >= {since} waits for {self.marker})")
        self.t_start = time.perf_counter()
        self.fetch.start()

    def close(self) -> None:
        """No fetch outlives the converter run: one still running is not
        needed any more — stopped and joined (never started: nothing)."""
        if self.t_start is None:
            return
        if self.fetch.alive():
            self._abandon.set()
        self.fetch.join()

    def _discard(self, pool_dir: Optional[str],
                 names: Optional[List[str]]) -> None:
        if pool_dir is None or not names:
            return          # an ingest: its scratch directory is the caller's
        removal = remove_pool_tiles(pool_dir, names)
        left = [n for n in names if os.path.exists(os.path.join(pool_dir, n))]
        if left:
            _set_rebuild_flags(pool_dir, left, set(left))
        if removal.removed or left:
            _log(f"  buildings: the download did not end with its buildings "
                 f"burnt — {removal.removed} pool tile(s) it wrote removed"
                 + (f", {len(left)} that could not be removed flagged for "
                    f"rebuild" if left else "")
                 + " (terrain without the requested buildings is never "
                   "pooled)")

    def run(self, launch: Callable[..., Any],
            pool_dir: Optional[str] = None,
            names: Optional[List[str]] = None, burns: Any = None) -> Any:
        """Start the fetch (unless :meth:`start` did) and run
        *launch(should_cancel, on_start, pbf_ready=… or pbf_list=…,
        pbf_wait_s=…)* (:meth:`launch_fields`) next to
        it at once — ``_try_rust_download``, or the ingest's
        ``_run_converter`` (its jobs carry :meth:`job_fields`); returns what
        it returns. A fetch that already ended without its marker (failed,
        stopped — an ingest's jobs were being built) launches nothing. When
        it raises (Stop, the fetch's failure, any error) a download's pool
        tiles *names* (in *pool_dir*) are removed — but for the burn's own
        verdict on undecodable tiles, which the caller flags. A failed
        fetch raises its error (RuntimeError)."""
        from . import osm_buildings as osm_b
        self.start()
        t_launch = time.perf_counter()
        try:
            with self._lock:
                dead = self._dead
            if dead:
                raise ConverterCancelled()
            outcome = launch(self.converter_cancel, self.converter_on_start,
                             **self.launch_fields())
        except osm_b.UndecodableBuildings:
            self.close()
            if (burns is not None and burns.bad_tiles and not self._stopped()
                    and self.fetch.error is None):
                # The burn's own verdict, read after the marker: the caller
                # flags the pass's tiles and drops the undecodable ones.
                raise
            self._discard(pool_dir, names)
            raise
        except BaseException as exc:
            self.close()
            self._discard(pool_dir, names)
            if (isinstance(exc, ConverterCancelled) and not self._stopped()
                    and self.fetch.error is not None):
                raise RuntimeError(self.fetch.error) from None
            raise
        converter_s = time.perf_counter() - t_launch
        self.close()
        if self.marked is not None and (outcome or self.what != "download"):
            fetch_s = self.marked - self.t_start
            waited = burns.waited_s if burns is not None else 0.0
            if self.what == "download":
                _log(f"  buildings fetched next to the terrain download: "
                     f"fetch {fetch_s:.1f} s, download {converter_s:.1f} s, "
                     f"converter waited {waited:.1f} s")
            else:
                _log(f"  buildings fetched next to the conversion: fetch "
                     f"{fetch_s:.1f} s, converter {converter_s:.1f} s, "
                     f"converter waited {waited:.1f} s")
        return outcome


def _buildings_beside(todo: List[Dict[str, Any]],
                      verdicts: Dict[str, "PoolTile"],
                      binary_manager: Any,
                      should_cancel: Optional[Callable[[], bool]],
                      on_start: Optional[Callable[[Any], None]],
                      what: str = "download",
                      ) -> Tuple[Optional[_BuildingsBesideConverter],
                                 Optional[Tuple[Any, list]]]:
    """``(the fetch next to the converter run, (cover, needed))`` for a Site
    Analysis run with buildings over route *what* — ``"download"`` (an XYZ
    source without a buildings FILE) or ``"ingest"`` (every other source) —
    the fetch None for the sequential path (:func:`_fetch_osm_buildings`,
    handed the cover): a converter older than
    ``binary_manager.CONVERTER_BUILDINGS_READY_MIN_VERSION`` (0.2.10; an
    ingest: ``CONVERTER_INGEST_BUILDINGS_READY_MIN_VERSION``, 0.2.12) or
    unknown (the cached ``--version`` probe) ignores ``buildings_pbf_ready``
    and would read the directory half-filled; no tile needed; or a marker
    an earlier run left that cannot be removed (``osm_buildings.
    ready_view``). The cover is None when the converter decided.

    On aether_converter >= 0.2.14
    (``binary_manager.converter_supports_buildings_list``, toolkit CONTRACT
    changelog 51) the fetch is in LIST mode: no view, the jobs read THE
    store through a list file named now (``osm_buildings.ready_list``); a
    list an earlier run left that cannot be removed → the view and its
    marker, as on 0.2.12."""
    from . import binary_manager as bm
    from . import openfreemap
    from . import osm_buildings as osm_b
    supports = (bm.converter_supports_buildings_ready if what == "download"
                else bm.converter_supports_ingest_buildings_ready)
    try:
        exe = binary_manager.find_binary("aether_converter")
        ready = supports(exe)
        listing = ready and bm.converter_supports_buildings_list(exe)
    except Exception:  # noqa: BLE001 — a probe never takes the run down
        ready = listing = False
    if not ready:
        return None, None
    cover, needed = _osm_buildings_cover(todo, verdicts)
    if not needed:
        return None, (cover, needed)
    names = [openfreemap.tile_filename(*t) for t in needed]
    if listing:
        pbf_list = osm_b.ready_list(names)
        if pbf_list is not None:
            return (_BuildingsBesideConverter(
                cover, needed, osm_buildings_store_dir(), should_cancel,
                on_start, what, pbf_list=pbf_list), (cover, needed))
        _log(f"  WARNING: buildings: {osm_buildings_list_path(names)} (left "
             f"by an earlier run) could not be removed — the building tiles "
             f"are linked into a view for the "
             + ("download" if what == "download" else "conversion")
             + " instead")
    view = osm_b.ready_view(names)
    if view is None:
        stale = os.path.join(
            osm_buildings_view_dir(names, marker=osm_b.READY_MARKER),
            osm_b.READY_MARKER)
        _log(f"  WARNING: buildings: {stale} (left by an earlier run) could "
             f"not be removed — the building tiles are fetched before the "
             + ("download" if what == "download" else "conversion")
             + " instead")
        return None, (cover, needed)
    return (_BuildingsBesideConverter(cover, needed, view, should_cancel,
                                      on_start, what), (cover, needed))


def _buildings_burn_failed(burns: Any, pool: bool = False) -> str:
    """The run's error when the converter's buildings report (*burns*, an
    ``osm_buildings.BurnMonitor``) is not clean — logged; building tiles it
    could not decode leave the store and every view first. *pool*: the
    pass wrote into the pool (its tiles were flagged for rebuild)."""
    from . import osm_buildings as osm_b
    if burns.bad_tiles:
        msg = osm_b.drop_undecodable(burns.bad_tiles)
    else:
        msg = burns.verdict() or "Buildings: the burn could not be verified."
    _log(f"  ERROR: {msg}")
    return (msg + "\n\nThe run was stopped — terrain without the requested "
            "buildings is not used. "
            + ("The terrain tiles this run wrote are flagged for rebuild."
               if pool else "Nothing was added to the terrain cache."))


def _log_buildings_burn(burns: Any) -> None:
    """The burn's log line: the totals, or — every report "no buildings in
    extent" — the warning that the area holds none (no error: the tiles
    were fetched and read, there is simply nothing to burn)."""
    if burns.empty:
        _log(f"  WARNING: buildings: the {burns.pbf_tiles:,} OpenFreeMap "
             f"tile(s) of these pool tiles hold no building (\"no buildings "
             f"in extent\") — the terrain carries none here")
    else:
        _log(f"  buildings: {burns.summary()}")


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
    progress_span: Optional[Tuple[float, float]] = None,
    min_range_km: float = 0.0,
    skip_footprints: Optional[set] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    on_start: Optional[Callable[[Any], None]] = None,
    source_infos: Optional[Tuple[List[Dict[str, Any]], bool,
                                 Optional[FootprintIndex]]] = None,
    output_res_m: Optional[float] = None,
    region_union: Optional[Dict[str, List[Dict[str, float]]]] = None,
    terrain_prep: Optional[TerrainPrep] = None,
) -> str:
    """Produce .abt terrain tiles for aether_core. Returns cache directory.

    File sources (a folder, a file layer) build only the part of each tile
    the job needs (:func:`tile_regions`; a partial tile is pooled as
    ``<name>.part``, see :func:`resolve_pool_tile`) — the view links it under
    the canonical name. *output_res_m* is the job's output pixel when it is
    coarser than *resolution_m* (a range-profile band), for the region
    margin. *region_union* (:func:`run_region_union`) is every region the
    run's jobs need per tile: a tile this job builds is built for all of
    them at once, so a multi-site run builds each tile once.

    *source_infos* ``(infos, use_qgis_export, index)`` is the source already
    read by :func:`_read_source_once` — :func:`prepare_terrain_profile` reads
    and indexes it once for all bands; None (every other caller) reads it
    here. *terrain_prep* (Site Analysis: the run's :class:`TerrainPrep` of
    *terrain_dir*) stands in for that read while it is current — no index
    re-check, source-info build, overlap sort or FootprintIndex build; the
    files near the job's tiles are matched through its index
    (:class:`SubsetFootprintIndex`), so the jobs are the ones the read
    gives. A stale or absent one: the folder is read here, as before.

    *should_cancel()* is polled per tile (fingerprints, job building), per
    folder-index file and per engine output line; *on_start(proc)* receives
    every engine process this starts (download passes, ingest) so the
    caller's cancel can kill it — the converter prints ingest progress only
    every 10 tiles, so polling lines alone cannot stop it promptly. A stop is
    always :class:`ConverterCancelled`; scratch files are removed on the way
    out. Both default to None (not cancellable), as for every older caller.

    *min_range_km* and *skip_footprints* narrow the tile set for one band of a
    range profile (:func:`prepare_terrain_profile`): tiles lying wholly inside
    the *min_range_km* disc, and tiles whose footprint (:func:`tile_footprint`)
    is in *skip_footprints*, are left out — a finer band already covers them, so
    they are never downloaded. Both default to "keep everything".

    *progress_span* ``(lo, hi)``, when given together with a *feedback* that
    has ``setProgress``, maps the terrain-download fraction into that
    sub-range of the caller's progress bar (the caller knows its own phase
    layout; the adapter must not claim 0-100).

    Buildings can come from *buildings_file* (FlatGeobuf, absolute roof
    elevations) and/or *osm_buildings* (OpenFreeMap vector tiles, heights above
    ground). Either way they are burned into the terrain surface, so the choice
    is part of the cache identity. OpenFreeMap goes the one strict way
    (``osm_buildings``, as the Map Converter's row): the z14 tiles of the
    WHOLE pool tiles to build are fetched — next to the download on
    aether_converter >= 0.2.10 and next to the ingest on >= 0.2.12
    (:class:`_BuildingsBesideConverter`: the jobs wait for the view's marker
    file at their burn), else before the download/ingest
    (:func:`_fetch_osm_buildings`); a failure ends the run (a converter it
    ran beside killed, a download's pool tiles removed) — the download
    job or the ingest jobs carry them as ``buildings_pbf_dir``, and the
    converter's lines decide (``BurnMonitor``: an undecodable tile, a pass
    without a burn report, buildings decoded and none drawn → RuntimeError,
    pool tiles a download pass wrote flagged for rebuild; "no buildings in
    extent" everywhere or no tile with data → a warning).
    """
    t0 = time.perf_counter()
    source = terrain_dir or dem_layer.source()
    _log(f"prepare_terrain: resolution={resolution_m}m, range={max_range_km}km, "
         f"az={az_start:.1f}-{az_end:.1f}"
         + (f", buildings={os.path.basename(buildings_file)}"
            if buildings_file else "")
         + (", buildings=openfreemap" if osm_buildings else ""))

    # A layer that classifies as imagery must never become terrain: a
    # rendered map service or an RGB basemap hands back a PICTURE, and
    # ingesting one reads colour values as metres — a confident analysis
    # over shaded relief. The GUI's "Not a DEM? Use it anyway?" prompt is
    # advisory; this is where the line is drawn, and it holds for both
    # tabs and both Processing algorithms (which never see a prompt).
    # Mirrors _gather_source_infos: the layer is only the source when no
    # usable terrain directory was given.
    if (not (terrain_dir and os.path.isdir(terrain_dir))
            and dem_layer is not None
            and classify_raster_layer(dem_layer) == "imagery"):
        try:
            lname = dem_layer.name() or source
        except Exception:  # noqa: BLE001 — refusal must not depend on name()
            lname = source
        raise RuntimeError(
            f"Layer '{lname}' classifies as imagery, not elevation — it "
            "cannot be used as terrain. A map service (WMS / WMTS / "
            "ArcGIS) or RGB basemap returns a rendered picture; reading "
            "its pixels as metres would run a confident analysis over "
            "colours. Use an elevation source: a DEM file or folder, a "
            "WCS coverage, or an elevation-encoded tile service "
            "(Terrarium / Terrain-RGB)."
        )

    # A directory of .abt tiles is already an engine input — hand it over
    # untouched. Everything below this point exists to *produce* .abt tiles, so
    # running any of it would rebuild what the Map Converter already built (and
    # the converter would reject a .abt as an ingest source anyway).
    if is_abt_tile_dir(terrain_dir):
        tiles = list_abt_tiles(terrain_dir)
        _log(f"  using {len(tiles)} pre-built .abt tile(s) from {terrain_dir} "
             f"— no download or conversion")
        abt_bands = [(max_range_km * 1000.0, int(resolution_m))]
        # Re-run the same checks the GUI shows as a dialog. A Processing run
        # never sees that dialog, and this path skips every other safeguard in
        # this function, so anything wrong with the tiles has to be said here
        # or the run produces a confident, wrong answer with no trace. The
        # selection is memoised: _abt_view_for below reuses it.
        selection = select_abt_tiles(terrain_dir, tx_lat, tx_lon, abt_bands,
                                     az_start, az_end)
        problem = _abt_coverage_warning(
            terrain_dir, tiles,
            _compute_sector_bbox(tx_lat, tx_lon, max_range_km, az_start, az_end),
            resolution_m, selections=[selection],
        )
        if problem:
            for line in problem.splitlines():
                if line.strip():
                    _log(f"  WARNING: {line.strip()}")
        # Buildings are baked into a prepared tile when it is built: what
        # the Map Converter recorded for the chosen tiles is said here (the
        # GUI asks before the run).
        _log_prepared_buildings(terrain_dir, selection,
                                bool(osm_buildings or buildings_file))
        _record_route("terrain_dir")
        return _abt_view_for(terrain_dir, tx_lat, tx_lon, abt_bands,
                             az_start, az_end)

    bbox = _compute_sector_bbox(tx_lat, tx_lon, max_range_km, az_start, az_end)
    subtiles = _compute_subtiles(bbox, resolution_m, tx_lat, tx_lon,
                                 max_range_km, az_start, az_end)
    subtiles = _band_tiles(subtiles, tx_lat, tx_lon, min_range_km, skip_footprints)
    expected = [t["filename"] for t in subtiles]

    # The converter reads FlatGeobuf in WGS84 and nothing else, so a GeoJSON, a
    # shapefile in EPSG:2056 or a "…gpkg|layername=buildings" URI has to be
    # converted first — otherwise it warns once, exits 0 and writes terrain
    # with no buildings on it. The Map Converter tab has always done this in
    # its worker; every other caller of prepare_terrain (both GUI tabs, both
    # Processing algorithms) passed the raw path straight through. One shared
    # implementation now: core.buildings_source.
    #
    # Before the identity below, so the identity fingerprints the file that is
    # actually burned in rather than the one that was asked for.
    # Deferred: buildings_source imports this module for source_fingerprint.
    from . import buildings_source
    if buildings_file:
        resolved_buildings = buildings_source.resolve_buildings_source(
            buildings_file, log=_log)
        if resolved_buildings != buildings_file:
            _log(f"  buildings: {buildings_file} → {resolved_buildings}")
        buildings_file = resolved_buildings

    # One identity covering both sources, so switching either one — or turning
    # buildings off — lands on a different cache entry. Shared with the pre-run
    # estimate, so the GUI prices the pool this run will really use.
    buildings_id = buildings_identity(buildings_file, osm_buildings)

    # The pool holds tiles for this source; the view is what the engine reads.
    # Both identities are computed ONCE here and reused for the rest of the
    # run — never recomputed at the launch site, where a changed cache-dir
    # setting would move them out from under the directories already made.
    pool_dir = _pool_dir(source, buildings_id)

    # File sources (a folder or a file layer): read the folder index BEFORE
    # deciding what is cached. The pool is keyed on the folder path, so each
    # pooled tile also records the fingerprint of exactly the files that fed
    # it (path + size + mtime, priority order); a file added, changed or
    # removed makes the tiles it touches stale — and no others. Without this
    # a folder whose files changed served the old terrain as a "cache HIT".
    #
    # Tile -> file matching goes through ONE FootprintIndex per run, so each
    # tile's files are found once (not by testing every file) and the job
    # build below reuses the match the fingerprint made.
    early_infos: Optional[List[Dict[str, Any]]] = None
    early_qgis_export = False
    src_index: Optional[FootprintIndex] = None
    if source_infos is not None:
        # prepare_terrain_profile read the source (and indexed it) once for
        # all of its bands.
        early_infos, early_qgis_export, src_index = source_infos
    elif terrain_dir or (dem_layer is not None
                         and "type=xyz" not in dem_layer.source()):
        prep = _current_prep(terrain_prep, terrain_dir)
        if prep is not None:
            # The run's preparation (one index check, one source-info build,
            # one FootprintIndex per Run click): the same files near the
            # job's tiles, matched through its index.
            early_infos = _infos_near(prep.infos, subtiles)
            src_index = prep.index_for(early_infos)
        else:
            early_infos, early_qgis_export = _gather_source_infos(
                dem_layer, terrain_dir, should_cancel=should_cancel)
            if early_infos:
                early_infos = _infos_near(early_infos, subtiles)
                src_index = footprint_index(early_infos)
    # A file source builds only the part of each tile this job needs
    # (resolve_pool_tile); XYZ and rendered sources always whole tiles. The
    # view identity carries the regions, so a view is never handed a partial
    # tile built for another region.
    partial_ok = bool(early_infos)
    regions = tile_regions(subtiles, bbox, resolution_m, output_res_m,
                           partial_ok)
    link_tiles = ({t["filename"]: (t, regions[t["filename"]])
                   for t in subtiles} if partial_ok else None)
    view_dir = _view_dir(source, subtiles, resolution_m, buildings_id,
                         regions)
    ensure_engine_dirs(pool_dir, view_dir)
    # Log the resolved paths so the user can inspect the .abt tiles directly
    # (e.g. to check for missing tiles that show up as empty stripes through a
    # line-of-sight). Set QgsSettings "waveshed/cache_dir" to relocate.
    _log(f"  tile pool: {pool_dir}")
    _log(f"  .abt view dir: {view_dir}")

    # One fingerprint per (tile, region), kept for the run: the stale check
    # and the sidecars written after the build share them.
    fp_memo: Dict[Tuple[Any, ...], str] = {}

    def fp_for(t: Dict[str, Any], box: Dict[str, float]) -> str:
        key = (t["filename"], box["west"], box["south"], box["east"],
               box["north"])
        if key not in fp_memo:
            fp_memo[key] = tile_source_fingerprint(
                early_infos, box, t["exact_res_m"], index=src_index)
        return fp_memo[key]

    if early_infos:
        for t in subtiles:
            if should_cancel is not None and should_cancel():
                raise ConverterCancelled()
            fp_for(t, regions[t["filename"]] or _subtile_bbox(t))

    # Membership is per tile, so a smaller range, a nudged site or a narrower
    # sector fetches nothing: every tile it needs is already pooled.
    #
    # ONE listing of the pool decides which tiles are there (no header read
    # per tile; _tile_ready's size rule), which carry a fingerprint and
    # which regions a partial tile holds — THE hit rule, resolve_pool_tile.
    listing = _pool_listing(pool_dir)
    verdicts: Dict[str, PoolTile] = {}
    for t in subtiles:
        if should_cancel is not None and should_cancel():
            raise ConverterCancelled()
        name = t["filename"]
        verdicts[name] = resolve_pool_tile(
            pool_dir, t, regions[name],
            fp_of=((lambda box, t=t: fp_for(t, box)) if early_infos
                   else None),
            listing=listing, extra_regions=(region_union or {}).get(name))
    stale = [t for t in subtiles if not verdicts[t["filename"]].hit
             and verdicts[t["filename"]].reason == "stale"]
    if stale:
        _log(f"  {len(stale)} pooled tile(s) are stale — the files that "
             f"feed them changed (or were added/removed) since they were "
             f"built; rebuilding just those (first: {stale[0]['filename']})")
    todo = [t for t in subtiles if not verdicts[t["filename"]].hit]
    if not todo:
        # Every tile is pooled from an earlier run: the void counts that
        # run recorded say which ones need the 0 m fill.
        gone = _sync_view(pool_dir, view_dir, expected, tiles=link_tiles,
                          voids=_view_voids(pool_dir, expected, link_tiles))
        if not gone:
            n_part = sum(1 for v in verdicts.values() if v.partial)
            _log(f"  cache HIT — {len(expected)} tile(s) from the pool"
                 + (f" ({n_part} partial)" if n_part else "")
                 + f" ({time.perf_counter() - t0:.1f}s)")
            # A hit re-serves what the source's contract route pooled
            # earlier (nothing else can fill a pool any more), so it counts
            # as that route — a warm run must not read as "acquired nothing".
            _record_route("sources" if terrain_dir
                          else _source_route(source, dem_layer))
            _record_view_dir(view_dir)
            return view_dir
        _log(f"  {len(gone)} pooled tile(s) vanished between check and link; "
             f"rebuilding those")
        listing = _pool_listing(pool_dir)
        for name in gone:
            spec = next(t for t in subtiles if t["filename"] == name)
            v = resolve_pool_tile(
                pool_dir, spec, regions[name],
                fp_of=((lambda box, t=spec: fp_for(t, box)) if early_infos
                       else None),
                listing=listing, extra_regions=(region_union or {}).get(name))
            if v.hit:               # raced back in: rebuild what the job needs
                r = regions[name]
                v = PoolTile(False, None, r is not None,
                             [r] if r is not None else None, "missing")
            verdicts[name] = v
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
    # A partial build (resolve_pool_tile) reads only its region's files —
    # say how many there are and how much ground they cover.
    part_todo = [t for t in todo if verdicts[t["filename"]].regions]
    part_note = ""
    if part_todo:
        reg_deg2 = sum(_bbox_area(r) for t in part_todo
                       for r in verdicts[t["filename"]].regions)
        tile_deg2 = sum(_bbox_area(_subtile_bbox(t)) for t in part_todo)
        part_note = (f" ({len(part_todo)} partial: region {reg_deg2:.3f} deg² "
                     f"of their {tile_deg2:.3f} deg²)")
    _log(f"  cache MISS — {len(todo)} of {len(subtiles)} output tiles (.abt) "
         f"need building{part_note}, ~{disk_mb} MB on disk, "
         f"bbox N={bbox['north']:.3f} S={bbox['south']:.3f} "
         f"E={bbox['east']:.3f} W={bbox['west']:.3f}")

    # Buildings are fetched strictly: every tile is in the store or
    # answered "no data", or the run ENDS with the cause — a pool keyed
    # "with buildings" is never built without them. A DOWNLOAD on
    # aether_converter >= 0.2.10 fetches them next to the terrain (the job
    # waits for the view's marker file at its buildings post-pass) and an
    # INGEST (file / folder / rendered sources, a buildings FILE beside
    # OpenFreeMap) on >= 0.2.12 next to the conversion (it starts at once
    # and decodes the view when the marker appears): _BuildingsBesideConverter.
    # On >= 0.2.14 both read THE store through a list file instead of a
    # view (buildings_pbf_list: nothing linked, the list is the signal).
    # Older converters fetch them first — a download reads the directory as
    # it finds it when its terrain is done, an older ingest decodes it when
    # it STARTS.
    #
    # `buildings_missing` is the reconciliation for a buildings FILE: an
    # unusable one (here) or the converter's own complaint after the ingest
    # flags the tiles for rebuild, so a pool keyed "with buildings" never
    # serves a cache hit over building-free terrain.
    pbf_dir: Optional[str] = None
    burns: Any = None
    beside: Optional[_BuildingsBesideConverter] = None
    buildings_missing = False
    if buildings_file and not buildings_source.is_converter_readable(
            buildings_file):
        _log(f"  WARNING: the converter cannot read {buildings_file} as "
             f"buildings — it reads FlatGeobuf only, and could not be handed "
             f"a converted copy of this source. It will warn and write "
             f"terrain without buildings.")
        buildings_missing = True
    if osm_buildings:
        from . import osm_buildings as osm_b
        # The route below: the download (an XYZ source without a buildings
        # FILE — that diverts to the ingest path), else the ingest.
        route = ("download" if (not buildings_file and not terrain_dir
                                and "type=xyz" in dem_layer.source())
                 else "ingest")
        beside, cover_needed = _buildings_beside(
            todo, verdicts, binary_manager, should_cancel, on_start, route)
        if beside is not None:
            pbf_dir = beside.pbf_dir
        else:
            pbf_dir = _fetch_osm_buildings(todo, verdicts, should_cancel,
                                           cover_needed=cover_needed)
        if pbf_dir:
            # Every pass that carries the field is read (the converter's
            # own lines): a tile it cannot decode, a pass that reports no
            # burn, buildings decoded and none drawn — each ends the run.
            burns = osm_b.BurnMonitor()

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
        # Trivial progress wiring only (A3): map the download fraction onto
        # the SUB-SPAN of the processing feedback the caller reserved for
        # terrain (progress_span). Mapping onto 0-100 here made the bar hit
        # 100 % mid-download and snap back when the caller resumed.
        progress_cb = None
        if (progress_span is not None and feedback is not None
                and hasattr(feedback, "setProgress")):
            span_lo, span_hi = progress_span

            def progress_cb(frac: float, _label: str) -> None:
                with contextlib.suppress(Exception):  # progress must never abort
                    feedback.setProgress(
                        int(span_lo + frac * (span_hi - span_lo)))
        built_names = [t["filename"] for t in todo]

        def download(cancel: Any, start: Any, **ready: Any) -> Any:
            return _try_rust_download(
                dem_layer.source(), pool_dir, bbox, resolution_m, todo,
                binary_manager, pbf_dir=pbf_dir, progress_cb=progress_cb,
                should_cancel=cancel, on_start=start,
                **({"burns": burns} if burns is not None else {}), **ready)
        try:
            if beside is not None:
                # The building fetch runs NOW, next to this download, which
                # waits for the view's marker before its burn; a failed
                # fetch raises its own error (the pass's tiles removed).
                outcome = beside.run(download, pool_dir, built_names, burns)
            else:
                outcome = download(should_cancel, on_start)
        except ConverterCancelled:
            if (burns is None or not burns.bad_tiles
                    or (should_cancel is not None and should_cancel())):
                raise
            # A pass warned it could not decode building tiles (raised after
            # its lines were read): the pool tiles it wrote carry only some
            # of their buildings — flagged, never served as built.
            _set_rebuild_flags(pool_dir, built_names, set(built_names))
            raise RuntimeError(_buildings_burn_failed(burns, pool=True))
        if outcome and burns is not None:
            if burns.verdict() is not None:
                _set_rebuild_flags(pool_dir, built_names, set(built_names))
                raise RuntimeError(_buildings_burn_failed(burns, pool=True))
            if beside is None or not beside.no_data:
                # (No tile with data: the fetch warned already, and the
                # burn of the empty view says "0 pbf tile(s)".)
                _log_buildings_burn(burns)
        if outcome:
            # A buildings FILE that turned out unusable: the pool identity
            # claims buildings these tiles do not carry — never reused.
            if buildings_missing:
                _set_rebuild_flags(pool_dir, [t["filename"] for t in todo],
                                   {t["filename"] for t in todo})
                _log("  buildings were requested but unavailable — tiles "
                     "flagged for rebuild so the next run retries them")
            # This download's counts first, the pool's records for the
            # tiles an earlier run pooled.
            _finish_view(pool_dir, view_dir, expected, t0,
                         voids=_view_voids(
                             pool_dir, expected,
                             run=getattr(outcome, "voids", None)))
            _record_route("download")
            _record_view_dir(view_dir)
            return view_dir
        # NO render fallback for an XYZ elevation service: the router
        # contract says XYZ goes through the shared Rust downloader, and
        # rendering the tiles through QGIS instead is the slow aliasing
        # path the downloader replaced — terrain would appear, silently
        # built the wrong way. Fail with the engine's own diagnosis.
        raise RuntimeError(
            "Terrain download failed for this XYZ elevation service, and "
            "rendering its tiles through QGIS is not a fallback. "
            + ((outcome.detail or "").strip()
               or "The engine gave no further detail — check that the "
                  "Aether binaries are installed (Waveshed Settings)."))

    # Path 2/3: hand the converter the source rasters themselves, per tile.
    # Each job carries `sources[]` — the files (in their OWN CRS) whose WGS84
    # bounds intersect that tile plus a per-file halo — and the converter
    # reprojects while sampling. `void_fill_m: 0.0` keeps the engine-visible
    # behaviour of the old INIT_DEST=0 warp: 0 m ground outside the DEM.
    # Per-tile filtering (not one whole-extent raster) bounds every decode to
    # one tile, independent of the total range or resolution.
    temp_tifs: List[str] = []
    # The converter writes every output into a scratch directory inside the
    # pool, and each finished tile is renamed into place: a pool tile is
    # replaced, never truncated in place — views hardlink pool tiles, and a
    # rewrite in place would change (or, mid-write, break) the terrain of a
    # view built earlier, possibly for another region.
    incoming: Optional[str] = None
    # The void counts the ingest printed for the tiles it placed, bound to
    # the placed files (converter >= 0.2.11; 0 with void_fill_m).
    built_voids: Dict[str, Dict[str, int]] = {}
    try:
        if beside is not None:
            # The building fetch runs NOW, beside the job build and the
            # conversion (the converter, >= 0.2.12, waits for the view's
            # marker at its burn).
            beside.start()
        if early_infos is not None or early_qgis_export:
            infos, use_qgis_export = early_infos or [], early_qgis_export
        else:
            infos, use_qgis_export = _gather_source_infos(
                dem_layer, terrain_dir, should_cancel=should_cancel)
        if src_index is None or src_index.infos is not infos:
            src_index = footprint_index(infos)
        if use_qgis_export:
            # One provider query for the run; the shared rule keeps this
            # export identical to the Map Converter's for the same layer.
            render_res = render_resolution_m(
                layer_native_resolution_m(dem_layer), float(resolution_m))
        if infos:
            union = _union_bbox([i["wgs84_bounds"] for i in infos])
            # Tell the user (once per run) if the range spills past the data.
            _warn_if_bbox_exceeds_bounds(union, bbox, source)
        elif use_qgis_export and dem_layer is not None:
            # A rendered provider (WMS/WMTS/WCS/ArcGIS) has no source files,
            # but QGIS knows the LAYER's own extent — the one bounds signal
            # that exists for it. Without this, running an elevation coverage
            # service over ground it does not publish (the NRW WCS over Bern)
            # exported empty tiles, the converter wrote them as void, Site
            # Analysis filled 0 m — and nothing ever told the user their
            # whole result was sea.
            # Same bounds :func:`rendered_export_gap` measures the export
            # against, read by the one helper, so "past the service" and
            # "the service dropped part of its answer" can never be told
            # apart by two different ideas of what it publishes.
            layer_bounds = layer_wgs84_bounds(dem_layer)
            if layer_bounds is not None:
                _warn_if_bbox_exceeds_bounds(layer_bounds, bbox, source)

        jobs = []
        ensure_engine_dirs(pool_dir)
        incoming = tempfile.mkdtemp(prefix=".incoming-", dir=pool_dir)
        for t in todo:
            # Per tile: a rendered export or a materialized window can take
            # seconds each, and nothing else here can be killed.
            if should_cancel is not None and should_cancel():
                raise ConverterCancelled()
            res_deg = t["exact_res_m"] / 111_111.0
            tile_bbox = {
                "north": t["ul_lat"],
                "south": t["ul_lat"] - t["size_px"] * res_deg,
                "west": t["ul_lon"],
                "east": t["ul_lon"] + t["size_px"] * res_deg,
            }
            # A partial build keeps the whole tile's geometry (ul_lat /
            # ul_lon / size_px) and lists only its regions' files.
            job = {
                "output_path": os.path.abspath(os.path.join(incoming,
                                                            t["filename"])),
                "format": "r16sint",
                "ul_lat": t["ul_lat"],
                "ul_lon": t["ul_lon"],
                "resolution_m": t["exact_res_m"],
                "size_px": t["size_px"],
                "void_fill_m": 0.0,
            }
            if use_qgis_export:
                # Non-file provider: server-rendered per tile into a WGS84
                # GeoTIFF (~4 output-px halo so edge sampling has
                # neighbours). The export IS WGS84, whatever the layer's own
                # CRS is, so the source entry says so explicitly.
                sub_bbox = _expand_bbox(tile_bbox, res_deg * 4.0)
                # Disambiguate by pool identity AND process: tile names are
                # globally canonical, so the same name means different pixels
                # for a different source, and two concurrent runs must not
                # share a scratch file the `finally` below deletes.
                tile_tif = os.path.join(
                    tempfile.gettempdir(),
                    f"aether_{os.path.basename(pool_dir)}_{os.getpid()}_"
                    f"{t['filename']}.tif",
                )
                t1 = time.perf_counter()
                _export_via_qgis(dem_layer, tile_tif, sub_bbox, render_res)
                temp_tifs.append(tile_tif)
                _log(f"  export {t['filename']}: "
                     f"{time.perf_counter() - t1:.1f}s, "
                     f"{os.path.getsize(tile_tif) / 1e6:.0f}MB")
                job["sources"] = [{"path": os.path.abspath(tile_tif),
                                   "crs": "EPSG:4326"}]
            else:
                job["sources"], temps = build_tile_sources(
                    infos, tile_bbox, tempfile.gettempdir(),
                    tag=f"{os.path.basename(pool_dir)}_{os.getpid()}_"
                        f"{t['filename']}",
                    output_res_m=t["exact_res_m"], index=src_index,
                    regions=verdicts[t["filename"]].regions)
                temp_tifs.extend(temps)
            # Omitted entirely when unset — MPT_SIGMA and older converters must
            # keep seeing exactly the job shape they do today.
            if buildings_file:
                job["buildings_file"] = os.path.abspath(buildings_file)
            if pbf_dir:
                job["buildings_pbf_dir"] = os.path.abspath(pbf_dir)
                if beside is not None:
                    job.update(beside.job_fields())
            jobs.append(job)

        # Immediately before the write and the launch, not only at the top of
        # this function: the pool has been observed to disappear between the
        # two on Windows, and the converter writes every output .abt into it.
        job_file = os.path.join(view_dir, "batch_job.json")
        ensure_engine_dirs(pool_dir, view_dir,
                           os.path.dirname(os.path.abspath(job_file)))
        with open(job_file, "w") as fh:
            json.dump(jobs, fh)

        # Name whatever is missing instead of letting the converter die with
        # a pathless "os error 3".
        preflight_ingest_jobs(jobs)

        t2 = time.perf_counter()
        exe = binary_manager.find_binary("aether_converter")
        if burns is not None:
            burns.begin(sum(1 for j in jobs if j.get("buildings_pbf_dir")))
        try:
            if beside is not None:
                # Launched at once: the converter decodes the view once the
                # fetch has written its marker; a failed fetch kills it and
                # raises its own error (nothing is placed).
                out_lines = beside.run(
                    lambda cancel, start, **_ready: _run_converter(
                        exe, job_file, should_cancel=cancel, on_start=start,
                        burns=burns), burns=burns)
            else:
                out_lines = _run_converter(
                    exe, job_file, should_cancel=should_cancel,
                    on_start=on_start,
                    **({"burns": burns} if burns is not None else {}))
        except ConverterCancelled:
            if (burns is None or not burns.bad_tiles
                    or (should_cancel is not None and should_cancel())):
                raise
            # Killed at the line after the converter's decode warning —
            # before its first tile: nothing reaches the pool (the
            # `finally` removes the scratch dir).
            raise RuntimeError(_buildings_burn_failed(burns))
        _log(f"  converter: {time.perf_counter() - t2:.1f}s ({len(jobs)} tiles)")
        if burns is not None:
            burns.end(tiles=len(jobs))
            if burns.verdict() is not None:
                # Before a tile is placed: the pool keeps what it had (those
                # tiles were not hits — they are built again next run).
                raise RuntimeError(_buildings_burn_failed(burns))
            if beside is None or not beside.no_data:
                # (No tile with data: the fetch warned already, and the
                # burn of the empty view says "0 tile(s)".)
                _log_buildings_burn(burns)

        # Exit 0 is not the verdict on buildings: a file it could not read is
        # one "[Warn] Failed to apply buildings: …" line and a clean exit, and
        # the tiles would then be pooled under an identity claiming buildings
        # they do not carry — a cache HIT, forever, for every later run.
        if buildings_file:
            complaint = _buildings_complaint(out_lines)
            if complaint:
                _log(f"  WARNING: the converter could not apply the buildings "
                     f"file: {complaint}")
                buildings_missing = True
            else:
                # Readable is not the same as applied: a source whose features
                # carry no usable height draws nothing and still exits 0.
                nothing = _buildings_drew_nothing(out_lines)
                if nothing:
                    _log(f"  WARNING: the buildings file was read but raised "
                         f"no pixel on any tile: {nothing}")
                    buildings_missing = True

        # The converter can exit 0 having skipped a tile it could not build,
        # so flag whatever did not land whole (a .part by the same rule as a
        # tile). Buildings that were requested but unavailable flag
        # everything: the pool identity claims buildings these pixels do not
        # carry.
        built = [t["filename"] for t in todo]
        short = {n for n in built
                 if not _tile_is_whole(os.path.join(incoming, n))}
        if short:
            _log(f"  WARNING: {len(built) - len(short)}/{len(built)} tiles "
                 f"written whole; {len(short)} short or missing "
                 f"(first: {sorted(short)[0]}) — flagged for rebuild")
        short |= _place_built_tiles(pool_dir, incoming, todo, verdicts, short,
                                    fp_for if early_infos else None)
        bad = set(short)
        if buildings_missing:
            _log("  buildings were requested but unavailable — tiles flagged "
                 "for rebuild so the next run retries them")
            bad = set(built)
        _set_rebuild_flags(pool_dir, built, bad)
        _write_source_fps(pool_dir, {
            t["filename"]: (fp_for(t, _subtile_bbox(t)) if early_infos
                            else None)
            for t in todo if verdicts[t["filename"]].regions is None
            and t["filename"] not in short}, bad)
        # Each placed tile's `[Tile] <name> voids=<n>` count, bound to the
        # file it was placed as (`<name>`, or `<name>.part` for a region
        # build — _place_built_tiles' rule); a tile not placed, or without a
        # line (an older converter), is unknown: its earlier record goes.
        counts = _parse_tile_voids(out_lines)
        placed: Dict[str, Optional[Dict[str, int]]] = {}
        for t in todo:
            name = t["filename"]
            n = None if name in short else counts.get(name)
            target = os.path.join(
                pool_dir, name + (_PART_SUFFIX if verdicts[name].regions
                                  is not None else ""))
            placed[name] = None if n is None else _voids_record(target, n)
        _record_pool_voids(pool_dir, placed)
        built_voids = {n: r for n, r in placed.items() if r}
    finally:
        if beside is not None:
            beside.close()          # no fetch outlives the run
        for tf in temp_tifs:
            if os.path.exists(tf):
                os.remove(tf)
        if incoming:
            shutil.rmtree(incoming, ignore_errors=True)

    _finish_view(pool_dir, view_dir, expected, t0, tiles=link_tiles,
                 voids=_view_voids(pool_dir, expected, link_tiles,
                                   run=built_voids))
    _record_view_dir(view_dir)
    return view_dir


#: Windows errors of a rename refused because another process holds the
#: source or the target open: ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION.
_RENAME_REFUSED_WINERRORS = (5, 32)


def _rename_refused(exc: OSError) -> bool:
    """True for the refusal :func:`replace_with_retry` waits out:
    PermissionError, ``EACCES`` / ``EPERM``, Windows error 5 / 32."""
    return (isinstance(exc, PermissionError)
            or exc.errno in (errno.EACCES, errno.EPERM)
            or getattr(exc, "winerror", None) in _RENAME_REFUSED_WINERRORS)


def replace_with_retry(src: str, dst: str, attempts: int = 5,
                       delay: float = 0.1) -> None:
    """``os.replace(src, dst)``, tried up to *attempts* times *delay*
    seconds apart while the rename is REFUSED (:func:`_rename_refused`).

    Windows refuses a rename over (or of) a file another process still
    holds open — Defender's on-close scan of a file just written, GDAL, a
    viewer — and lets go a moment later; POSIX renames over open files, so
    a working rename there is one call, as before. Any other error, and a
    refusal on the last attempt, is raised."""
    attempts = max(1, int(attempts))
    for attempt in range(1, attempts + 1):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            if attempt == attempts or not _rename_refused(exc):
                raise
        time.sleep(delay)


def _read_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _write_bytes_quietly(path: str, data: bytes) -> None:
    try:
        with open(path, "wb") as fh:
            fh.write(data)
    except OSError:
        pass


def _place_built_tiles(pool_dir: str, incoming: str,
                       todo: List[Dict[str, Any]],
                       verdicts: Dict[str, PoolTile], short: set,
                       fp_for: Optional[Callable[[Dict[str, Any],
                                                  Dict[str, float]], str]],
                       ) -> set:
    """Rename each finished tile from *incoming* into the pool — a partial
    build as ``<name>.part`` + its ``.part.json`` (abt first, sidecar
    second; the superseded complete tile and its ``.src`` removed), a whole
    build as ``<name>`` (a promoted ``.part`` pair removed; its ``.src`` is
    the caller's). A *short* tile is not served at all: every pool copy of
    it is removed (as an in-place rewrite used to destroy it).

    The rename goes through :func:`replace_with_retry`. One that is still
    refused after its retries leaves the pool as it was — the previous
    ``<name>`` / ``.part`` pair (its sidecar restored) and ``.src`` stay,
    only the new file is removed — and the tile counts as not built.
    Returns those names (the caller flags them for rebuild like a short
    tile; the view then links the previous copy, or reports the hole)."""
    failed: set = set()
    for t in todo:
        name = t["filename"]
        v = verdicts[name]
        tmp = os.path.join(incoming, name)
        canonical = os.path.join(pool_dir, name)
        src_fp = canonical + _SOURCE_FP_SUFFIX
        part, meta = _part_paths(pool_dir, name)
        if name in short:
            _remove_quietly(tmp, canonical, src_fp, part, meta)
            continue
        target = part if v.regions is not None else canonical
        old_meta: Optional[bytes] = None
        try:
            st = os.stat(tmp)
            if v.regions is not None:
                # Never bound to the new file — kept to restore if the old
                # .part stays.
                old_meta = _read_bytes(meta)
                _remove_quietly(meta)
            replace_with_retry(tmp, target)
        except OSError as exc:
            _remove_quietly(tmp)
            if old_meta is not None and not os.path.exists(meta):
                _write_bytes_quietly(meta, old_meta)
            kept = os.path.exists(canonical) or os.path.exists(part)
            _log(f"  WARNING: could not move {name} into the pool ({exc}) — "
                 + ("the previous pool copy is kept, " if kept else "")
                 + "flagged for rebuild")
            failed.add(name)
            continue
        if v.regions is not None:
            # Sidecar AFTER the rename (abt first, json second), bound to
            # this build's own file.
            _write_part_meta(pool_dir, t, [
                {"bbox": r, "fp": fp_for(t, r) if fp_for else None}
                for r in v.regions], st)
            _remove_quietly(canonical, src_fp)        # superseded
        else:
            _remove_quietly(part, meta)               # promoted / replaced
    return failed


def _profile_band_tiles(tx_lat: float, tx_lon: float,
                        bands: Sequence[Tuple[float, int]],
                        az_start: float, az_end: float):
    """Per band of a range profile: ``(idx, until_km, step, inner_km, bbox,
    tiles, skip_footprints)`` — the tiles :func:`prepare_terrain` builds for
    the band (its outer edge, minus tiles wholly inside the finer disc and
    footprints a finer band holds; *skip_footprints* is that set). ONE
    enumeration for :func:`prepare_terrain_profile` and
    :func:`run_region_union`."""
    covered: set = set()
    inner_km = 0.0
    for idx, (until_m, step) in enumerate(bands):
        until_km = until_m / 1000.0
        bbox = _compute_sector_bbox(tx_lat, tx_lon, until_km, az_start, az_end)
        skip = set(covered)
        tiles = _band_tiles(
            _compute_subtiles(bbox, step, tx_lat, tx_lon, until_km, az_start,
                              az_end),
            tx_lat, tx_lon, inner_km, skip)
        yield idx, until_km, step, inner_km, bbox, tiles, skip
        if tiles:
            covered.update(tile_footprint(t) for t in tiles)
        inner_km = until_km


def prepare_terrain_profile(
    dem_layer: Any,
    tx_lat: float,
    tx_lon: float,
    bands: List[Tuple[float, int]],
    binary_manager: Any,
    feedback: Optional[Any] = None,
    terrain_dir: Optional[str] = None,
    az_start: float = 0.0,
    az_end: float = 360.0,
    osm_buildings: bool = False,
    band_buildings: Optional[Sequence[bool]] = None,
    buildings_notes: Optional[Sequence[str]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    on_start: Optional[Callable[[Any], None]] = None,
    output_res_m: Optional[float] = None,
    region_union: Optional[Dict[str, List[Dict[str, float]]]] = None,
    terrain_prep: Optional[TerrainPrep] = None,
) -> str:
    """Terrain for a range-profile job (core.range_profile): one view directory
    holding, per band, the tiles at that band's step.

    *should_cancel* / *on_start* as for :func:`prepare_terrain`, which every
    band runs through; *should_cancel* is also polled once per band.

    Each band gets the tiles :func:`prepare_terrain` would build for its outer
    edge, minus what a finer band already covers — tiles wholly inside the finer
    disc, and tiles on a footprint a finer band holds (5 m and 10 m share the
    0.25 deg grid). Those are filtered BEFORE anything is fetched, so the
    download is the minimum: fine terrain only near the transmitter, coarse
    terrain only where the coarse band needs it. All bands share the one pool,
    so a later fixed run at any of these resolutions reuses the tiles.

    aether_core keeps every tile group in the directory and lets finer tiles
    win where they overlap (toolkit CONTRACT.md 3.3), which is what the
    combined view relies on.

    *osm_buildings* is the master switch; *band_buildings* (one flag per band,
    core.range_profile.band_buildings) narrows it per band — None means every
    band follows *osm_buildings*. A band without buildings builds into the
    building-free pool, one with them into the buildings pool; the view links
    each tile from its own pool (footprints never repeat across bands, so a
    view still holds one tile per footprint). *buildings_notes* is the per-band
    reason, logged alongside each band.

    A file source builds each band's tiles for the band's region only
    (:func:`resolve_pool_tile`; *output_res_m* = the job's output pixel, for
    the margin) and the view links each band tile through the same rule —
    the complete tile or a ``.part`` holding the region — under the
    canonical name; the view key carries the regions. *region_union*: see
    :func:`prepare_terrain`. *terrain_prep* (the run's :class:`TerrainPrep`):
    while current it IS the source read once for all bands (its infos and
    FootprintIndex); else the folder is read here, as before.
    """
    if is_abt_tile_dir(terrain_dir):
        # Pre-built .abt tiles: chosen per footprint for each band's step (never
        # left to the engine's closest-to-output-resolution pick).
        _log(f"  using pre-built .abt tiles from {terrain_dir} for the profile "
             f"— no download or conversion")
        # Buildings cannot be added to (or taken from) a prepared tile: say
        # what its record holds (the selection is memoised — _abt_view_for
        # reuses it).
        _log_prepared_buildings(
            terrain_dir, select_abt_tiles(terrain_dir, tx_lat, tx_lon,
                                          list(bands), az_start, az_end),
            bool(osm_buildings) and (band_buildings is None
                                     or any(band_buildings)))
        _record_route("terrain_dir")
        return _abt_view_for(terrain_dir, tx_lat, tx_lon, list(bands),
                             az_start, az_end)
    source = terrain_dir or dem_layer.source()
    if band_buildings is None:
        flags = [bool(osm_buildings)] * len(bands)
    else:
        if len(band_buildings) != len(bands):
            raise ValueError(f"band_buildings has {len(band_buildings)} flag(s) "
                             f"for {len(bands)} band(s)")
        flags = [bool(osm_buildings) and bool(f) for f in band_buildings]
    # pool identity (buildings id or None) -> tile names linked from that pool
    groups: Dict[Optional[str], List[str]] = {}
    names: List[str] = []
    # name -> (spec, region): how each band tile is linked (file sources)
    link: Dict[str, Tuple[Dict[str, Any], Optional[Dict[str, float]]]] = {}
    partial_ok = False
    # The source is read and its FootprintIndex built ONCE for every band
    # (each band used to re-read the folder and re-match every file).
    source_infos = None
    for (idx, until_km, step, inner_km, bbox, tiles,
         skip) in _profile_band_tiles(tx_lat, tx_lon, bands, az_start, az_end):
        if should_cancel is not None and should_cancel():
            raise ConverterCancelled()
        band_osm = flags[idx]
        if buildings_notes and idx < len(buildings_notes):
            note = buildings_notes[idx]
        elif band_osm:
            note = "buildings on"
        elif osm_buildings:
            note = "buildings skipped (band setting)"
        else:
            note = "buildings off"
        _log(f"prepare_terrain_profile: band {inner_km:g}-{until_km:g} km @ {step} m "
             f"— {len(tiles)} tile(s), {note}")
        if tiles:
            if source_infos is None:
                prep = _current_prep(terrain_prep, terrain_dir)
                source_infos = (
                    (prep.infos, False, prep.index) if prep is not None
                    else _read_source_once(dem_layer, terrain_dir,
                                           should_cancel=should_cancel))
                partial_ok = bool(source_infos and source_infos[0])
            prepare_terrain(dem_layer, tx_lat, tx_lon, until_km, step,
                            binary_manager, feedback, terrain_dir, az_start, az_end,
                            osm_buildings=band_osm,
                            min_range_km=inner_km, skip_footprints=skip,
                            should_cancel=should_cancel, on_start=on_start,
                            source_infos=source_infos,
                            output_res_m=output_res_m,
                            region_union=region_union)
            band_regions = tile_regions(tiles, bbox, step, output_res_m,
                                        partial_ok)
            for t in tiles:
                link[t["filename"]] = (t, band_regions[t["filename"]])
            band_names = [t["filename"] for t in tiles]
            names.extend(band_names)
            groups.setdefault(buildings_identity(None, band_osm), []).extend(band_names)

    # The view identity carries WHICH bands have buildings: the same tile
    # names with buildings on every band, on the fine bands only, or on none
    # are different terrain and must never share a view directory.
    if len(groups) > 1:
        osm_steps = ",".join(str(s) for (_, s), f in zip(bands, flags) if f)
        view_buildings: Optional[str] = (
            f"{buildings_identity(None, True)}@{osm_steps}m")
    else:
        view_buildings = next(iter(groups), buildings_identity(None, osm_buildings))
    view_dir = _view_dir(source, [{"filename": n} for n in names],
                         "profile", view_buildings,
                         {n: r for n, (_t, r) in link.items()})
    gone: List[str] = []
    for buildings_id, group_names in (groups.items() or
                                      [(view_buildings, [])]):
        pool_dir = _pool_dir(source, buildings_id)
        ensure_engine_dirs(pool_dir, view_dir)
        group_tiles = ({n: link[n] for n in group_names}
                       if partial_ok else None)
        # The bands' prepare_terrain recorded each tile's void count in its
        # pool (this run's downloads / ingests, or an earlier run's).
        gone += _sync_view(pool_dir, view_dir, group_names, keep=names,
                           tiles=group_tiles,
                           voids=_view_voids(pool_dir, group_names,
                                             group_tiles))
    if gone:
        raise RuntimeError(
            f"{len(gone)} terrain tile(s) vanished from the cache while the "
            f"profile view was assembled: {', '.join(gone[:5])}")
    _log(f"  profile .abt view dir: {view_dir} ({len(names)} tile(s))")
    _record_view_dir(view_dir)
    return view_dir
