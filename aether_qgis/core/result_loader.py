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
    QgsProject,
    QgsRasterLayer,
    QgsRasterShader,
    QgsSingleBandPseudoColorRenderer,
    QgsVectorLayer,
)
from qgis.PyQt.QtGui import QColor

from .layer_utils import mark_aether_output

TAG = "AETHER"

# Layer-tree group paths so AETHER outputs stay organised instead of piling up
# at the top level of the Layers panel.
GROUP_ROOT = "AETHER"
GROUP_COVERAGE = (GROUP_ROOT, "Coverage")
GROUP_P2P = (GROUP_ROOT, "P2P")
GROUP_TERRAIN = (GROUP_ROOT, "Terrain")


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
    else:
        QgsMessageLog.logMessage(
            f"Unknown model '{model}' — layer loaded without styling",
            TAG,
            Qgis.MessageLevel.Warning,
        )

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

    ``_ensure_layer_group("AETHER", "Coverage")`` returns the ``Coverage``
    group nested under a top-level ``AETHER`` group, creating either level
    that does not yet exist. With no names, returns the tree root.

    The top-level AETHER group is inserted at the *top* of the Layers panel
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
        under (e.g. ``("AETHER", "Coverage")``). The groups are created if
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
