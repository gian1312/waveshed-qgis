"""Ported from MPT SIGMA tests/unit (the plotting package is shared).

Smoke tests for gui/plotting cartesian & polar widgets.

conftest mocks all of Qt; its default ``QWidget`` stand-in lacks the instance
methods our widgets call in ``__init__`` (``setMinimumSize``) and setters
(``update``). Following the repo pattern (see test_vertical_button), we swap in
a richer ``_StubQWidget`` before importing the package so the widgets construct
for real, then assert on the STORED state (not merely "no exception").

paintEvent is never exercised under mocks (per the design spec).
"""

import sys
from unittest import mock

import numpy as np
import pytest



class _StubQWidget:
    """A real QWidget stand-in with no-op Qt methods, so BasePlotWidget.__init__
    and the ``self.update()`` calls in setters run without a live Qt."""

    def __init__(self, *args, **kwargs):
        pass

    def setMinimumSize(self, *args, **kwargs):
        pass

    def update(self, *args, **kwargs):
        pass

    def font(self):
        return mock.MagicMock()

    def palette(self):
        return mock.MagicMock()

    def rect(self):
        return mock.MagicMock()

    def width(self):
        return 480

    def height(self):
        return 360

    def grab(self):
        return mock.MagicMock()

    def mapToGlobal(self, p):
        return p


def _import_plotting():
    """Import the plotting package with the rich QWidget base installed."""
    qtw = sys.modules["qgis.PyQt.QtWidgets"]
    original = getattr(qtw, "QWidget", None)
    qtw.QWidget = _StubQWidget
    try:
        for name in list(sys.modules):
            if "gui.plotting" in name:
                del sys.modules[name]
        from waveshed.gui.plotting import (
            PolarPlotWidget,
            XYPlotWidget,
        )
    finally:
        if original is not None:
            qtw.QWidget = original
    return XYPlotWidget, PolarPlotWidget


XYPlotWidget, PolarPlotWidget = _import_plotting()


# ===========================================================================
# XYPlotWidget
# ===========================================================================
class TestXYConfig:
    """Titles, labels and the secondary-axis toggle."""

    def test_construct_defaults(self):
        w = XYPlotWidget()
        assert w._series == []
        assert w._fills == []
        assert w._hlines == []
        assert w._y2_enabled is False
        assert w._legend_visible is False

    def test_set_title_and_labels(self):
        w = XYPlotWidget()
        w.set_title("Combined Link")
        w.set_x_label("Distance (km)")
        w.set_y_label("Elevation (m)", color="darkgreen")
        assert w._title == "Combined Link"
        assert w._x_label == "Distance (km)"
        assert w._y_label == "Elevation (m)"
        assert w._y_label_color == "darkgreen"

    def test_multiline_title_preserved(self):
        w = XYPlotWidget()
        w.set_title("Line one\nLine two")
        assert w._title.split("\n") == ["Line one", "Line two"]

    def test_set_y2_label_enables_secondary_axis(self):
        w = XYPlotWidget()
        assert w._y2_enabled is False
        w.set_y2_label("Received Signal (dBm)", color="red")
        assert w._y2_enabled is True
        assert w._y2_label == "Received Signal (dBm)"
        assert w._y2_label_color == "red"

    def test_methods_chain(self):
        w = XYPlotWidget()
        assert w.set_title("t") is w
        assert w.set_x_label("x") is w
        assert w.add_line([0, 1], [0, 1], color="red") is w


class TestXYAddLine:
    """add_line storage and validation."""

    def test_add_line_stores_series(self):
        w = XYPlotWidget()
        w.add_line([0, 1, 2], [3, 4, 5], color="darkgreen", width=1.5, style="dashed", label="Terrain")
        assert len(w._series) == 1
        s = w._series[0]
        assert s["color"] == "darkgreen"
        assert s["width"] == 1.5
        assert s["style"] == "dashed"
        assert s["label"] == "Terrain"
        assert s["axis"] == "left"
        assert s["markers"] is False
        assert s["x"].tolist() == [0.0, 1.0, 2.0]
        assert s["y"].tolist() == [3.0, 4.0, 5.0]

    def test_add_line_accepts_numpy(self):
        w = XYPlotWidget()
        w.add_line(np.arange(4), np.linspace(0, 1, 4), color="blue")
        assert w._series[0]["x"].tolist() == [0.0, 1.0, 2.0, 3.0]

    def test_add_line_right_axis_enables_secondary(self):
        w = XYPlotWidget()
        w.add_line([0, 1], [10, 20], color="red", axis="right")
        assert w._series[0]["axis"] == "right"
        assert w._y2_enabled is True

    def test_add_line_markers_flag(self):
        w = XYPlotWidget()
        w.add_line([0, 1, 2], [1, 2, 3], color="blue", markers=True)
        assert w._series[0]["markers"] is True

    def test_add_line_mismatched_lengths_skipped(self):
        w = XYPlotWidget()
        w.add_line([0, 1, 2], [0, 1], color="red")
        assert w._series == []

    def test_add_line_empty_skipped(self):
        w = XYPlotWidget()
        w.add_line([], [], color="red")
        assert w._series == []


class TestXYFill:
    """add_fill (fill_between) storage and broadcasting."""

    def test_add_fill_stores_band(self):
        w = XYPlotWidget()
        w.add_fill([0, 1, 2], [0, 0, 0], [1, 2, 3], color="green", alpha=0.4, label="Terrain")
        assert len(w._fills) == 1
        f = w._fills[0]
        assert f["alpha"] == 0.4
        assert f["label"] == "Terrain"
        assert f["lo"].tolist() == [0.0, 0.0, 0.0]
        assert f["hi"].tolist() == [1.0, 2.0, 3.0]

    def test_add_fill_broadcasts_scalar_bound(self):
        # matplotlib fill_between(x, y, scalar) form.
        w = XYPlotWidget()
        w.add_fill([0, 1, 2], 0.0, [5, 6, 7], color="green")
        f = w._fills[0]
        assert f["lo"].tolist() == [0.0, 0.0, 0.0]
        assert f["hi"].tolist() == [5.0, 6.0, 7.0]

    def test_add_fill_bad_length_skipped(self):
        w = XYPlotWidget()
        w.add_fill([0, 1, 2], [0, 0], [1, 1], color="green")
        assert w._fills == []

    def test_add_fill_empty_x_skipped(self):
        w = XYPlotWidget()
        w.add_fill([], 0.0, 1.0, color="green")
        assert w._fills == []


class TestXYHLine:
    """add_hline storage."""

    def test_add_hline_stores(self):
        w = XYPlotWidget()
        w.add_hline(-110.0, color="gray", style="dashed", alpha=0.8, axis="right", label="Sensitivity")
        assert len(w._hlines) == 1
        h = w._hlines[0]
        assert h["y"] == -110.0
        assert h["axis"] == "right"
        assert h["alpha"] == 0.8
        assert h["label"] == "Sensitivity"
        assert w._y2_enabled is True


class TestXYRanges:
    """Autorange and manual limits."""

    def test_x_range_padded_union(self):
        w = XYPlotWidget()
        w.add_line([0, 10], [1, 2], color="red")
        w.add_line([-5, 5], [1, 2], color="blue")
        lo, hi = w._x_range()
        # union is [-5, 10]; padded 5% of span (15) -> 0.75 each side.
        assert lo == pytest.approx(-5.75)
        assert hi == pytest.approx(10.75)

    def test_x_range_none_when_empty(self):
        w = XYPlotWidget()
        assert w._x_range() is None

    def test_y_range_left_autorange(self):
        w = XYPlotWidget()
        w.add_line([0, 1], [5, 15], color="red", axis="left")
        lo, hi = w._y_range("left")
        assert lo == pytest.approx(4.5)
        assert hi == pytest.approx(15.5)

    def test_y_range_separates_axes(self):
        w = XYPlotWidget()
        w.add_line([0, 1], [0, 100], color="red", axis="left")
        w.add_line([0, 1], [-120, -80], color="blue", axis="right")
        assert w._y_range("left")[0] == pytest.approx(-5.0)
        assert w._y_range("right")[1] == pytest.approx(-78.0)

    def test_set_y_limits_overrides_autorange(self):
        w = XYPlotWidget()
        w.add_line([0, 1], [5, 15], color="red")
        w.set_y_limits(0.0, 100.0, axis="left")
        assert w._y_range("left") == (0.0, 100.0)

    def test_hline_included_in_range(self):
        w = XYPlotWidget()
        w.add_line([0, 1], [0, 10], color="red", axis="right")
        w.add_hline(50.0, color="gray", axis="right")
        assert w._y_range("right")[1] >= 50.0

    def test_effective_ranges_uses_autorange_without_view(self):
        w = XYPlotWidget()
        w.add_line([0, 10], [0, 5], color="red")
        xr, left, right = w._effective_ranges()
        assert xr is not None and left is not None
        assert right is None  # secondary not enabled

    def test_has_data(self):
        w = XYPlotWidget()
        assert w._has_data() is False
        w.add_line([0, 1], [0, 1], color="red")
        assert w._has_data() is True


class TestXYLegendAndMessage:
    def test_set_legend(self):
        w = XYPlotWidget()
        w.set_legend(True, loc="upper left")
        assert w._legend_visible is True
        assert w._legend_loc == "upper left"

    def test_legend_items_collects_labels_only(self):
        w = XYPlotWidget()
        w.add_line([0, 1], [0, 1], color="red", label="A")
        w.add_line([0, 1], [1, 2], color="blue")  # no label
        w.add_fill([0, 1], 0.0, [1, 1], color="green", label="Band")
        items = w._legend_items()
        labels = [it["label"] for it in items]
        assert labels == ["Band", "A"]  # fills first, then labelled series

    def test_set_message_and_clear(self):
        w = XYPlotWidget()
        w.set_message("No Terrain Data")
        assert w._message == "No Terrain Data"
        w.set_message(None)
        assert w._message is None


class TestXYInteractionHelpers:
    """Pure interaction helpers used by the (guarded) mouse handlers."""

    def test_zoom_interval_zooms_in(self):
        from waveshed.gui.plotting.cartesian import _zoom_interval

        lo, hi = _zoom_interval((0.0, 10.0), 5.0, 0.5)
        assert (lo, hi) == pytest.approx((2.5, 7.5))

    def test_zoom_interval_zooms_out(self):
        from waveshed.gui.plotting.cartesian import _zoom_interval

        lo, hi = _zoom_interval((0.0, 10.0), 5.0, 2.0)
        assert (lo, hi) == pytest.approx((-5.0, 15.0))

    def test_fmt_ticks(self):
        from waveshed.gui.plotting.cartesian import _fmt_ticks

        labels, step = _fmt_ticks([0.0, 0.5, 1.0])
        assert labels == ["0", "0.5", "1"]
        assert step == pytest.approx(0.5)

    def test_norm_axis(self):
        from waveshed.gui.plotting.cartesian import _norm_axis

        assert _norm_axis("right") == "right"
        assert _norm_axis("left") == "left"
        assert _norm_axis("anything") == "left"

    def test_double_click_resets_view(self):
        w = XYPlotWidget()
        w._view = {"x": (0, 1), "left": (0, 1), "right": None}
        w.mouseDoubleClickEvent(mock.MagicMock())
        assert w._view is None

    def test_interaction_hint_present(self):
        # The XY chart advertises its built-in pan/zoom/reset so users can find it.
        w = XYPlotWidget()
        assert w._hint
        assert "reset" in w._hint.lower()


# ===========================================================================
# PolarPlotWidget
# ===========================================================================
class TestPolarConfig:
    def test_construct_defaults(self):
        w = PolarPlotWidget()
        assert w._theta is None
        assert w._r is None
        assert w._theta_zero == "E"
        assert w._direction == "ccw"
        assert w._theta_range is None
        # No pan/zoom on the polar chart, so it must not show the interaction hint.
        assert w._hint is None

    def test_set_series_stores(self):
        w = PolarPlotWidget()
        w.set_series([0, 90, 180, 270], [1, 0.5, 1, 0.5], color="b", width=2)
        assert w._theta.tolist() == [0.0, 90.0, 180.0, 270.0]
        assert w._r.tolist() == [1.0, 0.5, 1.0, 0.5]
        assert w._color == "b"
        assert w._width == 2.0

    def test_set_series_mismatched_lengths_ignored(self):
        w = PolarPlotWidget()
        w.set_series([0, 90], [1], color="r")
        assert w._theta is None

    def test_methods_chain(self):
        w = PolarPlotWidget()
        assert w.set_title("Azimuth") is w
        assert w.set_theta_zero("N") is w
        assert w.set_r_max(1.0) is w

    def test_set_theta_zero_valid(self):
        w = PolarPlotWidget()
        w.set_theta_zero("N")
        assert w._theta_zero == "N"

    def test_set_theta_zero_invalid_ignored(self):
        w = PolarPlotWidget()
        w.set_theta_zero("Q")
        assert w._theta_zero == "E"  # unchanged

    def test_set_theta_direction(self):
        w = PolarPlotWidget()
        w.set_theta_direction("cw")
        assert w._direction == "cw"
        w.set_theta_direction("ccw")
        assert w._direction == "ccw"

    def test_set_theta_range(self):
        w = PolarPlotWidget()
        w.set_theta_range(-90, 90)
        assert w._theta_range == (-90.0, 90.0)

    def test_set_r_max_and_ticks(self):
        w = PolarPlotWidget()
        w.set_r_max(1.0)
        w.set_r_ticks([0.25, 0.5, 0.75, 1.0])
        assert w._r_max == 1.0
        assert w._r_ticks == [0.25, 0.5, 0.75, 1.0]

    def test_set_message(self):
        w = PolarPlotWidget()
        w.set_message("No Data to Display")
        assert w._message == "No Data to Display"


class TestPolarOrientation:
    """The angle math that positions the trace."""

    def test_default_zero_east_ccw(self):
        w = PolarPlotWidget()
        assert w._phi_deg(0) == pytest.approx(0.0)
        assert w._phi_deg(90) == pytest.approx(90.0)

    def test_azimuth_north_clockwise(self):
        # Antenna azimuth: 0 deg = North (up), clockwise.
        w = PolarPlotWidget()
        w.set_theta_zero("N")
        w.set_theta_direction("cw")
        assert w._phi_deg(0) == pytest.approx(90.0)  # up
        assert w._phi_deg(90) == pytest.approx(0.0)  # compass-E to the right

    def test_elevation_east_ccw(self):
        # Antenna elevation: 0 = East (right), +90 up, -90 down.
        w = PolarPlotWidget()
        w.set_theta_zero("E")
        w.set_theta_direction("ccw")
        assert w._phi_deg(-90) == pytest.approx(-90.0)  # down
        assert w._phi_deg(90) == pytest.approx(90.0)  # up

    def test_r_max_value_from_data(self):
        w = PolarPlotWidget()
        w.set_series([0, 1, 2], [0.5, 1.0, 2.0], color="b")
        assert w._r_max_value() == pytest.approx(2.0)

    def test_r_max_value_explicit(self):
        w = PolarPlotWidget()
        w.set_series([0, 1, 2], [0.5, 1.0, 2.0], color="b")
        w.set_r_max(1.0)
        assert w._r_max_value() == pytest.approx(1.0)

    def test_r_max_value_default(self):
        w = PolarPlotWidget()
        assert w._r_max_value() == pytest.approx(1.0)

    def test_sweep_full_and_partial(self):
        w = PolarPlotWidget()
        assert w._sweep() == (0.0, 360.0)
        w.set_theta_range(90, -90)  # reversed input
        assert w._sweep() == (-90.0, 90.0)


class TestPolarHelpers:
    def test_default_r_ticks(self):
        from waveshed.gui.plotting.polar import _default_r_ticks

        assert _default_r_ticks(1.0) == [0.25, 0.5, 0.75, 1.0]

    def test_spoke_angles_full_circle(self):
        from waveshed.gui.plotting.polar import _spoke_angles

        angles = _spoke_angles(0, 360, True)
        assert angles == [0, 30, 60, 90, 120, 150, 180, 210, 240, 270, 300, 330]

    def test_spoke_angles_semicircle(self):
        from waveshed.gui.plotting.polar import _spoke_angles

        angles = _spoke_angles(-90, 90, False)
        assert angles == [-90.0, -60.0, -30.0, 0.0, 30.0, 60.0, 90.0]

    def test_fmt_r(self):
        from waveshed.gui.plotting.polar import _fmt_r

        assert _fmt_r(0.25) == "0.25"
        assert _fmt_r(0.5) == "0.5"
        assert _fmt_r(1.0) == "1"
