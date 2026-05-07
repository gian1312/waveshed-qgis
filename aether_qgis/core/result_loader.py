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

TAG = "AETHER"


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


def add_layer_to_project(layer: QgsRasterLayer | QgsVectorLayer) -> None:
    """Add a layer to the current QGIS project after a validity check.

    Parameters
    ----------
    layer:
        A QgsRasterLayer or QgsVectorLayer to register with the project.

    Raises
    ------
    ValueError
        If the layer is not valid.
    """
    if not layer.isValid():
        raise ValueError(
            f"Cannot add invalid layer '{layer.name()}' to the project"
        )

    QgsProject.instance().addMapLayer(layer)
