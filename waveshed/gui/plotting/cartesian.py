"""Cartesian XY chart (``XYPlotWidget``) for the P2P plots.

Supports multiple line series (solid/dashed, optional markers, alpha), filled
bands (``fill_between``), horizontal reference lines, an optional secondary
right Y axis (``twinx``), a corner legend, manual or auto Y limits, and light
wheel-zoom / drag-pan / double-click-reset interaction. Only ``_draw_plot`` and
the interaction handlers live here; all shared chrome comes from
:class:`~.base.BasePlotWidget`.
"""

import contextlib
from typing import Dict, List, Optional, Tuple

import numpy as np
from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QFontMetrics, QPen, QPolygonF

from .base import BasePlotWidget, resolve_color
from .ticks import nice_ticks, tick_format
from .transform import LinearScale

# Axis identifiers.
_LEFT = "left"
_RIGHT = "right"


def _norm_axis(axis: str) -> str:
    """Map any right-ish name to ``"right"``, everything else to ``"left"``."""
    return _RIGHT if str(axis).lower() in (_RIGHT, "right", "y2", "secondary") else _LEFT


def _pen_style(style: str) -> "Qt.PenStyle":
    """Translate ``"solid"``/``"dashed"``/``"dotted"`` to a scoped ``Qt.PenStyle``."""
    key = str(style).lower()
    if key in ("dashed", "dash", "--"):
        return Qt.PenStyle.DashLine
    if key in ("dotted", "dot", ":"):  # Waveshed addition: the F1 outline
        return Qt.PenStyle.DotLine
    return Qt.PenStyle.SolidLine


def _as_float_array(values) -> np.ndarray:
    """Coerce a python list / numpy array to a flat float ``ndarray``."""
    return np.asarray(values, dtype=float).ravel()


def _pad_range(lo: float, hi: float, frac: float = 0.05) -> Tuple[float, float]:
    """Pad ``[lo, hi]`` by ``frac`` on each side; widen a flat range sensibly."""
    if lo == hi:
        pad = abs(lo) * frac if lo != 0.0 else 0.5
        if pad == 0.0:
            pad = 0.5
        return lo - pad, hi + pad
    span = hi - lo
    return lo - span * frac, hi + span * frac


class XYPlotWidget(BasePlotWidget):
    """Interactive cartesian line/area chart.

    Configure it with the ``set_*``/``add_*`` methods (each returns ``self`` so
    calls chain), then it repaints itself. X autoranges over the union of every
    series; each Y axis autoranges over the series bound to it unless
    :meth:`set_y_limits` overrides it.
    """

    def __init__(self, parent=None) -> None:
        """Create an empty chart."""
        super().__init__(parent)
        self._x_label: str = ""
        self._y_label: str = ""
        self._y_label_color = None
        self._y2_label: str = ""
        self._y2_label_color = None
        self._y2_enabled: bool = False

        # Drawable primitives, kept in add-order.
        self._series: List[Dict] = []
        self._fills: List[Dict] = []
        self._hlines: List[Dict] = []

        # Manual Y limits per axis (None -> autorange).
        self._ylim: Dict[str, Optional[Tuple[float, float]]] = {_LEFT: None, _RIGHT: None}

        self._legend_visible: bool = False
        self._legend_loc: str = "upper right"

        # Discoverability hint for the built-in interaction (drawn by the base).
        self._hint = "Scroll: zoom  ·  Drag: pan  ·  Double-click: reset"

        # Interaction state: a view override (set by zoom/pan) and the scales
        # captured at the last paint so mouse events can invert pixels->data.
        self._view: Optional[Dict[str, Optional[Tuple[float, float]]]] = None
        self._plot_rect: Optional[QRectF] = None
        self._scale_x: Optional[LinearScale] = None
        self._scale_y: Dict[str, Optional[LinearScale]] = {_LEFT: None, _RIGHT: None}
        self._pan_anchor: Optional[Tuple[float, float]] = None
        self._pan_start_view: Optional[Dict] = None
        self._pan_scale_x: Optional[LinearScale] = None
        self._pan_scale_y: Dict[str, Optional[LinearScale]] = {_LEFT: None, _RIGHT: None}

    # ================================================================= config
    def set_x_label(self, text: str) -> "XYPlotWidget":
        """Set the X axis title."""
        self._x_label = text or ""
        self.update()
        return self

    def set_y_label(self, text: str, color=None) -> "XYPlotWidget":
        """Set the left Y axis title, optionally tinted with ``color``."""
        self._y_label = text or ""
        self._y_label_color = color
        self.update()
        return self

    def set_y2_label(self, text: str, color=None) -> "XYPlotWidget":
        """Set the right Y axis title (and enable the secondary axis)."""
        self._y2_label = text or ""
        self._y2_label_color = color
        self._y2_enabled = True
        self.update()
        return self

    def add_line(
        self,
        x,
        y,
        *,
        color,
        width: float = 1.0,
        style: str = "solid",
        label: Optional[str] = None,
        axis: str = "left",
        alpha: float = 1.0,
        markers: bool = False,
    ) -> "XYPlotWidget":
        """Add a line series. Mismatched-length or empty ``x``/``y`` is skipped."""
        xa = _as_float_array(x)
        ya = _as_float_array(y)
        if xa.size == 0 or xa.size != ya.size:
            return self
        ax = _norm_axis(axis)
        self._series.append(
            {
                "x": xa,
                "y": ya,
                "color": color,
                "width": float(width),
                "style": style,
                "label": label,
                "axis": ax,
                "alpha": float(alpha),
                "markers": bool(markers),
            }
        )
        if ax == _RIGHT:
            self._y2_enabled = True
        self.update()
        return self

    def add_fill(
        self,
        x,
        y_lo,
        y_hi,
        *,
        color,
        alpha: float = 0.35,
        label: Optional[str] = None,
        axis: str = "left",
    ) -> "XYPlotWidget":
        """Add a filled band between ``y_lo`` and ``y_hi`` (``fill_between``).

        Either bound may be a scalar (broadcast to ``x``'s length), matching
        matplotlib's ``fill_between(x, y, scalar)`` form.
        """
        xa = _as_float_array(x)
        if xa.size == 0:
            return self
        try:
            lo = np.broadcast_to(np.asarray(y_lo, dtype=float), xa.shape).astype(float)
            hi = np.broadcast_to(np.asarray(y_hi, dtype=float), xa.shape).astype(float)
        except (ValueError, TypeError):
            return self  # non-broadcastable lengths -> skip rather than crash
        ax = _norm_axis(axis)
        self._fills.append(
            {"x": xa, "lo": lo, "hi": hi, "color": color, "alpha": float(alpha), "label": label, "axis": ax}
        )
        if ax == _RIGHT:
            self._y2_enabled = True
        self.update()
        return self

    def add_hline(
        self,
        y: float,
        *,
        color,
        style: str = "dashed",
        width: float = 1.0,
        alpha: float = 1.0,
        label: Optional[str] = None,
        axis: str = "left",
    ) -> "XYPlotWidget":
        """Add a horizontal reference line at ``y`` (matplotlib ``axhline``)."""
        ax = _norm_axis(axis)
        self._hlines.append(
            {
                "y": float(y),
                "color": color,
                "style": style,
                "width": float(width),
                "alpha": float(alpha),
                "label": label,
                "axis": ax,
            }
        )
        if ax == _RIGHT:
            self._y2_enabled = True
        self.update()
        return self

    def set_y_limits(self, lo: float, hi: float, *, axis: str = "left") -> "XYPlotWidget":
        """Fix an axis Y range instead of autoranging it."""
        self._ylim[_norm_axis(axis)] = (float(lo), float(hi))
        self.update()
        return self

    def set_legend(self, visible: bool = True, loc: str = "upper right") -> "XYPlotWidget":
        """Show/hide the legend and pick its corner."""
        self._legend_visible = bool(visible)
        self._legend_loc = loc
        self.update()
        return self

    # ================================================================ ranges
    def _x_range(self) -> Optional[Tuple[float, float]]:
        """Autorange X over every series and fill (padded), or ``None`` if empty."""
        arrays = [s["x"] for s in self._series if s["x"].size] + [f["x"] for f in self._fills if f["x"].size]
        if not arrays:
            return None
        lo = min(float(a.min()) for a in arrays)
        hi = max(float(a.max()) for a in arrays)
        return _pad_range(lo, hi)

    def _y_range(self, axis: str) -> Optional[Tuple[float, float]]:
        """Range for ``axis``: the manual limit if set, else padded autorange."""
        if self._ylim[axis] is not None:
            return self._ylim[axis]
        vals: List[float] = []
        for s in self._series:
            if s["axis"] == axis and s["y"].size:
                vals.append(float(s["y"].min()))
                vals.append(float(s["y"].max()))
        for f in self._fills:
            if f["axis"] == axis and f["x"].size:
                vals.append(float(f["lo"].min()))
                vals.append(float(f["hi"].max()))
        for h in self._hlines:
            if h["axis"] == axis:
                vals.append(h["y"])
        if not vals:
            return None
        return _pad_range(min(vals), max(vals))

    def _effective_ranges(self):
        """Return ``(x, left, right)`` ranges, honouring any pan/zoom override."""
        if self._view is not None:
            return self._view.get("x"), self._view.get(_LEFT), self._view.get(_RIGHT)
        xr = self._x_range()
        left = self._y_range(_LEFT)
        right = self._y_range(_RIGHT) if self._y2_enabled else None
        return xr, left, right

    def _has_data(self) -> bool:
        """True if anything is drawable."""
        return bool(self._series or self._fills or self._hlines)

    # ================================================================ drawing
    def _draw_plot(self, painter, rect: QRectF) -> None:
        """Draw axes, grid, series, fills, hlines and legend inside ``rect``."""
        if not self._has_data():
            self._draw_center_message(painter, rect, "No data")
            return

        xr, left_r, right_r = self._effective_ranges()
        if xr is None or (left_r is None and right_r is None):
            self._draw_center_message(painter, rect, "No data")
            return

        fg = self._fg()
        tick_font = self._tick_font()
        label_font = self._label_font()
        fm_tick = QFontMetrics(tick_font)
        fm_label = QFontMetrics(label_font)
        tick_h = fm_tick.height()
        label_h = fm_label.height()
        gap = 4.0

        # --- Ticks (compute early so margins can fit their labels).
        x_ticks = nice_ticks(xr[0], xr[1])
        x_labels, x_step = _fmt_ticks(x_ticks)
        left_ticks = nice_ticks(left_r[0], left_r[1]) if left_r else []
        left_labels, left_step = _fmt_ticks(left_ticks)
        right_ticks = nice_ticks(right_r[0], right_r[1]) if right_r else []
        right_labels, right_step = _fmt_ticks(right_ticks)

        left_lbl_w = max((fm_tick.horizontalAdvance(t) for t in left_labels), default=0.0)
        right_lbl_w = max((fm_tick.horizontalAdvance(t) for t in right_labels), default=0.0)
        x_first_w = fm_tick.horizontalAdvance(x_labels[0]) if x_labels else 0.0
        x_last_w = fm_tick.horizontalAdvance(x_labels[-1]) if x_labels else 0.0

        # --- Margins inside rect.
        pad_left = left_lbl_w + gap * 2 + (label_h if self._y_label else 0.0) + 2.0
        pad_left = max(pad_left, x_first_w / 2.0 + 2.0)
        if right_r is not None:
            pad_right = right_lbl_w + gap * 2 + (label_h if self._y2_label else 0.0) + 2.0
        else:
            pad_right = x_last_w / 2.0 + 2.0
        pad_bottom = tick_h + gap + (label_h if self._x_label else 0.0) + 2.0
        pad_top = tick_h / 2.0 + 2.0

        plot_left = rect.left() + pad_left
        plot_right = rect.right() - pad_right
        plot_top = rect.top() + pad_top
        plot_bottom = rect.bottom() - pad_bottom
        if plot_right - plot_left < 4.0 or plot_bottom - plot_top < 4.0:
            return  # too small to draw meaningfully
        plot_area = QRectF(plot_left, plot_top, plot_right - plot_left, plot_bottom - plot_top)

        # --- Scales.
        sx = LinearScale(xr[0], xr[1], plot_left, plot_right)
        sy_left = LinearScale(left_r[0], left_r[1], plot_bottom, plot_top) if left_r else None
        sy_right = LinearScale(right_r[0], right_r[1], plot_bottom, plot_top) if right_r else None
        # The gridded/primary vertical axis is the left one if present.
        grid_ticks = left_ticks if left_r else right_ticks
        grid_scale = sy_left if sy_left is not None else sy_right

        # Cache for interaction handlers.
        self._plot_rect = plot_area
        self._scale_x = sx
        self._scale_y = {_LEFT: sy_left, _RIGHT: sy_right}

        # --- Grid (dashed, low-alpha).
        grid_pen = QPen(self._grid())
        grid_pen.setWidthF(1.0)
        grid_pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(grid_pen)
        for t in x_ticks:
            px = sx.px(t)
            painter.drawLine(QPointF(px, plot_top), QPointF(px, plot_bottom))
        if grid_scale is not None:
            for t in grid_ticks:
                py = grid_scale.px(t)
                painter.drawLine(QPointF(plot_left, py), QPointF(plot_right, py))

        # --- Data (clipped to the plot area).
        painter.save()
        painter.setClipRect(plot_area)
        for f in self._fills:
            self._draw_fill(painter, f, sx, sy_left, sy_right)
        for h in self._hlines:
            self._draw_hline(painter, h, plot_left, plot_right, sy_left, sy_right)
        for s in self._series:
            self._draw_series(painter, s, sx, sy_left, sy_right)
        painter.restore()

        # --- Axis spines.
        axis_pen = QPen(fg)
        axis_pen.setWidthF(1.2)
        painter.setPen(axis_pen)
        painter.drawLine(QPointF(plot_left, plot_top), QPointF(plot_left, plot_bottom))
        painter.drawLine(QPointF(plot_left, plot_bottom), QPointF(plot_right, plot_bottom))
        if right_r is not None:
            painter.drawLine(QPointF(plot_right, plot_top), QPointF(plot_right, plot_bottom))

        # --- Tick marks + labels.
        painter.setFont(tick_font)
        for t, lbl in zip(x_ticks, x_labels):
            px = sx.px(t)
            painter.setPen(axis_pen)
            painter.drawLine(QPointF(px, plot_bottom), QPointF(px, plot_bottom + 3.0))
            self._text(
                painter,
                px,
                plot_bottom + gap,
                lbl,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                self._muted(),
            )
        if sy_left is not None:
            lcol = resolve_color(self._y_label_color) if self._y_label_color is not None else self._muted()
            for t, lbl in zip(left_ticks, left_labels):
                py = sy_left.px(t)
                painter.setPen(axis_pen)
                painter.drawLine(QPointF(plot_left - 3.0, py), QPointF(plot_left, py))
                self._text(
                    painter,
                    plot_left - gap,
                    py,
                    lbl,
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    lcol,
                )
        if sy_right is not None:
            rcol = resolve_color(self._y2_label_color) if self._y2_label_color is not None else self._muted()
            for t, lbl in zip(right_ticks, right_labels):
                py = sy_right.px(t)
                painter.setPen(axis_pen)
                painter.drawLine(QPointF(plot_right, py), QPointF(plot_right + 3.0, py))
                self._text(
                    painter,
                    plot_right + gap,
                    py,
                    lbl,
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    rcol,
                )

        # --- Axis titles.
        painter.setFont(label_font)
        if self._x_label:
            self._text(
                painter,
                (plot_left + plot_right) / 2.0,
                plot_bottom + tick_h + gap,
                self._x_label,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                fg,
            )
        if self._y_label:
            col = resolve_color(self._y_label_color) if self._y_label_color is not None else fg
            self._draw_vertical_label(
                painter, rect.left() + label_h - 2.0, (plot_top + plot_bottom) / 2.0, self._y_label, col
            )
        if self._y2_label and right_r is not None:
            col = resolve_color(self._y2_label_color) if self._y2_label_color is not None else fg
            # Rotated -90, the glyphs extend LEFT of the baseline by the ascent;
            # baseline at the far right keeps them inside the reserved strip
            # (MPT's ``rect.right() - label_h + 2`` overlapped the tick labels).
            self._draw_vertical_label(
                painter, rect.right() - fm_label.descent() - 1.0, (plot_top + plot_bottom) / 2.0, self._y2_label, col
            )

        # --- Legend.
        if self._legend_visible:
            self._draw_legend(painter, plot_area)

    # ------------------------------------------------------------ draw helpers
    def _points(self, x: np.ndarray, y: np.ndarray, sx: LinearScale, sy: LinearScale) -> QPolygonF:
        """Build a ``QPolygonF`` of pixel points from data arrays."""
        poly = QPolygonF()
        for xv, yv in zip(x, y):
            poly.append(QPointF(sx.px(xv), sy.px(yv)))
        return poly

    def _y_scale_for(self, axis: str, sy_left, sy_right) -> Optional[LinearScale]:
        """Pick the pixel scale for ``axis`` (falls back to the other if missing)."""
        if axis == _RIGHT:
            return sy_right if sy_right is not None else sy_left
        return sy_left if sy_left is not None else sy_right

    def _draw_series(self, painter, s: Dict, sx, sy_left, sy_right) -> None:
        """Draw one line series (and its markers)."""
        sy = self._y_scale_for(s["axis"], sy_left, sy_right)
        if sy is None:
            return
        color = resolve_color(s["color"], s["alpha"])
        pen = QPen(color)
        pen.setWidthF(max(0.5, s["width"]))
        pen.setStyle(_pen_style(s["style"]))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        poly = self._points(s["x"], s["y"], sx, sy)
        if poly.count() > 1:
            painter.drawPolyline(poly)
        elif poly.count() == 1:
            painter.drawPoint(poly.at(0))
        if s["markers"]:
            painter.setBrush(QBrush(color))
            painter.setPen(Qt.PenStyle.NoPen)
            radius = max(2.0, s["width"] + 1.5)
            for i in range(poly.count()):
                painter.drawEllipse(poly.at(i), radius, radius)

    def _draw_fill(self, painter, f: Dict, sx, sy_left, sy_right) -> None:
        """Draw one filled band as a single closed polygon."""
        sy = self._y_scale_for(f["axis"], sy_left, sy_right)
        if sy is None:
            return
        poly = QPolygonF()
        for xv, yv in zip(f["x"], f["hi"]):
            poly.append(QPointF(sx.px(xv), sy.px(yv)))
        for xv, yv in zip(reversed(list(f["x"])), reversed(list(f["lo"]))):
            poly.append(QPointF(sx.px(xv), sy.px(yv)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(resolve_color(f["color"], f["alpha"])))
        painter.drawPolygon(poly)

    def _draw_hline(self, painter, h: Dict, plot_left, plot_right, sy_left, sy_right) -> None:
        """Draw one horizontal reference line."""
        sy = self._y_scale_for(h["axis"], sy_left, sy_right)
        if sy is None:
            return
        py = sy.px(h["y"])
        pen = QPen(resolve_color(h["color"], h["alpha"]))
        pen.setWidthF(max(0.5, h["width"]))
        pen.setStyle(_pen_style(h["style"]))
        painter.setPen(pen)
        painter.drawLine(QPointF(plot_left, py), QPointF(plot_right, py))

    def _draw_vertical_label(self, painter, x: float, cy: float, text: str, color) -> None:
        """Draw a rotated (bottom-to-top) axis title centred vertically at ``cy``."""
        painter.save()
        painter.translate(x, cy)
        painter.rotate(-90.0)
        fm = QFontMetrics(painter.font())
        w = fm.horizontalAdvance(text)
        painter.setPen(color)
        painter.drawText(QPointF(-w / 2.0, 0.0), text)
        painter.restore()

    def _legend_items(self) -> List[Dict]:
        """Collect labelled primitives for the legend, in a stable order."""
        items: List[Dict] = []
        for f in self._fills:
            if f["label"]:
                items.append({"label": f["label"], "color": f["color"], "alpha": f["alpha"], "kind": "patch"})
        for s in self._series:
            if s["label"]:
                items.append(
                    {
                        "label": s["label"],
                        "color": s["color"],
                        "alpha": s["alpha"],
                        "kind": "line",
                        "style": s["style"],
                        "width": s["width"],
                    }
                )
        for h in self._hlines:
            if h["label"]:
                items.append(
                    {
                        "label": h["label"],
                        "color": h["color"],
                        "alpha": h["alpha"],
                        "kind": "line",
                        "style": h["style"],
                        "width": h["width"],
                    }
                )
        return items

    def _draw_legend(self, painter, plot_rect: QRectF) -> None:
        """Draw a small legend box in the configured corner."""
        items = self._legend_items()
        if not items:
            return
        painter.setFont(self._label_font())
        fm = QFontMetrics(painter.font())
        line_h = fm.height()
        swatch = 18.0
        padx, pady, inner = 6.0, 4.0, 6.0
        text_w = max(fm.horizontalAdvance(it["label"]) for it in items)
        box_w = padx * 2 + swatch + inner + text_w
        box_h = pady * 2 + line_h * len(items)

        loc = str(self._legend_loc).lower()
        inset = 6.0
        x = plot_rect.right() - box_w - inset if "right" in loc else plot_rect.left() + inset
        y = plot_rect.top() + inset if "upper" in loc or "top" in loc else plot_rect.bottom() - box_h - inset

        bg = QColor(self._bg())
        bg.setAlpha(220)
        painter.setBrush(QBrush(bg))
        painter.setPen(QPen(self._muted(), 1.0))
        painter.drawRect(QRectF(x, y, box_w, box_h))

        for i, it in enumerate(items):
            row_cy = y + pady + i * line_h + line_h / 2.0
            col = resolve_color(it["color"], it.get("alpha", 1.0))
            sx0 = x + padx
            if it["kind"] == "patch":
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(col))
                painter.drawRect(QRectF(sx0, row_cy - 4.0, swatch, 8.0))
            else:
                pen = QPen(col)
                pen.setWidthF(max(1.0, it.get("width", 1.0)))
                pen.setStyle(_pen_style(it.get("style", "solid")))
                painter.setPen(pen)
                painter.drawLine(QPointF(sx0, row_cy), QPointF(sx0 + swatch, row_cy))
            self._text(
                painter,
                sx0 + swatch + inner,
                row_cy,
                it["label"],
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                self._fg(),
            )

    # ============================================================ interaction
    # Wheel = zoom about the cursor, left-drag = pan, double-click = reset.
    # All handlers are guarded so a bad interaction never corrupts the chart;
    # the static render path never depends on ``self._view`` being set.
    def _event_pos(self, event) -> Optional[Tuple[float, float]]:
        """Return the event's widget-pixel position, dual-Qt safe."""
        pos = getattr(event, "position", None)
        try:
            if pos is not None:
                p = event.position()  # QPointF (Qt6)
                return float(p.x()), float(p.y())
            p = event.pos()  # QPoint (Qt5)
            return float(p.x()), float(p.y())
        except Exception:
            return None

    def _current_ranges_dict(self) -> Dict[str, Optional[Tuple[float, float]]]:
        """Snapshot the current effective ranges into a view dict."""
        xr, left_r, right_r = self._effective_ranges()
        return {"x": xr, _LEFT: left_r, _RIGHT: right_r}

    def wheelEvent(self, event) -> None:  # noqa: D401 - Qt override
        """Zoom both axes about the cursor."""
        with contextlib.suppress(Exception):
            if self._scale_x is None:
                return
            pos = self._event_pos(event)
            if pos is None:
                return
            delta = event.angleDelta().y()
            factor = 0.8 if delta > 0 else 1.25
            self._zoom_about(pos[0], pos[1], factor)
            self.update()

    def _zoom_about(self, px: float, py: float, factor: float) -> None:
        """Scale each axis range by ``factor`` around the data point under (px, py)."""
        view = self._current_ranges_dict()
        new_view: Dict[str, Optional[Tuple[float, float]]] = {"x": None, _LEFT: None, _RIGHT: None}
        if view["x"] is not None:
            new_view["x"] = _zoom_interval(view["x"], self._scale_x.data(px), factor)
        for axis in (_LEFT, _RIGHT):
            scale = self._scale_y.get(axis)
            if view[axis] is not None and scale is not None:
                new_view[axis] = _zoom_interval(view[axis], scale.data(py), factor)
        self._view = new_view

    def mousePressEvent(self, event) -> None:  # noqa: D401 - Qt override
        """Begin a pan on left-button press."""
        try:
            if event.button() == Qt.MouseButton.LeftButton and self._scale_x is not None:
                self._pan_anchor = self._event_pos(event)
                self._pan_start_view = self._current_ranges_dict()
                self._pan_scale_x = self._scale_x
                self._pan_scale_y = dict(self._scale_y)
        except Exception:
            self._pan_anchor = None

    def mouseMoveEvent(self, event) -> None:  # noqa: D401 - Qt override
        """Translate the view while dragging."""
        with contextlib.suppress(Exception):
            if self._pan_anchor is None or self._pan_scale_x is None or self._pan_start_view is None:
                return
            pos = self._event_pos(event)
            if pos is None:
                return
            dx_data = self._pan_scale_x.data(pos[0]) - self._pan_scale_x.data(self._pan_anchor[0])
            new_view: Dict[str, Optional[Tuple[float, float]]] = {"x": None, _LEFT: None, _RIGHT: None}
            xr = self._pan_start_view["x"]
            if xr is not None:
                new_view["x"] = (xr[0] - dx_data, xr[1] - dx_data)
            for axis in (_LEFT, _RIGHT):
                scale = self._pan_scale_y.get(axis)
                rng = self._pan_start_view[axis]
                if scale is not None and rng is not None:
                    dy_data = scale.data(pos[1]) - scale.data(self._pan_anchor[1])
                    new_view[axis] = (rng[0] - dy_data, rng[1] - dy_data)
            self._view = new_view
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: D401 - Qt override
        """End a pan."""
        self._pan_anchor = None
        self._pan_start_view = None

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: D401 - Qt override
        """Reset zoom/pan back to autorange."""
        self._view = None
        self.update()


def _fmt_ticks(ticks: List[float]) -> Tuple[List[str], float]:
    """Return (formatted labels, tick step) for a list of tick values."""
    if len(ticks) >= 2:
        step = ticks[1] - ticks[0]
    elif ticks:
        step = abs(ticks[0]) or 1.0
    else:
        step = 1.0
    return [tick_format(t, step) for t in ticks], step


def _zoom_interval(interval: Tuple[float, float], center: float, factor: float) -> Tuple[float, float]:
    """Scale ``interval`` about ``center`` by ``factor`` (``<1`` zooms in)."""
    lo, hi = interval
    return center - (center - lo) * factor, center + (hi - center) * factor
