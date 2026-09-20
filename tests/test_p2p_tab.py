"""Unit tests for waveshed/gui/p2p_tab — the batch P2P worker's job sizing.

QGIS (and the PyQt stubs the worker's QThread needs) come from conftest.py.

The bug these pin: the worker wrote the TERRAIN radius into the job's
``analysis.max_range_km``. aether_core reads that field as the per-link
distance CAP (engines/p2p.rs builds every link with
``max_dist_m = max_range_km * 1000`` and its trivial-link guard answers 0/0
for anything longer), so every link longer than the radius of the terrain
disc came back as a silent zero. A Bern -> Thun batch is 25.2 km against an
18 km radius — the whole result was two zeros and no error anywhere.
"""

# Bootstrap stubs before any plugin import.
import conftest  # noqa: F401

import json
import math
import os
import tempfile
import unittest
from unittest import mock

from waveshed.core.job_builder import (
    MAX_RANGE_MARGIN_KM,
    P2PParams,
    batch_max_range_km,
    haversine_km,
    longest_link_km,
)
import waveshed.gui.p2p_tab as p2p_tab


# Bern (TX) and Thun (RX): a real 25.2 km link inside a bounding box whose
# half-diagonal is only ~12.6 km, so the terrain radius the worker asks for
# is ~17.6 km — comfortably shorter than the link it has to compute.
_BERN = (46.9480, 7.4474)
_THUN = (46.7580, 7.6280)


def _write_batch_csv(path: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(f"S,BERN,{_BERN[0]:.6f},{_BERN[1]:.6f},30.0,AGL\n")
        fh.write(f"R,THUN,{_THUN[0]:.6f},{_THUN[1]:.6f},2.0,AGL\n")


class TestLinkDistance(unittest.TestCase):
    """The distance the plugin measures must be the one the engine measures."""

    def test_bern_thun_is_about_25_km(self):
        d = haversine_km(_BERN[0], _BERN[1], _THUN[0], _THUN[1])
        self.assertAlmostEqual(d, 25.2, delta=0.5)

    def test_longest_link_is_the_cross_product_maximum(self):
        # Two sources, two targets: the longest of the FOUR links wins, not
        # the longest row-to-next-row pair. aether_core pairs S x R in full.
        entries = [
            ("S", "A", 46.95, 7.45, 30.0, "AGL"),
            ("S", "B", 46.94, 7.44, 30.0, "AGL"),
            ("R", "C", 46.76, 7.63, 2.0, "AGL"),
            ("R", "D", 46.95, 7.46, 2.0, "AGL"),
        ]
        expected = max(
            haversine_km(46.95, 7.45, 46.76, 7.63),
            haversine_km(46.94, 7.44, 46.76, 7.63),
            haversine_km(46.95, 7.45, 46.95, 7.46),
            haversine_km(46.94, 7.44, 46.95, 7.46),
        )
        self.assertAlmostEqual(longest_link_km(entries), expected, places=9)

    def test_a_batch_with_only_sources_has_no_link(self):
        entries = [("S", "A", 46.95, 7.45, 30.0, "AGL")]
        self.assertEqual(longest_link_km(entries), 0.0)

    def test_terrain_radius_wins_when_it_is_the_larger_number(self):
        # A single short link inside a big terrain disc must not SHRINK the
        # cap — the two numbers are a max, not a replacement.
        entries = [
            ("S", "A", 46.95, 7.45, 30.0, "AGL"),
            ("R", "B", 46.96, 7.46, 2.0, "AGL"),
        ]
        self.assertEqual(batch_max_range_km(entries, 50.0), 50)

    def test_cap_clears_the_longest_link_by_the_margin(self):
        entries = [
            ("S", "BERN", _BERN[0], _BERN[1], 30.0, "AGL"),
            ("R", "THUN", _THUN[0], _THUN[1], 2.0, "AGL"),
        ]
        longest = longest_link_km(entries)
        cap = batch_max_range_km(entries, 17.6)
        self.assertEqual(cap, math.ceil(longest) + MAX_RANGE_MARGIN_KM)
        self.assertGreater(cap, longest)


class TestBatchJobRange(unittest.TestCase):
    """End to end through the worker: what lands in the job file."""

    def test_terrain_radius_is_shorter_than_the_link(self):
        # The premise of the bug. If this ever stops holding, the worker test
        # below would pass for the wrong reason.
        with tempfile.TemporaryDirectory() as d:
            csv_path = os.path.join(d, "batch.csv")
            _write_batch_csv(csv_path)
            _lat, _lon, radius = p2p_tab._terrain_extent_for_batch(csv_path)
        self.assertLess(radius, 25.0,
                        "terrain radius should be the small number here")

    def test_job_range_for_batch_covers_the_longest_link(self):
        with tempfile.TemporaryDirectory() as d:
            csv_path = os.path.join(d, "batch.csv")
            _write_batch_csv(csv_path)
            _lat, _lon, radius = p2p_tab._terrain_extent_for_batch(csv_path)
            self.assertGreaterEqual(
                p2p_tab.job_range_for_batch(csv_path, radius), 25)

    def test_worker_writes_a_job_whose_cap_clears_the_link(self):
        """The wiring, not just the helper: run the worker and read the JSON.

        Everything past the job file is stubbed — terrain preparation, the
        engine subprocess, the license env — so the run ends in
        ``finished_err`` on the missing result CSV. That is fine: the job
        file it wrote on the way there is what is under test.
        """
        with tempfile.TemporaryDirectory() as d:
            csv_path = os.path.join(d, "batch.csv")
            _write_batch_csv(csv_path)
            out_dir = os.path.join(d, "out")

            # In batch mode the CSV drives every link; these tx values are
            # the inert fallbacks build_p2p_job validates.
            params = P2PParams(
                tx_lat=_BERN[0], tx_lon=_BERN[1],
                output_name="p2p_test", resolution_m=30, model="ITM",
            )

            worker = p2p_tab._P2PWorker(
                params, mock.Mock(), out_dir, csv_path)

            with mock.patch(
                "waveshed.core.terrain_adapter.prepare_terrain",
                return_value=os.path.join(d, "abt")
            ), mock.patch.object(
                p2p_tab, "find_binary", return_value="aether_core"
            ), mock.patch.object(
                p2p_tab.api_key, "apply_license_env"
            ), mock.patch.object(
                p2p_tab.terrain_adapter, "run_converter_streaming",
                return_value=0
            ):
                worker.run()

            job_path = os.path.join(out_dir, "p2p_test_job.json")
            self.assertTrue(os.path.isfile(job_path),
                            f"worker wrote no job file in {out_dir}")
            with open(job_path, encoding="utf-8") as fh:
                job = json.load(fh)

        self.assertEqual(job["analysis"]["task_type"], "BATCH_P2P")
        self.assertGreaterEqual(
            job["analysis"]["max_range_km"], 25,
            "a 25 km link under a smaller cap is answered 0/0 by aether_core",
        )
        # And the params object the worker mutated agrees with the file.
        self.assertGreaterEqual(params.max_range_km, 25)

    def test_prepare_terrain_still_gets_the_terrain_radius(self):
        """Raising the cap must NOT inflate the terrain download.

        The tile disc is sized by the batch's own bounding box; the engine
        picks the tiles a link needs by walking that link's path, not by the
        cap. Handing the bigger number to prepare_terrain would download
        terrain no link can reach.
        """
        with tempfile.TemporaryDirectory() as d:
            csv_path = os.path.join(d, "batch.csv")
            _write_batch_csv(csv_path)
            out_dir = os.path.join(d, "out")
            params = P2PParams(output_name="p2p_test", resolution_m=30)
            worker = p2p_tab._P2PWorker(
                params, mock.Mock(), out_dir, csv_path)

            with mock.patch(
                "waveshed.core.terrain_adapter.prepare_terrain",
                return_value=os.path.join(d, "abt")
            ) as prep, mock.patch.object(
                p2p_tab, "find_binary", return_value="aether_core"
            ), mock.patch.object(
                p2p_tab.api_key, "apply_license_env"
            ), mock.patch.object(
                p2p_tab.terrain_adapter, "run_converter_streaming",
                return_value=0
            ):
                worker.run()

            prep.assert_called_once()
            asked = prep.call_args.kwargs["max_range_km"]
            self.assertLess(asked, 25.0)
            self.assertGreater(params.max_range_km, asked)


if __name__ == "__main__":
    unittest.main()
