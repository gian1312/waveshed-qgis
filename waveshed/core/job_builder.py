"""Build aether_core job config JSON from dialog parameters.

Produces JSON matching the schema defined in rust/aether_core/src/config.rs.
"""

import dataclasses
import json
import math
import os
from typing import Optional, Sequence, Tuple

# Resolution values (metres) the plugin offers throughout the UI. aether_core
# takes resolution_m as a plain f32 (clamped to >= 0.1 m) with no fixed set, so
# this is a plugin-side convenience list, not an engine constraint. The coarse
# entries suit large-area studies: 90 m ~ SRTM 3-arcsec, 250 m ~ GMTED/MODIS.
# Keep this in sync with terrain_adapter.ABT_EXTENT_DEG (every value offered
# here needs a per-resolution .abt tile extent).
VALID_RESOLUTIONS = [2, 5, 10, 30, 90, 250]

# Mean Earth radius (m) — the value aether_core's P2P engine uses for the
# great-circle distance it measures every link with (engines/p2p.rs). Kept
# identical here so the plugin can never compute a shorter link than the
# engine does and cap a link the engine considers in range.
EARTH_RADIUS_M = 6_371_000.0

#: Safety margin (km) added on top of the longest link in a batch when the
#: job's ``analysis.max_range_km`` is derived from it. ``max_range_km`` is the
#: per-link distance CAP in aether_core: engines/p2p.rs builds every link with
#: ``max_dist_m = max_range_km * 1000`` and its trivial-link guard writes a
#: 0/0 result for any link whose measured distance exceeds it. An integer
#: ceiling alone would leave a link sitting exactly on the cap at the mercy of
#: the f32 rounding the engine does, so round up and add a kilometre.
MAX_RANGE_MARGIN_KM = 1

#: One batch-CSV row as the two P2P parsers produce it:
#: ``(type, id, lat, lon, altitude, mode)`` with type ``"S"`` (source/TX) or
#: ``"R"`` (receiver/RX).
BatchEntry = Tuple[str, str, float, float, float, str]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two WGS84 points.

    Same formula and same Earth radius as aether_core's P2P engine, so the
    distance the plugin uses to size ``analysis.max_range_km`` is the one the
    engine will compare against that cap.
    """
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2.0) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2)
    return (EARTH_RADIUS_M * 2.0
            * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))) / 1000.0


def longest_link_km(entries: Sequence[BatchEntry]) -> float:
    """Longest TX-RX great-circle distance (km) over a batch CSV's links.

    aether_core pairs a BATCH_P2P run as the full cross product of the ``S``
    rows with the ``R`` rows (engines/p2p.rs), so every such pair is a link
    that will be computed. Returns 0.0 when one of the two sides is empty —
    there is then no link to size anything from.
    """
    tx = [e for e in entries if e[0].strip().upper() == "S"]
    rx = [e for e in entries if e[0].strip().upper() == "R"]
    if not tx or not rx:
        return 0.0
    return max(haversine_km(s[2], s[3], r[2], r[3]) for s in tx for r in rx)


def batch_max_range_km(entries: Sequence[BatchEntry],
                       terrain_radius_km: float) -> int:
    """The ``analysis.max_range_km`` a BATCH_P2P job over *entries* needs.

    *terrain_radius_km* is what terrain preparation was asked for — the radius
    of the disc around the batch centroid that had to be tiled. It is NOT a
    valid distance cap: a batch whose endpoints span the full bounding box has
    links up to twice that radius long, and aether_core silently writes a 0/0
    result for every link longer than the cap (see ``MAX_RANGE_MARGIN_KM``).
    A 25.2 km Bern-Thun link under an 18 km terrain radius is exactly that
    case.

    So take whichever is larger: the terrain radius (kept so the two numbers
    never disagree for a small batch, and so a single-site job still covers
    its own disc) or the longest link plus ``MAX_RANGE_MARGIN_KM``.

    Raising the cap costs nothing in terrain: the engine selects the tiles a
    link needs by walking that link's own great-circle path, not by the cap,
    and the terrain the batch's bounding box produced already covers every
    path between two of its own endpoints.
    """
    needed = math.ceil(longest_link_km(entries)) + MAX_RANGE_MARGIN_KM
    return int(max(math.ceil(terrain_radius_km), needed))


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


# ---------------------------------------------------------------------------
# Model validity checks
# ---------------------------------------------------------------------------

CAUTION = "CAUTION"  # nearly out of range — usable with care (ITM kwx=1)
INVALID = "INVALID"  # out of range — probably invalid (ITM kwx=3/4)

# Vertical quantum of the terrain data: .abt stores elevations in 0.5 m steps.
# Not a limit on its own — it is the reason MIN_ANTENNA_AGL_M sits where it does.
TERRAIN_QUANTUM_M = 0.5

# Smallest antenna height above ground level the plugin accepts, for the
# transmitter and the receiver alike. Mirrors aether_core::config's floor.
#
# Two reasons converge on 1 m. ITM is undefined below it (the effective antenna
# height collapses to zero and the ground-reflection term saturates), and the
# terrain data is quantised to TERRAIN_QUANTUM_M, so a sub-metre antenna leaves
# the line-of-sight test a margin no bigger than the DEM's own rounding error —
# the visibility decision is then made by rounding rather than geometry, which
# renders as checkerboard speckle.
#
# The engine floors both TX and RX as a backstop so that old job JSON still
# runs. The plugin does not rely on that: it rejects the value outright at job
# build time (see _validate_antenna_heights) so the user is told, rather than
# being handed a result computed at a height they did not ask for.
#
# AGL only. An AMSL height is an absolute elevation and is legitimately zero or
# negative (Dead Sea shore -430 m, Schiphol -4 m) — it is never floored.
MIN_ANTENNA_AGL_M = 1.0

#: Lowest AMSL antenna elevation accepted anywhere. The Dead Sea shore, the
#: lowest exposed land on Earth, is about -430 m; -500 m clears it with room
#: to spare while still rejecting a value typed by accident. The GUI
#: spinboxes (gui/height_inputs.py) clamp to this same constant; this module
#: is the gate for every path that has no spinbox.
MIN_ANTENNA_AMSL_M = -500.0


@dataclasses.dataclass(frozen=True)
class ModelWarning:
    """One violated model constraint."""

    severity: str
    parameter: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.parameter}: {self.message}"


def model_warnings(params: "CoverageParams | P2PParams") -> "list[ModelWarning]":
    """Check job parameters against the propagation model's validated envelope.

    Mirror of ``aether_core::config::validate_model_inputs`` (rust/aether_core/
    src/config.rs) so the user sees the problem in the dialog instead of only in
    the engine log after the run. The limits are the ones ITM enforces itself in
    ``lrprop()``/``avar()`` (Splat NG ``itwom3.0.cpp:1250-1310``), where they
    raise the ``kwx`` indicator that neither engine surfaces in map mode.

    Keep this in sync with the Rust version when either changes.
    """
    out: "list[ModelWarning]" = []
    model = (params.model or "").upper()
    is_itm = "ITM" in model
    is_min_alt = "MIN_ALT" in model

    def add(severity: str, parameter: str, message: str) -> None:
        out.append(ModelWarning(severity, parameter, message))

    # Antenna heights. The lower bound applies to every propagation model, not
    # just ITM: it is a terrain-data limit (TERRAIN_QUANTUM_M) as much as a model
    # one, so a LOS run at 0.4 m AGL is decided by DEM rounding just the same.
    # The upper bounds are ITM's alone — validated to 1000 m AGL, hard max 12000.
    def check_height(label: str, height: float, mode: str) -> None:
        if (mode or "").upper() != "AGL":
            # AMSL is an absolute elevation, legitimately zero or negative; the
            # AGL height it implies is only known once terrain is sampled.
            return
        if height < MIN_ANTENNA_AGL_M:
            add(INVALID, label,
                f"{height:.2f} m AGL is below the {MIN_ANTENNA_AGL_M:.1f} m hard "
                f"minimum antenna height. ITM is undefined there (effective height "
                f"collapses to zero and the ground-reflection term saturates), and "
                f"the line-of-sight margin falls under the "
                f"{TERRAIN_QUANTUM_M:.1f} m vertical quantum of the terrain data, "
                f"so the visibility decision would be made by rounding rather than "
                f"geometry (checkerboard speckle). Use at least "
                f"{MIN_ANTENNA_AGL_M:.1f} m — 2 m to stay inside ITM's validated "
                f"range.")
        elif is_itm and height > 12000.0:
            add(INVALID, label, f"{height:.0f} m AGL exceeds ITM's 12000 m maximum.")
        elif is_itm and height > 1000.0:
            add(CAUTION, label,
                f"{height:.0f} m AGL is above ITM's 1000 m validated maximum.")

    check_height("TX height", params.tx_height, params.tx_mode)
    if not is_min_alt:
        # MIN_ALT solves for the receiver altitude; a 0 m receiver is the
        # question being asked there, not a mistake.
        check_height("RX height", params.rx_height, params.rx_mode)

    if is_itm:
        # Frequency: ITM works on wn = f/47.7, validated 0.838-210, hard 0.419-420.
        f = params.freq_mhz
        if f < 20.0 or f > 20000.0:
            add(INVALID, "Frequency",
                f"{f:.1f} MHz is outside ITM's 20 MHz - 20 GHz hard range.")
        elif f < 40.0 or f > 10000.0:
            add(CAUTION, "Frequency",
                f"{f:.1f} MHz is outside ITM's 40 MHz - 10 GHz validated range.")

        # Path distance: ITM is undefined below 1 km and above 2000 km. A coverage
        # map always contains the inner disc, so part of it is always out of range.
        r_km = params.max_range_km
        if r_km < 1:
            add(INVALID, "Range",
                f"{r_km} km is below ITM's 1 km minimum path length — the entire "
                f"result is outside the model's range.")
        else:
            add(CAUTION, "Range",
                f"ITM is undefined below 1 km path length, so the innermost 1 km of "
                f"this {r_km} km result is out of range on every azimuth "
                f"(the engine substitutes free-space loss below 100 m).")
        if r_km > 2000:
            add(INVALID, "Range", f"{r_km} km exceeds ITM's 2000 km maximum path length.")
        elif r_km > 1000:
            add(CAUTION, "Range", f"{r_km} km exceeds ITM's 1000 km validated path length.")

        # Atmosphere: ITM needs the height-reduced refractivity in 250-400 N-units.
        # It reduces the surface value per path as ens = N0 * exp(-zsys / 9460), so
        # a high site walks out of range even with a textbook sea-level N0.
        if not 250.0 <= params.ens <= 400.0:
            add(INVALID, "Surface refractivity",
                f"{params.ens:.1f} N-units is outside ITM's 250 - 400 range.")
        else:
            import math
            z_limit = 9460.0 * math.log(params.ens / 250.0)
            add(CAUTION, "Surface refractivity",
                f"N0 = {params.ens:.1f} reduces to ens = N0*exp(-zsys/9460) along each "
                f"path, so ITM reports out-of-range wherever the mean terrain height "
                f"exceeds {z_limit:.0f} m AMSL. Raise N0 for high-altitude scenes.")

        if not 1 <= params.climate <= 7:
            add(INVALID, "Radio climate",
                f"{params.climate} is not a valid ITM radio climate (1-7); "
                f"the model substitutes 5.")
        if params.pol not in (0, 1):
            add(INVALID, "Polarization",
                f"{params.pol} is not valid (0 = horizontal, 1 = vertical).")
        for label, v in (("Confidence", params.conf), ("Reliability", params.rel)):
            if not 0.01 <= v <= 0.99:
                add(CAUTION, label,
                    f"{v:.3f} is outside the 0.01 - 0.99 range ITM's quantile "
                    f"inversion is validated for.")

    return out


def format_model_warnings(warnings: "list[ModelWarning]") -> str:
    """Render warnings as dialog text, most severe first, deduplicated.

    String formatting only — ``core`` stays free of GUI imports, so each tab
    supplies its own QMessageBox around this.
    """
    seen = set()
    lines = []
    for w in sorted(warnings, key=lambda x: 0 if x.severity == INVALID else 1):
        key = (w.severity, w.parameter, w.message)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"• [{w.severity}] {w.parameter}\n    {w.message}")
    return "\n\n".join(lines)


def log_model_warnings(params: "CoverageParams | P2PParams") -> "list[ModelWarning]":
    """Write model-validity warnings to the QGIS log and return them.

    Called from the job builders so every entry point (both GUI tabs and both
    Processing algorithms) records them, even when no dialog is shown.
    """
    warnings = model_warnings(params)
    if not warnings:
        return warnings
    try:
        from qgis.core import Qgis, QgsMessageLog
        for w in warnings:
            level = (Qgis.MessageLevel.Warning if w.severity == INVALID
                     else Qgis.MessageLevel.Info)
            QgsMessageLog.logMessage(f"model validity: {w}", "Waveshed", level)
    except Exception:  # noqa: BLE001 — no QGIS available (unit tests)
        pass
    return warnings


def height_floor_error(
    label: str, height: float, mode: str,
) -> Optional[str]:
    """Return a rejection message if one AGL antenna height is below the floor.

    The one place the wording lives, so the dialogs, the Processing algorithms
    and the batch-CSV parsers all name the same field and the same limit.

    Args:
        label: What to call the field in the message, e.g. ``"TX height"``.
        height: The height in metres.
        mode: ``"AGL"`` or ``"AMSL"``.

    Returns:
        The message, or None when the height is acceptable. AMSL is an
        absolute elevation and is legitimately zero or negative (Dead Sea
        shore -430 m, Schiphol -4 m), so its floor is not the AGL one — it is
        :data:`MIN_ANTENNA_AMSL_M`, below the lowest exposed land on Earth.
        The GUI spinboxes clamp to the same constant; this is the gate for
        the paths that have no spinbox (batch CSVs, the Processing
        algorithms, direct params), which used to accept any depth at all —
        the torture suite's gate:amsl-floor check found -501 m sailing
        through to the engine.
    """
    if (mode or "").upper() != "AGL":
        if height >= MIN_ANTENNA_AMSL_M:
            return None
        return (
            f"{label} is {height:.2f} m AMSL, below the "
            f"{MIN_ANTENNA_AMSL_M:.0f} m floor — deeper than the lowest "
            f"exposed land on Earth (the Dead Sea shore, about -430 m). "
            f"Check the value and its units, or its AGL/AMSL mode."
        )
    if height >= MIN_ANTENNA_AGL_M:
        return None
    return (
        f"{label} is {height:.2f} m AGL, below the {MIN_ANTENNA_AGL_M:.1f} m "
        f"minimum antenna height above ground level. Below it the propagation "
        f"model is undefined and the line-of-sight decision is made by the "
        f"terrain data's {TERRAIN_QUANTUM_M:.1f} m rounding rather than by "
        f"geometry. Raise it to at least {MIN_ANTENNA_AGL_M:.1f} m, or switch "
        f"its height mode to AMSL if this is an absolute elevation."
    )


def antenna_height_error(params: "CoverageParams | P2PParams") -> Optional[str]:
    """Return a rejection message if either antenna is below the AGL floor.

    Args:
        params: The coverage or P2P parameters to check.

    Returns:
        The message for the first offending height, or None when both are fine.
    """
    is_min_alt = "MIN_ALT" in (params.model or "").upper()

    checks = [("TX height", params.tx_height, params.tx_mode)]
    if not is_min_alt:
        # MIN_ALT solves for the receiver altitude rather than being given one,
        # so its RX height is not an antenna and is exempt from the floor.
        checks.append(("RX height", params.rx_height, params.rx_mode))

    for label, height, mode in checks:
        message = height_floor_error(label, height, mode)
        if message is not None:
            return message
    return None


def _validate_antenna_heights(params: "CoverageParams | P2PParams") -> None:
    """Raise ValueError if a TX/RX antenna sits below :data:`MIN_ANTENNA_AGL_M`.

    The single convergence point for every entry point — both GUI tabs and both
    Processing algorithms reach the engine through ``build_coverage_job`` /
    ``build_p2p_job``, so putting the rejection here means no caller can bypass
    it by constructing params directly. Callers that want to report the problem
    before starting work call :func:`antenna_height_error` instead.

    Args:
        params: The coverage or P2P parameters about to be serialized.

    Raises:
        ValueError: If an AGL antenna height is below the floor.
    """
    message = antenna_height_error(params)
    if message is not None:
        raise ValueError(message)


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
        ValueError: If resolution_m is not in VALID_RESOLUTIONS, or if an AGL
            antenna height is below MIN_ANTENNA_AGL_M.
    """
    _validate_resolution(params.resolution_m)
    _validate_antenna_heights(params)
    log_model_warnings(params)

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
        ValueError: If resolution_m is not in VALID_RESOLUTIONS, or if an AGL
            antenna height is below MIN_ANTENNA_AGL_M. In batch mode the two
            heights here are the fallbacks; the per-link heights in the CSV are
            validated by whoever parses it (``gui.p2p_tab._parse_batch_csv`` /
            ``algorithms.p2p.P2PAlgorithm._parse_csv``).
    """
    _validate_resolution(params.resolution_m)
    _validate_antenna_heights(params)
    log_model_warnings(params)

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
