"""Load AETHER output GeoTIFFs as styled QGIS raster layers.

Provides helpers to create QgsRasterLayer / QgsVectorLayer objects from
AETHER computation outputs and add them to the current QGIS project.
GeoTIFF styling is chosen automatically based on the propagation model
that produced the result.
"""

from __future__ import annotations

import os
from typing import Optional, Sequence, Tuple

from qgis.core import (
    Qgis,
    QgsColorRampShader,
    QgsMessageLog,
    QgsPalettedRasterRenderer,
    QgsProject,
    QgsRasterBandStats,
    QgsRasterLayer,
    QgsRasterShader,
    QgsSingleBandPseudoColorRenderer,
    QgsVectorLayer,
)
from qgis.PyQt.QtGui import QColor

from . import min_alt as _ma
from .job_builder import read_job_terrain_dir
from .layer_utils import (
    altitude_reference,
    mark_aether_model,
    mark_aether_output,
    mark_altitude_reference,
    mark_derived_from,
    mark_terrain_dir,
)

TAG = "Waveshed"

# Layer-tree group paths so AETHER outputs stay organised instead of piling up
# at the top level of the Layers panel.
GROUP_ROOT = "Waveshed"
GROUP_COVERAGE = (GROUP_ROOT, "Coverage")
GROUP_P2P = (GROUP_ROOT, "P2P")
GROUP_TERRAIN = (GROUP_ROOT, "Terrain")
GROUP_MERGE = (GROUP_ROOT, "Best Site")
GROUP_CONTOURS = (GROUP_ROOT, "Contours")
GROUP_AMSL = (GROUP_ROOT, "Above Sea Level")

# Distinct, colour-blind-friendly hues cycled across sites in the best-site map.
_SITE_PALETTE = [
    "#4477AA", "#EE6677", "#228833", "#CCBB44", "#66CCEE",
    "#AA3377", "#BBBBBB", "#EE8866", "#44BB99", "#999933",
]


# ---------------------------------------------------------------------------
# Styling helpers (private)
# ---------------------------------------------------------------------------

def _apply_los_style(layer: QgsRasterLayer) -> None:
    """Apply binary visible/not-visible pseudocolor to a LOS result.

    Value 0 is treated as transparent (no data).
    Value 1 is rendered as green indicating line-of-sight visibility.
    """
    color_ramp = QgsColorRampShader()
    color_ramp.setColorRampType(QgsColorRampShader.Type.Exact)

    visible_color = QColor("#00CC00")
    visible_color.setAlphaF(0.7)

    color_ramp.setColorRampItemList([
        QgsColorRampShader.ColorRampItem(0, QColor(0, 0, 0, 0), "No Data"),
        QgsColorRampShader.ColorRampItem(1, visible_color, "Visible"),
    ])

    shader = QgsRasterShader()
    shader.setRasterShaderFunction(color_ramp)

    renderer = QgsSingleBandPseudoColorRenderer(
        layer.dataProvider(), 1, shader,
    )
    layer.setRenderer(renderer)
    layer.triggerRepaint()


def _apply_signal_strength_style(layer: QgsRasterLayer) -> None:
    """Apply a continuous signal-strength color ramp to a loss-model result.

    Covers the range -140 dBm (red) through -70 dBm (yellow) to 0 dBm
    (green).  Used for both SIMPLE_LOSS and ITM outputs.
    """
    color_ramp = QgsColorRampShader()
    color_ramp.setColorRampType(QgsColorRampShader.Type.Interpolated)

    color_ramp.setColorRampItemList([
        QgsColorRampShader.ColorRampItem(-140, QColor("#FF0000"), "-140 dBm"),
        QgsColorRampShader.ColorRampItem(-70, QColor("#FFFF00"), "-70 dBm"),
        QgsColorRampShader.ColorRampItem(0, QColor("#00FF00"), "0 dBm"),
    ])

    shader = QgsRasterShader()
    shader.setRasterShaderFunction(color_ramp)

    renderer = QgsSingleBandPseudoColorRenderer(
        layer.dataProvider(), 1, shader,
    )
    layer.setRenderer(renderer)
    layer.triggerRepaint()


# ---------------------------------------------------------------------------
# MIN_ALT (minimum-LOS-altitude) styling
# ---------------------------------------------------------------------------

def estimate_altitude_range_m(
    layer: QgsRasterLayer, band: int = 1,
) -> Tuple[float, float]:
    """Return tidy ``(lowest, highest)`` altitudes for ramps/sliders over a
    MIN_ALT layer, derived from band statistics (which exclude the no-data
    sentinel).

    The lower bound only matters above sea level: an AGL surface starts at 0,
    but an AMSL one starts at the valley floor, and a slider anchored at 0
    would spend most of its travel underground.  Falls back to
    ``(0, DEFAULT_RAMP_MAX_M)`` when statistics are unavailable (e.g. an
    all-no-data tile).
    """
    fallback = (0.0, _ma.DEFAULT_RAMP_MAX_M)
    try:
        provider = layer.dataProvider()
        stats = provider.bandStatistics(
            band, QgsRasterBandStats.Stats.Min | QgsRasterBandStats.Stats.Max,
        )
        max_raw = stats.maximumValue
        min_raw = stats.minimumValue
    except Exception:
        return fallback

    if max_raw is None or max_raw <= 0 or max_raw >= _ma.MIN_ALT_SENTINEL:
        return fallback
    highest = float(_ma.nice_ceiling(_ma.raw_to_altitude(int(max_raw))))

    if min_raw is None or min_raw <= 0 or min_raw >= _ma.MIN_ALT_SENTINEL:
        return (0.0, highest)
    lowest = float(_ma.nice_floor(_ma.raw_to_altitude(int(min_raw))))
    # A layer whose whole range rounds into one step would otherwise hand the
    # caller an empty slider.
    return (lowest, highest) if lowest < highest else (0.0, highest)


def estimate_max_altitude_m(layer: QgsRasterLayer, band: int = 1) -> float:
    """Return a tidy upper altitude (metres) for ramps/sliders on a MIN_ALT
    layer — the top of :func:`estimate_altitude_range_m`."""
    return estimate_altitude_range_m(layer, band)[1]


def build_min_alt_renderer(
    provider,
    band: int = 1,
    threshold_m: Optional[float] = None,
    shade: bool = True,
    max_ramp_m: Optional[float] = None,
    alpha: float = 0.75,
    min_ramp_m: float = 0.0,
    reference: str = _ma.REF_AGL,
) -> QgsSingleBandPseudoColorRenderer:
    """Build a pseudocolour renderer for a MIN_ALT raster.

    Parameters
    ----------
    threshold_m:
        When set, only pixels reachable at/below this altitude are drawn;
        everything above — and the no-data sentinel — is clipped to
        transparent.  ``None`` renders the whole raster (continuous ramp).
    shade:
        When *True*, drawn pixels are coloured by their required altitude so the
        climb needed is visible at a glance.  When *False* they get a single
        flat colour — a clean "reachable / not" coverage mask.
    max_ramp_m:
        Altitude mapped to the top (red) end of the ramp.  Defaults to
        *threshold_m* in threshold mode, else :data:`min_alt.DEFAULT_RAMP_MAX_M`.
    min_ramp_m:
        Altitude mapped to the bottom (blue) end.  0 for an AGL surface; above
        sea level it is the valley floor, without which every colour in an
        alpine scene would be squeezed into the top of the ramp.
    reference:
        ``"AGL"``/``"AMSL"`` — legend labels only.

    Notes
    -----
    All ramp/threshold values are converted to **raw u16 units** because QGIS
    classifies on the unscaled band value (see :mod:`.min_alt`).
    """
    ramp = QgsColorRampShader()
    ramp.setColorRampType(QgsColorRampShader.Type.Interpolated)

    if max_ramp_m is None:
        max_ramp_m = threshold_m if threshold_m else _ma.DEFAULT_RAMP_MAX_M
    if not max_ramp_m or max_ramp_m <= 0:
        max_ramp_m = _ma.DEFAULT_RAMP_MAX_M

    # Colours span min_ramp_m..max_ramp_m so they stay comparable across layers
    # even when the threshold is lower; the *coloured* stops themselves only run
    # up to the threshold (in threshold mode) so nothing above it is ever drawn.
    top_m = threshold_m if threshold_m is not None else max_ramp_m
    base_m = min(min_ramp_m, top_m)
    span_m = max(max_ramp_m - base_m, _ma.MIN_ALT_STEP_M)
    suffix = _ma.reference_suffix(reference)

    def _color_for(alt_m: float) -> "QColor":
        if shade:
            r, g, b = _ma.altitude_color(alt_m - base_m, span_m)
        else:
            r, g, b = (0, 204, 0)
        c = QColor(r, g, b)
        c.setAlphaF(alpha)
        return c

    items = []
    seen_raw = set()
    steps = 8
    for i in range(steps + 1):
        alt_m = base_m + (top_m - base_m) * i / steps
        raw = _ma.altitude_to_raw(alt_m)
        if raw in seen_raw:
            continue  # avoid duplicate stops when top_m is tiny
        seen_raw.add(raw)
        items.append(QgsColorRampShader.ColorRampItem(
            raw, _color_for(alt_m),
            "Reachable" if not shade else f"{alt_m:.0f}{suffix}",
        ))

    if threshold_m is not None:
        # Everything above the chosen altitude (and, via nodata, the sentinel)
        # renders transparent — this is what enforces the "reachable at ≤ X" mask
        # independently of the colour scale above.
        transparent = QColor(0, 0, 0, 0)
        cutoff_raw = _ma.altitude_to_raw(threshold_m) + 1
        if cutoff_raw not in seen_raw:
            items.append(QgsColorRampShader.ColorRampItem(
                cutoff_raw, transparent, "",
            ))

    ramp.setColorRampItemList(items)
    # Interpolated with no clip: pixels below 0 take the first colour and — in
    # full-range mode — pixels above the ramp saturate at red; in threshold mode
    # the explicit transparent stop already hides everything above the cutoff.
    ramp.setClip(False)

    shader = QgsRasterShader()
    shader.setRasterShaderFunction(ramp)
    return QgsSingleBandPseudoColorRenderer(provider, band, shader)


def build_band_renderer(
    provider,
    band: int = 1,
    bands: Sequence[_ma.AltitudeBand] = (),
    reference: str = _ma.REF_AGL,
    alpha: float = 0.75,
) -> QgsSingleBandPseudoColorRenderer:
    """Build a renderer that paints several altitudes at once, each its own
    colour.

    Coverage at altitude *A* contains coverage at every lower altitude, so a set
    of altitudes draws as nested rings — "reachable by 100 m", "needs 100–200 m",
    and so on — which is a *discrete* (stepped) ramp keyed on each band's upper
    bound.  :func:`min_alt.band_stops` works out the bounds and the legend text;
    this only turns them into QGIS objects.

    Anything above the highest band is transparent, so the map answers "where
    can I get to at these altitudes" without a legend lookup.
    """
    ramp = QgsColorRampShader()
    ramp.setColorRampType(QgsColorRampShader.Type.Discrete)

    items = []
    for stop in _ma.band_stops(bands, reference):
        if stop.color is None:
            color = QColor(0, 0, 0, 0)
        else:
            color = QColor(*stop.color)
            color.setAlphaF(alpha)
        items.append(QgsColorRampShader.ColorRampItem(
            stop.raw, color, stop.label,
        ))

    ramp.setColorRampItemList(items)
    ramp.setClip(False)
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(ramp)
    return QgsSingleBandPseudoColorRenderer(provider, band, shader)


def _apply_min_alt_style(layer: QgsRasterLayer) -> None:
    """Apply the default continuous minimum-altitude ramp to a MIN_ALT result.

    Pixels are coloured by the lowest altitude at which they gain LOS to the
    transmitter (blue = reachable near the ground, red = must climb high). The
    no-data sentinel is already transparent via the GeoTIFF's nodata tag.
    """
    min_ramp_m, max_ramp_m = estimate_altitude_range_m(layer)
    renderer = build_min_alt_renderer(
        layer.dataProvider(), 1, threshold_m=None, shade=True,
        max_ramp_m=max_ramp_m, min_ramp_m=min_ramp_m,
        reference=altitude_reference(layer),
    )
    layer.setRenderer(renderer)
    layer.triggerRepaint()


# ---------------------------------------------------------------------------
# Best-site + contour result loaders (Options E and D)
# ---------------------------------------------------------------------------

def load_best_alt_result(
    tif_path: str, display_name: str = "Best required altitude",
) -> QgsRasterLayer:
    """Load a merged *best required altitude* raster as a MIN_ALT layer.

    Styled with the minimum-altitude ramp and tagged as MIN_ALT so the Altitude
    Explorer drives it exactly like a single-site result.
    """
    return load_coverage_result(tif_path, "MIN_ALT", display_name)


def load_amsl_result(
    tif_path: str,
    display_name: str,
    source_layer_id: str = "",
    terrain_dir: str = "",
) -> QgsRasterLayer:
    """Load an above-sea-level twin (see ``raster_tools.build_amsl_raster``).

    It carries the MIN_ALT encoding, so it is tagged as one: the Altitude
    Explorer, the contour tool and the best-site merge then drive it exactly
    like the AGL original.  The extra stamps record what it is measured from
    and which layer it came from, so the explorer can find it again — including
    in a later session, since custom properties survive a project save.
    """
    layer = load_coverage_result(tif_path, _ma.MIN_ALT_MODEL, display_name)
    mark_altitude_reference(layer, _ma.REF_AMSL)
    mark_derived_from(layer, source_layer_id)
    if terrain_dir:
        mark_terrain_dir(layer, terrain_dir)
    # Re-style now that it is stamped: the ramp spans the valley floor to the
    # highest required altitude rather than starting at sea level.
    _apply_min_alt_style(layer)
    return layer


def load_best_site_result(
    tif_path: str,
    labels: list,
    display_name: str = "Best site",
) -> QgsRasterLayer:
    """Load a best-site index raster with a categorical, per-site palette.

    *labels* maps pixel value ``i`` to a human-readable site name.
    """
    if not os.path.isfile(tif_path):
        raise ValueError(f"GeoTIFF not found: {tif_path}")

    layer = QgsRasterLayer(tif_path, display_name)
    if not layer.isValid():
        raise ValueError(f"Failed to create a valid raster layer from {tif_path}")

    classes = []
    for i, label in enumerate(labels):
        color = QColor(_SITE_PALETTE[i % len(_SITE_PALETTE)])
        color.setAlphaF(0.8)
        classes.append(QgsPalettedRasterRenderer.Class(i, color, str(label)))

    renderer = QgsPalettedRasterRenderer(layer.dataProvider(), 1, classes)
    layer.setRenderer(renderer)
    layer.triggerRepaint()
    return layer


#: Legend classes for a contour layer. Contours carry their value in the label;
#: the colour only has to read as a low-to-high gradient, and a legend longer
#: than this stops being one.
_MAX_CONTOUR_CLASSES = 8

#: Above this many features, labelling every one of them on every repaint makes
#: the canvas crawl, so labels wait until the user has zoomed in.
_LABEL_ALWAYS_MAX_FEATURES = 5000

#: Map scale (denominator) below which labels appear on a dense contour layer.
_LABEL_MIN_SCALE = 150000


def load_contour_result(
    gpkg_path: str,
    display_name: str = "Iso-altitude contours",
    levels_m: Optional[list] = None,
) -> QgsVectorLayer:
    """Load an iso-altitude contour GeoPackage with graduated colours + labels.

    Lines are coloured by required altitude and labelled in metres so the map
    reads "fly above N m to clear this ridge" at a glance.

    *levels_m* is the level list :func:`raster_tools.generate_contours` reports;
    passing it lets the legend be built without reading the features.
    """
    if not os.path.isfile(gpkg_path):
        raise ValueError(f"Contour file not found: {gpkg_path}")

    uri = f"{gpkg_path}|layername=contours"
    layer = QgsVectorLayer(uri, display_name, "ogr")
    if not layer.isValid():
        raise ValueError(f"Failed to load contour layer from {gpkg_path}")

    _style_contours(layer, levels_m)
    return layer


def _set_enum(obj, attr: str, *candidates) -> None:
    """Assign the first *candidate* the binding accepts for ``obj.attr``.

    QGIS moved several labelling enums out of ``QgsPalLayerSettings`` /
    ``QgsUnitTypes`` into the ``Qgis`` namespace across the 3.x line.  The old
    names survive as aliases, but SIP does not always accept an alias where the
    new enum type is expected — so try the modern spelling, then the old one.

    This guards the enum *value* only.  A SIP wrapper has a real ``__dict__``,
    so assigning an attribute the class no longer has succeeds silently instead
    of raising — this cannot tell you that ``attr`` itself has gone away.
    """
    for value in candidates:
        if value is None:
            continue
        try:
            setattr(obj, attr, value)
            return
        except (TypeError, ValueError):
            continue


def _min_alt_color_ramp(top_m: float):
    """A QGIS gradient ramp matching :func:`min_alt.altitude_color`.

    Attached to the renderer purely so re-classifying the layer by hand in Layer
    Properties keeps the plugin's colours instead of jumping to a QGIS default.
    """
    from qgis.core import QgsGradientColorRamp, QgsGradientStop

    stops = []
    for i in range(1, 8):
        frac = i / 8.0
        stops.append(QgsGradientStop(
            frac, QColor(*_ma.altitude_color(frac * top_m, top_m)),
        ))
    return QgsGradientColorRamp(
        QColor(*_ma.altitude_color(0.0, top_m)),
        QColor(*_ma.altitude_color(top_m, top_m)),
        False, stops,
    )


def _contour_levels_from_layer(layer: QgsVectorLayer) -> list:
    """Fall back to ~``_MAX_CONTOUR_CLASSES`` levels spanning the layer's own
    ``alt_m`` range, for contour files loaded without their generated levels."""
    idx = layer.fields().indexOf("alt_m")
    if idx < 0:
        return []
    try:
        lo = float(layer.minimumValue(idx))
        hi = float(layer.maximumValue(idx))
    except (TypeError, ValueError):
        return []
    if not hi > lo:
        return [hi] if hi > 0 else []
    step = (hi - lo) / _MAX_CONTOUR_CLASSES
    return [lo + i * step for i in range(_MAX_CONTOUR_CLASSES + 1)]


def _contour_classes(levels: list) -> list:
    """Group ascending *levels* (metres AGL) into at most
    :data:`_MAX_CONTOUR_CLASSES` legend classes.

    Returns ``[(lower, upper, label, (r, g, b)), ...]``.  Kept free of QGIS
    types so the bound arithmetic stays unit-testable — it decides whether a
    contour is drawn at all, since a value matching no class renders as nothing
    rather than falling back to a default colour.
    """
    top_m = levels[-1] if levels[-1] > 0 else _ma.DEFAULT_RAMP_MAX_M
    n_classes = min(len(levels), _MAX_CONTOUR_CLASSES)

    chunks = []
    for i in range(n_classes):
        chunk = levels[(i * len(levels)) // n_classes:
                       ((i + 1) * len(levels)) // n_classes]
        if chunk:
            chunks.append(chunk)

    # Half a level's spacing, so the outermost classes reach just past the data
    # rather than clipping whatever sits exactly on the end.
    spacing = ((levels[-1] - levels[0]) / (len(levels) - 1)
               if len(levels) > 1 else max(levels[-1], 1.0))

    classes = []
    for i, chunk in enumerate(chunks):
        # QgsGraduatedSymbolRenderer matches EVERY range as [lower, upper] —
        # inclusive at both ends — and takes the first hit in list order. So
        # chaining each lower to the previous upper leaves no gap for an
        # off-level value to fall through, and the single-value overlap at each
        # boundary resolves to the lower class, which is what its label says.
        lower = chunk[0] - spacing / 2.0 if i == 0 else chunks[i - 1][-1]
        upper = chunk[-1] + spacing / 2.0 if i == len(chunks) - 1 else chunk[-1]
        label = (f"{chunk[0]:g} m" if len(chunk) == 1
                 else f"{chunk[0]:g}–{chunk[-1]:g} m")
        color = _ma.altitude_color((chunk[0] + chunk[-1]) / 2.0, top_m)
        classes.append((lower, upper, label, color))
    return classes


def _contour_renderer(levels: list):
    """Build a graduated renderer over *levels* (ascending metres AGL).

    Deliberately **not** ``QgsGraduatedSymbolRenderer.createRenderer``: that
    helper calls ``ramp->clone()`` with no null check, so passing ``None`` for
    the colour ramp crashes QGIS outright with an access violation — and its
    classification modes (Jenks especially) read every feature's value off disk
    on the GUI thread, which on a large contour set costs seconds to minutes.
    The levels are known exactly, so the classes are just built from them.
    """
    from qgis.core import (
        QgsGraduatedSymbolRenderer,
        QgsLineSymbol,
        QgsRendererRange,
    )

    ranges = []
    for lower, upper, label, (r, g, b) in _contour_classes(levels):
        # QgsRendererRange takes ownership of its symbol, so each range needs
        # its own — sharing one would double-free it.
        symbol = QgsLineSymbol.createSimple({"line_width": "0.4"})
        symbol.setColor(QColor(r, g, b))
        ranges.append(QgsRendererRange(lower, upper, symbol, label))

    top_m = levels[-1] if levels[-1] > 0 else _ma.DEFAULT_RAMP_MAX_M
    renderer = QgsGraduatedSymbolRenderer("alt_m", ranges)
    renderer.setSourceSymbol(QgsLineSymbol.createSimple({"line_width": "0.4"}))
    renderer.setSourceColorRamp(_min_alt_color_ramp(top_m))
    return renderer


def _contour_labeling(layer: QgsVectorLayer):
    """Altitude labels tuned for line features that come in the thousands."""
    from qgis.core import (
        QgsPalLayerSettings,
        QgsTextBufferSettings,
        QgsTextFormat,
        QgsUnitTypes,
        QgsVectorLayerSimpleLabeling,
    )

    settings = QgsPalLayerSettings()
    settings.fieldName = "concat(format_number(\"alt_m\", 0), ' m')"
    settings.isExpression = True
    _set_enum(
        settings, "placement",
        getattr(getattr(Qgis, "LabelPlacement", None), "Line", None),
        getattr(QgsPalLayerSettings, "Line", None),
    )

    # Merge connected segments so a contour is labelled once end-to-end.
    # ContourGenerateEx emits a separate feature per traced run, so without this
    # a single ridge line can carry dozens of identical labels — and every one
    # of them is another candidate for PAL's conflict solver to place.
    line_settings = settings.lineSettings()
    line_settings.setMergeLines(True)
    settings.setLineSettings(line_settings)

    # Repeat in page units so long contours stay readable at any zoom without
    # depending on whether the layer CRS is metres or degrees.
    settings.repeatDistance = 40
    _set_enum(
        settings, "repeatDistanceUnit",
        getattr(getattr(Qgis, "RenderUnit", None), "Millimeters", None),
        getattr(QgsUnitTypes, "RenderMillimeters", None),
    )
    # Contours are a backdrop: they should not shove other layers' labels aside,
    # and testing thousands of them as obstacles is itself expensive. This has
    # to go through obstacleSettings() — the old `settings.obstacle` was a SIP
    # property that QGIS 3.36 removed, and assigning it does not fail, it just
    # parks a dead attribute on the wrapper while the C++ default (obstacle =
    # true) quietly stands.
    settings.obstacleSettings().setIsObstacle(False)

    text_format = QgsTextFormat()
    text_format.setSize(8)
    buffer_settings = QgsTextBufferSettings()
    buffer_settings.setEnabled(True)
    buffer_settings.setSize(0.8)
    buffer_settings.setColor(QColor(255, 255, 255, 220))
    text_format.setBuffer(buffer_settings)
    settings.setFormat(text_format)

    if layer.featureCount() > _LABEL_ALWAYS_MAX_FEATURES:
        # Zoomed out, these labels would all collide anyway — PAL would burn the
        # placement pass to draw a smear. Show them once the map is close in.
        settings.scaleVisibility = True
        settings.minimumScale = _LABEL_MIN_SCALE
        settings.maximumScale = 0

    return QgsVectorLayerSimpleLabeling(settings)


def _style_contours(layer: QgsVectorLayer, levels_m: Optional[list] = None) -> None:
    """Graduated line colours by ``alt_m`` plus altitude labels."""
    levels = sorted({float(v) for v in (levels_m or [])})
    if not levels:
        levels = _contour_levels_from_layer(layer)

    if levels:
        try:
            layer.setRenderer(_contour_renderer(levels))
        except Exception:  # noqa: BLE001 — keep the default single-symbol style
            QgsMessageLog.logMessage(
                "Contour colours could not be applied; using the default style",
                TAG, Qgis.MessageLevel.Warning,
            )

    # Labels are the point of a contour layer, but never worth losing the layer
    # over — a version-shifted labelling API must not fail the whole load.
    try:
        layer.setLabeling(_contour_labeling(layer))
        layer.setLabelsEnabled(True)
    except Exception:  # noqa: BLE001
        QgsMessageLog.logMessage(
            "Contour labels could not be applied; lines are drawn unlabelled",
            TAG, Qgis.MessageLevel.Warning,
        )

    # No render-simplification block here on purpose: QGIS already defaults a
    # vector layer to GeometrySimplification at the map-to-pixel threshold, and
    # the renderer prefers the render context's method over the layer's anyway,
    # so setting it per layer would only restate the default.
    layer.triggerRepaint()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_coverage_result(
    tif_path: str,
    model: str,
    display_name: Optional[str] = None,
) -> QgsRasterLayer:
    """Create a styled QgsRasterLayer from an AETHER coverage GeoTIFF.

    Parameters
    ----------
    tif_path:
        Absolute path to the output ``.tif`` file produced by
        ``aether_export``.
    model:
        Propagation model that generated the result.  One of ``"LOS"``,
        ``"SIMPLE_LOSS"``, or ``"ITM"``.  Determines the colour scheme
        applied to the layer.
    display_name:
        Human-readable name shown in the QGIS Layers panel.  Defaults to
        the filename stem when *None*.

    Returns
    -------
    QgsRasterLayer
        The layer with model-appropriate styling already applied.

    Raises
    ------
    ValueError
        If *tif_path* does not exist or the resulting layer is invalid.
    """
    if not os.path.isfile(tif_path):
        raise ValueError(f"GeoTIFF not found: {tif_path}")

    if display_name is None:
        display_name = os.path.splitext(os.path.basename(tif_path))[0]

    layer = QgsRasterLayer(tif_path, display_name)

    if not layer.isValid():
        raise ValueError(
            f"Failed to create a valid raster layer from {tif_path}"
        )

    model_upper = model.upper()
    if model_upper == "LOS":
        _apply_los_style(layer)
    elif model_upper in ("SIMPLE_LOSS", "ITM"):
        _apply_signal_strength_style(layer)
    elif model_upper == _ma.MIN_ALT_MODEL:
        _apply_min_alt_style(layer)
    else:
        QgsMessageLog.logMessage(
            f"Unknown model '{model}' — layer loaded without styling",
            TAG,
            Qgis.MessageLevel.Warning,
        )

    # Record the model so tools like the Altitude Explorer can recognise the
    # layers they know how to drive (survives project save/reload).
    mark_aether_model(layer, model_upper)
    if model_upper == _ma.MIN_ALT_MODEL:
        # What the solver emits. load_amsl_result overrides this on its twin.
        mark_altitude_reference(layer, _ma.REF_AGL)
        # The run wrote its job config beside the GeoTIFF, and that config names
        # the terrain the run used. Reading it here is what lets the explorer
        # offer the above-sea-level view without asking the user to re-identify
        # a DEM that would have to match the run's to the metre.
        mark_terrain_dir(layer, read_job_terrain_dir(tif_path) or "")

    return layer


def load_p2p_result_csv(csv_path: str) -> QgsVectorLayer:
    """Load a point-to-point batch result CSV as a QGIS table layer.

    The CSV is expected to contain columns such as ``Source_ID``,
    ``Target_ID``, ``Signal_dBm``, and ``Path_Loss_dB``.  No geometry is
    created — the layer appears as an attribute table only.

    Parameters
    ----------
    csv_path:
        Absolute path to the CSV file.

    Returns
    -------
    QgsVectorLayer
        A non-spatial vector layer backed by the CSV.

    Raises
    ------
    ValueError
        If *csv_path* does not exist or the resulting layer is invalid.
    """
    if not os.path.isfile(csv_path):
        raise ValueError(f"CSV file not found: {csv_path}")

    # Normalise to forward slashes for the QGIS URI (works on all platforms).
    normalised = csv_path.replace("\\", "/")
    uri = f"file:///{normalised}?delimiter=,&type=csv&detectTypes=yes"

    display_name = os.path.splitext(os.path.basename(csv_path))[0]
    layer = QgsVectorLayer(uri, display_name, "delimitedtext")

    if not layer.isValid():
        raise ValueError(
            f"Failed to create a valid vector layer from {csv_path}"
        )

    return layer


def _ensure_layer_group(*names: str):
    """Return the nested layer-tree group for *names*, creating it if needed.

    ``_ensure_layer_group("Waveshed", "Coverage")`` returns the ``Coverage``
    group nested under a top-level ``Waveshed`` group, creating either level
    that does not yet exist. With no names, returns the tree root.

    The top-level Waveshed group is inserted at the *top* of the Layers panel
    (index 0) so results sit above basemaps/DEMs instead of being buried at
    the bottom. Nested groups are appended in creation order.
    """
    root = QgsProject.instance().layerTreeRoot()
    node = root
    for name in names:
        child = node.findGroup(name)
        if child is None:
            if node is root:
                child = node.insertGroup(0, name)
            else:
                child = node.addGroup(name)
        node = child
    return node


def add_layer_to_project(
    layer: QgsRasterLayer | QgsVectorLayer,
    group_path: Optional[tuple[str, ...]] = None,
) -> None:
    """Add a layer to the current QGIS project after a validity check.

    Parameters
    ----------
    layer:
        A QgsRasterLayer or QgsVectorLayer to register with the project.
    group_path:
        Optional tuple of nested layer-tree group names to place the layer
        under (e.g. ``("Waveshed", "Coverage")``). The groups are created if
        absent. When ``None`` the layer is added at the top level, preserving
        the previous behaviour.

    Raises
    ------
    ValueError
        If the layer is not valid.
    """
    if not layer.isValid():
        raise ValueError(
            f"Cannot add invalid layer '{layer.name()}' to the project"
        )

    # Stamp it as plugin-created (with its category role) so the DEM source
    # pickers can hide result rasters while keeping terrain we generated.
    role = group_path[1].lower() if group_path and len(group_path) >= 2 else ""
    mark_aether_output(layer, role)

    if not group_path:
        QgsProject.instance().addMapLayer(layer)
        return

    # Register the layer without auto-adding a top-level tree node, then
    # attach it under the requested group so the tree stays organised.
    QgsProject.instance().addMapLayer(layer, False)
    _ensure_layer_group(*group_path).addLayer(layer)
