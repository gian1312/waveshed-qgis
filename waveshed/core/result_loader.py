"""Load AETHER output GeoTIFFs as styled QGIS raster layers.

Provides helpers to create QgsRasterLayer / QgsVectorLayer objects from
AETHER computation outputs and add them to the current QGIS project.
GeoTIFF styling is chosen automatically based on the propagation model
that produced the result.
"""

from __future__ import annotations

import os
from typing import Optional

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
from .layer_utils import mark_aether_model, mark_aether_output

TAG = "Waveshed"

# Layer-tree group paths so AETHER outputs stay organised instead of piling up
# at the top level of the Layers panel.
GROUP_ROOT = "Waveshed"
GROUP_COVERAGE = (GROUP_ROOT, "Coverage")
GROUP_P2P = (GROUP_ROOT, "P2P")
GROUP_TERRAIN = (GROUP_ROOT, "Terrain")
GROUP_MERGE = (GROUP_ROOT, "Best Site")
GROUP_CONTOURS = (GROUP_ROOT, "Contours")

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
    color_ramp.setColorRampType(QgsColorRampShader.Exact)

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
    color_ramp.setColorRampType(QgsColorRampShader.Interpolated)

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

def estimate_max_altitude_m(layer: QgsRasterLayer, band: int = 1) -> float:
    """Return a tidy upper altitude (metres AGL) for ramps/sliders on a MIN_ALT
    layer, derived from band statistics (which exclude the no-data sentinel).

    Falls back to :data:`min_alt.DEFAULT_RAMP_MAX_M` when statistics are
    unavailable (e.g. an all-no-data tile).
    """
    try:
        provider = layer.dataProvider()
        stats = provider.bandStatistics(band, QgsRasterBandStats.Max)
        max_raw = stats.maximumValue
        if max_raw is None or max_raw <= 0 or max_raw >= _ma.MIN_ALT_SENTINEL:
            return _ma.DEFAULT_RAMP_MAX_M
        return float(_ma.nice_ceiling(_ma.raw_to_altitude(int(max_raw))))
    except Exception:
        return _ma.DEFAULT_RAMP_MAX_M


def build_min_alt_renderer(
    provider,
    band: int = 1,
    threshold_m: Optional[float] = None,
    shade: bool = True,
    max_ramp_m: Optional[float] = None,
    alpha: float = 0.75,
) -> QgsSingleBandPseudoColorRenderer:
    """Build a pseudocolour renderer for a MIN_ALT raster.

    Parameters
    ----------
    threshold_m:
        When set, only pixels reachable at/below this altitude (metres AGL) are
        drawn; everything above — and the no-data sentinel — is clipped to
        transparent.  ``None`` renders the whole raster (continuous ramp).
    shade:
        When *True*, drawn pixels are coloured by their required altitude so the
        climb needed is visible at a glance.  When *False* they get a single
        flat colour — a clean "reachable / not" coverage mask.
    max_ramp_m:
        Altitude mapped to the top (red) end of the ramp.  Defaults to
        *threshold_m* in threshold mode, else :data:`min_alt.DEFAULT_RAMP_MAX_M`.

    Notes
    -----
    All ramp/threshold values are converted to **raw u16 units** because QGIS
    classifies on the unscaled band value (see :mod:`.min_alt`).
    """
    ramp = QgsColorRampShader()
    ramp.setColorRampType(QgsColorRampShader.Interpolated)

    if max_ramp_m is None:
        max_ramp_m = threshold_m if threshold_m else _ma.DEFAULT_RAMP_MAX_M
    if not max_ramp_m or max_ramp_m <= 0:
        max_ramp_m = _ma.DEFAULT_RAMP_MAX_M

    # Colours span 0..max_ramp_m so they stay comparable across layers even when
    # the threshold is lower; the *coloured* stops themselves only run up to the
    # threshold (in threshold mode) so nothing above it is ever drawn.
    top_m = threshold_m if threshold_m is not None else max_ramp_m

    def _color_for(alt_m: float) -> "QColor":
        if shade:
            r, g, b = _ma.altitude_color(alt_m, max_ramp_m)
        else:
            r, g, b = (0, 204, 0)
        c = QColor(r, g, b)
        c.setAlphaF(alpha)
        return c

    items = []
    seen_raw = set()
    steps = 8
    for i in range(steps + 1):
        alt_m = top_m * i / steps
        raw = _ma.altitude_to_raw(alt_m)
        if raw in seen_raw:
            continue  # avoid duplicate stops when top_m is tiny
        seen_raw.add(raw)
        items.append(QgsColorRampShader.ColorRampItem(
            raw, _color_for(alt_m),
            "Reachable" if not shade else f"{alt_m:.0f} m",
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


def _apply_min_alt_style(layer: QgsRasterLayer) -> None:
    """Apply the default continuous minimum-altitude ramp to a MIN_ALT result.

    Pixels are coloured by the lowest AGL altitude at which they gain LOS to the
    transmitter (blue = reachable near the ground, red = must climb high). The
    no-data sentinel is already transparent via the GeoTIFF's nodata tag.
    """
    max_ramp_m = estimate_max_altitude_m(layer)
    renderer = build_min_alt_renderer(
        layer.dataProvider(), 1, threshold_m=None, shade=True, max_ramp_m=max_ramp_m,
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


def load_contour_result(
    gpkg_path: str, display_name: str = "Iso-altitude contours",
) -> QgsVectorLayer:
    """Load an iso-altitude contour GeoPackage with graduated colours + labels.

    Lines are coloured by required altitude and labelled in metres so the map
    reads "fly above N m to clear this ridge" at a glance.
    """
    if not os.path.isfile(gpkg_path):
        raise ValueError(f"Contour file not found: {gpkg_path}")

    uri = f"{gpkg_path}|layername=contours"
    layer = QgsVectorLayer(uri, display_name, "ogr")
    if not layer.isValid():
        raise ValueError(f"Failed to load contour layer from {gpkg_path}")

    _style_contours(layer)
    return layer


def _style_contours(layer: QgsVectorLayer) -> None:
    """Graduated line colours by ``alt_m`` plus altitude labels."""
    from qgis.core import (
        QgsGraduatedSymbolRenderer,
        QgsLineSymbol,
        QgsPalLayerSettings,
        QgsTextFormat,
        QgsVectorLayerSimpleLabeling,
    )

    # Graduated colour by required altitude (shares the min-alt ramp intent:
    # cool = low/easy, warm = high). Uses the layer's own value range.
    try:
        renderer = QgsGraduatedSymbolRenderer.createRenderer(
            layer, "alt_m", 5,
            QgsGraduatedSymbolRenderer.Jenks,
            QgsLineSymbol.createSimple({"line_width": "0.5"}),
            None,
        )
        layer.setRenderer(renderer)
    except Exception:
        pass  # fall back to the default single-symbol renderer

    # Label each contour with its altitude in metres.
    settings = QgsPalLayerSettings()
    settings.fieldName = "concat(format_number(\"alt_m\", 0), ' m')"
    settings.isExpression = True
    settings.placement = QgsPalLayerSettings.Line
    settings.setFormat(QgsTextFormat())
    layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
    layer.setLabelsEnabled(True)
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
