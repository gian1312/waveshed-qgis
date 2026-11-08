"""Cold folder scans through ``aether_converter probe`` (2026-10-21), the
index they leave for the next session, and the Windows cold-scan note.

* ``FolderIndex.refresh`` reads new/changed plain GeoTIFFs through ONE
  converter probe run (>= 0.2.7) and builds their entries from its lines —
  equal to the GDAL probe's — while everything else (sidecars, other formats,
  ``ok: false`` lines, files changed since the listing, a failed run) goes
  through the in-process probes unchanged. Cancel kills the run and keeps what
  was read; the footprint CRS work is ONE batched TransformPoints per EPSG
  code, never one per file. The runs here drive the real ``run_probe``
  subprocess path against a stand-in converter that replays the real
  converter's lines (live when ``aether_converter`` is on AETHER_BIN_DIR/PATH,
  else ``fixtures/probe_parity.jsonl``).
* The index such a scan saves (``<cache>/index/<md5>.json``) is valid in the
  next session — in this process with every in-memory index dropped, and in
  a real second process: every file only stat'ed, no probe run, no data file
  opened, whatever spelling of the folder (trailing separator, relative,
  Windows letter case) — and a refused rename of the save is retried, not
  dropped (real converter).
* ``terrain_folder_scan.scan_folder`` on Windows adds ONE note to its
  progress dialog when a bulk read of >= COLD_SCAN_NOTE_MIN_FILES files
  starts (logged once per folder); nothing below it, nothing off Windows,
  and the module never starts a process or touches Windows Defender (the
  exclusion offer was removed, owner decision 2026-10-23).
"""

# Bootstrap stubs before any plugin import.
import conftest  # noqa: F401 — QGIS/PyQt stubs

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from _real_gdal import GDAL, HAVE_GDAL, real_gdal, write_tif
from test_terrain_index_fast import (EXACT, FLOATS, _close, probe_cases,
                                     probe_facts, real_converter, write_case)

import waveshed.core.terrain_adapter as ta
import waveshed.gui.terrain_folder_scan as tfs
from waveshed.core import binary_manager as bm
from waveshed.core import terrain_index as ti

#: A stand-in ``aether_converter probe``: replays the lines it is given per
#: path (unknown paths: ``ok: false``), records the list it got, prints the
#: ``[Probe]`` summary on stderr. ``_cfg``: ``open_ms``, ``exit`` (fail
#: before any line), ``stall_after`` (go quiet after n lines).
FAKE_CONVERTER = r'''
import json, os, sys, time
args = sys.argv[1:]
with open(args[args.index("--replay") + 1], encoding="utf-8") as fh:
    replay = json.load(fh)
cfg = replay.pop("_cfg", {})
with open(args[args.index("--list") + 1], encoding="utf-8") as fh:
    paths = [l for l in fh.read().split("\n") if l.strip()]
with open(cfg["seen"], "w", encoding="utf-8") as fh:
    json.dump({"pid": os.getpid(), "paths": paths, "argv": args}, fh)
if cfg.get("exit"):
    sys.stderr.write("error: boom\n")
    sys.exit(cfg["exit"])
ok = 0
for i, p in enumerate(paths):
    if i == cfg.get("stall_after"):
        sys.stdout.flush()
        time.sleep(60)
    line = replay.get(p) or {"schema": "aether-probe/1", "path": p, "ok": False,
                             "reason": "cannot open: not recorded",
                             "size": None, "mtime": None, "mtime_ns": None}
    ok += bool(line.get("ok"))
    sys.stdout.write(json.dumps(line) + "\n")
sys.stdout.flush()
sys.stderr.write("[Probe] %d files, %d ok, 0.01s, open %.2f ms avg, read 0.10 ms avg\n"
                 % (len(paths), ok, cfg.get("open_ms", 0.05)))
'''

LOG_RE = re.compile(r"folder index .+ probe: (\d+) files in [0-9.]+ s \(open ([0-9.?]+) ms, "
                    r"read ([0-9.?]+) ms avg\), entries built in [0-9.]+ s")


def _gdal_entry(path):
    with mock.patch.object(ti, "probe_geotiff_fast", return_value=None):
        return ti.probe_file(path)


@unittest.skipUnless(HAVE_GDAL, "needs the real GDAL")
class _ProbeFolder(unittest.TestCase):
    """dem/ with the probe parity cases + files the probe must never see,
    a private cache, real GDAL, the stand-in converter wired in."""

    CASES = ("p_lv95_f32.tif", "p_lv95_int16_lzw_tiled.tif", "p_lv95_zstd.tif",
             "p_lv95_nodata_nan.tif", "p_lv95_scaled.tif", "p_lv95_rotated.tif",
             "p_lv95_big_tile.tif", "p_wgs84.tif", "p_utm32.tif", "p_lv95_byte.tif",
             "d_rgb.tif", "d_nocrs.tif", "d_custom_crs.tif", "d_cut.tif")

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ws_probe_")
        self.dem = os.path.join(self.root, "dem")
        os.makedirs(os.path.join(self.dem, "sub"))
        self._gdal = real_gdal()
        self._gdal.__enter__()
        cases = probe_cases()
        self.paths = {n: write_case(self.dem if i % 2 else os.path.join(self.dem, "sub"),
                                    n, cases[n])
                      for i, n in enumerate(self.CASES)}
        # Never listed for the probe: a sidecar can override what the header
        # says, and only .tif/.tiff are GeoTIFFs to it.
        self.side = write_tif(os.path.join(self.dem, "side.tif"),
                              {"west": 7.40, "east": 7.41, "south": 46.90, "north": 46.91})
        with open(self.side + ".aux.xml", "w") as fh:
            fh.write("<PAMDataset/>")
        src = write_tif(os.path.join(self.root, "grid_src.tif"),
                        {"west": 7.40, "east": 7.41, "south": 46.90, "north": 46.91})
        self.asc = os.path.join(self.dem, "grid.asc")
        GDAL.Translate(self.asc, src, format="AAIGrid")
        facts, self.source = probe_facts(self.paths)
        self.replay_path = os.path.join(self.root, "replay.json")
        self.seen_path = os.path.join(self.root, "seen.json")
        self.fake = os.path.join(self.root, "fake_converter.py")
        with open(self.fake, "w", encoding="utf-8") as fh:
            fh.write(FAKE_CONVERTER)
        self.replay = dict(facts)
        self.logs = []
        self._patches = [
            mock.patch.object(ta, "get_cache_dir",
                              return_value=os.path.join(self.root, "cache")),
            mock.patch.object(ti, "probe_converter", return_value="aether_converter"),
            mock.patch.object(ti, "_probe_command", side_effect=lambda exe, lst: [
                sys.executable, self.fake, "--replay", self.replay_path,
                "probe", "--list", lst]),
            mock.patch.object(ti, "PROBE_MIN_FILES", 1),
            mock.patch.object(ti, "_log", side_effect=self.logs.append),
        ]
        for p in self._patches:
            p.start()
        ti.forget_indexes()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._gdal.__exit__(None, None, None)
        ti.forget_indexes()
        shutil.rmtree(self.root, ignore_errors=True)

    def write_replay(self, **cfg):
        cfg["seen"] = self.seen_path
        with open(self.replay_path, "w", encoding="utf-8") as fh:
            json.dump(dict(self.replay, _cfg=cfg), fh)

    def seen(self):
        with open(self.seen_path, encoding="utf-8") as fh:
            return json.load(fh)

    def candidates(self):
        return sorted(self.paths.values())

    def assertEntryEqual(self, got, want, name):
        for k in EXACT:
            self.assertEqual(got.get(k), want.get(k), f"{name}: {k}")
        for k in FLOATS:
            self.assertTrue(_close(got.get(k), want.get(k)), f"{name}: {k}")
        for side in ("native_bounds", "wgs84_bounds"):
            for k in ("west", "east", "south", "north"):
                self.assertAlmostEqual(got[side][k], want[side][k], places=7,
                                       msg=f"{name}: {side}.{k}")
        self.assertEqual(got["footprint"], want["footprint"], name)
        self.assertEqual((got["size"], got["mtime_ns"]), (want["size"], want["mtime_ns"]), name)


class TestRefreshThroughTheProbe(_ProbeFolder):
    def test_one_probe_run_reads_the_geotiffs_and_gdal_the_rest(self):
        self.write_replay(open_ms=3.25)
        index = ti.get_index(self.dem)
        stats = index.refresh()
        seen = self.seen()
        self.assertEqual(seen["paths"], self.candidates(),
                         "exactly the plain GeoTIFFs, sorted, once")
        self.assertEqual(seen["argv"][-3:-1], ["probe", "--list"])
        self.assertEqual(stats.probe_engine, "converter")
        self.assertEqual(stats.open_ms, 3.25)
        self.assertEqual(stats.new, len(self.paths) + 2)
        for name, path in self.paths.items():
            with self.subTest(name, source=self.source):
                want = _gdal_entry(path)
                got = index.entries[path]
                if name.startswith("p_"):
                    self.assertEntryEqual(got, want, name)
                else:
                    self.assertEqual((got["readable"], got["reason"]),
                                     (want["readable"], want["reason"]), name)
                self.assertEqual(ti.problem_of(got), ti.problem_of(want), name)
        for path in (self.side, self.asc):
            self.assertEqual(index.entries[path]["readable"], True, path)
        lines = [m for m in map(LOG_RE.search, self.logs) if m]
        self.assertEqual(len(lines), 1, self.logs)
        self.assertEqual(lines[0].groups(), (str(len(self.paths)), "3.25", "0.10"))
        # Saved, and a repeat refresh launches nothing.
        ti.forget_indexes()
        with mock.patch.object(ti, "run_probe", side_effect=AssertionError("probed again")):
            again = ti.get_index(self.dem).refresh()
        self.assertEqual((again.new, again.changed, again.unchanged),
                         (0, 0, len(self.paths) + 2))
        self.assertEqual(ti.get_index(self.dem).entries[self.paths["p_utm32.tif"]]["crs_authid"],
                         "EPSG:32632")

    def test_cancel_kills_the_probe_and_keeps_what_was_read(self):
        self.write_replay(stall_after=3)
        # The probe's own Popen, to ask IT whether the process is gone — never
        # a signal to a PID (os.kill(pid, 0) is TerminateProcess on Windows:
        # an unrelated process, should the PID have been reused).
        launched = []
        real_popen = subprocess.Popen

        def popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            launched.append(proc)
            return proc
        t0 = time.monotonic()
        index = ti.get_index(self.dem)
        with self.assertRaises(ti.ScanCancelled), \
             mock.patch.object(ti.subprocess, "Popen", popen):
            index.refresh(should_cancel=lambda: time.monotonic() - t0 > 1.0)
        self.assertLess(time.monotonic() - t0, 15.0, "the stalled converter was not killed")
        self.assertEqual([p.pid for p in launched], [self.seen()["pid"]],
                         "one probe process, the one that stalled")
        self.assertIsNotNone(launched[0].returncode,
                             "killed and reaped by run_probe (returncode set)")
        self.assertIsNotNone(launched[0].poll())
        first = self.candidates()[:3]
        ti.forget_indexes()
        reloaded = ti.get_index(self.dem)
        reloaded.load()
        kept = set(reloaded.entries)
        self.assertTrue(set(p for p in first if os.path.basename(p).startswith("p_")) <= kept,
                        (first, kept))
        self.assertFalse(set(self.candidates()[3:]) & kept, "lines never read were kept")
        self.assertTrue(any("cancelled" in m for m in self.logs), self.logs)
        # The next refresh probes only the rest.
        self.write_replay()
        reloaded.refresh()
        self.assertEqual(self.seen()["paths"], [p for p in self.candidates() if p not in kept])

    def test_a_failed_probe_leaves_every_file_to_the_in_process_probe(self):
        self.write_replay(exit=2)
        index = ti.get_index(self.dem)
        stats = index.refresh()
        self.assertEqual(stats.probe_engine, "")
        self.assertIsNone(stats.open_ms)
        for name, path in self.paths.items():
            want = _gdal_entry(path)
            self.assertEqual(index.entries[path]["readable"], want["readable"], name)
        self.assertTrue(any("converter probe failed (exit 2: error: boom)" in m
                            for m in self.logs), self.logs)

    def test_a_line_that_does_not_match_the_listing_goes_to_gdal(self):
        changed = self.paths["p_utm32.tif"]
        wrong_path = self.paths["p_wgs84.tif"]
        self.replay[changed] = dict(self.replay[changed], size=1)          # changed since listing
        self.replay[wrong_path] = dict(self.replay[wrong_path], path="elsewhere.tif")
        self.write_replay()
        spy = mock.Mock(side_effect=ti.probe_file)
        with mock.patch.object(ti, "probe_file", spy):
            index = ti.get_index(self.dem)
            index.refresh()
        in_process = {c.args[0] for c in spy.call_args_list}
        self.assertTrue({changed, wrong_path, self.side, self.asc} <= in_process, in_process)
        self.assertNotIn(self.paths["p_lv95_f32.tif"], in_process)
        self.assertEntryEqual(index.entries[changed], _gdal_entry(changed), "changed")

    def test_old_or_missing_converter_means_the_in_process_probes(self):
        for p in self._patches[1:2]:
            p.stop()
        try:
            with mock.patch.object(bm, "find_binary", side_effect=RuntimeError("absent")):
                self.assertIsNone(ti.probe_converter())
            with mock.patch.object(bm, "find_binary", return_value="/x/aether_converter"):
                for version, want in (("0.2.6", None), (None, None),
                                      ("0.2.7", "/x/aether_converter"),
                                      ("0.3.0", "/x/aether_converter")):
                    with mock.patch.object(bm, "probe_converter_version", return_value=version):
                        self.assertEqual(ti.probe_converter(), want, version)
            self.assertEqual(bm.CONVERTER_PROBE_MIN_VERSION, "0.2.7")
            with mock.patch.object(ti, "probe_converter", return_value=None), \
                 mock.patch.object(ti, "run_probe", side_effect=AssertionError("ran")):
                stats = ti.get_index(self.dem).refresh()
            self.assertEqual(stats.probe_engine, "")
            self.assertEqual(len(ti.get_index(self.dem).entries), len(self.paths) + 2)
        finally:
            self._patches[1].start()

    def test_a_few_files_never_launch_the_converter(self):
        with mock.patch.object(ti, "PROBE_MIN_FILES", len(self.paths) + 1), \
             mock.patch.object(ti, "probe_converter", side_effect=AssertionError("looked up")):
            stats = ti.get_index(self.dem).refresh()
        self.assertEqual(stats.probe_engine, "")

    def test_the_bulk_read_opens_with_a_tick_before_any_file_is_read(self):
        """``(0, n, "reading")`` is the first tick of each bulk read — before
        the converter starts (its own ticks come every 256 lines) and before
        the in-process probes read their first file — with n = the files
        that read covers: the GUI's cold-scan note keys on it."""
        self.write_replay()
        events = []
        real_run, real_file = ti.run_probe, ti.probe_file

        def run(exe, paths, **kw):
            events.append(("probe", len(paths)))
            return real_run(exe, paths, **kw)

        def one(path, *a):
            events.append(("file", path))
            return real_file(path, *a)
        with mock.patch.object(ti, "run_probe", side_effect=run), \
             mock.patch.object(ti, "probe_file", side_effect=one):
            ti.get_index(self.dem).refresh(
                progress=lambda done, total, phase: events.append((phase, done, total)))
        n = len(self.candidates())
        probe_at = events.index(("probe", n))
        self.assertEqual([e for e in events[:probe_at] if e[0] == "reading"],
                         [("reading", 0, n)], events)
        files = [i for i, e in enumerate(events) if e[0] == "file"]
        self.assertTrue(files, "the sidecar file and the .asc are read in-process")
        before = [e for e in events[probe_at:files[0]] if e[0] == "reading"]
        self.assertEqual(before[-1:], [("reading", 0, len(files))], events)


@unittest.skipUnless(HAVE_GDAL, "needs the real GDAL")
class TestFootprintsAreBatchedPerCrs(unittest.TestCase):
    """ONE TransformPoints per EPSG code for every file's ring (+ one for the
    80-point edges of the files whose ring cannot stand in), never per file —
    and the result is _geo_fields' file by file."""

    def setUp(self):
        self._gdal = real_gdal()
        self._gdal.__enter__()

    def tearDown(self):
        self._gdal.__exit__(None, None, None)

    @staticmethod
    def line(code, x0, y0, px, n=500):
        return {"schema": ti.PROBE_SCHEMA, "path": "x", "ok": True, "size": 10**7,
                "mtime": 1, "mtime_ns": 1, "width": n, "height": n, "bands": 1,
                "dtype": "f32", "compression": {"code": 8, "name": "DEFLATE"},
                "predictor": 3, "tiled": True, "tile_w": 256, "tile_h": 256,
                "epsg": code, "proj": None, "geotransform": [x0, px, 0.0, y0, 0.0, -px],
                "rotated": False, "pixel_is_point": False, "nodata": -9999.0,
                "scale": None, "offset": None, "overviews": 0, "ifd_offset": 8,
                "last_chunk_end": 10**7}

    def test_one_call_per_crs_and_file_by_file_values(self):
        facts = {}
        for i in range(200):            # 1 km LV95 tiles (swissALTI3D 2 m)
            facts[f"lv95_{i}.tif"] = self.line(2056, 2_590_000.0 + (i % 20) * 1000.0,
                                               1_210_000.0 - (i // 20) * 1000.0, 2.0)
        for i in range(100):            # 1 km UTM 32N tiles
            facts[f"utm_{i}.tif"] = self.line(32632, 400_000.0 + (i % 10) * 1000.0,
                                              5_200_000.0 - (i // 10) * 1000.0, 2.0)
        for i in range(3):              # 100 km tiles: need the 80-point edges
            facts[f"big_{i}.tif"] = self.line(32632, 300_000.0 + i * 100_000.0,
                                              5_300_000.0, 200.0)
        facts["wgs.tif"] = self.line(4326, 7.0, 47.0, 0.001)
        calls = {}
        real_transform = ti._transform

        class Counting:
            def __init__(self, ct, key):
                self.ct, self.key = ct, key

            def TransformPoints(self, pts):
                calls[self.key] = calls.get(self.key, 0) + 1
                return self.ct.TransformPoints(pts)

        def counting(wkt, inverse=False):
            ct = real_transform(wkt, inverse)
            return None if ct is None else Counting(ct, wkt[-40:])

        with mock.patch.object(ti, "_transform", side_effect=counting):
            got = ti.entries_from_probe(facts)
        self.assertEqual(len(got), len(facts))
        per_crs = sorted(calls.values())
        self.assertEqual(per_crs, [1, 2], calls)   # LV95 ring only; UTM ring + dense
        for path, f in facts.items():
            ref = ti._geo_fields(tuple(f["geotransform"]), f["width"], f["height"],
                                 ti._epsg_crs(f["epsg"]))
            e = got[path]
            self.assertEqual(e["footprint"], ref["footprint"], path)
            self.assertEqual(e["native_bounds"], ref["native_bounds"], path)
            for k in ("px_x", "px_y", "px_x_m", "px_y_m", "px_m"):
                self.assertEqual(e[k], ref[k], path)
            for k in ("west", "east", "south", "north"):
                self.assertLessEqual(abs(e["wgs84_bounds"][k] - ref["wgs84_bounds"][k]),
                                     ti._RING_BBOX_EPS_DEG, path)
            if path.startswith(("big", "wgs")):
                self.assertEqual(e["wgs84_bounds"], ref["wgs84_bounds"], path)


# ---------------------------------------------------------------------------
# The index outlives the session
# ---------------------------------------------------------------------------

_TESTS = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_TESTS)
#: Scratch inside the project's gitignored .tmp (never the container drive).
_SCRATCH = os.path.join(_REPO, ".tmp", "test_probe_scan")

#: The second process. It imports the plugin the way the suite does
#: (conftest's QGIS/PyQt stubs, the real GDAL through _real_gdal), points the
#: terrain cache and the converter where the first process had them, and
#: refreshes the folder once per spelling — each a session of its own, every
#: in-memory index dropped — recording every file terrain_index opens, every
#: process started and every probe.
SECOND_SESSION = r'''
import json, os, sys
tests, repo, cache, exe, spellings = json.loads(sys.argv[1])
sys.path[:0] = [tests, repo]
import conftest  # noqa: F401 — the suite's QGIS/PyQt stubs
from unittest import mock
from _real_gdal import real_gdal
import waveshed.core.terrain_adapter as ta
from waveshed.core import binary_manager as bm
from waveshed.core import terrain_index as ti

events = {"open": [], "popen": [], "run_probe": [], "probe_file": [], "log": []}
real_open, real_popen = open, ti.subprocess.Popen
real_run_probe, real_probe_file = ti.run_probe, ti.probe_file

def spy_open(file, *a, **k):
    events["open"].append(os.fspath(file))
    return real_open(file, *a, **k)

def spy_popen(args, *a, **k):
    events["popen"].append([str(x) for x in args])
    return real_popen(args, *a, **k)

def spy_run_probe(exe_, paths, *a, **k):
    events["run_probe"].append(len(paths))
    return real_run_probe(exe_, paths, *a, **k)

def spy_probe_file(path, *a, **k):
    events["probe_file"].append(path)
    return real_probe_file(path, *a, **k)

def find(name):
    if name == "aether_converter":
        return exe
    raise RuntimeError(name + " not here")

out = []
with real_gdal(), \
        mock.patch.object(ta, "get_cache_dir", return_value=cache), \
        mock.patch.object(bm, "find_binary", side_effect=find), \
        mock.patch.object(ti, "open", spy_open, create=True), \
        mock.patch.object(ti.subprocess, "Popen", spy_popen), \
        mock.patch.object(ti, "run_probe", spy_run_probe), \
        mock.patch.object(ti, "probe_file", spy_probe_file), \
        mock.patch.object(ti, "_log", side_effect=events["log"].append):
    for spelling in spellings:
        ti.expire_all()
        ti.forget_indexes()
        for got in events.values():
            got.clear()
        index = ti.get_index(spelling)
        st = index.refresh()
        out.append({"spelling": spelling,
                    "stats": [st.files, st.new, st.changed, st.removed,
                              st.unchanged],
                    "probe_engine": st.probe_engine, "open_ms": st.open_ms,
                    "folder": index.folder, "pid": os.getpid(),
                    "events": {k: list(v) for k, v in events.items()}})
print("RESULT " + json.dumps(out))
'''


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


@unittest.skipUnless(HAVE_GDAL, "needs the real GDAL")
class TestTheIndexOutlivesTheSession(unittest.TestCase):
    """A cold scan through the REAL converter probe saves the index; the next
    session — this process with every in-memory index dropped, or a real
    second process — only stats the files: no probe, no data file opened,
    the index read from ``<cache>/index/<md5>.json``, for every spelling of
    the folder."""

    #: >= PROBE_MIN_FILES plain GeoTIFFs: the converter-probe path.
    N = 70

    @classmethod
    def setUpClass(cls):
        cls.exe = real_converter()

    def setUp(self):
        if self.exe is None:
            self.skipTest("needs aether_converter >= 0.2.7 on AETHER_BIN_DIR "
                          "or PATH (the converter-probe path)")
        self.assertGreaterEqual(self.N, ti.PROBE_MIN_FILES)
        os.makedirs(_SCRATCH, exist_ok=True)
        self.root = tempfile.mkdtemp(prefix="s_", dir=_SCRATCH)
        self.dem = os.path.join(self.root, "DEM")
        self.cache = os.path.join(self.root, "cache")
        self._gdal = real_gdal()
        self._gdal.__enter__()
        self.paths = []
        for i in range(self.N):                 # 1 km LV95 tiles, 2 folders
            x0 = 2_600_000.0 + (i % 10) * 1000.0
            y0 = 1_200_000.0 - (i // 10) * 1000.0
            sub = os.path.join(self.dem, "a" if i % 2 else "b")
            os.makedirs(sub, exist_ok=True)
            self.paths.append(write_tif(
                os.path.join(sub, f"t{i:03d}.tif"),
                {"west": x0, "east": x0 + 1000.0, "south": y0 - 1000.0,
                 "north": y0}, epsg=2056, value=400.0 + i))
        self.logs = []
        self._patches = [
            mock.patch.object(ta, "get_cache_dir", return_value=self.cache),
            mock.patch.object(bm, "find_binary", side_effect=self._find),
            mock.patch.object(ti, "_log", side_effect=self.logs.append),
        ]
        for p in self._patches:
            p.start()
        ti.forget_indexes()
        # Computed here, not through terrain_index: md5 of the normalised path.
        self.stored = os.path.join(self.cache, "index", hashlib.md5(
            os.path.normcase(os.path.abspath(self.dem)).encode("utf-8"),
            usedforsecurity=False).hexdigest() + ".json")

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._gdal.__exit__(None, None, None)
        ti.forget_indexes()
        shutil.rmtree(self.root, ignore_errors=True)

    def _find(self, name):
        if name == "aether_converter":
            return self.exe
        raise RuntimeError(f"{name} not here")

    def cold(self):
        st = ti.get_index(self.dem).refresh()
        self.assertEqual((st.files, st.new, st.changed, st.removed, st.unchanged),
                         (self.N, self.N, 0, 0, 0))
        self.assertEqual(st.probe_engine, "converter")
        self.assertIsNotNone(st.open_ms)
        self.assertEqual(len([m for m in self.logs if " probe: " in m]), 1, self.logs)
        self.assertTrue(os.path.isfile(self.stored), self.stored)
        # The stored change detector is exactly what the listing gives.
        with open(self.stored, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(doc["entries"]), sorted(self.paths))
        for p in self.paths:
            e, st_p = doc["entries"][p], os.stat(p)
            self.assertEqual((e["size"], e["mtime_ns"], e["probe"]),
                             (st_p.st_size, st_p.st_mtime_ns, ti.PROBE_VERSION), p)
            self.assertTrue(e["readable"], p)
        self.logs.clear()
        return st

    def stat_only(self, spelling, new_session=True):
        """Refresh *spelling* as the next session would (or, *new_session*
        False, as the next run of this one): ``(stats, files opened by
        terrain_index)``; any probe or process fails the test."""
        if new_session:
            ti.forget_indexes()
        ti.expire_all()
        opened = []
        real_open = open

        def spy_open(file, *a, **k):
            opened.append(os.fspath(file))
            return real_open(file, *a, **k)
        with mock.patch.object(ti, "open", spy_open, create=True), \
             mock.patch.object(ti, "run_probe", side_effect=AssertionError("probe ran")), \
             mock.patch.object(ti, "probe_file", side_effect=AssertionError("file probed")), \
             mock.patch.object(ti.subprocess, "Popen",
                               side_effect=AssertionError("process started")):
            st = ti.get_index(spelling).refresh()
        self.assertEqual((st.files, st.new, st.changed, st.removed, st.unchanged),
                         (self.N, 0, 0, 0, self.N), spelling)
        self.assertEqual((st.probe_engine, st.open_ms), ("", None), spelling)
        self.assertFalse([m for m in self.logs if " probe: " in m], self.logs)
        self.assertEqual(os.path.normcase(ti.get_index(spelling).folder),
                         os.path.normcase(os.path.abspath(self.dem)))
        return st, opened

    def test_a_new_session_only_stats_the_files(self):
        self.cold()
        saved = _read(self.stored)
        for spelling in (self.dem, self.dem + os.sep, os.path.relpath(self.dem)):
            with self.subTest(spelling=spelling):
                _st, opened = self.stat_only(spelling)
                self.assertEqual(opened, [self.stored], "read from the stored index only")
        self.assertEqual(_read(self.stored), saved, "nothing re-indexed")

    def test_the_next_run_of_the_same_session_only_stats_the_files(self):
        self.cold()
        _st, opened = self.stat_only(self.dem, new_session=False)
        self.assertEqual(opened, [], "the index is in memory already")

    def test_a_second_process_only_stats_the_files(self):
        self.cold()
        saved = _read(self.stored)
        spellings = [self.dem, self.dem + os.sep, "DEM"]       # "DEM": cwd = root
        done = subprocess.run(
            [sys.executable, "-c", SECOND_SESSION,
             json.dumps([_TESTS, _REPO, self.cache, self.exe, spellings])],
            cwd=self.root, capture_output=True, text=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        lines = [ln for ln in done.stdout.splitlines() if ln.startswith("RESULT ")]
        self.assertEqual(len(lines), 1, done.stdout[-2000:])
        runs = json.loads(lines[0][len("RESULT "):])
        self.assertEqual([r["spelling"] for r in runs], spellings)
        for r in runs:
            with self.subTest(spelling=r["spelling"]):
                self.assertNotEqual(r["pid"], os.getpid())
                self.assertEqual(r["stats"], [self.N, 0, 0, 0, self.N])
                self.assertEqual((r["probe_engine"], r["open_ms"]), ("", None))
                self.assertEqual(r["folder"], os.path.abspath(self.dem))
                ev = r["events"]
                self.assertEqual((ev["run_probe"], ev["probe_file"], ev["popen"]),
                                 ([], [], []), ev)
                self.assertEqual(ev["open"], [self.stored], "read from the stored index only")
                self.assertFalse([m for m in ev["log"] if " probe: " in m], ev["log"])
        self.assertEqual(_read(self.stored), saved, "nothing re-indexed")

    def test_another_letter_case_of_the_folder_reads_the_same_entries(self):
        """Windows (normcase: one index file for C:\\Data and c:\\data): the
        listing goes through the spelling the index was saved with, so the
        entries keep matching — a second spelling used to list every file
        under new keys: all "new", the whole folder probed again, and the
        next session with the first spelling the same once more. Simulated
        with Windows' normcase; the stored spelling is the one on disk."""
        win_norm = lambda f: os.path.abspath(f).lower()  # noqa: E731
        self.stored = os.path.join(self.cache, "index", hashlib.md5(
            win_norm(self.dem).encode("utf-8"), usedforsecurity=False).hexdigest() + ".json")
        with mock.patch.object(ti, "_norm_folder", side_effect=win_norm):
            self.cold()
            _st, opened = self.stat_only(self.dem.lower())
            self.assertEqual(opened, [self.stored])
            self.assertEqual(ti.get_index(self.dem.lower()).folder,
                             os.path.abspath(self.dem), "the stored spelling")

    def test_a_refused_rename_of_the_save_is_retried(self):
        """Windows refuses the rename over a file another process holds open
        (Defender scanning the index just written, a second QGIS reading
        it). The save retries like every pool tile (replace_with_retry);
        still refused, it says so — the old silent drop cost the next
        session the whole folder again."""
        real_replace = os.replace
        refusals = []

        def refuse_twice(src, dst):
            if dst == self.stored and len(refusals) < 2:
                refusals.append(dst)
                raise PermissionError(13, "The process cannot access the file")
            return real_replace(src, dst)
        with mock.patch.object(os, "replace", side_effect=refuse_twice):
            self.cold()
        self.assertEqual(len(refusals), 2)
        _st, opened = self.stat_only(self.dem)
        self.assertEqual(opened, [self.stored])
        # Refused every time: logged, no temp file left, this session goes on.
        shutil.rmtree(self.cache)
        ti.forget_indexes()

        def refuse(src, dst):
            if dst == self.stored:
                raise PermissionError(13, "The process cannot access the file")
            return real_replace(src, dst)
        with mock.patch.object(os, "replace", side_effect=refuse), \
             mock.patch.object(time, "sleep"):
            st = ti.get_index(self.dem).refresh()
        self.assertEqual(st.new, self.N)
        self.assertFalse(os.path.exists(self.stored))
        self.assertEqual(os.listdir(os.path.dirname(self.stored)), [])
        said = [m for m in self.logs if "could not be saved" in m]
        self.assertEqual(len(said), 1, self.logs)
        self.assertIn(self.stored, said[0])
        again = ti.get_index(self.dem)
        ti.expire_all()
        with mock.patch.object(ti, "probe_file", side_effect=AssertionError("probed")):
            self.assertEqual(again.refresh().unchanged, self.N)


# ---------------------------------------------------------------------------
# The cold-scan note (Windows only)
# ---------------------------------------------------------------------------

class _FakeIndex:
    """A folder index whose refresh replays *ticks* and returns a cold scan
    of *n* files."""

    def __init__(self, ticks, n):
        self.ticks, self.n = ticks, n

    def refresh(self, progress=None, should_cancel=None):
        for tick in self.ticks:
            progress(*tick)
        return ti.RefreshStats(files=self.n, new=self.n, changed=0, removed=0,
                               unchanged=0, stat_s=0.1, probe_s=2.0, total_s=2.5,
                               probe_engine="converter", open_ms=86.0)


def _cold_ticks(n, rest=0):
    """A refresh's ticks: list, check, the probe's bulk read of *n* files,
    index, then (*rest* > 0) the in-process read of *rest* files, done."""
    ticks = [(0, 0, "listing"), (n, n, "checking"), (0, n, "reading"),
             (min(256, n), n, "reading"), (n, n, "indexing")]
    if rest:
        ticks += [(0, rest, "reading"), (rest, rest, "reading")]
    return ticks + [(n + rest, n + rest, "done")]


def _flat(text):
    return " ".join(text.split())


#: The QgsSettings stub the plugin actually resolves (pytest may have loaded
#: conftest under a second module name, with its own class).
_SETTINGS = sys.modules["qgis.core"].QgsSettings


class TestColdScanNote(unittest.TestCase):
    FOLDER = os.path.join(os.sep, "terrain", "swissALTI3D")
    MIN = tfs.COLD_SCAN_NOTE_MIN_FILES

    def setUp(self):
        tfs._NOTE_LOGGED.clear()
        _SETTINGS._store.clear()
        self.logs = []
        self.dialog = mock.MagicMock()
        qtcore = sys.modules["qgis.PyQt.QtCore"]
        self._patches = [
            mock.patch.object(tfs.FolderScanThread, "start", lambda thread: thread.run()),
            mock.patch.object(tfs, "QProgressDialog", return_value=self.dialog),
            mock.patch.object(qtcore, "QEventLoop", mock.MagicMock(), create=True),
            mock.patch.object(tfs, "_log", side_effect=self.logs.append),
            mock.patch.object(ta, "get_cache_dir", return_value="/cache/waveshed"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        tfs._NOTE_LOGGED.clear()

    def scan(self, ticks, platform="win32", folder=None):
        """``(labels set on the dialog, the note's log lines)``."""
        self.dialog.reset_mock()
        index = _FakeIndex(ticks, ticks[-1][1])
        start = len(self.logs)
        with mock.patch.object(sys, "platform", platform), \
             mock.patch.object(ti, "get_index", return_value=index):
            result = tfs.scan_folder(None, folder or self.FOLDER)
        self.assertIsNotNone(result)
        labels = [c.args[0] for c in self.dialog.setLabelText.call_args_list]
        self.assertEqual(len(labels), len(ticks))
        notes = [m for m in self.logs[start:] if tfs.COLD_SCAN_NOTE in m]
        return labels, notes

    def test_the_note_joins_the_dialog_when_a_big_bulk_read_starts(self):
        labels, notes = self.scan(_cold_ticks(self.MIN))
        has = ["Defender" in lb for lb in labels]
        # listing, checking: no note; from the first "reading" tick (0 of n)
        # to the end of the scan: the note, as one more paragraph.
        self.assertEqual(has, [False, False, True, True, True, True])
        self.assertTrue(labels[2].startswith(
            f"Reading terrain folder…\nReading new or changed files: 0 / {self.MIN:,}\n\n"),
            labels[2])
        self.assertEqual(_flat(labels[2].split("\n\n", 1)[1]), tfs.COLD_SCAN_NOTE)
        self.assertTrue(all(len(ln) <= 80 for ln in labels[2].splitlines()), labels[2])
        self.assertEqual(tfs.COLD_SCAN_NOTE,
                         "Windows Defender scans every file on first open. For large "
                         "folders, exclude this folder and the Waveshed terrain cache "
                         "from real-time scanning (Windows Security \u2192 Virus & "
                         "threat protection \u2192 Exclusions).")
        self.assertEqual(len(notes), 1, self.logs)
        self.assertIn(self.FOLDER, notes[0])
        self.assertIn("/cache/waveshed", notes[0])

    def test_below_the_threshold_there_is_no_note(self):
        for ticks in (_cold_ticks(self.MIN - 1),
                      # 50,000 files listed and checked, 10 of them read:
                      [(0, 0, "listing"), (50_000, 50_000, "checking"),
                       (0, 10, "reading"), (10, 10, "done")],
                      # two reads, neither of them a bulk read:
                      _cold_ticks(self.MIN - 1, rest=self.MIN - 1)):
            with self.subTest(ticks=ticks[2]):
                labels, notes = self.scan(ticks)
                self.assertFalse([lb for lb in labels if "Defender" in lb], labels)
                self.assertEqual(notes, [])

    def test_an_in_process_bulk_read_gets_it_too(self):
        labels, notes = self.scan(_cold_ticks(10, rest=self.MIN))
        has = ["Defender" in lb for lb in labels]
        self.assertEqual(has, [False] * 5 + [True] * 3)
        self.assertEqual(len(notes), 1)

    def test_never_off_windows(self):
        for platform in ("linux", "darwin"):
            with self.subTest(platform):
                labels, notes = self.scan(_cold_ticks(50_000), platform=platform)
                self.assertFalse([lb for lb in labels if "Defender" in lb], labels)
                self.assertEqual(notes, [])
                with mock.patch.object(sys, "platform", platform):
                    self.assertEqual(tfs.cold_scan_note(50_000, "reading"), "")
        # Nothing but the scan's own summary line.
        self.assertEqual([m for m in self.logs
                          if not m.startswith(f"terrain folder index {self.FOLDER}:")], [])
        with mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(tfs.cold_scan_note(self.MIN, "reading"), tfs.COLD_SCAN_NOTE)
            self.assertEqual(tfs.cold_scan_note(self.MIN - 1, "reading"), "")
            for phase in ("listing", "checking", "indexing", "done"):
                self.assertEqual(tfs.cold_scan_note(50_000, phase), "", phase)

    def test_logged_once_per_folder_shown_in_every_cold_scan(self):
        first = self.scan(_cold_ticks(self.MIN))
        second = self.scan(_cold_ticks(self.MIN))
        other = self.scan(_cold_ticks(self.MIN), folder=os.path.join(os.sep, "other"))
        self.assertEqual([len(first[1]), len(second[1]), len(other[1])], [1, 0, 1])
        for labels, _notes in (first, second, other):
            self.assertTrue(any("Defender" in lb for lb in labels))

    def test_the_module_never_starts_a_process_or_touches_defender(self):
        with open(tfs.__file__, encoding="utf-8") as fh:
            source = fh.read()
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
                imported.update(a.name for a in node.names)
        for name in ("subprocess", "ctypes", "winreg", "QProcess", "codecs", "ntpath"):
            self.assertNotIn(name, imported)
        for needle in ("subprocess", "powershell", "Get-Mp", "Add-MpPreference",
                       "os.system", "os.startfile", "QProcess", "RunAs",
                       "defender_exclusion"):
            self.assertFalse(needle.lower() in source.lower(), needle)
        for gone in ("offer_defender_exclusion", "defender_status",
                     "defender_exclusions", "_powershell", "_queries",
                     "defender_elevated_args", "add_defender_exclusion",
                     "_defender_dialog", "DefenderStatus", "DEFENDER_KEY",
                     "engine_programs", "defender_folders", "_PS_OUTPUT_ENCODING",
                     "_ps_output_encoding", "_off_gui_thread", "_CallThread"):
            self.assertFalse(hasattr(tfs, gone), gone)
        self.assertFalse(hasattr(ti, "_time_opens"))
        # A cold scan on Windows: no process, no setting, one dialog.
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("Popen")), \
             mock.patch.object(subprocess, "run", side_effect=AssertionError("run")), \
             mock.patch.object(os, "system", side_effect=AssertionError("system")):
            labels, notes = self.scan(_cold_ticks(43_650))
        self.assertTrue(notes)
        self.assertEqual(_SETTINGS._store, {})
        self.assertEqual(tfs.QProgressDialog.call_count, 1)


if __name__ == "__main__":
    unittest.main()
