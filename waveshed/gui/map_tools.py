"""Map interaction tools for the AETHER QGIS plugin.

Provides proper map capture tools that correctly deactivate and restore
the previous map tool state, following the MPT SIGMA plugin pattern.
"""

from qgis.PyQt.QtCore import pyqtSignal, Qt
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
)
from qgis.gui import QgsMapToolEmitPoint


class PointCaptureTool(QgsMapToolEmitPoint):
    """Map tool for picking a single point on the canvas.

    Transforms the clicked coordinate to WGS84 (EPSG:4326) and emits
    it as (lat, lon).  After capture the tool automatically deactivates
    and restores whatever map tool was active before.

    Signals:
        point_captured(float, float): Emitted with (latitude, longitude)
            in WGS84 after a successful capture.
    """

    point_captured = pyqtSignal(float, float)  # lat, lon in WGS84

    def __init__(self, canvas, previous_tool=None):
        super().__init__(canvas)
        self._canvas = canvas
        self._previous_tool = previous_tool
        self.setCursor(Qt.CursorShape.CrossCursor)

    def canvasReleaseEvent(self, event):
        """Handle mouse release: capture point, transform, emit, restore."""
        point = self.toMapCoordinates(event.pos())

        # Transform to WGS84 if the map canvas uses a different CRS.
        map_crs = self._canvas.mapSettings().destinationCrs()
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        if map_crs != wgs84:
            xform = QgsCoordinateTransform(
                map_crs, wgs84, QgsProject.instance()
            )
            point = xform.transform(point)

        self.point_captured.emit(point.y(), point.x())  # lat, lon
        self._restore_tool()

    def keyPressEvent(self, event):
        """Allow cancellation with Escape."""
        if event.key() == Qt.Key.Key_Escape:
            self._restore_tool()

    def _restore_tool(self):
        """Deactivate this tool and reinstate the previous one."""
        if self._previous_tool:
            self._canvas.setMapTool(self._previous_tool)
        else:
            self._canvas.unsetMapTool(self)


def activate_point_capture(iface, callback):
    """Activate point capture on the map canvas.

    Saves the current map tool, creates a :class:`PointCaptureTool`,
    connects it to *callback*, and sets it as the active tool.

    Args:
        iface: QgisInterface instance.
        callback: Callable accepting (lat: float, lon: float).

    Returns:
        PointCaptureTool instance.  The caller **must** keep a reference
        to prevent garbage collection while the tool is active.
    """
    canvas = iface.mapCanvas()
    prev_tool = canvas.mapTool()
    tool = PointCaptureTool(canvas, previous_tool=prev_tool)
    tool.point_captured.connect(callback)
    canvas.setMapTool(tool)
    return tool
