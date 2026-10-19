"""Polar chart (``PolarPlotWidget``) for the antenna azimuth/elevation patterns.

Supports a configurable zero direction (N/E/S/W), CW/CCW winding, a full circle
or a partial sweep (elevation's -90..90 semicircle), radial grid rings/arcs with
labels, and a single traced series. Only ``_draw_plot`` lives here; shared
chrome comes from :class:`~.base.BasePlotWidget`.
"""

import math
from typing import List, Optional, Tuple

import numpy as np
from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QFontMetrics, QPen, QPolygonF

from .base import BasePlotWidget, resolve_color

# Screen angle (degrees, math convention: 0 = +x/right, CCW positive) at which
# theta = 0 points, per the requested compass letter.
_ZERO_PHI = {"E": 0.0, "N": 90.0, "W": 180.0, "S": -90.0}


class PolarPlotWidget(BasePlotWidget):
    """Polar trace chart configured via ``set_*`` methods (each returns ``self``)."""

    def __init__(self, parent=None) -> None:
        """Create an empty polar chart with default orientation (zero East, CCW)."""
        super().__init__(parent)
        self._theta: Optional[np.ndarray] = None  # degrees
        self._r: Optional[np.ndarray] = None
        self._color = None
        self._width: float = 2.0

        self._theta_zero: str = "E"
        self._direction: str = "ccw"
        self._theta_range: Optional[Tuple[float, float]] = None  # None -> full circle
        self._r_max: Optional[float] = None  # None -> autoscale from data
        self._r_ticks: Optional[List[float]] = None

    # ================================================================= config
    def set_series(self, theta_deg, r, *, color, width: float = 2.0) -> "PolarPlotWidget":
        """Set the traced series. ``theta_deg`` is in DEGREES; ``r`` any positive scale."""
        ta = np.asarray(theta_deg, dtype=float).ravel()
        ra = np.asarray(r, dtype=float).ravel()
        if ta.size and ta.size == ra.size:
            self._theta = ta
            self._r = ra
            self._color = color
            self._width = float(width)
        self.update()
        return self

    def set_theta_zero(self, where: str) -> "PolarPlotWidget":
        """Set where 0 degrees points: ``"N"``, ``"E"``, ``"S"`` or ``"W"``."""
        key = str(where).upper()[:1]
        if key in _ZERO_PHI:
            self._theta_zero = key
        self.update()
        return self

    def set_theta_direction(self, direction: str) -> "PolarPlotWidget":
        """Set the winding of increasing theta: ``"cw"`` or ``"ccw"``."""
        self._direction = "cw" if str(direction).lower() in ("cw", "clockwise", "-1", "-1.0") else "ccw"
        self.update()
        return self

    def set_theta_range(self, min_deg: float, max_deg: float) -> "PolarPlotWidget":
        """Restrict the drawn sweep to ``[min_deg, max_deg]`` (a partial wedge)."""
        self._theta_range = (float(min_deg), float(max_deg))
        self.update()
        return self

    def set_r_max(self, value: float) -> "PolarPlotWidget":
        """Set the radius mapped to the outer edge (clamps larger r)."""
        self._r_max = float(value)
        self.update()
        return self

    def set_r_ticks(self, ticks) -> "PolarPlotWidget":
        """Set the radial gridline radii (also used as their labels)."""
        self._r_ticks = [float(t) for t in ticks]
        self.update()
        return self

    # ============================================================ orientation
    def _phi_deg(self, theta_deg: float) -> float:
        """Screen angle (deg, math convention) for a data ``theta_deg``."""
        sign = -1.0 if self._direction == "cw" else 1.0
        return _ZERO_PHI[self._theta_zero] + sign * theta_deg

    def _r_max_value(self) -> float:
        """Effective outer radius value (explicit, else from data, else 1.0)."""
        if self._r_max is not None and self._r_max > 0:
            return self._r_max
        if self._r is not None and self._r.size:
            m = float(np.nanmax(self._r))
            if m > 0:
                return m
        return 1.0

    def _sweep(self) -> Tuple[float, float]:
        """Data theta sweep (min, max) in degrees; full circle if unset."""
        if self._theta_range is None:
            return 0.0, 360.0
        lo, hi = self._theta_range
        return (lo, hi) if hi >= lo else (hi, lo)

    def _unit_extent(self) -> Tuple[float, float, float, float]:
        """Bounding box (xmin, xmax, ymin, ymax) of the unit-radius wedge.

        Includes the origin so a partial wedge is fitted (and centred) to fill
        the content rect. Screen Y is negated (``-sin``) to match pixel space.
        """
        th_lo, th_hi = self._sweep()
        xs = [0.0]
        ys = [0.0]
        steps = 60
        for i in range(steps + 1):
            theta = th_lo + (th_hi - th_lo) * (i / steps)
            phi = math.radians(self._phi_deg(theta))
            xs.append(math.cos(phi))
            ys.append(-math.sin(phi))
        return min(xs), max(xs), min(ys), max(ys)

    # ================================================================ drawing
    def _draw_plot(self, painter, rect: QRectF) -> None:
        """Draw the polar grid and the traced series inside ``rect``."""
        if self._theta is None or self._r is None or not self._theta.size:
            self._draw_center_message(painter, rect, "No data")
            return

        r_max = self._r_max_value()
        r_ticks = self._r_ticks if self._r_ticks is not None else _default_r_ticks(r_max)

        # Fit the (possibly partial) wedge into rect, leaving room for labels.
        fm = QFontMetrics(self._tick_font())
        pad = fm.height() + 6.0
        xmin, xmax, ymin, ymax = self._unit_extent()
        span_x = max(1e-9, xmax - xmin)
        span_y = max(1e-9, ymax - ymin)
        radius = min((rect.width() - 2 * pad) / span_x, (rect.height() - 2 * pad) / span_y)
        if radius <= 1.0:
            return
        # Centre the unit box (scaled by radius) inside rect.
        box_w = span_x * radius
        box_h = span_y * radius
        origin_x = rect.left() + (rect.width() - box_w) / 2.0 - xmin * radius
        origin_y = rect.top() + (rect.height() - box_h) / 2.0 - ymin * radius

        def to_pixel(theta_deg: float, r_val: float) -> QPointF:
            phi = math.radians(self._phi_deg(theta_deg))
            rr = radius * max(0.0, min(1.0, r_val / r_max if r_max else 0.0))
            return QPointF(origin_x + rr * math.cos(phi), origin_y - rr * math.sin(phi))

        th_lo, th_hi = self._sweep()
        full_circle = self._theta_range is None

        # --- Radial grid rings / arcs.
        grid_pen = QPen(self._grid())
        grid_pen.setWidthF(1.0)
        painter.setPen(grid_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for tick in r_ticks:
            if tick <= 0 or tick > r_max:
                continue
            rr = radius * (tick / r_max)
            ring = QRectF(origin_x - rr, origin_y - rr, 2 * rr, 2 * rr)
            if full_circle:
                painter.drawEllipse(ring)
            else:
                phi_a = self._phi_deg(th_lo)
                phi_b = self._phi_deg(th_hi)
                start = min(phi_a, phi_b)
                span = abs(phi_b - phi_a)
                # Qt arc angles: 1/16 deg, 0 at 3 o'clock, CCW positive.
                painter.drawArc(ring, int(round(start * 16)), int(round(span * 16)))

        # --- Angular spokes.
        painter.setPen(grid_pen)
        for theta in _spoke_angles(th_lo, th_hi, full_circle):
            painter.drawLine(to_pixel(theta, 0.0), to_pixel(theta, r_max))

        # --- Outer boundary for a partial wedge (two radii + the outer arc).
        if not full_circle:
            painter.setPen(grid_pen)
            painter.drawLine(to_pixel(th_lo, 0.0), to_pixel(th_lo, r_max))
            painter.drawLine(to_pixel(th_hi, 0.0), to_pixel(th_hi, r_max))

        # --- Radial tick labels along a label spoke (~22.5 deg into the sweep).
        painter.setFont(self._tick_font())
        label_theta = th_lo + (th_hi - th_lo) * (22.5 / 360.0) if full_circle else (th_lo + th_hi) / 2.0
        for tick in r_ticks:
            if tick <= 0 or tick > r_max:
                continue
            pt = to_pixel(label_theta, tick)
            self._text(
                painter,
                pt.x(),
                pt.y(),
                _fmt_r(tick),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                self._muted(),
            )

        # --- Traced series.
        poly = QPolygonF()
        for theta, r_val in zip(self._theta, self._r):
            poly.append(to_pixel(float(theta), float(r_val)))
        pen = QPen(resolve_color(self._color))
        pen.setWidthF(max(0.5, self._width))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if poly.count() > 1:
            painter.drawPolyline(poly)
        elif poly.count() == 1:
            painter.drawPoint(poly.at(0))


def _default_r_ticks(r_max: float) -> List[float]:
    """Quarter-fraction radial ticks when the caller supplies none."""
    return [r_max * f for f in (0.25, 0.5, 0.75, 1.0)]


def _spoke_angles(th_lo: float, th_hi: float, full_circle: bool) -> List[float]:
    """Nice angular spoke positions (degrees) within the sweep."""
    if full_circle:
        return [float(a) for a in range(0, 360, 30)]
    span = th_hi - th_lo
    if span <= 0:
        return [th_lo]
    step = 15.0 if span <= 90.0 else 30.0 if span <= 180.0 else 45.0
    angles = []
    a = math.ceil(th_lo / step) * step
    while a <= th_hi + 1e-9:
        angles.append(a)
        a += step
    return angles


def _fmt_r(value: float) -> str:
    """Compact radial-tick label (no trailing zeros): 0.25, 0.5, 1."""
    return format(float(value), "g")
