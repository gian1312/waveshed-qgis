"""Custom resolution profile editor — resolution against distance, as a staircase.

Opened from the Site Analysis "Resolution" row when *Custom* is chosen. Each step
of the staircase is one band of ``analysis.range_profile`` (core.range_profile):

* drag a band up / down to change its resolution (snaps to the resolutions the
  plugin has terrain tiles for, and never finer than the band before it);
* drag a boundary left / right to move where the resolution changes (0.5 km
  snap, finer when zoomed in — down to 10 m);
* double-click inside a band to split it there; right-click a boundary to
  remove it;
* mouse wheel zooms the distance axis around the cursor, middle- or
  right-drag pans it, *Reset zoom* shows the whole range again — on a long
  range the near field is otherwise a few pixels wide.

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
#: Boundary snap steps; the coarsest one that still gives ~80 snaps across the
#: visible span is used, so zooming in makes boundaries placeable to 10 m.
_SNAP_STEPS_M = (10.0, 20.0, 50.0, 100.0, 250.0, _SNAP_M)
#: Narrowest visible span the wheel zooms to.
_MIN_SPAN_M = 200.0
#: Wheel zoom per 120-unit notch (one click on most mice).
_ZOOM_STEP = 0.8


def snap_for_span(span_m: float) -> float:
    """Boundary snap for a visible distance span of *span_m* metres."""
    fitting = [s for s in _SNAP_STEPS_M if s * 80.0 <= span_m]
    return fitting[-1] if fitting else _SNAP_STEPS_M[0]


def zoom_view(view: Tuple[float, float], anchor_m: float, factor: float,
              range_m: float) -> Tuple[float, float]:
    """*view* scaled by *factor* (< 1 zooms in) about *anchor_m*, kept inside
    0..*range_m* and no narrower than :data:`_MIN_SPAN_M`."""
    v0, v1 = view
    span = min(max((v1 - v0) * factor, min(_MIN_SPAN_M, range_m)), range_m)
    frac = (anchor_m - v0) / (v1 - v0) if v1 > v0 else 0.5
    start = anchor_m - frac * span
    return pan_view((start, start + span), 0.0, range_m)


def pan_view(view: Tuple[float, float], delta_m: float,
             range_m: float) -> Tuple[float, float]:
    """*view* shifted by *delta_m*, clamped to 0..*range_m* (span kept)."""
    v0, v1 = view
    span = v1 - v0
    start = min(max(v0 + delta_m, 0.0), max(range_m - span, 0.0))
    return start, start + span


def nice_ticks(v0: float, v1: float, target: int = 5) -> List[float]:
    """Round distance ticks (1/2/5 x 10^n metres) covering *v0*..*v1*."""
    span = v1 - v0
    if span <= 0:
        return [v0]
    raw = span / target
    mag = 10.0 ** int(f"{raw:e}".split("e")[1])
    step = next(m * mag for m in (1.0, 2.0, 5.0, 10.0) if m * mag >= raw)
    first = -(-v0 // step) * step
    ticks = []
    t = first
    while t <= v1 + step * 1e-6:
        ticks.append(round(t, 6))
        t += step
    return ticks


def _km_label(d_m: float) -> str:
    return f"{round(d_m / 1000, 2):g} km"


def _event_pos(event) -> QPointF:
    """Mouse position as QPointF on Qt5 (pos) and Qt6 (position)."""
    return QPointF(event.position()) if hasattr(event, "position") else QPointF(event.pos())


class ProfileChart(QWidget):
    """The staircase editor. Emits :attr:`changed` after every edit."""

    changed = pyqtSignal()
    #: The visible distance span changed (wheel, pan, reset).
    zoomed = pyqtSignal()

    def __init__(self, bands: List[Band], base_m: int, range_km: float, parent=None):
        super().__init__(parent)
        self.base_m = base_m
        self.range_m = range_km * 1000.0
        self.levels = [base_m] + rp.ladder_for(base_m)
        self.bands: List[Band] = list(bands)
        self._drag: Optional[Tuple[str, int]] = None  # ("edge"|"band", index)
        self.view: Tuple[float, float] = (0.0, self.range_m)  # visible distance span
        self._pan_from: Optional[Tuple[float, Tuple[float, float]]] = None  # (x px, view)
        self.setMinimumSize(420, 220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setToolTip(
            "Drag a step up or down to change its resolution, a boundary left or "
            "right to move it. Double-click to split a band, right-click a "
            "boundary to remove it. Mouse wheel zooms the distance axis, "
            "middle- or right-drag pans it."
        )

    # ---- geometry ----------------------------------------------------------
    def _plot(self) -> QRectF:
        # Room for the y labels left, half a label above the top line, and the
        # last x label's right half.
        return QRectF(64, 20, max(self.width() - 64 - 34, 10), max(self.height() - 20 - 26, 10))

    def _x(self, d_m: float) -> float:
        r = self._plot()
        v0, v1 = self.view
        return r.left() + r.width() * (d_m - v0) / (v1 - v0)

    def _d(self, x: float) -> float:
        r = self._plot()
        v0, v1 = self.view
        d = v0 + (x - r.left()) / r.width() * (v1 - v0)
        return min(max(d, v0), v1)

    def snap_m(self) -> float:
        """Boundary snap for the current zoom."""
        return snap_for_span(self.view[1] - self.view[0])

    def is_zoomed(self) -> bool:
        return self.view != (0.0, self.range_m)

    def reset_zoom(self) -> None:
        self.view = (0.0, self.range_m)
        self.update()
        self.zoomed.emit()

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
        v0, v1 = self.view
        for i, (until, _) in enumerate(self.bands[:-1]):
            if v0 <= until <= v1 and abs(self._x(until) - x) <= _GRAB_PX:
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
        for d in nice_ticks(*self.view):
            x = self._x(d)
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.setPen(QPen(text, 1))
            p.drawText(QRectF(x - 30, r.bottom() + 4, 60, 16),
                       int(Qt.AlignmentFlag.AlignHCenter), _km_label(d))
            p.setPen(QPen(grid, 1))
        if self.is_zoomed():
            p.setPen(QPen(text, 1))
            p.drawText(QRectF(r.left(), 0, r.width(), 18),
                       int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                       f"showing {_km_label(self.view[0])} – {_km_label(self.view[1])}"
                       f" of {_km_label(self.range_m)}")
        # Zoomed in, the staircase runs past the plot edges: keep it inside.
        p.setClipRect(r.adjusted(-2, -2, 2, 2))
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
    def wheelEvent(self, event) -> None:  # noqa: N802
        notches = event.angleDelta().y() / 120.0
        if not notches:
            return
        anchor = self._d(_event_pos(event).x())
        self.view = zoom_view(self.view, anchor, _ZOOM_STEP ** notches, self.range_m)
        self.update()
        self.zoomed.emit()
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        pos = _event_pos(event)
        edge = self._edge_at(pos.x())
        if (event.button() == Qt.MouseButton.MiddleButton
                or (event.button() == Qt.MouseButton.RightButton and edge is None)):
            self._pan_from = (pos.x(), self.view)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
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
        if self._pan_from is not None:
            x0, view = self._pan_from
            metres_per_px = (view[1] - view[0]) / self._plot().width()
            self.view = pan_view(view, (x0 - pos.x()) * metres_per_px, self.range_m)
            self.update()
            self.zoomed.emit()
            return
        if self._drag is None:
            cursor = (Qt.CursorShape.SizeHorCursor if self._edge_at(pos.x()) is not None
                      else Qt.CursorShape.SizeVerCursor)
            self.setCursor(cursor)
            return
        kind, i = self._drag
        if kind == "edge":
            lo = self.bands[i - 1][0] if i > 0 else 0.0
            hi = self.bands[i + 1][0]
            snap = self.snap_m()
            d = round(self._d(pos.x()) / snap) * snap
            d = min(max(d, lo + snap), hi - snap)
            if lo + snap <= d <= hi - snap:
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
        if self._pan_from is not None:
            self._pan_from = None
            self.unsetCursor()
            return
        if self._drag is not None:
            self._drag = None
            self.bands = rp.normalise_bands(self.bands, self.range_m / 1000.0)
            self.update()
            self.changed.emit()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        pos = _event_pos(event)
        i = self._band_at(pos.x())
        snap = self.snap_m()
        d = round(self._d(pos.x()) / snap) * snap
        start = self.bands[i - 1][0] if i > 0 else 0.0
        if len(self.bands) < rp.MAX_BANDS and start + snap <= d <= self.bands[i][0] - snap:
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
        self.btn_zoom = QPushButton("Reset zoom")
        self.btn_zoom.setToolTip("Show the whole range again.")
        self.btn_zoom.clicked.connect(self.chart.reset_zoom)
        self.lbl_zoom = QLabel("")
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(btn_reset)
        row.addWidget(self.btn_zoom)
        row.addWidget(self.lbl_zoom)
        row.addStretch()
        row.addWidget(self.buttons)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.chart, 1)
        layout.addWidget(self.lbl)
        layout.addLayout(row)
        self.chart.changed.connect(self._refresh)
        self.chart.zoomed.connect(self._refresh_zoom)
        self.resize(640, 400)
        self._refresh()
        self._refresh_zoom()

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

    def _refresh_zoom(self) -> None:
        self.btn_zoom.setEnabled(self.chart.is_zoomed())
        snap = self.chart.snap_m()
        self.lbl_zoom.setText(f"wheel: zoom · boundaries snap to {snap:g} m")

    def bands(self) -> List[Band]:
        # A split that was never given its own resolution merges back.
        return rp.normalise_bands(self.chart.bands, self.range_km)
