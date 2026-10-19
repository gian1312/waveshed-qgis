"""Ported from MPT SIGMA tests/unit (the plotting package is shared).

Unit tests for gui/plotting/transform.py — LinearScale.

Loaded in isolation (importlib) so it runs with NO Qt imported, proving the
module stays pure.
"""

import importlib.util
import os

import pytest


_PLOT_DIR = os.path.join(os.path.dirname(__file__), "..", "waveshed", "gui", "plotting")


def _load_pure(name):
    path = os.path.join(_PLOT_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("plotting_pure_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


transform = _load_pure("transform")
LinearScale = transform.LinearScale


class TestForwardMapping:
    """data -> pixel."""

    def test_endpoints_and_midpoint(self):
        s = LinearScale(0, 10, 0, 100)
        assert s.px(0) == pytest.approx(0.0)
        assert s.px(10) == pytest.approx(100.0)
        assert s.px(5) == pytest.approx(50.0)

    def test_quarter_point(self):
        s = LinearScale(0, 10, 0, 100)
        assert s.px(2.5) == pytest.approx(25.0)

    def test_negative_domain(self):
        s = LinearScale(-10, 10, 0, 200)
        assert s.px(-10) == pytest.approx(0.0)
        assert s.px(0) == pytest.approx(100.0)
        assert s.px(10) == pytest.approx(200.0)

    def test_inverted_pixel_axis(self):
        # Y axis: pmin=bottom(100), pmax=top(0).
        s = LinearScale(0, 10, 100, 0)
        assert s.px(0) == pytest.approx(100.0)
        assert s.px(10) == pytest.approx(0.0)
        assert s.px(5) == pytest.approx(50.0)

    def test_extrapolates_outside_domain(self):
        s = LinearScale(0, 10, 0, 100)
        assert s.px(15) == pytest.approx(150.0)
        assert s.px(-5) == pytest.approx(-50.0)


class TestInverseMapping:
    """pixel -> data."""

    def test_inverse_of_forward(self):
        s = LinearScale(0, 10, 0, 100)
        assert s.data(50) == pytest.approx(5.0)
        assert s.data(0) == pytest.approx(0.0)
        assert s.data(100) == pytest.approx(10.0)

    def test_inverted_axis_inverse(self):
        s = LinearScale(0, 10, 100, 0)
        assert s.data(100) == pytest.approx(0.0)
        assert s.data(0) == pytest.approx(10.0)

    def test_round_trip(self):
        s = LinearScale(-3.5, 42.0, 12.0, 480.0)
        for v in (-3.5, 0.0, 10.0, 21.0, 42.0):
            assert s.data(s.px(v)) == pytest.approx(v)


class TestDegenerate:
    """Degenerate domains/ranges must not divide by zero."""

    def test_degenerate_domain_maps_to_midpoint(self):
        s = LinearScale(5, 5, 0, 100)
        # No ZeroDivisionError; a flat domain maps to the pixel midpoint.
        assert s.px(5) == pytest.approx(50.0, abs=1e-6)

    def test_degenerate_domain_data_is_stable(self):
        s = LinearScale(5, 5, 0, 100)
        assert s.data(50) == pytest.approx(5.0, abs=1e-6)

    def test_degenerate_zero_domain(self):
        s = LinearScale(0, 0, 0, 100)
        assert s.px(0) == pytest.approx(50.0, abs=1e-6)

    def test_degenerate_pixel_range(self):
        s = LinearScale(0, 10, 50, 50)
        assert s.px(5) == pytest.approx(50.0)
        # Inverse of a collapsed pixel range returns the domain minimum.
        assert s.data(50) == pytest.approx(0.0)
