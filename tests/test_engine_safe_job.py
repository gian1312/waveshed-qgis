"""engine_safe_job / write_job_file: no job the engine cannot parse (2026-11-11).

The website sent a fractional ``max_range_km`` (engine type u32) and ``null``
for a cleared field; these tests pin that the plugin's single job writer can
do neither.
"""
import json
import math
import os
import tempfile
import unittest

import conftest  # noqa: F401

from waveshed.core.job_builder import engine_safe_job, write_job_file


def _job(**analysis):
    base = {"task_type": "SINGLE", "propagation_model": "ITM", "max_range_km": 30,
            "resolution_m": 30.0, "azimuth_start_deg": 0.0, "azimuth_end_deg": 360.0}
    base.update(analysis)
    return {"tx": {"lat": 46.9, "lon": 8.4, "height_m": 30.0, "mode": "AGL",
                   "freq_mhz": 433.0, "erp_watts": 10.0},
            "rx": {"height_m": 2.0, "mode": "AGL"}, "analysis": base}


class TestEngineSafeJob(unittest.TestCase):
    def test_integer_range_untouched(self):
        job = _job(max_range_km=30)
        self.assertIs(engine_safe_job(job), job)

    def test_float_range_rounds_up(self):
        # Up, never down: for P2P the value is the per-link distance cap.
        self.assertEqual(engine_safe_job(_job(max_range_km=12.1))["analysis"]["max_range_km"], 13)
        self.assertEqual(engine_safe_job(_job(max_range_km=0.2))["analysis"]["max_range_km"], 1)
        self.assertIsInstance(engine_safe_job(_job(max_range_km=7.0))["analysis"]["max_range_km"], int)

    def test_input_dict_not_mutated(self):
        job = _job(max_range_km=12.5)
        engine_safe_job(job)
        self.assertEqual(job["analysis"]["max_range_km"], 12.5)

    def test_nan_and_inf_are_refused_with_the_field_named(self):
        job = _job()
        job["tx"]["freq_mhz"] = math.nan
        job["rx"]["height_m"] = math.inf
        with self.assertRaises(ValueError) as cm:
            engine_safe_job(job)
        self.assertIn("tx.freq_mhz", str(cm.exception))
        self.assertIn("rx.height_m", str(cm.exception))

    def test_nested_lists_checked(self):
        job = _job()
        job["tx"]["az_pattern_data"] = [[0.0, 0.0], [90.0, math.nan]]
        with self.assertRaises(ValueError) as cm:
            engine_safe_job(job)
        self.assertIn("tx.az_pattern_data[1][1]", str(cm.exception))

    def test_write_job_file_writes_parseable_integer_range(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_job_file(_job(max_range_km=32.18688), d, "x")
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertEqual(json.loads(text)["analysis"]["max_range_km"], 33)
            self.assertNotIn("NaN", text)

    def test_write_job_file_refuses_nan(self):
        with tempfile.TemporaryDirectory() as d:
            job = _job()
            job["analysis"]["min_db"] = math.nan
            with self.assertRaises(ValueError):
                write_job_file(job, d, "x")
            self.assertFalse(os.path.exists(os.path.join(d, "x_job.json")))


if __name__ == "__main__":
    unittest.main()
