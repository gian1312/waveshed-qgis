"""Site Analysis' ``aether_converter ingest`` lines: what reaches the log
panel and what only the callers read.

The converter prints one ``[Tile] <name> covered=a/b`` line and, with a
``buildings_pbf_dir``, one ``[Info] buildings_pbf_dir: …`` report per output
tile. The Map Converter parses both without logging them; Site Analysis did
log them (52 lines for a 26-tile run). Both tabs now keep the panel to the
summary lines. QGIS stubs provided by conftest.py.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import unittest
from unittest import mock

from waveshed.core import osm_buildings, terrain_adapter as ta


CONVERTER_LINES = [
    "[Rust] Batch processing 2 tiles...",
    "[Rust] Decoding building tiles in \"V\" (once for the run)...",
    "[Rust] Decoding building tiles: 3679/3679",
    "[Rust] Building tiles decoded: 3679 tile(s), 825770 building(s) in 2.1 s",
    "[Info] buildings_pbf_dir: 3679 tile(s), 825770 building(s) (794725 after "
    "dedup), 9239 drawn, 19214 pixel(s) raised",
    "[Tile] tile_N46.25E6.50_10m.abt covered=0/7728400",
    "[Info] buildings_pbf_dir: 3679 tile(s), 825770 building(s) (794725 after "
    "dedup), 10485 drawn, 31584 pixel(s) raised",
    "[Tile] tile_N47.25E6.50_10m.abt covered=834186/7728400",
    "[Tile] tile_N47.25E6.50_10m.abt voids=0",
    "[Rust] Progress: 2/2",
    "[Rust] run: wall 17.9s, cpu user 70.5s sys 24.2s, page faults 898657, "
    "threads 16, blocked ~191.5s",
]


class TestPanelLines(unittest.TestCase):
    def _run(self, burns):
        logged = []

        def fake_stream(exe, args, on_line, on_start=None, env=None):
            for line in CONVERTER_LINES:
                on_line(line)
            return 0

        with mock.patch.object(ta, "run_converter_streaming", fake_stream), \
                mock.patch.object(ta, "_log", side_effect=logged.append):
            lines = ta._run_converter("conv", "job.json", burns=burns)
        return lines, [l.strip() for l in logged]

    def test_per_tile_lines_are_kept_for_the_callers_not_logged(self):
        burns = osm_buildings.BurnMonitor()
        burns.begin(2)
        lines, logged = self._run(burns)
        self.assertEqual(lines, CONVERTER_LINES,
                         "the callers still get every line, in order")
        for line in logged:
            self.assertIsNone(ta.parse_tile_line(line), line)
            self.assertIsNone(ta.parse_tile_voids_line(line), line)
            self.assertNotIn("buildings_pbf_dir:", line)
        self.assertIn("[Rust] Progress: 2/2", logged)
        self.assertIn("[Rust] Decoding building tiles: 3679/3679", logged)
        self.assertTrue(any(l.startswith("[Rust] run:") for l in logged))
        self.assertEqual(len(logged), len(CONVERTER_LINES) - 5)

    def test_the_monitor_still_sums_the_reports(self):
        burns = osm_buildings.BurnMonitor()
        burns.begin(2)
        self._run(burns)
        self.assertEqual(burns.batch_reports, 2)
        self.assertEqual(burns.batch_drawn, 9239 + 10485)
        self.assertEqual(burns.batch_raised, 19214 + 31584)

    def test_without_a_monitor_only_the_tile_lines_are_held_back(self):
        lines, logged = self._run(None)
        self.assertEqual(lines, CONVERTER_LINES)
        self.assertEqual(
            [l for l in logged if l.startswith("[Info] buildings_pbf_dir:")],
            [l for l in CONVERTER_LINES if l.startswith("[Info] buildings_pbf_dir:")],
            "no monitor: nothing sums the reports, so they stay visible")
        self.assertFalse(any(ta.parse_tile_line(l) for l in logged))


if __name__ == "__main__":
    unittest.main()
