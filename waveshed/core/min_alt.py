"""Quantization contract and colour definitions for MIN_ALT results.

The ``MIN_ALT`` propagation model (see AETHER ``solver.rs::OutputMode::MinAltLos``)
emits a 16-bit-per-pixel raster where each pixel is the **lowest altitude**,
above ground level, at which a receiver at that location first gains
line-of-sight to the transmitter.  aether_export writes this as a UInt16
GeoTIFF with:

* ``value * MIN_ALT_STEP_M`` metres AGL (GDAL SCALE tag = 0.5, OFFSET = 0),
* ``MIN_ALT_SENTINEL`` (65535) = never visible / no data.

The same encoding also carries the **above-sea-level** view of that surface
(:data:`REF_AMSL`): adding the terrain height to every pixel turns "how high
must I climb here" into "what sea-level altitude must I hold here", which is
the number a pilot actually flies.  ``raster_tools.build_amsl_raster`` does the
addition; because the result is quantised identically, every renderer and tool
in this package drives it without knowing which reference it is looking at.

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

from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple, TypeVar

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
# Altitude reference (what the metres are measured from)
# ---------------------------------------------------------------------------

#: Height above the ground directly below — what the solver emits.
REF_AGL = "AGL"

#: Height above mean sea level — the AGL surface plus the terrain under it.
REF_AMSL = "AMSL"

REFERENCES = (REF_AGL, REF_AMSL)


def normalize_reference(reference: Optional[str]) -> str:
    """Return a known reference for *reference*, defaulting to :data:`REF_AGL`.

    Anything unrecognised (including ``None`` and layers stamped before the
    reference existed) reads as AGL — that is what the solver emits, so it is
    the safe assumption for an unlabelled raster.
    """
    ref = str(reference or "").strip().upper()
    return ref if ref in REFERENCES else REF_AGL


def reference_suffix(reference: Optional[str]) -> str:
    """Unit suffix for a spin box / readout, e.g. ``" m AMSL"``."""
    return f" m {normalize_reference(reference)}"


def reference_phrase(reference: Optional[str]) -> str:
    """Plain-language name of *reference*, for prose in the UI."""
    return ("above sea level" if normalize_reference(reference) == REF_AMSL
            else "above ground")


def band_label(altitude_m: float, reference: Optional[str]) -> str:
    """How one altitude is written wherever it is listed: ``"100 m AGL"``.

    The reference is part of the label because a set of altitudes may now mix
    them — ``100`` on its own is a sensible drone height above ground and
    underground for most of the Alps above sea level.
    """
    return f"{altitude_m:g}{reference_suffix(reference)}"


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


#: Ladder of round numbers a slider bound is snapped to.
_STEP_LADDER = (10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)


def nice_floor(value_m: float) -> int:
    """Round *value_m* down to a tidy slider minimum.

    Only AMSL needs this: an AGL surface starts at 0, but a sea-level one
    starts at the valley floor, and a slider that began at 0 would spend most
    of its travel below the terrain — for an alpine site, thousands of metres
    of it.  The step grows with the value so the remaining travel is always
    ~10 steps or more of the chosen grain.
    """
    if value_m <= 0:
        return 0
    for step in _STEP_LADDER:
        if value_m <= step * 10:
            return int(value_m // step) * step
    return int(value_m // 5000) * 5000


# ---------------------------------------------------------------------------
# Altitude bands
# ---------------------------------------------------------------------------

#: Distinct, colour-blind-safe hues (Paul Tol's bright/vibrant sets) for the
#: Altitude Explorer's bands, ordered cool → warm so an untouched set of bands
#: still reads "low = cool, high = warm" like the continuous ramp above.
#: Colours are assigned by band *index* rather than by altitude on purpose:
#: bands are read categorically ("which one is that?"), so adding a band must
#: not repaint the others.
BAND_PALETTE: List[Tuple[int, int, int]] = [
    (68, 119, 170),    # #4477AA blue
    (102, 204, 238),   # #66CCEE cyan
    (34, 136, 51),     # #228833 green
    (204, 187, 68),    # #CCBB44 yellow
    (238, 119, 51),    # #EE7733 orange
    (238, 102, 119),   # #EE6677 rose
    (170, 51, 119),    # #AA3377 purple
    (136, 204, 238),   # #88CCEE pale blue
    (153, 153, 51),    # #999933 olive
    (187, 187, 187),   # #BBBBBB grey
]


def band_color(index: int) -> Tuple[int, int, int]:
    """Default ``(r, g, b)`` for the band at *index* (cycles the palette)."""
    return BAND_PALETTE[index % len(BAND_PALETTE)]


def next_band_color(used: Sequence[Tuple[int, int, int]]) -> Tuple[int, int, int]:
    """First palette colour not in *used*, so a band added after one was
    removed does not come back in a colour already on the map."""
    taken = {tuple(color) for color in used}
    for color in BAND_PALETTE:
        if color not in taken:
            return color
    return band_color(len(taken))


class AltitudeBand(NamedTuple):
    """One altitude the explorer draws, with the colour it draws it in."""

    #: Upper bound of the band, in metres of its own :attr:`reference`.
    altitude_m: float
    #: Fill colour ``(r, g, b)``.
    color: Tuple[int, int, int]
    #: When False the band still splits the ones above it but is not drawn —
    #: hiding "≤ 100 m" leaves a hole rather than handing its area to "≤ 200 m".
    visible: bool = True
    #: What this band's metres are measured from.  Bands of different
    #: references are drawn on different rasters (the original and its
    #: sea-level twin), so this is what :func:`split_bands_by_reference` sorts
    #: them by.  Defaults to AGL — what the solver emits, and what a band
    #: restored from state written before references existed means.
    reference: str = REF_AGL


#: Anything with ``altitude_m`` and ``reference`` — an :class:`AltitudeBand` or
#: the Altitude Explorer's own mutable band object.
_BandLike = TypeVar("_BandLike")


def split_bands_by_reference(
    bands: Sequence[_BandLike],
) -> Dict[str, List[_BandLike]]:
    """Group *bands* by what their altitudes are measured from.

    Altitudes of different references live on different rasters — AGL on the
    result the solver wrote, AMSL on its sea-level twin — so a mixed set of
    bands has to be split before any of it reaches a renderer: nesting 100 m
    AGL inside 2500 m AMSL would paint one surface with the other's rings.

    Every reference is present in the result, with an empty list when no band
    uses it, so a caller can ask for one without guarding.  Each list comes
    back in altitude order, which is the order :func:`band_stops` needs.  A
    band with no (or an unknown) reference reads as AGL.
    """
    grouped: Dict[str, List[_BandLike]] = {ref: [] for ref in REFERENCES}
    for band in bands:
        grouped[normalize_reference(getattr(band, "reference", None))].append(band)
    for group in grouped.values():
        group.sort(key=lambda band: band.altitude_m)
    return grouped


class BandStop(NamedTuple):
    """One stop of a *discrete* colour ramp: everything at or below
    :attr:`raw` — and above the previous stop — takes :attr:`color`."""

    #: Upper bound in raw u16 band units, inclusive.
    raw: int
    #: ``(r, g, b)``, or None for "draw nothing here".
    color: Optional[Tuple[int, int, int]]
    #: Legend text (empty for the transparent tail).
    label: str


def band_stops(
    bands: Sequence[AltitudeBand],
    reference: Optional[str] = REF_AGL,
) -> List[BandStop]:
    """Turn nested altitude *bands* into discrete colour-ramp stops.

    Coverage at altitude *A* is ``{pixels needing <= A}``, so several altitudes
    drawn together are nested rings: the first band owns everything up to its
    altitude, each later one owns only what the one below it did not reach.
    That is exactly a discrete (stepped) ramp keyed on the band's upper bound,
    which is why this returns bounds rather than intervals.

    Bands are sorted by altitude and de-duplicated — two bands at the same
    altitude describe the same ring, and the second would cover nothing.  A
    final transparent stop at the sentinel clips everything above the top band,
    so the renderer never paints the "needs more than you asked for" area.

    Returns an empty list for empty *bands* (the caller falls back to the
    continuous full-range ramp).
    """
    if not bands:
        return []

    suffix = reference_suffix(reference)
    stops: List[BandStop] = []
    seen_raw = set()
    previous_m: Optional[float] = None

    for band in sorted(bands, key=lambda b: b.altitude_m):
        raw = altitude_to_raw(band.altitude_m)
        if raw in seen_raw:
            continue
        seen_raw.add(raw)
        label = (f"≤ {band.altitude_m:g}{suffix}" if previous_m is None
                 else f"{previous_m:g} – {band.altitude_m:g}{suffix}")
        stops.append(BandStop(
            raw, tuple(band.color) if band.visible else None, label,
        ))
        previous_m = band.altitude_m

    # Above the highest band nothing is reachable *at the altitudes asked for*,
    # so it must not inherit the top colour. The stop sits on the sentinel so it
    # also swallows a no-data pixel that arrives without its nodata tag.
    stops.append(BandStop(MIN_ALT_SENTINEL, None, ""))
    return stops
