"""The timing lanes (2026-10-23): log lines that make a silence attributable.

The owner's run had 3 min 48 s of silence between a degraded z15 pass ending
and its repair pass launching, and nothing in the log said where it went.
Two lanes, Info lines only, ``perf_counter`` only, parsed by nothing:

* per download PASS (``terrain_adapter._try_rust_download``): ``timing: z15
  pass 1: converter exit +X.Xs after its last line; post-pass parse A.As,
  flags B.Bs, degraded/repair plan C.Cs, next launch D.Ds`` — logged when the
  next pass is about to launch (the last one: ``next launch —``). Driven
  here through the REAL ``run_converter_streaming`` with a stand-in engine
  process that lingers after its last line, and a slow gap scan — which a
  degraded-only pass no longer runs (round 6: only ``missing - degraded``
  misses can leave a hole; the owner's band read 44 GB twice for none);
* per Map Converter BATCH (``_unit_done`` → ``_next_batch`` →
  ``_run_batch``): ``timing: band k/n ready at +X s, started at +Y s (queue
  wait Z s), building view W s, …`` right before its converter starts.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import json
import math
import os
import re
import sys
import tempfile
import time
import unittest
from unittest import mock

import waveshed.core.terrain_adapter as ta
from waveshed.core import binary_manager
import waveshed.gui.map_converter_tab as mct

from test_mc_pipeline import _Case as _BandCase, _Recorder, _xyz_only
from test_run_progress import _XYZ, _converter_pass_lines, _write_abt

_REAL_RUNNER = ta.run_converter_streaming

#: The stand-in engine: prints its argv lines, then lingers LINGER_S
#: before it exits (the converter's own wind-down after its last line).
_ENGINE = ("import sys, time\n"
           "for line in sys.argv[2:]:\n"
           "    print(line, flush=True)\n"
           "time.sleep(float(sys.argv[1]))\n")
LINGER_S = 0.4
#: The gap scan reads every tile of the group; slowed down to this per tile.
SCAN_S = 0.15

_PASS_RE = re.compile(
    r"^  timing: z(?P<zoom>\d+) (?P<kind>pass|repair pass) (?P<n>\d+): "
    r"converter exit \+(?P<exit>[\d.]+)s after its last line; post-pass "
    r"parse (?P<parse>[\d.]+)s, flags (?P<flags>[\d.]+)s, degraded/repair "
    r"plan (?P<plan>[\d.]+)s, next launch (?P<next>[\d.]+s|—)$")


class DownloadPassLane(unittest.TestCase):
    """A degraded pass, then its repair pass — as on the owner's run: ONE
    source tile timed out and the converter filled it from z14 (``[Stats]
    Tiles: total-1/total``, one ``[Degraded]`` line). The plugin then read
    all 706 output tiles of the band twice (159-252 s, "degraded/repair
    plan") to find nothing: a degraded tile leaves no hole, and only
    ``missing - degraded`` misses can (round 6)."""

    def _run(self, degraded=1, hard=0, hole_in=None):
        """Pass 1 reports *degraded* coarse-filled source tiles and *hard*
        misses without a fill; the gap scan (``_abt_has_holes``, SCAN_S a
        tile) finds a hole in the tile named *hole_in* only. Later passes
        fetch everything they are asked for."""
        tiles = mct._enumerate_tiles(
            mct._snap_bbox({"south": 47.1, "north": 47.25, "west": 8.1,
                            "east": 8.35}, [2]), [2])
        subtiles = [t for _r, t in tiles]
        (zoom, specs), = ta._download_specs_by_zoom(_XYZ, subtiles).items()
        s0 = specs[0]
        n = 2.0 ** zoom
        lat = s0["ul_lat"] - 0.001
        x0 = int((s0["ul_lon"] + 0.001 + 180.0) / 360.0 * n)
        y0 = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi)
                 / 2 * n)
        bad = [(zoom, x0, y0), (zoom, x0 + 1, y0)][:degraded]
        total1 = ta._source_tile_count(specs, zoom)
        launches, jobs, logs = [], [], []
        scans = {"holes": [], "zero": []}

        def runner(exe, args, on_line, on_start=None, env=None):
            with open(args[-1], encoding="utf-8") as fh:
                job = json.load(fh)
            launches.append((time.perf_counter(), len(logs)))
            jobs.append(job)
            if len(jobs) == 1:
                out = [f"[Download] z={zoom} tiles=1x{total1}={total1} "
                       f"(~1MB) connections=256"]
                out += _converter_pass_lines(total1)
                out += [f"[Degraded] z={z} x={x} y={y} reason=timeout "
                        f"filled_from=z{zoom - 1}" for z, x, y in bad]
                if hard:
                    out.append(f"[Download] first timeout: z={zoom}/"
                               f"x={x0 + 5}/y={y0}")
                out.append(f"[Stats] Tiles: {total1 - degraded - hard}/"
                           f"{total1} OK")
            else:
                k = (len(job["repair_source_tiles"])
                     if "repair_source_tiles" in job
                     else ta._source_tile_count(job["tiles"], zoom))
                out = [f"[Download] z={zoom} tiles=1x{k}={k} (~1MB) "
                       f"connections=256"]
                out += _converter_pass_lines(k)
                out.append(f"[Stats] Tiles: {k}/{k} OK")
            # The REAL runner, a real process: its last line, then it
            # lingers before exiting.
            return _REAL_RUNNER(sys.executable,
                                ["-c", _ENGINE, str(LINGER_S), *out],
                                on_line, on_start=on_start, env=env)

        def slow_scan(path):
            scans["holes"].append(os.path.basename(path))
            time.sleep(SCAN_S)
            return os.path.basename(path) == hole_in

        def zero_scan(path):
            scans["zero"].append(os.path.basename(path))
            return False

        bm = mock.Mock()
        bm.find_binary.return_value = "aether_converter"
        with tempfile.TemporaryDirectory() as pool:
            for t in subtiles:
                _write_abt(os.path.join(pool, t["filename"]))
            with mock.patch.object(ta, "run_converter_streaming",
                                   side_effect=runner), \
                 mock.patch.object(ta, "_log", side_effect=logs.append), \
                 mock.patch.object(ta, "_abt_has_holes",
                                   side_effect=slow_scan), \
                 mock.patch.object(ta, "_abt_has_zero_fill",
                                   side_effect=zero_scan), \
                 mock.patch.object(ta, "_get_download_max_passes",
                                   return_value=4), \
                 mock.patch.object(ta, "_get_download_connections",
                                   return_value=256), \
                 mock.patch.object(binary_manager, "probe_converter_version",
                                   return_value="0.2.6"):
                ok = ta._try_rust_download(_XYZ, pool, {}, 2, subtiles, bm)
        return {"ok": ok, "zoom": zoom, "specs": specs, "logs": logs,
                "launches": launches, "jobs": jobs, "scans": scans,
                "bad": bad}

    def _pass_lines(self, logs):
        lines = [(i, m) for i, m in enumerate(logs) if "timing:" in m]
        out = []
        for i, line in lines:
            m = _PASS_RE.match(line)
            self.assertIsNotNone(m, line)
            out.append((i, m))
        return out

    def test_one_line_per_pass_naming_where_the_gap_went(self):
        r = self._run()
        logs, launches, zoom = r["logs"], r["launches"], r["zoom"]
        self.assertTrue(r["ok"])
        self.assertEqual(len(launches), 2)            # the pass, its repair
        lines = self._pass_lines(logs)
        self.assertEqual(len(lines), 2, [m.group(0) for _i, m in lines])
        (i1, m1), (i2, m2) = lines
        self.assertEqual((int(m1["zoom"]), m1["kind"], m1["n"]),
                         (zoom, "pass", "1"))
        self.assertEqual((int(m2["zoom"]), m2["kind"], m2["n"]),
                         (zoom, "repair pass", "2"))
        # The engine lingered LINGER_S after its last line, each pass.
        for m in (m1, m2):
            self.assertGreaterEqual(float(m["exit"]), LINGER_S - 0.05)
            self.assertLess(float(m["exit"]), LINGER_S + 2.0)
        # One degraded tile, no other miss: no output tile can hold a hole,
        # so none is read — the plan is ~0 (was SCAN_S × every tile).
        self.assertEqual(r["scans"], {"holes": [], "zero": []})
        self.assertLess(float(m1["plan"]), 0.1)
        self.assertFalse(any("gap scan:" in m for m in logs), logs)
        self.assertLess(float(m1["parse"]), 0.5)
        self.assertNotEqual(m1["next"], "—")
        self.assertLess(float(m1["next"][:-1]), 0.5)
        # Logged when the repair pass launched — before its first line.
        repair_launch_at = launches[1][1]
        self.assertLessEqual(i1, repair_launch_at)
        self.assertTrue(any(m.startswith("  Rust download z=") and
                            "repair pass 2/4" in m for m in logs[:i1]), logs)
        # The repair pass patches exactly the degraded tile.
        self.assertEqual(r["jobs"][1]["repair_source_tiles"],
                         [list(t) for t in r["bad"]])
        # The repair pass is the last: no next launch in this download.
        self.assertEqual(m2["next"], "—")
        self.assertGreater(i2, i1)
        # The parts add up to the silence between the two engine runs.
        gap = launches[1][0] - launches[0][0]
        booked = sum(float(m1[k]) for k in ("exit", "parse", "flags", "plan"))
        self.assertLessEqual(booked, gap + 0.1)

    def test_a_miss_without_fill_reads_each_tile_of_the_pass_once(self):
        # One degraded tile AND one tile that failed outright: holes are
        # possible and the converter does not say where, so every output
        # tile of the pass is read — once (holes and zero fill share the
        # read) — and the timing line books it to the plan. No hole found:
        # the degraded tile still gets its repair pass.
        r = self._run(degraded=1, hard=1)
        specs, logs = r["specs"], r["logs"]
        self.assertTrue(r["ok"])
        names = sorted(s["filename"] for s in specs)
        self.assertEqual(sorted(r["scans"]["holes"]), names)
        self.assertEqual(sorted(r["scans"]["zero"]), names)
        (_i1, m1), (_i2, _m2) = self._pass_lines(logs)
        self.assertGreaterEqual(float(m1["plan"]), SCAN_S * len(specs) - 0.05)
        self.assertIn(f"  gap scan: reading {len(specs)} output tile(s) at "
                      f"z{r['zoom']} — 1 source tile(s) missing without a "
                      f"coarser fill; the converter does not name the output "
                      f"tiles they hit", logs)
        self.assertIn("repair_source_tiles", r["jobs"][1])

    def test_a_miss_without_fill_still_finds_its_hole(self):
        r = self._run(degraded=0, hard=1, hole_in="tile_N47.20E8.20_2m.abt")
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["jobs"]), 2)
        # The holed tile alone is downloaded again, whole.
        self.assertNotIn("repair_source_tiles", r["jobs"][1])
        self.assertEqual([t["filename"] for t in r["jobs"][1]["tiles"]],
                         ["tile_N47.20E8.20_2m.abt"])
        # Pass 2 fetched all it was asked for: complete, no second scan.
        self.assertEqual(len(r["scans"]["holes"]), len(r["specs"]))

    def test_stream_timing_of_the_real_runner(self):
        lines = []
        ta.forget_stream_timing()
        self.assertIsNone(ta.last_stream_timing())
        rc = _REAL_RUNNER(sys.executable, ["-c", _ENGINE, "0.3", "one", "two"],
                          lines.append)
        self.assertEqual(rc, 0)
        self.assertEqual(lines, ["one", "two"])
        st = ta.last_stream_timing()
        self.assertIsNotNone(st)
        self.assertLessEqual(st.launched, st.first_line)
        self.assertLessEqual(st.first_line, st.last_line)
        self.assertGreaterEqual(st.exit_after_last_line, 0.25)
        # A run that raised leaves no record for its caller to misread.
        with self.assertRaises(ta.ConverterCancelled):
            _REAL_RUNNER(sys.executable, ["-c", _ENGINE, "5", "x"],
                         lambda _l: (_ for _ in ()).throw(
                             ta.ConverterCancelled()))
        self.assertIsNone(ta.last_stream_timing())


_BATCH_RE = re.compile(
    r"^  timing: (?P<what>band \d+/\d+|Phase-2 holes|batch \d+) ready at "
    r"\+(?P<ready>[\d.]+) s, started at \+(?P<started>[\d.]+) s \(queue "
    r"wait (?P<wait>[\d.]+) s\), building view (?P<view>[\d.]+) s, disk "
    r"check (?P<disk>[\d.]+) s, jobs (?P<jobs>[\d.]+) s, launch prep "
    r"(?P<prep>[\d.]+) s$")


class BatchLane(_BandCase):
    """Two bands of an XYZ-only stack: band 2 lands while band 1 still
    converts (each ingest takes HOLD_S), so it waits in the queue."""

    HOLD_S = 0.6

    def test_one_line_per_batch_before_its_converter_starts(self):
        conv = _Recorder(hold_s=self.HOLD_S)
        r = self.run_(entries=_xyz_only(), converter=conv)
        self.assertEqual(r.errs, [])
        lines = [(i, m) for i, m in enumerate(r.logs) if "timing:" in m]
        self.assertEqual(len(lines), 2, r.logs)
        got = []
        for i, line in lines:
            m = _BATCH_RE.match(line)
            self.assertIsNotNone(m, line)
            got.append(m)
            # Right before that batch's converter starts (after its
            # "Launching: … ingest" line, before the converter's first).
            self.assertEqual(r.logs[i - 1], "Launching: /bin/conv ingest",
                             r.logs[i - 2:i + 3])
        self.assertEqual([m["what"] for m in got], ["band 1/2", "band 2/2"])
        for m in got:
            ready, started, wait = (float(m[k]) for k in
                                    ("ready", "started", "wait"))
            self.assertLessEqual(ready, started + 0.05)
            self.assertAlmostEqual(wait, started - ready, delta=0.11)
        # Band 2 was ready long before the queue took it: band 1 converted.
        self.assertGreaterEqual(float(got[1]["wait"]), self.HOLD_S - 0.15)
        self.assertGreaterEqual(float(got[1]["started"]),
                                float(got[0]["started"]) + self.HOLD_S - 0.1)


if __name__ == "__main__":
    unittest.main()
