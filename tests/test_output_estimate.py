"""Pre-run disk estimate for the coverage raster (core/output_estimate.py)."""

import pytest

from waveshed.core import output_estimate as oe

GB = 1024 ** 3


def test_bits_per_pixel_follow_the_engine_output_modes():
    assert oe.bits_per_pixel("LOS") == 1
    assert oe.bits_per_pixel("MIN_ALT") == 16
    assert oe.bits_per_pixel("ITM") == 8
    assert oe.bits_per_pixel("SIMPLE_LOSS") == 8


def test_owner_case_2m_to_500km_los_floor_is_hundreds_of_gb():
    # 2026-10-13: died with "not enough space" at wedge 158/380.
    size = oe.engine_output_bytes("MIN_ALT", 500, 2)
    assert 360 * GB < size < 400 * GB


def test_size_scales_with_range_over_resolution_squared():
    # Edge tiles (512 px) are stored whole, so the ratio only nears 4 once
    # the range is many tiles wide.
    a = oe.raster_pixels(1000, 10)
    b = oe.raster_pixels(1000, 20)
    assert a / b == pytest.approx(4.0, rel=0.05)


def test_sector_scales_the_disc():
    full = oe.raster_pixels(300, 30)
    quarter = oe.raster_pixels(300, 30, 0, 90)
    assert quarter / full == pytest.approx(0.25, rel=0.01)
    assert oe.sector_fraction(350, 10) == pytest.approx(20 / 360)
    assert oe.sector_fraction(0, 360) == 1.0


def test_small_range_is_bounded_by_the_square():
    # One tile's worth, not a disc grown by a tile diagonal.
    assert oe.raster_pixels(0.5, 10) <= (2 * 50 + oe.OUTPUT_TILE_PX) ** 2


def test_blocking_when_disk_too_small_and_suggests_a_fitting_resolution():
    msg, blocking = oe.output_space_problem(
        [("Site 1", "MIN_ALT", 500, 2, 0, 360)], "/out", free=71 * GB)
    assert blocking
    assert "71.0 GB is free" in msg
    assert "5 m pixels would fit" in msg


def test_large_but_fitting_asks_and_ordinary_runs_say_nothing():
    msg, blocking = oe.output_space_problem(
        [("Site 1", "LOS", 500, 2, 0, 360)], "/out", free=500 * GB)
    assert not blocking and "Run anyway?" in msg
    assert oe.output_space_problem(
        [("Site 1", "ITM", 30, 30, 0, 360)], "/out", free=10 * GB) == (None, False)


def test_jobs_add_up():
    one = oe.engine_output_bytes("MIN_ALT", 100, 10)
    free = int(one * 1.5)
    assert not oe.output_space_problem(
        [("a", "MIN_ALT", 100, 10, 0, 360)], "/out", free=free)[1]
    assert oe.output_space_problem(
        [("a", "MIN_ALT", 100, 10, 0, 360), ("b", "MIN_ALT", 100, 10, 0, 360)],
        "/out", free=free)[1]


def test_free_bytes_walks_up_to_an_existing_parent(tmp_path):
    assert oe.free_bytes(str(tmp_path / "not" / "yet" / "made")) > 0
