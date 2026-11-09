"""Shared scaffold for the QPainter plotting widgets.

``BasePlotWidget`` owns everything the concrete cartesian/polar widgets have in
common: palette-derived colours, derived fonts, the text-anchor helper, the
paint template (background, empty-state message, title), and the right-click
"Save Image..." context menu. Subclasses implement only :meth:`_draw_plot`.

Dual-Qt: imports go through the ``qgis.PyQt`` shim and use scoped enums only, so
one source runs on PyQt5 (QGIS 3) and PyQt6 (QGIS 4).
"""

import contextlib
from typing import Optional, Sequence, Union

from qgis.PyQt.QtCore import QPointF, QRectF, QSize, Qt
from qgis.PyQt.QtGui import QColor, QCursor, QFont, QFontMetrics, QPainter, QPalette
from qgis.PyQt.QtWidgets import QFileDialog, QMenu, QWidget

# Matplotlib single-letter colour codes, so call sites migrated verbatim from
# matplotlib (e.g. the antenna dialog's "b"/"r") still resolve to real colours.
_MPL_SHORT = {
    "b": "blue",
    "g": "green",
    "r": "red",
    "c": "cyan",
    "m": "magenta",
    "y": "yellow",
    "k": "black",
    "w": "white",
}

# A colour argument may be a QColor, an "#rrggbb"/CSS-name string, an mpl short
# code, or an (r, g, b[, a]) tuple with either 0-1 floats or 0-255 ints.
ColorLike = Union[QColor, str, Sequence[float]]


def resolve_color(color: Optional[ColorLike], alpha: float = 1.0) -> QColor:
    """Coerce a flexible colour argument into a ``QColor``.

    Accepts a ``QColor``, a Qt/CSS colour name or ``#rrggbb`` string, a
    matplotlib single-letter code, or an ``(r, g, b[, a])`` sequence (floats in
    0-1 or ints in 0-255). Unknown values fall back to a neutral grey rather
    than raising. ``alpha`` (0-1) is applied on top when < 1.
    """
    if isinstance(color, QColor):
        out = QColor(color)
    elif isinstance(color, (tuple, list)) and len(color) >= 3:
        vals = list(color)
        if max(vals[:3]) <= 1.0:
            rgb = [int(round(v * 255)) for v in vals[:3]]
        else:
            rgb = [int(v) for v in vals[:3]]
        out = QColor(rgb[0], rgb[1], rgb[2])
        if len(vals) >= 4:  # fold an explicit tuple alpha into the request
            a4 = vals[3] if vals[3] <= 1.0 else vals[3] / 255.0
            alpha = alpha * a4
    elif color is None:
        out = QColor(128, 128, 128)
    else:
        out = QColor(_MPL_SHORT.get(str(color), str(color)))
        if not out.isValid():
            out = QColor(128, 128, 128)

    a = max(0.0, min(1.0, float(alpha)))
    if a < 1.0:
        out.setAlpha(int(round(a * 255)))
    return out


class BasePlotWidget(QWidget):
    """Base class for the QPainter charts. Not used directly by callers."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """Initialise shared state and a sensible minimum size."""
        super().__init__(parent)
        self.setMinimumSize(240, 180)
        self._title: str = ""
        self._message: Optional[str] = None
        # Optional one-line interaction hint drawn muted along the bottom edge.
        # Subclasses that support mouse interaction set it (the polar chart, which
        # has none, leaves it None so no strip is reserved).
        self._hint: Optional[str] = None

    # ------------------------------------------------------------------ sizing
    def sizeHint(self) -> QSize:
        """Recommended default size."""
        return QSize(480, 360)

    # ---------------------------------------------------------- palette colours
    # Chart chrome is resolved from the widget palette so it tracks the host
    # theme automatically. Only data series carry explicit colours.
    def _bg(self) -> QColor:
        """Background colour (palette Base)."""
        return self.palette().color(QPalette.ColorRole.Base)

    def _fg(self) -> QColor:
        """Foreground colour for axes/labels (palette Text)."""
        return self.palette().color(QPalette.ColorRole.Text)

    def _grid(self) -> QColor:
        """Low-alpha foreground for gridlines."""
        c = QColor(self._fg())
        c.setAlpha(40)
        return c

    def _muted(self) -> QColor:
        """Mid-alpha foreground for secondary labels / empty-state text."""
        c = QColor(self._fg())
        c.setAlpha(140)
        return c

    # ------------------------------------------------------------------- fonts
    def _scaled_font(self, delta_pt: float = 0.0, bold: bool = False) -> QFont:
        """Copy the widget font, nudged by ``delta_pt`` points, so chart text
        scales with the host UI-scale setting."""
        f = QFont(self.font())
        ps = f.pointSizeF()
        if ps > 0:
            f.setPointSizeF(max(1.0, ps + delta_pt))
        f.setBold(bold)
        return f

    def _label_font(self) -> QFont:
        """Font for axis titles / legend text."""
        return self._scaled_font(0.0)

    def _title_font(self) -> QFont:
        """Bold, slightly larger font for the chart title."""
        return self._scaled_font(1.0, bold=True)

    def _tick_font(self) -> QFont:
        """Slightly smaller font for tick labels."""
        return self._scaled_font(-1.0)

    # ------------------------------------------------------------ text helpers
    def _text(
        self,
        painter: QPainter,
        x: float,
        y: float,
        text: str,
        align: "Qt.AlignmentFlag" = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
        color: Optional[QColor] = None,
    ) -> None:
        """Draw ``text`` anchored at ``(x, y)`` per ``align``.

        Uses ``QFontMetrics.horizontalAdvance`` (never ``.width``) for width so
        it is dual-Qt safe. This is the single text placement primitive used by
        every label in both subclasses.
        """
        if color is not None:
            painter.setPen(color)
        fm = QFontMetrics(painter.font())
        w = fm.horizontalAdvance(text)
        h = fm.height()
        ascent = fm.ascent()

        if align & Qt.AlignmentFlag.AlignHCenter:
            tx = x - w / 2.0
        elif align & Qt.AlignmentFlag.AlignRight:
            tx = x - w
        else:
            tx = x

        if align & Qt.AlignmentFlag.AlignVCenter:
            baseline = y - h / 2.0 + ascent
        elif align & Qt.AlignmentFlag.AlignBottom:
            baseline = y - h + ascent
        else:  # AlignTop
            baseline = y + ascent
        painter.drawText(QPointF(tx, baseline), text)

    def _draw_center_message(self, painter: QPainter, rect: QRectF, text: str) -> None:
        """Draw a (possibly multi-line) muted message centred in ``rect``."""
        painter.setFont(self._label_font())
        fm = QFontMetrics(painter.font())
        lines = text.split("\n")
        line_h = fm.height()
        top = rect.center().y() - line_h * len(lines) / 2.0
        for i, line in enumerate(lines):
            self._text(
                painter,
                rect.center().x(),
                top + i * line_h,
                line,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                self._muted(),
            )

    # ---------------------------------------------------------- shared config
    def set_title(self, text: str) -> "BasePlotWidget":
        """Set the chart title (``\\n`` splits into multiple centred lines)."""
        self._title = text or ""
        self.update()
        return self

    def set_message(self, text: Optional[str]) -> "BasePlotWidget":
        """Show ``text`` as a centred placeholder instead of a plot; ``None`` clears it."""
        self._message = text if text else None
        self.update()
        return self

    # -------------------------------------------------------------- paint flow
    def paintEvent(self, event) -> None:  # noqa: D401 - Qt override
        """Template method: background -> empty-state OR title + ``_draw_plot``."""
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            full = QRectF(self.rect())
            painter.fillRect(full, self._bg())

            if self._message:
                self._draw_center_message(painter, full, self._message)
                return

            pad = 8.0
            content = full.adjusted(pad, pad, -pad, -pad)

            if self._title:
                painter.setFont(self._title_font())
                fm = QFontMetrics(painter.font())
                line_h = fm.height()
                lines = self._title.split("\n")
                for i, line in enumerate(lines):
                    self._text(
                        painter,
                        content.center().x(),
                        content.top() + i * line_h,
                        line,
                        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                        self._fg(),
                    )
                used = line_h * len(lines) + 6.0
                content = QRectF(
                    content.left(),
                    content.top() + used,
                    content.width(),
                    content.height() - used,
                )

            if self._hint:
                # Reserve a bottom strip and draw the hint there, so it can never
                # overlap the plot the subclass draws into the reduced content.
                painter.setFont(self._tick_font())
                hint_h = QFontMetrics(painter.font()).height()
                self._text(
                    painter,
                    content.right(),
                    content.bottom() - hint_h,
                    self._hint,
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                    self._muted(),
                )
                content = QRectF(
                    content.left(),
                    content.top(),
                    content.width(),
                    content.height() - (hint_h + 4.0),
                )

            if content.width() > 2.0 and content.height() > 2.0:
                self._draw_plot(painter, content)
        finally:
            painter.end()

    def _draw_plot(self, painter: QPainter, rect: QRectF) -> None:
        """Draw the plot body inside ``rect``. Implemented by subclasses."""
        raise NotImplementedError

    # --------------------------------------------------------- save-image menu
    def contextMenuEvent(self, event) -> None:  # noqa: D401 - Qt override
        """Right-click menu offering "Save Image..." (replaces the mpl toolbar export)."""
        menu = QMenu(self)
        save_action = menu.addAction("Save Image...")
        chosen = menu.exec(QCursor.pos())
        if chosen is not None and chosen == save_action:
            self._save_image()

    def _save_image(self) -> None:
        """Grab the widget to a PNG chosen via a file dialog. Best-effort."""
        # Exporting is a convenience; never let it crash the host.
        with contextlib.suppress(Exception):
            path, _ = QFileDialog.getSaveFileName(self, "Save Image", "", "PNG Image (*.png)")
            if not path:
                return
            if not path.lower().endswith(".png"):
                path += ".png"
            self.grab().save(path)
