"""Pre-built ``.abt`` folders: tile selection, header index, cost.

Pinned here (2026-10-23):

* **No holes.** ``select_abt_headers`` is two-pass: the exact step per
  footprint, then — best match first — every other footprint is left out
  only when CHOSEN tiles serve each of its sample points at the step that
  point needs (finer counts). The old single pass dropped a footprint
  whenever other footprints merely HELD its step; on the owner's folder
  (site 47.0568 N 8.4853 E, 10 m to 31.5 km, 30 m to 126 km, 90 m to
  500 km) two such footprints dropped each other: 44 cells of 0.25° got no
  tile — NODATA in the engine, nothing logged. Built from the owner's
  geometry with synthetic headers (no files): the old rule reproduces the
  44 cells, the new one leaves none, and no spurious "coarser" line.
* Only the NEEDED ground counts (round 6): footprint ∩ sector bbox, within
  the range. On the owner's 2/5/10/30/90 m folder (2,463 tiles) a fixed
  10 m / 50 km run took a 2 m, four 30 m and two 90 m tiles beside the 23
  10 m ones, six with a false "needs 10 m, only 30 m / 90 m" — samples
  outside the sector were "uncovered". Now exactly the 10 m tiles; a
  coarser tile only for a true gap (warned), a finer one only where nothing
  else serves (logged); random gapped folders leave no hole.
* Coarser-than-needed substitutions (dialog) vs finer-than-needed (log).
* Uncovered cells are reported with NODATA wording, never "sea level".
* ONE header index per folder (``core/abt_index``): a run lists the folder
  once and opens only new/changed tiles, persisted to
  ``<cache>/index/abt-<md5>.json``; the selection is memoised so the GUI
  pre-check, the coverage warning and the worker share one computation.
* Site Analysis: the pre-built selection runs in the coverage check's
  worker thread; ``is_abt_tile_dir`` once on the GUI thread; a pre-built
  folder prices nothing to build.
"""

# Bootstrap stubs before any plugin import.
import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import json
import math
import os
import random
import shutil
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock

import waveshed.core.terrain_adapter as ta
import waveshed.gui.site_analysis_tab as sat
from waveshed.core import abt, abt_index
from waveshed.core.job_builder import CoverageParams

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: Scratch inside the project's gitignored .tmp (never the container drive).
_SCRATCH = os.path.join(_REPO, ".tmp", "test_abt_selection")

#: The owner's run.
SITE = (47.0568, 8.4853)
BANDS = [(31500.0, 10), (126000.0, 30), (500000.0, 90)]


def _scratch_dir(test: unittest.TestCase) -> str:
    os.makedirs(_SCRATCH, exist_ok=True)
    d = tempfile.mkdtemp(prefix="t_", dir=_SCRATCH)
    test.addCleanup(shutil.rmtree, d, True)
    return d


# ---------------------------------------------------------------------------
# The owner's folder: Map Converter output for S=45.7161 N=48.1913
# W=4.9482 E=11.6466 at 2/5/10/30/90 m — 2,463 tiles, as listed on disk.
# ---------------------------------------------------------------------------

#: resolution -> (first south edge, rows, first west edge, columns)
_OWNER_GRID = {2: (45.7, 25, 4.9, 69), 5: (45.5, 11, 4.75, 28),
               10: (45.5, 11, 4.75, 28), 30: (45.5, 6, 4.5, 15),
               90: (45.0, 4, 4.0, 8)}


def _owner_headers():
    """Synthetic headers of the owner's folder (``_tile_params`` geometry,
    pixel step = exact_res_m / 111111 as ingest.rs writes it), by path."""
    out = []
    for res, (lat0, rows, lon0, cols) in _OWNER_GRID.items():
        ext = ta.ABT_EXTENT_DEG[res]
        for i in range(rows):
            for j in range(cols):
                t = ta._tile_params(ext, round(lat0 + i * ext, 6),
                                    round(lon0 + j * ext, 6), res)
                size = int(t["size_px"])
                out.append(abt.TileHeader(
                    "/owner/" + t["filename"], size, t["ul_lat"], t["ul_lon"],
                    float(t["exact_res_m"]) / 111_111.0,
                    (size * 2 + 255) & ~255))
    return sorted(out, key=lambda h: h.path)


def _rect(h):
    span = h.size * h.pixel_res
    return h.ul_lat, h.ul_lat - span, h.ul_lon, h.ul_lon + span   # n s w e


def _hole_cells(headers, paths, cell=0.25):
    """0.25° cells of the folder extent whose centre lies in a folder tile
    but in none of *paths* — the rasterisation of .tmp/adv/sim_holes.py."""
    rects = [_rect(h) for h in headers]
    chosen = [_rect(h) for h in headers if h.path in set(paths)]
    n = max(r[0] for r in rects)
    s = min(r[1] for r in rects)
    w = min(r[2] for r in rects)
    e = max(r[3] for r in rects)

    def inside(rs, la, lo):
        return any(r[1] <= la <= r[0] and r[2] <= lo <= r[3] for r in rs)

    holes = []
    for i in range(round((n - s) / cell)):
        for j in range(round((e - w) / cell)):
            la, lo = n - (i + 0.5) * cell, w + (j + 0.5) * cell
            if inside(rects, la, lo) and not inside(chosen, la, lo):
                holes.append((round(la, 3), round(lo, 3)))
    return holes


def _old_rule(headers, tx_lat, tx_lon, bands):
    """The single-pass selection before 2026-10-23 (for the fixture check):
    a footprint lacking its step is dropped when other footprints HOLD it."""
    sector = ta._compute_sector_bbox(tx_lat, tx_lon, bands[-1][0] / 1000.0)
    rings, inner = [], 0.0
    for until_m, step in bands:
        rings.append((inner, until_m, step))
        inner = until_m
    groups, needs, rects = {}, {}, {}
    for h in headers:
        n, s_, w, e = _rect(h)
        if (s_ > sector["north"] or n < sector["south"]
                or w > sector["east"] or e < sector["west"]):
            continue
        near, far = ta._rect_distance_range_m(n, s_, w, e, tx_lat, tx_lon)
        steps = [st for lo, hi, st in rings if near <= hi and far >= lo]
        if steps:
            key = (round(n, 6), round(w, 6), round(n - s_, 6))
            groups.setdefault(key, []).append(h)
            needs[key] = min(steps)
            rects[key] = (n, s_, w, e)
    held = {k: {ta._abt_nominal_res(h) for h in hs} for k, hs in groups.items()}
    out = []
    for key in sorted(groups):
        want = needs[key]
        exact = [h for h in groups[key] if ta._abt_nominal_res(h) == want]
        if exact:
            out.append(exact[0].path)
            continue
        n, s_, w, e = rects[key]
        others = [rects[k] for k in groups if k != key and want in held[k]]
        if all(any(o[1] <= s_ + (n - s_) * (i + .5) / 5 <= o[0]
                   and o[2] <= w + (e - w) * (j + .5) / 5 <= o[3]
                   for o in others) for i in range(5) for j in range(5)):
            continue
        out.append(min(groups[key], key=lambda h: abs(
            ta._abt_nominal_res(h) - want)).path)
    return out


class OwnerGeometryHasNoHoles(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.headers = _owner_headers()
        cls.sel = ta.select_abt_headers(cls.headers, *SITE, BANDS)
        cls.names = {os.path.basename(p) for p in cls.sel.paths}

    def test_the_fixture_is_the_owners_folder(self):
        self.assertEqual(len(self.headers), 2463)
        old = _old_rule(self.headers, *SITE, BANDS)
        holes = _hole_cells(self.headers, old)
        self.assertEqual(len(holes), 44, "the old rule's hole set")
        # The two kinds the owner hit: 1 deg N47 E6 vs 0.5 deg 30 m tiles
        # (126 km ring) and 0.5 deg N47 E8.0 vs 0.25 deg tiles (31.5 km).
        self.assertIn((46.375, 6.875), holes)
        self.assertIn((46.625, 8.375), holes)

    def test_the_44_cells_are_gone(self):
        self.assertEqual(_hole_cells(self.headers, self.sel.paths), [])
        self.assertEqual(list(self.sel.uncovered), [])

    def test_the_rings_come_out_covered_by_the_right_tiles(self):
        # 126 km ring: the 1 deg 90 m tiles serve the 90 m ground they hold
        # (the 30 m part is served by finer 0.5 deg tiles).
        for name in ("tile_N47.00E6.00_90m.abt", "tile_N47.00E10.00_90m.abt",
                     "tile_N48.00E6.00_90m.abt", "tile_N48.00E10.00_90m.abt",
                     # 31.5 km ring: the 0.5 deg 30 m tiles beyond 10 m.
                     "tile_N47.00E8.00_30m.abt", "tile_N47.00E8.50_30m.abt"):
            self.assertIn(name, self.names)
        self.assertEqual(self.sel.summary,
                         "10 m × 12 tile(s), 30 m × 36 tile(s), "
                         "90 m × 26 tile(s)")

    def test_no_spurious_substitution_and_nothing_finer_than_needed(self):
        # The old rule said "N46.00 E7.00 (1°): needs 30 m, only 90 m" for
        # tiles whose 30 m part a 0.5 deg tile serves; and taking smaller
        # footprints first would have chosen 330 2 m tiles (19.8 GiB).
        self.assertEqual(self.sel.substitutions, [])
        self.assertEqual(list(self.sel.finer), [])
        self.assertFalse(any(n.endswith("_2m.abt") for n in self.names))

    def test_one_call_is_fast(self):
        t0 = time.perf_counter()
        ta.select_abt_headers(self.headers, *SITE, BANDS)
        self.assertLess(time.perf_counter() - t0, 3.0)


class EveryPointIsServed(unittest.TestCase):
    """Random sites, rings and sectors over the owner's folder: every point
    of a 0.04° grid inside the range and the folder has a chosen tile, and
    the finest chosen tile there is never coarser than the ring needs when
    the folder holds a fine-enough tile at that point."""

    def test_random_runs(self):
        headers = _owner_headers()
        folder = ta._RectBuckets(0.25)
        for h in headers:
            folder.add((_rect(h), ta._abt_nominal_res(h), h.path), _rect(h))
        rng = random.Random(23)
        checked = 0
        for _trial in range(12):
            lat, lon = rng.uniform(45.3, 48.9), rng.uniform(4.2, 11.8)
            k = rng.randint(1, 3)
            steps = sorted(rng.sample([2, 5, 10, 30, 90], k))
            lims = sorted(rng.sample([3000, 8000, 15000, 31500, 60000,
                                      126000, 250000], k))
            bands = [(float(u), s) for u, s in zip(lims, steps)]
            az = (0.0, 360.0) if rng.random() < 0.7 else (
                lambda a: (a, a + rng.uniform(20, 60)))(rng.uniform(0, 290))
            sel = ta.select_abt_headers(headers, lat, lon, bands, *az)
            chosen = set(sel.paths)
            sector = ta._compute_sector_bbox(lat, lon, bands[-1][0] / 1000.0,
                                             *az)
            m_lon = 111_111.0 * math.cos(math.radians(lat))
            la = sector["south"] + 0.02
            while la < sector["north"]:
                lo = sector["west"] + 0.02
                while lo < sector["east"]:
                    d = math.hypot((la - lat) * 111_111.0, (lo - lon) * m_lon)
                    here = [(res, p) for r, res, p in folder.at(la, lo)
                            if r[1] <= la <= r[0] and r[2] <= lo <= r[3]]
                    if d <= bands[-1][0] and here:
                        want = next(s for u, s in bands if d <= u)
                        checked += 1
                        served = [res for res, p in here if p in chosen]
                        self.assertTrue(served, (lat, lon, bands, az, la, lo))
                        if min(res for res, _p in here) <= want:
                            self.assertLessEqual(
                                min(served), want,
                                (lat, lon, bands, az, la, lo))
                    lo += 0.04
                la += 0.04
            self.assertEqual(list(sel.uncovered), [])
        self.assertGreater(checked, 20000, "the check must not be vacuous")


# ---------------------------------------------------------------------------
# Round 6 (owner's log): the Map Converter folder for S45.7143 N48.1412
# W4.8427 E11.6080 at 2/5/10/30/90 m — 2,463 tiles, every step everywhere.
# A FIXED 10 m / 50 km run at 46.5090 N 7.0360 E chose "2 m × 1, 10 m × 23,
# 30 m × 4, 90 m × 2" with six false "needs 10 m, the folder has only 30 m /
# 90 m there" and one "finer than needed" 2 m tile: coverage was judged over
# the WHOLE footprint, outside the sector and past the range too. The 2 m
# tile then set the engine's terrain grid (VRAM 2.8 instead of 1.4 GB).
# ---------------------------------------------------------------------------

_ROUND6_BBOX = {"south": 45.7143, "north": 48.1412, "west": 4.8427,
                "east": 11.6080}
_ROUND6_RES = [2, 5, 10, 30, 90]


def _round6_headers():
    """Synthetic headers of that folder: the Map Converter's own grid
    (epsilon snap, converter >= 0.2.9), header geometry as ingest.rs
    writes it."""
    import waveshed.gui.map_converter_tab as mct
    tiles = mct._enumerate_tiles(
        mct._snap_bbox(_ROUND6_BBOX, _ROUND6_RES, epsilon=True), _ROUND6_RES,
        epsilon=True)
    out = []
    for _res, t in tiles:
        size = int(t["size_px"])
        out.append(abt.TileHeader(
            "/round6/" + t["filename"], size, t["ul_lat"], t["ul_lon"],
            float(t["exact_res_m"]) / 111_111.0, (size * 2 + 255) & ~255))
    return sorted(out, key=lambda h: h.path)


def _sector_tiles(headers, res, site, range_km):
    """Tiles of *res* that meet the sector: inside its bbox and within
    *range_km* of the TX at their nearest point."""
    sector = ta._compute_sector_bbox(*site, range_km)
    out = []
    for h in headers:
        n, s_, w, e = _rect(h)
        if (ta._abt_nominal_res(h) != res or s_ > sector["north"]
                or n < sector["south"] or w > sector["east"]
                or e < sector["west"]):
            continue
        near, _far = ta._rect_distance_range_m(n, s_, w, e, *site)
        if near <= range_km * 1000.0:
            out.append(h.path)
    return sorted(out)


def _served_check(headers, sel, site, bands, az=(0.0, 360.0), step=0.02):
    """Lattice points of the needed ground (sector bbox, within the range)
    the folder has data for: ``(holes, too_coarse)`` — no chosen tile there
    / only chosen tiles coarser than the point's limit (the step its ring
    wants where the folder holds it or finer there, else the finest the
    folder has there)."""
    lat, lon = site
    folder = ta._RectBuckets(0.25)
    for h in headers:
        folder.add((_rect(h), ta._abt_nominal_res(h), h.path), _rect(h))
    chosen = set(sel.paths)
    sector = ta._compute_sector_bbox(lat, lon, bands[-1][0] / 1000.0, *az)
    m_lon = 111_111.0 * math.cos(math.radians(lat))
    holes, coarse = [], []
    la = sector["south"] + step / 2
    while la < sector["north"]:
        lo = sector["west"] + step / 2
        while lo < sector["east"]:
            d = math.hypot((la - lat) * 111_111.0, (lo - lon) * m_lon)
            here = [(res, p) for r, res, p in folder.at(la, lo)
                    if r[1] <= la <= r[0] and r[2] <= lo <= r[3]]
            if d <= bands[-1][0] and here:
                want = next(s for u, s in bands if d <= u)
                best = min(res for res, _p in here)
                limit = want if best <= want else best
                served = [res for res, p in here if p in chosen]
                if not served:
                    holes.append((round(la, 3), round(lo, 3)))
                elif min(served) > limit:
                    coarse.append((round(la, 3), round(lo, 3), want, limit))
            lo += step
        la += step
    return holes, coarse


class Round6FixedRunsTakeTheStepOnly(unittest.TestCase):
    """Coverage is judged over footprint ∩ sector only: where the folder
    has the step for the whole needed ground, exactly that step's tiles —
    no coarser, no finer; a coarser tile only for a true gap (warned), a
    finer one only where nothing else serves (logged)."""

    SITE_50 = (46.5090, 7.0360)
    SITE_150 = (46.7284, 8.8965)

    @classmethod
    def setUpClass(cls):
        cls.headers = _round6_headers()

    def _without(self, resolutions, cell):
        """The folder minus the tiles of *resolutions* reaching into *cell*
        ``(south, north, west, east)``."""
        s0, n0, w0, e0 = cell

        def reaches(h):
            n, s_, w, e = _rect(h)
            return s_ < n0 - 1e-9 and n > s0 + 1e-9 and w < e0 - 1e-9 \
                and e > w0 + 1e-9
        return [h for h in self.headers
                if not (ta._abt_nominal_res(h) in resolutions and reaches(h))]

    def test_the_fixture_is_the_owners_folder(self):
        self.assertEqual(len(self.headers), 2463)
        counts = {}
        for h in self.headers:
            r = ta._abt_nominal_res(h)
            counts[r] = counts.get(r, 0) + 1
        self.assertEqual(counts, {2: 1725, 5: 308, 10: 308, 30: 90, 90: 32})
        # The two kinds the owner's log showed: the 0.5° tile N46.50 E7.50
        # reaches 8.0 E, the 10 m tiles of the sector stop at 7.75 E; the
        # 2 m tile N46.30 E6.40 holds ground inside the bbox but past 50 km.
        sector = ta._compute_sector_bbox(*self.SITE_50, 50.0)
        tens = _sector_tiles(self.headers, 10, self.SITE_50, 50.0)
        east = max(_rect(h)[3] for h in self.headers if h.path in set(tens))
        self.assertAlmostEqual(east, 7.75, places=6)
        self.assertLess(sector["east"], 7.75)
        stray = next(h for h in self.headers
                     if h.path.endswith("tile_N46.30E6.40_2m.abt"))
        n, s_, w, e = _rect(stray)
        near, far = ta._rect_distance_range_m(n, s_, w, e, *self.SITE_50)
        self.assertLess(near, 50_000.0)
        self.assertGreater(far, 50_000.0)

    def test_fixed_10m_50km_takes_exactly_the_10m_tiles(self):
        bands = [(50_000.0, 10)]
        sel = ta.select_abt_headers(self.headers, *self.SITE_50, bands)
        tens = _sector_tiles(self.headers, 10, self.SITE_50, 50.0)
        self.assertEqual(len(tens), 23)
        self.assertEqual(sorted(sel.paths), tens)
        self.assertEqual(sel.summary, "10 m × 23 tile(s)")
        self.assertEqual(sel.substitutions, [])
        self.assertEqual(list(sel.finer), [])
        self.assertEqual(list(sel.uncovered), [])
        self.assertEqual(_served_check(self.headers, sel, self.SITE_50,
                                       bands), ([], []))

    def test_fixed_10m_150km_adds_only_the_true_gap(self):
        bands = [(150_000.0, 10)]
        sel = ta.select_abt_headers(self.headers, *self.SITE_150, bands)
        tens = _sector_tiles(self.headers, 10, self.SITE_150, 150.0)
        self.assertEqual(len(tens), 157)
        self.assertEqual(sorted(p for p in sel.paths
                                if p.endswith("_10m.abt")), tens)
        self.assertEqual(list(sel.finer), [])
        # 150 km reaches 45.38 N; the folder's 10 m (and 30 m) grid starts
        # at 45.5 N and south of it only the 90 m 1° tiles hold ground: a
        # true gap, warned — nothing else besides the 10 m tiles.
        self.assertLess(self.SITE_150[0] - 150_000.0 / 111_111.0, 45.5)
        self.assertEqual(sorted(p for p in sel.paths
                                if not p.endswith("_10m.abt")),
                         ["/round6/tile_N46.00E8.00_90m.abt",
                          "/round6/tile_N46.00E9.00_90m.abt"])
        self.assertEqual(sel.substitutions,
                         ["N46.00 E8.00 (1°): needs 10 m, the folder has "
                          "only 90 m there",
                          "N46.00 E9.00 (1°): needs 10 m, the folder has "
                          "only 90 m there"])
        self.assertEqual(list(sel.uncovered), [])
        self.assertEqual(_served_check(self.headers, sel, self.SITE_150,
                                       bands), ([], []))

    def test_a_true_gap_substitutes_and_warns(self):
        # No 2/5/10 m over the 0.25° cell beside the site: 30 m stands in
        # (warned) — not 90 m as well, and nothing else changes.
        hdrs = self._without({2, 5, 10}, (46.5, 46.75, 7.0, 7.25))
        bands = [(50_000.0, 10)]
        sel = ta.select_abt_headers(hdrs, *self.SITE_50, bands)
        self.assertEqual(sel.summary, "10 m × 22 tile(s), 30 m × 1 tile(s)")
        self.assertIn("/round6/tile_N47.00E7.00_30m.abt", sel.paths)
        self.assertEqual(sel.substitutions,
                         ["N47.00 E7.00 (0.5°): needs 10 m, the folder has "
                          "only 30 m there"])
        self.assertEqual(list(sel.finer), [])
        self.assertEqual(_served_check(hdrs, sel, self.SITE_50, bands),
                         ([], []))

    def test_finer_only_where_nothing_else_serves(self):
        # The 0.25° cell lost its 5 m and 10 m tiles but keeps 2 m: the
        # 2 m tiles reaching into it serve it (logged as finer); no 30 m
        # substitution — the folder has the step or finer there.
        hdrs = self._without({5, 10}, (46.5, 46.75, 7.0, 7.25))
        bands = [(50_000.0, 10)]
        sel = ta.select_abt_headers(hdrs, *self.SITE_50, bands)
        self.assertEqual(sel.summary, "2 m × 9 tile(s), 10 m × 22 tile(s)")
        self.assertEqual(sel.substitutions, [])
        self.assertEqual(len(sel.finer), 9)
        self.assertTrue(all("needs 10 m" in f and "2 m (finer" in f
                            for f in sel.finer), sel.finer)
        self.assertEqual(_served_check(hdrs, sel, self.SITE_50, bands),
                         ([], []))

    def test_selection_stays_fast(self):
        # Container: 8 / 34 / 64-72 ms (12 / 53 / 115 ms before round 6).
        for site, bands in ((self.SITE_50, [(50_000.0, 10)]),
                            (self.SITE_150, [(150_000.0, 10)]),
                            (SITE, BANDS)):
            t0 = time.perf_counter()
            ta.select_abt_headers(self.headers, *site, bands)
            self.assertLess(time.perf_counter() - t0, 1.5, (site, bands))


class GappedFoldersAreServed(unittest.TestCase):
    """Random runs over the round-6 folder with random tiles missing (a
    stopped conversion): no needed ground the folder has data for goes
    without a tile, none gets coarser than its limit, nothing uncovered."""

    def test_random_gaps(self):
        full = _round6_headers()
        rng = random.Random(6)
        checked = 0
        for _trial in range(10):
            drop = rng.choice([0.05, 0.2, 0.5])
            gone = set(rng.sample([2, 5, 10, 30, 90], rng.randint(1, 3)))
            hdrs = [h for h in full if not (ta._abt_nominal_res(h) in gone
                                            and rng.random() < drop)]
            lat, lon = rng.uniform(45.6, 48.3), rng.uniform(4.9, 11.6)
            k = rng.randint(1, 3)
            steps = sorted(rng.sample([2, 5, 10, 30, 90], k))
            lims = sorted(rng.sample([3000, 8000, 15000, 31500, 50000,
                                      126000, 150000], k))
            bands = [(float(u), s) for u, s in zip(lims, steps)]
            az = (0.0, 360.0) if rng.random() < 0.7 else (
                lambda a: (a, a + rng.uniform(20, 90)))(rng.uniform(0, 270))
            sel = ta.select_abt_headers(hdrs, lat, lon, bands, *az)
            holes, coarse = _served_check(hdrs, sel, (lat, lon), bands, az,
                                          step=0.04)
            self.assertEqual((holes, coarse), ([], []),
                             (lat, lon, bands, az, drop, gone))
            self.assertEqual(list(sel.uncovered), [])
            checked += 1
        self.assertEqual(checked, 10)


class TwoFootprintsNoLongerDropEachOther(unittest.TestCase):
    """Minimal 31.5 km-ring case: a 0.5° footprint wanting 10 m holds only
    30 m; the 0.25° footprints beyond the ring want 30 m and hold only 10 m.
    The old rule dropped all of them where the 10 m ring ends."""

    def _hdr(self, name, res, ext, south, west):
        t = ta._tile_params(ext, south, west, res)
        size = int(t["size_px"])
        return abt.TileHeader(f"/f/{name}", size, t["ul_lat"], t["ul_lon"],
                              float(t["exact_res_m"]) / 111_111.0,
                              (size * 2 + 255) & ~255)

    def test_the_far_half_is_served(self):
        hdrs = [self._hdr("half_30m.abt", 30, 0.5, 46.5, 8.0)]
        for i, (s, w) in enumerate([(46.75, 8.0), (46.75, 8.25),
                                    (46.5, 8.0), (46.5, 8.25)]):
            hdrs.append(self._hdr(f"q{i}_10m.abt", 10, 0.25, s, w))
        tx = (47.0568, 8.4853)
        bands = [(31500.0, 10), (60000.0, 30)]
        old = _old_rule(hdrs, *tx, bands)
        self.assertTrue(_hole_cells(hdrs, old), "the old rule leaves a hole")
        sel = ta.select_abt_headers(hdrs, *tx, bands)
        self.assertEqual(_hole_cells(hdrs, sel.paths), [])
        # Beyond 31.5 km the 0.5 deg tile is exactly what the ground needs.
        self.assertIn("/f/half_30m.abt", sel.paths)
        self.assertEqual(sel.substitutions, [])


class SubstitutionKinds(unittest.TestCase):

    def _hdr(self, name, res, ext, south, west):
        t = ta._tile_params(ext, south, west, res)
        size = int(t["size_px"])
        return abt.TileHeader(f"/f/{name}", size, t["ul_lat"], t["ul_lon"],
                              float(t["exact_res_m"]) / 111_111.0,
                              (size * 2 + 255) & ~255)

    def test_finer_than_needed_is_info_not_a_substitution(self):
        # A 30 m run over a folder with only 10 m tiles.
        hdrs = [self._hdr("a_10m.abt", 10, 0.25, 47.0, 8.0)]
        sel = ta.select_abt_headers(hdrs, 47.1, 8.1, [(5000.0, 30)])
        self.assertEqual(sel.paths, ["/f/a_10m.abt"])
        self.assertEqual(sel.substitutions, [])
        self.assertEqual(len(sel.finer), 1)
        self.assertIn("needs 30 m", sel.finer[0])
        self.assertIn("10 m (finer", sel.finer[0])

    def test_coarser_than_needed_is_a_substitution(self):
        hdrs = [self._hdr("a_90m.abt", 90, 1.0, 47.0, 8.0)]
        sel = ta.select_abt_headers(hdrs, 47.5, 8.5, [(5000.0, 30)])
        self.assertEqual(sel.paths, ["/f/a_90m.abt"])
        self.assertEqual(sel.finer, [])
        self.assertEqual(sel.substitutions,
                         ["N48.00 E8.00 (1°): needs 30 m, the folder has "
                          "only 90 m there"])

    def test_closest_wins_and_finer_on_a_tie(self):
        # 0.25 deg footprint with 5 m and 10 m, the ground wants 30 m.
        hdrs = [self._hdr("a_5m.abt", 5, 0.25, 47.0, 8.0),
                self._hdr("a_10m.abt", 10, 0.25, 47.0, 8.0)]
        sel = ta.select_abt_headers(hdrs, 47.1, 8.1, [(5000.0, 30)])
        self.assertEqual(sel.paths, ["/f/a_10m.abt"])


class UncoveredCellsAreNodata(unittest.TestCase):

    def test_cells_a_selection_misses_are_listed(self):
        hdr = abt.TileHeader("/f/a_90m.abt", 1200, 48.0, 8.0, 1.0 / 1200, 2560)
        chooser = ta._AbtChooser([hdr], 47.5, 8.5, [(20000.0, 90)], 0.0, 360.0)
        # Nothing chosen yet: every needed cell of the tile is a hole ...
        cells = ta._abt_uncovered_cells([hdr], chooser)
        self.assertEqual(len(cells), 4)          # 20 km around the centre
        self.assertIn({"north": 47.5, "south": 47.25, "west": 8.25,
                       "east": 8.5}, cells)
        # ... and none once the tile is chosen.
        chooser.pass1()
        self.assertEqual(ta._abt_uncovered_cells([hdr], chooser), [])

    def test_the_coverage_warning_says_nodata_not_sea_level(self):
        root = _scratch_dir(self)
        d = os.path.join(root, "prebuilt")
        os.makedirs(d)
        _write_tile(os.path.join(d, "a_30m.abt"), 30, 64, 48.0, 8.0)
        h = abt.read_header(os.path.join(d, "a_30m.abt"))
        span = h.size * h.pixel_res
        inside = {"north": 48.0 - span * 0.1, "south": 48.0 - span * 0.9,
                  "west": 8.0 + span * 0.1, "east": 8.0 + span * 0.9}
        cell = {"north": 48.0, "south": 47.75, "west": 8.0, "east": 8.25}
        sel = ta.AbtSelection([], [], "", (), [cell])
        with mock.patch.object(ta, "get_cache_dir", return_value=root):
            msg = ta.terrain_coverage_warning(d, inside, abt_selections=[sel])
            self.assertIn("NODATA", msg)
            self.assertIn("N47.75–48.00 E8.00–8.25", msg)
            self.assertNotIn("sea level", msg)
            # Partial coverage of a pre-built folder: NODATA, not 0 m.
            wide = {"north": 48.5, "south": 47.0, "west": 7.5, "east": 9.0}
            msg = ta.terrain_coverage_warning(d, wide)
            self.assertIn("NODATA", msg)
            self.assertNotIn("sea level", msg)


def _bump_dir_mtime(d):
    """Move *d*'s mtime on by 2 s — what adding/removing a file does, made
    certain on filesystems with a coarse mtime clock."""
    st = os.stat(d)
    os.utime(d, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))


def _write_tile(path, res_m, size, ul_lat, ul_lon):
    """A header-only .abt (the selection reads nothing else)."""
    px = res_m / 111_111.0
    stride = (size * 2 + 255) & ~255
    with open(path, "wb") as fh:
        fh.write(struct.pack("<4sHHddddhH", abt.MAGIC, 1, size, ul_lat,
                             ul_lon, px, px, 0, stride) + b"\0" * 16)


class _FolderCase(unittest.TestCase):
    """A small pre-built folder + a private cache dir."""

    def setUp(self):
        self.root = _scratch_dir(self)
        self.cache = os.path.join(self.root, "cache")
        self.dir = os.path.join(self.root, "prebuilt")
        os.makedirs(self.dir)
        for i in range(4):
            _write_tile(os.path.join(self.dir, f"t{i}_30m.abt"), 30, 64,
                        48.0 - 0.02 * i, 8.0)
        for patch in (mock.patch.object(ta, "get_cache_dir",
                                        return_value=self.cache),
                      # persist even this small folder
                      mock.patch.object(abt_index, "PERSIST_MIN_TILES", 1)):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(abt_index.forget)

    def _reads(self):
        return mock.patch.object(abt, "read_header", wraps=abt.read_header)


class HeaderIndex(_FolderCase):

    def test_headers_are_read_once_and_persisted(self):
        with self._reads() as rd:
            first = abt_index.read_folder(self.dir)
            self.assertEqual(rd.call_count, 4)
            abt_index.expire(self.dir)              # a new run: re-checked
            again = abt_index.read_folder(self.dir)
            self.assertEqual(rd.call_count, 4, "unchanged tiles not reopened")
        self.assertEqual(first, again)
        path = abt_index.index_path(self.dir)
        self.assertEqual(os.path.basename(path),
                         "abt-" + ta.terrain_index.folder_key(self.dir) + ".json")
        self.assertEqual(os.path.dirname(path), os.path.join(self.cache, "index"))
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(len(json.load(fh)["entries"]), 4)
        # A new session loads the file and opens nothing.
        abt_index.forget()
        with self._reads() as rd:
            self.assertEqual(abt_index.read_folder(self.dir), first)
            rd.assert_not_called()

    def test_only_changed_tiles_are_reopened(self):
        abt_index.read_folder(self.dir)
        changed = os.path.join(self.dir, "t1_30m.abt")
        _write_tile(changed, 30, 128, 48.5, 8.5)
        os.remove(os.path.join(self.dir, "t3_30m.abt"))
        _write_tile(os.path.join(self.dir, "t9_30m.abt"), 30, 64, 47.0, 8.0)
        _bump_dir_mtime(self.dir)
        with self._reads() as rd:
            got = abt_index.read_folder(self.dir)
        self.assertEqual(sorted(c.args[0] for c in rd.call_args_list),
                         sorted([changed, os.path.join(self.dir, "t9_30m.abt")]))
        self.assertEqual([os.path.basename(h.path) for h in got.headers],
                         ["t0_30m.abt", "t1_30m.abt", "t2_30m.abt",
                          "t9_30m.abt"])
        self.assertEqual(got.headers[1].size, 128)

    def test_a_corrupt_or_missing_index_file_is_an_empty_index(self):
        path = abt_index.index_path(self.dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        for junk in ("{not json", json.dumps([1, 2]),
                     json.dumps({"schema": "abt1", "folder": self.dir,
                                 "entries": {"t0_30m.abt": ["x"]}})):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(junk)
            abt_index.forget()
            with self._reads() as rd:
                self.assertEqual(len(abt_index.read_folder(self.dir).headers), 4)
                self.assertEqual(rd.call_count, 4)

    def test_a_small_folder_is_not_persisted(self):
        with mock.patch.object(abt_index, "PERSIST_MIN_TILES", 5):
            abt_index.read_folder(self.dir)          # 4 tiles
        self.assertFalse(os.path.exists(abt_index.index_path(self.dir)))

    def test_unreadable_tiles_are_listed(self):
        with open(os.path.join(self.dir, "broken.abt"), "wb") as fh:
            fh.write(b"nope")
        got = abt_index.read_folder(self.dir)
        self.assertEqual(got.unreadable, [os.path.join(self.dir, "broken.abt")])
        self.assertEqual(len(got.headers), 4)

    def test_listing_is_memoised_but_sees_added_tiles(self):
        self.assertTrue(ta.is_abt_tile_dir(self.dir))
        with mock.patch.object(abt_index.os, "scandir",
                               wraps=os.scandir) as scan:
            for _ in range(5):
                self.assertEqual(len(ta.list_abt_tiles(self.dir)), 4)
            scan.assert_not_called()
            _write_tile(os.path.join(self.dir, "t5_30m.abt"), 30, 64, 47.5, 8.0)
            _bump_dir_mtime(self.dir)
            self.assertEqual(len(ta.list_abt_tiles(self.dir)), 5)
            self.assertEqual(scan.call_count, 1)

    def test_clear_index_removes_the_header_index_too(self):
        abt_index.read_folder(self.dir)
        path = abt_index.index_path(self.dir)
        self.assertIn(path, ta.index_files())
        ta.clear_index()
        self.assertFalse(os.path.exists(path))
        with self._reads() as rd:
            abt_index.read_folder(self.dir)
            self.assertEqual(rd.call_count, 4, "the memory forgot it too")


class SelectionIsShared(_FolderCase):
    """GUI pre-check, coverage warning and the worker's view: one selection,
    every header opened once."""

    def test_one_computation_per_run(self):
        bands = [(5000.0, 30)]
        with self._reads() as rd, \
             mock.patch.object(ta, "select_abt_headers",
                               wraps=ta.select_abt_headers) as pick, \
             mock.patch.object(ta, "_record_view_dir"):
            sel = ta.select_abt_tiles(self.dir, 47.95, 8.02, bands)
            ta.terrain_coverage_warning(
                self.dir, ta.analysis_bbox(47.95, 8.02, 5.0),
                resolution_m=30, abt_selections=[sel])
            view = ta.prepare_terrain(mock.Mock(), 47.95, 8.02, 5.0, 30,
                                      mock.Mock(), terrain_dir=self.dir)
        self.assertEqual(pick.call_count, 1)
        self.assertEqual(rd.call_count, 4)
        self.assertEqual(sorted(os.listdir(view)),
                         sorted(os.path.basename(p) for p in sel.paths))

    def test_a_changed_folder_is_selected_again(self):
        bands = [(5000.0, 30)]
        with mock.patch.object(ta, "select_abt_headers",
                               wraps=ta.select_abt_headers) as pick:
            ta.select_abt_tiles(self.dir, 47.95, 8.02, bands)
            _write_tile(os.path.join(self.dir, "t7_30m.abt"), 30, 64, 47.9, 8.0)
            _bump_dir_mtime(self.dir)
            ta.select_abt_tiles(self.dir, 47.95, 8.02, bands)
        self.assertEqual(pick.call_count, 2)


class PrebuiltFolderPricesNothing(_FolderCase):

    def test_plan_profile_and_size_warning(self):
        logged = []
        with mock.patch.object(ta, "_log", side_effect=logged.append):
            plan = ta.terrain_plan(self.dir, 47.95, 8.02, 50.0, 30)
            prof = ta.terrain_plan_profile(self.dir, 47.95, 8.02,
                                           [(5000.0, 10), (50000.0, 30)])
            warn = ta.terrain_size_warning(300 * 1024, 0, source=self.dir)
        for p in (plan, prof):
            self.assertTrue(p["prebuilt"])
            for k in ("tiles_total", "tiles_missing", "download_mb",
                      "total_mb", "raw_mb"):
                self.assertEqual(p[k], 0, k)
        self.assertIsNone(warn)
        self.assertTrue(any("pre-built folder — nothing to build" in m
                            for m in logged), logged)
        # Not a pre-built folder: unchanged.
        self.assertIsNotNone(ta.terrain_size_warning(300 * 1024))


class SiteAnalysisPrecheck(unittest.TestCase):
    """``_on_run`` on a pre-built folder: the selection runs in the coverage
    check's worker thread, ``is_abt_tile_dir`` once on the GUI thread, the
    coarser-than-needed dialog from that thread's answer, nothing priced."""

    def setUp(self):
        self.root = _scratch_dir(self)
        self.dir = os.path.join(self.root, "prebuilt")
        os.makedirs(self.dir)
        # One 0.5 deg 30 m tile around the site; the run asks for 10 m.
        t = ta._tile_params(0.5, 47.5, 8.0, 30)
        _write_tile(os.path.join(self.dir, t["filename"]),
                    float(t["exact_res_m"]), int(t["size_px"]),
                    t["ul_lat"], t["ul_lon"])
        patch = mock.patch.object(ta, "get_cache_dir",
                                  return_value=os.path.join(self.root, "cache"))
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(abt_index.forget)

    def _run(self, answers):
        jobs = [(CoverageParams(tx_lat=47.75, tx_lon=8.25, resolution_m=10,
                                max_range_km=4, model="LOS",
                                output_name="site1"), "Site 1")]
        tab = sat.SiteAnalysisTab.__new__(sat.SiteAnalysisTab)
        tab.sites_table = mock.MagicMock()
        tab.sites_table.rowCount.return_value = 1
        tab.alt_table = mock.MagicMock()
        tab.alt_table.rowCount.return_value = 1
        tab._read_sites = lambda: [{"coords": (47.75, 8.25)}]
        tab._selected_dem_layer = lambda: None
        tab._selected_terrain_dir = lambda: self.dir
        tab.line_output_dir = mock.Mock(text=mock.Mock(
            return_value=os.path.join(self.root, "out")))
        tab._refresh_asset_cache = lambda: None
        tab._build_jobs = lambda: jobs
        tab.chk_buildings = mock.Mock(isChecked=mock.Mock(return_value=False))
        tab._launch_worker = mock.Mock()
        box = mock.Mock()
        box.StandardButton = sat.QMessageBox.StandardButton   # the stub enum
        box.warning.side_effect = lambda _p, title, *_a: getattr(
            box.StandardButton, answers.get(title, "No"))
        gui = threading.current_thread()
        where = {"select": [], "is_abt": []}

        def scan_folder(_parent, _folder, after=None, **_kw):
            out = {}
            th = threading.Thread(target=lambda: out.update(extra=after(None)))
            th.start()
            th.join()
            return {"index": None, "stats": None, "extra": out["extra"]}

        real_select, real_is_abt = sat.select_abt_tiles, sat.is_abt_tile_dir

        def select(*a):
            where["select"].append(threading.current_thread())
            return real_select(*a)

        def is_abt(d):
            where["is_abt"].append(threading.current_thread())
            return real_is_abt(d)

        logged = []
        with mock.patch.object(sat, "dem_layer_warning", return_value=None), \
             mock.patch.object(sat, "save_output_dir"), \
             mock.patch.object(sat, "setup_warning", return_value=None), \
             mock.patch.object(sat, "antenna_height_error", return_value=None), \
             mock.patch.object(sat, "model_warnings", return_value=[]), \
             mock.patch.object(sat, "output_space_problem",
                               return_value=(None, False)), \
             mock.patch.object(sat, "_disk_refusal", return_value=None), \
             mock.patch.object(sat, "QMessageBox", box), \
             mock.patch.object(sat.terrain_folder_scan, "scan_folder",
                               side_effect=scan_folder), \
             mock.patch.object(sat, "_plan_jobs") as plan, \
             mock.patch.object(sat, "select_abt_tiles", side_effect=select), \
             mock.patch.object(sat, "is_abt_tile_dir", side_effect=is_abt), \
             mock.patch.object(sat.QgsMessageLog, "logMessage",
                               side_effect=lambda m, *a, **k: logged.append(m)):
            tab._on_run()
        titles = [c.args[1] for c in box.warning.call_args_list]
        return tab, titles, where, gui, plan, logged

    def test_selection_off_the_gui_thread_and_the_dialog_from_it(self):
        tab, titles, where, gui, plan, logged = self._run(
            {"Incomplete terrain coverage": "Yes"})
        self.assertEqual(len(where["select"]), 1)
        self.assertIsNot(where["select"][0], gui)
        self.assertEqual(where["is_abt"], [gui])
        plan.assert_not_called()
        # The 30 m-for-10 m mismatch: the resolution warning, then the
        # coarser-than-needed substitution dialog.
        self.assertEqual(titles, ["Incomplete terrain coverage",
                                  "Terrain resolution not available"])
        tab._launch_worker.assert_not_called()       # answered "No"

    def test_yes_runs_and_nothing_is_priced(self):
        tab, titles, _where, _gui, plan, logged = self._run(
            {"Incomplete terrain coverage": "Yes",
             "Terrain resolution not available": "Yes"})
        tab._launch_worker.assert_called_once()
        plan.assert_not_called()
        self.assertIn("terrain plan: pre-built folder — nothing to build",
                      logged)
        self.assertFalse(any(m.startswith("terrain plan:") and "to build (~"
                             in m for m in logged), logged)


if __name__ == "__main__":
    unittest.main()
