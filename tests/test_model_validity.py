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
    MIN_ANTENNA_AGL_M,
    CoverageParams,
    P2PParams,
    antenna_height_error,
    build_coverage_job,
    build_p2p_job,
    format_model_warnings,
    height_floor_error,
    model_warnings,
)


def _for(param, warnings):
    return [w for w in warnings if w.parameter == param]


def _severities(param, warnings):
    return {w.severity for w in _for(param, warnings)}


class TestAntennaHeights(unittest.TestCase):
    def test_the_floor_is_one_metre(self):
        # The shared floor across the plugin and the engine. If this moves,
        # every message and every spinbox minimum below moves with it.
        self.assertEqual(1.0, MIN_ANTENNA_AGL_M)

    def test_ground_level_receiver_is_invalid(self):
        # Sub-floor AGL is refused outright now: the plugin rejects the job
        # rather than letting the engine's backstop silently raise the height.
        w = model_warnings(CoverageParams(model="ITM", rx_height=0.0))
        self.assertEqual({INVALID}, _severities("RX height", w))
        self.assertIn(f"{MIN_ANTENNA_AGL_M:.1f} m", _for("RX height", w)[0].message)

    def test_below_the_floor_is_invalid_not_caution(self):
        w = model_warnings(CoverageParams(model="ITM", rx_height=0.75))
        self.assertEqual({INVALID}, _severities("RX height", w))

    def test_exactly_the_floor_is_accepted(self):
        # Boundary: the floor itself is legal, one ULP below it is not.
        self.assertEqual(
            [], _for("RX height", model_warnings(
                CoverageParams(model="ITM", rx_height=MIN_ANTENNA_AGL_M))))
        self.assertEqual(
            {INVALID}, _severities("RX height", model_warnings(
                CoverageParams(model="ITM", rx_height=MIN_ANTENNA_AGL_M - 0.01))))

    def test_two_metres_is_clean(self):
        w = model_warnings(CoverageParams(model="ITM", rx_height=2.0))
        self.assertEqual([], _for("RX height", w))

    def test_ground_level_transmitter_is_invalid(self):
        # The floor covers the transmitter too, not just the receiver.
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

    def test_negative_amsl_heights_are_not_checked(self):
        # Below sea level is a real place, not an error: the Dead Sea shore is
        # -430 m and Schiphol is -4 m. Flooring an AMSL height would lift such
        # a site hundreds of metres into the air.
        w = model_warnings(
            CoverageParams(model="ITM", rx_height=-430.0, rx_mode="AMSL",
                           tx_height=-4.0, tx_mode="AMSL"))
        self.assertEqual([], _for("RX height", w))
        self.assertEqual([], _for("TX height", w))


class TestTerrainQuantisationFloor(unittest.TestCase):
    """The floor is a data-resolution limit too, so it is not ITM-only."""

    def test_fires_for_los_at_ground_level(self):
        # .abt stores elevations in 0.5 m steps, so a sub-metre antenna leaves
        # the LOS test a margin inside the DEM's own rounding — that is wrong
        # for a pure geometric run just as much as for ITM.
        w = model_warnings(CoverageParams(model="LOS", rx_height=0.0))
        self.assertEqual({INVALID}, _severities("RX height", w))

    def test_fires_for_a_sub_floor_los_transmitter(self):
        w = model_warnings(CoverageParams(model="LOS", tx_height=0.4))
        self.assertEqual({INVALID}, _severities("TX height", w))

    def test_plugin_default_receiver_height_is_silent(self):
        # 1.5 m is the shipped default; warning here would be crying wolf.
        self.assertEqual([], model_warnings(CoverageParams(model="LOS", rx_height=1.5)))

    def test_min_alt_is_exempt(self):
        # MIN_ALT computes the minimum visible altitude; a 0 m receiver is the
        # question being asked, not a mistake.
        self.assertEqual([], model_warnings(CoverageParams(model="MIN_ALT", rx_height=0.0)))

    def test_min_alt_still_checks_the_transmitter(self):
        # Only the receiver is the unknown being solved for; the MIN_ALT
        # transmitter is a real antenna and keeps the floor.
        w = model_warnings(CoverageParams(model="MIN_ALT", tx_height=0.0))
        self.assertEqual({INVALID}, _severities("TX height", w))


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
        # Both ends now carry the same floor — the receiver is no longer the
        # lenient one.
        w = model_warnings(P2PParams(model="ITM", tx_height=0.1, rx_height=0.1))
        self.assertIn(INVALID, _severities("TX height", w))
        self.assertEqual({INVALID}, _severities("RX height", w))


class TestFormatting(unittest.TestCase):
    def test_invalid_sorts_before_caution(self):
        w = model_warnings(CoverageParams(model="ITM", tx_height=0.0, max_range_km=30))
        text = format_model_warnings(w)
        self.assertLess(text.index(INVALID), text.index(CAUTION))

    def test_duplicates_are_collapsed(self):
        # Two sites differing only by name produce identical warnings; the
        # dialog must not repeat them once per job.
        w = model_warnings(CoverageParams(model="ITM", rx_height=0.0)) * 3
        self.assertEqual(1, format_model_warnings(w).count("hard minimum"))

    def test_sub_floor_receiver_reaches_the_blocking_dialog(self):
        # Inverted deliberately. Both tabs gate their modal on INVALID, so a
        # sub-metre receiver has to carry that severity to be shown at all —
        # a CAUTION would let the run through in silence.
        w = model_warnings(CoverageParams(model="ITM", rx_height=0.0))
        self.assertTrue([x for x in w if x.severity == INVALID])

    def test_empty_input_renders_empty(self):
        self.assertEqual("", format_model_warnings([]))


class TestHeightFloorError(unittest.TestCase):
    """The shared message helper the dialogs and CSV parsers all use."""

    def test_names_the_field_and_the_floor(self):
        msg = height_floor_error("TX height", 0.3, "AGL")
        self.assertIsNotNone(msg)
        self.assertIn("TX height", msg)
        self.assertIn(f"{MIN_ANTENNA_AGL_M:.1f} m", msg)

    def test_at_and_above_the_floor_is_none(self):
        self.assertIsNone(height_floor_error("RX height", MIN_ANTENNA_AGL_M, "AGL"))
        self.assertIsNone(height_floor_error("RX height", 1.5, "AGL"))

    def test_amsl_is_never_floored(self):
        for altitude in (0.0, -4.0, -430.0):
            self.assertIsNone(height_floor_error("TX height", altitude, "AMSL"))

    def test_mode_matching_is_case_insensitive(self):
        self.assertIsNone(height_floor_error("TX height", -430.0, "amsl"))
        self.assertIsNotNone(height_floor_error("TX height", 0.3, "agl"))


class TestJobBuilderRejection(unittest.TestCase):
    """The authoritative refusal: every entry point funnels through these two.

    Qt-level clamping cannot be tested here — conftest stubs QDoubleSpinBox as
    an inert mock with no range semantics — so the core rejection is the thing
    under test.
    """

    def test_coverage_job_rejects_a_sub_floor_receiver(self):
        with self.assertRaises(ValueError) as ctx:
            build_coverage_job(
                CoverageParams(rx_height=0.5), "/abt", "/out")
        self.assertIn("RX height", str(ctx.exception))
        self.assertIn(f"{MIN_ANTENNA_AGL_M:.1f} m", str(ctx.exception))

    def test_coverage_job_rejects_a_sub_floor_transmitter(self):
        with self.assertRaises(ValueError) as ctx:
            build_coverage_job(
                CoverageParams(tx_height=0.0), "/abt", "/out")
        self.assertIn("TX height", str(ctx.exception))

    def test_p2p_job_rejects_a_sub_floor_antenna(self):
        with self.assertRaises(ValueError):
            build_p2p_job(P2PParams(rx_height=0.99), "/abt", "/out")

    def test_amsl_below_sea_level_is_built_untouched(self):
        # The whole point of the AGL/AMSL split: a Dead Sea site must survive.
        job = build_coverage_job(
            CoverageParams(tx_height=-430.0, tx_mode="AMSL",
                           rx_height=-428.5, rx_mode="AMSL"),
            "/abt", "/out")
        self.assertEqual(-430.0, job["tx"]["height_m"])
        self.assertEqual(-428.5, job["rx"]["height_m"])

    def test_the_floor_itself_is_built(self):
        job = build_coverage_job(
            CoverageParams(tx_height=MIN_ANTENNA_AGL_M,
                           rx_height=MIN_ANTENNA_AGL_M),
            "/abt", "/out")
        self.assertEqual(MIN_ANTENNA_AGL_M, job["tx"]["height_m"])
        self.assertEqual(MIN_ANTENNA_AGL_M, job["rx"]["height_m"])

    def test_min_alt_receiver_stays_exempt(self):
        # MIN_ALT solves for the receiver altitude, so a 0 m RX is the question.
        job = build_coverage_job(
            CoverageParams(model="MIN_ALT", rx_height=0.0), "/abt", "/out")
        self.assertEqual(0.0, job["rx"]["height_m"])

    def test_min_alt_transmitter_is_not_exempt(self):
        with self.assertRaises(ValueError):
            build_coverage_job(
                CoverageParams(model="MIN_ALT", tx_height=0.0), "/abt", "/out")

    def test_antenna_height_error_reports_the_transmitter_first(self):
        msg = antenna_height_error(CoverageParams(tx_height=0.1, rx_height=0.1))
        self.assertIn("TX height", msg)

    def test_antenna_height_error_is_none_for_defaults(self):
        self.assertIsNone(antenna_height_error(CoverageParams()))
        self.assertIsNone(antenna_height_error(P2PParams()))


if __name__ == "__main__":
    unittest.main()


class TestMemoryBudgetAuto(unittest.TestCase):
    """Auto (0) omits the coverage job's memory budgets; a number is sent."""

    def test_auto_omits_both_fields(self):
        p = build_coverage_job(
            CoverageParams(max_ram_gb=0, max_vram_gb=0), "/abt", "/out")["processing"]
        self.assertEqual({"terrain_dir": "/abt"}, p)

    def test_explicit_values_are_sent(self):
        p = build_coverage_job(
            CoverageParams(max_ram_gb=32, max_vram_gb=12), "/abt", "/out")["processing"]
        self.assertEqual(32, p["max_ram_usage_gb"])
        self.assertEqual(12, p["max_vram_usage_gb"])

    def test_unset_settings_default_to_auto(self):
        params = CoverageParams()
        self.assertEqual((0, 0), (params.max_ram_gb, params.max_vram_gb))
