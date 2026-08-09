"""Building footprints from OpenFreeMap vector tiles.

The Aether converter can burn building footprints into the terrain surface. It
reads them either from FlatGeobuf (absolute roof elevations in the geometry Z)
or from Mapbox Vector Tiles (a height above ground). This module fetches the
latter from `OpenFreeMap <https://openfreemap.org>`_ — the same source the
Waveshed web app uses, so both platforms model the same buildings.

Tiles are written as ``{z}_{x}_{y}.pbf``, which is the layout
``aether_converter``'s ``buildings_pbf_dir`` job field expects.

The data is OpenStreetMap-derived and carries an attribution requirement; see
:data:`ATTRIBUTION`. No API key is involved.
"""

from __future__ import annotations

import concurrent.futures
import json
import math
import os
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

# TileJSON endpoint. The tile URL it returns embeds a snapshot timestamp, so it
# has to be read at runtime rather than hard-coded.
TILEJSON_URL = "https://tiles.openfreemap.org/planet"

# Used only if TileJSON cannot be reached. OpenFreeMap may reject it when a
# snapshot path is required, so it is a last resort, not a shortcut.
FALLBACK_TEMPLATE = "https://tiles.openfreemap.org/planet/{z}/{x}/{y}.pbf"

# OpenMapTiles only carries the `building` layer from z14, so a coarser zoom
# would silently yield no buildings at all.
BUILDING_ZOOM = 14

# ODbL requires this to be shown wherever the data is presented.
ATTRIBUTION = (
    "Building data © OpenStreetMap contributors, © OpenMapTiles, "
    "© OpenFreeMap — ODbL"
)

# A z14 tile covers roughly 2.4 km, so area grows fast: a 100 km radius is
# already ~7000 tiles. Past this we stop and say so rather than starting a
# download that would run for hours.
MAX_TILES = 4096

_USER_AGENT = "waveshed-qgis"

# Resolved TileJSON template, cached for the life of the process.
_cached_template: Optional[str] = None


def lon_to_tile_x(lon: float, zoom: int) -> int:
    """Slippy-map tile column containing *lon*."""
    n = 1 << zoom
    x = int((lon + 180.0) / 360.0 * n)
    return max(0, min(n - 1, x))


def lat_to_tile_y(lat: float, zoom: int) -> int:
    """Slippy-map tile row containing *lat* (Web Mercator)."""
    n = 1 << zoom
    lat = max(-85.05112878, min(85.05112878, lat))
    rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(rad)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, y))


def tiles_for_bbox(bbox: Dict[str, float],
                   zoom: int = BUILDING_ZOOM) -> List[Tuple[int, int, int]]:
    """Every ``(z, x, y)`` tile covering *bbox*."""
    x0 = lon_to_tile_x(bbox["west"], zoom)
    x1 = lon_to_tile_x(bbox["east"], zoom)
    # Tile rows count southwards, so the north edge gives the smaller index.
    y0 = lat_to_tile_y(bbox["north"], zoom)
    y1 = lat_to_tile_y(bbox["south"], zoom)
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    return [(zoom, x, y)
            for y in range(y0, y1 + 1)
            for x in range(x0, x1 + 1)]


def estimate_tile_count(bbox: Dict[str, float],
                        zoom: int = BUILDING_ZOOM) -> int:
    """Tile count for *bbox* without building the list."""
    x0 = lon_to_tile_x(bbox["west"], zoom)
    x1 = lon_to_tile_x(bbox["east"], zoom)
    y0 = lat_to_tile_y(bbox["north"], zoom)
    y1 = lat_to_tile_y(bbox["south"], zoom)
    return (abs(x1 - x0) + 1) * (abs(y1 - y0) + 1)


def resolve_tile_template(timeout: float = 15.0) -> str:
    """Tile URL template from TileJSON, falling back to a fixed path.

    The result is cached: the snapshot timestamp in the URL is stable for the
    life of a session, and this saves a request per terrain build.
    """
    global _cached_template
    if _cached_template:
        return _cached_template

    try:
        req = urllib.request.Request(
            TILEJSON_URL, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
        tiles = doc.get("tiles") or []
        if tiles and isinstance(tiles[0], str):
            _cached_template = tiles[0]
            return _cached_template
    except Exception:  # noqa: BLE001 — any failure just means the fallback.
        pass

    _cached_template = FALLBACK_TEMPLATE
    return _cached_template


def tile_url(template: str, z: int, x: int, y: int) -> str:
    """Substitute tile coordinates into a ``{z}/{x}/{y}`` template."""
    return (template
            .replace("{z}", str(z))
            .replace("{x}", str(x))
            .replace("{y}", str(y)))


def tile_filename(z: int, x: int, y: int) -> str:
    """Name the converter's ``buildings_pbf_dir`` parser expects."""
    return f"{z}_{x}_{y}.pbf"


def download_building_tiles(
    bbox: Dict[str, float],
    dest_dir: str,
    zoom: int = BUILDING_ZOOM,
    max_workers: int = 8,
    timeout: float = 30.0,
    log=None,
) -> Tuple[int, int]:
    """Fetch building tiles covering *bbox* into *dest_dir*.

    Returns ``(downloaded, missing)``. A tile with no buildings answers 404,
    which is normal over water or empty countryside and is counted as missing
    rather than treated as an error.

    Raises :class:`ValueError` if the area needs more than :data:`MAX_TILES`.
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    count = estimate_tile_count(bbox, zoom)
    if count > MAX_TILES:
        raise ValueError(
            f"This area needs {count} building tiles (limit {MAX_TILES}). "
            "Building data is only published at zoom 14, so a large analysis "
            "area means a very large download. Reduce the range, or run "
            "without buildings."
        )

    os.makedirs(dest_dir, exist_ok=True)
    template = resolve_tile_template()
    tiles = tiles_for_bbox(bbox, zoom)
    _log(f"  buildings: {len(tiles)} tile(s) from OpenFreeMap (z{zoom})")

    def fetch(spec: Tuple[int, int, int]) -> bool:
        z, x, y = spec
        dest = os.path.join(dest_dir, tile_filename(z, x, y))
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            return True
        url = tile_url(template, z, x, y)
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
        except urllib.error.HTTPError as exc:
            # 404 = no buildings in this tile. Anything else is worth seeing.
            if exc.code != 404:
                _log(f"  buildings: {tile_filename(z, x, y)} HTTP {exc.code}")
            return False
        except Exception as exc:  # noqa: BLE001
            _log(f"  buildings: {tile_filename(z, x, y)} failed ({exc})")
            return False

        if not data:
            return False
        # Write via a temp name so an interrupted run cannot leave a truncated
        # tile behind that the next run would treat as already downloaded.
        tmp = dest + ".part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
        return True

    # `pool.map` yields in submission order, so one slow tile blocks counting
    # every tile behind it for up to `timeout`. Results are order-independent
    # here — only the running total matters — so take them as they land.
    ok = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(fetch, t) for t in tiles]
        for fut in concurrent.futures.as_completed(futures):
            if fut.result():
                ok += 1

    missing = len(tiles) - ok
    _log(f"  buildings: {ok} tile(s) with data, {missing} empty or unavailable")
    return ok, missing
