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
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from osgeo import gdal

from .layer_utils import classify_raster_layer

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

# Matches the converter's no-data tally (CONTRACT changelog item 17), e.g.
#   [Stats] NO-DATA (HTTP 404): 43 of 49 tile(s) — the service has no data there
# A 404 is the service answering "no tile here" — data, not failure. The
# converter writes those pixels as void and exits 0; this count is how the
# plugin knows the misses are PERMANENT (retrying cannot change them) and that
# the user must be told part of their area reads as 0 m sea level.
_NO_DATA_RE = re.compile(r"\[Stats\]\s*NO-DATA\s*\(HTTP 404\):\s*(\d+)\s+of\s+\d+")


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


def _sync_view(pool_dir: str, view_dir: str, names: List[str]) -> List[str]:
    """Populate *view_dir* with exactly *names*, linked from the pool.

    Hardlinks cost nothing; where the filesystem refuses one (a different
    volume, FAT, or Windows without the privilege) we fall back to a copy.
    A pool tile that contains voids is not linked but COPIED WITH ITS VOIDS
    FILLED to 0 m — see :func:`_fill_view_voids`; the pool copy keeps them.
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
            try:
                fresh = os.path.getmtime(dst) >= os.path.getmtime(src)
            except OSError:
                fresh = True
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
        if not _tile_is_whole(src):
            unavailable.append(name)
            continue
        # A gapped pool tile (the run crossed the source's coverage edge)
        # must not reach the engine raw: the view gets a 0 m-filled copy —
        # the Site Analysis sea-level contract — while the pool keeps its
        # voids for the Map Converter.
        if _tile_has_voids(src):
            if not _fill_view_voids(src, dst):
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

class ConverterCancelled(Exception):
    """Raised by an ``on_line`` callback to abort a streaming engine run.

    ``run_converter_streaming`` kills the process and re-raises, so the
    caller keeps its own cancel control flow.
    """


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

    Every engine invocation (aether_converter download/ingest, aether_core)
    goes through here so pipe handling, encoding and cancellation exist
    exactly once.
    """
    with subprocess.Popen(
        [exe, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True, encoding="utf-8", errors="replace",
        env=env,
        creationflags=_SUBPROCESS_FLAGS,
    ) as proc:
        if on_start is not None:
            on_start(proc)
        try:
            for line in iter(proc.stdout.readline, ""):
                line = line.rstrip()
                if line:
                    on_line(line)
        except ConverterCancelled:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:  # noqa: BLE001 — best-effort kill on cancel
                pass
            raise
    return proc.returncode


# The converter's download progress line is a PARSED INTERFACE (see
# aether-tools docs/CONTRACT.md). Exact format, download.rs ~:747:
#   [Download] {pct}% ({done}/{total}) — {mb:.1} MB/s, {n} errors, {m} in-flight
_DL_PROGRESS_RE = re.compile(r"\[Download\]\s+(\d+)%\s+\((\d+)/(\d+)\)")


def _parse_download_progress(line: str) -> Optional[Tuple[int, int, int]]:
    """``(pct, done, total)`` from a ``[Download] X% (a/b) — …`` line, or None."""
    m = _DL_PROGRESS_RE.search(line)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


class DownloadProgressAggregator:
    """Pure math: per-zoom-group download fractions → one monotonic 0..1.

    A download runs one pass per zoom group (plus gap-repair retry passes
    over subsets). Each group is weighted by its output-tile count; a group's
    fraction only ever rises (a retry pass over a subset reports small
    ``done/total`` values again, which must not walk the bar backwards).
    """

    def __init__(self, group_weights: List[int]) -> None:
        self._w = [max(0, int(w)) for w in group_weights]
        self._total = sum(self._w) or 1
        self._fracs = [0.0] * len(self._w)

    def group_update(self, group_idx: int, frac: float) -> float:
        """Set group *group_idx* progress to *frac*; return overall 0..1."""
        frac = min(max(frac, 0.0), 1.0)
        self._fracs[group_idx] = max(self._fracs[group_idx], frac)
        return sum(w * f for w, f in zip(self._w, self._fracs)) / self._total


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
        _log(f"    {line}")
        lines.append(line)
        if on_line is not None:
            on_line(line)

    rc = run_converter_streaming(
        converter_exe, ["download", "--job-file", job_file], handle,
        on_start=on_start)

    if rc != 0:
        text = " ".join(lines).lower()
        if "insufficient disk" in text or "not enough space" in text:
            detail = lines[-1] if lines else "unknown"
            raise RuntimeError(
                f"Insufficient disk space for terrain download. {detail}"
            )
    return rc, lines


# Concurrency floor for the halving retry — below this, extra parallelism buys
# nothing and only risks more dropped connections.
_MIN_DOWNLOAD_CONNECTIONS = 32


def _abt_block_any(path: str, predicate, block: int = 64) -> bool:
    """True if any *block*×*block* pixel block of the .abt satisfies *predicate*
    on every pixel. Returns False for a file it cannot open or parse."""
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
        hit = predicate(elev[:h, :h]).reshape(h // block, block, h // block, block)
        return bool(hit.all(axis=(1, 3)).any())
    except Exception:
        return False


def _abt_has_zero_fill(path: str, block: int = 64) -> bool:
    """True if the .abt contains a fully-zero block of *block*×*block* pixels.

    A converter OLDER than the missing-tile fix wrote a failed XYZ tile as
    0 m marked real, so a zero block is the signature of failure fill from
    such a build. Kept as the honesty backstop (the torture suite calls it);
    genuine sea-level 0 (ocean) also trips it, which is why it only ever
    flags, never deletes.
    """
    return _abt_block_any(path, lambda e: e == 0, block)


def _abt_has_holes(path: str, block: int = 64) -> bool:
    """True if the .abt contains a fully-VOID block of *block*×*block* pixels.

    The converter writes a *failed* XYZ tile as the -9999 void sentinel, so
    a failed tile leaves a void block. Used to pick which sub-tiles to
    re-download. Genuine no-coverage voids (past a fixture's edge, ocean in
    a bathymetry-less set) also trip it, but the retry loop is bounded by
    its own pass count, so a legitimately-void tile can't loop forever.
    """
    from . import abt
    return _abt_block_any(path, lambda e: e <= abt.MIN_VALID_ELEV_COUNTS, block)


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
    try:
        if dem_layer is not None:
            src = dem_layer.source() or source
    except Exception:  # noqa: BLE001 — routing must not depend on the layer
        pass
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
) -> DownloadOutcome:
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

    Returns a :class:`DownloadOutcome`: falsy when the tiles could not be
    produced, carrying the engine's own explanation for the caller to raise.
    """
    # Each of the three bail-outs below used to return False without a word,
    # and the caller printed one generic "Rust download unavailable" for all of
    # them — so a missing engine looked exactly like an unsupported layer, and
    # the run then spent the whole extract on the slow path before anything
    # mentioned it. Say which one it was.
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

    # Per-run scratch job file: the Map Converter worker and Site Analysis
    # can download in the same QGIS process now, so a pid-derived shared name
    # would race. mkstemp is unique per call; removed in the finally below.
    job_fd, job_file = tempfile.mkstemp(prefix="aether_dl_", suffix=".json")
    os.close(job_fd)

    # Set by _run_pass; read after the passes to detect an engine that silently
    # ignored buildings_pbf_dir. A list so the closure can write to it.
    buildings_seen: List[bool] = []

    # Aggregated download progress across zoom groups and retry passes, for
    # callers that want a live bar (progress_cb(frac_0_to_1, label)).
    zoom_order = sorted(by_zoom)
    aggregator = (DownloadProgressAggregator(
        [len(by_zoom[z]) for z in zoom_order]) if progress_cb else None)

    def _run_pass(specs: List[Dict[str, Any]], conn: int, zoom: int):
        """Write + run one download pass for *specs* at *conn* connections.

        Returns ``(returncode, ok_tiles, total_tiles, lines)`` — the output
        lines ride along because they are the only place the *reason* for a
        failure exists (see :func:`_download_failure_detail`).
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

        on_line = None
        if progress_cb is not None:
            group_idx = zoom_order.index(zoom)

            def on_line(line: str) -> None:
                parsed = _parse_download_progress(line)
                if parsed:
                    _pct, done, total = parsed
                    overall = aggregator.group_update(
                        group_idx, done / max(total, 1))
                    progress_cb(overall,
                                f"Downloading terrain (z{zoom}): "
                                f"{done}/{total} source tiles")

        rc, lines = _run_converter_download_once(converter_exe, job_file,
                                                 on_line=on_line,
                                                 should_cancel=should_cancel,
                                                 on_start=on_start)
        if pbf_dir and rc == 0:
            buildings_seen.append(_buildings_were_applied(lines))
        ok, total = _parse_download_completeness(lines)
        return rc, ok, total, lines

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
            return DownloadOutcome(
                False, f"this aether_converter ({converter_exe}) predates "
                       f"buildings-on-download")
        if supported is None:
            _log(f"  could not read {converter_exe} to check for "
                 "buildings-on-download; assuming it is supported")
        _log("  buildings fused during download (buildings_pbf_dir)")

    try:
        ok_all = True
        reasons: List[str] = []
        for zoom in sorted(by_zoom):
            group = _download_zoom_group(
                cache_dir, by_zoom[zoom], zoom, base_conn, max_passes,
                _run_pass, _produced)
            if not group:
                ok_all = False
                if group.detail:
                    reasons.append(f"z{zoom}: {group.detail}")

        # The engine's own report is only a warning here, never the gate —
        # see the note on the capability check above. A supporting engine
        # that printed nothing still produced buildings; a *non*-supporting
        # one never got this far, because the gate refused the fast path
        # before any download ran.
        if pbf_dir and buildings_seen and not any(buildings_seen):
            _log("  note: no '[Buildings]' line in the converter output, but "
                 "the engine has the feature — buildings were applied. Report "
                 "this if the result is missing buildings.")

        return DownloadOutcome(ok_all, "\n".join(reasons))
    finally:
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
) -> DownloadOutcome:
    """Download one homogeneous-zoom group, with the halving retry.

    A failure carries the engine's own diagnosis (``DownloadOutcome.detail``)
    and leaves NOTHING of this group in the pool — see :func:`_drop_pool_tiles`.
    """
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
            rc, ok, total, lines = _run_pass(specs, conn, zoom)
            elapsed = time.perf_counter() - t
            if rc != 0:
                detail = _download_failure_detail(lines)
                _log(f"  Rust download failed (exit {rc}) at z={zoom}"
                     + (f":\n    {detail}" if detail else ""))
                # The engine pads every output tile to full size BEFORE it
                # decides the run failed, so a complete, valid .abt of 0 m
                # terrain is sitting in the pool right now. Leave it and the
                # next run reports a cache hit over flat sea.
                gone = _drop_pool_tiles(cache_dir, all_names)
                _log(f"    {gone} unusable tile(s) removed from the pool")
                return DownloadOutcome(False, detail or f"exit {rc}")
            if total < 0:
                # Older converter without completeness stats — trust exit 0.
                _log(f"  Rust download z={zoom}: {elapsed:.1f}s")
                _set_rebuild_flags(cache_dir, all_names)
                return DownloadOutcome(_produced())
            if ok >= total:
                _log(f"  Rust download z={zoom}: {elapsed:.1f}s, "
                     f"{ok}/{total} source tiles (XYZ) OK "
                     f"→ {len(specs)} output tile(s) @ {conn} conn")
                _set_rebuild_flags(cache_dir, all_names)
                return DownloadOutcome(True)

            no_data = _parse_download_no_data(lines)
            if total - ok == no_data:
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
                return DownloadOutcome(True)

            if ok == 0:
                # TOTAL failure, which is a different animal from the partial
                # one warned about further down: not one source tile arrived
                # and the misses are NOT all no-data answers (that case
                # returned as complete above — the converter said which), so
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
            affected = []
            for s in tile_specs:
                path = os.path.join(cache_dir, s["filename"])
                if (not os.path.exists(path) or _abt_has_holes(path)
                        or _abt_has_zero_fill(path)):
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
                return DownloadOutcome(True)

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
                return DownloadOutcome(_produced(),
                                      _download_failure_detail(lines))

            conn = max(_MIN_DOWNLOAD_CONNECTIONS, conn // 2)
            specs = affected
            _log(f"  Rust download incomplete ({missing} source tile(s) "
                 f"missing): retrying {len(specs)} affected output tile(s) "
                 f"@ {conn} connections (pass {attempt + 1}/{max_passes})")
        _set_rebuild_flags(cache_dir, all_names,
                           {s["filename"] for s in specs})
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


def ensure_pool_tiles(
    source_uri: str,
    subtiles: List[Dict[str, Any]],
    resolution_m: int,
    binary_manager: Any = None,
    progress_cb: Any = None,
    should_cancel: Any = None,
    on_start: Any = None,
) -> Dict[str, str]:
    """Shared acquisition entry: make the pool hold every tile in *subtiles*.

    THE one XYZ downloader for both terrain paths. Uses the same pool cache
    Site Analysis uses (v4 identity from ``source_identity`` — pure terrain,
    deliberately NO buildings term: buildings are applied at ingest by the
    Map Converter, which keeps this pool shareable between the tabs) and the
    same download machinery (zoom groups, gap-repair retry passes).

    Returns ``{filename: absolute pool path}`` for every requested tile.
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

    todo = [t for t in subtiles if not _tile_ready(pool_dir, t["filename"])]
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

    mapping: Dict[str, str] = {}
    missing: List[str] = []
    for t in subtiles:
        path = os.path.join(pool_dir, t["filename"])
        # A tile flagged for rebuild (gap-repair exhausted) still holds
        # usable partial terrain for THIS run — the same policy Site
        # Analysis applies when it links such tiles into its view. Only a
        # missing/short file is a hard failure.
        if _tile_is_whole(path):
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
) -> str:
    """Window-copy *path* to a plain GeoTIFF the converter can read.

    **Never resampled**: no target resolution, no warp — ``gdal.Translate``
    copies the source pixels at native resolution, optionally windowed to
    *window* (a bbox in the SOURCE CRS, already clamped to the file's own
    extent by the caller so GDAL never zero-fills a partially-outside
    window with fake 0 m ground).

    Shared by both terrain paths (Site Analysis and the Map Converter tab).
    Returns *dest*. Failure is a hard error naming the file.
    """
    opts: Dict[str, Any] = {"format": "GTiff"}
    if window:
        opts["projWin"] = [window["west"], window["north"],
                           window["east"], window["south"]]
    out = gdal.Translate(dest, path, **opts)
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
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """``sources[]`` entries for one tile, from per-file *infos*.

    *infos* is in priority order (first = highest; the converter takes the
    first valid sample per pixel). A file contributes when its WGS84 bounds
    intersect the tile bbox grown by a per-file halo (below). Files the
    converter cannot read directly are window-copied (tile window + halo,
    clamped to the file) into *tmp_dir*; the created temp paths are returned
    for cleanup.

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
    """
    _record_route("sources")
    from qgis.core import QgsCoordinateReferenceSystem

    center_lat = (tile_bbox["north"] + tile_bbox["south"]) / 2.0
    cos_lat = max(math.cos(math.radians(center_lat)), 0.2)

    sources: List[Dict[str, Any]] = []
    temps: List[str] = []
    for idx, info in enumerate(infos):
        halo_lat = max(info["halo_deg"], 0.6 * output_res_m / 111_111.0)
        halo_lon = halo_lat / cos_lat
        halo_bbox = {
            "west": tile_bbox["west"] - halo_lon,
            "east": tile_bbox["east"] + halo_lon,
            "south": tile_bbox["south"] - halo_lat,
            "north": tile_bbox["north"] + halo_lat,
        }
        if not bboxes_intersect(info["wgs84_bounds"], halo_bbox):
            continue
        path = info["path"]
        if _needs_materialization(path):
            if info["crs_authid"] == "EPSG:4326":
                window_native = halo_bbox
            else:
                wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
                window_native = _bbox_transform(halo_bbox, wgs84, info["crs"])
            window = _clamp_bbox(window_native, info["native_bounds"])
            if window is None:
                # The transformed tile+halo window misses the file's native
                # extent (the WGS84 filter and the native clamp can disagree
                # near a CRS's edge of validity). The file cannot contribute
                # to this tile — skip it. Falling back to a whole-file copy
                # here would defeat the very RAM bound windowing exists for
                # (a > 512 MB source would be copied in full).
                continue
            dest = os.path.join(tmp_dir, f"aether_src_{tag}_{idx}.tif")
            materialize_for_converter(path, dest, window)
            temps.append(dest)
            path = dest
        entry: Dict[str, Any] = {"path": os.path.abspath(path)}
        if info["crs_authid"].startswith("EPSG:"):
            entry["crs"] = info["crs_authid"]
        sources.append(entry)
    return sources, temps


_TERRAIN_EXTS = (".tif", ".tiff", ".dem", ".hgt")


def list_terrain_files(terrain_dir: str) -> List[str]:
    """Return every terrain raster under *terrain_dir* (recursive), sorted.

    Sorted ascending by full path, deliberately: this list becomes the
    ``sources[]`` priority order (first valid sample wins per pixel), so for
    overlapping files the earlier-sorted filename wins. os.walk order is
    filesystem-dependent — unsorted, two machines with the same folder would
    build different pixels under the identical pool cache key. This is THE
    folder scanner: the Map Converter uses it too (its own flat copy is
    gone).
    """
    found: List[str] = []
    for root, _, files in os.walk(terrain_dir):
        for f in files:
            if f.lower().endswith(_TERRAIN_EXTS):
                found.append(os.path.join(root, f))
    return sorted(found)


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
    handing `.abt` files to the converter as `sources` would fail, and running
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
    (``_gather_source_infos``), with no fall back to the base DEM, so
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
    if err != QgsRasterFileWriter.NoError:
        raise RuntimeError(f"writeRaster failed (code {err})")


def _gather_source_infos(dem_layer: Any, terrain_dir: Optional[str]
                         ) -> Tuple[List[Dict[str, Any]], bool]:
    """``(source file infos, use_qgis_export)`` for one prepare_terrain run.

    A terrain directory yields one info per raster file (CRS read from each
    file). A file-backed layer yields one info, carrying the layer's own
    authid when QGIS knows it. A non-file provider (WMS/WMTS/ArcGIS/…)
    yields no infos and ``use_qgis_export=True`` — those are server-rendered
    per tile via :func:`_export_via_qgis`, which is unavoidable.
    """
    if terrain_dir and os.path.isdir(terrain_dir):
        files = list_terrain_files(terrain_dir)
        if not files:
            raise RuntimeError(f"No terrain files in {terrain_dir}")
        _log(f"  local dir: {len(files)} files")
        return [source_file_info(f) for f in files], False

    src = dem_layer.source()
    if os.path.isfile(src) or src.startswith("/vsi"):
        authid = ""
        try:
            crs = dem_layer.crs()
            if crs is not None and crs.isValid():
                authid = crs.authid() or ""
        except Exception:  # noqa: BLE001 — fall back to the file's own CRS
            authid = ""
        return [source_file_info(src, crs_authid=authid or None)], False

    return [], True


def _run_converter(exe: str, job_file: str) -> List[str]:
    # Stream the converter's output into the log so the .abt creation step is
    # visible. Keep a tail for the error message. Uses the one shared
    # subprocess runner (run_converter_streaming).
    #
    # The lines come back because exit 0 is not the whole verdict: a buildings
    # file it could not read is one `[Warn]` line and a successful exit (see
    # ingest.rs `[Warn] Failed to apply buildings`), so the caller has to read
    # what it said to know whether the run did what was asked.
    lines: List[str] = []

    def handle(line: str) -> None:
        _log(f"    {line}")
        lines.append(line)

    rc = run_converter_streaming(exe, ["ingest", "--job-file", job_file],
                                 handle)
    if rc != 0:
        tail = "\n".join(lines[-20:])
        raise RuntimeError(f"converter failed ({rc}):\n{tail}")
    return lines


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
) -> str:
    """Produce .abt terrain tiles for aether_core. Returns cache directory.

    *progress_span* ``(lo, hi)``, when given together with a *feedback* that
    has ``setProgress``, maps the terrain-download fraction into that
    sub-range of the caller's progress bar (the caller knows its own phase
    layout; the adapter must not claim 0-100).

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
        _record_route("terrain_dir")
        return terrain_dir

    bbox = _compute_sector_bbox(tx_lat, tx_lon, max_range_km, az_start, az_end)
    subtiles = _compute_subtiles(bbox, resolution_m, tx_lat, tx_lon,
                                 max_range_km, az_start, az_end)
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
            # A hit re-serves what the source's contract route pooled
            # earlier (nothing else can fill a pool any more), so it counts
            # as that route — a warm run must not read as "acquired nothing".
            _record_route("sources" if terrain_dir
                          else _source_route(source, dem_layer))
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
    #
    # `buildings_missing` is the reconciliation between what the identity above
    # CLAIMS these tiles carry and what they will really carry: whenever it
    # ends up True the tiles are flagged for rebuild, so a pool keyed "with
    # buildings" never serves a cache hit over building-free terrain. Three
    # things can set it: an unusable buildings file (here), a failed
    # OpenFreeMap fetch (below), and the converter's own complaint after the
    # ingest ran.
    pbf_dir: Optional[str] = None
    buildings_missing = False
    if buildings_file and not buildings_source.is_converter_readable(
            buildings_file):
        _log(f"  WARNING: the converter cannot read {buildings_file} as "
             f"buildings — it reads FlatGeobuf only, and could not be handed "
             f"a converted copy of this source. It will warn and write "
             f"terrain without buildings.")
        buildings_missing = True
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
            # The (ok, missing) result used to be dropped on the floor, so a
            # run that fetched NOTHING carried on with buildings_missing=False
            # and pooled building-free terrain under a buildings-keyed
            # identity — a cache HIT for every later run, forever. Per-tile
            # failures are the silent case: the over-MAX_TILES guard raises,
            # but a dead network or a moved endpoint just returns (0, N).
            ok, missing = openfreemap.download_building_tiles(
                bbox, pbf_pool, log=_log)
            if ok == 0:
                raise RuntimeError(
                    f"no building tiles could be fetched ({missing} tile(s) "
                    f"empty or unavailable) — the terrain would carry no "
                    f"buildings at all"
                )
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
        # Trivial progress wiring only (A3): map the download fraction onto
        # the SUB-SPAN of the processing feedback the caller reserved for
        # terrain (progress_span). Mapping onto 0-100 here made the bar hit
        # 100 % mid-download and snap back when the caller resumed.
        progress_cb = None
        if (progress_span is not None and feedback is not None
                and hasattr(feedback, "setProgress")):
            span_lo, span_hi = progress_span

            def progress_cb(frac: float, _label: str) -> None:
                try:
                    feedback.setProgress(
                        int(span_lo + frac * (span_hi - span_lo)))
                except Exception:  # noqa: BLE001 — progress must never abort
                    pass
        outcome = _try_rust_download(dem_layer.source(), pool_dir, bbox,
                                     resolution_m, todo, binary_manager,
                                     pbf_dir=pbf_dir, progress_cb=progress_cb)
        if outcome:
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
            _record_route("download")
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
    try:
        infos, use_qgis_export = _gather_source_infos(dem_layer, terrain_dir)
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
            try:
                ext = dem_layer.extent()
                if ext is not None and not ext.isEmpty():
                    from qgis.core import QgsCoordinateReferenceSystem
                    layer_bounds = _bbox_transform(
                        {"west": ext.xMinimum(), "east": ext.xMaximum(),
                         "south": ext.yMinimum(), "north": ext.yMaximum()},
                        dem_layer.crs(),
                        QgsCoordinateReferenceSystem("EPSG:4326"))
                    _warn_if_bbox_exceeds_bounds(layer_bounds, bbox, source)
            except Exception:  # noqa: BLE001 — a warning must never abort a run
                pass

        jobs = []
        for t in todo:
            res_deg = t["exact_res_m"] / 111_111.0
            tile_bbox = {
                "north": t["ul_lat"],
                "south": t["ul_lat"] - t["size_px"] * res_deg,
                "west": t["ul_lon"],
                "east": t["ul_lon"] + t["size_px"] * res_deg,
            }
            job = {
                "output_path": os.path.abspath(os.path.join(pool_dir, t["filename"])),
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
                    output_res_m=t["exact_res_m"])
                temp_tifs.extend(temps)
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
        out_lines = _run_converter(exe, job_file)
        _log(f"  converter: {time.perf_counter() - t2:.1f}s ({len(jobs)} tiles)")

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
