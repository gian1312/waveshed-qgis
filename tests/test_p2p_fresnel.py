"""Fresnel zone geometry for the P2P profile plot.

The radius formula is unit-sensitive in a way that fails silently: the wrong
constant still produces a plausible-looking band, just far too narrow. These
tests pin it against hand-computed values.
"""

from __future__ import annotations

import math

import pytest

from waveshed.gui.p2p_tab import FRESNEL_CLEARANCE, fresnel_radius_m


class TestFresnelRadius:
    def test_midpath_40km_100mhz(self):
        """40 km at 100 MHz is 173.2 m at midpath, not 5.5 m.

        5.5 m is what the GHz constant (17.32) yields when handed MHz — the
        exact defect this pins against.
        """
        assert fresnel_radius_m(20.0, 20.0, 100.0) == pytest.approx(173.2, abs=0.1)

    def test_matches_first_principles(self):
        """Validate against F1 = sqrt(lambda*d1*d2/D) derived from c.

        Tolerance is 0.1%: the shipped 547.7 is the literature rounding of
        sqrt(c/1000) = 547.5331, which is 0.03% high. That is far tighter than
        any unit slip — a MHz/GHz mix-up is off by 3162%.
        """
        C = 299792458.0
        d1 = d2 = 5.0
        for f_mhz in (100.0, 145.0, 446.0, 868.0, 2400.0, 5800.0):
            lam_m = C / (f_mhz * 1e6)
            d1_m, d2_m = d1 * 1000.0, d2 * 1000.0
            exact = math.sqrt(lam_m * d1_m * d2_m / (d1_m + d2_m))
            assert fresnel_radius_m(d1, d2, f_mhz) == pytest.approx(exact, rel=1e-3)

    def test_gigahertz_constant_would_be_wrong(self):
        """Guard the exact regression: 17.32 with MHz is 31.6x too small."""
        wrong = 17.32 * math.sqrt(20.0 * 20.0 / (100.0 * 40.0))
        assert wrong == pytest.approx(5.48, abs=0.05)
        assert fresnel_radius_m(20.0, 20.0, 100.0) / wrong == pytest.approx(
            math.sqrt(1000), rel=1e-3
        )

    def test_scales_as_inverse_sqrt_frequency(self):
        """Quadrupling frequency halves the radius."""
        low = fresnel_radius_m(10.0, 10.0, 150.0)
        high = fresnel_radius_m(10.0, 10.0, 600.0)
        assert high == pytest.approx(low / 2.0, rel=1e-9)

    def test_widest_at_midpath(self):
        total = 30.0
        mid = fresnel_radius_m(total / 2, total / 2, 500.0)
        for d1 in (1.0, 5.0, 10.0, 14.0, 20.0, 29.0):
            assert fresnel_radius_m(d1, total - d1, 500.0) <= mid + 1e-9

    def test_symmetric_about_midpath(self):
        total = 30.0
        for d1 in (1.0, 7.5, 12.0):
            assert fresnel_radius_m(d1, total - d1, 300.0) == pytest.approx(
                fresnel_radius_m(total - d1, d1, 300.0), rel=1e-12
            )

    @pytest.mark.parametrize(
        "d1,d2,f",
        [(0.0, 10.0, 100.0), (10.0, 0.0, 100.0), (-1.0, 10.0, 100.0),
         (10.0, 10.0, 0.0), (10.0, 10.0, -5.0)],
    )
    def test_degenerate_inputs_are_zero_not_an_error(self, d1, d2, f):
        """Endpoints and an unset frequency must not raise in a paint path."""
        assert fresnel_radius_m(d1, d2, f) == 0.0


class TestClearanceFraction:
    def test_is_sixty_percent(self):
        assert FRESNEL_CLEARANCE == pytest.approx(0.6)

    def test_clearance_band_is_inside_the_full_zone(self):
        f1 = fresnel_radius_m(20.0, 20.0, 100.0)
        assert 0.0 < FRESNEL_CLEARANCE * f1 < f1
