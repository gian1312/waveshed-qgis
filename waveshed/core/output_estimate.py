"""Disk the engine's coverage raster will need, before anything is run.

A coverage map is written at ``analysis.resolution_m`` over the whole range —
also for an adaptive / custom range profile, whose coarse bands change the ray
sampling, not the pixel size. So the raster grows with (range / pixel)²: 2 m
out to 500 km is ~2·10¹¹ pixels, which LOS Floor (2 bytes/px) writes as ~390 GB.
That run used to download its terrain for minutes and then die with "not
enough space on the disk" two hours into the engine (2026-10-13).

The figures mirror aether_core's writer (``solver.rs::output_tile_bytes``,
``io/tiled_buffer.rs``): 512 px tiles, written only where a pixel is in range
(all-empty tiles are skipped), at 1 bit (LOS), 2 bytes (MIN_ALT / LOS Floor) or
1 byte (SIMPLE_LOSS, ITM) per pixel. aether_core runs the same check itself;
this one exists so the plugin can warn *before* the terrain download.

No GUI imports.
"""

from __future__ import annotations

import math
import os
import shutil
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Tuple

from .job_builder import VALID_RESOLUTIONS

#: aether_core's output tile edge (``io/tiled_buffer.rs::OUTPUT_TILE_SIZE``).
OUTPUT_TILE_PX = 512

#: Above this the run asks before starting, even when the disk has room.
LARGE_OUTPUT_BYTES = 20 * 1024 ** 3

#: Keep this much of the disk free on top of the estimate (the GeoTIFF export,
#: QGIS, the OS).
DISK_HEADROOM = 1.10


def bits_per_pixel(model: str) -> int:
    """Raster bits per pixel for an ``analysis.propagation_model`` value,
    as ``OutputMode::from_analysis_mode`` maps it."""
    model = (model or "").upper()
    if model == "MIN_ALT":
        return 16
    if model in ("SIMPLE_LOSS", "ITM"):
        return 8
    return 1


def sector_fraction(az_start: float, az_end: float) -> float:
    """Share of the full circle an azimuth sector covers (0..1]."""
    span = (az_end - az_start) % 360.0
    if span == 0.0 and az_end != az_start:
        span = 360.0
    if az_start == az_end or span == 0.0:
        return 1.0
    return span / 360.0


def raster_pixels(max_range_km: float, resolution_m: float,
                  az_start: float = 0.0, az_end: float = 360.0) -> int:
    """Pixels the engine stores: the in-range disc (or sector), rounded out
    to the 512 px tiles it touches."""
    r_px = max_range_km * 1000.0 / max(resolution_m, 1e-6)
    # Tiles crossing the edge are stored whole: grow the radius by a tile
    # diagonal, but never beyond the raster's bounding square.
    grown = r_px + OUTPUT_TILE_PX * math.sqrt(2.0)
    disc = math.pi * grown * grown * sector_fraction(az_start, az_end)
    square = (2.0 * r_px + OUTPUT_TILE_PX) ** 2
    return int(min(disc, square))


def engine_output_bytes(model: str, max_range_km: float, resolution_m: float,
                        az_start: float = 0.0, az_end: float = 360.0) -> int:
    """Bytes of the ``.bit``/``.tiles`` raster aether_core writes for one job."""
    pixels = raster_pixels(max_range_km, resolution_m, az_start, az_end)
    return pixels * bits_per_pixel(model) // 8


def free_bytes(path: str) -> Optional[int]:
    """Free space on the volume holding *path* (its nearest existing parent)."""
    probe = os.path.abspath(path or ".")
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def format_bytes(n: float) -> str:
    """``1.2 GB`` style, binary units."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def coarsest_fitting_resolution(model: str, max_range_km: float,
                                budget_bytes: int, candidates: Iterable[int],
                                az_start: float = 0.0, az_end: float = 360.0,
                                ) -> Optional[int]:
    """The finest of *candidates* whose raster fits in *budget_bytes*."""
    for res in sorted(candidates):
        if engine_output_bytes(model, max_range_km, res, az_start, az_end) <= budget_bytes:
            return res
    return None


def output_space_problem(
    jobs: List[Tuple[str, str, float, float, float, float]],
    output_dir: str,
    free: Optional[int] = None,
) -> Tuple[Optional[str], bool]:
    """``(message, blocking)`` for the rasters *jobs* will write, or
    ``(None, False)`` when there is nothing to say.

    *jobs* are ``(label, model, max_range_km, resolution_m, az_start, az_end)``.
    ``blocking`` means the disk cannot hold the engine output (Site Analysis
    then refuses the run through :func:`drive_needs` / :func:`refusal_text`,
    which add the terrain pool on its drive — 2026-10-21; before, this was a
    "Run anyway?" warning). A non-blocking message is a large output that
    still fits.
    """
    sizes = [(label, model, rng, res, engine_output_bytes(model, rng, res, a0, a1))
             for label, model, rng, res, a0, a1 in jobs]
    total = sum(s[-1] for s in sizes)
    if free is None:
        free = free_bytes(output_dir)
    big = sorted(sizes, key=lambda s: -s[-1])[:5]
    lines = [f"  {label}: {format_bytes(b)} ({res:g} m pixels to {rng:g} km)"
             for label, _m, rng, res, b in big]
    if len(sizes) > len(big):
        lines.append(f"  … and {len(sizes) - len(big)} more")
    detail = "\n".join(lines)
    why = ("The map is written at the chosen resolution over the whole range "
           "(also with Adaptive / Custom, whose coarser bands only change the "
           "sampling), so its size grows with (range ÷ resolution)².")
    if free is not None and total * DISK_HEADROOM > free:
        hint = ""
        label, model, rng, res, biggest = big[0]
        a0, a1 = next((j[4], j[5]) for j in jobs if j[0] == label)
        budget = int(free / DISK_HEADROOM) - (total - biggest)
        fit = coarsest_fitting_resolution(model, rng, budget, VALID_RESOLUTIONS, a0, a1)
        if fit is not None and fit != res:
            hint = f" At {rng:g} km, {fit} m pixels would fit for {label}."
        return (f"The coverage raster{'s' if len(sizes) > 1 else ''} need about "
                f"{format_bytes(total)}, but only {format_bytes(free)} is free in\n"
                f"{output_dir}\n\n{detail}\n\n{why}\n\nUse a coarser resolution, "
                f"a shorter range or a sector, or free disk space.{hint}\n\n"
                f"Run anyway? It will fail when the disk is full.", True)
    if total > LARGE_OUTPUT_BYTES:
        free_txt = f" ({format_bytes(free)} free)" if free is not None else ""
        return (f"The coverage raster{'s' if len(sizes) > 1 else ''} will take "
                f"about {format_bytes(total)} on disk{free_txt}, plus the GeoTIFF "
                f"exported from it.\n\n{detail}\n\n{why}\n\nRun anyway?", False)
    return None, False


# ---------------------------------------------------------------------------
# Per-drive disk need (Map Converter, Site Analysis): refuse instead of ENOSPC
# ---------------------------------------------------------------------------

#: A run is refused when its need on a drive exceeds that drive's free space
#: minus this reserve (QGIS, the OS, the converter's in-flight temp tile, the
#: GeoTIFF export all need room too).
DISK_RESERVE_BYTES = 2 * 1024 ** 3


class DriveNeed(NamedTuple):
    """What one run writes to one drive (volume)."""
    drive: str                  # display name: "C:" / the mount point
    labels: Tuple[str, ...]     # what goes there ("output tiles", ...)
    paths: Tuple[str, ...]      # the directories, first seen first
    need: int                   # bytes
    free: Optional[int]         # bytes; None = could not be measured

    @property
    def short(self) -> bool:
        """need > free − :data:`DISK_RESERVE_BYTES` (unknown free: never)."""
        return self.free is not None and self.need > self.free - DISK_RESERVE_BYTES


def _existing_parent(path: str) -> Optional[str]:
    probe = os.path.abspath(path or ".")
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    return probe


def volume_of(path: str) -> Tuple[Any, str]:
    """``(key, display name)`` of the volume holding *path* (its nearest
    existing parent): the drive letter / UNC share on Windows, the device
    and its mount point elsewhere. Paths on one volume share one key."""
    probe = _existing_parent(path)
    if probe is None:
        absolute = os.path.abspath(path or ".")
        return ("?", absolute), absolute
    if os.name == "nt":
        drive = os.path.splitdrive(probe)[0].upper() or probe
        return drive, drive
    try:
        dev = os.stat(probe).st_dev
    except OSError:
        return ("?", probe), probe
    mount = probe
    while not os.path.ismount(mount):
        parent = os.path.dirname(mount)
        if parent == mount:
            break
        mount = parent
    return dev, mount


def drive_needs(items: Iterable[Tuple[str, str, int]],
                free_of: Optional[Callable[[str], Optional[int]]] = None,
                volume: Optional[Callable[[str], Tuple[Any, str]]] = None,
                ) -> List[DriveNeed]:
    """Group ``(label, directory, bytes)`` *items* per drive and measure each
    drive's free space once (``shutil.disk_usage`` via :func:`free_bytes`;
    *free_of* / *volume* replace the probes). Items with 0 bytes still name
    their drive (need 0 there); order = first seen."""
    free_of = free_of or free_bytes
    volume = volume or volume_of
    groups: Dict[Any, Dict[str, Any]] = {}
    for label, path, need in items:
        key, name = volume(path)
        g = groups.setdefault(key, {"drive": name, "labels": [], "paths": [],
                                    "need": 0, "probe": path})
        if label not in g["labels"]:
            g["labels"].append(label)
        if path not in g["paths"]:
            g["paths"].append(path)
        g["need"] += max(0, int(need))
    return [DriveNeed(g["drive"], tuple(g["labels"]), tuple(g["paths"]),
                      g["need"], free_of(g["probe"]))
            for g in groups.values()]


def short_drives(needs: Iterable[DriveNeed]) -> List[DriveNeed]:
    """The drives a run cannot fit on (:attr:`DriveNeed.short`)."""
    return [d for d in needs if d.short]


def format_gb(n: Optional[float]) -> str:
    """``12.3 GB`` (``0.06 GB`` below 10 GB); ``?`` when unknown."""
    if n is None:
        return "?"
    gb = n / 1024 ** 3
    return f"{gb:,.2f} GB" if gb < 10 else f"{gb:,.1f} GB"


def format_drive_need(d: DriveNeed) -> str:
    """``C: need 12.3 GB / free 40.1 GB (output tiles, terrain pool)``."""
    return (f"{d.drive} need {format_gb(d.need)} / free {format_gb(d.free)}"
            f" ({', '.join(d.labels)})")


def refusal_text(short: List[DriveNeed], what: str = "this run") -> str:
    """The message a refused run shows: per short drive the same numbers as
    the estimate label, and what to do."""
    lines = "\n".join(f"  {format_drive_need(d)}" for d in short)
    return (f"Not enough disk space for {what}:\n\n{lines}\n\n"
            f"{format_gb(DISK_RESERVE_BYTES)} is kept free on every drive. "
            f"Free disk space, choose another output folder or a smaller "
            f"area / fewer resolutions, then run again.")
