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
# Keep this in sync with map_converter_tab._ABT_EXTENT_DEG (the converter needs
# a per-resolution .abt tile extent for each value offered here).
VALID_RESOLUTIONS = [2, 5, 10, 30, 90, 250]


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

    # Processing
    max_ram_gb: int = 16
    max_vram_gb: int = 8

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

    # Processing
    max_ram_gb: int = 16
    max_vram_gb: int = 8

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
