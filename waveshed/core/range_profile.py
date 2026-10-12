"""Variable radial step ("adaptive resolution") for coverage runs.

Only the near field of a coverage map needs the fine resolution; further out a
coarser step loses little and costs much less. aether_core supports that through
``analysis.range_profile`` (engine >= :data:`RANGE_PROFILE_MIN_ENGINE`): a list
of bands ``{"until_m", "step_m"}``, each radial one continuous ray sampled at
each band's own step — so the far field keeps the horizon the fine near field
produced (toolkit CONTRACT.md 3.3).

An adaptive site is ONE job: the profile, with ``resolution_m`` the chosen
(base) resolution — the engine's output pixel size. The map is written at that
pixel size everywhere; in the coarse bands neighbouring pixels share the value of
their (coarser) radial sample.

Band steps are taken from :data:`job_builder.VALID_RESOLUTIONS` so every band has
.abt tiles of its own, and only steps whose tile extents nest with the base's
are used: aether_core's terrain page table needs every tile extent to be an
integer multiple of the smallest (2 m tiles are 0.1 deg, 5 m / 10 m are 0.25 deg,
which do not nest).

No GUI code — the Site Analysis tab and its worker both build on it.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from .job_builder import VALID_RESOLUTIONS
from .terrain_adapter import ABT_EXTENT_DEG

#: First engine that understands ``analysis.range_profile``. Older engines
#: ignore the unknown field and would silently run the job with one uniform
#: step, so the worker refuses to start an adaptive run on them.
RANGE_PROFILE_MIN_ENGINE = "0.4.9"

#: The modes the Site Analysis resolution row offers.
MODE_FIXED = "fixed"
MODE_ADAPTIVE = "adaptive"
MODE_CUSTOM = "custom"

#: "Adaptive": the chosen resolution out to here ...
ADAPTIVE_NEAR_M = 7000.0
#: ... then steps up through the ladder over this distance ...
ADAPTIVE_RAMP_M = 5000.0
#: ... to this step, kept to the end of the range.
ADAPTIVE_FAR_STEP_M = 90

#: aether_core accepts at most this many bands.
MAX_BANDS = 8

#: One band: (outer edge in metres from the TX, step in metres).
Band = Tuple[float, int]


def extents_nest(steps: Sequence[int]) -> bool:
    """True when the .abt tile extents of *steps* nest (each an integer
    multiple of the smallest), which aether_core's terrain grid requires."""
    extents = [ABT_EXTENT_DEG[s] for s in set(steps)]
    smallest = min(extents)
    return all(abs(e / smallest - round(e / smallest)) < 1e-9 for e in extents)


def ladder_for(base_m: int) -> List[int]:
    """Resolutions a profile starting at *base_m* may step through: coarser
    than the base and with tile extents that nest with it."""
    return [r for r in VALID_RESOLUTIONS
            if r > base_m and extents_nest([base_m, r])]


def adaptive_bands(base_m: int, max_range_km: float,
                   near_m: float = ADAPTIVE_NEAR_M,
                   ramp_m: float = ADAPTIVE_RAMP_M,
                   far_step_m: int = ADAPTIVE_FAR_STEP_M) -> Optional[List[Band]]:
    """The "Adaptive" profile for *base_m*, or None when it would not differ
    from a fixed run (the base is already as coarse as the far step, or the
    range ends inside the near field).

    The base resolution holds to *near_m*; the ladder steps between the base
    and *far_step_m* share the next *ramp_m* evenly; *far_step_m* covers the
    rest. 5 m with defaults: 5 m to 7 km, 10 m to 9.5 km, 30 m to 12 km,
    90 m beyond.
    """
    range_m = max_range_km * 1000.0
    if base_m >= far_step_m or range_m <= near_m:
        return None
    ladder = ladder_for(base_m)
    if far_step_m not in ladder:
        return None
    ramp = [r for r in ladder if r < far_step_m]
    bands: List[Band] = [(near_m, base_m)]
    for k, step in enumerate(ramp, 1):
        bands.append((near_m + ramp_m * k / len(ramp), step))
    bands.append((range_m, far_step_m))
    return normalise_bands(bands, max_range_km)


def normalise_bands(bands: Sequence[Band], max_range_km: float) -> List[Band]:
    """Clip *bands* to the range: drop bands that start beyond it, end the last
    one exactly on it, and merge neighbours with the same step."""
    range_m = max_range_km * 1000.0
    out: List[Band] = []
    start = 0.0
    for until, step in bands:  # in order: a band clipped to the range ends the list
        if start >= range_m:
            break
        until = min(float(until), range_m)
        if until <= start:
            continue
        if out and out[-1][1] == step:
            out[-1] = (until, step)
        else:
            out.append((until, int(step)))
        start = until
    if out and out[-1][0] < range_m:
        out[-1] = (range_m, out[-1][1])
    return out


def validate_bands(bands: Sequence[Band], max_range_km: float) -> Optional[str]:
    """Why *bands* cannot be run, or None when they can."""
    if not bands:
        return "The resolution profile is empty."
    if len(bands) > MAX_BANDS:
        return f"At most {MAX_BANDS} resolution bands are supported (got {len(bands)})."
    steps = [s for _, s in bands]
    bad = [s for s in steps if s not in VALID_RESOLUTIONS]
    if bad:
        return f"Band resolutions must be one of {VALID_RESOLUTIONS} m (got {bad})."
    if steps != sorted(steps):
        return "Resolution must not get finer with distance."
    if not extents_nest(steps):
        return ("These resolutions cannot be combined: 2 m terrain tiles do not "
                "line up with 5 m / 10 m ones. Use 30, 90 or 250 m after 2 m.")
    edges = [u for u, _ in bands]
    if any(b <= a for a, b in zip(edges, edges[1:])):
        return "Band edges must increase with distance."
    if abs(edges[-1] - max_range_km * 1000.0) > 1.0:
        return "The last band must end at the analysis range."
    return None


def profile_json(bands: Sequence[Band]) -> List[dict]:
    """``analysis.range_profile`` for *bands*."""
    return [{"until_m": float(u), "step_m": float(s)} for u, s in bands]


def describe(bands: Sequence[Band]) -> str:
    """One line for the GUI: ``5 m to 7 km · 10 m to 9.5 km · 90 m beyond``."""
    if not bands:
        return ""
    parts = [f"{s} m to {u / 1000:g} km" for u, s in bands[:-1]]
    parts.append(f"{bands[-1][1]} m beyond" if len(bands) > 1
                 else f"{bands[-1][1]} m")
    return " · ".join(parts)
