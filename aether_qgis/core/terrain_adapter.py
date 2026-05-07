"""Terrain adapter — converts any QGIS raster layer to .abt tiles for aether_core.

Pipeline:
  1. Reject unsupported layer types (WMS/WMTS)
  2. Calculate bounding box around TX location
  3. Check tile cache; return early on hit
  4. Extract + reproject to WGS84 GeoTIFF via GDAL
  5. Compute tile dimensions (multiple-of-4 for BC6H)
  6. Write converter ingest job JSON
  7. Run aether_converter subprocess
  8. Clean up temp GeoTIFF
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import tempfile
from typing import Any, Dict, Optional

from osgeo import gdal


def get_cache_dir() -> str:
    """Return the terrain tile cache directory from plugin settings.

    Falls back to ``~/.aether/cache`` when no explicit setting exists.
    """
    from qgis.core import QgsSettings
    return QgsSettings().value("aether/cache_dir", os.path.expanduser("~/.aether/cache"))


def _compute_bbox(tx_lat: float, tx_lon: float, max_range_km: float) -> Dict[str, float]:
    """Return a WGS84 bounding box centred on *tx_lat*/*tx_lon*.

    The box extends *max_range_km* in each cardinal direction.  Longitude
    extent is widened by ``1 / cos(lat)`` to account for meridian
    convergence.
    """
    range_deg = max_range_km / 111.0
    cos_lat = math.cos(math.radians(tx_lat))
    # Guard against extreme latitudes where cos -> 0.
    if cos_lat < 1e-6:
        cos_lat = 1e-6
    lon_range_deg = range_deg / cos_lat

    return {
        "north": tx_lat + range_deg,
        "south": tx_lat - range_deg,
        "east": tx_lon + lon_range_deg,
        "west": tx_lon - lon_range_deg,
    }


def _cache_key(source: str, bbox: Dict[str, float], resolution_m: int) -> str:
    """Deterministic MD5 hash for a given source/bbox/resolution combination."""
    payload = f"{source}|{bbox}|{resolution_m}"
    return hashlib.md5(payload.encode()).hexdigest()


def _cache_hit(cache_dir: str) -> bool:
    """Return *True* if *cache_dir* already contains at least one .abt file."""
    if not os.path.isdir(cache_dir):
        return False
    return any(f.endswith(".abt") for f in os.listdir(cache_dir))


def _extract_reproject(
    dem_source: str,
    dest_tif: str,
    bbox: Dict[str, float],
) -> None:
    """Extract the area of interest from *dem_source* and reproject to WGS84.

    The result is a Float32 GeoTIFF written to *dest_tif*.
    """
    warp_options = gdal.WarpOptions(
        dstSRS="EPSG:4326",
        outputBounds=[bbox["west"], bbox["south"], bbox["east"], bbox["north"]],
        format="GTiff",
        outputType=gdal.GDT_Float32,
        resampleAlg=gdal.GRA_Bilinear,
    )
    result = gdal.Warp(dest_tif, dem_source, options=warp_options)
    if result is None:
        raise RuntimeError(
            f"GDAL Warp failed for source '{dem_source}'. "
            "Check that the DEM layer is readable and covers the analysis area."
        )
    # Flush and close the dataset.
    result.FlushCache()
    result = None  # noqa: F841  — closes the GDAL dataset


def _tile_size(bbox: Dict[str, float], resolution_m: int) -> int:
    """Compute the square tile edge length in pixels, rounded up to a multiple of 4.

    A multiple of 4 is required for BC6H block-compression compatibility in
    aether_core.
    """
    deg_per_m = 1.0 / 111_111.0
    pixel_deg = resolution_m * deg_per_m

    width_px = int(math.ceil((bbox["east"] - bbox["west"]) / pixel_deg))
    height_px = int(math.ceil((bbox["north"] - bbox["south"]) / pixel_deg))
    size_px = max(width_px, height_px)

    # Round up to next multiple of 4.
    return ((size_px + 3) // 4) * 4


def _build_job(
    cache_dir: str,
    cache_hash: str,
    bbox: Dict[str, float],
    resolution_m: int,
    size_px: int,
    temp_tif: str,
) -> str:
    """Write an aether_converter ingest job JSON file and return its path.

    The JSON schema matches ``rust/aether_converter/src/ingest.rs:18-29``.
    """
    job: Dict[str, Any] = {
        "output_path": os.path.join(cache_dir, f"tile_{cache_hash}.abt"),
        "format": "r16sint",
        "ul_lat": bbox["north"],
        "ul_lon": bbox["west"],
        "resolution_m": resolution_m,
        "size_px": size_px,
        "base_tif": os.path.abspath(temp_tif),
        "swiss_tifs": [],
    }

    job_file = os.path.join(cache_dir, "convert_job.json")
    with open(job_file, "w") as fh:
        json.dump(job, fh, indent=2)
    return job_file


def _run_converter(converter_exe: str, job_file: str) -> None:
    """Invoke ``aether_converter ingest --job-file <path>`` and check for errors."""
    result = subprocess.run(
        [converter_exe, "ingest", "--job-file", job_file],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"aether_converter failed (exit {result.returncode}):\n{result.stderr}"
        )


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
) -> str:
    """Convert any QGIS raster layer to .abt tiles for aether_core.

    Parameters
    ----------
    dem_layer:
        A ``QgsRasterLayer`` loaded in QGIS.
    tx_lat, tx_lon:
        Transmitter location in WGS84 decimal degrees.
    max_range_km:
        Maximum analysis radius in kilometres.
    resolution_m:
        Terrain resolution in metres (must be one of 2, 5, 10, 30).
    binary_manager:
        Object exposing ``find_binary(name) -> str`` to locate executables.
    feedback:
        Optional ``QgsProcessingFeedback`` (or compatible) for progress
        reporting.  Currently reserved for future use.

    Returns
    -------
    str
        Absolute path to a directory containing .abt tile(s).

    Raises
    ------
    ValueError
        If the layer type is unsupported (WMS/WMTS).
    RuntimeError
        If GDAL extraction or aether_converter fails.
    """

    # Step 1 — reject WMS/WMTS (rendered images, not elevation data).
    if dem_layer.providerType() in ("wms", "wmts"):
        raise ValueError(
            "WMS/WMTS layers provide rendered images, not elevation data. "
            "Please select a DEM raster layer (GeoTIFF, WCS, etc.)."
        )

    # Step 2 — bounding box around the transmitter.
    bbox = _compute_bbox(tx_lat, tx_lon, max_range_km)

    # Step 3 — check the tile cache.
    cache_hash = _cache_key(dem_layer.source(), bbox, resolution_m)
    cache_dir = os.path.join(get_cache_dir(), cache_hash)

    if _cache_hit(cache_dir):
        return cache_dir

    # Step 4 — extract + reproject to WGS84 GeoTIFF.
    temp_tif = os.path.join(tempfile.gettempdir(), f"aether_extract_{cache_hash}.tif")
    try:
        _extract_reproject(dem_layer.source(), temp_tif, bbox)

        # Step 5 — tile dimensions.
        size_px = _tile_size(bbox, resolution_m)

        # Step 6 — write converter ingest job.
        os.makedirs(cache_dir, exist_ok=True)
        job_file = _build_job(cache_dir, cache_hash, bbox, resolution_m, size_px, temp_tif)

        # Step 7 — run aether_converter.
        converter_exe = binary_manager.find_binary("aether_converter")
        _run_converter(converter_exe, job_file)

    finally:
        # Step 8 — always clean up the temporary GeoTIFF.
        if os.path.exists(temp_tif):
            os.remove(temp_tif)

    return cache_dir
