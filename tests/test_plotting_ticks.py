"""Ported from MPT SIGMA tests/unit (the plotting package is shared).

Unit tests for gui/plotting/ticks.py — nice-number tick math.

The module is loaded in isolation (via importlib, straight from its file) so
these tests exercise it with NO Qt imported at all, proving it stays pure.
"""

import importlib.util
import os

import pytest


_PLOT_DIR = os.path.join(os.path.dirname(__file__), "..", "waveshed", "gui", "plotting")


def _load_pure(name):
    """Import a plotting sub-module directly from its file, bypassing the
    package __init__ (which would pull in Qt)."""
    path = os.path.join(_PLOT_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("plotting_pure_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ticks = _load_pure("ticks")
nice_ticks = ticks.nice_ticks
tick_format = ticks.tick_format


def _approx(values):
    return pytest.approx(values, abs=1e-9)


class TestNiceTicks:
    """Exact tick sequences for the 1/2/5 progression."""

    def test_zero_to_hundred(self):
        assert nice_ticks(0, 100) == _approx([0, 20, 40, 60, 80, 100])

    def test_zero_to_ten(self):
        assert nice_ticks(0, 10) == _approx([0, 2, 4, 6, 8, 10])

    def test_zero_to_one_uses_fifth_steps(self):
        assert nice_ticks(0, 1) == _approx([0.0, 0.2, 0.4, 0.6, 0.8, 1.0])

    def test_negative_range(self):
        assert nice_ticks(-30, -5) == _approx([-30, -25, -20, -15, -10, -5])

    def test_reversed_input_is_swapped(self):
        assert nice_ticks(100, 0) == nice_ticks(0, 100)

    def test_flat_nonzero_series_is_padded(self):
        # vmin == vmax != 0 -> padded by 5% -> [4.75, 5.25] -> 0.1 steps.
        assert nice_ticks(5, 5) == _approx([4.8, 4.9, 5.0, 5.1, 5.2])

    def test_flat_zero_series_is_padded(self):
        assert nice_ticks(0, 0) == _approx([-0.4, -0.2, 0.0, 0.2, 0.4])

    def test_target_controls_density(self):
        result = nice_ticks(0, 100, target=10)
        assert result == _approx([0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
        assert len(result) == 11

    def test_all_ticks_cover_range(self):
        result = nice_ticks(3, 97)
        assert result[0] <= 3 + (result[1] - result[0])
        assert result[-1] >= 97 - (result[1] - result[0])

    def test_no_negative_zero(self):
        # 0.0 must be positive zero for clean labels.
        for t in nice_ticks(-1, 1):
            if t == 0:
                assert str(t) == "0.0"

    def test_non_finite_returns_empty(self):
        assert nice_ticks(float("nan"), 10) == []
        assert nice_ticks(0, float("inf")) == []

    def test_large_range(self):
        result = nice_ticks(0, 5000)
        assert result[0] == 0
        assert result[-1] == 5000
        # step should be a clean 1000.
        assert result[1] - result[0] == pytest.approx(1000.0)


class TestTickFormat:
    """Label formatting: minimal decimals, no trailing zeros."""

    def test_integer_step_drops_decimals(self):
        assert tick_format(100.0, 20.0) == "100"
        assert tick_format(0.0, 20.0) == "0"

    def test_half_step_one_decimal(self):
        assert tick_format(2.5, 0.5) == "2.5"

    def test_whole_value_on_fractional_step_has_no_point(self):
        assert tick_format(5.0, 0.5) == "5"

    def test_two_decimals(self):
        assert tick_format(3.14159, 0.05) == "3.14"

    def test_negative_zero_renders_as_zero(self):
        assert tick_format(-0.0, 0.2) == "0"
        assert tick_format(-0.0001, 1.0) == "0"

    def test_tenth_step(self):
        assert tick_format(0.3, 0.1) == "0.3"
        assert tick_format(1.0, 0.1) == "1"

    def test_negative_value(self):
        assert tick_format(-25.0, 5.0) == "-25"
