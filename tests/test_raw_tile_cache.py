"""Raw source-tile cache — the plugin side of toolkit CONTRACT changelog 38
(aether_converter 0.2.8), 2026-10-21.

* Every XYZ download job carries ``cache_dir`` = ``<cache>/xyz``: the Map
  Converter worker (through ``ensure_pool_tiles``), Site Analysis (through
  ``prepare_terrain``), retry and repair passes; the directory exists when
  the converter starts, also when it vanished between two passes.
* The disk need prices what a download adds there ("raw tiles", on the
  cache drive): the converter's fetch set (``_fetch_tile_count`` — the
  ``[Download] z=`` total of the real converter, recorded and live) ×
  ``XYZ_SOURCE_TILE_BYTES``. Map Converter label, ``_on_run`` and the
  worker's re-checks before both downloads; Site Analysis ``terrain_plan``,
  ``_disk_refusal`` and ``_on_run``.
* ``cache_entries()`` lists ``<cache>/xyz`` only when it exists;
  ``clear_cache()`` deletes it, never the folder index.
* The Clear Terrain Cache dialog names the downloaded source tiles.
* ``[Download] cache: …`` is logged as is and parsed by nothing.
* The frozen real converter (0.2.7 ignores the field) writes pool tiles
  byte-identical to a job without it, through both tabs' paths.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import json
import math
import os
import random
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import waveshed.core.terrain_adapter as ta
import waveshed.gui.map_converter_tab as mct
import waveshed.gui.settings_dialog as sd
import waveshed.gui.site_analysis_tab as sat
from waveshed.core import binary_manager, run_progress
from waveshed.core import output_estimate as oe
from waveshed.core.job_builder import CoverageParams
from waveshed.gui.map_converter_tab import _LayerEntry, _MapConverterWorker

from test_settings_dialog import _FakeBox, _FakeCheckBox, _new_dialog
from test_two_phase_convert import (_EXT, _HUGE, _XYZ, _Converter, _Run,
                                    _find_real_converter,
                                    _first_band_landed, _tab,
                                    _terrarium_png, _tiles, _xyz_layer)

MIB = 1024 * 1024
RAW = ta.XYZ_SOURCE_TILE_BYTES
RESERVE = oe.DISK_RESERVE_BYTES


def _abt(path, ul_lat, ul_lon, size, res_m):
    """A whole ``.abt``: the 44-byte header over a full-length (sparse)
    body — what ``_tile_is_whole`` accepts."""
    stride = (int(size) * 2 + 255) & ~255
    px = float(res_m) / 111_111.0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"AETH" + struct.pack("<HH", 1, int(size))
                 + struct.pack("<dddd", ul_lat, ul_lon, px, px)
                 + struct.pack("<hH", 0, stride))
        fh.truncate(ta._abt_whole_bytes(int(size)))


def _source_tile_inside(spec, zoom):
    """``(x, y)`` of the source tile under the centre of *spec*."""
    span = spec["size_px"] * spec["resolution_m"] / 111_111.0
    n = 2.0 ** zoom
    lon = spec["ul_lon"] + span / 2
    lat = math.radians(spec["ul_lat"] - span / 2)
    return (int((lon + 180.0) / 360.0 * n),
            int((1 - math.asinh(math.tan(lat)) / math.pi) / 2 * n))


class _Engine:
    """Stand-in for ``run_converter_streaming``. A ``download`` records its
    job file as the converter reads it — and whether ``cache_dir`` existed
    at that moment — writes every output tile whole and reports the pass:
    header, the informational cache line, progress, ``[Stats]``. *mode*
    ``"gap"``: the first pass leaves one output tile unwritten (→ a retry
    pass for it); ``"degraded"``: the first pass reports one degraded
    source tile (→ a repair pass). *drop_raw*: the cache dir is removed
    after each pass (it must be there again for the next). An ``ingest``
    writes its tiles whole with full ``[Tile]`` coverage."""

    CACHE_LINE = "[Download] cache: {hits} of {n} source tiles from cache"

    def __init__(self, mode="ok", drop_raw=False):
        self.mode = mode
        self.drop_raw = drop_raw
        self.downloads = []
        self.ingests = []

    def __call__(self, exe, args, on_line, on_start=None, env=None):
        with open(args[-1], encoding="utf-8") as fh:
            job = json.load(fh)
        if args[0] == "ingest":
            self.ingests.append(job)
            for j in job:
                _abt(j["output_path"], j["ul_lat"], j["ul_lon"],
                     j["size_px"], j["resolution_m"])
                px = j["size_px"] ** 2
                on_line(f"[Tile] {os.path.basename(j['output_path'])} "
                        f"covered={px}/{px}")
            on_line(f"[Rust] Progress: {len(job)}/{len(job)}")
            return 0
        assert args[0] == "download", args
        first = not self.downloads
        rec = {"job": job,
               "existed": os.path.isdir(job.get("cache_dir") or ""),
               "lines": []}
        self.downloads.append(rec)

        def say(line):
            rec["lines"].append(line)
            on_line(line)

        specs, zoom = job["tiles"], job["zoom"]
        repair = job.get("repair_source_tiles")
        total = len(repair) if repair else ta._fetch_tile_count(specs, zoom)
        say(f"[Download] z={zoom} tiles=1x{total}={total} (~1MB) "
            f"connections={job['max_connections']}")
        say(self.CACHE_LINE.format(hits=total // 2, n=total))
        if not repair:
            skip = 1 if first and self.mode == "gap" else 0
            for s in specs[skip:]:
                _abt(os.path.join(job["output_dir"], s["filename"]),
                     s["ul_lat"], s["ul_lon"], s["size_px"],
                     s["resolution_m"])
        say(f"[Download] 100% ({total}/{total}) — 9.9 MB/s, 0 errors, "
            f"0 in-flight")
        ok = total
        if first and self.mode == "gap":
            ok = total - 5
        if first and self.mode == "degraded":
            x, y = _source_tile_inside(specs[0], zoom)
            say(f"[Degraded] z={zoom} x={x} y={y} reason=timeout "
                f"filled_from=z{zoom - 1}")
            say(f"[Stats] DEGRADED: 1 of {total} source tile(s) filled "
                f"from coarser zooms after a failed fetch")
            ok = total - 1
        say(f"[Stats] Tiles: {ok}/{total} OK")
        if self.drop_raw:
            shutil.rmtree(job["cache_dir"], ignore_errors=True)
        return 0


def _bm(exe="/bin/conv"):
    manager = mock.Mock()
    manager.find_binary.return_value = exe
    return manager


def _xyz_dem_layer(uri=_XYZ):
    layer = mock.Mock()
    layer.name.return_value = "terrarium"
    layer.source.return_value = uri
    return layer


#: The job's fields before changelog 38 (no buildings, no repair).
_JOB_FIELDS = {"url_template", "encoding", "output_dir", "tiles", "zoom",
               "max_connections"}

#: A site whose 5 km reach lies inside ONE 30 m tile, N47.50E8.50 — the
#: converter fetches 63 source tiles for it (recorded below).
_SITE = (47.25, 8.75)


# ---------------------------------------------------------------------------
# Every download job names the cache, and the directory is there at launch
# ---------------------------------------------------------------------------

class JobsCarryTheCacheDir(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ws_raw_")
        self.root = self._tmp.name
        self.cache = os.path.join(self.root, "cache")
        os.makedirs(self.cache)
        self.raw = os.path.join(self.cache, "xyz")

    def tearDown(self):
        self._tmp.cleanup()

    def test_map_converter_worker(self):
        """The shipped worker over an XYZ-only stack: its z12 download jobs
        — one per 0.5 deg latitude band (2026-10-23) — name <cache>/xyz,
        which exists when the converter reads each job, and Phase 3
        converts the pool tiles as before, band by band."""
        out = os.path.join(self.root, "out")
        engine = _Engine()
        entry = {"kind": "xyz", "uri": _XYZ, "extent": dict(_EXT)}
        layers = [_LayerEntry(layer_type="raster", source_path=_XYZ,
                              extent=dict(_EXT))]
        worker = _MapConverterWorker(layers, out, [30], [_XYZ])
        errs, oks = [], []
        worker.finished_err.connect(errs.append)
        worker.finished_ok.connect(oks.append)
        self.assertFalse(os.path.exists(self.raw))
        with mock.patch.object(_MapConverterWorker, "_resolve_raster",
                               return_value=entry), \
             mock.patch.object(_MapConverterWorker, "_resolve_buildings",
                               return_value=None), \
             mock.patch.object(mct, "find_binary", return_value="/bin/conv"), \
             mock.patch.object(binary_manager, "find_binary",
                               return_value="/bin/conv"), \
             mock.patch.object(mct, "_plan_cross_check"), \
             mock.patch.object(ta, "get_cache_dir", return_value=self.cache), \
             mock.patch.object(oe, "free_bytes", return_value=_HUGE), \
             mock.patch.object(ta, "run_converter_streaming",
                               side_effect=engine):
            worker.run()
        self.assertEqual(errs, [])
        self.assertEqual(oks, [out])
        self.assertEqual(len(engine.downloads), 2)       # two bands
        for rec in engine.downloads:
            self.assertEqual(rec["job"]["cache_dir"],
                             os.path.abspath(self.raw))
            self.assertTrue(rec["existed"], "cache_dir missing at launch")
            self.assertEqual(set(rec["job"]), _JOB_FIELDS | {"cache_dir"})
            self.assertEqual(rec["job"]["zoom"], 12)
            self.assertEqual(len(rec["job"]["tiles"]), 2)
        # Phase 3 read the four pool tiles the downloads wrote, one batch
        # per band.
        self.assertEqual([len(b) for b in engine.ingests], [2, 2])
        jobs = [job for batch in engine.ingests for job in batch]
        self.assertEqual(len(jobs), 4)
        for job in jobs:
            self.assertEqual(len(job["sources"]), 1)
            self.assertTrue(job["sources"][0]["path"].startswith(
                os.path.join(os.path.abspath(self.cache), "pool")),
                job["sources"])

    def test_site_analysis_prepare_terrain(self):
        """Site Analysis (and Processing): prepare_terrain's XYZ download
        job names the same <cache>/xyz."""
        engine = _Engine()
        with mock.patch.object(ta, "get_cache_dir", return_value=self.cache), \
             mock.patch.object(ta, "classify_raster_layer",
                               return_value="dem"), \
             mock.patch.object(ta, "run_converter_streaming",
                               side_effect=engine):
            view = ta.prepare_terrain(_xyz_dem_layer(), *_SITE, 5.0, 30,
                                      _bm())
            pool = ta._pool_dir(_XYZ)
        self.assertEqual(len(engine.downloads), 1)
        rec = engine.downloads[0]
        self.assertEqual(rec["job"]["cache_dir"], os.path.abspath(self.raw))
        self.assertTrue(rec["existed"], "cache_dir missing at launch")
        self.assertEqual(set(rec["job"]), _JOB_FIELDS | {"cache_dir"})
        self.assertEqual(os.path.abspath(rec["job"]["output_dir"]),
                         os.path.abspath(pool))
        self.assertEqual([t["filename"] for t in rec["job"]["tiles"]],
                         ["tile_N47.50E8.50_30m.abt"])
        self.assertTrue(ta._tile_is_whole(
            os.path.join(view, "tile_N47.50E8.50_30m.abt")))

    def _ensure(self, mode, drop_raw=False):
        specs = [t for _r, t in _tiles()[0]]
        engine = _Engine(mode=mode, drop_raw=drop_raw)
        with mock.patch.object(ta, "get_cache_dir", return_value=self.cache), \
             mock.patch.object(ta, "run_converter_streaming",
                               side_effect=engine), \
             mock.patch.object(ta, "_abt_has_holes", return_value=False), \
             mock.patch.object(ta, "_abt_has_zero_fill", return_value=False), \
             mock.patch.object(binary_manager, "probe_converter_version",
                               return_value="0.2.7"):
            mapping = ta.ensure_pool_tiles(_XYZ, specs, 30,
                                           binary_manager=_bm())
        self.assertEqual(sorted(mapping), sorted(t["filename"] for t in specs))
        return engine

    def test_a_retry_pass_carries_it_and_finds_the_dir_made_again(self):
        engine = self._ensure("gap", drop_raw=True)
        self.assertEqual(len(engine.downloads), 2)
        first, retry = engine.downloads
        self.assertEqual(len(retry["job"]["tiles"]), 1)   # the gapped tile
        for rec in (first, retry):
            self.assertEqual(rec["job"]["cache_dir"],
                             os.path.abspath(self.raw))
            self.assertTrue(rec["existed"],
                            "the pass started without its cache_dir")

    def test_a_repair_pass_carries_it_too(self):
        engine = self._ensure("degraded", drop_raw=True)
        self.assertEqual(len(engine.downloads), 2)
        first, repair = engine.downloads
        self.assertNotIn("repair_source_tiles", first["job"])
        self.assertEqual(len(repair["job"]["repair_source_tiles"]), 1)
        for rec in (first, repair):
            self.assertEqual(rec["job"]["cache_dir"],
                             os.path.abspath(self.raw))
            self.assertTrue(rec["existed"])

    def test_the_cache_follows_the_cache_setting(self):
        other = os.path.join(self.root, "elsewhere")
        with mock.patch.object(ta, "get_cache_dir", return_value=other):
            self.assertEqual(ta.xyz_cache_dir(), os.path.join(other, "xyz"))


# ---------------------------------------------------------------------------
# The fetch set: the converter's own count
# ---------------------------------------------------------------------------

def _grid(ext, res):
    return [t for _r, t in mct._enumerate_tiles(mct._snap_bbox(ext, res),
                                                res)]


_CH = {"south": 47.0, "north": 48.0, "west": 8.0, "east": 9.0}
_ZERO = {"south": -0.5, "north": 0.5, "west": -0.5, "east": 0.5}

#: ``(name, tiles, zoom, total)`` — *total* is the ``[Download] z=… tiles=
#: AxB=<total>`` aether_converter 0.2.7 printed for exactly these specs
#: (recorded 2026-10-21 against a local server). Where it fetches fewer
#: than the bounding rectangle (scattered tiles) the rectangle count
#: (``_source_tile_count``) is given too.
_RECORDED = [
    ("30 m, 4 tiles", lambda: _grid(_CH, [30]), 12, 216, 216),
    ("30 m, N47.50E8.50", lambda: [t for t in _grid(_CH, [30])
                                   if t["filename"]
                                   == "tile_N47.50E8.50_30m.abt"], 12, 63, 63),
    ("30 m, all but N47.50E8.00", lambda: [
        t for t in _grid(_CH, [30])
        if t["filename"] != "tile_N47.50E8.00_30m.abt"], 12, 176, 216),
    ("10 m, 16 tiles", lambda: _grid(_CH, [10]), 14, 3128, 3128),
    ("10 m, two far corners", lambda: [_grid(_CH, [10])[0],
                                       _grid(_CH, [10])[-1]], 14, 432, 3128),
    ("10 m, every 5th tile", lambda: _grid(_CH, [10])[::5], 14, 867, 3128),
    ("2 m + 5 m, one z15 job", lambda: _grid(
        {"south": 47.1, "north": 47.3, "west": 8.1, "east": 8.3}, [2, 5]),
     15, 3128, 3128),
    ("2 m, one tile", lambda: _grid(
        {"south": 47.0, "north": 47.1, "west": 8.0, "east": 8.1}, [2]),
     15, 140, 140),
    ("30 m, Chile", lambda: _grid(
        {"south": -33.6, "north": -33.2, "west": -70.8, "east": -70.4},
        [30]), 13, 672, 672),
    ("90 m, 69-70 N", lambda: _grid(
        {"south": 69.0, "north": 70.0, "west": 20.0, "east": 21.0}, [90]),
     10, 36, 36),
    ("250 m, N48E8", lambda: _grid(
        {"south": 47.3, "north": 47.5, "west": 8.4, "east": 8.6}, [250]),
     9, 24, 24),
    # Tile edges ON the equator / meridian: the strict comparisons decide.
    ("30 m, diagonal at 0/0", lambda: [
        t for t in _grid(_ZERO, [30]) if t["filename"] in (
            "tile_N0.00E-0.50_30m.abt", "tile_N0.50E0.00_30m.abt")],
     13, 288, 576),
    ("90 m, anti-diagonal at 0/0", lambda: [
        t for t in _grid({"south": -1.0, "north": 1.0, "west": -1.0,
                          "east": 1.0}, [90]) if t["filename"] in (
            "tile_N1.00E-1.00_90m.abt", "tile_N0.00E0.00_90m.abt")],
     11, 84, 144),
]


def _brute_force_count(specs, zoom):
    """Independent of the code under test: every tile of the bounding
    rectangle, tested against every spec with the converter's predicate
    (download.rs ``tile_touches_spec``)."""
    n = 2.0 ** zoom

    def lon2tx(lon):
        return int(max(0.0, math.floor((lon + 180.0) / 360.0 * n)))

    def lat2ty(lat):
        return int(max(0.0, math.floor(
            (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi)
            / 2.0 * n)))

    def lon_of(x):
        return x / n * 360.0 - 180.0

    def lat_of(y):
        return math.degrees(math.atan(math.sinh(math.pi
                                                * (1.0 - 2.0 * y / n))))

    boxes = []
    for s in specs:
        span = s["size_px"] * (s["resolution_m"] / 111_111.0)
        boxes.append((s["ul_lon"], s["ul_lon"] + span,
                      s["ul_lat"] - span, s["ul_lat"]))
    x0 = lon2tx(min(b[0] for b in boxes))
    x1 = lon2tx(max(b[1] for b in boxes))
    y0 = lat2ty(max(b[3] for b in boxes))
    y1 = lat2ty(min(b[2] for b in boxes))
    count = 0
    for y in range(y0, y1 + 1):
        north, south = lat_of(y), lat_of(y + 1)
        for x in range(x0, x1 + 1):
            west, east = lon_of(x), lon_of(x + 1)
            if any(west < e and east > w and south < nn and north > s_
                   for w, e, s_, nn in boxes):
                count += 1
    return count


class FetchSetIsTheConvertersOwn(unittest.TestCase):

    def test_recorded_converter_totals(self):
        for name, tiles, zoom, total, rect in _RECORDED:
            with self.subTest(name):
                (z, specs), = ta._download_specs_by_zoom(
                    _XYZ, tiles()).items()
                self.assertEqual(z, zoom)
                self.assertEqual(ta._fetch_tile_count(specs, z), total)
                self.assertEqual(ta._source_tile_count(specs, z), rect)

    def test_equals_a_brute_force_count_on_random_tile_sets(self):
        rng = random.Random(20261021)
        areas = [(_CH, [10]), (_CH, [30, 90]),
                 ({"south": -33.6, "north": -33.0, "west": -71.0,
                   "east": -70.4}, [10]),
                 ({"south": 47.1, "north": 47.4, "west": 8.1, "east": 8.4},
                  [2, 5]),
                 (_ZERO, [10]), (_ZERO, [30])]
        for ext, res in areas:
            tiles = _grid(ext, res)
            for k in (1, 2, 3, len(tiles) // 3, len(tiles)):
                pick = rng.sample(tiles, max(1, k))
                for z, specs in ta._download_specs_by_zoom(_XYZ,
                                                           pick).items():
                    with self.subTest(ext=ext, res=res, k=k, z=z):
                        self.assertEqual(ta._fetch_tile_count(specs, z),
                                         _brute_force_count(specs, z))

    def test_a_spec_one_ulp_west_of_a_tile_edge(self):
        """Its own index rounds up to the tile east of the edge, yet the
        tile west of it is touched: the scan's one tile of slack (the
        converter's) finds it once another spec widens the rectangle."""
        zoom, k = 15, 17000
        edge = k / 2.0 ** zoom * 360.0 - 180.0
        tight = {"ul_lat": 47.1, "ul_lon": math.nextafter(edge, -math.inf),
                 "size_px": 100, "resolution_m": 2.0}
        west = {"ul_lat": 47.3, "ul_lon": edge - 0.05, "size_px": 100,
                "resolution_m": 2.0}
        self.assertEqual(ta._TileGrid(zoom).lon2tx(tight["ul_lon"]), k)
        self.assertEqual(ta._fetch_tile_count([tight, west], zoom), 3)
        self.assertEqual(_brute_force_count([tight, west], zoom), 3)

    def test_raw_tile_bytes_per_zoom_group(self):
        self.assertEqual(ta.XYZ_SOURCE_TILE_BYTES, 200 * 1024)
        self.assertEqual(ta.raw_tile_bytes(_XYZ, []), 0)
        self.assertEqual(ta.raw_tile_bytes(_XYZ, _grid(_CH, [30])), 216 * RAW)
        # Two resolutions on two zooms are two jobs: their fetch sets add.
        both = _grid(_CH, [30]) + _grid(_CH, [10])
        self.assertEqual(sorted(ta._download_specs_by_zoom(_XYZ, both)),
                         [12, 14])
        self.assertEqual(ta.raw_tile_bytes(_XYZ, both), (216 + 3128) * RAW)

    def test_live_converter_total_equals_the_count(self):
        """The converter in AETHER_BIN_DIR prints the same total for
        scattered sets, where the rectangle would overcount (a local server
        that answers 404: nothing to decode; small output tiles only)."""
        exe = _find_real_converter()
        if not exe:
            self.skipTest("aether_converter not provided via "
                          "AETHER_BIN_DIR or PATH")
        import functools
        import http.server
        corners = _grid({"south": 44.0, "north": 48.0, "west": 6.0,
                         "east": 10.0}, [90])
        cases = [("30 m, all but N47.50E8.00", _RECORDED[2][1]),
                 ("90 m, two far corners", lambda: [corners[0],
                                                    corners[-1]])]
        with tempfile.TemporaryDirectory() as root:
            empty = os.path.join(root, "empty")
            os.makedirs(empty)

            class Quiet(http.server.SimpleHTTPRequestHandler):
                def log_message(self, *_a):
                    pass

            server = http.server.ThreadingHTTPServer(
                ("127.0.0.1", 0), functools.partial(Quiet, directory=empty))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            url = (f"http://127.0.0.1:{server.server_address[1]}"
                   "/{z}/{x}/{y}.png")
            checked = 0
            for name, tiles in cases:
                for z, specs in ta._download_specs_by_zoom(
                        _XYZ, tiles()).items():
                    job_file = os.path.join(root, "job.json")
                    with open(job_file, "w") as fh:
                        json.dump({"url_template": url,
                                   "encoding": "terrarium",
                                   "output_dir": os.path.join(root, "out"),
                                   "tiles": specs, "zoom": z,
                                   "max_connections": 16,
                                   "cache_dir": os.path.join(root, "xyz")},
                                  fh)
                    proc = subprocess.run(
                        [exe, "download", "--job-file", job_file],
                        capture_output=True, text=True, timeout=300)
                    headers = [h for h in map(
                        run_progress.parse_download_header,
                        (proc.stdout + proc.stderr).splitlines()) if h]
                    count = ta._fetch_tile_count(specs, z)
                    with self.subTest(name, zoom=z):
                        self.assertEqual(proc.returncode, 0,
                                         proc.stderr[-500:])
                        self.assertEqual(headers, [(z, count)])
                        self.assertLess(count,
                                        ta._source_tile_count(specs, z))
                    checked += 1
                shutil.rmtree(os.path.join(root, "out"), ignore_errors=True)
            self.assertEqual(checked, 2)        # one zoom group per case


# ---------------------------------------------------------------------------
# Map Converter: the disk need has the raw tiles on the cache drive
# ---------------------------------------------------------------------------

class MapConverterDiskItems(unittest.TestCase):

    def test_raw_item_prices_the_fetch_set_of_the_tiles_not_pooled(self):
        expected, n = _tiles()
        entries = [{"kind": "xyz", "uri": _XYZ}]
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(ta, "get_cache_dir", return_value=d):
            raw_dir = os.path.join(d, "xyz")
            items = mct._disk_items([], entries, d, d, download=expected)
            self.assertEqual([i for i in items if i[0] == mct._NEED_RAW],
                             [(mct._NEED_RAW, raw_dir, 216 * RAW)])
            self.assertEqual([i[0] for i in items],
                             [mct._NEED_OUTPUT, mct._NEED_POOL,
                              mct._NEED_RAW, mct._NEED_TEMP])
            # A pooled: B, C and D fetch the converter's 176 source tiles.
            pool = ta._pool_dir(_XYZ)
            a = [t for _r, t in expected if t["filename"] == n["A"]][0]
            _abt(os.path.join(pool, n["A"]), a["ul_lat"], a["ul_lon"],
                 a["size_px"], a["exact_res_m"])
            items = mct._disk_items([], entries, d, d, download=expected)
            self.assertEqual([i for i in items if i[0] == mct._NEED_RAW],
                             [(mct._NEED_RAW, raw_dir, 176 * RAW)])
            # Everything pooled: nothing is fetched, no raw item at all.
            for r, t in expected:
                _abt(os.path.join(pool, t["filename"]), t["ul_lat"],
                     t["ul_lon"], t["size_px"], t["exact_res_m"])
            items = mct._disk_items([], entries, d, d, download=expected)
            self.assertEqual([i for i in items if i[0] == mct._NEED_RAW], [])
            # No download at all (Phase 3's re-check): no raw item.
            self.assertEqual(
                [i for i in mct._disk_items(expected, entries, d, d)
                 if i[0] == mct._NEED_RAW], [])

    def test_two_entries_of_one_service_fetch_once(self):
        expected, _n = _tiles()
        entries = [{"kind": "xyz", "uri": _XYZ},
                   {"kind": "xyz", "uri": _XYZ}]
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(ta, "get_cache_dir", return_value=d):
            items = mct._disk_items([], entries, d, d, download=expected)
        self.assertEqual([i[2] for i in items if i[0] == mct._NEED_RAW],
                         [216 * RAW])

    def test_merged_phases_add_the_raw_tiles(self):
        items = mct._merge_disk_items(
            [(mct._NEED_TEMP, "/t", 5), (mct._NEED_RAW, "/c/xyz", 7)],
            [(mct._NEED_RAW, "/c/xyz", 11), (mct._NEED_OUTPUT, "/o", 1)])
        self.assertEqual(items, [(mct._NEED_OUTPUT, "/o", 1),
                                 (mct._NEED_RAW, "/c/xyz", 18),
                                 (mct._NEED_TEMP, "/t", 5)])


class MapConverterLabelAndRun(unittest.TestCase):
    """The label names the raw tiles; ``_on_run`` refuses a run that fits
    only without them, and starts one that fits with them."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ws_rawmc_")
        self.d = self._tmp.name
        expected, _n = _tiles()
        self.expected = expected
        self.per_tile = ta._abt_whole_bytes(expected[0][1]["size_px"])
        # 4 output tiles + 4 pool tiles; the raw tiles come on top.
        self.without_raw = 8 * self.per_tile
        self.raw = 216 * RAW

    def tearDown(self):
        self._tmp.cleanup()

    def test_label(self):
        with mock.patch.object(ta, "get_cache_dir", return_value=self.d), \
             mock.patch.object(oe, "free_bytes", return_value=_HUGE):
            tab = _tab(os.path.join(self.d, "out"), [_xyz_layer()])
            tab._update_estimate()
            tab.flush_estimate()         # the estimate thread's work, inline
            self.assertIn("output tiles, terrain pool, raw tiles, temporary "
                          "copies", tab._lbl_estimate.text)
            pool = ta._pool_dir(_XYZ)
            for _r, t in self.expected:
                _abt(os.path.join(pool, t["filename"]), t["ul_lat"],
                     t["ul_lon"], t["size_px"], t["exact_res_m"])
            tab._est_cache = {}
            tab._update_estimate()
            tab.flush_estimate()
            self.assertNotIn("raw tiles", tab._lbl_estimate.text)

    def _on_run(self, free):
        with mock.patch.object(ta, "get_cache_dir", return_value=self.d), \
             mock.patch.object(oe, "free_bytes", return_value=free):
            tab = _tab(os.path.join(self.d, "out"), [_xyz_layer()])
            box = mock.Mock()
            with mock.patch.object(mct, "QMessageBox", box), \
                 mock.patch.object(mct, "binaries_warning",
                                   return_value=None), \
                 mock.patch.object(mct, "resolve_sources_with_progress",
                                   return_value=None) as resolve:
                tab._on_run()
        return box, resolve

    def test_refused_when_only_the_raw_tiles_do_not_fit(self):
        box, resolve = self._on_run(self.without_raw + self.raw - 1
                                    + RESERVE)
        box.critical.assert_called_once()
        title, msg = box.critical.call_args[0][1:3]
        self.assertEqual(title, "Not enough disk space")
        self.assertIn("(output tiles, terrain pool, raw tiles, temporary "
                      "copies)", msg)
        resolve.assert_not_called()

    def test_runs_when_the_raw_tiles_fit_too(self):
        box, resolve = self._on_run(self.without_raw + self.raw + RESERVE)
        box.critical.assert_not_called()
        resolve.assert_called_once()


class MapConverterWorkerRechecks(unittest.TestCase):
    """The worker's checks before both downloads count the raw tiles."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ws_rawwk_")
        self.out = os.path.join(self._tmp.name, "out")
        self.cache = os.path.join(self._tmp.name, "cache")
        os.makedirs(self.cache)
        self.expected, self.n = _tiles()
        self.per_tile = ta._abt_whole_bytes(self.expected[0][1]["size_px"])

    def tearDown(self):
        self._tmp.cleanup()

    def test_before_the_download_next_to_phase2(self):
        xyz_only = [{"kind": "xyz", "uri": _XYZ, "extent": dict(_EXT)}]
        fits_without = 8 * self.per_tile + RESERVE
        r = _Run(self, self.out, self.cache, entries=xyz_only,
                 free={"v": fits_without + 216 * RAW - 1}).run()
        self.assertEqual(len(r.errs), 1)
        self.assertIn("Not enough disk space for the terrain download and "
                      "the remaining tiles", r.errs[0])
        self.assertIn("raw tiles", r.errs[0])
        self.assertEqual(r.asked, [])
        r = _Run(self, self.out, self.cache, entries=xyz_only,
                 free={"v": fits_without + 216 * RAW}).run()
        self.assertEqual(r.errs, [])
        self.assertEqual(sorted(r.asked), sorted(self.n.values()))

    def _second_pass(self, free_after_first_download):
        # Files + XYZ: D downloads next to Phase 2, B comes out of Phase 2
        # with a hole and is downloaded in the second pass.
        r = _Run(self, self.out, self.cache,
                 converter=_Converter(coverage={self.n["B"]: (1, 2)}))
        # D's band has landed when Phase 2 ends (the state the re-check
        # prices; the bands download next to Phase 2 since 2026-10-23).
        r.converter.before_phase2 = _first_band_landed(r)

        def disk_fills_up(call_no, _specs, _kw):
            if call_no == 1:
                r.free["v"] = free_after_first_download

        r.on_pool = disk_fills_up
        return r.run()

    def test_before_the_second_download(self):
        # The re-check then: output B and D, pool B, and B's 63 source
        # tiles (aether_converter: "[Download] z=12 tiles=7x9=63").
        fits_without = 3 * self.per_tile + RESERVE
        r = self._second_pass(fits_without + 63 * RAW - 1)
        self.assertEqual(len(r.errs), 1)
        self.assertIn("Not enough disk space for the terrain download and "
                      "the remaining tiles", r.errs[0])
        self.assertIn("raw tiles", r.errs[0])
        self.assertEqual(r.asked, [self.n["D"]])
        shutil.rmtree(self.out, ignore_errors=True)
        r = self._second_pass(fits_without + 63 * RAW)
        self.assertEqual(r.errs, [])
        self.assertEqual(r.asked, [self.n["D"], self.n["B"]])


# ---------------------------------------------------------------------------
# Site Analysis: terrain_plan → _disk_refusal → _on_run
# ---------------------------------------------------------------------------

_SITE_RAW_MB = math.ceil(63 * RAW / MIB)          # 13


class SiteAnalysisPlan(unittest.TestCase):

    def test_terrain_plan_prices_the_missing_xyz_tiles(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(ta, "get_cache_dir", return_value=d):
            plan = ta.terrain_plan(_XYZ, *_SITE, 5.0, 30)
            self.assertEqual(plan["tiles_missing"], 1)
            self.assertEqual(plan["raw_mb"], _SITE_RAW_MB)
            # Pooled: nothing is downloaded, nothing cached.
            pool = ta._pool_dir(_XYZ)
            t = [s for s in _grid(_CH, [30])
                 if s["filename"] == "tile_N47.50E8.50_30m.abt"][0]
            _abt(os.path.join(pool, t["filename"]), t["ul_lat"], t["ul_lon"],
                 t["size_px"], t["exact_res_m"])
            again = ta.terrain_plan(_XYZ, *_SITE, 5.0, 30)
            self.assertEqual((again["tiles_missing"], again["raw_mb"]),
                             (0, 0))
            # A folder is never downloaded: no raw tiles, whatever is missing.
            folder = os.path.join(d, "dem")
            os.makedirs(folder)
            files = ta.terrain_plan(folder, *_SITE, 5.0, 30)
            self.assertEqual(files["tiles_missing"], 1)
            self.assertEqual(files["raw_mb"], 0)

    def test_profile_and_jobs_sum_it(self):
        plans = iter([{"tiles_missing": 1, "download_mb": 6, "raw_mb": 3,
                       "total_mb": 6, "footprints": set(), "bbox": None},
                      {"tiles_missing": 1, "download_mb": 2, "raw_mb": 4,
                       "total_mb": 2, "footprints": set(), "bbox": None}])
        with mock.patch.object(ta, "terrain_plan",
                               side_effect=lambda *a, **k: next(plans)):
            prof = ta.terrain_plan_profile(_XYZ, *_SITE,
                                           [(5000.0, 2), (20000.0, 30)])
        self.assertEqual(prof["raw_mb"], 7)
        # Two sites with a tile each: N47.50E8.50 (63) and N48.00E8.00 (60).
        jobs = [CoverageParams(tx_lat=_SITE[0], tx_lon=_SITE[1],
                               resolution_m=30, max_range_km=5),
                CoverageParams(tx_lat=47.75, tx_lon=8.25, resolution_m=30,
                               max_range_km=5)]
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(ta, "get_cache_dir", return_value=d):
            totals = sat._plan_jobs(jobs, _XYZ, None,
                                    ta.run_region_union(jobs))
        self.assertEqual(totals["raw_mb"], math.ceil(63 * RAW / MIB)
                         + math.ceil(60 * RAW / MIB))


class SiteAnalysisRefusal(unittest.TestCase):

    def _jobs(self):
        p = mock.Mock(model="LOS", max_range_km=5.0, resolution_m=30,
                      az_start=0.0, az_end=360.0)
        return [(p, "Site 1")]

    def test_raw_tiles_add_to_the_cache_drive(self):
        raster = oe.engine_output_bytes("LOS", 5.0, 30)
        with tempfile.TemporaryDirectory() as d:
            fits = raster + (6 + 13) * MIB + RESERVE
            self.assertIsNone(sat._disk_refusal(
                self._jobs(), 6, d, d, free_of=lambda _p: fits, raw_mb=13))
            msg = sat._disk_refusal(self._jobs(), 6, d, d,
                                    free_of=lambda _p: fits - 1, raw_mb=13)
            # Without raw tiles the same disk is enough.
            self.assertIsNone(sat._disk_refusal(
                self._jobs(), 6, d, d, free_of=lambda _p: fits - 1))
        self.assertIn("Not enough disk space for this analysis", msg)
        self.assertIn("(coverage rasters, terrain pool, raw tiles)", msg)

    def test_they_are_judged_on_the_cache_drive(self):
        free = {"out": 100 * oe.DISK_RESERVE_BYTES, "cache": 6 * MIB + RESERVE}

        def volume(path):
            key = "cache" if path.startswith("/x/cache") else "out"
            return key, key.upper() + ":"

        with mock.patch.object(oe, "volume_of", side_effect=volume):
            self.assertIsNone(sat._disk_refusal(
                self._jobs(), 6, "/x/out", "/x/cache",
                free_of=lambda p: free[volume(p)[0]]))
            msg = sat._disk_refusal(self._jobs(), 6, "/x/out", "/x/cache",
                                    free_of=lambda p: free[volume(p)[0]],
                                    raw_mb=1)
        self.assertIn("CACHE: need", msg)
        self.assertIn("(terrain pool, raw tiles)", msg)
        self.assertNotIn("OUT:", msg)


class SiteAnalysisRunWiring(unittest.TestCase):
    """``SiteAnalysisTab._on_run`` over an XYZ layer: the plan's raw tiles
    reach the refusal (refused only because of them) and the log."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ws_rawsa_")
        root = self._tmp.name
        self.cache = os.path.join(root, "cache")
        self.out = os.path.join(root, "out")
        os.makedirs(self.cache)
        self.jobs = [(CoverageParams(tx_lat=_SITE[0], tx_lon=_SITE[1],
                                     resolution_m=30, max_range_km=5,
                                     model="LOS", output_name="site1"),
                      "Site 1")]
        # Coverage raster + the one pool tile as terrain_plan prices it.
        self.without_raw = (oe.engine_output_bytes("LOS", 5, 30)
                            + ta._estimate_abt_disk_mb(
                                [t for t in _grid(_CH, [30]) if t["filename"]
                                 == "tile_N47.50E8.50_30m.abt"]) * MIB)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, free):
        tab = sat.SiteAnalysisTab.__new__(sat.SiteAnalysisTab)
        tab.sites_table = mock.MagicMock()
        tab.sites_table.rowCount.return_value = 1
        tab.alt_table = mock.MagicMock()
        tab.alt_table.rowCount.return_value = 1
        tab._read_sites = lambda: [{"coords": _SITE}]
        tab._selected_dem_layer = lambda: _xyz_dem_layer()
        tab._selected_terrain_dir = lambda: ""
        tab.line_output_dir = mock.Mock(
            text=mock.Mock(return_value=self.out))
        tab._refresh_asset_cache = lambda: None
        tab._build_jobs = lambda: self.jobs
        tab.chk_buildings = mock.Mock(isChecked=mock.Mock(return_value=False))
        tab._launch_worker = mock.Mock()
        box = mock.Mock()
        logged = []
        with mock.patch.object(ta, "get_cache_dir", return_value=self.cache), \
             mock.patch.object(sat, "get_cache_dir",
                               return_value=self.cache), \
             mock.patch.object(sat, "dem_layer_warning", return_value=None), \
             mock.patch.object(sat, "save_output_dir"), \
             mock.patch.object(sat, "setup_warning", return_value=None), \
             mock.patch.object(sat, "antenna_height_error",
                               return_value=None), \
             mock.patch.object(sat, "model_warnings", return_value=[]), \
             mock.patch.object(sat, "output_space_problem",
                               return_value=(None, False)), \
             mock.patch.object(sat, "QMessageBox", box), \
             mock.patch.object(sat.QgsMessageLog, "logMessage",
                               side_effect=lambda m, *a, **k:
                               logged.append(m)), \
             mock.patch.object(oe, "free_bytes", return_value=free):
            tab._on_run()
        return tab, box, logged

    def test_refused_only_because_of_the_raw_tiles(self):
        tab, box, logged = self._run(self.without_raw + RESERVE)
        box.critical.assert_called_once()
        title, msg = box.critical.call_args[0][1:3]
        self.assertEqual(title, "Not enough disk space")
        self.assertIn("(coverage rasters, terrain pool, raw tiles)", msg)
        tab._launch_worker.assert_not_called()
        self.assertTrue(any(m.startswith("terrain plan:") and
                            f"up to ~{_SITE_RAW_MB} MB of downloaded source "
                            "tiles cached" in m for m in logged), logged)

    def test_runs_when_they_fit(self):
        tab, box, _logged = self._run(self.without_raw + _SITE_RAW_MB * MIB
                                      + RESERVE)
        box.critical.assert_not_called()
        tab._launch_worker.assert_called_once()


# ---------------------------------------------------------------------------
# Clear Terrain Cache
# ---------------------------------------------------------------------------

_MD5 = "0123456789abcdef0123456789abcdef"


class _CacheCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ws_rawclr_")
        self.cache = self._tmp.name
        self.raw = os.path.join(self.cache, "xyz")
        self._patches = [
            mock.patch.object(ta, "get_cache_dir", return_value=self.cache),
            mock.patch.object(ta, "_log")]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        ta.terrain_index.forget_indexes()
        self._tmp.cleanup()

    def put(self, *parts, size=10):
        path = os.path.join(self.cache, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"x" * size)
        return path


class CacheEntries(_CacheCase):

    def test_xyz_is_listed_only_when_it_exists_and_last(self):
        pool = os.path.join(self.cache, "pool", _MD5)
        view = os.path.join(self.cache, "views", "f" * 32)
        for d in (pool, view, os.path.join(self.cache, "notes"),
                  os.path.join(self.cache, "pool", "xyz")):
            os.makedirs(d)
        self.assertEqual(ta.cache_entries(), [pool, view])
        with open(self.raw, "w"):           # a FILE named xyz is not ours
            pass
        self.assertEqual(ta.cache_entries(), [pool, view])
        os.remove(self.raw)
        os.makedirs(os.path.join(self.raw, "svc", "z12"))
        self.assertEqual(ta.cache_entries(), [pool, view, self.raw])

    def test_clear_cache_removes_it_and_keeps_the_rest(self):
        tile = self.put("pool", _MD5, "tile_N47.00E8.00_30m.abt", size=1000)
        pack = self.put("xyz", "svc", "z12", "x2139_y1408.pack", size=3000)
        self.put("xyz", "svc", "service.json", size=50)
        kept = [self.put("index", _MD5 + ".json"),
                self.put("logs", "aether_converter-20261021.log"),
                self.put("notes", "keep.txt")]
        self.assertEqual(ta.cache_size_bytes(), 4050)
        self.assertEqual(ta.clear_cache(), (2, 4050))
        self.assertFalse(os.path.exists(tile))
        self.assertFalse(os.path.exists(pack))
        self.assertFalse(os.path.exists(self.raw))
        for path in kept:
            self.assertTrue(os.path.exists(path), path)
        self.assertEqual(ta.cache_entries(), [])


class ClearDialogTexts(unittest.TestCase):

    def test_texts(self):
        text, info = sd.clear_cache_texts(3, 120.4, 2, "/c", raw_mb=30.2)
        self.assertEqual(text, "Delete 3 cached terrain set(s), freeing "
                               "120 MB, of which 30 MB downloaded source "
                               "tiles?")
        self.assertEqual(info, sd.clear_cache_texts(3, 120.4, 2, "/c")[1])
        # Without source tiles: the text it always was.
        self.assertEqual(sd.clear_cache_texts(3, 120.4, 2, "/c")[0],
                         "Delete 3 cached terrain set(s), freeing 120 MB?")
        text, info = sd.clear_cache_texts(0, 30.2, 0, "/c", raw_mb=30.2)
        self.assertEqual(text, "Delete 30 MB of downloaded source tiles?")
        self.assertTrue(info.startswith(
            "/c\n\nThey will be downloaded again when a run needs them."),
            info)


class ClearDialog(_CacheCase):

    def setUp(self):
        super().setUp()
        _FakeBox.boxes, _FakeBox.results = [], []
        _FakeBox.click, _FakeBox.tick = "yes", False
        for p in (mock.patch.object(sd, "QMessageBox", _FakeBox),
                  mock.patch.object(sd, "QCheckBox", _FakeCheckBox)):
            p.start()
            self._patches.append(p)

    def fill(self, sets=True, raw=True):
        tile = (self.put("pool", _MD5, "tile_N47.00E8.00_30m.abt",
                         size=2_000_000) if sets else None)
        pack = (self.put("xyz", "svc", "z12", "x1_y0.pack", size=3_000_000)
                if raw else None)
        idx = [self.put("index", m + ".json")
               for m in (_MD5, "fedcba9876543210fedcba9876543210")]
        return tile, pack, idx

    def test_sets_and_source_tiles(self):
        tile, pack, idx = self.fill()
        _new_dialog()._clear_cache()
        box = _FakeBox.boxes[0]
        self.assertEqual(box.text, "Delete 1 cached terrain set(s), freeing "
                                   "5 MB, of which 3 MB downloaded source "
                                   "tiles?")
        self.assertFalse(os.path.exists(tile))
        self.assertFalse(os.path.exists(self.raw))
        self.assertTrue(all(os.path.exists(p) for p in idx))
        self.assertEqual(_FakeBox.results, [
            ("information", "Clear Terrain Cache",
             "Removed 1 cached terrain set(s) and the downloaded source "
             "tiles, freeing 5 MB.")])

    def test_source_tiles_alone_and_the_index_when_ticked(self):
        _tile, pack, idx = self.fill(sets=False)
        _FakeBox.tick = True
        _new_dialog()._clear_cache()
        self.assertEqual(_FakeBox.boxes[0].text,
                         "Delete 3 MB of downloaded source tiles?")
        self.assertFalse(os.path.exists(self.raw))
        self.assertFalse(any(os.path.exists(p) for p in idx))
        self.assertEqual(_FakeBox.results, [
            ("information", "Clear Terrain Cache",
             "Removed the downloaded source tiles, freeing 3 MB, and 2 "
             "index file(s).")])

    def test_no_deletes_nothing(self):
        tile, pack, idx = self.fill()
        _FakeBox.click = "no"
        _new_dialog()._clear_cache()
        self.assertTrue(all(os.path.exists(p) for p in [tile, pack] + idx))
        self.assertEqual(_FakeBox.results, [])

    def test_an_empty_source_tile_dir_alone_is_nothing(self):
        os.makedirs(self.raw)   # every download makes it; 0.2.7 adds nothing
        _new_dialog()._clear_cache()
        self.assertEqual(_FakeBox.boxes, [])
        self.assertEqual(_FakeBox.results, [
            ("information", "Clear Terrain Cache",
             f"No cached terrain found in\n{self.cache}")])

    def test_source_tiles_that_cannot_be_removed_are_reported(self):
        tile, pack, _idx = self.fill()
        real_rmtree = shutil.rmtree

        def rmtree(path, *a, **kw):
            if path == self.raw:
                raise PermissionError(13, "in use", path)
            return real_rmtree(path, *a, **kw)

        with mock.patch.object(ta.shutil, "rmtree", side_effect=rmtree):
            _new_dialog()._clear_cache()
        self.assertFalse(os.path.exists(tile))
        self.assertTrue(os.path.exists(pack))
        kind, _title, text = _FakeBox.results[0]
        self.assertEqual(kind, "warning")
        self.assertTrue(text.startswith(
            "Removed 1 cached terrain set(s), freeing 2 MB.\nThe rest could "
            "not be deleted"), text)


# ---------------------------------------------------------------------------
# The cache line: logged as is, parsed by nothing
# ---------------------------------------------------------------------------

class TheCacheLine(unittest.TestCase):

    LINE = "[Download] cache: 12 of 40 source tiles from cache"

    def test_logged_as_is_and_kept_with_the_lines(self):
        def runner(exe, args, on_line, on_start=None, env=None):
            for line in ("[Download] z=12 tiles=5x8=40 (~8MB) connections=16",
                         self.LINE,
                         "[Download] 100% (40/40) — 9.9 MB/s, 0 errors, "
                         "0 in-flight",
                         "[Stats] Tiles: 40/40 OK"):
                on_line(line)
            return 0

        logged = []
        with mock.patch.object(ta, "run_converter_streaming",
                               side_effect=runner), \
             mock.patch.object(ta, "_log", side_effect=logged.append):
            rc, lines = ta._run_converter_download_once("conv", "job.json")
        self.assertEqual(rc, 0)
        self.assertIn(f"    {self.LINE}", logged)
        self.assertIn(self.LINE, lines)

    def test_no_parser_takes_it_for_something_else(self):
        line = self.LINE
        self.assertIsNone(run_progress.parse_download_line(line))
        self.assertIsNone(run_progress.parse_download_header(line))
        self.assertIsNone(ta._parse_download_progress(line))
        self.assertEqual(ta._parse_download_completeness([line]), (-1, -1))
        self.assertEqual(ta._parse_download_no_data([line]), 0)
        self.assertEqual(ta._parse_degraded([line]), [])
        self.assertEqual(ta._parse_degraded_count([line]), 0)
        self.assertEqual(ta._download_failure_detail(["a", line, "b"]),
                         "a\n" + line + "\nb")      # the tail, no "cause"
        self.assertIsNone(ta.parse_tile_line(line))

    def test_a_pass_with_it_counts_and_ends_as_before(self):
        events = []

        class Sink:
            def __call__(self, frac, label):
                pass

            def download_event(self, info):
                events.append(dict(info))

        specs = [t for _r, t in _tiles()[0]]
        engine = _Engine()
        logged = []
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(ta, "get_cache_dir", return_value=d), \
             mock.patch.object(ta, "_log", side_effect=logged.append), \
             mock.patch.object(ta, "run_converter_streaming",
                               side_effect=engine):
            outcome = ta._try_rust_download(_XYZ, os.path.join(d, "p"), {},
                                            30, specs, _bm(),
                                            progress_cb=Sink())
        self.assertTrue(outcome)
        cache_line = engine.downloads[0]["lines"][1]
        self.assertTrue(cache_line.startswith("[Download] cache: "))
        self.assertIn(f"    {cache_line}", logged)
        totals = {e["pass_total"] for e in events if e.get("pass_total")}
        self.assertEqual(totals, {216})
        self.assertEqual(events[-1]["event"], "group_end")
        self.assertEqual(events[-1]["done_units"], events[-1]["total_units"])


# ---------------------------------------------------------------------------
# End to end: the frozen real converter ignores the field
# ---------------------------------------------------------------------------

class RealConverterIgnoresTheField(unittest.TestCase):
    """aether_converter from AETHER_BIN_DIR (0.2.7 here: no source-tile
    cache, unknown job fields ignored) and a local terrarium server: the
    pool tile the Map Converter's downloader and Site Analysis'
    prepare_terrain write with ``cache_dir`` in their jobs is byte for
    byte the tile of the same job without it, and the header total is
    ``_fetch_tile_count``'s."""

    NAME = "tile_N48.00E8.00_250m.abt"

    def test_both_tabs_write_the_tiles_of_a_job_without_it(self):
        exe = _find_real_converter()
        if not exe:
            self.skipTest("aether_converter not provided via "
                          "AETHER_BIN_DIR or PATH")
        import functools
        import http.server

        with tempfile.TemporaryDirectory() as root:
            tiles_dir = os.path.join(root, "tiles")
            for x in range(266, 272):               # z9 over 8..10 E
                os.makedirs(os.path.join(tiles_dir, "9", str(x)))
                for y in range(176, 184):            # z9 over 46..48 N
                    with open(os.path.join(tiles_dir, "9", str(x),
                                           f"{y}.png"), "wb") as fh:
                        fh.write(_terrarium_png(9, x, y, 900.0))

            class Quiet(http.server.SimpleHTTPRequestHandler):
                def log_message(self, *_a):
                    pass

            server = http.server.ThreadingHTTPServer(
                ("127.0.0.1", 0),
                functools.partial(Quiet, directory=tiles_dir))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            uri = ("type=xyz&url=http%3A//127.0.0.1%3A"
                   f"{server.server_address[1]}/%7Bz%7D/%7Bx%7D/%7By%7D.png"
                   "&zmax=15&interpretation=terrarium")

            launches = []
            real_run = ta.run_converter_streaming

            def launch(exe_, args, on_line, **kw):
                with open(args[-1], encoding="utf-8") as fh:
                    job = json.load(fh)
                rec = {"job": job,
                       "existed": os.path.isdir(job.get("cache_dir", "")),
                       "lines": []}
                launches.append(rec)

                def tap(line):
                    rec["lines"].append(line)
                    on_line(line)
                return real_run(exe_, args, tap, **kw)

            # The Map Converter's downloader (what its worker calls).
            cache_mc = os.path.join(root, "mc")
            tile = _grid({"south": 47.3, "north": 47.5, "west": 8.4,
                          "east": 8.6}, [250])
            self.assertEqual([t["filename"] for t in tile], [self.NAME])
            with mock.patch.object(ta, "get_cache_dir",
                                   return_value=cache_mc), \
                 mock.patch.object(binary_manager, "find_binary",
                                   return_value=exe), \
                 mock.patch.object(ta, "run_converter_streaming",
                                   side_effect=launch):
                mapping = ta.ensure_pool_tiles(uri, tile, 250)
            with open(mapping[self.NAME], "rb") as fh:
                mc_bytes = fh.read()

            # Site Analysis.
            cache_sa = os.path.join(root, "sa")
            with mock.patch.object(ta, "get_cache_dir",
                                   return_value=cache_sa), \
                 mock.patch.object(ta, "classify_raster_layer",
                                   return_value="dem"), \
                 mock.patch.object(ta, "run_converter_streaming",
                                   side_effect=launch):
                view = ta.prepare_terrain(_xyz_dem_layer(uri), 47.4, 8.5,
                                          5.0, 250, _bm(exe))
                sa_pool = ta._pool_dir(uri)
            with open(os.path.join(sa_pool, self.NAME), "rb") as fh:
                sa_bytes = fh.read()
            with open(os.path.join(view, self.NAME), "rb") as fh:
                view_bytes = fh.read()

            # The same job without cache_dir, straight to the converter.
            self.assertEqual(len(launches), 2)
            ref_job = dict(launches[0]["job"])
            del ref_job["cache_dir"]
            ref_job["output_dir"] = os.path.join(root, "ref")
            os.makedirs(ref_job["output_dir"])
            job_file = os.path.join(root, "ref.json")
            with open(job_file, "w") as fh:
                json.dump(ref_job, fh)
            proc = subprocess.run([exe, "download", "--job-file", job_file],
                                  capture_output=True, text=True, timeout=300)
            self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
            with open(os.path.join(ref_job["output_dir"], self.NAME),
                      "rb") as fh:
                ref_bytes = fh.read()

        for rec, cache in zip(launches, (cache_mc, cache_sa)):
            job = rec["job"]
            self.assertEqual(job["cache_dir"],
                             os.path.abspath(os.path.join(cache, "xyz")))
            self.assertTrue(rec["existed"])
            self.assertEqual(
                {k: v for k, v in job.items()
                 if k not in ("cache_dir", "output_dir")},
                {k: v for k, v in launches[0]["job"].items()
                 if k not in ("cache_dir", "output_dir")})
            headers = [h for h in map(run_progress.parse_download_header,
                                      rec["lines"]) if h]
            self.assertEqual(headers, [(9, ta._fetch_tile_count(
                job["tiles"], job["zoom"]))])
            self.assertIn("[Stats] Tiles: 24/24 OK", " ".join(rec["lines"]))
        self.assertGreater(len(ref_bytes), 44)
        self.assertEqual(mc_bytes, ref_bytes)
        self.assertEqual(sa_bytes, ref_bytes)
        self.assertEqual(view_bytes, ref_bytes)


if __name__ == "__main__":
    unittest.main()
