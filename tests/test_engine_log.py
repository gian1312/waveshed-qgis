"""Unit tests for waveshed.core.engine_log — aether_core's console split
between the QGIS Log Messages panel and the engine log file.

The sample lines are verbatim from the owner's 2026-10-24 Site Analysis runs
(aether_core 0.4.10). QGIS stubs provided by conftest.py.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import os
import tempfile
import unittest
from unittest import mock

from waveshed.core import engine_log, terrain_adapter


#: What a user reads: stays in the panel (owner, 2026-11-07: the run's
#: internals go to the file too; 16 of the 47 lines of a 360° run stay).
PANEL = [
    "[2026-10-24T08:33:51Z INFO aether_core::crypto] [License] free "
    "non-commercial licence 2f69a81518882060cf7ef322c828735d valid until "
    "2027-01-10, node-locked to this machine",
    "[Core] GPU adapter: NVIDIA GeForce GTX 1080 Ti (DiscreteGpu), backend "
    "vulkan, driver \"NVIDIA 581.57\"",
    "[Core] Compute backend: GPU (auto-detected)",
    "[Core] ERP: 10.0W → EIRP: 42.1 dBm",
    "[Core] Job: site1_alt1_20261024_103304 | Mode: LOS | Format: 1BIT_LOS | "
    "Radius: 50km | Azimuth Limit: 0.0° - 360.0°",
    "[Core] Limits Configured: RAM=16GB (System Total: 31GB) | VRAM=7GB "
    "(Safety Margin Applied)",
    "[Core] Spatial Index: 26 best-fit tiles selected for 10.0m resolution",
    "[Core] Selected tile nominal resolutions: {\"10m\": 26}",
    "[Core] Output Grid: 10130 x 10098 pixels",
    "[Core] Output raster: ~0.00 GB on disk, 309.31 GB free in "
    "\"G:/Aether_Test\\\\site1_alt1_20261105_221051\"",
    "[Core] Range profile: 4096 range steps: 0-7000 m @ 2.00 m (3500 steps), "
    "7000-12000 m @ 29.94 m (167 steps), 12000-50000 m @ 88.58 m (429 steps)",
    "[Cache] Warning: Forced synchronous load of 26 tiles",
    "[Core] GeoTIFF export complete: \"G:/Aether_Test\\\\site1.tif\"",
    "[Core] Success. Output saved to \"G:/Aether_Test\\\\site1.bit\"",
    "[Profile] Finalize + write: 314ms",
    "[Profile] Total wall time: 1.26s",
]

#: What a developer reads: the file only.
FILE_ONLY = [
    # The run's internals (the owner's 2026-11-05 console, 0.4.10).
    "[Core] GPU probe: remembered vulkan adapter from C:\\Users\\x\\.aether"
    "\\bin\\gpu_probe.json (11.5 h old), 740 ms",
    "[Core] GPU probe: remembered adapter in C:\\Users\\x\\.aether\\bin"
    "\\gpu_probe.json is 13.0 d old (limit 7 d); full enumeration",
    "[Core] Cropped Bounds: lat[46.126438, 47.034753], lon[5.947602, "
    "7.269107]",
    "[Core] Terrain Format: R16SINT Raw (v1)",
    "[Core] Grid will use finest scale: 1.799856e-5 °/px",
    "[Core] Tile Grid: 21 rows x 31 cols = 651 cells (0.1000° x 0.1000° per "
    "cell)",
    "[Core] Grid Origin (snapped): lat=48.000000, lon=4.900000",
    "[Core] TX Pixel: (1688.20, 1682.86)",
    "[Core] Inputs: VRAM=7GB, Range=50000m, Res=30m, Tile=5556px, "
    "TileRes=1.3796m",
    "[Core] Solver Solution:",
    "  Theta (Wedge Angle): 359.9997 degrees",
    " Batching: 12 tiles (RAM Optimization)",
    "  VRAM Est: 1372.37 MB",
    " Polar Buffer: 22016 x 4096",
    " Banks: 1",
    " Output Tile Buffer: 4.06 MB (estimated)",
    "[Core] Geometry: 21992 Azimuths * 4096 Range",
    "[Core] Execution: 2 Wedges of 360.00°, Split into 1 Banks each",
    "[Core] Macro-Blocks System: Pre-allocating capacity for 1000000 blocks "
    "(129.7 MB staging)",
    "[Core] pipeline cache: hit (59 KB, unchanged)",
    "[Core] Output Tile Grid: 7 x 7 tiles (512 px each, 32768 bytes/tile)",
    "[Core] Terrain eviction schedule: 12 tiles tracked",
    "[Core] Precomputation finished in 5 ms",
    "[Core] Output tile eviction: 46 tiles tracked, 46 pinned until end "
    "(seam/center)",
    "[Core] Output tile precomputation: 0 ms",
    "[Core] startup: backend probe 740 ms (cached), adapter→device 217 ms, "
    "pipelines 19 ms (cache hit), shader module 20 ms",
    "[W01/02] 0.0°-360.0° | 77673 Blocks | gpu: 23 rb: 2 mrg: 2 fl: 0(0/0) | "
    "buf:26 | bits:915848 | ts:0.0+0.0ms",
    "[W02/02] 360.0°-360.0° | 53 Blocks | gpu: 4 rb: 0 mrg: 0 fl: 1(23/0) | "
    "buf:0 | bits:95 | ts:0.0+0.0ms",
    "[Core] Final flush: 0 tiles remaining in buffer",
    "[Core] Tile file complete: 26 tiles written (0.81 MB data)",
    "[Core] Exporting to GeoTIFF: \"G:/Aether_Test\\\\site1.tif\"",
    "[Export] TILED input: 10130×10098 | Source tile: 512px | Output tile: "
    "512 | Format: 1BIT_LOS | Compress: deflate-1",
    "[Export] SUCCESS | 0.07s | 1 MB in → 0 MB out (6.6:1) | 12 MB/s",
    # The developer dumps.
    "[2026-10-24T08:33:52Z INFO aether_core::solver] --- RESOURCE SOLVER ---",
    "[2026-10-24T08:33:52Z INFO aether_core::solver] Limits: VRAM: 7.52 GB | "
    "RAM: 17.18 GB | Buffer: 2.15 GB",
    "[2026-10-24T08:33:52Z INFO aether_core::solver] Safe Single Alloc: "
    "3.86 GB (10% Margin)",
    "[2026-10-24T08:33:52Z INFO aether_core::solver] >>> SOLVER SUCCESS: "
    "Theta = 359.9997 degrees",
    "[P:0]",
    "[S:Configuring Job (Mode: LOS)]",
    "[S:Executing Wedge 1/2]",
    "[Debug] TX Location: lat=46.621185, lon=7.045538",
    "[DIAG] erp_dbm=42.14 min_db=-140.00 azimuth_start=0.00 azimuth_end=360.00",
    "[SCHED] Dispatch scheduler: budget=150ms resident=8192 adapter=NVIDIA "
    "GeForce GTX 1080 Ti (DiscreteGpu)",
    "[SCHED] Learned c_ns=684.181 resident=8192 band_tax_ns=10992.9 | "
    "projection (100× GPU): slice=31424 band=5056",
    "[DIAG-POLAR] Sampled 1048576 f32 from polar_grid (stride=1)",
    "[DIAG-POLAR]   positive=409 zero=1048167 nodata=0 NaN=0",
    "[DIAG-POLAR]   [r=0, az=0] = 1.0000",
    "[Cache] Evicted 23 terrain tiles (graceful)",
    "╔══════════════════════════════════════════════════════════════╗",
    "║                   PROFILING SUMMARY (v3)                    ║",
    "╠══════════════════════════════════════════════════════════════╣",
    "║ Disk Load      │       232 │  38.6 │  116 │     0 │   232 ║",
    "║────────────────┼───────────┼───────┼──────┼───────┼───────║",
    "║ CPU Merge: 915848 visible bits total, 457924 avg/wedge",
    "╚══════════════════════════════════════════════════════════════╝",
    "[Profile] Top 5 slowest wedges:",
    "     W01: 540ms | disk:232(26) upl:275(26) gpu:23 poll:5 rb:2 mrg:2 | "
    "t=26 bits:915848",
    "[Profile] GPU poll + CPU merge time per wedge:",
    "     W01 7ms ████████████████████████████████████████ | bits: 915848",
    "[TiledInput] Index: 26 tiles at offset 851968",
    "[Profile] Setup (tiled): 0.00s",
    "[Export] Sparse: 374/400 tiles skipped (94%)",
    "[Profile] Main tiles written (tiled): 0.02s",
    "[Export] Overview 1: 4× → 2533×2525",
    "[Profile] Overview tiles written (tiled): 0.04s",
    "[Profile] IFDs written: 0.00s",
]

#: Looks like a diagnostic, says there is a problem: panel, always.
NEVER_MASKED = [
    "[2026-10-24T08:33:52Z WARN aether_core::solver] VRAM estimate above the "
    "safety margin",
    "[2026-10-24T08:33:52Z ERROR aether_core::engines::coverage] device lost",
    "[DIAG-POLAR]   *** NO POSITIVE VALUES — sweep output not surviving to "
    "readback ***",
    "[SCHED] dispatch failed: timeout",
    "[P:50] warning: wedge skipped",
    "thread 'main' panicked at src/main.rs:10:5",
    "Error: terrain tile unreadable",
]


class TestIsDiagnostic(unittest.TestCase):
    def test_the_panel_lines_stay(self):
        for line in PANEL:
            self.assertFalse(engine_log.is_diagnostic(line), line)

    def test_the_developer_lines_go_to_the_file(self):
        for line in FILE_ONLY:
            self.assertTrue(engine_log.is_diagnostic(line), line)

    def test_a_problem_is_never_masked(self):
        for line in NEVER_MASKED:
            self.assertFalse(engine_log.is_diagnostic(line), line)

    def test_the_licence_line_is_not_a_solver_line(self):
        # Both come through env_logger; only the solver's INFO is a dump.
        self.assertFalse(engine_log.is_diagnostic(PANEL[0]))
        self.assertTrue(engine_log.is_diagnostic(FILE_ONLY[0]))

    def test_empty_is_nothing_to_show(self):
        self.assertTrue(engine_log.is_diagnostic(""))
        self.assertTrue(engine_log.is_diagnostic("   "))


class _Panel:
    def __init__(self):
        self.lines = []

    def __call__(self, text):
        self.lines.append(text)


class TestEngineLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "logs", "aether_core-20261025.log")
        p = mock.patch.object(engine_log, "engine_log_path",
                              return_value=self.path)
        p.start()
        self.addCleanup(p.stop)
        engine_log._announced = None
        self.addCleanup(setattr, engine_log, "_announced", None)

    def _file_lines(self):
        with open(self.path, encoding="utf-8") as fh:
            return fh.read().splitlines()

    def test_file_gets_everything_panel_gets_the_human_lines(self):
        panel = _Panel()
        with engine_log.EngineLog("Site 1 (46.62, 7.05)", panel) as log:
            for line in PANEL + FILE_ONLY + NEVER_MASKED:
                log.line(line)
        body = self._file_lines()
        self.assertTrue(body[0].startswith("=== "), body[0])
        self.assertIn("Site 1 (46.62, 7.05)", body[0])
        self.assertEqual(body[1:], PANEL + FILE_ONLY + NEVER_MASKED,
                         "the file keeps every line, in order")
        self.assertEqual(panel.lines[0],
                         f"engine log (full console): {self.path}")
        self.assertEqual(panel.lines[1:], PANEL + NEVER_MASKED)

    def test_the_path_is_announced_once_a_session(self):
        panel = _Panel()
        engine_log.EngineLog("a", panel).close()
        engine_log.EngineLog("b", panel).close()
        self.assertEqual(
            [l for l in panel.lines if l.startswith("engine log")],
            [f"engine log (full console): {self.path}"])
        self.assertEqual(
            sum(1 for l in self._file_lines() if l.startswith("=== ")), 2,
            "both runs are in the one file")

    def test_two_runs_append_to_the_same_day_file(self):
        panel = _Panel()
        with engine_log.EngineLog("first", panel) as log:
            log.line("[Core] first")
        with engine_log.EngineLog("second", panel) as log:
            log.line("[Core] second")
        body = self._file_lines()
        self.assertIn("[Core] first", body)
        self.assertIn("[Core] second", body)
        self.assertLess(body.index("[Core] first"), body.index("[Core] second"))

    def test_no_file_means_no_masking(self):
        # The logs dir cannot be made (its parent is a file): every line to
        # the panel — exactly the behaviour before the file existed — and
        # one notice, not one per run.
        blocker = os.path.join(self.tmp.name, "blocker")
        with open(blocker, "w") as fh:
            fh.write("")
        with mock.patch.object(
                engine_log, "engine_log_path",
                return_value=os.path.join(blocker, "aether_core-20261025.log")):
            panel = _Panel()
            with engine_log.EngineLog("a", panel) as log:
                for line in FILE_ONLY:
                    log.line(line)
            with engine_log.EngineLog("b", panel) as log:
                log.line(FILE_ONLY[0])
        notices = [l for l in panel.lines if l.startswith("engine log: no file")]
        self.assertEqual(len(notices), 1, panel.lines)
        self.assertIn("the panel shows every line", notices[0])
        self.assertEqual([l for l in panel.lines if not l.startswith("engine log")],
                         FILE_ONLY + [FILE_ONLY[0]])

    def test_a_write_failure_falls_back_to_the_panel(self):
        panel = _Panel()
        log = engine_log.EngineLog("a", panel)
        log.line(FILE_ONLY[0])                 # written, not shown
        log._fh.close()                        # the disk went away mid-run
        log.line(FILE_ONLY[1])                 # cannot be written: shown
        log.line("[Core] still here")
        log.close()
        self.assertEqual(panel.lines[1:], [FILE_ONLY[1], "[Core] still here"])
        self.assertEqual(self._file_lines()[1:], [FILE_ONLY[0]])

    def test_close_is_idempotent(self):
        log = engine_log.EngineLog("a", _Panel())
        log.close()
        log.close()
        log.line("[Core] after close")  # no file any more: panel, no raise


class TestRotation(unittest.TestCase):
    def test_engine_logs_rotate_with_the_converter_diagnostics(self):
        for name in ("aether_core-20261025.log", "aether_converter-20261025.log"):
            self.assertIsNotNone(terrain_adapter._DIAG_FILE_RE.match(name), name)
        for name in ("aether_core.log", "aether_core-2026.log", "manifest.json",
                     "pool", "aether_core-20261025.log.bak"):
            self.assertIsNone(terrain_adapter._DIAG_FILE_RE.match(name), name)

    def test_old_engine_logs_are_deleted_new_ones_kept(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.path.join(d, "aether_core-20250101.log")
            new = os.path.join(d, "aether_core-20261025.log")
            other = os.path.join(d, "notes.txt")
            for p in (old, new, other):
                with open(p, "w") as fh:
                    fh.write("x")
            stale = 1.0  # 1970: older than any retention
            os.utime(old, (stale, stale))
            os.utime(other, (stale, stale))
            terrain_adapter._rotate_diag_files(d)
            self.assertFalse(os.path.exists(old))
            self.assertTrue(os.path.exists(new))
            self.assertTrue(os.path.exists(other), "only our log files")


if __name__ == "__main__":
    unittest.main()
