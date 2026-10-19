"""QPainter charting package, ported from MPT SIGMA (gui/widgets/plotting).

A small, self-contained replacement for matplotlib in the plugin's dialogs —
the same widgets and interaction (scroll zoom, drag pan, double-click reset,
right-click Save Image) as MPT SIGMA, so both tools behave alike.
Chart chrome is derived from the widget ``QPalette`` (so it tracks the host
theme); only data series carry explicit colours. One source runs on PyQt5
(QGIS 3) and PyQt6 (QGIS 4) via the ``qgis.PyQt`` shim.

Public API:
    * :class:`XYPlotWidget`    -- cartesian line/area chart (P2P plots).
    * :class:`PolarPlotWidget` -- polar chart (antenna azimuth/elevation).
"""

from .cartesian import XYPlotWidget
from .polar import PolarPlotWidget

__all__ = ["XYPlotWidget", "PolarPlotWidget"]
