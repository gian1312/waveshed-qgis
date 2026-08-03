"""Build aether_core job config JSON from dialog parameters.

Produces JSON matching the schema defined in rust/aether_core/src/config.rs.
"""

import dataclasses
import json
import os
from typing import Optional

# Resolution values (metres) the plugin offers throughout the UI. aether_core
# takes resolution_m as a plain f32 (clamped to >= 0.1 m) with no fixed set, so
# this is a plugin-side convenience list, not an engine constraint. The coarse
# entries suit large-area studies: 90 m ~ SRTM 3-arcsec, 250 m ~ GMTED/MODIS.
# Keep this in sync with terrain_adapter.ABT_EXTENT_DEG (every value offered
# here needs a per-resolution .abt tile extent).
VALID_RESOLUTIONS = [2, 5, 10, 30, 90, 250]


def _processing_setting(key: str, default: int) -> int:
    """Read a processing budget (GB) from QgsSettings, falling back to default.

    Used as a dataclass ``default_factory`` rather than as a kwarg at every
    construction site on purpose: the Settings dialog has written
    ``waveshed/max_ram_gb`` and ``waveshed/max_vram_gb`` all along, but no
    construction site ever read them back, so both controls were dead and
    every job shipped the hardcoded defaults. Defaulting here means a new
    construction site cannot forget them.
    """
    try:
        from qgis.core import QgsSettings
        return max(1, int(QgsSettings().value(f"waveshed/{key}", default)))
    except Exception:  # noqa: BLE001 — no QGIS (unit tests), or a junk value
        return default


def _default_max_ram_gb() -> int:
    return _processing_setting("max_ram_gb", 16)


def _default_max_vram_gb() -> int:
    return _processing_setting("max_vram_gb", 8)


@dataclasses.dataclass
class CoverageParams:
    """Parameters for a coverage (site analysis) job."""

    # Transmitter
    tx_lat: float = 0.0
    tx_lon: float = 0.0
    tx_height: float = 30.0
    tx_mode: str = "AGL"
    freq_mhz: float = 433.0
    erp_watts: float = 10.0
    az_pattern: Optional[str] = None
    el_pattern: Optional[str] = None
    az_rotation: Optional[float] = None

    # Receiver
    rx_height: float = 1.5
    rx_mode: str = "AGL"

    # Analysis
    model: str = "LOS"
    resolution_m: int = 10
    max_range_km: int = 50
    az_start: float = 0.0
    az_end: float = 360.0
    backend: str = "AUTO"

    # Output
    output_name: str = "coverage"

    # Processing (defaults come from Settings — see _processing_setting)
    max_ram_gb: int = dataclasses.field(default_factory=_default_max_ram_gb)
    max_vram_gb: int = dataclasses.field(default_factory=_default_max_vram_gb)

    # Propagation (ITM defaults from config.rs)
    eps: float = 15.0
    sgm: float = 0.005
    ens: float = 301.0
    climate: int = 5
    pol: int = 0
    conf: float = 0.50
    rel: float = 0.50
    earth_radius: str = "FOUR_THIRDS"

    # GUI-only (not serialized to JSON)
    add_to_map: bool = True


@dataclasses.dataclass
class P2PParams:
    """Parameters for a point-to-point analysis job."""

    # Transmitter
    tx_lat: float = 0.0
    tx_lon: float = 0.0
    tx_height: float = 30.0
    tx_mode: str = "AGL"
    freq_mhz: float = 433.0
    erp_watts: float = 10.0
    az_pattern: Optional[str] = None
    el_pattern: Optional[str] = None
    az_rotation: Optional[float] = None

    # Receiver
    rx_height: float = 1.5
    rx_mode: str = "AGL"

    # Analysis
    model: str = "LOS"
    resolution_m: int = 10
    max_range_km: int = 50
    az_start: float = 0.0
    az_end: float = 360.0
    backend: str = "AUTO"

    # Output
    output_name: str = "p2p"

    # Processing (defaults come from Settings — see _processing_setting)
    max_ram_gb: int = dataclasses.field(default_factory=_default_max_ram_gb)
    max_vram_gb: int = dataclasses.field(default_factory=_default_max_vram_gb)

    # Propagation (ITM defaults from config.rs)
    eps: float = 15.0
    sgm: float = 0.005
    ens: float = 301.0
    climate: int = 5
    pol: int = 0
    conf: float = 0.50
    rel: float = 0.50
    earth_radius: str = "FOUR_THIRDS"

    # GUI-only (not serialized to JSON)
    add_to_map: bool = True


def _validate_resolution(resolution_m: int) -> None:
    """Raise ValueError if resolution is not in the allowed set."""
    if resolution_m not in VALID_RESOLUTIONS:
        raise ValueError(
            f"Invalid resolution_m={resolution_m}. "
            f"Must be one of {VALID_RESOLUTIONS}."
        )


def _build_tx(params: "CoverageParams | P2PParams") -> dict:
    """Build the tx section of the job config."""
    tx = {
        "lat": params.tx_lat,
        "lon": params.tx_lon,
        "height_m": params.tx_height,
        "mode": params.tx_mode,
        "freq_mhz": params.freq_mhz,
        "erp_watts": params.erp_watts,
    }
    if params.az_pattern:
        tx["az_pattern_file"] = params.az_pattern
    if params.el_pattern:
        tx["el_pattern_file"] = params.el_pattern
    if params.az_rotation is not None:
        tx["az_rotation"] = params.az_rotation
    return tx


def _build_propagation(params: "CoverageParams | P2PParams") -> dict:
    """Build the propagation section of the job config."""
    return {
        "eps_dielect": params.eps,
        "sgm_conductivity": params.sgm,
        "eno_ns_surfref": params.ens,
        "radio_climate": params.climate,
        "pol": params.pol,
        "conf": params.conf,
        "rel": params.rel,
        "earth_radius_mode": params.earth_radius,
    }


def build_coverage_job(
    params: CoverageParams,
    abt_dir: str,
    output_dir: str,
) -> dict:
    """Build a coverage (SINGLE) job config dict.

    Args:
        params: Coverage analysis parameters.
        abt_dir: Directory containing .abt terrain tiles.
        output_dir: Directory for output files.

    Returns:
        Job config dict matching aether_core's JobConfig schema.

    Raises:
        ValueError: If resolution_m is not in VALID_RESOLUTIONS.
    """
    _validate_resolution(params.resolution_m)

    return {
        "tx": _build_tx(params),
        "rx": {
            "height_m": params.rx_height,
            "mode": params.rx_mode,
        },
        "analysis": {
            "task_type": "SINGLE",
            "propagation_model": params.model,
            "max_range_km": params.max_range_km,
            "resolution_m": params.resolution_m,
            "azimuth_start_deg": params.az_start,
            "azimuth_end_deg": params.az_end,
            "compute_backend": params.backend,
        },
        "output": {
            "directory": output_dir,
            "filename": params.output_name,
        },
        "processing": {
            "terrain_dir": abt_dir,
            "max_ram_usage_gb": params.max_ram_gb or 16,
            "max_vram_usage_gb": params.max_vram_gb or 8,
        },
        "propagation": _build_propagation(params),
    }


def build_p2p_job(
    params: P2PParams,
    abt_dir: str,
    output_dir: str,
    batch_file: Optional[str] = None,
) -> dict:
    """Build a P2P or BATCH_P2P job config dict.

    Args:
        params: P2P analysis parameters.
        abt_dir: Directory containing .abt terrain tiles.
        output_dir: Directory for output files.
        batch_file: Path to CSV file for batch mode. When provided,
            task_type is set to BATCH_P2P; otherwise P2P.

    Returns:
        Job config dict matching aether_core's JobConfig schema.

    Raises:
        ValueError: If resolution_m is not in VALID_RESOLUTIONS.
    """
    _validate_resolution(params.resolution_m)

    task_type = "BATCH_P2P" if batch_file else "P2P"

    analysis = {
        "task_type": task_type,
        "propagation_model": params.model,
        "max_range_km": params.max_range_km,
        "resolution_m": params.resolution_m,
        "azimuth_start_deg": params.az_start,
        "azimuth_end_deg": params.az_end,
        "compute_backend": params.backend,
    }
    if batch_file:
        analysis["batch_file"] = batch_file

    return {
        "tx": _build_tx(params),
        "rx": {
            "height_m": params.rx_height,
            "mode": params.rx_mode,
        },
        "analysis": analysis,
        "output": {
            "directory": output_dir,
            "filename": params.output_name,
        },
        "processing": {
            "terrain_dir": abt_dir,
            "max_ram_usage_gb": params.max_ram_gb or 16,
            "max_vram_usage_gb": params.max_vram_gb or 8,
        },
        "propagation": _build_propagation(params),
    }


def write_job_file(job_config: dict, output_dir: str, name: str) -> str:
    """Write a job config dict to a JSON file.

    Args:
        job_config: The job configuration dictionary.
        output_dir: Directory in which to write the file.
        name: Base name for the file (produces {name}_job.json).

    Returns:
        Absolute path to the written JSON file.
    """
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"{name}_job.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(job_config, f, indent=2)
    return os.path.abspath(path)


def job_file_for_result(result_path: str) -> Optional[str]:
    """Return the ``{name}_job.json`` written beside *result_path*, if present.

    A run writes its job config next to the GeoTIFF it produces, which makes
    the config the one durable record of how that raster was made — it survives
    the QGIS session, and it is the same file whether the run came from the
    Site Analysis tab or a Processing algorithm.  Returns None when the file
    was moved away from its job config (nothing here is load-bearing enough to
    be worth an error).
    """
    if not result_path:
        return None
    stem = os.path.splitext(os.path.basename(result_path))[0]
    path = os.path.join(os.path.dirname(result_path), f"{stem}_job.json")
    return path if os.path.isfile(path) else None


def read_job_terrain_dir(result_path: str) -> Optional[str]:
    """Return the ``.abt`` terrain directory a result raster was computed over.

    Read back out of the job config beside it (see
    :func:`job_file_for_result`).  This is what lets the Altitude Explorer's
    above-sea-level view use *the run's own terrain* rather than asking the
    user to re-identify a DEM that has to match to the metre.  Returns None if
    the config or the directory is gone.
    """
    job_path = job_file_for_result(result_path)
    if job_path is None:
        return None
    try:
        with open(job_path, "r", encoding="utf-8") as handle:
            config = json.load(handle)
        terrain_dir = config.get("processing", {}).get("terrain_dir")
    except (OSError, ValueError, AttributeError):
        return None
    if terrain_dir and os.path.isdir(terrain_dir):
        return terrain_dir
    return None
