"""Custom resolution profile editor — resolution against distance, as a staircase.

Opened from the Site Analysis "Resolution" row when *Custom* is chosen. Each step
of the staircase is one band of ``analysis.range_profile`` (core.range_profile):

* drag a band up / down to change its resolution (snaps to the resolutions the
  plugin has terrain tiles for, and never finer than the band before it);
* drag a boundary left / right to move where the resolution changes (0.5 km
  snap);
* double-click inside a band to split it there; right-click a boundary to
  remove it.

Plain QPainter, no extra dependency.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from qgis.PyQt.QtCore import QPointF, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QPainter, QPen
from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core import range_profile as rp

Band = Tuple[float, int]

_SNAP_M = 500.0
_GRAB_PX = 6


def _event_pos(event) -> QPointF:
    """Mouse position as QPointF on Qt5 (pos) and Qt6 (position)."""
    return QPointF(event.position()) if hasattr(event, "position") else QPointF(event.pos())


class ProfileChart(QWidget):
    """The staircase editor. Emits :attr:`changed` after every edit."""

    changed = pyqtSignal()

    def __init__(self, bands: List[Band], base_m: int, range_km: float, parent=None):
        super().__init__(parent)
        self.base_m = base_m
        self.range_m = range_km * 1000.0
        self.levels = [base_m] + rp.ladder_for(base_m)
        self.bands: List[Band] = list(bands)
        self._drag: Optional[Tuple[str, int]] = None  # ("edge"|"band", index)
        self.setMinimumSize(420, 220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setToolTip(
            "Drag a step up or down to change its resolution, a boundary left or "
            "right to move it. Double-click to split a band, right-click a "
            "boundary to remove it."
        )

    # ---- geometry ----------------------------------------------------------
    def _plot(self) -> QRectF:
        # Room for the y labels left, half a label above the top line, and the
        # last x label's right half.
        return QRectF(64, 20, max(self.width() - 64 - 34, 10), max(self.height() - 20 - 26, 10))

    def _x(self, d_m: float) -> float:
        r = self._plot()
        return r.left() + r.width() * d_m / self.range_m

    def _d(self, x: float) -> float:
        r = self._plot()
        return min(max((x - r.left()) / r.width() * self.range_m, 0.0), self.range_m)

    def _y(self, level_idx: int) -> float:
        r = self._plot()
        n = max(len(self.levels) - 1, 1)
        return r.bottom() - r.height() * level_idx / n

    def _level_at(self, y: float) -> int:
        r = self._plot()
        n = max(len(self.levels) - 1, 1)
        idx = round((r.bottom() - y) / r.height() * n)
        return int(min(max(idx, 0), len(self.levels) - 1))

    def _band_at(self, x: float) -> int:
        d = self._d(x)
        for i, (until, _) in enumerate(self.bands):
            if d <= until:
                return i
        return len(self.bands) - 1

    def _edge_at(self, x: float) -> Optional[int]:
        for i, (until, _) in enumerate(self.bands[:-1]):
            if abs(self._x(until) - x) <= _GRAB_PX:
                return i
        return None

    # ---- painting ----------------------------------------------------------
    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt override)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self._plot()
        pal = self.palette()
        text = pal.windowText().color()
        grid = QColor(text)
        grid.setAlpha(50)
        p.setPen(QPen(grid, 1))
        for i, lvl in enumerate(self.levels):
            y = self._y(i)
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QPen(text, 1))
            p.drawText(QRectF(0, y - 8, r.left() - 6, 16),
                       int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), f"{lvl} m")
            p.setPen(QPen(grid, 1))
        ticks = 5
        for k in range(ticks + 1):
            d = self.range_m * k / ticks
            x = self._x(d)
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.setPen(QPen(text, 1))
            p.drawText(QRectF(x - 30, r.bottom() + 4, 60, 16),
                       int(Qt.AlignmentFlag.AlignHCenter), f"{d / 1000:g} km")
            p.setPen(QPen(grid, 1))
        line = QColor(48, 140, 198)
        p.setPen(QPen(line, 3))
        start = 0.0
        prev_y = None
        for until, step in self.bands:
            y = self._y(self.levels.index(step))
            if prev_y is not None:
                p.drawLine(QPointF(self._x(start), prev_y), QPointF(self._x(start), y))
            p.drawLine(QPointF(self._x(start), y), QPointF(self._x(until), y))
            prev_y = y
            start = until
        p.end()

    # ---- interaction -------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        pos = _event_pos(event)
        edge = self._edge_at(pos.x())
        if event.button() == Qt.MouseButton.RightButton:
            if edge is not None:
                del self.bands[edge]
                self.bands = rp.normalise_bands(self.bands, self.range_m / 1000.0)
                self.update()
                self.changed.emit()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._drag = ("edge", edge) if edge is not None else ("band", self._band_at(pos.x()))

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = _event_pos(event)
        if self._drag is None:
            cursor = (Qt.CursorShape.SizeHorCursor if self._edge_at(pos.x()) is not None
                      else Qt.CursorShape.SizeVerCursor)
            self.setCursor(cursor)
            return
        kind, i = self._drag
        if kind == "edge":
            lo = self.bands[i - 1][0] if i > 0 else 0.0
            hi = self.bands[i + 1][0]
            d = round(self._d(pos.x()) / _SNAP_M) * _SNAP_M
            d = min(max(d, lo + _SNAP_M), hi - _SNAP_M)
            if lo + _SNAP_M <= d <= hi - _SNAP_M:
                self.bands[i] = (d, self.bands[i][1])
        else:
            idx = self._level_at(pos.y())
            lo = self.levels.index(self.bands[i - 1][1]) if i > 0 else 0
            hi = (self.levels.index(self.bands[i + 1][1]) if i + 1 < len(self.bands)
                  else len(self.levels) - 1)
            if i == 0:
                idx = 0  # the first band is the chosen resolution itself
            self.bands[i] = (self.bands[i][0], self.levels[min(max(idx, lo), hi)])
        self.update()
        self.changed.emit()

    def mouseReleaseEvent(self, _event) -> None:  # noqa: N802
        if self._drag is not None:
            self._drag = None
            self.bands = rp.normalise_bands(self.bands, self.range_m / 1000.0)
            self.update()
            self.changed.emit()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        pos = _event_pos(event)
        i = self._band_at(pos.x())
        d = round(self._d(pos.x()) / _SNAP_M) * _SNAP_M
        start = self.bands[i - 1][0] if i > 0 else 0.0
        if len(self.bands) < rp.MAX_BANDS and start + _SNAP_M <= d <= self.bands[i][0] - _SNAP_M:
            self.bands.insert(i, (d, self.bands[i][1]))
            self.update()
            self.changed.emit()


class RangeProfileDialog(QDialog):
    """Edit a resolution profile for one base resolution and range."""

    def __init__(self, base_m: int, range_km: float, bands: Optional[List[Band]] = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Custom resolution profile")
        self.base_m = base_m
        self.range_km = range_km
        start = bands if bands and bands[0][1] == base_m else self._default()
        self.chart = ProfileChart(rp.normalise_bands(start, range_km), base_m, range_km)
        self.lbl = QLabel("")
        self.lbl.setWordWrap(True)
        intro = QLabel(
            f"Resolution against distance from the transmitter, starting at the "
            f"chosen {base_m} m. Coarser steps further out are much faster and "
            f"lose little; the near field keeps its detail."
        )
        intro.setWordWrap(True)
        btn_reset = QPushButton("Reset to Adaptive")
        btn_reset.clicked.connect(self._on_reset)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(btn_reset)
        row.addStretch()
        row.addWidget(self.buttons)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.chart, 1)
        layout.addWidget(self.lbl)
        layout.addLayout(row)
        self.chart.changed.connect(self._refresh)
        self.resize(560, 360)
        self._refresh()

    def _default(self) -> List[Band]:
        return (rp.adaptive_bands(self.base_m, self.range_km)
                or [(self.range_km * 1000.0, self.base_m)])

    def _on_reset(self) -> None:
        self.chart.bands = rp.normalise_bands(self._default(), self.range_km)
        self.chart.update()
        self._refresh()

    def _refresh(self) -> None:
        problem = rp.validate_bands(self.chart.bands, self.range_km)
        self.lbl.setText(problem or rp.describe(self.bands()))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(problem is None)

    def bands(self) -> List[Band]:
        # A split that was never given its own resolution merges back.
        return rp.normalise_bands(self.chart.bands, self.range_km)
