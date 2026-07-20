"""Quantization contract and colour definitions for MIN_ALT results.

The ``MIN_ALT`` propagation model (see AETHER ``solver.rs::OutputMode::MinAltLos``)
emits a 16-bit-per-pixel raster where each pixel is the **lowest altitude**,
above ground level, at which a receiver at that location first gains
line-of-sight to the transmitter.  aether_export writes this as a UInt16
GeoTIFF with:

* ``value * MIN_ALT_STEP_M`` metres AGL (GDAL SCALE tag = 0.5, OFFSET = 0),
* ``MIN_ALT_SENTINEL`` (65535) = never visible / no data.

This module is intentionally **GUI-free and QGIS-free** so it stays unit
testable.  The actual QgsRasterRenderer construction lives in
``result_loader`` (which already depends on ``qgis.core``); everything here is
pure Python that both the loader and the Altitude Explorer dock share.

.. note::
   QGIS' pseudocolour renderer classifies on the *raw* band value, not on the
   GDAL-scaled value.  Every threshold/ramp stop we hand to a renderer is
   therefore expressed in raw u16 units — convert metres with
   :func:`altitude_to_raw` at the boundary and nowhere else.
"""

from __future__ import annotations

from typing import List, Tuple

# ---------------------------------------------------------------------------
# Quantization contract (mirrors solver.rs — do not change independently)
# ---------------------------------------------------------------------------

MIN_ALT_MODEL = "MIN_ALT"

#: Metres of AGL altitude represented by one raw count.
MIN_ALT_STEP_M: float = 0.5

#: Raw value meaning "no line-of-sight at any altitude" / no data.
MIN_ALT_SENTINEL: int = 0xFFFF  # 65535

#: Largest *real* raw altitude value (sentinel - 1).
MIN_ALT_MAX_RAW: int = 0xFFFE  # 65534


def altitude_to_raw(altitude_m: float) -> int:
    """Convert an AGL altitude in metres to the raw u16 band value.

    Clamped to the valid data range ``[0, MIN_ALT_MAX_RAW]`` so a threshold can
    never accidentally collide with the no-data sentinel.
    """
    raw = int(round(altitude_m / MIN_ALT_STEP_M))
    if raw < 0:
        return 0
    if raw > MIN_ALT_MAX_RAW:
        return MIN_ALT_MAX_RAW
    return raw


def raw_to_altitude(raw: int) -> float:
    """Convert a raw u16 band value to AGL altitude in metres.

    The sentinel maps to ``inf`` so callers can treat "unreachable" as an
    altitude larger than anything a slider will produce.
    """
    if raw >= MIN_ALT_SENTINEL:
        return float("inf")
    return raw * MIN_ALT_STEP_M


# ---------------------------------------------------------------------------
# Colour ramp
# ---------------------------------------------------------------------------

# Sequential, colour-blind-safe (RdYlBu reversed): low required altitude — an
# easily reachable location — reads cool/blue; a location that needs to climb
# high before it sees the transmitter reads warm/red.  Anchors are given as
# (fraction-of-scale, (r, g, b)).
_RAMP_ANCHORS: List[Tuple[float, Tuple[int, int, int]]] = [
    (0.00, (44, 123, 182)),    # #2c7bb6 deep blue — reachable at ground level
    (0.25, (171, 217, 233)),   # #abd9e9 light blue
    (0.50, (255, 255, 191)),   # #ffffbf pale yellow
    (0.75, (253, 174, 97)),    # #fdae61 orange
    (1.00, (215, 25, 28)),     # #d7191c red — must climb high to see
]

#: Default upper bound (metres AGL) for the colour ramp when the caller does
#: not derive one from the data.  Most tactical / drone work lives well under
#: this; the ramp saturates (red) above it.
DEFAULT_RAMP_MAX_M: float = 300.0


def _lerp(a: int, b: int, t: float) -> int:
    return int(round(a + (b - a) * t))


def altitude_color(altitude_m: float, max_altitude_m: float) -> Tuple[int, int, int]:
    """Return the ``(r, g, b)`` ramp colour for *altitude_m*.

    *max_altitude_m* is the altitude that maps to the top (red) end of the
    ramp; values above it saturate.
    """
    if max_altitude_m <= 0:
        max_altitude_m = DEFAULT_RAMP_MAX_M
    frac = altitude_m / max_altitude_m
    if frac <= 0.0:
        return _RAMP_ANCHORS[0][1]
    if frac >= 1.0:
        return _RAMP_ANCHORS[-1][1]
    for i in range(1, len(_RAMP_ANCHORS)):
        lo_f, lo_c = _RAMP_ANCHORS[i - 1]
        hi_f, hi_c = _RAMP_ANCHORS[i]
        if frac <= hi_f:
            span = hi_f - lo_f
            t = 0.0 if span == 0 else (frac - lo_f) / span
            return (_lerp(lo_c[0], hi_c[0], t),
                    _lerp(lo_c[1], hi_c[1], t),
                    _lerp(lo_c[2], hi_c[2], t))
    return _RAMP_ANCHORS[-1][1]


def ramp_stops_m(max_altitude_m: float, steps: int = 8) -> List[Tuple[float, Tuple[int, int, int]]]:
    """Build ``[(altitude_m, (r, g, b)), ...]`` ramp stops from 0 to *max_altitude_m*.

    Used both for the continuous default style and to shade the reachable area
    by required altitude in the Altitude Explorer.
    """
    if max_altitude_m <= 0:
        max_altitude_m = DEFAULT_RAMP_MAX_M
    steps = max(2, steps)
    stops: List[Tuple[float, Tuple[int, int, int]]] = []
    for i in range(steps + 1):
        alt = max_altitude_m * i / steps
        stops.append((alt, altitude_color(alt, max_altitude_m)))
    return stops


def nice_ceiling(value_m: float) -> int:
    """Round *value_m* up to a tidy slider/ramp maximum (25 / 50 / 100 / …)."""
    if value_m <= 0:
        return int(DEFAULT_RAMP_MAX_M)
    for step in (10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000):
        if value_m <= step:
            return step
    # Fall back to the next multiple of 5000.
    return int(((value_m // 5000) + 1) * 5000)
