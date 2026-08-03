"""Unit tests for the propagation-model validity checks in ``core.job_builder``.

The limits mirror the ones ITM enforces internally (Splat NG
``itwom3.0.cpp:1250-1310``) and are kept in sync with
``aether_core::config::validate_model_inputs``. Neither engine surfaces ITM's
``kwx`` indicator in map mode, so these checks are the only thing standing
between a user and a silently out-of-range coverage map.

QGIS stubs provided by conftest.py.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import unittest

from waveshed.core.job_builder import (
    CAUTION,
    INVALID,
    CoverageParams,
    P2PParams,
    format_model_warnings,
    model_warnings,
)


def _for(param, warnings):
    return [w for w in warnings if w.parameter == param]


def _severities(param, warnings):
    return {w.severity for w in _for(param, warnings)}


class TestAntennaHeights(unittest.TestCase):
    def test_ground_level_receiver_is_invalid(self):
        w = model_warnings(CoverageParams(model="ITM", rx_height=0.0))
        self.assertIn(INVALID, _severities("RX height", w))

    def test_below_itm_validated_minimum_is_caution(self):
        w = model_warnings(CoverageParams(model="ITM", rx_height=0.75))
        self.assertEqual({CAUTION}, _severities("RX height", w))

    def test_two_metres_is_clean(self):
        w = model_warnings(CoverageParams(model="ITM", rx_height=2.0))
        self.assertEqual([], _for("RX height", w))

    def test_transmitter_is_checked_too(self):
        w = model_warnings(CoverageParams(model="ITM", tx_height=0.2))
        self.assertIn(INVALID, _severities("TX height", w))

    def test_above_validated_maximum_is_caution(self):
        w = model_warnings(CoverageParams(model="ITM", tx_height=1500.0))
        self.assertEqual({CAUTION}, _severities("TX height", w))

    def test_amsl_heights_are_not_checked(self):
        # An AMSL height says nothing about the AGL height ITM cares about;
        # that is only known once terrain has been sampled.
        w = model_warnings(
            CoverageParams(model="ITM", rx_height=0.0, rx_mode="AMSL",
                           tx_height=0.0, tx_mode="AMSL"))
        self.assertEqual([], _for("RX height", w))
        self.assertEqual([], _for("TX height", w))


class TestTerrainQuantisationFloor(unittest.TestCase):
    """Applies to every model — it is a data-resolution limit, not an ITM one."""

    def test_fires_for_los_at_ground_level(self):
        w = model_warnings(CoverageParams(model="LOS", rx_height=0.0))
        self.assertEqual({CAUTION}, _severities("RX height", w))

    def test_plugin_default_receiver_height_is_silent(self):
        # 1.5 m is the shipped default; warning here would be crying wolf.
        self.assertEqual([], model_warnings(CoverageParams(model="LOS", rx_height=1.5)))

    def test_min_alt_is_exempt(self):
        # MIN_ALT computes the minimum visible altitude; a 0 m receiver is the
        # question being asked, not a mistake.
        self.assertEqual([], model_warnings(CoverageParams(model="MIN_ALT", rx_height=0.0)))


class TestFrequency(unittest.TestCase):
    def test_below_hard_minimum_is_invalid(self):
        w = model_warnings(CoverageParams(model="ITM", freq_mhz=15.0))
        self.assertIn(INVALID, _severities("Frequency", w))

    def test_between_hard_and_validated_limits_is_caution(self):
        w = model_warnings(CoverageParams(model="ITM", freq_mhz=30.0))
        self.assertEqual({CAUTION}, _severities("Frequency", w))

    def test_uhf_is_clean(self):
        w = model_warnings(CoverageParams(model="ITM", freq_mhz=433.0))
        self.assertEqual([], _for("Frequency", w))

    def test_not_checked_for_los(self):
        # LOS mode carries freq_mhz = 0.0, which is not an ITM parameter there.
        w = model_warnings(CoverageParams(model="LOS", freq_mhz=0.0))
        self.assertEqual([], _for("Frequency", w))


class TestRange(unittest.TestCase):
    def test_sub_kilometre_range_is_invalid(self):
        w = model_warnings(CoverageParams(model="ITM", max_range_km=0))
        self.assertIn(INVALID, _severities("Range", w))

    def test_normal_range_still_notes_the_inner_kilometre(self):
        w = model_warnings(CoverageParams(model="ITM", max_range_km=30))
        self.assertEqual({CAUTION}, _severities("Range", w))

    def test_beyond_itm_maximum_is_invalid(self):
        w = model_warnings(CoverageParams(model="ITM", max_range_km=2500))
        self.assertIn(INVALID, _severities("Range", w))


class TestAtmosphere(unittest.TestCase):
    def test_refractivity_outside_range_is_invalid(self):
        w = model_warnings(CoverageParams(model="ITM", ens=200.0))
        self.assertIn(INVALID, _severities("Surface refractivity", w))

    def test_in_range_reports_the_terrain_height_limit(self):
        # ITM reduces N0 per path as ens = N0*exp(-zsys/9460); with the default
        # N0 = 301 that crosses the 250 N-unit floor at ~1756 m mean terrain,
        # which alpine scenes reach routinely.
        w = _for("Surface refractivity", model_warnings(CoverageParams(model="ITM", ens=301.0)))
        self.assertEqual([CAUTION], [x.severity for x in w])
        self.assertIn("1756", w[0].message)

    def test_higher_n0_raises_the_terrain_limit(self):
        w = _for("Surface refractivity", model_warnings(CoverageParams(model="ITM", ens=350.0)))
        self.assertIn("3183", w[0].message)


class TestStatisticalParameters(unittest.TestCase):
    def test_invalid_climate(self):
        w = model_warnings(CoverageParams(model="ITM", climate=9))
        self.assertIn(INVALID, _severities("Radio climate", w))

    def test_invalid_polarization(self):
        w = model_warnings(CoverageParams(model="ITM", pol=3))
        self.assertIn(INVALID, _severities("Polarization", w))

    def test_confidence_outside_validated_range(self):
        w = model_warnings(CoverageParams(model="ITM", conf=0.999))
        self.assertEqual({CAUTION}, _severities("Confidence", w))


class TestNonItmModels(unittest.TestCase):
    def test_los_skips_every_itm_only_check(self):
        w = model_warnings(CoverageParams(
            model="LOS", rx_height=1.5, freq_mhz=1.0, max_range_km=5000,
            ens=100.0, climate=99, pol=7, conf=0.999))
        self.assertEqual([], w)


class TestP2PParams(unittest.TestCase):
    def test_same_checks_apply(self):
        w = model_warnings(P2PParams(model="ITM", tx_height=0.1, rx_height=0.1))
        self.assertIn(INVALID, _severities("TX height", w))
        self.assertIn(INVALID, _severities("RX height", w))


class TestFormatting(unittest.TestCase):
    def test_invalid_sorts_before_caution(self):
        w = model_warnings(CoverageParams(model="ITM", rx_height=0.0, max_range_km=30))
        text = format_model_warnings(w)
        self.assertLess(text.index(INVALID), text.index(CAUTION))

    def test_duplicates_are_collapsed(self):
        # Two sites differing only by name produce identical warnings; the
        # dialog must not repeat them once per job.
        w = model_warnings(CoverageParams(model="ITM", rx_height=0.0)) * 3
        self.assertEqual(1, format_model_warnings(w).count("hard minimum"))

    def test_empty_input_renders_empty(self):
        self.assertEqual("", format_model_warnings([]))


if __name__ == "__main__":
    unittest.main()
