"""Zoom / pan / snap maths of the Custom resolution editor (gui.range_profile_dialog)."""

import pytest

from waveshed.gui import range_profile_dialog as rpd

RANGE_M = 300_000.0


def test_full_view_keeps_the_half_km_snap():
    assert rpd.snap_for_span(RANGE_M) == 500.0
    assert rpd.snap_for_span(40_000.0) == 500.0


@pytest.mark.parametrize("span, snap", [(20_000.0, 250.0), (8_000.0, 100.0),
                                        (4_000.0, 50.0), (1_000.0, 10.0),
                                        (200.0, 10.0)])
def test_zooming_in_refines_the_snap(span, snap):
    assert rpd.snap_for_span(span) == snap


def test_zoom_keeps_the_anchor_under_the_cursor():
    v0, v1 = rpd.zoom_view((0.0, RANGE_M), 150_000.0, 0.5, RANGE_M)
    assert (v0, v1) == (75_000.0, 225_000.0)


def test_zoom_clamps_to_the_range_and_min_span():
    assert rpd.zoom_view((0.0, RANGE_M), 1_000.0, 0.5, RANGE_M)[0] >= 0.0
    v0, v1 = rpd.zoom_view((0.0, 1_000.0), 500.0, 1e-6, RANGE_M)
    assert v1 - v0 == pytest.approx(rpd._MIN_SPAN_M)
    assert rpd.zoom_view((10_000.0, 20_000.0), 15_000.0, 1e6, RANGE_M) == (0.0, RANGE_M)


def test_short_range_cannot_be_zoomed_past_itself():
    assert rpd.zoom_view((0.0, 150.0), 75.0, 0.1, 150.0) == (0.0, 150.0)


def test_pan_keeps_span_and_stays_inside():
    assert rpd.pan_view((1_000.0, 3_000.0), 500.0, RANGE_M) == (1_500.0, 3_500.0)
    assert rpd.pan_view((1_000.0, 3_000.0), -5_000.0, RANGE_M) == (0.0, 2_000.0)
    assert rpd.pan_view((290_000.0, 299_000.0), 5_000.0, RANGE_M) == (291_000.0, RANGE_M)


@pytest.mark.parametrize("v0, v1, expected", [
    (0.0, RANGE_M, [0.0, 100_000.0, 200_000.0, 300_000.0]),
    (6_873.9, 12_278.2, [8_000.0, 10_000.0, 12_000.0]),
    (0.0, 1_000.0, [0.0, 200.0, 400.0, 600.0, 800.0, 1_000.0]),
])
def test_ticks_are_round(v0, v1, expected):
    assert rpd.nice_ticks(v0, v1) == expected
