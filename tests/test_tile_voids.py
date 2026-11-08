"""Recorded void counts of pool tiles (aether_converter >= 0.2.11, toolkit
CONTRACT changelog 46).

After a Site Analysis download of 119 tiles of 2 m, building the view read
EVERY pool tile for voids (``_tile_has_voids``: 7.3 GB memory-mapped, 18 s on
the owner's machine, 2026-10-24) for the two that had any. The converter now
prints ``[Tile] <name> voids=<n>`` for each tile it writes (a download's
non-repair pass, after the building burn; an ingest right after the tile's
``covered=`` line, after its void fill); the plugin records the count in the
pool's manifest (``"voids"``, bound to the file's size + mtime) and the view
links a tile recorded void-free without reading it, fills one recorded with
voids without a scan first, and scans only a tile without a bound record —
exactly as before. Pinned here:

* the line and its parser — the ``covered=`` parser never takes it, nor the
  other way round;
* the manifest's optional key: round trip, a manifest without it written as
  before, schema 1 / malformed records read as unknown, the Map Converter's
  own writes keep it;
* ``_sync_view``: known 0 → linked, never read; known > 0 → filled, no scan;
  unknown → scanned, as before; one ``view:`` line per view built;
* ``_view_voids``: this run's record first, the manifest's next, each only
  while bound to the file the view links (a ``.part`` included);
* the download: counts per non-repair pass, a repair pass's tiles unknown
  again, a retry pass's count replacing the first, a tile a pass printed no
  count for unknown (an earlier record dropped), a converter that prints no
  count writing nothing at all; ``remove_pool_tiles`` drops the records of
  what it removed;
* ``prepare_terrain``: download, cache HIT (an earlier run's records), the
  Site Analysis ingest (whole tiles and ``.part`` tiles) and a range profile
  — every view built without one scan once every count is recorded;
* the Map Converter's ingest panel does not log the line;
* the REAL converter >= 0.2.11 (``AETHER_CONVERTER_0211``, else
  AETHER_BIN_DIR's / PATH's when it is one; skipped without): the count it
  prints is the file's, it is recorded, and neither view scans.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import json
import math
import os
import re
import shutil
import struct
import tempfile
import unittest
from unittest import mock

import numpy as np

import waveshed.core.terrain_adapter as ta
from waveshed.core import abt
from waveshed.core import binary_manager as bmod

from _ofm_server import OfmServer
from _real_gdal import HAVE_GDAL, real_gdal, write_tif
from test_terrain_adapter import _fake_probe
from test_two_phase_convert import (_Converter, _Run, _find_real_converter,
                                    _surface, _tiles)

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: Scratch inside the project's gitignored .tmp (never the container drive).
_SCRATCH = os.path.join(_REPO, ".tmp", "test_tile_voids")

#: A terrarium service the downloader knows the encoding of.
_XYZ = ("type=xyz&url=https%3A//s3.amazonaws.com/elevation-tiles-prod/"
        "terrarium/%7Bz%7D/%7Bx%7D/%7By%7D.png&zmax=15")
_WMS = "contextualWMSLegend=0&crs=EPSG:4326&url=https://wms.example/x"
#: 1 km at 250 m around this site: the two 2 deg tiles W and E of 8 E.
SITE = (47.0, 8.0)
RES = 250
WEST, EAST = "tile_N48.00E6.00_250m.abt", "tile_N48.00E8.00_250m.abt"
SIZE = 892                          # their edge in pixels
VOID = -9999                        # the converter's void sentinel (counts)

_VIEW_RE = re.compile(r"^  view: (\d+) tile\(s\) linked from recorded void "
                      r"counts, (\d+) scanned, (\d+) filled \(\d+\.\ds\)$")


def _scratch(test):
    os.makedirs(_SCRATCH, exist_ok=True)
    d = tempfile.mkdtemp(prefix="t_", dir=_SCRATCH)
    test.addCleanup(shutil.rmtree, d, True)
    return d


def _write_tile(path, size=8, ul_lat=47.0, ul_lon=8.0, res_deg=None,
                void_rows=0, value=1200):
    """A whole ``.abt``: every sample *value* counts (600 m), the first
    *void_rows* rows the void sentinel."""
    stride = (size * 2 + 255) & ~255
    res = res_deg or 1.0 / size
    samples = np.full((size, size), value, "<i2")
    samples[:void_rows] = VOID
    body = np.zeros((size, stride), np.uint8)
    body[:, :size * 2] = samples.view(np.uint8).reshape(size, size * 2)
    with open(path, "wb") as fh:
        fh.write(b"AETH" + struct.pack("<HH", 1, size)
                 + struct.pack("<dddd", ul_lat, ul_lon, res, res)
                 + struct.pack("<hH", 0, stride))
        fh.write(body.tobytes())


def _write_spec_tile(path, spec, void_rows=0):
    """*spec*: a subtile (``exact_res_m``) or a download job's tile
    (``resolution_m``)."""
    res_m = spec.get("exact_res_m", spec.get("resolution_m"))
    _write_tile(path, int(spec["size_px"]), spec["ul_lat"], spec["ul_lon"],
                res_m / 111_111.0, void_rows)


def _set_mtime(path, seconds):
    os.utime(path, ns=(seconds * 10 ** 9, seconds * 10 ** 9))


def _samples(path):
    hdr = abt.read_header(path)
    raw = np.fromfile(path, dtype=np.uint8, offset=abt.HEADER_SIZE,
                      count=hdr.size * hdr.stride)
    return raw.reshape(hdr.size, hdr.stride)[:, :hdr.size * 2].view("<i2")


def _view_lines(logged):
    """``(linked from counts, scanned, filled)`` of every ``view:`` line."""
    out = []
    for line in logged:
        m = _VIEW_RE.match(line)
        if m:
            out.append(tuple(int(g) for g in m.groups()))
        elif line.lstrip().startswith("view:"):
            raise AssertionError(f"malformed view line: {line!r}")
    return out


def _no_scan():
    return mock.patch.object(ta, "_tile_has_voids",
                             side_effect=AssertionError("a tile was scanned"))


def _specs():
    bbox = ta._compute_sector_bbox(*SITE, 1.0, 0.0, 360.0)
    specs = ta._compute_subtiles(bbox, RES, *SITE, 1.0, 0.0, 360.0)
    assert [s["filename"] for s in specs] == [WEST, EAST], specs
    return specs


def _xyz_at(zoom, lat, lon):
    n = 2.0 ** zoom
    return (int((lon + 180.0) / 360.0 * n),
            int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi)
                / 2 * n))


# ---------------------------------------------------------------------------
# The line
# ---------------------------------------------------------------------------

class TileVoidsLine(unittest.TestCase):

    def test_parses_name_and_count(self):
        self.assertEqual(
            ta.parse_tile_voids_line("[Tile] tile_N48.00E8.00_250m.abt "
                                     "voids=0"),
            ("tile_N48.00E8.00_250m.abt", 0))
        self.assertEqual(
            ta.parse_tile_voids_line("[Tile] t.abt voids=1234567"),
            ("t.abt", 1234567))
        self.assertEqual(ta.parse_tile_voids_line(
            "stdout: [Tile] my tile.abt voids=5  "), ("my tile.abt", 5))

    def test_the_covered_parser_never_takes_it_nor_the_other_way(self):
        covered, voids = "[Tile] a.abt covered=10/16", "[Tile] a.abt voids=6"
        self.assertEqual(ta.parse_tile_line(covered), ("a.abt", 10, 16))
        self.assertIsNone(ta._TILE_LINE_RE.search(voids))
        self.assertIsNone(ta.parse_tile_line(voids))
        self.assertIsNone(ta.parse_tile_voids_line(covered))

    def test_other_lines_are_not_void_lines(self):
        for line in ("[Tile] a.abt voids=", "[Tile] voids=3",
                     "[Tile] a.abt voids=-1", "[Tile] a.abt voids=3 more",
                     "[Download] 100% (9/9) — 1.0 MB/s, 0 errors",
                     "[Stats] Tiles: 9/9 OK", "", None):
            self.assertIsNone(ta.parse_tile_voids_line(line), line)

    def test_the_counts_of_an_ingest_and_of_a_pass(self):
        lines = ["[Rust] Batch processing 2 tiles...",
                 "[Tile] a.abt covered=16/16", "[Tile] a.abt voids=0",
                 "[Tile] b.abt covered=10/16", "[Tile] b.abt voids=6",
                 "[Rust] Progress: 2/2"]
        self.assertEqual(ta._parse_tile_voids(lines),
                         {"a.abt": 0, "b.abt": 6})
        self.assertEqual(ta._parse_tile_voids([]), {})
        self.assertEqual(ta._parse_tile_voids(None), {})


# ---------------------------------------------------------------------------
# The manifest's optional key
# ---------------------------------------------------------------------------

class ManifestVoidsKey(unittest.TestCase):

    REC = {"voids": 3, "abt_size": 100, "abt_mtime": 5 * 10 ** 9}

    def setUp(self):
        self.dir = _scratch(self)
        self.path = os.path.join(self.dir, ta.TILES_MANIFEST)

    def doc(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_round_trip_with_the_key(self):
        clean = dict(self.REC, voids=0)
        ta.write_tiles_manifest(self.dir, {}, None,
                                voids={"a.abt": self.REC, "b.abt": clean})
        self.assertEqual(self.doc(), {"schema": 2, "tiles": {},
                                      "buildings": {},
                                      "voids": {"a.abt": self.REC,
                                                "b.abt": clean}})
        man = ta.read_tiles_manifest(self.dir)
        self.assertEqual((man.schema, man.tiles, man.buildings), (2, {}, {}))
        self.assertEqual(man.voids, {"a.abt": self.REC, "b.abt": clean})
        # A record dropped; the last one dropped takes the key along.
        ta.write_tiles_manifest(self.dir, {}, None, voids={"a.abt": None})
        self.assertEqual(self.doc()["voids"], {"b.abt": clean})
        ta.write_tiles_manifest(self.dir, {}, None, voids={"b.abt": None})
        self.assertEqual(self.doc(), {"schema": 2, "tiles": {},
                                      "buildings": {}})
        self.assertEqual(ta.read_tiles_manifest(self.dir).voids, {})

    def test_a_manifest_without_records_is_written_as_before(self):
        ta.write_tiles_manifest(self.dir, {"a.abt": "fp-a"},
                                {"a.abt": ta.BUILDINGS_NONE})
        self.assertEqual(self.doc(), {"schema": 2,
                                      "tiles": {"a.abt": "fp-a"},
                                      "buildings": {"a.abt": "none"}})
        man = ta.read_tiles_manifest(self.dir)
        self.assertEqual(man, ta.TilesManifest(2, {"a.abt": "fp-a"},
                                               {"a.abt": "none"}))
        self.assertEqual(man.voids, {})
        self.assertEqual(ta.read_tiles_manifest(
            os.path.join(self.dir, "none")), ta.TilesManifest(0, {}, {}))

    def test_old_and_malformed_records_are_unknown(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"schema": 1, "tiles": {"a.abt": "fp-a"},
                       "voids": {"a.abt": self.REC}}, fh)
        man = ta.read_tiles_manifest(self.dir)
        self.assertEqual((man.schema, man.tiles, man.voids),
                         (1, {"a.abt": "fp-a"}, {}))
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"schema": 2, "tiles": {}, "voids": {
                "ok.abt": self.REC,
                "negative.abt": dict(self.REC, voids=-1),
                "unbound.abt": {"voids": 0},
                "text.abt": dict(self.REC, voids="many"),
                "flat.abt": 0, "null.abt": None}}, fh)
        self.assertEqual(ta.read_tiles_manifest(self.dir).voids,
                         {"ok.abt": self.REC})
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"schema": 2, "tiles": {"a.abt": "fp"},
                       "voids": [1, 2]}, fh)
        man = ta.read_tiles_manifest(self.dir)
        self.assertEqual((man.tiles, man.voids), ({"a.abt": "fp"}, {}))

    def test_the_map_converters_writes_keep_the_records(self):
        import waveshed.gui.map_converter_tab as mct
        ta.write_tiles_manifest(self.dir, {}, None,
                                voids={"a.abt": self.REC, "b.abt": self.REC})
        mct._write_output_manifest(self.dir, {"c.abt": "fp-c"})
        man = ta.read_tiles_manifest(self.dir)
        self.assertEqual(man.tiles, {"c.abt": "fp-c"})
        self.assertEqual(man.voids, {"a.abt": self.REC, "b.abt": self.REC})
        self.assertEqual(mct._read_output_manifest(self.dir),
                         {"c.abt": "fp-c"})
        # A tile whose records are dropped loses its count too.
        ta.write_tiles_manifest(self.dir, {"a.abt": None})
        self.assertEqual(ta.read_tiles_manifest(self.dir).voids,
                         {"b.abt": self.REC})


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------

class ViewFromCounts(unittest.TestCase):

    def setUp(self):
        root = _scratch(self)
        self.pool = os.path.join(root, "pool")
        self.view = os.path.join(root, "view")
        os.makedirs(self.pool)
        self.logged = []
        p = mock.patch.object(ta, "_log", side_effect=self.logged.append)
        p.start()
        self.addCleanup(p.stop)

    def tile(self, name, void_rows=0):
        _write_tile(os.path.join(self.pool, name), void_rows=void_rows)

    def linked(self, name):
        return os.path.samefile(os.path.join(self.view, name),
                                os.path.join(self.pool, name))

    def test_known_void_free_is_linked_without_a_read(self):
        self.tile("a.abt")
        with _no_scan(), mock.patch.object(
                ta, "_fill_view_voids", side_effect=AssertionError("filled")):
            self.assertEqual(ta._sync_view(self.pool, self.view, ["a.abt"],
                                           voids={"a.abt": 0}), [])
        self.assertTrue(self.linked("a.abt"))
        self.assertEqual(_view_lines(self.logged), [(1, 0, 0)])

    def test_known_voids_are_filled_without_a_scan(self):
        self.tile("a.abt", void_rows=4)
        fill = mock.Mock(side_effect=ta._fill_view_voids)
        with _no_scan(), mock.patch.object(ta, "_fill_view_voids", fill):
            self.assertEqual(ta._sync_view(self.pool, self.view, ["a.abt"],
                                           voids={"a.abt": 32}), [])
        fill.assert_called_once_with(os.path.join(self.pool, "a.abt"),
                                     os.path.join(self.view, "a.abt"))
        self.assertFalse(self.linked("a.abt"))
        view = _samples(os.path.join(self.view, "a.abt"))
        self.assertFalse((view <= abt.MIN_VALID_ELEV_COUNTS).any())
        self.assertTrue((_samples(os.path.join(self.pool, "a.abt"))[:4]
                         == VOID).all(), "the pool keeps its voids")
        # The per-tile fill line stays.
        self.assertIn("  view fill: a.abt — 32 void sample(s) set to 0 m (sea "
                      "level) for the engine", self.logged)
        self.assertEqual(_view_lines(self.logged), [(0, 0, 1)])

    def test_unknown_is_scanned_as_before(self):
        self.tile("a.abt", void_rows=2)
        self.tile("b.abt")
        self.tile("c.abt")
        scan = mock.Mock(side_effect=ta._tile_has_voids)
        with mock.patch.object(ta, "_tile_has_voids", scan):
            # None, absent from the map, and no map at all: all unknown.
            self.assertEqual(ta._sync_view(
                self.pool, self.view, ["a.abt", "b.abt"],
                voids={"b.abt": None}), [])
            self.assertEqual(ta._sync_view(
                self.pool, self.view, ["a.abt", "b.abt", "c.abt"]), [])
        self.assertEqual(sorted(os.path.basename(c.args[0])
                                for c in scan.call_args_list),
                         ["a.abt", "b.abt", "c.abt"])
        self.assertFalse(self.linked("a.abt"))
        self.assertTrue(self.linked("b.abt"))
        self.assertTrue(self.linked("c.abt"))
        # The second view had only c to provide (a and b were fresh).
        self.assertEqual(_view_lines(self.logged), [(0, 2, 1), (0, 1, 0)])

    def test_one_line_counts_each_kind_and_a_fresh_view_says_nothing(self):
        self.tile("a.abt")
        self.tile("b.abt", void_rows=1)
        self.tile("c.abt", void_rows=1)
        self.tile("d.abt")
        names = ["a.abt", "b.abt", "c.abt", "d.abt"]
        scan = mock.Mock(side_effect=ta._tile_has_voids)
        with mock.patch.object(ta, "_tile_has_voids", scan):
            self.assertEqual(ta._sync_view(self.pool, self.view, names,
                                           voids={"a.abt": 0, "b.abt": 8}),
                             [])
            self.assertEqual(ta._sync_view(self.pool, self.view, names,
                                           voids={"a.abt": 0, "b.abt": 8}),
                             [])
        self.assertEqual(sorted(os.path.basename(c.args[0])
                                for c in scan.call_args_list),
                         ["c.abt", "d.abt"])
        self.assertEqual(_view_lines(self.logged), [(1, 2, 2)])
        self.assertEqual([self.linked(n) for n in names],
                         [True, False, False, True])

    def test_a_missing_pool_tile_is_reported_whatever_its_count(self):
        with _no_scan():
            self.assertEqual(ta._sync_view(self.pool, self.view, ["a.abt"],
                                           voids={"a.abt": 0}), ["a.abt"])
        self.assertEqual(_view_lines(self.logged), [])


class ViewVoidsResolver(unittest.TestCase):

    def setUp(self):
        root = _scratch(self)
        self.pool = os.path.join(root, "pool")
        os.makedirs(self.pool)
        self.a = os.path.join(self.pool, "a.abt")
        _write_tile(self.a)
        _set_mtime(self.a, 1000)

    def test_a_record_counts_while_it_is_bound_to_the_file(self):
        ta._record_pool_voids(self.pool, {"a.abt": ta._voids_record(self.a,
                                                                     0)})
        self.assertEqual(ta._view_voids(self.pool, ["a.abt", "b.abt"]),
                         {"a.abt": 0, "b.abt": None})
        _set_mtime(self.a, 2000)                 # rewritten since
        self.assertEqual(ta._view_voids(self.pool, ["a.abt"]),
                         {"a.abt": None})
        _write_tile(self.a, size=16)             # other bytes, the old mtime
        _set_mtime(self.a, 1000)
        self.assertEqual(ta._view_voids(self.pool, ["a.abt"]),
                         {"a.abt": None})

    def test_this_runs_record_first_then_the_manifests(self):
        rec = ta._voids_record(self.a, 0)
        ta._record_pool_voids(self.pool, {"a.abt": rec})
        self.assertEqual(ta._view_voids(self.pool, ["a.abt"],
                                        run={"a.abt": dict(rec, voids=7)}),
                         {"a.abt": 7})
        stale = dict(rec, voids=7, abt_mtime=rec["abt_mtime"] + 10 ** 9)
        self.assertEqual(ta._view_voids(self.pool, ["a.abt"],
                                        run={"a.abt": stale}),
                         {"a.abt": 0})
        # No manifest at all: this run's records alone.
        os.remove(os.path.join(self.pool, ta.TILES_MANIFEST))
        self.assertEqual(ta._view_voids(self.pool, ["a.abt"],
                                        run={"a.abt": dict(rec, voids=7)}),
                         {"a.abt": 7})
        self.assertEqual(ta._view_voids(self.pool, ["a.abt"]),
                         {"a.abt": None})

    def test_a_part_backed_tile_counts_only_with_its_own_record(self):
        spec = {"filename": "t.abt", "ul_lat": 47.0, "ul_lon": 8.0,
                "size_px": 8, "exact_res_m": 111_111.0 / 8}
        box = ta._subtile_bbox(spec)
        region = {"west": box["west"], "east": box["west"] + 0.25,
                  "south": box["south"], "north": box["south"] + 0.25}
        part = os.path.join(self.pool, "t.abt" + ta._PART_SUFFIX)
        _write_tile(part)
        _set_mtime(part, 3000)
        self.assertTrue(ta._write_part_meta(self.pool, spec,
                                            [{"bbox": region, "fp": None}]))
        tiles = {"t.abt": (spec, region)}
        # Without a record: unknown — today's scan.
        self.assertEqual(ta._view_voids(self.pool, ["t.abt"], tiles),
                         {"t.abt": None})
        ta._record_pool_voids(self.pool,
                              {"t.abt": ta._voids_record(part, 0)})
        self.assertEqual(ta._view_voids(self.pool, ["t.abt"], tiles),
                         {"t.abt": 0})
        # The view links the .part under the canonical name, unread.
        view = os.path.join(self.pool, "..", "view")
        with _no_scan(), mock.patch.object(ta, "_log"):
            self.assertEqual(ta._sync_view(
                self.pool, view, ["t.abt"], tiles=tiles,
                voids=ta._view_voids(self.pool, ["t.abt"], tiles)), [])
        self.assertTrue(os.path.samefile(os.path.join(view, "t.abt"), part))
        # A record of ANOTHER file (an older complete tile) never counts
        # for the .part the view links.
        whole = os.path.join(self.pool, "t.abt")
        _write_tile(whole)
        _set_mtime(whole, 2000)                   # older: the .part wins
        ta._record_pool_voids(self.pool,
                              {"t.abt": ta._voids_record(whole, 0)})
        self.assertEqual(ta._view_voids(self.pool, ["t.abt"], tiles),
                         {"t.abt": None})


# ---------------------------------------------------------------------------
# The download: what each pass leaves
# ---------------------------------------------------------------------------

class _Passes:
    """``_run_converter_download_once`` stand-in. Pass k (from 1) writes
    every tile of its job whole — ``void_rows[name]`` rows of void — at
    mtime 1000 + 10·k s, then prints the header, ``lines``, one ``[Tile] …
    voids=`` line per ``counts`` entry and ``[Stats] Tiles: <stats> OK``
    (``script[k-1]``, a dict; a tile it does not name gets no line)."""

    def __init__(self, script):
        self.script = list(script)
        self.jobs = []

    def __call__(self, exe, job_file, on_line=None, should_cancel=None,
                 on_start=None):
        with open(job_file, encoding="utf-8") as fh:
            job = json.load(fh)
        self.jobs.append(job)
        k = len(self.jobs)
        step = self.script[k - 1]
        for spec in job["tiles"]:
            path = os.path.join(job["output_dir"], spec["filename"])
            _write_spec_tile(path, spec,
                             step.get("void_rows", {}).get(spec["filename"],
                                                           0))
            _set_mtime(path, 1000 + 10 * k)
        lines = ([f"[Download] z={job['zoom']} tiles=3x3=9 (~1MB) "
                  f"connections=8"] + list(step.get("lines", ()))
                 + [f"[Tile] {name} voids={n}"
                    for name, n in step.get("counts", {}).items()]
                 + [f"[Stats] Tiles: {step.get('stats', '9/9')} OK"])
        for line in lines:
            if on_line is not None:
                on_line(line)
        return 0, lines


class DownloadPassCounts(unittest.TestCase):

    def setUp(self):
        self.cache = _scratch(self)
        self.pool = os.path.join(self.cache, "pool", "p")
        os.makedirs(self.pool)
        self.logged = []
        for p in (mock.patch.object(ta, "get_cache_dir",
                                    return_value=self.cache),
                  mock.patch.object(ta, "_log",
                                    side_effect=self.logged.append),
                  mock.patch.object(ta, "_get_download_max_passes",
                                    return_value=4),
                  mock.patch.object(ta, "_get_download_connections",
                                    return_value=8),
                  mock.patch.object(bmod, "probe_converter_version",
                                    return_value="0.2.11")):
            p.start()
            self.addCleanup(p.stop)
        self.specs = _specs()
        (self.zoom, _), = ta._download_specs_by_zoom(_XYZ, self.specs).items()

    def download(self, script):
        passes = _Passes(script)
        bm = mock.Mock()
        bm.find_binary.return_value = "aether_converter"
        with mock.patch.object(ta, "_run_converter_download_once",
                               side_effect=passes):
            outcome = ta._try_rust_download(_XYZ, self.pool, {}, RES,
                                            self.specs, bm)
        return outcome, passes

    def record(self, name):
        return ta._voids_record(os.path.join(self.pool, name),
                                ta.read_tiles_manifest(self.pool)
                                .voids[name]["voids"])

    def held(self):
        return ta.read_tiles_manifest(self.pool).voids

    def test_a_pass_records_its_counts_bound_to_the_files(self):
        outcome, passes = self.download([{
            "void_rows": {EAST: 3},
            "counts": {WEST: 0, EAST: 3 * SIZE}}])
        self.assertTrue(outcome)
        self.assertEqual(len(passes.jobs), 1)
        held = self.held()
        self.assertEqual({n: r["voids"] for n, r in held.items()},
                         {WEST: 0, EAST: 3 * SIZE})
        for name in (WEST, EAST):
            self.assertEqual(held[name], self.record(name), name)
        self.assertEqual(outcome.voids, held)
        self.assertEqual(ta._view_voids(self.pool, [WEST, EAST],
                                        run=outcome.voids),
                         {WEST: 0, EAST: 3 * SIZE})

    def test_a_repair_pass_leaves_its_tiles_unknown(self):
        # An earlier run's record of WEST, of the file as it was then.
        _write_spec_tile(os.path.join(self.pool, WEST), self.specs[0])
        ta._record_pool_voids(self.pool, {WEST: ta._voids_record(
            os.path.join(self.pool, WEST), 0)})
        x, y = _xyz_at(self.zoom, 47.0, 7.0)          # inside WEST only
        outcome, passes = self.download([
            {"lines": [f"[Degraded] z={self.zoom} x={x} y={y} "
                       f"reason=timeout filled_from=z{self.zoom - 1}"],
             "counts": {WEST: 0, EAST: 0}, "stats": "8/9"},
            {"stats": "1/1"}])                         # the repair pass
        self.assertTrue(outcome)
        self.assertEqual(len(passes.jobs), 2)
        self.assertEqual(passes.jobs[1]["repair_source_tiles"],
                         [[self.zoom, x, y]])
        self.assertEqual([t["filename"] for t in passes.jobs[1]["tiles"]],
                         [WEST])
        # WEST was patched by the repair pass, which counts nothing.
        self.assertEqual(set(self.held()), {EAST})
        self.assertEqual(set(outcome.voids), {EAST})
        self.assertEqual(ta._view_voids(self.pool, [WEST, EAST],
                                        run=outcome.voids),
                         {WEST: None, EAST: 0})

    def test_a_retry_pass_replaces_the_count(self):
        holed = mock.patch.object(
            ta, "_abt_has_holes",
            side_effect=lambda p: os.path.basename(p) == WEST)
        zero = mock.patch.object(ta, "_abt_has_zero_fill", return_value=False)
        with holed, zero:
            outcome, passes = self.download([
                {"void_rows": {WEST: 5}, "counts": {WEST: 5 * SIZE, EAST: 0},
                 "stats": "8/9"},
                {"void_rows": {WEST: 1}, "counts": {WEST: SIZE}}])
        self.assertTrue(outcome)
        self.assertEqual([[t["filename"] for t in j["tiles"]]
                          for j in passes.jobs], [[WEST, EAST], [WEST]])
        self.assertNotIn("repair_source_tiles", passes.jobs[1])
        held = self.held()
        self.assertEqual({n: r["voids"] for n, r in held.items()},
                         {WEST: SIZE, EAST: 0})
        self.assertEqual(held[WEST], self.record(WEST))

    def test_a_tile_without_a_line_is_unknown_and_its_old_record_goes(self):
        _write_spec_tile(os.path.join(self.pool, WEST), self.specs[0])
        ta._record_pool_voids(self.pool, {WEST: ta._voids_record(
            os.path.join(self.pool, WEST), 0)})
        outcome, _passes = self.download([{"counts": {EAST: 0}}])
        self.assertTrue(outcome)
        self.assertEqual(set(self.held()), {EAST})

    def test_a_converter_without_the_line_writes_nothing(self):
        outcome, _passes = self.download([{}])
        self.assertTrue(outcome)
        self.assertIsNone(outcome.voids)
        self.assertEqual(outcome, ta.DownloadOutcome(True))
        self.assertFalse(os.path.exists(os.path.join(self.pool,
                                                     ta.TILES_MANIFEST)))
        self.assertEqual(ta._view_voids(self.pool, [WEST, EAST]),
                         {WEST: None, EAST: None})

    def test_remove_pool_tiles_takes_the_records_along(self):
        self.download([{"counts": {WEST: 0, EAST: 0}}])
        got = ta.remove_pool_tiles(self.pool, [WEST, "never_there.abt"])
        self.assertEqual(got.removed, 1)
        self.assertEqual(set(self.held()), {EAST})


# ---------------------------------------------------------------------------
# prepare_terrain: every route builds its view from the counts
# ---------------------------------------------------------------------------

class _SaCase(unittest.TestCase):

    def setUp(self):
        self.cache = _scratch(self)
        self.logged = []
        for p in (mock.patch.object(ta, "get_cache_dir",
                                    return_value=self.cache),
                  mock.patch.object(ta, "_log",
                                    side_effect=self.logged.append),
                  mock.patch.object(ta, "classify_raster_layer",
                                    return_value="dem")):
            p.start()
            self.addCleanup(p.stop)
        self.bm = mock.Mock()
        self.bm.find_binary.return_value = "aether_converter"

    def layer(self, source=_XYZ):
        layer = mock.Mock()
        layer.source.return_value = source
        layer.name.return_value = "dem"
        layer.crs.return_value = conftest._FakeCRS("EPSG:4326")
        return layer


class XyzViews(_SaCase):

    SCRIPT = [{"void_rows": {EAST: 3}, "counts": {WEST: 0, EAST: 3 * SIZE}}]

    def run_(self, script, scan=False):
        passes = _Passes(script)
        guard = (mock.patch.object(ta, "_tile_has_voids",
                                   side_effect=ta._tile_has_voids)
                 if scan else _no_scan())
        self.logged.clear()
        with mock.patch.object(ta, "_run_converter_download_once",
                               side_effect=passes), guard:
            view = ta.prepare_terrain(self.layer(), *SITE, 1.0, RES, self.bm)
        return view, passes

    def pool(self):
        return ta._pool_dir(_XYZ, ta.buildings_identity())

    def check_view(self, view):
        pool = self.pool()
        self.assertTrue(os.path.samefile(os.path.join(view, WEST),
                                         os.path.join(pool, WEST)))
        self.assertFalse(os.path.samefile(os.path.join(view, EAST),
                                          os.path.join(pool, EAST)))
        self.assertFalse((_samples(os.path.join(view, EAST))
                          <= abt.MIN_VALID_ELEV_COUNTS).any())

    def test_download_then_cache_hit_never_scan(self):
        view, passes = self.run_(self.SCRIPT)
        self.assertEqual(len(passes.jobs), 1)
        self.check_view(view)
        self.assertEqual(_view_lines(self.logged), [(1, 0, 1)])
        self.assertEqual(set(ta.read_tiles_manifest(self.pool()).voids),
                         {WEST, EAST})
        # The next run: every tile pooled — the earlier run's records.
        shutil.rmtree(view)
        view2, passes2 = self.run_([])
        self.assertEqual(passes2.jobs, [])
        self.assertTrue(any("cache HIT" in ln for ln in self.logged))
        self.assertEqual(view2, view)
        self.check_view(view2)
        self.assertEqual(_view_lines(self.logged), [(1, 0, 1)])

    def test_an_older_converter_scans_as_before(self):
        view, _passes = self.run_([{"void_rows": {EAST: 3}}], scan=True)
        self.check_view(view)
        self.assertEqual(_view_lines(self.logged), [(0, 2, 1)])
        self.assertFalse(os.path.exists(os.path.join(self.pool(),
                                                     ta.TILES_MANIFEST)))

    def test_a_profile_links_both_bands_from_the_counts(self):
        # 0-1 km at 30 m (one 0.5 deg tile), 1-3 km at 250 m (one 2 deg
        # tile): each band's view and the profile's own, never a scan.
        site = (47.25, 8.25)
        fine, coarse = "tile_N47.50E8.00_30m.abt", "tile_N48.00E8.00_250m.abt"
        script = [{"counts": {fine: 0}}, {"counts": {coarse: 0}}]
        passes = _Passes(script)
        with mock.patch.object(ta, "_run_converter_download_once",
                               side_effect=passes), _no_scan():
            view = ta.prepare_terrain_profile(
                self.layer(), *site, [(1000.0, 30), (3000.0, RES)], self.bm)
        self.assertEqual([[t["filename"] for t in j["tiles"]]
                          for j in passes.jobs], [[fine], [coarse]])
        self.assertEqual(sorted(n for n in os.listdir(view)
                                if n.endswith(".abt")), [fine, coarse])
        for name in (fine, coarse):
            self.assertTrue(os.path.samefile(
                os.path.join(view, name), os.path.join(self.pool(), name)))
        # Band 1's view, band 2's, the profile's.
        self.assertEqual(_view_lines(self.logged),
                         [(1, 0, 0), (1, 0, 0), (2, 0, 0)])


class _IngestConverter:
    """``run_converter_streaming`` for ``_run_converter``: writes every job's
    tile whole and prints the converter's per-tile pair (``covered=`` then,
    unless *voids* is False, ``voids=0`` — after the job's void fill)."""

    def __init__(self, voids=True):
        self.voids = voids
        self.jobs = []

    def __call__(self, exe, args, on_line, on_start=None, env=None):
        with open(args[args.index("--job-file") + 1], encoding="utf-8") as fh:
            jobs = json.load(fh)
        self.jobs.extend(jobs)
        for job in jobs:
            assert job["void_fill_m"] == 0.0, job
            size = int(job["size_px"])
            _write_tile(job["output_path"], size, job["ul_lat"],
                        job["ul_lon"], job["resolution_m"] / 111_111.0)
            name = os.path.basename(job["output_path"])
            on_line(f"[Tile] {name} covered={size * size}/{size * size}")
            if self.voids:
                on_line(f"[Tile] {name} voids=0")
        on_line(f"[Rust] Progress: {len(jobs)}/{len(jobs)}")
        return 0


class IngestViews(_SaCase):

    def test_a_rendered_source_records_whole_tiles(self):
        conv = _IngestConverter()

        def export(dem_layer, dest, bbox, resolution_m):
            with open(dest, "wb") as fh:
                fh.write(b"\0" * 8)

        with mock.patch.object(ta, "_export_via_qgis", side_effect=export), \
             mock.patch.object(ta, "run_converter_streaming",
                               side_effect=conv), _no_scan():
            view = ta.prepare_terrain(self.layer(_WMS), *SITE, 1.0, RES,
                                      self.bm)
        pool = ta._pool_dir(_WMS, ta.buildings_identity())
        self.assertEqual(sorted(os.path.basename(j["output_path"])
                                for j in conv.jobs), [WEST, EAST])
        held = ta.read_tiles_manifest(pool).voids
        self.assertEqual({n: r["voids"] for n, r in held.items()},
                         {WEST: 0, EAST: 0})
        for name in (WEST, EAST):
            self.assertEqual(held[name], ta._voids_record(
                os.path.join(pool, name), 0))
            self.assertTrue(os.path.samefile(os.path.join(view, name),
                                             os.path.join(pool, name)))
        self.assertEqual(_view_lines(self.logged), [(2, 0, 0)])

    def test_a_folder_source_records_its_part_tiles(self):
        dem = os.path.join(self.cache, "dem")
        os.makedirs(dem)
        open(os.path.join(dem, "covering.tif"), "wb").close()
        conv = _IngestConverter()
        covering = {"west": 0.0, "east": 20.0, "south": 40.0, "north": 50.0}
        probe = mock.patch.object(
            ta.terrain_index, "probe_file",
            side_effect=lambda *_a: _fake_probe(covering))
        with probe, \
             mock.patch.object(ta, "run_converter_streaming",
                               side_effect=conv), _no_scan():
            view = ta.prepare_terrain(self.layer(dem), *SITE, 1.0, RES,
                                      self.bm, terrain_dir=dem)
        pool = ta._pool_dir(dem, ta.buildings_identity())
        held = ta.read_tiles_manifest(pool).voids
        self.assertEqual(set(held), {WEST, EAST})
        for name in (WEST, EAST):
            part = os.path.join(pool, name + ta._PART_SUFFIX)
            self.assertFalse(os.path.exists(os.path.join(pool, name)))
            self.assertEqual(held[name], ta._voids_record(part, 0))
            self.assertTrue(os.path.samefile(os.path.join(view, name), part))
        self.assertEqual(_view_lines(self.logged), [(2, 0, 0)])

    def test_an_ingest_without_the_line_scans_as_before(self):
        conv = _IngestConverter(voids=False)

        def export(dem_layer, dest, bbox, resolution_m):
            with open(dest, "wb") as fh:
                fh.write(b"\0" * 8)

        scan = mock.Mock(side_effect=ta._tile_has_voids)
        with mock.patch.object(ta, "_export_via_qgis", side_effect=export), \
             mock.patch.object(ta, "run_converter_streaming",
                               side_effect=conv), \
             mock.patch.object(ta, "_tile_has_voids", scan):
            ta.prepare_terrain(self.layer(_WMS), *SITE, 1.0, RES, self.bm)
        self.assertEqual(scan.call_count, 2)
        self.assertEqual(_view_lines(self.logged), [(0, 2, 0)])
        self.assertFalse(os.path.exists(os.path.join(
            ta._pool_dir(_WMS, ta.buildings_identity()), ta.TILES_MANIFEST)))


# ---------------------------------------------------------------------------
# The Map Converter's ingest panel
# ---------------------------------------------------------------------------

class _VoidsConverter(_Converter):
    """The two-phase fake, printing the 0.2.11 pair: ``voids=`` after
    every ``covered=`` line."""

    def __call__(self, exe, args, on_line, on_start=None, env=None):
        def tee(line):
            on_line(line)
            tile = ta.parse_tile_line(line)
            if tile is not None:
                on_line(f"[Tile] {tile[0]} voids=0")
        return super().__call__(exe, args, tee, on_start=on_start, env=env)


class MapConverterPanel(unittest.TestCase):

    def test_the_void_line_is_not_logged(self):
        root = _scratch(self)
        out, cache = os.path.join(root, "out"), os.path.join(root, "cache")
        os.makedirs(cache)
        _expected, n = _tiles()
        r = _Run(self, out, cache, converter=_VoidsConverter()).run()
        self.assertEqual(r.errs, [])
        self.assertEqual(r.oks, [out])
        self.assertEqual({f for f in os.listdir(out) if f.endswith(".abt")},
                         set(n.values()))
        self.assertFalse([ln for ln in r.logs if "voids=" in ln], r.logs)


# ---------------------------------------------------------------------------
# The real converter >= 0.2.11
# ---------------------------------------------------------------------------

def _voids_converter():
    """An aether_converter >= 0.2.11 (``[Tile] … voids=``): the one
    ``AETHER_CONVERTER_0211`` names, else AETHER_BIN_DIR's / PATH's when it
    is one; None."""
    for exe in (os.environ.get("AETHER_CONVERTER_0211"),
                _find_real_converter()):
        if (exe and os.path.isfile(exe)
                and bmod.converter_supports_tile_voids(exe)):
            return exe
    return None


#: The 30 m pool tile 47.0-47.5 N, 8.0-8.5 E, and where the stand-in
#: service's terrain ends: every pixel east of it is blank (RGB 0,0,0 =
#: -32768 m, the converter's void) at EVERY zoom, so its parent backfill
#: cannot fill it — the tile has voids there.
TILE_30 = "tile_N47.50E8.00_30m.abt"
VOID_EAST = 8.3


def _terrarium(zoom, x, y, base, void_east=None):
    """One 256² terrarium PNG of the two-phase tests' surface; pixels east
    of *void_east* blank (stdlib PNG encoder)."""
    import zlib
    n = 2.0 ** zoom
    j, i = np.mgrid[0:256, 0:256]
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * (y + (j + 0.5)
                                                          / 256) / n))))
    lon = (x + (i + 0.5) / 256) / n * 360.0 - 180.0
    v = _surface(lat, lon, base) + 32768.0
    rgb = np.dstack([np.floor(v / 256), np.floor(v) % 256,
                     np.floor((v - np.floor(v)) * 256)]).astype(np.uint8)
    if void_east is not None:
        rgb[lon > void_east] = 0
    raw = b"".join(b"\x00" + rgb[r].tobytes() for r in range(256))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 256, 256, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


class RealConverterTileVoids(unittest.TestCase):
    """The REAL converter >= 0.2.11 against the stand-in terrain service:
    the count it prints is the number of void samples in the pool file, it
    is recorded bound to that file, and the view of the download and of the
    next run's cache HIT is built without a scan — linked when void-free,
    filled when not. The ingest side: a local GeoTIFF folder's ``.part``
    tile, recorded void-free (``void_fill_m: 0``) and linked unread."""

    def setUp(self):
        self.exe = _voids_converter()
        if not self.exe:
            self.skipTest("no aether_converter >= 0.2.11 (set "
                          "AETHER_CONVERTER_0211)")
        self.root = _scratch(self)

    def _run(self, layer_source, cache, terrain_dir=None, res=30,
             site=(47.32, 8.42)):
        logged = []
        layer = mock.Mock()
        layer.source.return_value = layer_source
        layer.name.return_value = "terrain"
        bm = mock.Mock()
        bm.find_binary.return_value = self.exe
        with mock.patch.object(ta, "get_cache_dir", return_value=cache), \
             mock.patch.object(ta, "classify_raster_layer",
                               return_value="dem"), \
             mock.patch.object(ta, "_log", side_effect=logged.append), \
             _no_scan():
            view = ta.prepare_terrain(layer, *site, 1.0, res, bm,
                                      terrain_dir=terrain_dir)
            pool = ta._pool_dir(terrain_dir or layer_source,
                                ta.buildings_identity())
        return view, pool, logged

    def _download_twice(self, void_east):
        terrain = (lambda z, x, y: _terrarium(z, x, y, 900.0, void_east))
        cache = os.path.join(self.root, f"cache_{void_east}")
        with OfmServer(terrain=terrain) as srv:
            uri = srv.terrain_uri(zmax=12)
            view, pool, logged = self._run(uri, cache)
            shutil.rmtree(view)
            view2, pool2, logged2 = self._run(uri, cache)
        self.assertEqual((view2, pool2), (view, pool))
        text = "\n".join(logged)
        # The line itself never reaches the panel (one per tile); its count
        # is what the pool manifest records — bound to the file — and it is
        # the converter's floor rule: samples at or below -5000 counts.
        self.assertIsNone(re.search(r"\[Tile\] .* voids=\d+", text),
                          text[-3000:])
        path = os.path.join(pool, TILE_30)
        rec = ta.read_tiles_manifest(pool).voids.get(TILE_30)
        self.assertIsNotNone(rec, text[-3000:])
        n = rec["voids"]
        self.assertEqual(
            n, int((_samples(path) <= abt.MIN_VALID_ELEV_COUNTS).sum()))
        self.assertEqual(rec, ta._voids_record(path, n))
        self.assertTrue(any("cache HIT" in ln for ln in logged2), logged2)
        return n, view, pool, _view_lines(logged), _view_lines(logged2)

    def test_a_tile_with_voids_is_filled_without_a_scan(self):
        n, view, pool, first, again = self._download_twice(VOID_EAST)
        self.assertGreater(n, 0)
        self.assertEqual(first, [(0, 0, 1)])
        self.assertEqual(again, [(0, 0, 1)])
        self.assertFalse((_samples(os.path.join(view, TILE_30))
                          <= abt.MIN_VALID_ELEV_COUNTS).any())
        self.assertTrue((_samples(os.path.join(pool, TILE_30))
                         == VOID).any(), "the pool keeps its voids")

    def test_a_void_free_tile_is_linked_unread(self):
        n, view, pool, first, again = self._download_twice(None)
        self.assertEqual(n, 0)
        self.assertEqual(first, [(1, 0, 0)])
        self.assertEqual(again, [(1, 0, 0)])
        self.assertTrue(os.path.samefile(os.path.join(view, TILE_30),
                                         os.path.join(pool, TILE_30)))

    @unittest.skipUnless(HAVE_GDAL, "needs the real GDAL")
    def test_an_ingest_records_its_part_tile_void_free(self):
        dem = os.path.join(self.root, "dem")
        os.makedirs(dem)
        cache = os.path.join(self.root, "cache_ingest")
        site = (47.25, 8.25)
        with real_gdal():
            write_tif(os.path.join(dem, "dem.tif"),
                      {"west": 8.2, "east": 8.3, "south": 47.2,
                       "north": 47.3}, epsg=4326, value=640.0,
                      size=(120, 120))
            view, pool, logged = self._run(None, cache, terrain_dir=dem,
                                           res=RES, site=site)
        text = "\n".join(logged)
        self.assertIsNone(re.search(r"\[Tile\] .* voids=\d+", text),
                          text[-3000:])                 # read, never shown
        part = os.path.join(pool, EAST + ta._PART_SUFFIX)
        self.assertEqual(
            int((_samples(part) <= abt.MIN_VALID_ELEV_COUNTS).sum()), 0)
        rec = ta.read_tiles_manifest(pool).voids.get(EAST)
        self.assertIsNotNone(rec, text[-3000:])
        self.assertEqual(rec["voids"], 0)              # void_fill_m: 0
        self.assertEqual(rec, ta._voids_record(part, 0))
        self.assertTrue(os.path.samefile(os.path.join(view, EAST), part))
        self.assertEqual(_view_lines(logged), [(1, 0, 0)])


if __name__ == "__main__":
    unittest.main()
